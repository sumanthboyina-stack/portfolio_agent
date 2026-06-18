"""News database tools — read/write the news_daily_update table (SQLite).

Row types
---------
'ticker'      : one row per ticker per day — headline pair, sentiment, trending.
                Unique on (date, ticker).
'market_item' : one row per individual market news item.
                Unique on (date, headline_1).

Article-hash table
------------------
'news_article_hashes' : one row per (ticker, title_hash).
                        Used for Stage 4 change detection — skip full analysis
                        when all articles for a ticker were already processed.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from portfolio_agent.log import get_logger as _get_logger
from portfolio_agent.tools.db import db_conn

_log = _get_logger("news")
_CST = ZoneInfo("America/Chicago")


def _today_cst() -> str:
    return datetime.now(_CST).date().isoformat()


def _parse_themes(raw: str | None) -> list:
    """Safely parse top_themes — handles JSON lists, plain strings, and None."""
    if not raw:
        return []
    try:
        result = json.loads(raw)
        return result if isinstance(result, list) else [str(result)]
    except (json.JSONDecodeError, ValueError):
        # Plain comma-separated string stored by the agent
        return [t.strip() for t in raw.split(",") if t.strip()]


def _normalise_themes(top_themes) -> str:
    """Normalise top_themes to a JSON string before writing to DB."""
    if isinstance(top_themes, list):
        return json.dumps(top_themes)
    if not top_themes:
        return "[]"
    # Try to parse an existing JSON string
    try:
        parsed = json.loads(str(top_themes))
        return json.dumps(parsed if isinstance(parsed, list) else [str(parsed)])
    except (json.JSONDecodeError, ValueError):
        # Plain comma-separated — split into a proper list
        items = [t.strip() for t in str(top_themes).split(",") if t.strip()]
        return json.dumps(items)


# ── schema helpers ─────────────────────────────────────────────────────────────

def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS news_daily_update (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            as_of_date       TEXT    NOT NULL,
            ticker           TEXT    NOT NULL,
            row_type         TEXT    NOT NULL DEFAULT 'ticker',
            headline_1       TEXT,
            headline_2       TEXT,
            sentiment        TEXT,
            sentiment_score  REAL,
            top_themes       TEXT,
            trending         TEXT,
            impacted_tickers TEXT,
            source           TEXT,
            updated_at       TEXT    DEFAULT (datetime('now'))
        )
    """)
    # One ticker-summary per ticker per day
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uidx_ticker_daily
        ON news_daily_update(as_of_date, ticker)
        WHERE row_type = 'ticker'
    """)
    # One market item per headline per day (deduplicates cross-source)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uidx_market_headline
        ON news_daily_update(as_of_date, headline_1)
        WHERE row_type = 'market_item'
    """)
    # ── Filter audit log: one row per ticker per day across all 77 tickers ──────
    # stage_2_decision : 'interesting' | 'routine'
    # stage_3_decision : 'pass' | 'fail' | 'bypassed_portfolio' | 'skipped_stage2' | 'scorer_failed'
    # final_decision   : 'analyzed' | 'dropped'
    # followup_5d_return: filled later by validation engine
    conn.execute("""
        CREATE TABLE IF NOT EXISTS news_filter_log (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            as_of_date               TEXT    NOT NULL,
            ticker                   TEXT    NOT NULL,
            stage_2_decision         TEXT    NOT NULL,
            stage_2_reason           TEXT,
            stage_2_matched_keywords TEXT,
            stage_3_decision         TEXT,
            stage_3_score            INTEGER,
            stage_3_summary          TEXT,
            final_decision           TEXT    NOT NULL,
            followup_5d_return       REAL,
            created_at               TEXT    DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uidx_filter_log_daily
        ON news_filter_log(as_of_date, ticker)
    """)
    # ── Article-hash table — Stage 4 change detection ─────────────────────────
    # PRIMARY KEY (ticker, title_hash) prevents duplicates.
    # seen_at lets old hashes age out (load_seen_hashes queries last 7 days only).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS news_article_hashes (
            ticker       TEXT NOT NULL,
            title_hash   TEXT NOT NULL,
            url          TEXT DEFAULT '',
            publisher    TEXT DEFAULT '',
            published_at TEXT DEFAULT '',
            seen_at      TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (ticker, title_hash)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_article_hashes_seen_at
        ON news_article_hashes(seen_at)
    """)


def _add_model_cols(conn: sqlite3.Connection) -> None:
    from portfolio_agent.tools.db import migrate_columns
    migrate_columns(conn, "news_daily_update", [
        ("model_name",     "TEXT"),
        ("model_provider", "TEXT"),
    ])
    # Rename date → as_of_date (idempotent — only runs if old column still exists)
    for tbl in ("news_daily_update", "news_filter_log"):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({tbl})")}
        if "date" in cols and "as_of_date" not in cols:
            conn.execute(f"ALTER TABLE {tbl} RENAME COLUMN date TO as_of_date")


@contextmanager
def _db():
    def _setup(conn: sqlite3.Connection) -> None:
        _create_schema(conn)
        _add_model_cols(conn)
        conn.commit()
    with db_conn(setup=_setup) as conn:
        yield conn


# ── read helpers ───────────────────────────────────────────────────────────────

def get_todays_ticker_news(ticker: str) -> str:
    """
    Return today's ticker-summary row for *ticker*, or null if none exists.

    Use this before saving to avoid re-running analysis that already completed today.

    Args:
        ticker: Stock ticker symbol or "MARKET".

    Returns:
        JSON: {found: bool, record: {...} or null}
    """
    today = _today_cst()
    with _db() as c:
        row = c.execute(
            """SELECT id, as_of_date, ticker, headline_1, headline_2, sentiment,
                      sentiment_score, top_themes, trending,
                      impacted_tickers, source, updated_at
               FROM news_daily_update
               WHERE as_of_date = ? AND ticker = ? AND row_type = 'ticker'""",
            [today, ticker.upper()],
        ).fetchone()
    if row:
        return json.dumps({
            "found": True,
            "record": {
                "id": row["id"],
                "as_of_date": row["as_of_date"],
                "ticker": row["ticker"],
                "headline_1": row["headline_1"],
                "headline_2": row["headline_2"],
                "sentiment": row["sentiment"],
                "sentiment_score": row["sentiment_score"],
                "top_themes": json.loads(row["top_themes"]) if row["top_themes"] else [],
                "trending": row["trending"],
                "impacted_tickers": row["impacted_tickers"],
                "source": row["source"],
                "updated_at": row["updated_at"],
            },
        })
    return json.dumps({"found": False, "record": None})


def get_todays_market_news() -> str:
    """
    Return all market-item rows already stored today.

    Use this to check what has already been saved before adding more items.

    Returns:
        JSON: {date, count, items: [{id, headline, source, sentiment,
               sentiment_score, impacted_tickers, top_themes}]}
    """
    today = _today_cst()
    with _db() as c:
        rows = c.execute(
            """SELECT id, headline_1, source, sentiment, sentiment_score,
                      impacted_tickers, top_themes, updated_at
               FROM news_daily_update
               WHERE as_of_date = ? AND row_type = 'market_item'
               ORDER BY id ASC""",
            [today],
        ).fetchall()
    items = [
        {
            "id": r["id"],
            "headline": r["headline_1"],
            "source": r["source"],
            "sentiment": r["sentiment"],
            "sentiment_score": r["sentiment_score"],
            "impacted_tickers": r["impacted_tickers"],
            "top_themes": _parse_themes(r["top_themes"]),
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]
    return json.dumps({"as_of_date": today, "count": len(items), "items": items})


def get_historical_news(ticker: str, days: int = 7) -> str:
    """
    Return the last *days* days of ticker-summary rows for *ticker*.

    Args:
        ticker: Ticker symbol or "MARKET".
        days:   Calendar days to look back (default 7).

    Returns:
        JSON: {ticker, days, count, records:[{date, headline_1, headline_2,
        sentiment, sentiment_score, top_themes, trending, impacted_tickers, source}]}
    """
    cutoff = (datetime.now(_CST).date() - timedelta(days=days)).isoformat()
    with _db() as c:
        rows = c.execute(
            """SELECT as_of_date, headline_1, headline_2, sentiment, sentiment_score,
                      top_themes, trending, impacted_tickers, source
               FROM news_daily_update
               WHERE ticker = ? AND as_of_date >= ? AND row_type = 'ticker'
               ORDER BY as_of_date DESC""",
            [ticker.upper(), cutoff],
        ).fetchall()
    records = [
        {
            "as_of_date": r["as_of_date"],
            "headline_1": r["headline_1"],
            "headline_2": r["headline_2"],
            "sentiment": r["sentiment"],
            "sentiment_score": r["sentiment_score"],
            "top_themes": _parse_themes(r["top_themes"]),
            "trending": r["trending"],
            "impacted_tickers": r["impacted_tickers"],
            "source": r["source"],
        }
        for r in rows
    ]
    return json.dumps({"ticker": ticker.upper(), "days": days,
                       "count": len(records), "records": records})


def get_all_recent_news(days: int = 7) -> str:
    """
    Return the latest ticker-summary row per tracked ticker for the last *days* days.

    Sorted worst-sentiment-first so the most at-risk names stand out.

    Returns:
        JSON: {days, tickers_tracked, records:[...]}
    """
    cutoff = (datetime.now(_CST).date() - timedelta(days=days)).isoformat()
    with _db() as c:
        rows = c.execute(
            """SELECT n.ticker, n.as_of_date AS latest_date, n.sentiment,
                      n.sentiment_score, n.headline_1, n.trending, n.source
               FROM news_daily_update n
               INNER JOIN (
                   SELECT ticker, MAX(as_of_date) AS max_date
                   FROM news_daily_update
                   WHERE as_of_date >= ? AND row_type = 'ticker'
                   GROUP BY ticker
               ) latest ON n.ticker = latest.ticker AND n.as_of_date = latest.max_date
               WHERE n.row_type = 'ticker'
               ORDER BY n.sentiment_score ASC""",
            [cutoff],
        ).fetchall()
    records = [
        {
            "ticker": r["ticker"],
            "latest_date": r["latest_date"],
            "sentiment": r["sentiment"],
            "sentiment_score": r["sentiment_score"],
            "headline_1": r["headline_1"],
            "trending": r["trending"],
            "source": r["source"],
        }
        for r in rows
    ]
    return json.dumps({"days": days, "tickers_tracked": len(records), "records": records})


# ── write helpers ──────────────────────────────────────────────────────────────

def save_ticker_news(
    ticker: str,
    headline_1: str,
    headline_2: str,
    sentiment: str,
    sentiment_score: float,
    top_themes: str,
    trending: str = "",
    impacted_tickers: str = "",
    source: str = "",
) -> str:
    """
    Upsert today's ticker-summary row for *ticker*.

    One row per ticker per day. Safe to call multiple times — updates the existing
    row if one already exists today.

    Args:
        ticker:           Ticker symbol or "MARKET".
        headline_1:       Most important headline today (one sentence).
        headline_2:       Second most important headline.
        sentiment:        POSITIVE | NEUTRAL | NEGATIVE.
        sentiment_score:  Float in [-1.0, 1.0].
        top_themes:       Python list or JSON string of up to 3 theme strings.
        trending:         2-3 sentence trending narrative vs prior 7 days.
        impacted_tickers: Comma-separated tickers mentioned/impacted (e.g. "AAPL,MSFT").
        source:           News source name (e.g. "yahoo_finance", "reuters_business").

    Returns:
        JSON: {saved, date, ticker, action: "inserted"|"updated"}
    """
    today = _today_cst()
    ticker = ticker.upper()
    themes_str = _normalise_themes(top_themes)
    try:
        with _db() as c:
            existing = c.execute(
                """SELECT id FROM news_daily_update
                   WHERE as_of_date = ? AND ticker = ? AND row_type = 'ticker'""",
                [today, ticker],
            ).fetchone()
            if existing:
                c.execute(
                    """UPDATE news_daily_update SET
                           headline_1       = ?,
                           headline_2       = ?,
                           sentiment        = ?,
                           sentiment_score  = ?,
                           top_themes       = ?,
                           trending         = ?,
                           impacted_tickers = ?,
                           source           = ?,
                           updated_at       = datetime('now')
                       WHERE id = ?""",
                    [headline_1, headline_2, sentiment, float(sentiment_score),
                     themes_str, trending, impacted_tickers, source, existing["id"]],
                )
                action = "updated"
            else:
                c.execute(
                    """INSERT INTO news_daily_update
                           (as_of_date, ticker, row_type, headline_1, headline_2, sentiment,
                            sentiment_score, top_themes, trending,
                            impacted_tickers, source, updated_at)
                       VALUES (?, ?, 'ticker', ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                    [today, ticker, headline_1, headline_2, sentiment,
                     float(sentiment_score), themes_str, trending,
                     impacted_tickers, source],
                )
                action = "inserted"
            c.commit()
        return json.dumps({"saved": True, "as_of_date": today, "ticker": ticker, "action": action})
    except Exception as exc:
        return json.dumps({"saved": False, "error": str(exc)})


