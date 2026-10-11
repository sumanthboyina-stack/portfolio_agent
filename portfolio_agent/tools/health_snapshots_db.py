"""
Portfolio Health snapshots — PRIVATE, owner-scoped.

One row per (owner, day): the statuses (and, for the household, the numbers) of every check on that day, so the page can
say what changed since the last snapshot and since about 30 days ago. A row is written once a day and refreshed if the
page is opened again the same day; earlier days are never rewritten. Writes go through services.health_service (audit
event); this module is storage plus the pure diff.

Payload shape (JSON in checks_json):
    {"as_of": "2026-10-11", "health_version": "...", "profile": {...},
     "scopes": {"household": {"label", "total_value", "counts": {...}, "checks": {key: {"title", "status", "summary",
                              "reason_codes", "metrics"}}},
                "account:3": {...}, "tax:TAXABLE": {...}}}
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

from portfolio_agent.domain import HealthStatus
from portfolio_agent.tools.db import db_conn

_schema_ready: set[str] = set()
_SEV = {HealthStatus.GOOD.value: 0, HealthStatus.UNKNOWN.value: 1, HealthStatus.WATCH.value: 2, HealthStatus.ACTION.value: 3}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS health_snapshots (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            owner          TEXT NOT NULL,
            snapshot_date  TEXT NOT NULL,
            checks_json    TEXT NOT NULL,
            counts_json    TEXT NOT NULL,
            health_version TEXT,
            created_at     TEXT NOT NULL,
            updated_at     TEXT NOT NULL,
            UNIQUE (owner, snapshot_date)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_health_snapshots_owner_date ON health_snapshots(owner, snapshot_date DESC)")


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


# ── Payload ───────────────────────────────────────────────────────────────────

def payload_from_health(health) -> dict:
    """The JSON stored for one PortfolioHealth. Household keeps the numbers; slices keep statuses and summaries."""
    scopes = {}
    for sc in health.scopes():
        keep_metrics = sc.scope == "household"
        scopes[sc.scope] = {
            "label": sc.label, "total_value": sc.total_value, "counts": sc.counts,
            "checks": {c.key: {"title": c.title, "status": c.status, "summary": c.summary,
                               "reason_codes": list(c.reason_codes), **({"metrics": c.metrics} if keep_metrics else {})}
                       for c in sc.checks}}
    return {"as_of": health.as_of, "health_version": health.health_version, "profile": health.profile, "scopes": scopes}


# ── Storage ───────────────────────────────────────────────────────────────────

def upsert_snapshot(owner: str, snapshot_date: str, payload: dict, *, conn: sqlite3.Connection | None = None) -> str:
    """Insert today's row or refresh it. Returns 'created' or 'updated'."""
    now = _now()
    counts = payload["scopes"]["household"]["counts"]
    with _conn(conn) as c:
        existed = c.execute("SELECT 1 FROM health_snapshots WHERE owner = ? AND snapshot_date = ?", [owner, snapshot_date]).fetchone()
        c.execute(
            """INSERT INTO health_snapshots (owner, snapshot_date, checks_json, counts_json, health_version, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(owner, snapshot_date) DO UPDATE SET checks_json = excluded.checks_json,
                 counts_json = excluded.counts_json, health_version = excluded.health_version, updated_at = excluded.updated_at""",
            [owner, snapshot_date, json.dumps(payload, default=str), json.dumps(counts), payload.get("health_version"), now, now])
    return "updated" if existed else "created"


def _row(r) -> dict:
    return {"snapshot_date": r["snapshot_date"], "payload": json.loads(r["checks_json"]), "counts": json.loads(r["counts_json"]),
            "created_at": r["created_at"], "updated_at": r["updated_at"]}


def get_snapshot(owner: str, snapshot_date: str, *, conn: sqlite3.Connection | None = None) -> dict | None:
    with _conn(conn) as c:
        r = c.execute("SELECT * FROM health_snapshots WHERE owner = ? AND snapshot_date = ?", [owner, snapshot_date]).fetchone()
    return _row(r) if r else None


