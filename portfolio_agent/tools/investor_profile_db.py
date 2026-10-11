"""
Investor profile — PRIVATE, owner-scoped storage for one person's risk profile, emergency reserve, optional custom
target allocation and optional goals. Data layer only: services.investor_service is the writer (RequestContext,
ownership, audit event). Nothing here is ever read by the shared market-forecast path or the prediction pipeline
(tests/test_investor_profile.py enforces that boundary), and nothing here is an investment recommendation.

Tables (all keyed by owner — the verified identity's id):
  investor_profile         one ACTIVE row per owner (superseded_at IS NULL), older rows kept as history. A change to
                           the profile, the reserve or the quiz result closes the active row and inserts a new one.
                           No row at all means MODERATE / source DEFAULT / the global default reserve — that case is
                           computed by tools.portfolio_policy, it does not need a row.
  target_allocation_custom one active row per owner at most; replaces the profile's allocation for that person.
  investment_goals         optional and minimal. Nothing requires a goal and nothing reads goals yet.
                           TODO(retirement planning): goals (RETIREMENT with a target date and amount) are where a
                           later retirement-planning feature will start. The emergency reserve is NOT a goal; it is the
                           single investor_profile.emergency_reserve_amount value.

Every function takes the owner explicitly. Pass conn= to join the caller's transaction (the service does, so a write
and its audit event land together or not at all).
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from portfolio_agent.domain import GOAL_STATUSES, GOAL_TYPES, PROFILE_SOURCES, RISK_PROFILES
from portfolio_agent.tools.db import db_conn

_schema_ready: set[str] = set()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS investor_profile (
            id                        INTEGER PRIMARY KEY AUTOINCREMENT,
            owner                     TEXT NOT NULL,
            profile                   TEXT NOT NULL CHECK (profile IN ('CONSERVATIVE', 'MODERATE', 'AGGRESSIVE')),
            source                    TEXT NOT NULL CHECK (source IN ('DEFAULT', 'QUIZ', 'PICKED')),
            quiz_answers_json         TEXT,
            quiz_version              INTEGER,
            cap_profile               TEXT CHECK (cap_profile IS NULL
                                                  OR cap_profile IN ('CONSERVATIVE', 'MODERATE', 'AGGRESSIVE')),
            above_cap_acknowledged    INTEGER NOT NULL DEFAULT 0,
            emergency_reserve_amount  REAL CHECK (emergency_reserve_amount IS NULL OR emergency_reserve_amount >= 0),
            created_at                TEXT NOT NULL,
            superseded_at             TEXT
        )""")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_investor_profile_active "
                 "ON investor_profile(owner) WHERE superseded_at IS NULL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS target_allocation_custom (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            owner            TEXT NOT NULL,
            allocation_json  TEXT NOT NULL,
            created_at       TEXT NOT NULL,
            superseded_at    TEXT
        )""")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_target_allocation_custom_active "
                 "ON target_allocation_custom(owner) WHERE superseded_at IS NULL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS investment_goals (
            goal_id        INTEGER PRIMARY KEY AUTOINCREMENT,
            owner          TEXT NOT NULL,
            goal_type      TEXT NOT NULL CHECK (goal_type IN ('RETIREMENT', 'HOUSE', 'EDUCATION', 'OTHER')),
            name           TEXT NOT NULL,
            target_amount  REAL CHECK (target_amount IS NULL OR target_amount >= 0),
            target_date    TEXT,
            status         TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'DONE')),
            created_at     TEXT NOT NULL,
            updated_at     TEXT NOT NULL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_investment_goals_owner ON investment_goals(owner, status)")


def _ensure_schema_once() -> None:
    """Create the tables once per process per database file — reads on the hot path (every policy resolution) must
    not re-run DDL on each connection."""
    from portfolio_agent.tools import db as _db_module
    key = str(_db_module.DB_PATH)
    if key not in _schema_ready:
        with db_conn(setup=lambda c: (_create_schema(c), c.commit())):
            pass
        _schema_ready.add(key)


@contextmanager
def _conn(conn: sqlite3.Connection | None):
    """The caller's connection (never committed here), or a fresh one committed on success."""
    _ensure_schema_once()
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


