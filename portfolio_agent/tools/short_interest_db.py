"""
Short interest database — read/write the short_interest table (SQLite).

One row per (ticker, settlement_date). FINRA publishes two settlement dates per
month (~15th and last business day). We store 2-3 periods per ticker so the
Python trend layer can compute direction without an LLM.

Refresh cadence tracking: we record the last fully-fetched settlement date in
the short_interest_meta table so the morning pipeline knows whether today's
FINRA data drop is new.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from portfolio_agent.tools.db import db_conn, migrate_columns, safe_float as _f

_CST = ZoneInfo("America/Chicago")


# ── DB connection + schema ─────────────────────────────────────────────────────

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
        CREATE TABLE IF NOT EXISTS short_interest (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker           TEXT    NOT NULL,
            settlement_date  TEXT    NOT NULL,
            current_shares   INTEGER,
            prev_shares      INTEGER,
            pct_change       REAL,
            avg_daily_volume INTEGER,
            days_to_cover    REAL,
            fetched_at       TEXT    DEFAULT (datetime('now')),
            UNIQUE(ticker, settlement_date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS short_interest_meta (
            settlement_date  TEXT PRIMARY KEY,
            fetched_at       TEXT DEFAULT (datetime('now')),
            ticker_count     INTEGER
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_si_ticker_date
        ON short_interest(ticker, settlement_date DESC)
    """)


def _migrate(conn: sqlite3.Connection) -> None:
    migrate_columns(conn, "short_interest", [
        ("fetched_at", "TEXT DEFAULT (datetime('now'))"),
    ])


# ── cadence check ──────────────────────────────────────────────────────────────

def needs_refresh(settlement_date: str) -> bool:
    """Return True if we have not yet fetched data for *settlement_date*."""
    with _db() as c:
        row = c.execute(
            "SELECT settlement_date FROM short_interest_meta WHERE settlement_date = ?",
            [settlement_date],
        ).fetchone()
    return row is None


def mark_fetched(settlement_date: str, ticker_count: int) -> None:
    """Record that *settlement_date* has been fully fetched."""
    with _db() as c:
        c.execute(
            """INSERT INTO short_interest_meta (settlement_date, ticker_count)
               VALUES (?, ?)
               ON CONFLICT(settlement_date) DO UPDATE SET
                   fetched_at   = datetime('now'),
                   ticker_count = excluded.ticker_count""",
            [settlement_date, ticker_count],
        )
        c.commit()


# ── write helpers ──────────────────────────────────────────────────────────────

def upsert_short_interest(
    ticker: str,
    settlement_date: str,
    current_shares: Optional[int] = None,
    prev_shares: Optional[int] = None,
    pct_change: Optional[float] = None,
    avg_daily_volume: Optional[int] = None,
    days_to_cover: Optional[float] = None,
) -> None:
    ticker = ticker.upper()
    with _db() as c:
        c.execute(
            """INSERT INTO short_interest
                   (ticker, settlement_date, current_shares, prev_shares,
                    pct_change, avg_daily_volume, days_to_cover, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(ticker, settlement_date) DO UPDATE SET
                   current_shares   = excluded.current_shares,
                   prev_shares      = excluded.prev_shares,
                   pct_change       = excluded.pct_change,
                   avg_daily_volume = excluded.avg_daily_volume,
                   days_to_cover    = excluded.days_to_cover,
                   fetched_at       = datetime('now')""",
            [ticker, settlement_date, current_shares, prev_shares,
             _f(pct_change), avg_daily_volume, _f(days_to_cover)],
        )
        c.commit()


def batch_upsert(rows: list[dict]) -> int:
    """Upsert a batch of FINRA rows; returns the number of rows written."""
    written = 0
    with _db() as c:
        for r in rows:
            c.execute(
                """INSERT INTO short_interest
                       (ticker, settlement_date, current_shares, prev_shares,
                        pct_change, avg_daily_volume, days_to_cover, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
                   ON CONFLICT(ticker, settlement_date) DO UPDATE SET
                       current_shares   = excluded.current_shares,
                       prev_shares      = excluded.prev_shares,
                       pct_change       = excluded.pct_change,
                       avg_daily_volume = excluded.avg_daily_volume,
                       days_to_cover    = excluded.days_to_cover,
                       fetched_at       = datetime('now')""",
                [
                    r["ticker"].upper(), r["settlement_date"],
                    r.get("current_shares"), r.get("prev_shares"),
                    _f(r.get("pct_change")), r.get("avg_daily_volume"),
                    _f(r.get("days_to_cover")),
                ],
            )
            written += 1
        c.commit()
    return written


# ── read helpers ───────────────────────────────────────────────────────────────

def get_history(ticker: str, limit: int = 3) -> list[dict]:
    """Return the last *limit* settlement records for *ticker*, newest first."""
    with _db() as c:
        rows = c.execute(
            """SELECT * FROM short_interest
               WHERE ticker = ?
               ORDER BY settlement_date DESC
               LIMIT ?""",
            [ticker.upper(), limit],
        ).fetchall()
    return [dict(r) for r in rows]


# ── trend computation (pure Python, no LLM) ────────────────────────────────────

def compute_trend(ticker: str) -> dict:
    """
    Derive short interest trend from the last 2-3 settlement records.

    Returns a compact summary dict suitable for injecting into the Research
    prompt — same philosophy as the weight engine: Python does the maths,
    the LLM sees the pre-digested signal.
    """
    rows = get_history(ticker, limit=3)
    if not rows:
        return {"ticker": ticker.upper(), "available": False}

    latest = rows[0]
    current = latest.get("current_shares")
    dtc     = latest.get("days_to_cover")
    pct     = latest.get("pct_change")

    # Direction: average the pct_change values across the last 1-2 periods
    changes = [r["pct_change"] for r in rows if r.get("pct_change") is not None]
    if len(changes) >= 1:
        avg_chg = sum(changes) / len(changes)
        if avg_chg > 5:
            direction = "rising"
        elif avg_chg < -5:
            direction = "falling"
        else:
            direction = "stable"
    else:
        direction = "unknown"

    # Squeeze pressure proxy: days-to-cover ≥ 5 is elevated, ≥ 10 is high
    squeeze = (
        "high"     if dtc is not None and dtc >= 10 else
        "elevated" if dtc is not None and dtc >= 5  else
        "low"      if dtc is not None                else
        "unknown"
    )

    return {
        "ticker":          ticker.upper(),
        "available":       True,
        "settlement_date": latest.get("settlement_date"),
        "current_shares":  current,
        "pct_change":      pct,
        "avg_daily_volume": latest.get("avg_daily_volume"),
        "days_to_cover":   dtc,
        "trend":           direction,
        "squeeze_pressure": squeeze,
        "periods_available": len(rows),
    }
