"""
Prediction database — append-only store for APEX reasoning agent recommendations.

Each row represents one horizon prediction for one ticker on one date.
Most days only a 5d prediction is generated; 21d on Mondays; 63d on month-start.

Schema (predictions table):
  ticker, created_at, as_of_date,
  horizon_days, prediction_type, evaluation_date,
  predicted_direction, predicted_return_low, predicted_return_high, conviction_score,
  prediction, recommendation, confidence, target_price, horizon (legacy),
  fundamental_score, research_score, macro_score, news_score, composite_score,
  risk_segment, reasoning, reasoning_text, panel_summary, data_sources,
  previous_prediction, changed_from_previous, model_name, model_provider,
  pt_mean, pt_median, pt_high, pt_low, pt_num_analysts, pt_current_price,
  actual_return, actual_direction, outcome, error_magnitude, evaluated_at,
  guardrail_flags (deterministic post-hoc caps applied before storage, e.g.
  'no_news_bullish_capped', 'bounce_thesis_capped'),
  prediction_role (champion | fallback | challenger | legacy_random | interactive —
  see ROLE_* below; challenger rows are evaluated but never shown to users),
  instrument_type (domain.InstrumentType of the ticker when the forecast was made; NULL = before this column,
  shown as "Unclassified"), leverage_factor (1.0 normal, 2.0 / 3.0 leveraged, negative inverse — a column rather
  than reasoning metadata so validation can filter and group on it)
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Optional
from zoneinfo import ZoneInfo

_CST = ZoneInfo("America/Chicago")


def _now_cst() -> str:
    """Current CST/CDT datetime as ISO string."""
    return datetime.now(_CST).isoformat()


def _today_cst() -> str:
    """Current CST/CDT date as ISO date string (YYYY-MM-DD)."""
    return datetime.now(_CST).date().isoformat()

from portfolio_agent.domain import Prediction
from portfolio_agent.tools.db import db_conn, migrate_columns

CURRENT_SYSTEM_VERSION = "v1.0"

# ── Scope of a forecast row ───────────────────────────────────────────────────
# shared_market        built by scheduled generation from a market-only context
#                      (portfolio_agent.tools.market_context) — may feed prompts,
#                      shared history and failure-pattern mining.
# private              produced for one person (chat questions, personalized
#                      root-agent results) — never enters shared inputs.
# legacy_unclassified  rows written before scopes existed. Their inputs cannot be
#                      proven market-only, so they are NOT shared. They remain
#                      readable through the restricted legacy path so existing
#                      historical evaluation keeps working.
SCOPE_SHARED = "shared_market"
SCOPE_PRIVATE = "private"
SCOPE_LEGACY = "legacy_unclassified"
SHARED_SCOPES: tuple[str, ...] = (SCOPE_SHARED,)                           # prompts, shared history, failure patterns
LEGACY_REVIEW_SCOPES: tuple[str, ...] = (SCOPE_SHARED, SCOPE_LEGACY)       # restricted: historical evaluation/dashboards
DISPLAY_SCOPES: tuple[str, ...] = (SCOPE_SHARED, SCOPE_LEGACY, SCOPE_PRIVATE)  # what the local user may see of their own data
WRITABLE_SCOPES = frozenset({SCOPE_SHARED, SCOPE_PRIVATE})


def scope_clause(scopes: tuple[str, ...]) -> tuple[str, list]:
    """SQL fragment + params restricting a predictions query to *scopes*."""
    scopes = tuple(scopes)
    if not scopes:
        raise ValueError("scope_clause: at least one scope is required")
    return f"scope IN ({','.join('?' * len(scopes))})", list(scopes)


# ── Role of a forecast row: which model-selection path produced it ────────────
# champion       the configured champion model (engine_settings.apex_champion_model)
# fallback       a model from the ordered fallback list, because the champion
#                timed out / failed — still the production prediction that day
# challenger     an optional shadow run of engine_settings.apex_challenger_model on
#                the same prompt. Evaluated like any row, but NEVER shown to users
#                and never fed back into prompts, same-day dedup,
#                previous_prediction, rolling metrics or failure-pattern mining
# legacy_random  rows written before champion selection, when run_apex picked a
#                random model per run — kept and viewable, labelled as such
# interactive    private chat / personalized forecasts (not the batch model plan)
# insert_prediction always writes a role; NULL only ever means "predates the
# column" and the migration below backfills it to legacy_random.
ROLE_CHAMPION = "champion"
ROLE_FALLBACK = "fallback"
ROLE_CHALLENGER = "challenger"
ROLE_LEGACY_RANDOM = "legacy_random"
ROLE_INTERACTIVE = "interactive"
PREDICTION_ROLES = frozenset({ROLE_CHAMPION, ROLE_FALLBACK, ROLE_CHALLENGER, ROLE_LEGACY_RANDOM, ROLE_INTERACTIVE})
VALIDATION_ROLES: tuple[str, ...] = (ROLE_CHAMPION, ROLE_FALLBACK)                    # default validation views
VALIDATION_ROLES_WITH_LEGACY: tuple[str, ...] = (*VALIDATION_ROLES, ROLE_LEGACY_RANDOM)
REVIEW_ROLES: tuple[str, ...] = (*VALIDATION_ROLES_WITH_LEGACY, ROLE_CHALLENGER)       # role breakdown / comparison
ROLE_LABELS = {
    ROLE_CHAMPION: "Champion",
    ROLE_FALLBACK: "Fallback",
    ROLE_CHALLENGER: "Challenger",
    ROLE_LEGACY_RANDOM: "Legacy (random model)",
    ROLE_INTERACTIVE: "Interactive",
}


_schema_ready: set[str] = set()


def ensure_schema() -> None:
    """
    Run the idempotent predictions schema/migration once per process per DB file.
    Several readers (web pages, chat tools, batch helpers) open raw connections
    that never pass through _db(); the two SQL-fragment helpers below call this
    so a query that references prediction_role can never run before the column
    exists on a database prediction_db hasn't touched yet in this process.
    """
    from portfolio_agent.tools import db as _db_module
    key = str(_db_module.DB_PATH)
    if key in _schema_ready:
        return
    with _db():
        pass
    _schema_ready.add(key)


def role_clause(roles: tuple[str, ...], alias: str = "") -> tuple[str, list]:
    """SQL fragment + params restricting a predictions query to *roles* (alias: table alias, e.g. "p")."""
    roles = tuple(roles)
    if not roles:
        raise ValueError("role_clause: at least one role is required")
    ensure_schema()
    col = f"{alias}.prediction_role" if alias else "prediction_role"
    return f"{col} IN ({','.join('?' * len(roles))})", list(roles)


def not_challenger_sql(alias: str = "") -> str:
    """Parameterless SQL fragment every user-facing / prompt-feeding reader adds: a challenger
    row is written AFTER the champion row for the same ticker, so any "latest row" query would
    otherwise surface it. Evaluate it at query time (it ensures the column exists)."""
    ensure_schema()
    col = f"{alias}.prediction_role" if alias else "prediction_role"
    return f"COALESCE({col}, '') <> '{ROLE_CHALLENGER}'"


@contextmanager
def _db():
    def _setup(conn: sqlite3.Connection) -> None:
        _create_schema(conn)
        _migrate(conn)
        conn.commit()
    with db_conn(setup=_setup) as conn:
        yield conn


def _backfill(conn: sqlite3.Connection, where: str, update_sql: str, params: list | tuple = ()) -> None:
    """
    Run a one-time backfill UPDATE only if some row actually needs it. This setup runs on every connection, and a bare
    UPDATE takes the database write lock even when it changes nothing — with the pipeline's parallel threads that
    contention surfaced as "database is locked" on prediction saves. The existence check is a plain read.
    """
    if conn.execute(f"SELECT 1 FROM predictions WHERE {where} LIMIT 1").fetchone() is not None:
        conn.execute(update_sql, list(params))


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS predictions (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker                TEXT    NOT NULL,
            created_at            TEXT    NOT NULL,
            as_of_date            TEXT,
            horizon_days          INTEGER,
            prediction_type       TEXT,
            evaluation_date       TEXT,
            predicted_direction   TEXT,
            predicted_return_low  REAL,
            predicted_return_high REAL,
            conviction_score      REAL,
            prediction            TEXT    NOT NULL,
            confidence            INTEGER,
            recommendation        TEXT    NOT NULL,
            target_price          REAL,
            horizon               TEXT    DEFAULT '1m',
            fundamental_score     INTEGER,
            research_score        INTEGER,
            macro_score           INTEGER,
            news_score            INTEGER,
            composite_score       REAL,
            risk_segment          TEXT,
            reasoning             TEXT,
            reasoning_text        TEXT,
            panel_summary         TEXT,
            data_sources          TEXT,
            previous_prediction   TEXT,
            changed_from_previous INTEGER DEFAULT 0,
            model_name            TEXT,
            model_provider        TEXT,
            pt_mean               REAL,
            pt_median             REAL,
            pt_high               REAL,
            pt_low                REAL,
            pt_num_analysts       INTEGER,
            pt_current_price      REAL,
            start_price           REAL,
            actual_return         REAL,
            actual_direction      TEXT,
            outcome               TEXT,
            error_magnitude       REAL,
            evaluated_at          TEXT,
            p_strong_down         REAL,
            p_moderate_down       REAL,
            p_flat                REAL,
            p_moderate_up         REAL,
            p_strong_up           REAL,
            actual_bucket         TEXT,
            brier_score           REAL,
            log_loss              REAL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_predictions_ticker_date
        ON predictions (ticker, as_of_date DESC)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS metrics_rolling (
            metric_date        TEXT    NOT NULL,
            horizon_days       INTEGER NOT NULL,
            segment            TEXT    NOT NULL DEFAULT 'unknown',
            system_version     TEXT    NOT NULL DEFAULT 'v1.0',
            lookback_days      INTEGER NOT NULL,
            directional_accuracy   REAL,
            in_range_pct           REAL,
            mean_excess_return     REAL,
            mean_error_magnitude   REAL,
            high_conviction_accuracy REAL,
            low_conviction_accuracy  REAL,
            brier_score            REAL,
            mean_log_loss          REAL,
            num_predictions        INTEGER,
            computed_at            TEXT,
            bucket_edges_version   INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (metric_date, horizon_days, segment, system_version, lookback_days, bucket_edges_version)
        )
    """)
    # Interim, NOT-final marks for predictions that have not matured yet (long horizons). A separate table on purpose: nothing
    # here is ever read by the final metrics, which come only from the evaluated columns of `predictions`.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS prediction_provisional_marks (
            prediction_id      INTEGER PRIMARY KEY,
            marked_on          TEXT    NOT NULL,
            trading_days_elapsed INTEGER NOT NULL,
            return_so_far      REAL,
            benchmark_so_far   REAL,
            excess_so_far      REAL,
            updated_at         TEXT    NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS validation_reports (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            report_date         TEXT    NOT NULL,
            period              TEXT    NOT NULL,
            patterns_identified TEXT,
            llm_summary         TEXT,
            metrics_snapshot    TEXT,
            created_at          TEXT    NOT NULL
        )
    """)


def _migrate(conn: sqlite3.Connection) -> None:
    migrate_columns(conn, "predictions", [
        ("model_name",                  "TEXT"),
        ("model_provider",              "TEXT"),
        ("pt_mean",                     "REAL"),
        ("pt_median",                   "REAL"),
        ("pt_high",                     "REAL"),
        ("pt_low",                      "REAL"),
        ("pt_num_analysts",             "INTEGER"),
        ("pt_current_price",            "REAL"),
        ("as_of_date",                  "TEXT"),
        ("horizon_days",                "INTEGER"),
        ("prediction_type",             "TEXT"),
        ("evaluation_date",             "TEXT"),
        ("predicted_direction",         "TEXT"),
        ("predicted_return_low",        "REAL"),
        ("predicted_return_high",       "REAL"),
        ("conviction_score",            "REAL"),
        ("risk_segment",                "TEXT"),
        ("reasoning_text",              "TEXT"),
        ("actual_return",               "REAL"),
        ("actual_direction",            "TEXT"),
        ("outcome",                     "TEXT"),
        ("error_magnitude",             "REAL"),
        ("evaluated_at",                "TEXT"),
        ("evaluation_status",           "TEXT    DEFAULT 'pending'"),
        ("benchmark_return",            "REAL"),
        ("excess_return",               "REAL"),
        ("outcome_score",               "REAL"),
        ("in_predicted_range",          "INTEGER"),
        ("system_version",              "TEXT"),
        # The horizon-scaled bucket edges this prediction was MADE under (tools.bucket_edges). It is always scored with them.
        # Rows that predate the column are version 1 (the old fixed ±1% / ±5% / 2% for every horizon).
        ("bucket_edges_version",        "INTEGER"),
        ("prompt_hash",                 "TEXT"),
        ("snapshot_fundamentals_score", "REAL"),
        ("snapshot_news_score",         "REAL"),
        ("snapshot_research_score",     "REAL"),
        ("snapshot_macro_score",        "REAL"),
        ("snapshot_news_headlines",     "TEXT"),
        ("start_price",                 "REAL"),
        ("p_strong_down",               "REAL"),
        ("p_moderate_down",             "REAL"),
        ("p_flat",                      "REAL"),
        ("p_moderate_up",               "REAL"),
        ("p_strong_up",                 "REAL"),
        ("actual_bucket",               "TEXT"),
        ("brier_score",                 "REAL"),
        ("log_loss",                    "REAL"),
        ("is_ensemble",                 "INTEGER DEFAULT 0"),
        ("agreement_score",             "REAL"),
        ("is_single_model",             "INTEGER DEFAULT 0"),
        ("used_fallback",               "INTEGER DEFAULT 0"),
        ("parent_merged_id",            "INTEGER"),
        ("trigger_type",                "TEXT"),
        ("trigger_event_id",            "INTEGER"),
        ("weight_regime",               "TEXT"),
        ("weights_used",                "TEXT"),
        ("valuation_score",             "INTEGER"),
        ("guardrail_flags",             "TEXT"),
        ("raw_conviction_score",        "REAL"),
        # Scope & provenance (see SCOPE_* above)
        ("scope",                       "TEXT"),
        ("origin",                      "TEXT"),
        ("origin_run_id",               "TEXT"),
        ("origin_time_utc",             "TEXT"),
        ("owner_scope",                 "TEXT"),
        ("evidence_refs",               "TEXT"),
        ("policy_version",              "TEXT"),
        ("model_used",                  "TEXT"),
        ("guardrail_version",           "TEXT"),
        ("history_lineage",             "TEXT"),
        ("context_digest",              "TEXT"),
        # No DEFAULT on purpose: ADD COLUMN ... DEFAULT 'champion' would stamp every
        # pre-existing (randomly-selected) row as champion. insert_prediction always
        # writes the role explicitly instead.
        ("prediction_role",             "TEXT"),
        # What the security is (Step 3): rows before this stay NULL ("Unclassified") until the optional
        # backfill_instrument_types() below stamps them from the instruments table.
        ("instrument_type",             "TEXT"),
        ("leverage_factor",             "REAL"),
    ])
    # Rows that predate prediction_role came from random per-run model selection.
    # Idempotent: once backfilled, no row is NULL again.
    _backfill(conn, "prediction_role IS NULL", "UPDATE predictions SET prediction_role = ? WHERE prediction_role IS NULL",
              [ROLE_LEGACY_RANDOM])
    conn.execute("CREATE INDEX IF NOT EXISTS idx_predictions_role ON predictions(prediction_role, horizon_days)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_predictions_itype ON predictions(instrument_type, horizon_days)")
    # Rows that predate horizon-scaled bucket edges were made (and prompted) under the old fixed edges: version 1. Conditional
    # like every backfill here, so a connection that has nothing to migrate takes no write lock.
    _backfill(conn, "bucket_edges_version IS NULL", "UPDATE predictions SET bucket_edges_version = 1 WHERE bucket_edges_version IS NULL")
    # Rows that predate scopes stay unclassified: they are never promoted to shared
    # by default and only reach readers through the restricted legacy path.
    _backfill(conn, "scope IS NULL", "UPDATE predictions SET scope = ? WHERE scope IS NULL", [SCOPE_LEGACY])
    # One-time attribution: this deployment has exactly one user; legacy rows
    # predate owner_scope entirely (NULL, not "unowned by design" the way a
    # shared_market row's NULL owner_scope is). Attribute them to that user
    # without touching `scope` -- they stay legacy_unclassified, still excluded
    # from SHARED_SCOPES-gated prompts/mining, still visible via
    # LEGACY_REVIEW_SCOPES/DISPLAY_SCOPES exactly as before. Idempotent.
    from portfolio_agent.domain import LOCAL_OWNER
    _backfill(conn, f"scope = '{SCOPE_LEGACY}' AND owner_scope IS NULL",
              "UPDATE predictions SET owner_scope = ? WHERE scope = ? AND owner_scope IS NULL",
              (f"user:{LOCAL_OWNER}", SCOPE_LEGACY))
    # One-time rename: any row already owner-scoped to the old "user:local"
    # placeholder now belongs to "user:sumanth_b" -- idempotent.
    _backfill(conn, "owner_scope = 'user:local'", "UPDATE predictions SET owner_scope = ? WHERE owner_scope = 'user:local'",
              (f"user:{LOCAL_OWNER}",))
    conn.execute("CREATE INDEX IF NOT EXISTS idx_predictions_scope ON predictions(scope, ticker, created_at)")
    # Index for per-model accuracy queries and parent linkage
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_predictions_model "
        "ON predictions(model_name)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_predictions_parent "
        "ON predictions(parent_merged_id)"
    )
    # Migrate metrics_rolling table
    mr_cols = {r[1] for r in conn.execute("PRAGMA table_info(metrics_rolling)").fetchall()}
    if "mean_log_loss" not in mr_cols:
        conn.execute("ALTER TABLE metrics_rolling ADD COLUMN mean_log_loss REAL")

    # 'unknown' was the segment every row got under the old (dead -- risk_segment
    # is never set) bucketing; recompute_rolling_metrics() now writes 'all' for
    # the same blended view plus 'portfolio' / 'opportunity' splits. Rename in
    # place (one-time, idempotent) so existing trend history stays visible
    # under the new segment name instead of silently disappearing.
    if conn.execute("SELECT 1 FROM metrics_rolling WHERE segment = 'unknown' LIMIT 1").fetchone() is not None:
        conn.execute("UPDATE metrics_rolling SET segment = 'all' WHERE segment = 'unknown'")

    # Index on new columns — only safe after migration ensures columns exist
    # Rename prediction_date → as_of_date (idempotent)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(predictions)")}
    if "prediction_date" in cols and "as_of_date" not in cols:
        conn.execute("ALTER TABLE predictions RENAME COLUMN prediction_date TO as_of_date")

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_predictions_ticker_horizon
        ON predictions (ticker, as_of_date, horizon_days)
    """)
    _rebuild_metrics_rolling_with_edges(conn)


