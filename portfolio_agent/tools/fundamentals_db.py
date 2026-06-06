"""
Fundamentals database tools — read/write the fundamentals table (SQLite).

One row per (ticker, filing_date). Refreshed when:
  - EDGAR shows a newer filing_date than stored, OR
  - Stored as_of_date is older than max_age_days (default 95 — catches missed filings).
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Optional

from portfolio_agent.domain import FundamentalsSnapshot
from portfolio_agent.tools.db import (
    db_conn, migrate_columns, safe_float as _f,
    to_json_list as _to_json_list, from_json_list as _parse_json_list,
)


@contextmanager
def _db():
    def _setup(conn: sqlite3.Connection) -> None:
        _create_schema(conn)
        _migrate(conn)
        conn.commit()
    with db_conn(setup=_setup) as conn:
        yield conn


def _migrate(conn: sqlite3.Connection) -> None:
    migrate_columns(conn, "fundamentals", [
        ("summary",        "TEXT"),
        ("updated_at",     "TEXT DEFAULT (datetime('now'))"),
        ("model_name",     "TEXT"),
        ("model_provider", "TEXT"),
    ])


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fundamentals (
            id                     INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker                 TEXT    NOT NULL,
            as_of_date             TEXT    NOT NULL,
            filing_type            TEXT,
            filing_date            TEXT,
            revenue_growth_yoy_pct REAL,
            net_margin             REAL,
            fcf                    REAL,
            debt_to_equity         REAL,
            fundamental_score      INTEGER,
            key_strengths          TEXT,
            key_risks              TEXT,
            summary                TEXT,
            raw_filing_ref         TEXT,
            updated_at             TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uidx_fundamentals_ticker_filing
        ON fundamentals(ticker, filing_date)
    """)


# ── read helpers ───────────────────────────────────────────────────────────────

def get_stored_fundamentals(ticker: str) -> Optional[FundamentalsSnapshot]:
    """Return the most recently stored fundamentals row for *ticker*, or None."""
    with _db() as c:
        row = c.execute(
            """SELECT * FROM fundamentals
               WHERE ticker = ?
               ORDER BY filing_date DESC NULLS LAST
               LIMIT 1""",
            [ticker.upper()],
        ).fetchone()
    if not row:
        return None
    return FundamentalsSnapshot.from_db_row(dict(row))


def needs_refresh(
    ticker: str,
    latest_filing_date: Optional[str],
    max_age_days: int = 95,
) -> tuple[bool, str]:
    """
    Decide whether to re-run the fundamentals agent for *ticker*.

    Logic (checked in order):
      1. No stored row → refresh
      2. latest_filing_date (from EDGAR) > stored filing_date → new filing, refresh
      3. stored as_of_date older than max_age_days → stale fallback, refresh
      4. Otherwise → skip

    Returns:
        (needs_refresh: bool, reason: str)
    """
    stored = get_stored_fundamentals(ticker)
    if not stored:
        return True, "no_data"

    stored_filing = stored.get("filing_date") or ""
    if latest_filing_date and latest_filing_date > stored_filing:
        return True, "new_filing"

    anchor = stored.get("as_of_date") or stored_filing
    if anchor:
        try:
            age = (date.today() - date.fromisoformat(anchor)).days
            if age > max_age_days:
                return True, f"stale_{age}d"
        except ValueError:
            pass

    return False, "current"


# ── write helpers ──────────────────────────────────────────────────────────────

def upsert_fundamentals(
    ticker: str,
    as_of_date: str,
    filing_type: str = "",
    filing_date: str = "",
    revenue_growth_yoy_pct: Optional[float] = None,
    net_margin: Optional[float] = None,
    fcf: Optional[float] = None,
    debt_to_equity: Optional[float] = None,
    fundamental_score: Optional[int] = None,
    key_strengths=None,
    key_risks=None,
    summary: str = "",
    raw_filing_ref: str = "",
    model_name: Optional[str] = None,
    model_provider: Optional[str] = None,
) -> dict:
    """
    Insert or update a fundamentals row.

    Unique key is (ticker, filing_date). Safe to call multiple times — updates
    the existing row if one already exists for this ticker + filing date.

    Returns:
        {saved, ticker, action: "inserted"|"updated"}
    """
    ticker = ticker.upper()
    filing_date_key = filing_date or as_of_date

    strengths_str = _to_json_list(key_strengths)
    risks_str = _to_json_list(key_risks)

    try:
        with _db() as c:
            existing = c.execute(
                "SELECT id FROM fundamentals WHERE ticker = ? AND filing_date = ?",
                [ticker, filing_date_key],
            ).fetchone()

            if existing:
                c.execute(
                    """UPDATE fundamentals SET
                           as_of_date             = ?,
                           filing_type            = ?,
                           revenue_growth_yoy_pct = ?,
                           net_margin             = ?,
                           fcf                    = ?,
                           debt_to_equity         = ?,
                           fundamental_score      = ?,
                           key_strengths          = ?,
                           key_risks              = ?,
                           summary                = ?,
                           raw_filing_ref         = ?,
                           model_name             = COALESCE(?, model_name),
                           model_provider         = COALESCE(?, model_provider),
                           updated_at             = datetime('now')
                       WHERE id = ?""",
                    [as_of_date, filing_type,
                     _f(revenue_growth_yoy_pct), _f(net_margin), _f(fcf), _f(debt_to_equity),
                     fundamental_score, strengths_str, risks_str, summary, raw_filing_ref,
                     model_name, model_provider, existing["id"]],
                )
                action = "updated"
            else:
                c.execute(
                    """INSERT INTO fundamentals
                           (ticker, as_of_date, filing_type, filing_date,
                            revenue_growth_yoy_pct, net_margin, fcf, debt_to_equity,
                            fundamental_score, key_strengths, key_risks,
                            summary, raw_filing_ref, model_name, model_provider, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                    [ticker, as_of_date, filing_type, filing_date_key,
                     _f(revenue_growth_yoy_pct), _f(net_margin), _f(fcf), _f(debt_to_equity),
                     fundamental_score, strengths_str, risks_str, summary, raw_filing_ref,
                     model_name, model_provider],
                )
                action = "inserted"

            c.commit()
        return {"saved": True, "ticker": ticker, "action": action}
    except Exception as exc:
        return {"saved": False, "ticker": ticker, "error": str(exc)}
