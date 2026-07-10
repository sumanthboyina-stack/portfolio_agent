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
  actual_return, actual_direction, outcome, error_magnitude, evaluated_at
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
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


@contextmanager
def _db():
    def _setup(conn: sqlite3.Connection) -> None:
        _create_schema(conn)
        _migrate(conn)
        conn.commit()
    with db_conn(setup=_setup) as conn:
        yield conn


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
            PRIMARY KEY (metric_date, horizon_days, segment, system_version, lookback_days)
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
    ])
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

    # Index on new columns — only safe after migration ensures columns exist
    # Rename prediction_date → as_of_date (idempotent)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(predictions)")}
    if "prediction_date" in cols and "as_of_date" not in cols:
        conn.execute("ALTER TABLE predictions RENAME COLUMN prediction_date TO as_of_date")

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_predictions_ticker_horizon
        ON predictions (ticker, as_of_date, horizon_days)
    """)


# ── Trading calendar helpers ───────────────────────────────────────────────────

def _us_market_holidays(year: int) -> set[date]:
    """Return approximate NYSE holidays for the given year (excludes Good Friday)."""
    try:
        from pandas.tseries.holiday import USFederalHolidayCalendar
        from pandas import Timestamp
        cal = USFederalHolidayCalendar()
        holidays_ts = cal.holidays(start=f"{year}-01-01", end=f"{year}-12-31")
        return {Timestamp(ts).date() for ts in holidays_ts}
    except Exception:
        return set()


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
    Compute the evaluation date for a prediction made on start.
    Approximately horizon_days trading days forward.
    Uses business-day offset from pandas.
    """
    try:
        from pandas import Timestamp
        from pandas.tseries.offsets import BDay
        return (Timestamp(start) + BDay(horizon_days)).date().isoformat()
    except Exception:
        # fallback: rough calendar-day approximation
        cal_days = {5: 7, 21: 30, 63: 91}.get(horizon_days, horizon_days * 7 // 5)
        return (start + timedelta(days=cal_days)).isoformat()


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


def get_today_horizons(ticker: str, today: str) -> set[int]:
    """Return the set of horizon_days already saved for ticker on today (ISO date)."""
    with _db() as c:
        rows = c.execute(
            """SELECT horizon_days FROM predictions
               WHERE ticker = ? AND as_of_date = ?
               AND horizon_days IS NOT NULL""",
            [ticker.upper(), today],
        ).fetchall()
    return {r[0] for r in rows}


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
    # Model selection metadata
    used_fallback: bool = False,
    parent_merged_id: Optional[int] = None,
    # Event-driven metadata
    trigger_type: Optional[str] = None,
    trigger_event_id: Optional[int] = None,
) -> dict:
    """
    Insert a new prediction row (append-only). One row per horizon per ticker per date.
    Returns {saved, ticker, changed, previous}.
    """
    ticker = ticker.upper()
    now_cst   = _now_cst()
    today_str = _today_cst()
    today_date = date.fromisoformat(today_str)

    # Derive horizon_days from legacy horizon string if not provided
    if horizon_days is None and horizon:
        _map = {"5d": 5, "1w": 5, "21d": 21, "1m": 21, "63d": 63, "3m": 63}
        horizon_days = _map.get(horizon)

    # Derive prediction_type
    if prediction_type is None and horizon_days:
        prediction_type = _horizon_label(horizon_days)

    # Derive evaluation_date
    if evaluation_date is None and horizon_days:
        evaluation_date = _evaluation_date(today_date, horizon_days)

    # Use reasoning_text → reasoning for backward compat
    if reasoning_text and not reasoning:
        reasoning = reasoning_text
    if not reasoning_text and reasoning:
        reasoning_text = reasoning

    prev = get_latest_prediction(ticker)
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
                        trigger_type, trigger_event_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           ?, ?, ?, ?)""",
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
                ],
            )
            c.commit()
        return {"saved": True, "ticker": ticker, "changed": bool(changed), "previous": prev_pred}
    except Exception as exc:
        return {"saved": False, "ticker": ticker, "error": str(exc)}


def get_latest_prediction(ticker: str, horizon_days: Optional[int] = None) -> Optional[Prediction]:
    """Return the most recent prediction row for ticker (optionally filtered by horizon_days)."""
    with _db() as c:
        if horizon_days is not None:
            row = c.execute(
                """SELECT * FROM predictions WHERE ticker = ? AND horizon_days = ?
                   ORDER BY created_at DESC LIMIT 1""",
                [ticker.upper(), horizon_days],
            ).fetchone()
        else:
            row = c.execute(
                """SELECT * FROM predictions WHERE ticker = ?
                   ORDER BY created_at DESC LIMIT 1""",
                [ticker.upper()],
            ).fetchone()
    if not row:
        return None
    return Prediction.from_db_row(dict(row))


def get_prediction_history(ticker: str, limit: int = 10) -> list[Prediction]:
    """Return the last limit prediction rows for ticker, newest first."""
    with _db() as c:
        rows = c.execute(
            """SELECT * FROM predictions WHERE ticker = ?
               ORDER BY created_at DESC LIMIT ?""",
            [ticker.upper(), limit],
        ).fetchall()
    return [Prediction.from_db_row(dict(row)) for row in rows]


def get_matured_pending_predictions(today: str) -> list[dict]:
    """Return predictions where evaluation_date=today and not yet evaluated."""
    with _db() as c:
        rows = c.execute(  # noqa: E501
            """SELECT * FROM predictions
               WHERE evaluation_date = ?
               AND (evaluation_status IS NULL OR evaluation_status = 'pending')
               AND horizon_days IS NOT NULL""",
            [today],
        ).fetchall()
    return [_parse_pred_row(r) for r in rows]


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


def upsert_rolling_metric(row: dict) -> None:
    with _db() as c:
        c.execute(
            """INSERT OR REPLACE INTO metrics_rolling
               (metric_date, horizon_days, segment, system_version, lookback_days,
                directional_accuracy, in_range_pct, mean_excess_return,
                mean_error_magnitude, high_conviction_accuracy, low_conviction_accuracy,
                brier_score, mean_log_loss, num_predictions, computed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                row["metric_date"], row["horizon_days"], row["segment"],
                row["system_version"], row["lookback_days"],
                row.get("directional_accuracy"), row.get("in_range_pct"),
                row.get("mean_excess_return"), row.get("mean_error_magnitude"),
                row.get("high_conviction_accuracy"), row.get("low_conviction_accuracy"),
                row.get("brier_score"), row.get("mean_log_loss"), row.get("num_predictions"),
                _now_cst(),
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
            """SELECT * FROM metrics_rolling
               WHERE metric_date = ? AND lookback_days = ?
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


def get_all_latest_predictions() -> dict[str, Prediction]:
    """Return the most recent prediction row for every ticker that has one."""
    with _db() as c:
        rows = c.execute("""
            SELECT p.* FROM predictions p
            INNER JOIN (
                SELECT ticker, MAX(id) AS max_id
                FROM predictions GROUP BY ticker
            ) latest ON p.id = latest.max_id
        """).fetchall()
    result = {}
    for row in rows:
        pred = Prediction.from_db_row(dict(row))
        result[pred.ticker] = pred
    return result
