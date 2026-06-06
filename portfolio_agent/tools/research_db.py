"""
Research database — read/write the research table (SQLite).

Two-track update strategy:
  Track A — Daily raw  (no LLM): consensus, targets, recent firm actions.
             Stored every day via upsert_raw_research().
  Track B — LLM summary (weekly or post-earnings): highlights, score, narrative.
             Stored when needs_llm_summary() returns True.

Schema has two separate timestamp columns to track each track independently:
  raw_fetched_at     — last time Track A was refreshed
  last_llm_run_date  — last time Track B ran the LLM
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Optional

from portfolio_agent.domain import ResearchSnapshot
from portfolio_agent.tools.db import (
    db_conn, migrate_columns, safe_float as _f,
    to_json as _to_json, from_json_list as _parse_list,
)

_LLM_CADENCE_DAYS = 7   # minimum days between LLM summary runs


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
        CREATE TABLE IF NOT EXISTS research (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker               TEXT    NOT NULL UNIQUE,
            -- Track A: daily raw (no LLM)
            raw_fetched_at       TEXT,
            consensus            TEXT,
            consensus_mean       REAL,
            num_analysts         INTEGER,
            price_target_avg     REAL,
            price_target_high    REAL,
            price_target_low     REAL,
            current_price        REAL,
            upside_to_mean_pct   REAL,
            latest_upgrade_date  TEXT,
            recent_upgrades      TEXT,
            quarterly_ratings    TEXT,
            -- Track B: weekly LLM summary
            last_llm_run_date    TEXT,
            highlights           TEXT,
            research_score       INTEGER,
            summary              TEXT,
            -- legacy / compat
            as_of_date           TEXT,
            updated_at           TEXT DEFAULT (datetime('now'))
        )
    """)


def _migrate(conn: sqlite3.Connection) -> None:
    migrate_columns(conn, "research", [
        ("raw_fetched_at",      "TEXT"),
        ("last_llm_run_date",   "TEXT"),
        ("model_name",          "TEXT"),
        ("model_provider",      "TEXT"),
        ("rec_trend",           "TEXT"),
        ("finnhub_pt",          "TEXT"),
        ("price_target_median", "REAL"),
    ])


# ── read helpers ───────────────────────────────────────────────────────────────

def get_stored_research(ticker: str) -> Optional[ResearchSnapshot]:
    """Return the stored research row for *ticker*, or None."""
    with _db() as c:
        row = c.execute(
            "SELECT * FROM research WHERE ticker = ?", [ticker.upper()]
        ).fetchone()
    return ResearchSnapshot.from_db_row(dict(row)) if row else None


def get_all_stored_research(tickers: list[str]) -> dict[str, ResearchSnapshot]:
    """Batch fetch stored research for a list of tickers (single query)."""
    if not tickers:
        return {}
    ph = ",".join("?" * len(tickers))
    with _db() as c:
        rows = c.execute(
            f"SELECT * FROM research WHERE ticker IN ({ph})",
            [t.upper() for t in tickers],
        ).fetchall()
    return {
        snap.ticker: snap
        for row in rows
        for snap in [ResearchSnapshot.from_db_row(dict(row))]
    }


def needs_llm_summary(ticker: str) -> tuple[bool, str]:
    """
    Pure-Python decision on whether to run the LLM research summary.

    Triggers (in order):
      1. No LLM summary ever stored                    → "no_summary"
      2. Last LLM run ≥ _LLM_CADENCE_DAYS days ago    → "weekly_Nd"
      3. New firm upgrade/downgrade since last LLM run → "new_action_YYYY-MM-DD"
         (uses latest_upgrade_date stored in Track A — no extra API call)
      4. Post-earnings: new filing in fundamentals
         table is more recent than last LLM run        → "post_earnings_YYYY-MM-DD"

    Returns (needs_llm: bool, reason: str).
    """
    stored = get_stored_research(ticker)
    if not stored or not stored.get("last_llm_run_date"):
        return True, "no_summary"

    last_llm = stored["last_llm_run_date"]
    try:
        age_days = (date.today() - date.fromisoformat(last_llm)).days
    except ValueError:
        return True, "bad_date"

    if age_days >= _LLM_CADENCE_DAYS:
        return True, f"weekly_{age_days}d"

    # New firm action since last LLM run (Track A already stored latest_upgrade_date)
    latest_upgrade = stored.get("latest_upgrade_date") or ""
    if latest_upgrade and latest_upgrade > last_llm:
        return True, f"new_action_{latest_upgrade}"

    # Post-earnings trigger — check fundamentals table for a newer filing
    try:
        with _db() as c:
            row = c.execute(
                """SELECT filing_date FROM fundamentals
                   WHERE ticker = ?
                   ORDER BY filing_date DESC LIMIT 1""",
                [ticker.upper()],
            ).fetchone()
        if row and row["filing_date"] and row["filing_date"] > last_llm:
            return True, f"post_earnings_{row['filing_date']}"
    except Exception:
        pass

    return False, "current"