def save_market_news_item(
    headline: str,
    source: str,
    sentiment: str = "NEUTRAL",
    sentiment_score: float = 0.0,
    impacted_tickers: str = "",
    top_themes: str = "",
) -> str:
    """
    Insert one market-news item if it has not been stored today.

    Deduplicates by (date, headline) — same headline from a different source is
    treated as a duplicate and skipped. Call once per unique news item.

    Args:
        headline:         The news headline (one sentence, as-is from the source).
        source:           Feed name (e.g. "reuters_business", "cnbc", "yahoo_finance").
        sentiment:        POSITIVE | NEUTRAL | NEGATIVE.
        sentiment_score:  Float in [-1.0, 1.0].
        impacted_tickers: Comma-separated tickers mentioned in this item (e.g. "NVDA,AMD").
        top_themes:       Python list or JSON/comma string of themes for this item.

    Returns:
        JSON: {saved, date, headline, source, reason: "inserted"|"duplicate"|"error"}
    """
    today = _today_cst()
    themes_str = _normalise_themes(top_themes)
    try:
        with _db() as c:
            exists = c.execute(
                """SELECT 1 FROM news_daily_update
                   WHERE as_of_date = ? AND headline_1 = ? AND row_type = 'market_item'""",
                [today, headline],
            ).fetchone()
            if exists:
                return json.dumps({
                    "saved": False,
                    "as_of_date": today,
                    "headline": headline,
                    "source": source,
                    "reason": "duplicate",
                })
            c.execute(
                """INSERT INTO news_daily_update
                       (as_of_date, ticker, row_type, headline_1, sentiment, sentiment_score,
                        top_themes, impacted_tickers, source, updated_at)
                   VALUES (?, 'MARKET', 'market_item', ?, ?, ?, ?, ?, ?, datetime('now'))""",
                [today, headline, sentiment, float(sentiment_score),
                 themes_str, impacted_tickers, source],
            )
            c.commit()
        return json.dumps({
            "saved": True, "as_of_date": today,
            "headline": headline, "source": source, "reason": "inserted",
        })
    except Exception as exc:
        return json.dumps({"saved": False, "error": str(exc)})


