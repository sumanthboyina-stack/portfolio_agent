"""
Blackout windows — admin/global trading blackout periods (organizational
policy: earnings blackout, M&A blackout, SEC rule window, etc.) — NOT a
personal preference. Deliberately separate from tools.restricted_list_db's
personal restricted list, and from tools.user_profile_db: disabling a
person's own pre_clearance_required preference must never lift one of these
(see clearance.py).

Table: blackout_windows — many rows, editable (an admin can add/deactivate a
window; this is NOT an append-only audit table like policy_decisions), but
every clearance evaluation records which window ids/versions it saw in its
own evidence_refs so a later audit can reconstruct what was in effect.

Columns:
  scope        'global' or 'ticker:<TICKER>' — which instruments this window
               applies to. Any other string is inert (never matches anything),
               not an error — an admin typo doesn't silently become "global."
  start_date / end_date   ISO date (YYYY-MM-DD), inclusive, evaluated in
               `timezone`. Either may be NULL for an open-ended window. A
               non-NULL value that fails to parse is INVALID data, not "no
               restriction" — see clearance.evaluate_blackout_windows, which
               surfaces that as UNKNOWN, never as "not blocked."
  timezone     IANA zone the dates above are evaluated in — NOT necessarily
               the acting user's own profile timezone (an employer-mandated
               window is defined in the employer's own terms, not the
               individual's).
  source       who/what defined this window (e.g. "employer:legal",
               "manual:admin", "sec-rule:10b5-1").
  reason       free text surfaced in the clearance memo.
  active       0/1 — an inactive window is kept (audit trail) but never
               evaluated as blocking.

Migration: any pre-existing user_profile.blackout_start/blackout_end is
migrated into exactly one 'global' row here, once, idempotently (see
migrate_legacy_profile_blackout) — never broadening what was already
blocked, never run more than once (gated on a dedicated marker row, the
same pattern restricted_list_db/watchlist_db use for their legacy YAML
migrations).
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from portfolio_agent.tools.db import db_conn

_LEGACY_MARKER_SOURCE = "migrated:user_profile.blackout_start_end"


@contextmanager
def _db():
    def _setup(conn: sqlite3.Connection) -> None:
        _create_schema(conn)
        conn.commit()
    with db_conn(setup=_setup) as conn:
        yield conn


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS blackout_windows (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            scope      TEXT NOT NULL,
            start_date TEXT,
            end_date   TEXT,
            timezone   TEXT NOT NULL DEFAULT 'America/Chicago',
            source     TEXT NOT NULL,
            reason     TEXT,
            active     INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_blackout_windows_scope ON blackout_windows(scope, active)")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def add_blackout_window(*, scope: str, start_date: str | None, end_date: str | None,
                        tz: str = "America/Chicago", source: str, reason: str | None = None) -> int:
    if not scope or not scope.strip():
        raise ValueError("add_blackout_window: scope is required")
    if not source or not source.strip():
        raise ValueError("add_blackout_window: source is required")
    with _db() as conn:
        cur = conn.execute(
            """INSERT INTO blackout_windows (scope, start_date, end_date, timezone, source, reason, active, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
            (scope.strip(), start_date, end_date, tz, source.strip(), reason, _now()),
        )
        conn.commit()
        return cur.lastrowid


def deactivate_blackout_window(window_id: int) -> None:
    with _db() as conn:
        conn.execute("UPDATE blackout_windows SET active = 0 WHERE id = ?", (window_id,))
        conn.commit()


def list_active_blackout_windows(*, scope_in: tuple[str, ...] | None = None) -> list[dict]:
    """Active windows, optionally restricted to a set of scope keys (e.g. ('global', 'ticker:AAPL'))."""
    with _db() as conn:
        if scope_in:
            ph = ",".join("?" * len(scope_in))
            rows = conn.execute(
                f"SELECT * FROM blackout_windows WHERE active = 1 AND scope IN ({ph}) ORDER BY id", scope_in
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM blackout_windows WHERE active = 1 ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def migrate_legacy_profile_blackout(blackout_start: str | None, blackout_end: str | None, *,
                                    tz: str = "America/Chicago") -> None:
    """
    One-time, idempotent migration of the old single user_profile.blackout_
    start/blackout_end pair into exactly one 'global' blackout_windows row.
    Never runs twice (gated on a row with source == _LEGACY_MARKER_SOURCE
    already existing) and never removes/narrows anything — if the legacy
    fields were empty, nothing is created, which preserves "no blackout" as
    it was; it never invents a window that wasn't already in effect.
    """
    if not blackout_start and not blackout_end:
        return
    with _db() as conn:
        existing = conn.execute(
            "SELECT id FROM blackout_windows WHERE source = ?", (_LEGACY_MARKER_SOURCE,)
        ).fetchone()
        if existing is not None:
            return
        conn.execute(
            """INSERT INTO blackout_windows (scope, start_date, end_date, timezone, source, reason, active, created_at)
               VALUES ('global', ?, ?, ?, ?, ?, 1, ?)""",
            (blackout_start, blackout_end, tz, _LEGACY_MARKER_SOURCE,
             "migrated from the single legacy profile blackout window", _now()),
        )
        conn.commit()
