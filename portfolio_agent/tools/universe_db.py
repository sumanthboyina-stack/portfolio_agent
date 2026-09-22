"""
SQLite persistence for the tiered ticker universe (extended / broad).

Tables:
  universe_tickers  — one row per (ticker, tier); S&P members → extended, rest → broad
  universe_meta     — per-index last-fetched timestamps for 90-day refresh logic
  screening_signals — daily math-screen results; promoted candidates flow to intraday triage

Tiers:
  core     — portfolio holdings + manually curated watchlist (see portfolio_agent/tools/watchlist_db.py)
  extended — S&P 1500 (500 + 400 + 600); ~1,500 names; screened daily for signals
  broad    — Nasdaq/NYSE listed tickers outside S&P 1500; ~3,000–5,000 names; lighter screen
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date
from typing import Optional

from portfolio_agent.tools.db import db_conn, migrate_columns


@contextmanager
def _db():
    def _setup(conn: sqlite3.Connection) -> None:
        _create_schema(conn)
        _migrate(conn)
        conn.commit()
    with db_conn(setup=_setup) as conn:
        yield conn


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS universe_tickers (
            ticker              TEXT NOT NULL,
            tier                TEXT NOT NULL CHECK(tier IN ('extended','broad')),
            index_name          TEXT,
            sector              TEXT,
            market_cap_category TEXT,
            fetched_at          TEXT,
            PRIMARY KEY (ticker, tier)
        );

        CREATE INDEX IF NOT EXISTS idx_universe_tier
            ON universe_tickers(tier);

        CREATE TABLE IF NOT EXISTS universe_meta (
            index_name   TEXT PRIMARY KEY,
            fetched_at   TEXT NOT NULL,
            ticker_count INTEGER
        );

        CREATE TABLE IF NOT EXISTS screening_signals (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker       TEXT NOT NULL,
            tier         TEXT NOT NULL,
            signal_date  TEXT NOT NULL,
            signals_json TEXT,
            score        REAL,
            promoted     INTEGER DEFAULT 0,
            created_at   TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_signals_date
            ON screening_signals(signal_date, score DESC);
    """)


def _migrate(conn: sqlite3.Connection) -> None:
    migrate_columns(conn, "universe_tickers", [
        ("market_cap_category", "TEXT"),
    ])
    migrate_columns(conn, "screening_signals", [
        ("fwd_return", "REAL"),
    ])
    _dedupe_and_constrain_signals(conn)


def _dedupe_and_constrain_signals(conn: sqlite3.Connection) -> None:
    """
    One-time cleanup + guard: earlier versions of insert_signals() had no
    uniqueness constraint, so re-screening the same tier twice in one day
    (e.g. morning + intraday) silently accumulated duplicate rows per
    (ticker, tier, signal_date) instead of replacing them. Keep only the
    highest-scoring row per key, then add a unique index so it can't recur —
    insert_signals() upserts against it going forward.
    """
    conn.execute("""
        DELETE FROM screening_signals
        WHERE id NOT IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY ticker, tier, signal_date
                    ORDER BY score DESC, id DESC
                ) AS rn
                FROM screening_signals
            )
            WHERE rn = 1
        )
    """)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_unique "
        "ON screening_signals(ticker, tier, signal_date)"
    )


# ── Upsert helpers ────────────────────────────────────────────────────────────

def upsert_batch(rows: list[dict]) -> int:
    """
    Insert or replace universe ticker rows.
    Each row must have: {ticker, tier}.
    Optional keys: index_name, sector, market_cap_category.
    Returns count of rows written.
    """
    if not rows:
        return 0
    today = date.today().isoformat()
    with _db() as c:
        c.executemany(
            """
            INSERT INTO universe_tickers
                (ticker, tier, index_name, sector, market_cap_category, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, tier) DO UPDATE SET
                index_name          = excluded.index_name,
                sector              = excluded.sector,
                market_cap_category = excluded.market_cap_category,
                fetched_at          = excluded.fetched_at
            """,
            [
                (
                    r["ticker"].upper(),
                    r["tier"],
                    r.get("index_name"),
                    r.get("sector"),
                    r.get("market_cap_category"),
                    today,
                )
                for r in rows
            ],
        )
        c.commit()
    return len(rows)


def mark_refreshed(index_name: str, count: int) -> None:
    with _db() as c:
        c.execute(
            """
            INSERT INTO universe_meta (index_name, fetched_at, ticker_count)
            VALUES (?, ?, ?)
            ON CONFLICT(index_name) DO UPDATE SET
                fetched_at   = excluded.fetched_at,
                ticker_count = excluded.ticker_count
            """,
            (index_name, date.today().isoformat(), count),
        )
        c.commit()


# ── Query helpers ─────────────────────────────────────────────────────────────