def _rebuild_metrics_rolling_with_edges(conn: sqlite3.Connection) -> None:
    """
    metrics_rolling's primary key now includes bucket_edges_version (rolling metrics are computed per edges version and never
    mix them). SQLite cannot change a primary key in place, so an existing table is copied into the new shape — every row kept,
    as version 1, the edges all of them were scored under — and the old table dropped only after the copy. Runs once.
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(metrics_rolling)").fetchall()]
    if not cols or "bucket_edges_version" in cols:
        return
    conn.execute("ALTER TABLE metrics_rolling RENAME TO metrics_rolling_pre_edges")
    conn.execute("""
        CREATE TABLE metrics_rolling (
            metric_date        TEXT    NOT NULL,
            horizon_days       INTEGER NOT NULL,
            segment            TEXT    NOT NULL DEFAULT 'unknown',
            system_version     TEXT    NOT NULL DEFAULT 'v1.0',
            lookback_days      INTEGER NOT NULL,
            directional_accuracy   REAL,
            in_range_pct           REAL,
            mean_excess_return     REAL,
            mean_error_magnitude   REAL,
            high_conviction_accuracy REAL,
            low_conviction_accuracy  REAL,
            brier_score            REAL,
            mean_log_loss          REAL,
            num_predictions        INTEGER,
            computed_at            TEXT,
            bucket_edges_version   INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (metric_date, horizon_days, segment, system_version, lookback_days, bucket_edges_version)
        )""")
    old_cols = [c for c in cols if c != "bucket_edges_version"]
    names = ", ".join(old_cols)
    conn.execute(f"INSERT INTO metrics_rolling ({names}, bucket_edges_version) SELECT {names}, 1 FROM metrics_rolling_pre_edges")
    conn.execute("DROP TABLE metrics_rolling_pre_edges")


# ── Trading calendar helpers ───────────────────────────────────────────────────

@lru_cache(maxsize=32)
def _us_market_holidays(year: int) -> frozenset[date]:
    """Return approximate NYSE holidays for the given year (excludes Good Friday)."""
    try:
        from pandas.tseries.holiday import USFederalHolidayCalendar
        from pandas import Timestamp
        cal = USFederalHolidayCalendar()
        holidays_ts = cal.holidays(start=f"{year}-01-01", end=f"{year}-12-31")
        return frozenset(Timestamp(ts).date() for ts in holidays_ts)
    except Exception:
        return frozenset()


def is_trading_day(dt: date) -> bool:
    """Return True if dt is a NYSE trading day (Mon-Fri, not a US market holiday)."""
    if dt.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    return dt not in _us_market_holidays(dt.year)


def _next_trading_day(dt: date) -> date:
    """Return the next trading day on or after dt."""
    d = dt
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def _evaluation_date(start: date, horizon_days: int) -> str:
    """
    LEGACY rule: the evaluation date as a weekday offset (pandas BDay, holidays ignored). insert_prediction no longer uses
    it — new predictions use trading_day_evaluation_date — but it is kept as the record of how every earlier row's
    evaluation_date was derived.
    """
    try:
        from pandas import Timestamp
        from pandas.tseries.offsets import BDay
        return (Timestamp(start) + BDay(horizon_days)).date().isoformat()
    except Exception:
        # fallback: rough calendar-day approximation
        cal_days = {5: 7, 21: 30, 63: 91}.get(horizon_days, horizon_days * 7 // 5)
        return (start + timedelta(days=cal_days)).isoformat()


def trading_day_evaluation_date(start: date, horizon_days: int) -> str:
    """
    The date `horizon_days` TRADING days after `start` (is_trading_day: weekends and market holidays do not count). This is what a
    long horizon needs: a 250-day forecast made on a weekday ends 250 sessions later, not 250 weekdays (which drifts weeks off
    over a year of holidays). Used for NEW predictions only; rows already stored keep the evaluation_date they were given by
    _evaluation_date (the weekday-offset rule) and are never rewritten.
    """
    d, n = start, 0
    while n < horizon_days:
        d += timedelta(days=1)
        if is_trading_day(d):
            n += 1
    return d.isoformat()


def _horizon_label(horizon_days: int) -> str:
    return {5: "5d", 21: "21d", 63: "63d"}.get(horizon_days, f"{horizon_days}d")


def get_scheduled_horizons(today: date) -> list[int]:
    """
    Return the horizon_days to generate on this trading day.

      5d  — every trading day       (news/momentum)
      21d — every Monday             (earnings drift, monthly themes)
      63d — first trading day of month (fundamentals, full earnings cycle)
    """
    if not is_trading_day(today):
        return []
    horizons = [5]
    if today.weekday() == 0:  # Monday
        horizons.append(21)
    # First trading day of the month
    month_start = today.replace(day=1)
    if _next_trading_day(month_start) == today:
        if 63 not in horizons:
            horizons.append(63)
    return horizons


def get_today_horizons(ticker: str, today: str, *, scopes: tuple[str, ...] = DISPLAY_SCOPES) -> set[int]:
    """Horizon_days already saved for ticker today within *scopes*. The scheduled writer
    passes SHARED_SCOPES so a private chat forecast never suppresses shared generation."""
    sc, sp = scope_clause(scopes)
    with _db() as c:
        rows = c.execute(
            f"""SELECT horizon_days FROM predictions
                WHERE ticker = ? AND as_of_date = ? AND horizon_days IS NOT NULL AND {sc}
                  AND {not_challenger_sql()}""",
            [ticker.upper(), today, *sp],
        ).fetchall()
    return {r[0] for r in rows}


def event_already_processed(ticker: str, trigger_event_id: int, today: str, *,
                            scopes: tuple[str, ...] = DISPLAY_SCOPES) -> bool:
    """
    True if a row already exists today carrying this EXACT trigger_event_id —
    the event-driven counterpart to get_today_horizons's "any horizon done
    today" check. Used so a new, not-yet-processed event is never suppressed
    just because some OTHER (e.g. routine cadence) generation already ran for
    the same ticker earlier today — see pipeline.daily.apex.
    """
    if trigger_event_id is None:
        return False
    sc, sp = scope_clause(scopes)
    with _db() as c:
        row = c.execute(
            f"""SELECT 1 FROM predictions
                WHERE ticker = ? AND as_of_date = ? AND trigger_event_id = ? AND {sc}
                  AND {not_challenger_sql()} LIMIT 1""",
            [ticker.upper(), today, trigger_event_id, *sp],
        ).fetchone()
    return row is not None


# ── Core write/read API ────────────────────────────────────────────────────────

def insert_prediction(
    ticker: str,
    prediction: str,
    recommendation: str,
    confidence: Optional[int] = None,
    target_price: Optional[float] = None,
    horizon: str = "1m",
    horizon_days: Optional[int] = None,
    prediction_type: Optional[str] = None,
    evaluation_date: Optional[str] = None,
    predicted_direction: Optional[str] = None,
    predicted_return_low: Optional[float] = None,
    predicted_return_high: Optional[float] = None,
    conviction_score: Optional[float] = None,
    fundamental_score: Optional[int] = None,
    valuation_score: Optional[int] = None,
    research_score: Optional[int] = None,
    macro_score: Optional[int] = None,
    news_score: Optional[int] = None,
    composite_score: Optional[float] = None,
    risk_segment: Optional[str] = None,
    reasoning: str = "",
    reasoning_text: Optional[str] = None,
    panel_summary: Optional[dict] = None,
    data_sources: Optional[list] = None,
    model_name: Optional[str] = None,
    model_provider: Optional[str] = None,
    system_version: str = CURRENT_SYSTEM_VERSION,
    snapshot_news_headlines: Optional[list] = None,
    start_price: Optional[float] = None,
    pt_mean: Optional[float] = None,
    pt_median: Optional[float] = None,
    pt_high: Optional[float] = None,
    pt_low: Optional[float] = None,
    pt_num_analysts: Optional[int] = None,
    pt_current_price: Optional[float] = None,
    p_strong_down: Optional[float] = None,
    p_moderate_down: Optional[float] = None,
    p_flat: Optional[float] = None,
    p_moderate_up: Optional[float] = None,
    p_strong_up: Optional[float] = None,
    # The bucket-edges version the prompt used (tools.bucket_edges). Default 1 = the old fixed edges: only a writer that
    # prompted with the horizon-scaled ranges (the APEX phase) passes the current version.
    bucket_edges_version: int = 1,
    # Model selection metadata
    used_fallback: bool = False,
    parent_merged_id: Optional[int] = None,
    # Event-driven metadata
    trigger_type: Optional[str] = None,
    trigger_event_id: Optional[int] = None,
    # Dynamic weight mix actually used to compute composite_score
    weight_regime: Optional[str] = None,
    weights_used: Optional[dict] = None,
    # Deterministic post-hoc guardrails applied before this row was stored
    guardrail_flags: Optional[list] = None,
    # The conviction the model actually produced, BEFORE any guardrail cap —
    # conviction_score above is the (possibly capped) value everything else
    # uses; raw_conviction_score is preserved so failure-pattern mining can
    # still see a call that WOULD have been high-conviction, instead of it
    # silently landing in the "low conviction" bucket once capped. None when
    # no cap applied (raw == conviction_score) or for rows that predate this.
    raw_conviction_score: Optional[float] = None,
    # Scope & provenance. The default scope is PRIVATE: only forecast_writer.write_shared_forecast,
    # holding a market-only MarketContext, stores a row as shared_market.
    scope: str = SCOPE_PRIVATE,
    origin: str = "unspecified",
    origin_run_id: Optional[str] = None,
    owner_scope: Optional[str] = None,
    evidence_refs: Optional[dict] = None,
    policy_version: Optional[str] = None,
    model_used: Optional[str] = None,
    guardrail_version: Optional[str] = None,
    history_lineage: Optional[list] = None,
    context_digest: Optional[str] = None,
    # Which model-selection path produced the row (see ROLE_* above). Always written.
    prediction_role: str = ROLE_CHAMPION,
    # What kind of security the ticker was at forecast time (None = unknown / pre-taxonomy) and its leverage.
    instrument_type: Optional[str] = None,
    leverage_factor: Optional[float] = None,
) -> dict:
    """
    Insert a new prediction row (append-only). One row per horizon per ticker per date.
    Returns {saved, ticker, changed, previous}.
    """
    if prediction_role not in PREDICTION_ROLES:
        raise ValueError(f"insert_prediction: prediction_role must be one of {sorted(PREDICTION_ROLES)}, "
                         f"not {prediction_role!r}")
    if scope not in WRITABLE_SCOPES:
        raise ValueError(f"insert_prediction: scope must be one of {sorted(WRITABLE_SCOPES)}, not {scope!r}")
    if scope == SCOPE_SHARED and not context_digest:
        raise ValueError("insert_prediction: a shared_market row needs the market-context digest it was built from")
    ticker = ticker.upper()
    now_cst   = _now_cst()
    today_str = _today_cst()
    today_date = date.fromisoformat(today_str)
    origin_time_utc = datetime.now(ZoneInfo("UTC")).isoformat(timespec="seconds")

    # Derive horizon_days from legacy horizon string if not provided
    if horizon_days is None and horizon:
        _map = {"5d": 5, "1w": 5, "21d": 21, "1m": 21, "63d": 63, "3m": 63}
        horizon_days = _map.get(horizon)

    # Derive prediction_type
    if prediction_type is None and horizon_days:
        prediction_type = _horizon_label(horizon_days)

    # Derive evaluation_date
    if evaluation_date is None and horizon_days:
        evaluation_date = trading_day_evaluation_date(today_date, horizon_days)

    # Use reasoning_text → reasoning for backward compat
    if reasoning_text and not reasoning:
        reasoning = reasoning_text
    if not reasoning_text and reasoning:
        reasoning_text = reasoning

    # "Changed from previous" is judged against the same kind of history the row belongs to:
    # a shared row against shared rows only; a private row against everything its owner may see.
    prev = get_latest_prediction(ticker, scopes=SHARED_SCOPES if scope == SCOPE_SHARED else DISPLAY_SCOPES)
    prev_pred = prev["recommendation"] if prev else None
    changed = 1 if prev_pred and prev_pred != recommendation else 0

    try:
        with _db() as c:
            c.execute(
                """INSERT INTO predictions
                       (ticker, created_at, as_of_date, horizon_days, prediction_type, evaluation_date,
                        predicted_direction, predicted_return_low, predicted_return_high, conviction_score,
                        prediction, confidence, recommendation, target_price,
                        horizon, fundamental_score, research_score, macro_score,
                        news_score, composite_score, risk_segment, reasoning, reasoning_text,
                        panel_summary, data_sources, previous_prediction, changed_from_previous,
                        model_name, model_provider,
                        pt_mean, pt_median, pt_high, pt_low,
                        pt_num_analysts, pt_current_price,
                        evaluation_status, system_version,
                        snapshot_fundamentals_score, snapshot_news_score,
                        snapshot_research_score, snapshot_macro_score,
                        snapshot_news_headlines, start_price,
                        p_strong_down, p_moderate_down, p_flat, p_moderate_up, p_strong_up,
                        used_fallback, parent_merged_id,
                        trigger_type, trigger_event_id,
                        weight_regime, weights_used, valuation_score, guardrail_flags, raw_conviction_score,
                        scope, origin, origin_run_id, origin_time_utc, owner_scope, evidence_refs,
                        policy_version, model_used, guardrail_version, history_lineage, context_digest,
                        prediction_role, instrument_type, leverage_factor, bucket_edges_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    ticker, now_cst, today_str, horizon_days, prediction_type, evaluation_date,
                    predicted_direction, predicted_return_low, predicted_return_high, conviction_score,
                    prediction, confidence, recommendation, target_price,
                    horizon, fundamental_score, research_score, macro_score,
                    news_score, composite_score, risk_segment, reasoning, reasoning_text,
                    json.dumps(panel_summary or {}),
                    json.dumps(data_sources or []),
                    prev_pred, changed,
                    model_name, model_provider,
                    pt_mean, pt_median, pt_high, pt_low,
                    pt_num_analysts, pt_current_price,
                    'pending', system_version,
                    fundamental_score, news_score,
                    research_score, macro_score,
                    json.dumps(snapshot_news_headlines or []),
                    start_price,
                    p_strong_down, p_moderate_down, p_flat, p_moderate_up, p_strong_up,
                    int(used_fallback), parent_merged_id,
                    trigger_type, trigger_event_id,
                    weight_regime, json.dumps(weights_used) if weights_used else None,
                    valuation_score,
                    json.dumps(guardrail_flags) if guardrail_flags else None,
                    raw_conviction_score,
                    scope, origin, origin_run_id, origin_time_utc, owner_scope,
                    json.dumps(evidence_refs, default=str) if evidence_refs else None,
                    policy_version, model_used, guardrail_version,
                    json.dumps(history_lineage, default=str) if history_lineage else None,
                    context_digest,
                    prediction_role, instrument_type, leverage_factor, int(bucket_edges_version),
                ],
            )
            c.commit()
        return {"saved": True, "ticker": ticker, "changed": bool(changed), "previous": prev_pred}
    except Exception as exc:
        return {"saved": False, "ticker": ticker, "error": str(exc)}


