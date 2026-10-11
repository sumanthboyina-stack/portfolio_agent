"""
Manual fund breakdown — PRIVATE, owner-scoped.

What a person typed in from a plan fact sheet for a fund the data providers know nothing about (a 401(k) collective
trust, a stable value fund): its asset-class mix and, optionally, sector mix, in percentage points as a fact sheet
shows them. History is kept: one ACTIVE row per (owner, instrument), older rows superseded.

Never imported by the shared forecast path (tools.instrument_reference / market_context) — that is why this is not part
of tools.fund_data_db. Writes happen through services.fund_data_service (ownership check + audit event).
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
        CREATE TABLE IF NOT EXISTS fund_manual_breakdown (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            owner            TEXT NOT NULL,
            instrument_id    INTEGER NOT NULL,
            asset_class_json TEXT NOT NULL,
            sector_json      TEXT,
            as_of            TEXT,
            created_at       TEXT NOT NULL,
            superseded_at    TEXT
        )""")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_fund_manual_breakdown_active "
                 "ON fund_manual_breakdown(owner, instrument_id) WHERE superseded_at IS NULL")


def _ensure_once() -> None:
    from portfolio_agent.tools import db as _db_module
    key = str(_db_module.DB_PATH)
    if key not in _schema_ready:
        with db_conn(setup=lambda c: (_create_schema(c), c.commit())):
            pass
        _schema_ready.add(key)


@contextmanager
def _conn(conn: sqlite3.Connection | None):
    _ensure_once()
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


def get_manual_breakdown(owner: str, instrument_id: int, *, conn: sqlite3.Connection | None = None) -> dict | None:
    """{asset_class: {...pct}, sector: {...pct} | None, as_of, created_at} — the owner's active row, else None."""
    with _conn(conn) as c:
        r = c.execute("SELECT * FROM fund_manual_breakdown WHERE owner = ? AND instrument_id = ? AND superseded_at IS NULL",
                      [owner, instrument_id]).fetchone()
    if r is None:
        return None
    return {"instrument_id": instrument_id, "asset_class": json.loads(r["asset_class_json"]),
            "sector": json.loads(r["sector_json"]) if r["sector_json"] else None, "as_of": r["as_of"],
            "created_at": r["created_at"]}


def list_manual_breakdowns(owner: str, *, conn: sqlite3.Connection | None = None) -> dict[int, dict]:
    with _conn(conn) as c:
        ids = [r["instrument_id"] for r in c.execute(
            "SELECT instrument_id FROM fund_manual_breakdown WHERE owner = ? AND superseded_at IS NULL", [owner]).fetchall()]
        return {i: get_manual_breakdown(owner, i, conn=c) for i in ids}


def replace_manual_breakdown(owner: str, instrument_id: int, *, asset_class: dict, sector: dict | None, as_of: str | None,
                             conn: sqlite3.Connection | None = None) -> None:
    now = _now()
    with _conn(conn) as c:
        c.execute("UPDATE fund_manual_breakdown SET superseded_at = ? WHERE owner = ? AND instrument_id = ? AND superseded_at IS NULL",
                  [now, owner, instrument_id])
        c.execute("INSERT INTO fund_manual_breakdown (owner, instrument_id, asset_class_json, sector_json, as_of, created_at) "
                  "VALUES (?, ?, ?, ?, ?, ?)",
                  [owner, instrument_id, json.dumps(asset_class), json.dumps(sector) if sector else None, as_of, now])


def clear_manual_breakdown(owner: str, instrument_id: int, *, conn: sqlite3.Connection | None = None) -> int:
    with _conn(conn) as c:
        return c.execute("UPDATE fund_manual_breakdown SET superseded_at = ? WHERE owner = ? AND instrument_id = ? "
                         "AND superseded_at IS NULL", [_now(), owner, instrument_id]).rowcount


def manual_breakdown_version(owner: str, *, conn: sqlite3.Connection | None = None) -> str:
    """Fingerprint of the owner's active manual breakdowns — part of the exposures cache key."""
    with _conn(conn) as c:
        rows = c.execute("SELECT instrument_id, id FROM fund_manual_breakdown WHERE owner = ? AND superseded_at IS NULL "
                         "ORDER BY instrument_id", [owner]).fetchall()
    return "|".join(f"{r['instrument_id']}:{r['id']}" for r in rows) or "none"
