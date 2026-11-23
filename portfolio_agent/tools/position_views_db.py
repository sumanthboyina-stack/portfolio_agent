"""
Position views — PRIVATE, owner-scoped storage.

One row per (owner, ticker, day): the owner's deterministic position view (tools.position_view) as of that day, so the
page and the chat can read today's view and compare with the last one. A row is written once a day per ticker and refreshed
if the phase re-runs the same day; earlier days are never rewritten. Writes go through services.position_view_service
(audit event); this module is storage only. Every read takes the owner and filters on it.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from portfolio_agent.tools.db import db_conn

_schema_ready: set[str] = set()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS position_views (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            owner              TEXT NOT NULL,
            ticker             TEXT NOT NULL,
            view_date          TEXT NOT NULL,
            relation           TEXT NOT NULL,
            forecast_direction TEXT,
            fit                TEXT,
            view_json          TEXT NOT NULL,
            created_at         TEXT NOT NULL,
            updated_at         TEXT NOT NULL,
            UNIQUE (owner, ticker, view_date)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_position_views_owner_date ON position_views(owner, view_date DESC)")


def ensure_schema_once() -> None:
    from portfolio_agent.tools import db as _db_module
    key = str(_db_module.DB_PATH)
    if key not in _schema_ready:
        with db_conn(setup=lambda c: (_create_schema(c), c.commit())):
            pass
        _schema_ready.add(key)


@contextmanager
def _conn(conn: sqlite3.Connection | None):
    ensure_schema_once()
    if conn is not None:
        yield conn
        return
    with db_conn() as c:
        try:
            yield c
        except BaseException:
            c.rollback()
            raise
        else:
            c.commit()


def _row(r) -> dict:
    return {"ticker": r["ticker"], "view_date": r["view_date"], "relation": r["relation"], "view": json.loads(r["view_json"]),
            "created_at": r["created_at"], "updated_at": r["updated_at"]}


def upsert_view(owner: str, ticker: str, view_date: str, view: dict, *, conn: sqlite3.Connection | None = None) -> str:
    """Insert the day's row or refresh it. Returns 'created' or 'updated'."""
    now = _now()
    ticker = ticker.upper()
    with _conn(conn) as c:
        existed = c.execute("SELECT 1 FROM position_views WHERE owner = ? AND ticker = ? AND view_date = ?",
                            [owner, ticker, view_date]).fetchone()
        c.execute(
            """INSERT INTO position_views (owner, ticker, view_date, relation, forecast_direction, fit, view_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(owner, ticker, view_date) DO UPDATE SET relation = excluded.relation,
                 forecast_direction = excluded.forecast_direction, fit = excluded.fit, view_json = excluded.view_json,
                 updated_at = excluded.updated_at""",
            [owner, ticker, view_date, view["relation"], (view.get("forecast") or {}).get("direction"), view.get("fit"),
             json.dumps(view, default=str), now, now])
    return "updated" if existed else "created"


def get_latest(owner: str, ticker: str, *, conn: sqlite3.Connection | None = None) -> dict | None:
    with _conn(conn) as c:
        r = c.execute("SELECT * FROM position_views WHERE owner = ? AND ticker = ? ORDER BY view_date DESC LIMIT 1",
                      [owner, ticker.upper()]).fetchone()
    return _row(r) if r else None


def latest_before(owner: str, ticker: str, view_date: str, *, conn: sqlite3.Connection | None = None) -> dict | None:
    """The newest view strictly before *view_date* (to see what changed)."""
    with _conn(conn) as c:
        r = c.execute("SELECT * FROM position_views WHERE owner = ? AND ticker = ? AND view_date < ? ORDER BY view_date DESC LIMIT 1",
                      [owner, ticker.upper(), view_date]).fetchone()
    return _row(r) if r else None


def list_for_date(owner: str, view_date: str, *, conn: sqlite3.Connection | None = None) -> list[dict]:
    with _conn(conn) as c:
        rows = c.execute("SELECT * FROM position_views WHERE owner = ? AND view_date = ? ORDER BY ticker", [owner, view_date]).fetchall()
    return [_row(r) for r in rows]


def list_latest(owner: str, *, conn: sqlite3.Connection | None = None) -> list[dict]:
    """The newest stored view of each ticker the owner has one for."""
    with _conn(conn) as c:
        rows = c.execute(
            """SELECT p.* FROM position_views p
               JOIN (SELECT ticker, MAX(view_date) d FROM position_views WHERE owner = ? GROUP BY ticker) x
                 ON p.ticker = x.ticker AND p.view_date = x.d
               WHERE p.owner = ? ORDER BY p.ticker""", [owner, owner]).fetchall()
    return [_row(r) for r in rows]