def update_ticker_news_model(
    ticker: str,
    model_name: str,
    model_provider: str,
    news_date: str | None = None,
) -> None:
    """
    Backfill model_name / model_provider on today's ticker-summary row.

    Called after run_analysis_with_failover() returns so we know which model succeeded.
    news_date defaults to today. Silently no-ops if the row doesn't exist yet.
    """
    target_date = news_date or _today_cst()
    try:
        with _db() as c:
            c.execute(
                """UPDATE news_daily_update
                   SET model_name = ?, model_provider = ?
                   WHERE as_of_date = ? AND ticker = ? AND row_type = 'ticker'""",
                [model_name, model_provider, target_date, ticker.upper()],
            )
            c.commit()
    except Exception:
        pass


def _title_hash(title: str) -> str:
    """MD5 of the lowercased, stripped title — stable across minor formatting diffs."""
    return hashlib.md5(title.strip().lower().encode()).hexdigest()


def load_seen_hashes(tickers: list[str], lookback_days: int = 7) -> dict[str, set[str]]:
    """
    Return {ticker: {title_hash, ...}} for all tickers, looking back *lookback_days* days.

    One DB query for all tickers — call once per news phase, not per ticker.
    """
    result: dict[str, set[str]] = {t.upper(): set() for t in tickers}
    if not tickers:
        return result
    cutoff = (datetime.now(_CST).date() - timedelta(days=lookback_days)).isoformat()
    placeholders = ",".join("?" * len(tickers))
    try:
        with _db() as c:
            rows = c.execute(
                f"SELECT ticker, title_hash FROM news_article_hashes "
                f"WHERE ticker IN ({placeholders}) AND seen_at >= ?",
                [t.upper() for t in tickers] + [cutoff],
            ).fetchall()
        for row in rows:
            result[row["ticker"]].add(row["title_hash"])
    except Exception:
        pass
    return result


