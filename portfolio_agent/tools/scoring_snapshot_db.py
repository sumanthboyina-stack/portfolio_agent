"""
Score Calibration storage — one row per (ticker, as_of_date) capturing every
parallel scoring system (APEX composite, Valuation, Opportunity Engine,
Portfolio Fit, and a blended Final) on the same day, plus realized forward
returns at 30/60/90/250 days once each matures.

The point: none of these scores (besides APEX's own composite_score) were
ever persisted before this table existed, so there was no way to retrospectively
ask "did the Opportunity Engine's ranking actually predict returns better than
plain APEX." This table is what makes that question answerable — see
calibration_analysis.py for the actual comparison.

Outcome-fill follows the same idiom as universe_db.py's screener outcome
tracking (record entry now, diff against an exit price once matured) rather
than APEX's richer direction/bucket/Brier evaluation — these are continuous
0-100 (or 1-10) scores being correlated against a continuous return, not
categorical BUY/SELL calls needing a hit/miss judgment.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Optional

from portfolio_agent.tools.db import db_conn, safe_float

HORIZONS = (30, 60, 90, 250)
_BENCHMARK = "SPY"
_OUTCOME_GRACE_DAYS = 90  # stop retrying a horizon this far past its target date


@contextmanager
def _db():
    def _setup(conn: sqlite3.Connection) -> None:
        _create_schema(conn)
        conn.commit()
    with db_conn(setup=_setup) as conn:
        yield conn


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS scoring_snapshots (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker               TEXT NOT NULL,
            as_of_date           TEXT NOT NULL,
            entry_price          REAL,
            apex_score           REAL,
            valuation_score      REAL,
            opportunity_score    REAL,
            portfolio_fit_score  REAL,
            final_score          REAL,
            return_30d           REAL,
            return_60d           REAL,
            return_90d           REAL,
            return_250d          REAL,
            benchmark_return_30d  REAL,
            benchmark_return_60d  REAL,
            benchmark_return_90d  REAL,
            benchmark_return_250d REAL,
            created_at           TEXT DEFAULT (datetime('now')),
            UNIQUE(ticker, as_of_date)
        );

        CREATE INDEX IF NOT EXISTS idx_scoring_snapshots_date
            ON scoring_snapshots(as_of_date);
    """)


def upsert_snapshot(
    ticker: str,
    as_of_date: str,
    entry_price: Optional[float],
    apex_score: Optional[float],
    valuation_score: Optional[float],
    opportunity_score: Optional[float],
    portfolio_fit_score: Optional[float],
    final_score: Optional[float],
) -> None:
    with _db() as c:
        c.execute(
            """
            INSERT INTO scoring_snapshots
                (ticker, as_of_date, entry_price, apex_score, valuation_score,
                 opportunity_score, portfolio_fit_score, final_score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, as_of_date) DO UPDATE SET
                entry_price         = excluded.entry_price,
                apex_score          = excluded.apex_score,
                valuation_score     = excluded.valuation_score,
                opportunity_score   = excluded.opportunity_score,
                portfolio_fit_score = excluded.portfolio_fit_score,
                final_score         = excluded.final_score
            """,
            (
                ticker.upper(), as_of_date, safe_float(entry_price),
                safe_float(apex_score), safe_float(valuation_score),
                safe_float(opportunity_score), safe_float(portfolio_fit_score),
                safe_float(final_score),
            ),
        )
        c.commit()


def get_snapshots_needing_outcome(horizon_days: int, limit: int = 300) -> list[dict]:
    """
    Rows whose as_of_date is old enough for *horizon_days* to have matured,
    that horizon's return not yet recorded, and not so old we've given up
    (permanently delisted / no data ever available).
    """
    today = date.today()
    max_as_of = (today - timedelta(days=horizon_days)).isoformat()
    min_as_of = (today - timedelta(days=horizon_days + _OUTCOME_GRACE_DAYS)).isoformat()
    col = f"return_{horizon_days}d"
    with _db() as c:
        rows = c.execute(
            f"""
            SELECT id, ticker, as_of_date, entry_price
            FROM scoring_snapshots
            WHERE {col} IS NULL
              AND as_of_date <= ?
              AND as_of_date >= ?
            ORDER BY as_of_date ASC
            LIMIT ?
            """,
            (max_as_of, min_as_of, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def record_outcome(row_id: int, horizon_days: int, fwd_return: float, benchmark_return: Optional[float]) -> None:
    with _db() as c:
        c.execute(
            f"UPDATE scoring_snapshots SET return_{horizon_days}d = ?, benchmark_return_{horizon_days}d = ? WHERE id = ?",
            (fwd_return, benchmark_return, row_id),
        )
        c.commit()


def fill_matured_outcomes() -> dict[int, dict]:
    """
    For each of the four horizons independently, fetch the exit price (+ SPY)
    at as_of_date + horizon_days and record the realized return. A row can
    have some horizons filled and others still pending (e.g. 30d done, 250d
    not due yet). Returns {horizon_days: {"evaluated": n, "data_missing": n}}.
    """
    from portfolio_agent.tools.yfinance_tools import get_close

    results: dict[int, dict] = {}
    for horizon_days in HORIZONS:
        due = get_snapshots_needing_outcome(horizon_days)
        evaluated = 0
        data_missing = 0
        for row in due:
            entry = row.get("entry_price")
            if not entry:
                data_missing += 1
                continue
            target_date = (date.fromisoformat(row["as_of_date"]) + timedelta(days=horizon_days)).isoformat()
            exit_ = get_close(row["ticker"], target_date)
            if not exit_:
                data_missing += 1
                continue

            spy_entry = get_close(_BENCHMARK, row["as_of_date"])
            spy_exit = get_close(_BENCHMARK, target_date)
            benchmark_return = (spy_exit / spy_entry - 1.0) if spy_entry and spy_exit else None

            record_outcome(row["id"], horizon_days, exit_ / entry - 1.0, benchmark_return)
            evaluated += 1
        results[horizon_days] = {"evaluated": evaluated, "data_missing": data_missing}
    return results


def get_snapshots_for_calibration(horizon_days: int) -> list[dict]:
    """Matured rows for *horizon_days*: all scores + the realized return."""
    col = f"return_{horizon_days}d"
    bench_col = f"benchmark_return_{horizon_days}d"
    with _db() as c:
        rows = c.execute(
            f"""
            SELECT ticker, as_of_date, apex_score, valuation_score, opportunity_score,
                   portfolio_fit_score, final_score, {col} AS return_pct, {bench_col} AS benchmark_return_pct
            FROM scoring_snapshots
            WHERE {col} IS NOT NULL
            """
        ).fetchall()
    return [dict(r) for r in rows]


def get_latest_snapshot_date() -> Optional[str]:
    with _db() as c:
        row = c.execute("SELECT MAX(as_of_date) AS d FROM scoring_snapshots").fetchone()
    return row["d"] if row and row["d"] else None


def get_snapshots_for_date(as_of_date: str) -> list[dict]:
    with _db() as c:
        rows = c.execute(
            """
            SELECT ticker, apex_score, valuation_score, opportunity_score,
                   portfolio_fit_score, final_score
            FROM scoring_snapshots
            WHERE as_of_date = ?
            ORDER BY final_score DESC
            """,
            (as_of_date,),
        ).fetchall()
    return [dict(r) for r in rows]
