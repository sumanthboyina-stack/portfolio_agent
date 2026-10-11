"""
Engine settings — single-row, database-backed store for the market-only
weighting engine's parameters: source weights per horizon, the
deterministic conviction guardrail thresholds, and APEX model selection
(champion, ordered fallbacks, optional challenger).

These numbers used to be Python module constants (portfolio_agent.tools.
weight_engine.BASE / HORIZON_BASE_WEIGHTS / DEFAULT_BASE_WEIGHTS / WEIGHT_FLOOR,
and the two thresholds inline in pipeline.daily.apex._apply_conviction_guardrails).
They still exist there as the SEED DEFAULTS (and as the fixed inputs Phase 1's
golden tests check), but the values weight_engine and apex actually use at
runtime now come from this table, so an administrator can tune them without a
code change or deploy. Nothing here is user-scoped: this is global, admin-only
configuration for the shared, market-only generation path.

instrument_type_horizons_json decides which prediction horizons the pipeline generates for each instrument type
(see SEED_INSTRUMENT_TYPE_HORIZONS): stocks and ETFs get all four, mutual funds / target-date funds only the long
ones (their NAV posts once a day and 401(k) frequent-trading rules make a 5-day call unactionable), cash-like
instruments none. The pipeline intersects this with the day's scheduled horizons.

The APEX model columns are seeded from apex_dual_run.DEFAULT_CHAMPION /
DEFAULT_FALLBACKS (column DEFAULTs, so an existing row picks them up when the
columns are added) and hold model DISPLAY LABELS ("Claude-Sonnet"), resolved
against apex_dual_run.APEX_MODELS at run time.

Table: engine_settings — exactly one row (id = 1, enforced by CHECK).
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from portfolio_agent.tools.db import db_conn, migrate_columns

_SOURCES = ("news", "research", "macro", "fundamentals", "valuation")

_ALL_HORIZONS = [5, 21, 63, 250]
_LONG_HORIZONS = [63, 250]
# Seed defaults: {instrument_type: [horizon_days]} — an empty list means "never predicted".
SEED_INSTRUMENT_TYPE_HORIZONS: dict[str, list[int]] = {
    "STOCK": _ALL_HORIZONS, "ETF": _ALL_HORIZONS, "ETN": _ALL_HORIZONS, "BOND": _ALL_HORIZONS,
    "OTHER": _ALL_HORIZONS, "UNKNOWN": _ALL_HORIZONS,
    "MUTUAL_FUND": _LONG_HORIZONS, "TARGET_DATE_FUND": _LONG_HORIZONS,
    "CIT": [], "STABLE_VALUE": [], "CASH": [], "MONEY_MARKET": [],
}
_UNSET = object()   # distinguishes "leave apex_challenger_model alone" from "clear it (None)"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_schema_ready: set[str] = set()


@contextmanager
def _db():
    """A connection to the engine-settings table. Create + migrate + seed run once per process per DB file, not on
    every connection (parallel pipeline threads each open one; per-connection DDL fought for the write lock)."""
    from portfolio_agent.tools import db as _db_module
    key = str(_db_module.DB_PATH)

    def _setup(conn: sqlite3.Connection) -> None:
        _create_schema(conn)
        _migrate(conn)
        _seed(conn)
        conn.commit()
        _schema_ready.add(key)
    with db_conn(setup=None if key in _schema_ready else _setup) as conn:
        yield conn


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS engine_settings (
            id                          INTEGER PRIMARY KEY CHECK (id = 1),
            base_weights                TEXT NOT NULL,
            horizon_base_weights        TEXT NOT NULL,
            default_base_weights        TEXT NOT NULL,
            weight_floor                REAL NOT NULL,
            no_news_conviction_threshold REAL NOT NULL,
            no_news_cap                 REAL NOT NULL,
            bounce_return_threshold     REAL NOT NULL,
            bounce_cap                  REAL NOT NULL,
            version                     INTEGER NOT NULL DEFAULT 1,
            created_at                  TEXT NOT NULL,
            updated_at                  TEXT NOT NULL,
            updated_by                  TEXT
        )
    """)


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent column additions. Fresh installs get them the same way (the
    CREATE above is the original shape), so there is exactly one definition."""
    from portfolio_agent.tools.apex_dual_run import DEFAULT_CHAMPION, DEFAULT_FALLBACKS

    def _lit(s: str) -> str:
        return "'" + s.replace("'", "''") + "'"

    migrate_columns(conn, "engine_settings", [
        ("apex_champion_model",        f"TEXT NOT NULL DEFAULT {_lit(DEFAULT_CHAMPION)}"),
        ("apex_fallback_models",       f"TEXT NOT NULL DEFAULT {_lit(json.dumps(list(DEFAULT_FALLBACKS)))}"),
        ("apex_challenger_model",      "TEXT"),                         # NULL = challenger off
        ("apex_challenger_sample_pct", "REAL NOT NULL DEFAULT 0"),      # 0–100, % of tickers
        ("instrument_type_horizons_json", f"TEXT NOT NULL DEFAULT {_lit(json.dumps(SEED_INSTRUMENT_TYPE_HORIZONS))}"),
    ])


def _seed(conn: sqlite3.Connection) -> None:
    """Insert the singleton row from weight_engine's own module constants —
    the seed defaults — if it doesn't exist yet. Never overwrites an existing row.
    The APEX model columns take their column DEFAULTs (see _migrate)."""
    from portfolio_agent.tools.weight_engine import BASE, DEFAULT_BASE_WEIGHTS, HORIZON_BASE_WEIGHTS, WEIGHT_FLOOR
    now = _now()
    conn.execute(
        """INSERT OR IGNORE INTO engine_settings
               (id, base_weights, horizon_base_weights, default_base_weights, weight_floor,
                no_news_conviction_threshold, no_news_cap, bounce_return_threshold, bounce_cap,
                created_at, updated_at)
           VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [json.dumps(BASE), json.dumps({str(k): v for k, v in HORIZON_BASE_WEIGHTS.items()}),
         json.dumps(DEFAULT_BASE_WEIGHTS), WEIGHT_FLOOR,
         7.0, 5.0, -0.05, 5.0, now, now],
    )


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["base_weights"] = json.loads(d["base_weights"])
    d["horizon_base_weights"] = {int(k): v for k, v in json.loads(d["horizon_base_weights"]).items()}
    d["default_base_weights"] = json.loads(d["default_base_weights"])
    try:
        d["apex_fallback_models"] = list(json.loads(d.get("apex_fallback_models") or "[]"))
    except (TypeError, ValueError):
        d["apex_fallback_models"] = []      # apex_dual_run.build_model_plan falls back to the code defaults
    try:
        stored = json.loads(d.get("instrument_type_horizons_json") or "{}")
    except (TypeError, ValueError):
        stored = {}
    # A type missing from the stored map (e.g. one added after it was saved) takes its seed default, never a guess.
    d["instrument_type_horizons"] = {t: list(stored.get(t, h)) for t, h in SEED_INSTRUMENT_TYPE_HORIZONS.items()}
    return d