def save_article_hashes(ticker: str, articles: list[dict]) -> None:
    """
    Store title hashes for a processed ticker's articles.

    Called after Stage 4 saves a ticker so future runs can detect unchanged feeds.
    Silently ignores conflicts (INSERT OR IGNORE) and DB errors.
    """
    ticker = ticker.upper()
    now = datetime.utcnow().isoformat()
    rows = [
        (
            ticker,
            _title_hash(a.get("title") or ""),
            a.get("url") or "",
            a.get("publisher") or a.get("source") or a.get("_source") or "",
            a.get("published_at") or a.get("published") or "",
            now,
        )
        for a in articles
        if (a.get("title") or "").strip()
    ]
    if not rows:
        return
    try:
        with _db() as c:
            c.executemany(
                """INSERT OR IGNORE INTO news_article_hashes
                       (ticker, title_hash, url, publisher, published_at, seen_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                rows,
            )
            c.commit()
    except Exception:
        pass


def log_filter_decisions(decisions: list[dict]) -> None:
    """
    Upsert one filter-log row per ticker for today.

    Each dict in *decisions* should contain:
        ticker                   — stock symbol
        stage_2_decision         — 'interesting' | 'routine'
        stage_2_reason           — keyword string or 'volume_spike:...' or 'routine'
        stage_2_matched_keywords — dict {high:[...], medium:[...], low:[...]} or None
        stage_3_decision         — 'pass' | 'fail' | 'bypassed_portfolio' |
                                   'skipped_stage2' | 'scorer_failed'
        stage_3_score            — int 0-3 or None
        stage_3_summary          — one-sentence scorer summary or None
        final_decision           — 'analyzed' | 'dropped'

    followup_5d_return is left NULL here and filled later by the validation engine.
    Silently skips on any DB error so it never blocks the main batch loop.
    """
    today = _today_cst()
    try:
        with _db() as c:
            for d in decisions:
                mk = d.get("stage_2_matched_keywords")
                mk_json = json.dumps(mk) if mk else None
                c.execute(
                    """INSERT INTO news_filter_log
                           (as_of_date, ticker, stage_2_decision, stage_2_reason,
                            stage_2_matched_keywords, stage_3_decision,
                            stage_3_score, stage_3_summary, final_decision)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(as_of_date, ticker) DO UPDATE SET
                           stage_2_decision         = excluded.stage_2_decision,
                           stage_2_reason           = excluded.stage_2_reason,
                           stage_2_matched_keywords = excluded.stage_2_matched_keywords,
                           stage_3_decision         = excluded.stage_3_decision,
                           stage_3_score            = excluded.stage_3_score,
                           stage_3_summary          = excluded.stage_3_summary,
                           final_decision           = excluded.final_decision""",
                    [
                        today,
                        d["ticker"].upper(),
                        d["stage_2_decision"],
                        d.get("stage_2_reason"),
                        mk_json,
                        d.get("stage_3_decision"),
                        d.get("stage_3_score"),
                        d.get("stage_3_summary"),
                        d["final_decision"],
                    ],
                )
            c.commit()
    except Exception as exc:
        _log.warning(f"  [warn] filter_log write failed: {exc}",
                     event_type="warning", error_type=type(exc).__name__)
