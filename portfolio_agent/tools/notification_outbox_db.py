"""
Notification outbox — PRIVATE, owner-scoped. Every notification the application has decided to send lives here first.

A producer (the event detector, the health run, the compliance checks) never talks to a mail or push server: it writes a row
here (services.notification_service.notify) and moves on. A separate delivery worker (services.notification_worker) claims due
rows, sends them OUTSIDE any database transaction, and records the outcome. That keeps notifications from ever slowing down or
breaking a pipeline run, a health run or a page, and makes every send retryable and auditable.

  status   PENDING    waiting to be sent (next_attempt_at says when; NULL = now)
           DEFERRED   held until the person's quiet hours end (next_attempt_at)
           SENDING    claimed by a worker (claimed_at); a worker that dies leaves this behind and the row is reclaimed after a
                      timeout, so nothing is stuck forever
           SENT       delivered to the provider (sent_at, provider_message_id)
           FAILED     gave up (permanent error, or retry_max_attempts reached) — an administrator can requeue it
           SUPPRESSED deliberately not sent (quiet hours with SUPPRESS, no usable contact any more, rate-limited, channel off);
                      reason_code says why, and fold_into_digest = 1 marks rate-limited rows the daily digest picks up

UNIQUE(owner, dedup_key, channel) is the dedup authority: the same thing notified to the same person on the same channel is
enqueued once however many times a detector re-runs. notification_log (tools.notification_log_db) is still written when a row
is SENT, so was_delivered() and every existing record_if_new() caller keep working.

Nothing here stores a raw address: the recipient is resolved from the contact at SEND time (so an opt-out between enqueue and
send is honoured), and last_error is scrubbed of addresses and secrets before it is stored.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from portfolio_agent.tools.db import db_conn

PENDING, DEFERRED, SENDING, SENT, FAILED, SUPPRESSED = "PENDING", "DEFERRED", "SENDING", "SENT", "FAILED", "SUPPRESSED"
STATUSES = (PENDING, DEFERRED, SENDING, SENT, FAILED, SUPPRESSED)
STALE_CLAIM_SECONDS = 600                       # a SENDING row older than this was abandoned by a dead worker
_MAX_ERROR = 300


def iso(dt: datetime) -> str:
    """The one timestamp format used for every comparison here: UTC, seconds, so strings sort chronologically."""
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def now_iso() -> str:
    return iso(datetime.now(timezone.utc))


# ── Connection / schema ───────────────────────────────────────────────────────

_schema_ready: set[str] = set()


def create_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notification_outbox (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            owner                TEXT NOT NULL,
            category             TEXT NOT NULL,
            channel              TEXT NOT NULL,
            contact_id           INTEGER,
            dedup_key            TEXT NOT NULL,
            subject              TEXT NOT NULL,
            body_brief           TEXT NOT NULL,
            body_detailed        TEXT,
            link_url             TEXT,
            severity             INTEGER,
            status               TEXT NOT NULL DEFAULT 'PENDING',
            attempts             INTEGER NOT NULL DEFAULT 0,
            next_attempt_at      TEXT,
            last_error           TEXT,
            provider_message_id  TEXT,
            created_at           TEXT NOT NULL,
            sent_at              TEXT,
            claimed_at           TEXT,
            fold_into_digest     INTEGER NOT NULL DEFAULT 0,
            reason_code          TEXT,
            UNIQUE (owner, dedup_key, channel)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_outbox_due ON notification_outbox(status, next_attempt_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_outbox_owner ON notification_outbox(owner, created_at)")


def ensure_schema_once() -> None:
    from portfolio_agent.tools import db as _db_module
    key = str(_db_module.DB_PATH)
    if key not in _schema_ready:
        with db_conn(setup=lambda c: (create_schema(c), c.commit())):
            pass
        _schema_ready.add(key)


@contextmanager
def _conn(conn: sqlite3.Connection | None):
    if conn is not None:
        create_schema(conn)                      # on the caller's connection: never from a second one (see notification_routing_db)
        yield conn
        return
    ensure_schema_once()
    with db_conn() as c:
        try:
            yield c
        except BaseException:
            c.rollback()
            raise
        else:
            c.commit()


def _scrub_error(text: str | None) -> str | None:
    return None if text is None else text.replace("\r", " ").replace("\n", " ")[:_MAX_ERROR]


# ── Writes ────────────────────────────────────────────────────────────────────

def enqueue(owner: str, category: str, channel: str, *, dedup_key: str, subject: str, body_brief: str,
            body_detailed: str | None = None, link_url: str | None = None, severity: int | None = None,
            contact_id: int | None = None, status: str = PENDING, next_attempt_at: str | None = None,
            reason_code: str | None = None, fold_into_digest: bool = False, created_at: str | None = None,
            conn: sqlite3.Connection | None = None) -> int | None:
    """Insert one row; returns its id, or None when (owner, dedup_key, channel) was already enqueued (a duplicate).
    created_at defaults to now; a caller that decided on an injected clock (tests) passes the same instant."""
    if not owner or not dedup_key:
        raise ValueError("notification_outbox: owner and dedup_key are required")
    with _conn(conn) as c:
        cur = c.execute(
            """INSERT OR IGNORE INTO notification_outbox
                 (owner, category, channel, contact_id, dedup_key, subject, body_brief, body_detailed, link_url, severity,
                  status, next_attempt_at, reason_code, fold_into_digest, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [owner, category, channel, contact_id, dedup_key, subject, body_brief, body_detailed, link_url, severity,
             status, next_attempt_at, reason_code, int(fold_into_digest), created_at or now_iso()])
        return cur.lastrowid if cur.rowcount else None


