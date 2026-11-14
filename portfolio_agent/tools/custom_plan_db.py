"""
Owner-scoped (PRIVATE) trade plans: a user's own stop / target, and the protective exit for a position they hold.

Table custom_trade_plans (created by prediction_db._migrate). Unlike trade_plans this is NOT market data: a row exists because of
what one person chose or holds. So:
  * it is written only by services.investor_service.create_custom_plan (authorised actor, one transaction, audit event);
  * every read here takes the owner and filters on it: there is no read that returns another owner's row;
  * the shared forecast path (forecast_writer, market_context, instrument_reference, the APEX pipeline, trade_plan_db) never imports
    this module (tests/test_exit_planner.py enforces it).
History is kept: a new plan for the same (owner, prediction, kind) supersedes the old one (the old row only gets superseded_by set).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from portfolio_agent.tools.prediction_db import _db

KINDS = ("custom_long", "protective")

_COLS = ("owner", "prediction_id", "kind", "stop_rule", "stop_param", "target_rule", "target_param", "entry", "stop", "target",
         "trim_level", "risk_per_share", "reward_risk", "time_stop_date", "plan_status", "no_plan_reason", "rules_version",
         "created_by")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def active_in(conn: sqlite3.Connection, owner: str, prediction_id: int, kind: str) -> dict | None:
    row = conn.execute("SELECT * FROM custom_trade_plans WHERE owner = ? AND prediction_id = ? AND kind = ? "
                       "AND superseded_by IS NULL ORDER BY id DESC LIMIT 1", [owner, int(prediction_id), kind]).fetchone()
    return dict(row) if row else None


def insert_plan(conn: sqlite3.Connection, **vals) -> int:
    """Insert on the caller's connection (so the service can put it, the supersede and the audit event in ONE transaction) and
    mark the previous active plan of the same (owner, prediction, kind) superseded. Returns the new id."""
    missing = [c for c in ("owner", "prediction_id", "kind", "stop_rule", "plan_status", "rules_version", "created_by") if not vals.get(c)]
    if missing:
        raise ValueError(f"custom plan: missing {missing}")
    if vals["kind"] not in KINDS:
        raise ValueError(f"custom plan: unknown kind {vals['kind']!r}")
    prev = active_in(conn, vals["owner"], vals["prediction_id"], vals["kind"])
    cols = [c for c in _COLS if c in vals]
    cur = conn.execute(f"INSERT INTO custom_trade_plans ({','.join(cols)}, created_at) VALUES ({','.join('?' * len(cols))}, ?)",
                       [vals[c] for c in cols] + [_now()])
    new_id = int(cur.lastrowid)
    if prev:
        conn.execute("UPDATE custom_trade_plans SET superseded_by = ? WHERE id = ?", [new_id, prev["id"]])
    return new_id


def get_active(owner: str, prediction_ids) -> dict[int, dict[str, dict]]:
    """{prediction_id: {kind: row}} of THIS owner's current (not superseded) plans. The owner filter is not optional."""
    if not owner:
        raise ValueError("custom plans are read per owner")
    ids = [int(i) for i in prediction_ids if i is not None]
    if not ids:
        return {}
    out: dict[int, dict[str, dict]] = {}
    with _db() as c:
        rows = c.execute(f"SELECT * FROM custom_trade_plans WHERE owner = ? AND superseded_by IS NULL "
                         f"AND prediction_id IN ({','.join('?' * len(ids))}) ORDER BY id", [owner, *ids]).fetchall()
    for r in rows:
        out.setdefault(r["prediction_id"], {})[r["kind"]] = dict(r)
    return out


def get_history(owner: str, prediction_id: int) -> list[dict]:
    if not owner:
        raise ValueError("custom plans are read per owner")
    with _db() as c:
        rows = c.execute("SELECT * FROM custom_trade_plans WHERE owner = ? AND prediction_id = ? ORDER BY id",
                         [owner, int(prediction_id)]).fetchall()
    return [dict(r) for r in rows]