def backfill_instrument_types() -> int:
    """
    Optional one-off: stamp predictions that predate the instrument_type column with the ticker's CURRENT type from
    the instruments table, so older rows show up under their real type instead of "Unclassified". Only NULL rows
    whose instrument is classified (not UNKNOWN) are touched; nothing already stamped is changed. Returns rows updated.
    """
    with _db() as c:
        try:
            n = c.execute(
                """UPDATE predictions
                   SET instrument_type = (SELECT i.instrument_type FROM instruments i WHERE i.ticker = predictions.ticker),
                       leverage_factor = COALESCE(leverage_factor,
                                                  (SELECT i.leverage_factor FROM instruments i WHERE i.ticker = predictions.ticker))
                   WHERE instrument_type IS NULL
                     AND EXISTS (SELECT 1 FROM instruments i WHERE i.ticker = predictions.ticker
                                 AND i.instrument_type <> 'UNKNOWN')""").rowcount
        except sqlite3.OperationalError:          # no instruments table yet: nothing to stamp from
            return 0
        c.commit()
    return n


def get_latest_prediction(ticker: str, horizon_days: Optional[int] = None, *,
                          scopes: tuple[str, ...] = DISPLAY_SCOPES) -> Optional[Prediction]:
    """Most recent prediction row for ticker within *scopes* (optionally one horizon)."""
    sc, sp = scope_clause(scopes)
    with _db() as c:
        if horizon_days is not None:
            row = c.execute(
                f"""SELECT * FROM predictions WHERE ticker = ? AND horizon_days = ? AND {sc}
                      AND {not_challenger_sql()}
                    ORDER BY created_at DESC LIMIT 1""",
                [ticker.upper(), horizon_days, *sp],
            ).fetchone()
        else:
            row = c.execute(
                f"""SELECT * FROM predictions WHERE ticker = ? AND {sc} AND {not_challenger_sql()}
                    ORDER BY created_at DESC LIMIT 1""",
                [ticker.upper(), *sp],
            ).fetchone()
    if not row:
        return None
    return Prediction.from_db_row(dict(row))


