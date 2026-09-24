"""
Policy decisions — private, append-only record of every pre-trade clearance
evaluation. Each row is a PolicyDecision: an action-specific permission or
approval state for one (ticker, action), decided entirely in Python (never
an LLM) and scoped to the owner it was decided for. Nothing here is shared.

Table: policy_decisions — append-only, one row per evaluation (never updated
in place, so a compliance review can see every decision ever made, not just
the latest).

Decision values (see portfolio_agent.clearance for the full evaluation):
  ALLOWED_BY_RULES   nothing in this system's rules blocks the action — NOT
                     external compliance approval.
  BLOCKED            a restricted-list hit or an active blackout window —
                     enforced regardless of the pre-clearance preference.
  PENDING_APPROVAL   pre-clearance is required and no active approval exists
                     for this exact (ticker, action).
  UNKNOWN            a required check could not be completed, or the policy
                     data involved (e.g. a blackout window's dates) was
                     invalid — never treated as "no restriction."

next_transition_at / valid_until: when the evaluating engine expects this
decision could next change (a blackout window boundary, an approval's
expiry, or worst-case "start of tomorrow") — a caller re-checks after this
point rather than treating an old row as still current indefinitely.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from portfolio_agent.tools.db import db_conn, migrate_columns

DECISION_ALLOWED = "ALLOWED_BY_RULES"
DECISION_BLOCKED = "BLOCKED"
DECISION_PENDING_APPROVAL = "PENDING_APPROVAL"
DECISION_UNKNOWN = "UNKNOWN"
VALID_DECISIONS = frozenset({DECISION_ALLOWED, DECISION_BLOCKED, DECISION_PENDING_APPROVAL, DECISION_UNKNOWN})

# Legacy decision values this table may still hold from before policy_version
# clearance-policy/3.0 — never written again, but old rows are not rewritten
# (see Phase 6 report); readers should treat these as their nearest new
# equivalent if they need to compare across the rename (CLEARED/SKIPPED ->
# ALLOWED_BY_RULES, INSUFFICIENT_DATA -> UNKNOWN).
LEGACY_DECISIONS = frozenset({"CLEARED", "SKIPPED", "INSUFFICIENT_DATA"})

DECIDED_BY_DETERMINISTIC = "deterministic-policy-engine"   # the only value this ever writes — an LLM never decides


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
        CREATE TABLE IF NOT EXISTS policy_decisions (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            evaluated_at      TEXT NOT NULL,
            owner_scope       TEXT NOT NULL,
            ticker            TEXT NOT NULL,
            action            TEXT NOT NULL DEFAULT 'trade',
            decision          TEXT NOT NULL,
            reason            TEXT,
            evidence_refs     TEXT,
            policy_version    TEXT NOT NULL,
            decided_by        TEXT NOT NULL,
            request_id        TEXT,
            next_transition_at TEXT,
            valid_until        TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_policy_decisions_owner "
                "ON policy_decisions(owner_scope, ticker, action, evaluated_at DESC)")


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive: rows written before action-tagging/next_transition_at existed
    get action='trade' (the only action the engine ever evaluated back then)
    and NULL transition fields — never rewritten beyond that default."""
    migrate_columns(conn, "policy_decisions", [
        ("action", "TEXT NOT NULL DEFAULT 'trade'"),
        ("next_transition_at", "TEXT"),
        ("valid_until", "TEXT"),
    ])
    # One-time rename: the single local owner was "user:local" before this
    # deployment's one user was named "sumanth_b" ahead of registration/login.
    # Idempotent -- a no-op once no row still says "user:local".
    from portfolio_agent.domain import LOCAL_OWNER
    conn.execute("UPDATE policy_decisions SET owner_scope = ? WHERE owner_scope = 'user:local'",
                 (f"user:{LOCAL_OWNER}",))


def record_policy_decision(*, owner_scope: str, ticker: str, decision: str, reason: str | None,
                          evidence_refs: dict, policy_version: str, action: str = "trade",
                          request_id: str | None = None, next_transition_at: str | None = None,
                          valid_until: str | None = None,
                          decided_by: str = DECIDED_BY_DETERMINISTIC) -> int:
    """Append one PolicyDecision row. decided_by defaults to (and should always be) the
    deterministic engine — this is the enforcement point for "an LLM cannot decide policy"."""
    if decision not in VALID_DECISIONS:
        raise ValueError(f"record_policy_decision: decision must be one of {sorted(VALID_DECISIONS)}, not {decision!r}")
    if decided_by != DECIDED_BY_DETERMINISTIC:
        raise ValueError("record_policy_decision: policy decisions may only be recorded as "
                         f"decided_by={DECIDED_BY_DETERMINISTIC!r}")
    with _db() as conn:
        cur = conn.execute(
            """INSERT INTO policy_decisions
                   (evaluated_at, owner_scope, ticker, action, decision, reason, evidence_refs,
                    policy_version, decided_by, request_id, next_transition_at, valid_until)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [datetime.now(timezone.utc).isoformat(timespec="seconds"), owner_scope, ticker.upper(), action,
             decision, reason, json.dumps(evidence_refs, default=str), policy_version, decided_by, request_id,
             next_transition_at, valid_until],
        )
        conn.commit()
        return cur.lastrowid


def get_latest_policy_decision(owner_scope: str, ticker: str, action: str | None = None) -> dict | None:
    """action=None matches any action (legacy callers) — pass it explicitly
    once a caller cares about more than one action for the same ticker."""
    clause, params = ("AND action = ?", [action]) if action is not None else ("", [])
    with _db() as conn:
        row = conn.execute(
            f"""SELECT * FROM policy_decisions WHERE owner_scope = ? AND ticker = ? {clause}
                ORDER BY id DESC LIMIT 1""",
            [owner_scope, ticker.upper(), *params],
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["evidence_refs"] = json.loads(d["evidence_refs"]) if d.get("evidence_refs") else {}
    return d


def get_policy_decision_history(owner_scope: str, ticker: str, action: str | None = None, limit: int = 25) -> list[dict]:
    """Every decision ever recorded for (owner, ticker[, action]) — append-only audit trail."""
    clause, params = ("AND action = ?", [action]) if action is not None else ("", [])
    with _db() as conn:
        rows = conn.execute(
            f"""SELECT * FROM policy_decisions WHERE owner_scope = ? AND ticker = ? {clause}
                ORDER BY id DESC LIMIT ?""",
            [owner_scope, ticker.upper(), *params, limit],
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["evidence_refs"] = json.loads(d["evidence_refs"]) if d.get("evidence_refs") else {}
        out.append(d)
    return out