def claim_due(*, limit: int = 50, now: datetime | None = None) -> list[dict]:
    """
    Atomically claim up to *limit* rows that are due (PENDING / DEFERRED with next_attempt_at reached, or SENDING rows
    abandoned by a dead worker) and mark them SENDING with attempts + 1. One IMMEDIATE transaction, so two workers running
    at once can never claim the same row. Returns the claimed rows.
    """
    ensure_schema_once()
    now = now or datetime.now(timezone.utc)
    stale = iso(now - timedelta(seconds=STALE_CLAIM_SECONDS))
    with db_conn() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            ids = [r["id"] for r in c.execute(
                """SELECT id FROM notification_outbox
                   WHERE (status IN ('PENDING', 'DEFERRED') AND (next_attempt_at IS NULL OR next_attempt_at <= ?))
                      OR (status = 'SENDING' AND claimed_at <= ?)
                   ORDER BY COALESCE(next_attempt_at, created_at), id LIMIT ?""", [iso(now), stale, limit]).fetchall()]
            rows: list[dict] = []
            if ids:
                marks = ",".join("?" * len(ids))
                c.execute(f"UPDATE notification_outbox SET status = 'SENDING', claimed_at = ?, attempts = attempts + 1 WHERE id IN ({marks})",
                          [iso(now), *ids])
                rows = [dict(r) for r in c.execute(f"SELECT * FROM notification_outbox WHERE id IN ({marks}) ORDER BY id", ids)]
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
    return rows


def _update(row_id: int, sets: dict, *, conn: sqlite3.Connection | None = None) -> None:
    sets = dict(sets)
    if "last_error" in sets:
        sets["last_error"] = _scrub_error(sets["last_error"])
    with _conn(conn) as c:
        c.execute(f"UPDATE notification_outbox SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?", [*sets.values(), row_id])


def mark_sent(row_id: int, provider_message_id: str | None, *, conn: sqlite3.Connection | None = None) -> None:
    _update(row_id, {"status": SENT, "sent_at": now_iso(), "provider_message_id": provider_message_id, "last_error": None,
                     "claimed_at": None, "next_attempt_at": None}, conn=conn)


def mark_retry(row_id: int, next_attempt_at: datetime, error: str, *, conn: sqlite3.Connection | None = None) -> None:
    _update(row_id, {"status": PENDING, "next_attempt_at": iso(next_attempt_at), "last_error": error, "claimed_at": None}, conn=conn)


def mark_failed(row_id: int, error: str, *, conn: sqlite3.Connection | None = None) -> None:
    _update(row_id, {"status": FAILED, "last_error": error, "claimed_at": None, "next_attempt_at": None}, conn=conn)


def mark_suppressed(row_id: int, reason_code: str, *, detail: str | None = None, conn: sqlite3.Connection | None = None) -> None:
    _update(row_id, {"status": SUPPRESSED, "reason_code": reason_code, "last_error": detail, "claimed_at": None,
                     "next_attempt_at": None}, conn=conn)


