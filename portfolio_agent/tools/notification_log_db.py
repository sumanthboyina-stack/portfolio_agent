"""
Notification delivery log — private, owner-scoped dedup for outbound
notifications (see events.detector._maybe_notify / _emit).

Table: notification_log — one row per (owner_scope, dedup_key, channel),
UNIQUE. record_if_new() is the only write path: it inserts and returns True
the first time a given key is delivered to a given recipient on a given
channel, and returns False (without inserting again) on every later attempt
— the detector re-running (self-healed missed morning, an intraday re-scan
that rediscovers the same trigger_events row) never re-delivers.

dedup_key is caller-defined and should identify the SPECIFIC thing being
notified about, not just the ticker — e.g. f"event:{trigger_event_id}" or
f"policy_decision:{request_id}" — so two different events for the same
ticker on the same day are each delivered once, not collapsed into one.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from portfolio_agent.tools.db import db_conn


@contextmanager
def _db():
    def _setup(conn: sqlite3.Connection) -> None:
        _create_schema(conn)
        conn.commit()
    with db_conn(setup=_setup) as conn:
        yield conn


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notification_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_scope TEXT NOT NULL,
            dedup_key   TEXT NOT NULL,
            channel     TEXT NOT NULL,
            sent_at     TEXT NOT NULL,
            UNIQUE(owner_scope, dedup_key, channel)
        )
    """)


def record_if_new(owner_scope: str, dedup_key: str, channel: str) -> bool:
    """True if this is the first delivery of (owner_scope, dedup_key, channel)
    — the caller should deliver. False means it was already delivered; the
    caller must not deliver again."""
    with _db() as conn:
        try:
            conn.execute(
                "INSERT INTO notification_log (owner_scope, dedup_key, channel, sent_at) VALUES (?, ?, ?, ?)",
                (owner_scope, dedup_key, channel, datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            conn.rollback()
            return False


def was_delivered(owner_scope: str, dedup_key: str, channel: str) -> bool:
    with _db() as conn:
        row = conn.execute(
            "SELECT 1 FROM notification_log WHERE owner_scope = ? AND dedup_key = ? AND channel = ?",
            (owner_scope, dedup_key, channel),
        ).fetchone()
    return row is not None
