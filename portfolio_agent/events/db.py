"""
trigger_events table — persistent log of detected market events that may drive predictions.

Schema:
  id            — autoincrement PK
  detected_at   — ISO datetime (CST) when the event was detected
  ticker        — affected ticker, or 'MARKET' for macro events
  event_type    — earnings | sec_8k | rating_change | material_news | macro_event
  severity      — 1 (low) | 2 (medium) | 3 (high)
  source        — originating data source (news_filter_log, sec_edgar, finnhub, etc.)
  summary       — one-sentence description
  processed     — 0=pending, 1=triggered a prediction, 2=checked but no prediction
  processed_at  — ISO datetime when processed
  prediction_id — FK to predictions.id (NULL if no prediction generated)
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from portfolio_agent.tools.db import db_conn, migrate_columns

_CST = ZoneInfo("America/Chicago")


def _now_cst() -> str:
    return datetime.now(_CST).isoformat()


def _today_cst() -> str:
    return datetime.now(_CST).date().isoformat()


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trigger_events (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_at   TEXT    NOT NULL,
            ticker        TEXT    NOT NULL,
            event_type    TEXT    NOT NULL,
            severity      INTEGER NOT NULL DEFAULT 1,
            source        TEXT,
            summary       TEXT,
            processed     INTEGER NOT NULL DEFAULT 0,
            processed_at  TEXT,
            prediction_id INTEGER
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_trigger_events_ticker_date
        ON trigger_events (ticker, detected_at DESC)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_trigger_events_processed
        ON trigger_events (processed, detected_at DESC)
    """)


def _migrate(conn: sqlite3.Connection) -> None:
    migrate_columns(conn, "trigger_events", [
        ("prediction_id", "INTEGER"),
    ])


@contextmanager
def _db():
    def _setup(conn: sqlite3.Connection) -> None:
        _create_schema(conn)
        _migrate(conn)
        conn.commit()
    with db_conn(setup=_setup) as conn:
        yield conn


# ── Write API ─────────────────────────────────────────────────────────────────

def insert_event(
    ticker: str,
    event_type: str,
    severity: int,
    source: str = "",
    summary: str = "",
) -> int:
    """
    Insert a detected event row and return its id.
    Duplicate events (same ticker + event_type + date) are silently skipped;
    returns the existing id in that case.
    """
    ticker = ticker.upper()
    today = _today_cst()
    with _db() as c:
        existing = c.execute(
            """SELECT id FROM trigger_events
               WHERE ticker = ? AND event_type = ?
               AND date(detected_at) = ?""",
            [ticker, event_type, today],
        ).fetchone()
        if existing:
            return existing["id"]
        c.execute(
            """INSERT INTO trigger_events
                   (detected_at, ticker, event_type, severity, source, summary)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [_now_cst(), ticker, event_type, severity, source, summary],
        )
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]


def get_pending_events(today: str, min_severity: int = 1) -> list[dict]:
    """Return all unprocessed events detected today with severity >= min_severity."""
    with _db() as c:
        rows = c.execute(
            """SELECT * FROM trigger_events
               WHERE date(detected_at) = ? AND processed = 0
               AND severity >= ?
               ORDER BY severity DESC, id ASC""",
            [today, min_severity],
        ).fetchall()
    return [dict(r) for r in rows]


def get_events_for_ticker(ticker: str, today: str, min_severity: int = 1) -> list[dict]:
    """Return all events for a specific ticker today with severity >= min_severity."""
    with _db() as c:
        rows = c.execute(
            """SELECT * FROM trigger_events
               WHERE ticker = ? AND date(detected_at) = ?
               AND severity >= ?
               ORDER BY severity DESC, id ASC""",
            [ticker.upper(), today, min_severity],
        ).fetchall()
    return [dict(r) for r in rows]


def mark_event_processed(event_id: int, prediction_id: Optional[int] = None) -> None:
    """Mark an event as processed (triggered prediction or checked but skipped)."""
    with _db() as c:
        c.execute(
            """UPDATE trigger_events
               SET processed = 1, processed_at = ?, prediction_id = ?
               WHERE id = ?""",
            [_now_cst(), prediction_id, event_id],
        )
        c.commit()


def mark_event_checked(event_id: int) -> None:
    """Mark an event as checked but no prediction generated (severity below threshold)."""
    with _db() as c:
        c.execute(
            "UPDATE trigger_events SET processed = 2, processed_at = ? WHERE id = ?",
            [_now_cst(), event_id],
        )
        c.commit()