def requeue(row_id: int, *, conn: sqlite3.Connection | None = None) -> bool:
    """Admin retry: put a FAILED row back to PENDING with a fresh attempt count. Returns whether a row was requeued."""
    with _conn(conn) as c:
        return c.execute("UPDATE notification_outbox SET status = 'PENDING', attempts = 0, next_attempt_at = NULL, last_error = NULL, "
                         "claimed_at = NULL WHERE id = ? AND status = 'FAILED'", [row_id]).rowcount == 1


# ── Reads ─────────────────────────────────────────────────────────────────────

def get(row_id: int, *, conn: sqlite3.Connection | None = None) -> dict | None:
    with _conn(conn) as c:
        r = c.execute("SELECT * FROM notification_outbox WHERE id = ?", [row_id]).fetchone()
    return dict(r) if r else None


def count_recent(owner: str, channel: str, since: datetime, *, conn: sqlite3.Connection | None = None) -> int:
    """How many messages were accepted for this person on this channel since *since* (for the hourly rate limit). Suppressed
    and failed rows do not count: they never reached the person."""
    with _conn(conn) as c:
        return int(c.execute(
            "SELECT count(*) FROM notification_outbox WHERE owner = ? AND channel = ? AND created_at >= ? "
            "AND status IN ('PENDING', 'DEFERRED', 'SENDING', 'SENT')", [owner, channel, iso(since)]).fetchone()[0])


def list_recent(owner: str, limit: int = 50, *, conn: sqlite3.Connection | None = None) -> list[dict]:
    """The person's OWN recent notifications, newest first."""
    with _conn(conn) as c:
        rows = c.execute("SELECT * FROM notification_outbox WHERE owner = ? ORDER BY id DESC LIMIT ?", [owner, limit]).fetchall()
    return [dict(r) for r in rows]


def digest_candidates(owner: str, since: datetime, *, conn: sqlite3.Connection | None = None) -> list[dict]:
    """Rate-limited rows waiting to be rolled into the person's next digest."""
    with _conn(conn) as c:
        rows = c.execute("SELECT * FROM notification_outbox WHERE owner = ? AND fold_into_digest = 1 AND created_at >= ? ORDER BY id",
                         [owner, iso(since)]).fetchall()
    return [dict(r) for r in rows]


def list_by_status(status: str, limit: int = 20, *, conn: sqlite3.Connection | None = None) -> list[dict]:
    """System-wide, newest first — for the administrator's outbox view only (no body text is selected)."""
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    with _conn(conn) as c:
        rows = c.execute("SELECT id, owner, category, channel, contact_id, status, attempts, last_error, reason_code, created_at, sent_at "
                         "FROM notification_outbox WHERE status = ? ORDER BY id DESC LIMIT ?", [status, limit]).fetchall()
    return [dict(r) for r in rows]


def exists(owner: str, dedup_key: str, *, conn: sqlite3.Connection | None = None) -> bool:
    """Has anything been enqueued for this person under this dedup key, on any channel?"""
    with _conn(conn) as c:
        return c.execute("SELECT 1 FROM notification_outbox WHERE owner = ? AND dedup_key = ? LIMIT 1", [owner, dedup_key]).fetchone() is not None


def mark_digested(ids: list[int], *, conn: sqlite3.Connection | None = None) -> int:
    """Rate-limited rows that a digest has now carried: they stop being digest candidates (and keep their reason for the record)."""
    if not ids:
        return 0
    with _conn(conn) as c:
        return c.execute(f"UPDATE notification_outbox SET fold_into_digest = 0, reason_code = 'RATE_LIMITED_DIGESTED' "
                         f"WHERE id IN ({','.join('?' * len(ids))}) AND fold_into_digest = 1", ids).rowcount


def counts_by_status(*, conn: sqlite3.Connection | None = None) -> dict[str, int]:
    """System-wide counts for the administrator (no content, no owners)."""
    with _conn(conn) as c:
        got = dict(c.execute("SELECT status, count(*) FROM notification_outbox GROUP BY status").fetchall())
    return {s: int(got.get(s, 0)) for s in STATUSES}