# ── write helpers ──────────────────────────────────────────────────────────────

def upsert_raw_research(
    ticker: str,
    consensus: str = "",
    consensus_mean: Optional[float] = None,
    num_analysts: Optional[int] = None,
    price_target_avg: Optional[float] = None,
    price_target_median: Optional[float] = None,
    price_target_high: Optional[float] = None,
    price_target_low: Optional[float] = None,
    current_price: Optional[float] = None,
    upside_to_mean_pct: Optional[float] = None,
    latest_upgrade_date: str = "",
    recent_upgrades=None,
    quarterly_ratings=None,
    rec_trend=None,       # Finnhub monthly recommendation snapshots
    finnhub_pt=None,      # Finnhub independent price target dict
) -> dict:
    """
    Track A — store raw broker data only (no LLM fields touched).

    Safe to call every day; never overwrites highlights / summary / score.
    """
    ticker = ticker.upper()
    today  = date.today().isoformat()
    try:
        with _db() as c:
            existing = c.execute(
                "SELECT id FROM research WHERE ticker = ?", [ticker]
            ).fetchone()

            raw_args = [
                today,
                consensus, _f(consensus_mean), num_analysts,
                _f(price_target_avg), _f(price_target_median),
                _f(price_target_high), _f(price_target_low),
                _f(current_price), _f(upside_to_mean_pct), latest_upgrade_date,
                _to_json(recent_upgrades or []),
                _to_json(quarterly_ratings or []),
                _to_json(rec_trend or []),
                json.dumps(finnhub_pt) if finnhub_pt else None,
            ]

            if existing:
                c.execute(
                    """UPDATE research SET
                           raw_fetched_at      = ?,
                           consensus           = ?,
                           consensus_mean      = ?,
                           num_analysts        = ?,
                           price_target_avg    = ?,
                           price_target_median = ?,
                           price_target_high   = ?,
                           price_target_low    = ?,
                           current_price       = ?,
                           upside_to_mean_pct  = ?,
                           latest_upgrade_date = ?,
                           recent_upgrades     = ?,
                           quarterly_ratings   = ?,
                           rec_trend           = ?,
                           finnhub_pt          = ?,
                           as_of_date          = ?,
                           updated_at          = datetime('now')
                       WHERE ticker = ?""",
                    raw_args + [today, ticker],
                )
                action = "raw_updated"
            else:
                c.execute(
                    """INSERT INTO research
                           (ticker, raw_fetched_at, consensus, consensus_mean,
                            num_analysts, price_target_avg, price_target_median,
                            price_target_high, price_target_low, current_price,
                            upside_to_mean_pct, latest_upgrade_date, recent_upgrades,
                            quarterly_ratings, rec_trend, finnhub_pt,
                            as_of_date, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                    [ticker] + raw_args + [today],
                )
                action = "raw_inserted"
            c.commit()
        return {"saved": True, "ticker": ticker, "action": action}
    except Exception as exc:
        return {"saved": False, "ticker": ticker, "error": str(exc)}


def upsert_llm_summary(
    ticker: str,
    highlights: Optional[list] = None,
    research_score: Optional[int] = None,
    summary: str = "",
    model_name: Optional[str] = None,
    model_provider: Optional[str] = None,
) -> dict:
    """
    Track B — store only LLM-generated fields.

    Updates last_llm_run_date to today. Never touches the raw data columns.
    """
    ticker = ticker.upper()
    today  = date.today().isoformat()
    try:
        with _db() as c:
            existing = c.execute(
                "SELECT id FROM research WHERE ticker = ?", [ticker]
            ).fetchone()
            if existing:
                c.execute(
                    """UPDATE research SET
                           last_llm_run_date = ?,
                           highlights        = ?,
                           research_score    = ?,
                           summary           = ?,
                           model_name        = COALESCE(?, model_name),
                           model_provider    = COALESCE(?, model_provider),
                           updated_at        = datetime('now')
                       WHERE ticker = ?""",
                    [today, _to_json(highlights or []), research_score, summary,
                     model_name, model_provider, ticker],
                )
                action = "llm_updated"
            else:
                # Row not yet created by raw fetch — insert a minimal row
                c.execute(
                    """INSERT INTO research
                           (ticker, raw_fetched_at, as_of_date,
                            last_llm_run_date, highlights, research_score, summary,
                            model_name, model_provider, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                    [ticker, today, today, today,
                     _to_json(highlights or []), research_score, summary,
                     model_name, model_provider],
                )
                action = "llm_inserted"
            c.commit()
        return {"saved": True, "ticker": ticker, "action": action}
    except Exception as exc:
        return {"saved": False, "ticker": ticker, "error": str(exc)}