def _owner(owner: str) -> str:
    if not owner or not str(owner).strip():
        raise ValueError("investor profile: owner is required")
    return str(owner)


# ── Investor profile ──────────────────────────────────────────────────────────

def _profile_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    d["above_cap_acknowledged"] = bool(d["above_cap_acknowledged"])
    d["quiz_answers"] = json.loads(d.pop("quiz_answers_json")) if d.get("quiz_answers_json") else None
    d.pop("quiz_answers_json", None)
    return d


def get_active_investor_profile(owner: str, *, conn: sqlite3.Connection | None = None) -> dict | None:
    """The owner's active row, or None (= computed defaults; see portfolio_policy)."""
    with _conn(conn) as c:
        return _profile_dict(c.execute(
            "SELECT * FROM investor_profile WHERE owner = ? AND superseded_at IS NULL", [_owner(owner)]).fetchone())


def list_investor_profile_history(owner: str, limit: int = 25, *, conn: sqlite3.Connection | None = None) -> list[dict]:
    """Newest first, including the active row."""
    with _conn(conn) as c:
        rows = c.execute("SELECT * FROM investor_profile WHERE owner = ? ORDER BY id DESC LIMIT ?",
                         [_owner(owner), limit]).fetchall()
    return [_profile_dict(r) for r in rows]


def replace_investor_profile(owner: str, *, profile: str, source: str, quiz_answers: dict | None = None,
                             quiz_version: int | None = None, cap_profile: str | None = None,
                             above_cap_acknowledged: bool = False, emergency_reserve_amount: float | None = None,
                             conn: sqlite3.Connection | None = None) -> dict:
    """Close the owner's active row (if any) and insert the new one. Validation of values is the service's job; the
    CHECK constraints are the backstop."""
    if profile not in RISK_PROFILES:
        raise ValueError(f"profile must be one of {list(RISK_PROFILES)}, got {profile!r}")
    if source not in PROFILE_SOURCES:
        raise ValueError(f"source must be one of {sorted(PROFILE_SOURCES)}, got {source!r}")
    now = _now()
    with _conn(conn) as c:
        c.execute("UPDATE investor_profile SET superseded_at = ? WHERE owner = ? AND superseded_at IS NULL",
                  [now, _owner(owner)])
        cur = c.execute(
            """INSERT INTO investor_profile (owner, profile, source, quiz_answers_json, quiz_version, cap_profile,
                                             above_cap_acknowledged, emergency_reserve_amount, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [owner, profile, source, json.dumps(quiz_answers) if quiz_answers is not None else None, quiz_version,
             cap_profile, int(bool(above_cap_acknowledged)),
             None if emergency_reserve_amount is None else float(emergency_reserve_amount), now])
        return _profile_dict(c.execute("SELECT * FROM investor_profile WHERE id = ?", [cur.lastrowid]).fetchone())


# ── Custom target allocation ──────────────────────────────────────────────────

def get_active_custom_allocation(owner: str, *, conn: sqlite3.Connection | None = None) -> dict | None:
    """{asset_class: {target, min, max}} or None when the owner uses their profile's allocation."""
    with _conn(conn) as c:
        row = c.execute("SELECT allocation_json FROM target_allocation_custom "
                        "WHERE owner = ? AND superseded_at IS NULL", [_owner(owner)]).fetchone()
    return json.loads(row["allocation_json"]) if row else None


def replace_custom_allocation(owner: str, allocation: dict, *, conn: sqlite3.Connection | None = None) -> None:
    now = _now()
    with _conn(conn) as c:
        c.execute("UPDATE target_allocation_custom SET superseded_at = ? WHERE owner = ? AND superseded_at IS NULL",
                  [now, _owner(owner)])
        c.execute("INSERT INTO target_allocation_custom (owner, allocation_json, created_at) VALUES (?, ?, ?)",
                  [owner, json.dumps(allocation), now])


def clear_custom_allocation(owner: str, *, conn: sqlite3.Connection | None = None) -> int:
    """Supersede the active custom allocation ('use profile default'). Returns rows closed (0 = there was none)."""
    with _conn(conn) as c:
        return c.execute("UPDATE target_allocation_custom SET superseded_at = ? "
                         "WHERE owner = ? AND superseded_at IS NULL", [_now(), _owner(owner)]).rowcount


# ── Goals ─────────────────────────────────────────────────────────────────────

_GOAL_EDITABLE = ("goal_type", "name", "target_amount", "target_date", "status")


def _clean_goal_fields(fields: dict) -> dict:
    out: dict = {}
    if "goal_type" in fields:
        if fields["goal_type"] not in GOAL_TYPES:
            raise ValueError(f"goal_type must be one of {sorted(GOAL_TYPES)}")
        out["goal_type"] = fields["goal_type"]
    if "name" in fields:
        if not (fields["name"] or "").strip():
            raise ValueError("a goal needs a name")
        out["name"] = fields["name"].strip()
    if "target_amount" in fields:
        v = fields["target_amount"]
        if v is not None and (float(v) < 0 or float(v) != float(v)):
            raise ValueError("target_amount must be 0 or more")
        out["target_amount"] = None if v is None else float(v)
    if "target_date" in fields:
        v = fields["target_date"]
        if v is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(v)):
            raise ValueError("target_date must be an ISO date (YYYY-MM-DD)")
        out["target_date"] = None if v is None else str(v)
    if "status" in fields:
        if fields["status"] not in GOAL_STATUSES:
            raise ValueError(f"status must be one of {sorted(GOAL_STATUSES)}")
        out["status"] = fields["status"]
    unknown = set(fields) - set(_GOAL_EDITABLE)
    if unknown:
        raise ValueError(f"cannot set goal field(s) {sorted(unknown)}")
    return out


