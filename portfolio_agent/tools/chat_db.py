"""
Chat session database — stores APEX conversation history.

Two tables:
  chat_sessions  — one row per conversation (title, tickers, last update)
  chat_messages  — one row per message (role, content, optional metadata)

This enables Claude-like chat history: sessions listed in sidebar,
click to restore the full conversation.

Ownership
---------
chat_sessions carries an `owner` column (backfilled to LOCAL_OWNER on
pre-existing rows, idempotent — see the pattern in user_profile_db /
restricted_list_db). Every function below takes a verified RequestContext
first: list_sessions/new_session are scoped to ctx.actor, and every lookup by
session_id (get_session, get_messages, add_message, touch_session,
rename_session, delete_session) checks the session's owner before touching
it — a stranger's session_id raises account_service.NotAuthorized rather than
silently returning or mutating someone else's conversation.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

from portfolio_agent.domain import LOCAL_OWNER
from portfolio_agent.services.context import RequestContext, require_context
from portfolio_agent.tools.db import db_conn, migrate_columns


@contextmanager
def _db():
    def _setup(conn: sqlite3.Connection) -> None:
        _create_schema(conn)
        conn.commit()
    with db_conn(setup=_setup) as conn:
        yield conn


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_sessions (
            id          TEXT PRIMARY KEY,
            title       TEXT NOT NULL,
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
            tickers     TEXT DEFAULT '[]',
            last_rec    TEXT DEFAULT '',
            owner       TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  TEXT NOT NULL,
            role        TEXT NOT NULL,
            content     TEXT NOT NULL,
            msg_type    TEXT NOT NULL DEFAULT 'text',
            metadata    TEXT DEFAULT '{}',
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (session_id) REFERENCES chat_sessions(id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_chat_messages_session
        ON chat_messages (session_id, created_at)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_chat_sessions_updated
        ON chat_sessions (updated_at DESC)
    """)
    # Column must exist before any index references it — migrate/backfill
    # FIRST, since a pre-existing chat_sessions table predates the owner
    # column (CREATE TABLE IF NOT EXISTS above is a no-op on it).
    migrate_columns(conn, "chat_sessions", [("owner", "TEXT")])
    conn.execute("UPDATE chat_sessions SET owner = ? WHERE owner IS NULL", [LOCAL_OWNER])
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_chat_sessions_owner
        ON chat_sessions (owner, updated_at DESC)
    """)


def _owned_session(conn: sqlite3.Connection, ctx: RequestContext, session_id: str) -> dict:
    """The session row, if it exists and belongs to ctx.actor. Raises NotFound /
    NotAuthorized otherwise — never silently no-ops on someone else's session."""
    from portfolio_agent.services.account_service import NotAuthorized, NotFound

    row = conn.execute("SELECT * FROM chat_sessions WHERE id=?", [session_id]).fetchone()
    if row is None:
        raise NotFound(f"session {session_id} does not exist")
    d = dict(row)
    if d.get("owner") != ctx.actor:
        raise NotAuthorized(f"{ctx.actor!r} may not access session {session_id}")
    return d


# ── Session CRUD ──────────────────────────────────────────────────────────────

def new_session(ctx: RequestContext, title: str = "New conversation") -> str:
    """Create a new chat session owned by ctx.actor. Returns the session ID (UUID)."""
    require_context(ctx)
    sid = str(uuid.uuid4())
    with _db() as c:
        c.execute(
            "INSERT INTO chat_sessions (id, title, owner) VALUES (?, ?, ?)",
            [sid, title, ctx.actor],
        )
        c.commit()
    return sid


def rename_session(ctx: RequestContext, session_id: str, title: str) -> None:
    require_context(ctx)
    with _db() as c:
        _owned_session(c, ctx, session_id)
        c.execute("UPDATE chat_sessions SET title=? WHERE id=?", [title, session_id])
        c.commit()


def touch_session(ctx: RequestContext, session_id: str, tickers: list[str] = None,
                  last_rec: str = "") -> None:
    """Update updated_at (and optionally tickers/last_rec) for ctx.actor's own session."""
    require_context(ctx)
    now = datetime.now().isoformat()
    with _db() as c:
        _owned_session(c, ctx, session_id)
        c.execute(
            """UPDATE chat_sessions
               SET updated_at=?, tickers=?, last_rec=?
               WHERE id=?""",
            [now,
             json.dumps([t.upper() for t in (tickers or [])]),
             last_rec,
             session_id],
        )
        c.commit()


def list_sessions(ctx: RequestContext, limit: int = 100) -> list[dict]:
    """Return ctx.actor's own sessions, newest first, with the first user question attached."""
    require_context(ctx)
    with _db() as c:
        rows = c.execute(
            """
            SELECT s.*,
                   (SELECT content FROM chat_messages
                    WHERE session_id = s.id AND role = 'user'
                    ORDER BY created_at LIMIT 1) AS first_question
            FROM chat_sessions s
            WHERE s.owner = ?
            ORDER BY s.updated_at DESC LIMIT ?
            """,
            [ctx.actor, limit],
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["tickers"] = json.loads(d.get("tickers") or "[]")
        result.append(d)
    return result


def delete_session(ctx: RequestContext, session_id: str) -> None:
    require_context(ctx)
    with _db() as c:
        _owned_session(c, ctx, session_id)
        c.execute("DELETE FROM chat_messages WHERE session_id=?", [session_id])
        c.execute("DELETE FROM chat_sessions WHERE id=?", [session_id])
        c.commit()


# ── Message CRUD ──────────────────────────────────────────────────────────────

def add_message(
    ctx: RequestContext,
    session_id: str,
    role: str,
    content: str,
    msg_type: str = "text",
    metadata: Optional[dict] = None,
) -> int:
    """
    Add a message to ctx.actor's own session. Returns the new message id.

    msg_type: "text" | "prediction" | "plan" | "system"
    metadata: for "prediction" type, store the full pred_data dict
    """
    require_context(ctx)
    with _db() as c:
        _owned_session(c, ctx, session_id)
        cursor = c.execute(
            """INSERT INTO chat_messages
                   (session_id, role, content, msg_type, metadata)
               VALUES (?, ?, ?, ?, ?)""",
            [session_id, role, content, msg_type,
             json.dumps(metadata or {})],
        )
        c.commit()
        return cursor.lastrowid


def get_messages(ctx: RequestContext, session_id: str) -> list[dict]:
    """Return all messages for ctx.actor's own session, oldest first."""
    require_context(ctx)
    with _db() as c:
        _owned_session(c, ctx, session_id)
        rows = c.execute(
            "SELECT * FROM chat_messages WHERE session_id=? ORDER BY created_at",
            [session_id],
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["metadata"] = json.loads(d.get("metadata") or "{}")
        result.append(d)
    return result


def get_session(ctx: RequestContext, session_id: str) -> Optional[dict]:
    """ctx.actor's own session, or None if it doesn't exist. Raises NotAuthorized
    if it exists but belongs to someone else."""
    require_context(ctx)
    with _db() as c:
        row = c.execute(
            "SELECT * FROM chat_sessions WHERE id=?", [session_id]
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    if d.get("owner") != ctx.actor:
        from portfolio_agent.services.account_service import NotAuthorized
        raise NotAuthorized(f"{ctx.actor!r} may not access session {session_id}")
    d["tickers"] = json.loads(d.get("tickers") or "[]")
    return d
