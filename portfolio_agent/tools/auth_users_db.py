"""
Auth allow-list — invited users who may sign in via Entra ID (see web/auth.py).

Table (auth_users):
  id, email (invite key, case-insensitive unique), issuer/subject (the OIDC
  identity, bound on first login), owner (internal owner id this login
  resolves to), role ('admin' | 'member'), enabled, created_at, last_login_at.

Identity binding
----------------
email is only the invite key and, after first login, a display label —
resolve_login() binds (issuer, subject) to the invited row the first time it
succeeds, and from then on the row is looked up by (issuer, subject) only. A
later change or reuse of that email address never re-authenticates a
different account, and a second registration of the same address can't take
over an already-bound row (see resolve_login).

owner must be an existing internal owner id (typically portfolio_agent.
domain.LOCAL_OWNER) — identity_from_verified_login() turns it directly into
ctx.actor, so it must match the owner value already stamped on every
pre-existing scoped row (holdings, watchlist, accounts, ...). It is not
validated against a registry here (there isn't one yet); pass it deliberately.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from portfolio_agent.domain import AuthUser
from portfolio_agent.tools.db import db_conn

logger = logging.getLogger(__name__)


@contextmanager
def _db():
    def _setup(conn: sqlite3.Connection) -> None:
        _create_schema(conn)
        conn.commit()
    with db_conn(setup=_setup) as conn:
        yield conn


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS auth_users (
            id            INTEGER PRIMARY KEY,
            email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
            issuer        TEXT,
            subject       TEXT,
            owner         TEXT NOT NULL,
            role          TEXT NOT NULL DEFAULT 'member',
            enabled       INTEGER NOT NULL DEFAULT 1,
            created_at    TEXT NOT NULL,
            last_login_at TEXT,
            UNIQUE(issuer, subject)
        )
    """)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_email(email: str) -> str:
    """Short, non-reversible tag for logs — never log the email or subject raw."""
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()[:12]


def invite_user(email: str, owner: str, role: str = "member") -> AuthUser:
    """
    Create an invite row for email, or re-enable/update an existing one.

    Raises ValueError for an unknown role. Re-inviting an existing row updates
    owner/role and re-enables it without touching issuer/subject/last_login_at
    — a previously bound identity stays bound.
    """
    if role not in ("admin", "member"):
        raise ValueError(f"role must be 'admin' or 'member', got {role!r}")
    email = email.strip()
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO auth_users (email, owner, role, enabled, created_at)
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(email) DO UPDATE SET
                owner = excluded.owner,
                role = excluded.role,
                enabled = 1
            """,
            (email, owner, role, _now()),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM auth_users WHERE email = ? COLLATE NOCASE", (email,)
        ).fetchone()
    return AuthUser.from_db_row(dict(row))


def disable_user(email: str) -> bool:
    """Disable the invite/account for email. Returns whether a row matched."""
    with _db() as conn:
        n = conn.execute(
            "UPDATE auth_users SET enabled = 0 WHERE email = ? COLLATE NOCASE",
            (email.strip(),),
        ).rowcount
        conn.commit()
    return n > 0


def list_users() -> list[AuthUser]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM auth_users ORDER BY email COLLATE NOCASE"
        ).fetchall()
    return [AuthUser.from_db_row(dict(r)) for r in rows]


def resolve_login(issuer: str, subject: str, email: str | None) -> AuthUser | None:
    """
    The only path that turns a successful OIDC login into an app identity.
    Called by web/auth.py after Streamlit's own token/signature verification
    already succeeded — this only checks the allow-list.

    - (issuer, subject) already bound and enabled: return it, bump last_login_at.
    - (issuer, subject) bound but disabled: return None.
    - Not yet bound, but an enabled row for email has NULL issuer/subject:
      bind this (issuer, subject) to it (first login), then return it.
    - Row for email exists but is already bound to a different (issuer,
      subject): return None and log a warning — blocks a later registration
      of the same address from taking over an existing account.
    - No row matches email, or it's disabled: return None.

    Never reveals to the caller *why* a login was denied (unknown email vs.
    disabled vs. already-bound-elsewhere) — all three are just None.
    """
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM auth_users WHERE issuer = ? AND subject = ?",
            (issuer, subject),
        ).fetchone()
        if row is not None:
            if not row["enabled"]:
                return None
            now = _now()
            conn.execute("UPDATE auth_users SET last_login_at = ? WHERE id = ?", (now, row["id"]))
            conn.commit()
            return AuthUser.from_db_row({**dict(row), "last_login_at": now})

        if not email:
            return None
        row = conn.execute(
            "SELECT * FROM auth_users WHERE email = ? COLLATE NOCASE", (email,)
        ).fetchone()
        if row is None or not row["enabled"]:
            return None
        if row["subject"] is not None:
            # Already bound to a different (issuer, subject) — the exact-match
            # query above would have hit otherwise.
            logger.warning(
                "auth: login for email=%s denied — already bound to a different identity",
                _hash_email(email),
            )
            return None

        now = _now()
        conn.execute(
            "UPDATE auth_users SET issuer = ?, subject = ?, last_login_at = ? WHERE id = ?",
            (issuer, subject, now, row["id"]),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM auth_users WHERE id = ?", (row["id"],)).fetchone()
    return AuthUser.from_db_row(dict(row))