def get_engine_settings(*, conn: sqlite3.Connection | None = None) -> dict:
    """The current effective settings (seeded from weight_engine's defaults on first use). Pass *conn* to read inside
    the caller's own transaction (it then sees that transaction's uncommitted changes)."""
    if conn is not None:
        return _row_to_dict(conn.execute("SELECT * FROM engine_settings WHERE id = 1").fetchone())
    with _db() as c:
        row = c.execute("SELECT * FROM engine_settings WHERE id = 1").fetchone()
    return _row_to_dict(row)


def _normalize_weights(weights: dict, *, what: str) -> dict:
    missing = set(_SOURCES) - set(weights)
    extra = set(weights) - set(_SOURCES)
    if missing or extra:
        raise ValueError(f"{what}: expected exactly the sources {sorted(_SOURCES)}, "
                         f"got {sorted(weights)} (missing {sorted(missing)}, unexpected {sorted(extra)})")
    values = [float(v) for v in weights.values()]
    if any(v < 0 for v in values):
        raise ValueError(f"{what}: weights cannot be negative")
    total = sum(values)
    if total <= 0:
        raise ValueError(f"{what}: weights must sum to a positive number")
    return {k: round(float(v) / total, 4) for k, v in weights.items()}


def _validate_horizons(horizons: dict) -> dict[str, list[int]]:
    """{instrument_type: [horizon_days]}: known types, supported horizons only, no duplicates; [] = never predicted."""
    from portfolio_agent.domain import INSTRUMENT_TYPES
    from portfolio_agent.tools.weight_engine import SUPPORTED_HORIZONS
    if not isinstance(horizons, dict) or not horizons:
        raise ValueError("instrument_type_horizons: expected {instrument_type: [horizon_days]}")
    clean: dict[str, list[int]] = {}
    for itype, hs in horizons.items():
        if itype not in INSTRUMENT_TYPES:
            raise ValueError(f"instrument_type_horizons: unknown instrument type {itype!r}")
        try:
            vals = [int(h) for h in hs]
        except (TypeError, ValueError):
            raise ValueError(f"instrument_type_horizons[{itype}]: must be a list of horizon days")
        bad = sorted(set(vals) - set(SUPPORTED_HORIZONS))
        if bad:
            raise ValueError(f"instrument_type_horizons[{itype}]: unsupported horizon(s) {bad}; supported: {list(SUPPORTED_HORIZONS)}")
        clean[itype] = sorted(set(vals))
    return clean


