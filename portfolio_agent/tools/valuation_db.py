"""
Valuation storage — one row per (ticker, as_of_date): DCF bear/base/bull
intrinsic values + probabilities, margin of safety, relative valuation vs
peers/market, and the sensitivity grid. Pure storage, mirrors risk_flags_db.py's
pattern — decoupled from `predictions` (APEX reads the compact valuation_score
it needs via reasoning_tools.py, this table holds the full detail for the
Valuation page).
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date
from typing import Optional

from portfolio_agent.tools.db import db_conn, safe_float


@contextmanager
def _db():
    def _setup(conn: sqlite3.Connection) -> None:
        _create_schema(conn)
        conn.commit()
    with db_conn(setup=_setup) as conn:
        yield conn


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS valuation (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker               TEXT NOT NULL,
            as_of_date           TEXT NOT NULL,
            current_price        REAL,
            intrinsic_bear       REAL,
            intrinsic_base       REAL,
            intrinsic_bull       REAL,
            probability_bear     REAL,
            probability_base     REAL,
            probability_bull     REAL,
            expected_value       REAL,
            margin_of_safety_pct REAL,
            valuation_label      TEXT,
            assumptions          TEXT,
            relative_valuation   TEXT,
            sensitivity_grid     TEXT,
            model_name           TEXT,
            created_at           TEXT DEFAULT (datetime('now')),
            UNIQUE(ticker, as_of_date)
        );

        CREATE INDEX IF NOT EXISTS idx_valuation_date
            ON valuation(as_of_date);
    """)


def upsert_valuation(
    ticker: str,
    scenarios: dict,
    relative_valuation: Optional[dict] = None,
    sensitivity_grid: Optional[dict] = None,
    model_name: str = "deterministic",
    as_of_date: Optional[str] = None,
) -> None:
    """scenarios: the dict returned by valuation_engine.run_dcf_scenarios()."""
    as_of = as_of_date or date.today().isoformat()
    with _db() as c:
        c.execute(
            """
            INSERT INTO valuation
                (ticker, as_of_date, current_price, intrinsic_bear, intrinsic_base,
                 intrinsic_bull, probability_bear, probability_base, probability_bull,
                 expected_value, margin_of_safety_pct, valuation_label,
                 assumptions, relative_valuation, sensitivity_grid, model_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, as_of_date) DO UPDATE SET
                current_price        = excluded.current_price,
                intrinsic_bear       = excluded.intrinsic_bear,
                intrinsic_base       = excluded.intrinsic_base,
                intrinsic_bull       = excluded.intrinsic_bull,
                probability_bear     = excluded.probability_bear,
                probability_base     = excluded.probability_base,
                probability_bull     = excluded.probability_bull,
                expected_value       = excluded.expected_value,
                margin_of_safety_pct = excluded.margin_of_safety_pct,
                valuation_label      = excluded.valuation_label,
                assumptions          = excluded.assumptions,
                relative_valuation   = excluded.relative_valuation,
                sensitivity_grid     = excluded.sensitivity_grid,
                model_name           = excluded.model_name,
                created_at           = datetime('now')
            """,
            (
                ticker.upper(), as_of,
                safe_float(scenarios.get("current_price")),
                safe_float(scenarios.get("intrinsic_bear")),
                safe_float(scenarios.get("intrinsic_base")),
                safe_float(scenarios.get("intrinsic_bull")),
                safe_float(scenarios.get("probability_bear")),
                safe_float(scenarios.get("probability_base")),
                safe_float(scenarios.get("probability_bull")),
                safe_float(scenarios.get("expected_value")),
                safe_float(scenarios.get("margin_of_safety_pct")),
                scenarios.get("valuation_label"),
                json.dumps(scenarios.get("assumptions") or {}),
                json.dumps(relative_valuation or {}),
                json.dumps(sensitivity_grid or {}),
                model_name,
            ),
        )
        c.commit()


def get_stored_valuation(ticker: str) -> Optional[dict]:
    """Most recent valuation row for ticker, with JSON columns parsed."""
    with _db() as c:
        row = c.execute(
            "SELECT * FROM valuation WHERE ticker = ? ORDER BY as_of_date DESC LIMIT 1",
            (ticker.upper(),),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    for key in ("assumptions", "relative_valuation", "sensitivity_grid"):
        try:
            d[key] = json.loads(d.get(key) or "{}")
        except (TypeError, ValueError):
            d[key] = {}
    return d


def needs_refresh(ticker: str, max_age_days: int = 95) -> bool:
    """True if there's no stored valuation, or it's older than max_age_days."""
    stored = get_stored_valuation(ticker)
    if not stored:
        return True
    try:
        age = (date.today() - date.fromisoformat(stored["as_of_date"])).days
        return age > max_age_days
    except (TypeError, ValueError):
        return True
