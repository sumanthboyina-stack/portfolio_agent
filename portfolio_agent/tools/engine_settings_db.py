"""
Engine settings — single-row, database-backed store for the market-only
weighting engine's parameters: source weights per horizon and the
deterministic conviction guardrail thresholds.

These numbers used to be Python module constants (portfolio_agent.tools.
weight_engine.BASE / HORIZON_BASE_WEIGHTS / DEFAULT_BASE_WEIGHTS / WEIGHT_FLOOR,
and the two thresholds inline in pipeline.daily.apex._apply_conviction_guardrails).
They still exist there as the SEED DEFAULTS (and as the fixed inputs Phase 1's
golden tests check), but the values weight_engine and apex actually use at
runtime now come from this table, so an administrator can tune them without a
code change or deploy. Nothing here is user-scoped: this is global, admin-only
configuration for the shared, market-only generation path.

Table: engine_settings — exactly one row (id = 1, enforced by CHECK).
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from portfolio_agent.tools.db import db_conn

_SOURCES = ("news", "research", "macro", "fundamentals", "valuation")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _db():
    def _setup(conn: sqlite3.Connection) -> None:
        _create_schema(conn)
        _seed(conn)
        conn.commit()
    with db_conn(setup=_setup) as conn:
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


def _seed(conn: sqlite3.Connection) -> None:
    """Insert the singleton row from weight_engine's own module constants —
    the seed defaults — if it doesn't exist yet. Never overwrites an existing row."""
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
    return d


def get_engine_settings() -> dict:
    """The current effective settings (seeded from weight_engine's defaults on first use)."""
    with _db() as conn:
        row = conn.execute("SELECT * FROM engine_settings WHERE id = 1").fetchone()
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


def update_engine_settings(*, updated_by: str, expected_version: int | None = None,
                           base_weights: dict | None = None,
                           horizon_base_weights: dict[int, dict] | None = None,
                           default_base_weights: dict | None = None,
                           weight_floor: float | None = None,
                           no_news_conviction_threshold: float | None = None,
                           no_news_cap: float | None = None,
                           bounce_return_threshold: float | None = None,
                           bounce_cap: float | None = None) -> dict:
    """
    Update one or more settings groups. Every weight dict is validated (exactly
    the five sources, non-negative) and normalized to sum to 1.0 so an admin's
    edit is never silently mis-weighted. Raises ValueError on bad input,
    VersionConflict-style RuntimeError if expected_version is stale.
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
    if not fields:
        return get_engine_settings()

    fields["updated_at"] = _now()
    fields["updated_by"] = updated_by
    cols = list(fields)
    sql = (f"UPDATE engine_settings SET {', '.join(f'{c} = ?' for c in cols)}, "
           "version = version + 1 WHERE id = 1")
    params = [fields[c] for c in cols]
    if expected_version is not None:
        sql += " AND version = ?"
        params.append(expected_version)
    with _db() as conn:
        n = conn.execute(sql, params).rowcount
    if n == 0 and expected_version is not None:
        raise RuntimeError("engine settings changed since you loaded them; reload and retry")
    return get_engine_settings()


def reset_engine_settings_to_defaults(*, updated_by: str) -> dict:
    """Restore the exact weight_engine module defaults (undoes every admin override)."""
    from portfolio_agent.tools.weight_engine import BASE, DEFAULT_BASE_WEIGHTS, HORIZON_BASE_WEIGHTS, WEIGHT_FLOOR
    return update_engine_settings(
        updated_by=updated_by, base_weights=dict(BASE), default_base_weights=dict(DEFAULT_BASE_WEIGHTS),
        horizon_base_weights={k: dict(v) for k, v in HORIZON_BASE_WEIGHTS.items()}, weight_floor=WEIGHT_FLOOR,
        no_news_conviction_threshold=7.0, no_news_cap=5.0, bounce_return_threshold=-0.05, bounce_cap=5.0,
    )