def get_prediction_history(ticker: str, limit: int = 10, *,
                           scopes: tuple[str, ...] = SHARED_SCOPES) -> list[Prediction]:
    """
    Last *limit* rows for ticker, newest first. Defaults to SHARED history only —
    this is what feeds prompts. Callers showing a person their own track record
    pass DISPLAY_SCOPES explicitly; historical evaluation passes LEGACY_REVIEW_SCOPES.
    """
    sc, sp = scope_clause(scopes)
    with _db() as c:
        rows = c.execute(
            f"""SELECT * FROM predictions WHERE ticker = ? AND {sc} AND {not_challenger_sql()}
                ORDER BY created_at DESC LIMIT ?""",
            [ticker.upper(), *sp, limit],
        ).fetchall()
    return [Prediction.from_db_row(dict(row)) for row in rows]


def get_matured_pending_predictions(today: str) -> list[dict]:
    """Return predictions where evaluation_date<=today and not yet evaluated."""
    with _db() as c:
        rows = c.execute(  # noqa: E501
            """SELECT * FROM predictions
               WHERE evaluation_date <= ?
               AND evaluation_date IS NOT NULL AND evaluation_date != ''
               AND (evaluation_status IS NULL OR evaluation_status = 'pending')
               AND horizon_days IS NOT NULL""",
            [today],
        ).fetchall()
    return [_parse_pred_row(r) for r in rows]


