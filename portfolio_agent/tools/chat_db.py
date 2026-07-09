"""
Chat session database — stores APEX conversation history.

Two tables:
  chat_sessions  — one row per conversation (title, tickers, last update)
  chat_messages  — one row per message (role, content, optional metadata)

This enables Claude-like chat history: sessions listed in sidebar,
click to restore the full conversation.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

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
        CREATE TABLE IF NOT EXISTS chat_sessions (
            id          TEXT PRIMARY KEY,
            title       TEXT NOT NULL,
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
            tickers     TEXT DEFAULT '[]',
            last_rec    TEXT DEFAULT ''
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


# ── Session CRUD ──────────────────────────────────────────────────────────────

def new_session(title: str = "New conversation") -> str:
    """Create a new chat session. Returns the session ID (UUID)."""
    sid = str(uuid.uuid4())
    with _db() as c:
        c.execute(
            "INSERT INTO chat_sessions (id, title) VALUES (?, ?)",
            [sid, title],
        )
        c.commit()
    return sid


def rename_session(session_id: str, title: str) -> None:
    with _db() as c:
        c.execute("UPDATE chat_sessions SET title=? WHERE id=?", [title, session_id])
        c.commit()


def touch_session(session_id: str, tickers: list[str] = None,
                  last_rec: str = "") -> None:
    """Update updated_at (and optionally tickers/last_rec) for a session."""
    now = datetime.now().isoformat()
    with _db() as c:
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


def list_sessions(limit: int = 100) -> list[dict]:
    """Return all sessions, newest first, with the first user question attached."""
    with _db() as c:
        rows = c.execute(
            """
            SELECT s.*,
                   (SELECT content FROM chat_messages
                    WHERE session_id = s.id AND role = 'user'
                    ORDER BY created_at LIMIT 1) AS first_question
            FROM chat_sessions s
            ORDER BY s.updated_at DESC LIMIT ?
            """,
            [limit],
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["tickers"] = json.loads(d.get("tickers") or "[]")
        result.append(d)
    return result


def delete_session(session_id: str) -> None:
    with _db() as c:
        c.execute("DELETE FROM chat_messages WHERE session_id=?", [session_id])
        c.execute("DELETE FROM chat_sessions WHERE id=?", [session_id])
        c.commit()


# ── Message CRUD ──────────────────────────────────────────────────────────────

def add_message(
    session_id: str,
    role: str,
    content: str,
    msg_type: str = "text",
    metadata: Optional[dict] = None,
) -> int:
    """
    Add a message to a session. Returns the new message id.

    msg_type: "text" | "prediction" | "plan" | "system"
    metadata: for "prediction" type, store the full pred_data dict
    """
    with _db() as c:
        cursor = c.execute(
            """INSERT INTO chat_messages
                   (session_id, role, content, msg_type, metadata)
               VALUES (?, ?, ?, ?, ?)""",
            [session_id, role, content, msg_type,
             json.dumps(metadata or {})],
        )
        c.commit()
        return cursor.lastrowid


def get_messages(session_id: str) -> list[dict]:
    """Return all messages for a session, oldest first."""
    with _db() as c:
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


def get_session(session_id: str) -> Optional[dict]:
    with _db() as c:
        row = c.execute(
            "SELECT * FROM chat_sessions WHERE id=?", [session_id]
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["tickers"] = json.loads(d.get("tickers") or "[]")
    return d