def horizons_for_instrument_type(instrument_type: str | None, settings: dict | None = None) -> list[int]:
    """The horizons the pipeline may generate for this instrument type. None / unclassified = the stock path."""
    settings = settings or get_engine_settings()
    table = settings["instrument_type_horizons"]
    return list(table.get(instrument_type or "UNKNOWN", table["UNKNOWN"]))


def _check_model_label(label: str, what: str) -> str:
    from portfolio_agent.tools.apex_dual_run import known_model_labels
    label = (label or "").strip()
    if label not in known_model_labels():
        raise ValueError(f"{what}: unknown APEX model {label!r}; known: {list(known_model_labels())}")
    return label


def update_engine_settings(*, updated_by: str, expected_version: int | None = None,
                           base_weights: dict | None = None,
                           horizon_base_weights: dict[int, dict] | None = None,
                           default_base_weights: dict | None = None,
                           weight_floor: float | None = None,
                           no_news_conviction_threshold: float | None = None,
                           no_news_cap: float | None = None,
                           bounce_return_threshold: float | None = None,
                           bounce_cap: float | None = None,
                           apex_champion_model: str | None = None,
                           apex_fallback_models: list[str] | None = None,
                           apex_challenger_model=_UNSET,
                           apex_challenger_sample_pct: float | None = None,
                           instrument_type_horizons: dict | None = None,
                           conn: sqlite3.Connection | None = None) -> dict:
    """
    Update one or more settings groups. Every weight dict is validated (exactly
    the five sources, non-negative) and normalized to sum to 1.0 so an admin's
    edit is never silently mis-weighted. APEX model labels must be known to
    apex_dual_run.APEX_MODELS; apex_challenger_model=None turns the challenger
    off; apex_challenger_sample_pct is a percentage 0–100. Raises ValueError on
    bad input, VersionConflict-style RuntimeError if expected_version is stale.

    conn: join the caller's transaction (nothing is committed here, so a settings change and its audit event can land
    together). The caller must have touched the engine-settings table once beforehand (get_engine_settings()) so its
    schema exists — creating it from inside someone else's write transaction would block.
    """
    fields: dict = {}
    if base_weights is not None:
        fields["base_weights"] = json.dumps(_normalize_weights(base_weights, what="base_weights"))
    if default_base_weights is not None:
        fields["default_base_weights"] = json.dumps(_normalize_weights(default_base_weights, what="default_base_weights"))
    if horizon_base_weights is not None:
        normalized = {str(h): _normalize_weights(w, what=f"horizon_base_weights[{h}]")
                     for h, w in horizon_base_weights.items()}
        fields["horizon_base_weights"] = json.dumps(normalized)
    if weight_floor is not None:
        if not (0.0 <= weight_floor <= 0.3):
            raise ValueError(f"weight_floor must be between 0.0 and 0.3, got {weight_floor}")
        fields["weight_floor"] = float(weight_floor)
    if no_news_conviction_threshold is not None:
        fields["no_news_conviction_threshold"] = float(no_news_conviction_threshold)
    if no_news_cap is not None:
        fields["no_news_cap"] = float(no_news_cap)
    if bounce_return_threshold is not None:
        fields["bounce_return_threshold"] = float(bounce_return_threshold)
    if bounce_cap is not None:
        fields["bounce_cap"] = float(bounce_cap)
    if apex_champion_model is not None:
        fields["apex_champion_model"] = _check_model_label(apex_champion_model, "apex_champion_model")
    if apex_fallback_models is not None:
        labels = [_check_model_label(m, "apex_fallback_models") for m in apex_fallback_models]
        fields["apex_fallback_models"] = json.dumps(list(dict.fromkeys(labels)))
    if apex_challenger_model is not _UNSET:
        fields["apex_challenger_model"] = (None if apex_challenger_model in (None, "")
                                           else _check_model_label(apex_challenger_model, "apex_challenger_model"))
    if apex_challenger_sample_pct is not None:
        if not (0.0 <= float(apex_challenger_sample_pct) <= 100.0):
            raise ValueError(f"apex_challenger_sample_pct must be between 0 and 100, got {apex_challenger_sample_pct}")
        fields["apex_challenger_sample_pct"] = float(apex_challenger_sample_pct)
    if instrument_type_horizons is not None:
        merged = {**get_engine_settings(conn=conn)["instrument_type_horizons"], **_validate_horizons(instrument_type_horizons)}
        fields["instrument_type_horizons_json"] = json.dumps(merged)
    if not fields:
        return get_engine_settings(conn=conn)

    fields["updated_at"] = _now()
    fields["updated_by"] = updated_by
    cols = list(fields)
    sql = (f"UPDATE engine_settings SET {', '.join(f'{c} = ?' for c in cols)}, "
           "version = version + 1 WHERE id = 1")
    params = [fields[c] for c in cols]
    if expected_version is not None:
        sql += " AND version = ?"
        params.append(expected_version)
    if conn is not None:
        n = conn.execute(sql, params).rowcount
        if n == 0 and expected_version is not None:
            raise RuntimeError("engine settings changed since you loaded them; reload and retry")
        return get_engine_settings(conn=conn)
    with _db() as c:
        n = c.execute(sql, params).rowcount
        c.commit()          # db_conn() never commits on exit; without this every update was silently rolled back
    if n == 0 and expected_version is not None:
        raise RuntimeError("engine settings changed since you loaded them; reload and retry")
    return get_engine_settings()


def reset_engine_settings_to_defaults(*, updated_by: str) -> dict:
    """Restore the exact weight_engine module defaults (undoes every weight / guardrail override) and the seeded
    per-instrument-type horizons. APEX model selection is a separate choice and is left as it is."""
    from portfolio_agent.tools.weight_engine import BASE, DEFAULT_BASE_WEIGHTS, HORIZON_BASE_WEIGHTS, WEIGHT_FLOOR
    return update_engine_settings(
        updated_by=updated_by, base_weights=dict(BASE), default_base_weights=dict(DEFAULT_BASE_WEIGHTS),
        horizon_base_weights={k: dict(v) for k, v in HORIZON_BASE_WEIGHTS.items()}, weight_floor=WEIGHT_FLOOR,
        no_news_conviction_threshold=7.0, no_news_cap=5.0, bounce_return_threshold=-0.05, bounce_cap=5.0,
        instrument_type_horizons={t: list(h) for t, h in SEED_INSTRUMENT_TYPE_HORIZONS.items()},
    )