def get_pending_for_provisional(horizons: tuple[int, ...], today: str) -> list[dict]:
    """Pending (not yet matured, not evaluated) predictions at these horizons made BEFORE today: the rows to mark."""
    ph = ",".join("?" * len(horizons))
    with _db() as c:
        rows = c.execute(
            f"""SELECT id, ticker, as_of_date, horizon_days, evaluation_date, start_price, predicted_direction,
                       bucket_edges_version
                FROM predictions
                WHERE (evaluation_status IS NULL OR evaluation_status = 'pending')
                  AND horizon_days IN ({ph}) AND as_of_date IS NOT NULL AND as_of_date < ?
                  AND (evaluation_date IS NULL OR evaluation_date > ?)""",
            [*horizons, today, today],
        ).fetchall()
    return [dict(r) for r in rows]


def upsert_provisional_mark(prediction_id: int, marked_on: str, trading_days_elapsed: int, return_so_far: Optional[float],
                            benchmark_so_far: Optional[float], excess_so_far: Optional[float]) -> None:
    """Record (replace) the latest interim mark for one pending prediction. Touches only prediction_provisional_marks."""
    with _db() as c:
        c.execute(
            """INSERT OR REPLACE INTO prediction_provisional_marks
               (prediction_id, marked_on, trading_days_elapsed, return_so_far, benchmark_so_far, excess_so_far, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [prediction_id, marked_on, trading_days_elapsed, return_so_far, benchmark_so_far, excess_so_far, _now_cst()],
        )
        c.commit()


def get_provisional_marks(horizon_days: int, *, scopes: tuple[str, ...] | None = None,
                          roles: tuple[str, ...] | None = None) -> list[dict]:
    """Interim marks of the predictions at this horizon that are STILL pending (a matured row's final result lives in
    `predictions`; its stale mark is never returned). Optional scope / role filters match the final-metric readers."""
    where, params = ["(p.evaluation_status IS NULL OR p.evaluation_status = 'pending')", "p.horizon_days = ?"], [horizon_days]
    if scopes:
        sc, sp = scope_clause(scopes)
        where.append("p." + sc); params += sp
    if roles:
        rc, rp = role_clause(roles, alias="p")
        where.append(rc); params += rp
    ensure_schema()
    with _db() as c:
        rows = c.execute(
            f"""SELECT p.id, p.ticker, p.as_of_date, p.evaluation_date, p.horizon_days, p.predicted_direction,
                       p.bucket_edges_version, m.marked_on, m.trading_days_elapsed, m.return_so_far, m.benchmark_so_far,
                       m.excess_so_far
                FROM predictions p JOIN prediction_provisional_marks m ON m.prediction_id = p.id
                WHERE {' AND '.join(where)}""",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def update_prediction_outcome(
    prediction_id: int,
    actual_return: float,
    actual_direction: str,
    benchmark_return: Optional[float],
    excess_return: Optional[float],
    outcome: str,
    outcome_score: float,
    error_magnitude: float,
    in_predicted_range: bool,
    evaluation_status: str = "evaluated",
    actual_bucket: Optional[str] = None,
    brier_score: Optional[float] = None,
    log_loss: Optional[float] = None,
) -> None:
    with _db() as c:
        c.execute(
            """UPDATE predictions SET
                actual_return=?, actual_direction=?, benchmark_return=?,
                excess_return=?, outcome=?, outcome_score=?,
                error_magnitude=?, in_predicted_range=?,
                evaluation_status=?, evaluated_at=?,
                actual_bucket=?, brier_score=?, log_loss=?
               WHERE id=?""",
            [
                actual_return, actual_direction, benchmark_return,
                excess_return, outcome, outcome_score,
                error_magnitude, 1 if in_predicted_range else 0,
                evaluation_status, _now_cst(),
                actual_bucket, brier_score, log_loss,
                prediction_id,
            ],
        )
        c.commit()


# For readers of metrics_rolling that want ONE row per (date, horizon, segment, lookback): the rows of the newest bucket-edges
# version present. (When only version 1 exists — the case until a horizon's edges actually change — this selects everything.)
LATEST_EDGES_ONLY = ("bucket_edges_version = (SELECT MAX(m2.bucket_edges_version) FROM metrics_rolling m2 "
                     "WHERE m2.metric_date = metrics_rolling.metric_date AND m2.horizon_days = metrics_rolling.horizon_days "
                     "AND m2.segment = metrics_rolling.segment AND m2.lookback_days = metrics_rolling.lookback_days)")


def upsert_rolling_metric(row: dict) -> None:
    with _db() as c:
        c.execute(
            """INSERT OR REPLACE INTO metrics_rolling
               (metric_date, horizon_days, segment, system_version, lookback_days,
                directional_accuracy, in_range_pct, mean_excess_return,
                mean_error_magnitude, high_conviction_accuracy, low_conviction_accuracy,
                brier_score, mean_log_loss, num_predictions, computed_at, bucket_edges_version)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                row["metric_date"], row["horizon_days"], row["segment"],
                row["system_version"], row["lookback_days"],
                row.get("directional_accuracy"), row.get("in_range_pct"),
                row.get("mean_excess_return"), row.get("mean_error_magnitude"),
                row.get("high_conviction_accuracy"), row.get("low_conviction_accuracy"),
                row.get("brier_score"), row.get("mean_log_loss"), row.get("num_predictions"),
                _now_cst(), int(row.get("bucket_edges_version") or 1),
            ],
        )
        c.commit()