def latest_before(owner: str, snapshot_date: str, *, conn: sqlite3.Connection | None = None) -> dict | None:
    """The newest snapshot strictly before *snapshot_date*."""
    with _conn(conn) as c:
        r = c.execute("SELECT * FROM health_snapshots WHERE owner = ? AND snapshot_date < ? ORDER BY snapshot_date DESC LIMIT 1",
                      [owner, snapshot_date]).fetchone()
    return _row(r) if r else None


def on_or_before(owner: str, snapshot_date: str, *, conn: sqlite3.Connection | None = None) -> dict | None:
    with _conn(conn) as c:
        r = c.execute("SELECT * FROM health_snapshots WHERE owner = ? AND snapshot_date <= ? ORDER BY snapshot_date DESC LIMIT 1",
                      [owner, snapshot_date]).fetchone()
    return _row(r) if r else None


def list_recent(owner: str, limit: int = 60, *, conn: sqlite3.Connection | None = None) -> list[dict]:
    """Newest first."""
    with _conn(conn) as c:
        rows = c.execute("SELECT * FROM health_snapshots WHERE owner = ? ORDER BY snapshot_date DESC LIMIT ?", [owner, limit]).fetchall()
    return [_row(r) for r in rows]


def thirty_days_ago(owner: str, today: str, *, conn: sqlite3.Connection | None = None) -> dict | None:
    """The newest snapshot on or before today − 30 days (None if the history is not that old yet)."""
    target = (date.fromisoformat(today) - timedelta(days=30)).isoformat()
    return on_or_before(owner, target, conn=conn)


# ── Pure diff ─────────────────────────────────────────────────────────────────

def diff_payloads(prev: dict | None, cur: dict) -> dict:
    """
    What changed from `prev` to `cur`: per scope and check, a status change with its direction (worse / better by
    severity ACTION > WATCH > UNKNOWN > GOOD); checks or scopes that appeared or disappeared; the household counts
    before and after. prev=None -> {"has_previous": False}.
    """
    if prev is None:
        return {"has_previous": False, "changes": [], "counts_from": None, "counts_to": cur["scopes"]["household"]["counts"]}
    changes = []
    for scope, now in cur["scopes"].items():
        before = prev["scopes"].get(scope)
        if before is None:
            changes.append({"scope": scope, "label": now["label"], "check": None, "title": "New scope", "from": None, "to": None,
                            "direction": "new"})
            continue
        for key, c in now["checks"].items():
            old = before["checks"].get(key)
            if old is None:
                changes.append({"scope": scope, "label": now["label"], "check": key, "title": c["title"], "from": None,
                                "to": c["status"], "direction": "new", "summary": c["summary"]})
            elif old["status"] != c["status"]:
                direction = "worse" if _SEV[c["status"]] > _SEV[old["status"]] else "better"
                changes.append({"scope": scope, "label": now["label"], "check": key, "title": c["title"], "from": old["status"],
                                "to": c["status"], "direction": direction, "summary": c["summary"]})
    for scope in prev["scopes"]:
        if scope not in cur["scopes"]:
            changes.append({"scope": scope, "label": prev["scopes"][scope]["label"], "check": None, "title": "Scope gone",
                            "from": None, "to": None, "direction": "removed"})
    changes.sort(key=lambda ch: (ch["scope"] != "household", ch["direction"] != "worse", ch["scope"], ch["check"] or ""))
    return {"has_previous": True, "previous_date": prev.get("as_of"), "changes": changes,
            "counts_from": prev["scopes"]["household"]["counts"], "counts_to": cur["scopes"]["household"]["counts"]}


def action_streak_start(owner_snapshots: list[dict], check_key: str, today: str, today_status: str, *, scope: str = "household") -> str:
    """
    The date the current run of ACTION for *check_key* began: walk back through the earlier snapshots (newest first,
    all dated before today) while the check was ACTION. Today alone if yesterday's snapshot was not ACTION (or there is
    none). Used as the notification de-duplication key, so one ongoing problem notifies once.
    """
    if today_status != HealthStatus.ACTION.value:
        return today
    start = today
    for snap in owner_snapshots:
        if snap["snapshot_date"] >= today:
            continue
        sc = snap["payload"]["scopes"].get(scope)
        st = (sc or {}).get("checks", {}).get(check_key, {}).get("status")
        if st != HealthStatus.ACTION.value:
            break
        start = snap["snapshot_date"]
    return start
