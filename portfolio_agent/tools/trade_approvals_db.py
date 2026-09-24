"""
Trade approvals — private, owner-scoped evidence that a specific action was
approved by someone/something with authority to grant it (compliance
officer, employer's pre-clearance desk, etc.). This is EVIDENCE the
deterministic policy engine (clearance.py) consults; it is never itself the
decision, and an LLM is never a valid `granted_by`.

Table: trade_approvals — append-only (a revoke or expiry is its own event
type recorded here, never an UPDATE/DELETE of the original grant, so a
compliance review sees the full history of what was approved and when it
stopped being valid).

Tied to a SPECIFIC action: (owner_scope, ticker, action) — an approval for
"AAPL buy" does not cover "AAPL sell" or "MSFT buy". expires_at is optional
(a standing approval), but get_active_approval always checks it against
`now`, and an approval whose expires_at has passed is not active — no
special "expired" status is needed since active-ness is computed at read
time, not written into the row.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from portfolio_agent.tools.db import db_conn

EVENT_GRANTED = "granted"
EVENT_REVOKED = "revoked"
VALID_EVENTS = frozenset({EVENT_GRANTED, EVENT_REVOKED})

GRANTED_BY_LLM_FORBIDDEN = "llm"   # sentinel: never a legitimate granted_by value


@contextmanager
def _db():
    def _setup(conn: sqlite3.Connection) -> None:
        _create_schema(conn)
        _migrate(conn)
        conn.commit()
    with db_conn(setup=_setup) as conn:
        yield conn


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trade_approvals (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_scope  TEXT NOT NULL,
            ticker       TEXT NOT NULL,
            action       TEXT NOT NULL,
            event        TEXT NOT NULL CHECK (event IN ('granted', 'revoked')),
            granted_by   TEXT NOT NULL,
            recorded_at  TEXT NOT NULL,
            expires_at   TEXT,
            evidence_ref TEXT,
            request_id   TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trade_approvals_lookup "
                "ON trade_approvals(owner_scope, ticker, action, id DESC)")


def _migrate(conn: sqlite3.Connection) -> None:
    """One-time rename: the single local owner was "user:local" before this
    deployment's one user was named "sumanth_b" ahead of registration/login.
    Idempotent -- a no-op once no row still says "user:local"."""
    from portfolio_agent.domain import LOCAL_OWNER
    conn.execute("UPDATE trade_approvals SET owner_scope = ? WHERE owner_scope = 'user:local'",
                 (f"user:{LOCAL_OWNER}",))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record_approval(*, owner_scope: str, ticker: str, action: str, granted_by: str,
                    expires_at: str | None = None, evidence_ref: str | None = None,
                    request_id: str | None = None) -> int:
    """Append a 'granted' event. granted_by must identify a real person/system
    with authority — never an LLM (see GRANTED_BY_LLM_FORBIDDEN)."""
    if granted_by.strip().lower() == GRANTED_BY_LLM_FORBIDDEN:
        raise ValueError("record_approval: an LLM cannot grant a trade approval")
    if not granted_by.strip():
        raise ValueError("record_approval: granted_by is required")
    return _append(owner_scope=owner_scope, ticker=ticker, action=action, event=EVENT_GRANTED,
                   granted_by=granted_by, expires_at=expires_at, evidence_ref=evidence_ref, request_id=request_id)


def revoke_approval(*, owner_scope: str, ticker: str, action: str, revoked_by: str,
                    evidence_ref: str | None = None) -> int:
    return _append(owner_scope=owner_scope, ticker=ticker, action=action, event=EVENT_REVOKED,
                   granted_by=revoked_by, expires_at=None, evidence_ref=evidence_ref, request_id=None)


def _append(*, owner_scope: str, ticker: str, action: str, event: str, granted_by: str,
           expires_at: str | None, evidence_ref: str | None, request_id: str | None) -> int:
    with _db() as conn:
        cur = conn.execute(
            """INSERT INTO trade_approvals
                   (owner_scope, ticker, action, event, granted_by, recorded_at, expires_at, evidence_ref, request_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (owner_scope, ticker.upper(), action, event, granted_by, _now_iso(), expires_at, evidence_ref, request_id),
        )
        conn.commit()
        return cur.lastrowid


def get_active_approval(owner_scope: str, ticker: str, action: str, *, now: str | None = None) -> dict | None:
    """
    The most recent 'granted' event for (owner_scope, ticker, action), if it
    is still active: not superseded by a later 'revoked' event, and (if
    expires_at is set) not yet expired as of `now` (default: current UTC time).
    Returns None — not a "safe" approval — for anything else, including a
    lookup that finds nothing at all.
    """
    now = now or _now_iso()
    with _db() as conn:
        row = conn.execute(
            """SELECT * FROM trade_approvals WHERE owner_scope = ? AND ticker = ? AND action = ?
               ORDER BY id DESC LIMIT 1""",
            (owner_scope, ticker.upper(), action),
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    if d["event"] != EVENT_GRANTED:
        return None
    if d["expires_at"] is not None and d["expires_at"] <= now:
        return None
    return d


def get_approval_history(owner_scope: str, ticker: str, action: str, limit: int = 25) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            """SELECT * FROM trade_approvals WHERE owner_scope = ? AND ticker = ? AND action = ?
               ORDER BY id DESC LIMIT ?""",
            (owner_scope, ticker.upper(), action, limit),
        ).fetchall()
    return [dict(r) for r in rows]