def get_tickers(
    tier: Optional[str] = None,
    exclude: Optional[set[str]] = None,
) -> list[str]:
    """
    Return tickers from universe_tickers.
    tier=None → all tiers. exclude → uppercase set to omit (e.g. core pipeline tickers).
    """
    exclude = {t.upper() for t in (exclude or set())}
    with _db() as c:
        if tier:
            rows = c.execute(
                "SELECT ticker FROM universe_tickers WHERE tier = ? ORDER BY ticker",
                (tier,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT ticker FROM universe_tickers ORDER BY tier, ticker"
            ).fetchall()
    return [r["ticker"] for r in rows if r["ticker"] not in exclude]


def get_sectors(tickers: list[str]) -> dict[str, str]:
    """Return {TICKER: sector} for whichever of *tickers* have a cached sector."""
    if not tickers:
        return {}
    upper = [t.upper() for t in tickers]
    ph = ",".join("?" * len(upper))
    with _db() as c:
        rows = c.execute(
            f"SELECT ticker, sector FROM universe_tickers "
            f"WHERE ticker IN ({ph}) AND sector IS NOT NULL",
            upper,
        ).fetchall()
    return {r["ticker"]: r["sector"] for r in rows}


def get_peers(ticker: str, limit: int = 5) -> list[str]:
    """
    Same-sector tickers ranked by market-cap proximity (closest first) — a
    simple peer screen for relative valuation, built from data already cached
    here rather than a new data source. Despite its name, market_cap_category
    stores the raw Nasdaq market-cap number, not a bucket label (see
    universe.py) — that's what proximity is measured against.

    Falls back to [] if the ticker or its sector isn't cached.
    """
    ticker = ticker.upper()
    with _db() as c:
        row = c.execute(
            "SELECT sector, market_cap_category FROM universe_tickers "
            "WHERE ticker = ? AND sector IS NOT NULL LIMIT 1",
            (ticker,),
        ).fetchone()
        if not row:
            return []
        sector, target_cap = row["sector"], row["market_cap_category"]

        candidates = c.execute(
            "SELECT DISTINCT ticker, market_cap_category FROM universe_tickers "
            "WHERE sector = ? AND ticker != ?",
            (sector, ticker),
        ).fetchall()

    if target_cap is None:
        return [r["ticker"] for r in candidates[:limit]]

    with_cap = [r for r in candidates if r["market_cap_category"] is not None]
    ranked = sorted(with_cap, key=lambda r: abs(r["market_cap_category"] - target_cap))
    return [r["ticker"] for r in ranked[:limit]]


def needs_refresh(index_name: str, max_age_days: int = 90) -> bool:
    """Return True if the index has never been fetched or was fetched more than max_age_days ago."""
    with _db() as c:
        row = c.execute(
            "SELECT fetched_at FROM universe_meta WHERE index_name = ?",
            (index_name,),
        ).fetchone()
    if not row:
        return True
    try:
        fetched = date.fromisoformat(row["fetched_at"])
        return (date.today() - fetched).days > max_age_days
    except Exception:
        return True


def has_any_universe_data() -> bool:
    """Return True if at least one universe ticker has been loaded."""
    with _db() as c:
        row = c.execute("SELECT COUNT(*) AS n FROM universe_tickers").fetchone()
    return (row["n"] if row else 0) > 0


# ── Screening signal helpers ───────────────────────────────────────────────────

def insert_signals(rows: list[dict]) -> int:
    """
    Write screening signal rows. Each row: {ticker, tier, score, signals_json}.
    Returns count written.
    """
    if not rows:
        return 0
    today = date.today().isoformat()
    with _db() as c:
        c.executemany(
            """
            INSERT INTO screening_signals
                (ticker, tier, signal_date, signals_json, score, promoted)
            VALUES (?, ?, ?, ?, ?, 0)
            ON CONFLICT(ticker, tier, signal_date) DO UPDATE SET
                signals_json = excluded.signals_json,
                score        = excluded.score,
                created_at   = datetime('now')
            """,
            [
                (
                    r["ticker"].upper(),
                    r["tier"],
                    today,
                    r.get("signals_json", "[]"),
                    float(r.get("score", 0)),
                )
                for r in rows
            ],
        )
        c.commit()
    return len(rows)


def get_latest_signal_date() -> Optional[str]:
    """Return the most recent signal_date in screening_signals, or None if empty."""
    with _db() as c:
        row = c.execute("SELECT MAX(signal_date) AS d FROM screening_signals").fetchone()
    return row["d"] if row and row["d"] else None


def get_signals(signal_date: str, min_score: float = 0) -> list[dict]:
    """Return screening signals for signal_date, ordered by score descending."""
    with _db() as c:
        rows = c.execute(
            """
            SELECT ticker, tier, signals_json, score
            FROM screening_signals
            WHERE signal_date = ? AND score >= ?
            ORDER BY score DESC
            """,
            (signal_date, min_score),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Screener outcome tracking (evening batch) ──────────────────────────────────
#
# A signal's forward return is evaluated once, ~1 trading week after it fired
# (min_age_days), against price at signal_date. Rows older than max_age_days
# with no price data available are left alone rather than retried forever —
# they've already aged out of the 30-day conviction window anyway.

def get_signals_needing_outcome(
    min_age_days: int = 7,
    max_age_days: int = 37,
    limit: int = 300,
) -> list[dict]:
    """
    Return signal rows old enough to score but not yet evaluated (fwd_return IS NULL).

    Oldest-first, capped at *limit* per call — each row costs 2 yfinance lookups,
    so an evening run with a large backlog (e.g. after fwd_return was first added)
    works through it gradually across several runs instead of one slow, rate-limit-prone batch.
    """
    with _db() as c:
        rows = c.execute(
            """
            SELECT id, ticker, signal_date
            FROM screening_signals
            WHERE fwd_return IS NULL
              AND signal_date <= date('now', ?)
              AND signal_date >= date('now', ?)
            ORDER BY signal_date ASC
            LIMIT ?
            """,
            (f"-{min_age_days} days", f"-{max_age_days} days", limit),
        ).fetchall()
    return [dict(r) for r in rows]


def record_signal_outcome(signal_id: int, fwd_return: float) -> None:
    """Write the evaluated forward return for a single screening_signals row."""
    with _db() as c:
        c.execute(
            "UPDATE screening_signals SET fwd_return = ? WHERE id = ?",
            (fwd_return, signal_id),
        )
        c.commit()


# ── Conviction ranking (shared by Opportunities page + morning batch) ─────────
#
# Conviction = today's raw score, adjusted by 30-day history:
#   + persistence: extra days (beyond today) flagged in the trailing 30 days
#   + outcome:     rolling avg forward return of matured historical flags,
#                  so a ticker that keeps firing but never actually moved
#                  doesn't outrank one with a genuine track record.
PERSISTENCE_STEP     = 0.5
PERSISTENCE_MAX_DAYS = 5     # cap: +2.5 max from persistence alone
OUTCOME_SCALE        = 20.0  # +10% avg fwd return -> +2.0 conviction points
OUTCOME_CAP          = 2.0


def rank_by_conviction(
    signals: list[dict],
    conviction_by_ticker: dict[str, dict],
    exclude: Optional[set[str]] = None,
) -> list[dict]:
    """
    Dedupe *signals* to one row per ticker (best score wins), attach conviction
    fields (_days_flagged, _avg_fwd_return, _n_outcomes, _first_flag_date,
    _conviction), and return sorted by conviction descending.

    exclude: uppercase tickers to drop entirely (e.g. already on watchlist/portfolio).
    """
    exclude = {t.upper() for t in (exclude or set())}
    best_by_ticker: dict[str, dict] = {}
    for s in signals:
        if s["ticker"] in exclude:
            continue
        prev = best_by_ticker.get(s["ticker"])
        if prev is None or (s.get("score") or 0) > (prev.get("score") or 0):
            best_by_ticker[s["ticker"]] = s

    for ticker, s in best_by_ticker.items():
        conv = conviction_by_ticker.get(ticker, {})
        days_flagged   = conv.get("days_flagged") or 1
        avg_fwd_return = conv.get("avg_fwd_return")
        n_outcomes     = conv.get("n_outcomes") or 0

        persistence_boost = min(max(days_flagged - 1, 0), PERSISTENCE_MAX_DAYS) * PERSISTENCE_STEP
        outcome_adj = 0.0
        if avg_fwd_return is not None and n_outcomes > 0:
            outcome_adj = max(-OUTCOME_CAP, min(OUTCOME_CAP, avg_fwd_return * OUTCOME_SCALE))

        s["_days_flagged"]    = days_flagged
        s["_avg_fwd_return"]  = avg_fwd_return
        s["_n_outcomes"]      = n_outcomes
        s["_first_flag_date"] = conv.get("first_flag_date")
        s["_conviction"]      = (s.get("score") or 0) + persistence_boost + outcome_adj

    return sorted(best_by_ticker.values(), key=lambda s: s.get("_conviction") or 0, reverse=True)


def get_conviction_data(
    latest_date: str,
    lookback_days: int = 30,
    min_score: float = 2.0,
) -> dict[str, dict]:
    """
    Per ticker, over the trailing `lookback_days` ending at `latest_date`:
      - days_flagged: distinct days score >= min_score fired (persistence)
      - avg_fwd_return: rolling average forward return across matured signals
        in the window (None if none have matured yet)
      - first_flag_date: earliest day in the window this ticker was flagged —
        the anchor for a live "since first flagged" price move on the page
    """
    with _db() as c:
        rows = c.execute(
            """
            SELECT
                ticker,
                COUNT(DISTINCT CASE WHEN score >= ? THEN signal_date END) AS days_flagged,
                AVG(fwd_return) AS avg_fwd_return,
                COUNT(fwd_return) AS n_outcomes,
                MIN(CASE WHEN score >= ? THEN signal_date END) AS first_flag_date
            FROM screening_signals
            WHERE signal_date <= ?
              AND signal_date >= date(?, ?)
            GROUP BY ticker
            """,
            (min_score, min_score, latest_date, latest_date, f"-{lookback_days} days"),
        ).fetchall()
    return {
        r["ticker"]: {
            "days_flagged": r["days_flagged"] or 0,
            "avg_fwd_return": r["avg_fwd_return"],
            "n_outcomes": r["n_outcomes"] or 0,
            "first_flag_date": r["first_flag_date"],
        }
        for r in rows
    }