def add_goal(owner: str, *, goal_type: str, name: str, target_amount: float | None = None,
             target_date: str | None = None, conn: sqlite3.Connection | None = None) -> dict:
    f = _clean_goal_fields({"goal_type": goal_type, "name": name, "target_amount": target_amount,
                            "target_date": target_date})
    now = _now()
    with _conn(conn) as c:
        cur = c.execute(
            "INSERT INTO investment_goals (owner, goal_type, name, target_amount, target_date, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [_owner(owner), f["goal_type"], f["name"], f["target_amount"], f["target_date"], now, now])
        return dict(c.execute("SELECT * FROM investment_goals WHERE goal_id = ?", [cur.lastrowid]).fetchone())


def get_goal(owner: str, goal_id: int, *, conn: sqlite3.Connection | None = None) -> dict | None:
    """The goal only if it belongs to *owner* — another owner's id looks exactly like a missing one."""
    with _conn(conn) as c:
        row = c.execute("SELECT * FROM investment_goals WHERE goal_id = ? AND owner = ?",
                        [goal_id, _owner(owner)]).fetchone()
    return dict(row) if row else None


def list_goals(owner: str, *, include_done: bool = True, conn: sqlite3.Connection | None = None) -> list[dict]:
    sql = "SELECT * FROM investment_goals WHERE owner = ?" + ("" if include_done else " AND status = 'ACTIVE'")
    with _conn(conn) as c:
        return [dict(r) for r in c.execute(sql + " ORDER BY goal_id", [_owner(owner)]).fetchall()]


def update_goal(owner: str, goal_id: int, *, conn: sqlite3.Connection | None = None, **fields) -> int:
    """Rows changed (0 = no such goal for this owner)."""
    f = _clean_goal_fields(fields)
    if not f:
        return 0
    with _conn(conn) as c:
        return c.execute(
            f"UPDATE investment_goals SET {', '.join(f'{k} = ?' for k in f)}, updated_at = ? "
            "WHERE goal_id = ? AND owner = ?", [*f.values(), _now(), goal_id, _owner(owner)]).rowcount


def delete_goal(owner: str, goal_id: int, *, conn: sqlite3.Connection | None = None) -> int:
    with _conn(conn) as c:
        return c.execute("DELETE FROM investment_goals WHERE goal_id = ? AND owner = ?",
                         [goal_id, _owner(owner)]).rowcount
