"""
Access requests — the "request access" form on the pre-login landing page
(web/auth.py) lets an uninvited visitor ask to be added to the auth
allow-list. This never grants access by itself: submit_request() only ever
writes a pending row here; approve_request() is the one path that turns a
request into an auth_users invite, and it's admin-only (called from the
Profile page's admin section).

Table (access_requests):
  id, name, email, message, status ('pending' | 'approved' | 'denied'),
  created_at, reviewed_at.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from portfolio_agent.domain import AccessRequest, AuthUser
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
        CREATE TABLE IF NOT EXISTS access_requests (
            id            INTEGER PRIMARY KEY,
            name          TEXT NOT NULL,
            email         TEXT NOT NULL,
            message       TEXT NOT NULL DEFAULT '',
            status        TEXT NOT NULL DEFAULT 'pending',
            created_at    TEXT NOT NULL,
            reviewed_at   TEXT
        )
    """)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def submit_request(name: str, email: str, message: str = "") -> AccessRequest:
    """Record a pending access request. Does not check the auth_users
    allow-list or grant anything — a human reviews it via approve_request()."""
    name = name.strip()
    email = email.strip()
    if not name or not email:
        raise ValueError("name and email are required")
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO access_requests (name, email, message, status, created_at) "
            "VALUES (?, ?, ?, 'pending', ?)",
            (name, email, message.strip(), _now()),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM access_requests WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return AccessRequest.from_db_row(dict(row))


def list_requests(status: str | None = "pending") -> list[AccessRequest]:
    """List requests, newest first. status=None returns every request."""
    with _db() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM access_requests WHERE status = ? ORDER BY created_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM access_requests ORDER BY created_at DESC"
            ).fetchall()
    return [AccessRequest.from_db_row(dict(r)) for r in rows]


def approve_request(request_id: int, owner: str, role: str = "member") -> AuthUser:
    """Approve a pending request: invite its email into auth_users (owner must
    be an existing internal owner id, e.g. LOCAL_OWNER) and mark it approved.
    Raises ValueError if the request doesn't exist or isn't pending."""
    from portfolio_agent.tools.auth_users_db import invite_user

    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM access_requests WHERE id = ?", (request_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"no access request with id {request_id}")
        if row["status"] != "pending":
            raise ValueError(f"request {request_id} is already {row['status']!r}")
        email = row["email"]

    auth_user = invite_user(email, owner=owner, role=role)

    with _db() as conn:
        conn.execute(
            "UPDATE access_requests SET status = 'approved', reviewed_at = ? WHERE id = ?",
            (_now(), request_id),
        )
        conn.commit()
    return auth_user


def deny_request(request_id: int) -> bool:
    """Mark a pending request denied. Returns whether a pending row matched."""
    with _db() as conn:
        n = conn.execute(
            "UPDATE access_requests SET status = 'denied', reviewed_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (_now(), request_id),
        ).rowcount
        conn.commit()
    return n > 0