def insert_validation_report(
    period: str,
    patterns_identified: dict,
    llm_summary: str,
    metrics_snapshot: Optional[dict] = None,
) -> None:
    with _db() as c:
        c.execute(
            """INSERT INTO validation_reports
               (report_date, period, patterns_identified, llm_summary, metrics_snapshot, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [_today_cst(), period, json.dumps(patterns_identified),
             llm_summary, json.dumps(metrics_snapshot or {}), _now_cst()],
        )
        c.commit()


def get_rolling_metrics(lookback_days: int = 90) -> list[dict]:
    today = _today_cst()
    with _db() as c:
        rows = c.execute(
            f"""SELECT * FROM metrics_rolling
               WHERE metric_date = ? AND lookback_days = ? AND {LATEST_EDGES_ONLY}
               ORDER BY horizon_days, segment""",
            [today, lookback_days],
        ).fetchall()
    return [dict(r) for r in rows]


def get_validation_reports(limit: int = 5) -> list[dict]:
    with _db() as c:
        rows = c.execute(
            "SELECT * FROM validation_reports ORDER BY created_at DESC LIMIT ?",
            [limit],
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["patterns_identified"] = json.loads(d.get("patterns_identified") or "{}")
        d["metrics_snapshot"] = json.loads(d.get("metrics_snapshot") or "{}")
        result.append(d)
    return result


def _parse_pred_row(row) -> dict:
    d = dict(row)
    d["data_sources"] = json.loads(d.get("data_sources") or "[]")
    d["panel_summary"] = json.loads(d.get("panel_summary") or "{}")
    return d


def get_all_latest_predictions(*, scopes: tuple[str, ...] = DISPLAY_SCOPES) -> dict[str, Prediction]:
    """Most recent prediction row per ticker within *scopes* (default: everything the local user may see)."""
    sc, sp = scope_clause(scopes)
    with _db() as c:
        rows = c.execute(f"""
            SELECT p.* FROM predictions p
            INNER JOIN (
                SELECT ticker, MAX(id) AS max_id
                FROM predictions WHERE {sc} AND {not_challenger_sql()} GROUP BY ticker
            ) latest ON p.id = latest.max_id
        """, sp).fetchall()
    result = {}
    for row in rows:
        pred = Prediction.from_db_row(dict(row))
        result[pred.ticker] = pred
    return result


# ── Score change attribution ("why did the recommendation change?") ───────────

_PANEL_VERDICT_KEYS = {
    "fundamentals": "chen_verdict",
    "research":     "webb_verdict",
    "macro":        "varga_verdict",
    "news":         "park_verdict",
}
_SOURCE_LABELS = {
    "fundamentals": "Fundamentals",
    "research":     "Research",
    "macro":        "Macro",
    "news":         "News",
}
_SCORE_COLUMNS = {
    "fundamentals": "fundamental_score",
    "research":     "research_score",
    "macro":        "macro_score",
    "news":         "news_score",
}


def _strip_verdict_score_prefix(verdict: str) -> str:
    """Strip the '(N/10)' score annotation — same regex the Agent Panel
    expander in web/components/predictions_cards.py already uses."""
    return re.sub(r"\(\d+(?:\.\d+)?/10\)\s*[—\-]?\s*", "", verdict or "").strip()


def get_score_change_breakdown(ticker: str) -> Optional[dict]:
    """
    Day-over-day attribution of a ticker's composite_score change: for each of
    fundamentals/research/macro/news, how many composite-score points that
    source's (weight x score) contribution moved, plus that day's panel
    verdict as the "why". Returns None if fewer than 2 distinct as_of_dates
    exist for this ticker.

    If either day's weights_used wasn't recorded (rows predating this
    column), falls back to weight_engine.DEFAULT_BASE_WEIGHTS for both days
    and sets weights_available=False so the caller can disclose the
    approximation rather than presenting it as exact.
    """
    from portfolio_agent.tools.weight_engine import DEFAULT_BASE_WEIGHTS

    ticker = ticker.upper()
    with _db() as c:
        rows = c.execute(
            f"""SELECT as_of_date, recommendation, composite_score,
                      fundamental_score, research_score, macro_score, news_score,
                      weight_regime, weights_used, panel_summary
               FROM predictions
               WHERE ticker = ? AND as_of_date IS NOT NULL AND {not_challenger_sql()}
               ORDER BY as_of_date DESC, created_at DESC""",
            [ticker],
        ).fetchall()

    by_date: dict[str, dict] = {}
    for r in rows:
        d = dict(r)
        if d["as_of_date"] not in by_date:
            by_date[d["as_of_date"]] = d
        if len(by_date) >= 2:
            break

    dates_sorted = sorted(by_date.keys(), reverse=True)
    if len(dates_sorted) < 2:
        return None

    today_row, yesterday_row = by_date[dates_sorted[0]], by_date[dates_sorted[1]]

    def _weights(row: dict) -> tuple[dict, bool]:
        try:
            w = json.loads(row.get("weights_used") or "{}")
        except (TypeError, ValueError):
            w = {}
        return (w, True) if w else (dict(DEFAULT_BASE_WEIGHTS), False)

    today_weights, today_has_weights = _weights(today_row)
    yesterday_weights, yesterday_has_weights = _weights(yesterday_row)
    weights_available = today_has_weights and yesterday_has_weights

    try:
        today_panel = json.loads(today_row.get("panel_summary") or "{}")
    except (TypeError, ValueError):
        today_panel = {}

    breakdown = []
    for source, score_col in _SCORE_COLUMNS.items():
        score_today = today_row.get(score_col)
        score_yesterday = yesterday_row.get(score_col)
        if score_today is None or score_yesterday is None:
            continue
        w_today = today_weights.get(source, DEFAULT_BASE_WEIGHTS[source])
        w_yesterday = yesterday_weights.get(source, DEFAULT_BASE_WEIGHTS[source])
        contribution = round(w_today * score_today - w_yesterday * score_yesterday, 2)
        breakdown.append({
            "source": _SOURCE_LABELS[source],
            "contribution": contribution,
            "rationale": _strip_verdict_score_prefix(today_panel.get(_PANEL_VERDICT_KEYS[source], "")),
        })
    breakdown.sort(key=lambda b: abs(b["contribution"]), reverse=True)

    composite_today, composite_yesterday = today_row.get("composite_score"), yesterday_row.get("composite_score")
    net_change = (
        round(composite_today - composite_yesterday, 2)
        if composite_today is not None and composite_yesterday is not None else None
    )

    return {
        "ticker": ticker,
        "today": {
            "as_of_date": today_row["as_of_date"],
            "recommendation": today_row.get("recommendation"),
            "composite_score": composite_today,
        },
        "yesterday": {
            "as_of_date": yesterday_row["as_of_date"],
            "recommendation": yesterday_row.get("recommendation"),
            "composite_score": composite_yesterday,
        },
        "net_change": net_change,
        "sum_of_contributions": round(sum(b["contribution"] for b in breakdown), 2),
        "breakdown": breakdown,
        "weights_available": weights_available,
    }
