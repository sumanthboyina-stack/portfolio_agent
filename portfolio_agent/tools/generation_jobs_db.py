"""
Work leases for shared (market-only) forecast generation — the concurrent-
deduplication layer. This is NOT the reuse-vs-generate decision (see
tools.run_planner for that); it only prevents two workers from calling a
provider for the exact same unit of work at the exact same time.

One row per work_key (see run_planner.work_key — instrument + horizon +
forecast origin/session + as_of_date), UNIQUE. claim_job() is the only way
to get permission to call a provider for that key; it succeeds only if no
OTHER lease on that key is currently active. A losing caller gets None and
should report WAIT_FOR_EXISTING_JOB, never call the provider itself.

Provider calls happen OUTSIDE any transaction here: claim_job() commits and
returns a lease_id BEFORE the caller ever calls a provider; the caller calls
the provider on its own time (arbitrarily long, network-bound), then calls
complete_job()/fail_job() afterward, in a separate, later transaction.

Lease recovery is not a separate step — an expired claim (leased_until in the
past) is simply available to claim_job() again, by anyone. This deliberately
never claims exactly-once provider billing: if a worker's provider call
genuinely completed just as its lease expired, a second worker's subsequent
claim may still result in a second provider call. What IS guaranteed:
  - no deadlock — an abandoned lease is always eventually reclaimable;
  - no double-publish — complete_job() only succeeds for the worker holding
    the CURRENT lease_id; a worker whose lease was reclaimed by someone else
    gets False back and must discard its result rather than write it or
    retry in a loop.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from portfolio_agent.tools.db import db_conn

DEFAULT_LEASE_SECONDS = 120.0

STATUS_CLAIMED = "claimed"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"


@contextmanager
def _db():
    def _setup(conn: sqlite3.Connection) -> None:
        _create_schema(conn)
        conn.commit()
    with db_conn(setup=_setup) as conn:
        yield conn


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS generation_jobs (
            work_key     TEXT PRIMARY KEY,
            lease_id     TEXT NOT NULL,
            worker_id    TEXT NOT NULL,
            status       TEXT NOT NULL CHECK (status IN ('claimed', 'completed', 'failed')),
            claimed_at   TEXT NOT NULL,
            leased_until TEXT NOT NULL,
            completed_at TEXT
        )
    """)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def claim_job(work_key: str, worker_id: str, *, lease_seconds: float = DEFAULT_LEASE_SECONDS) -> str | None:
    """
    Try to claim work_key. Returns a fresh lease_id on success, or None if
    another worker already holds an active (non-expired) lease on it — the
    caller should treat None as WAIT_FOR_EXISTING_JOB, not retry in a loop.

    Atomic across threads AND processes: BEGIN IMMEDIATE takes SQLite's
    write lock before the check, so two concurrent callers racing on the
    same work_key serialize through this function — exactly one gets a
    lease_id back for a given active window.
    """
    now_iso = _now_iso()
    leased_until = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat(timespec="seconds")
    lease_id = uuid4().hex
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT status, leased_until FROM generation_jobs WHERE work_key = ?", (work_key,)
            ).fetchone()
            if row is not None and row["status"] == STATUS_CLAIMED and row["leased_until"] > now_iso:
                conn.rollback()
                return None
            conn.execute(
                """INSERT INTO generation_jobs (work_key, lease_id, worker_id, status, claimed_at, leased_until)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(work_key) DO UPDATE SET
                       lease_id = excluded.lease_id, worker_id = excluded.worker_id, status = excluded.status,
                       claimed_at = excluded.claimed_at, leased_until = excluded.leased_until, completed_at = NULL""",
                (work_key, lease_id, worker_id, STATUS_CLAIMED, now_iso, leased_until),
            )
            conn.commit()
            return lease_id
        except BaseException:
            conn.rollback()
            raise


def complete_job(work_key: str, worker_id: str, lease_id: str, *, status: str = STATUS_COMPLETED) -> bool:
    """
    Publish the outcome of a claimed job. Returns True only if this exact
    (worker_id, lease_id) is still the CURRENT claim on work_key — false if
    the lease expired and was reclaimed by someone else in the meantime, in
    which case the caller must discard its result, never write it.
    """
    if status not in (STATUS_COMPLETED, STATUS_FAILED):
        raise ValueError(f"complete_job: status must be {STATUS_COMPLETED!r} or {STATUS_FAILED!r}, not {status!r}")
    with _db() as conn:
        cur = conn.execute(
            """UPDATE generation_jobs SET status = ?, completed_at = ?
               WHERE work_key = ? AND worker_id = ? AND lease_id = ? AND status = ?""",
            (status, _now_iso(), work_key, worker_id, lease_id, STATUS_CLAIMED),
        )
        conn.commit()
        return cur.rowcount > 0


def get_job(work_key: str) -> dict | None:
    with _db() as conn:
        row = conn.execute("SELECT * FROM generation_jobs WHERE work_key = ?", (work_key,)).fetchone()
    return dict(row) if row else None
