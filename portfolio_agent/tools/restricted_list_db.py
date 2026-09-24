"""
Restricted-list database — personal compliance blocklist + admin pipeline
phase-skip list. These are deliberately different kinds of data (see below)
and this phase keeps them that way rather than scoping both the same way.

Tables:
  restricted_list — tickers blocked from new recommendations (ticker, reason,
                     added_at, owner). A PERSONAL restriction (e.g. an
                     employer's insider-trading blocklist for THIS person) —
                     read by clearance.py's PolicyDecision engine. Carries an
                     `owner` column (backfilled to LOCAL_OWNER, idempotent) and
                     an authorized read path, list_restricted_for(ctx); the
                     existing global functions (list_restricted/is_restricted/
                     add_restricted/remove_restricted) are UNCHANGED and still
                     what clearance.py and the admin UI use — see the Phase 2
                     report's inventory for what still needs migrating onto
                     the scoped path.
  pipeline_skip    — tickers silently skipped in specific pipeline phases
                     (ticker, phases (JSON list), reason, added_at). ADMINISTRATOR
                     configuration for the shared, market-only generation path
                     (which tickers the pipeline itself won't touch) — NOT a
                     personal preference, so it deliberately has no owner
                     column and is not touched by this phase at all.

phases for pipeline_skip: any subset of [news, research, fundamentals], or [all].
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import yaml

from portfolio_agent.domain import LOCAL_OWNER
from portfolio_agent.tools.db import db_conn, from_json_list, migrate_columns, to_json_list

_LEGACY_YAML = Path(__file__).resolve().parents[2] / "config" / "restricted_list.yaml"
_ALL_PHASES = frozenset({"news", "research", "fundamentals"})


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
        CREATE TABLE IF NOT EXISTS restricted_list (
            ticker   TEXT PRIMARY KEY,
            reason   TEXT,
            added_at TEXT NOT NULL,
            owner    TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pipeline_skip (
            ticker   TEXT PRIMARY KEY,
            phases   TEXT NOT NULL,
            reason   TEXT,
            added_at TEXT NOT NULL
        )
    """)


def _migrate(conn: sqlite3.Connection) -> None:
    """
    One-time seed from the legacy config/restricted_list.yaml — runs at most
    once, ever. The DB is the source of truth from here on: after a
    successful seed the YAML is renamed to `.migrated` so a later run never
    re-reads it. That matters because gating this on "table is empty" (the
    old approach) would silently resurrect entries from the stale YAML the
    moment someone deleted the last row via the UI — indistinguishable from
    the feature never having left YAML in the first place.

    Also backfills restricted_list.owner on any pre-existing row to
    LOCAL_OWNER — idempotent, a no-op once set, never touches pipeline_skip
    (deliberately un-owned — see module docstring).
    """
    migrate_columns(conn, "restricted_list", [("owner", "TEXT")])
    conn.execute("UPDATE restricted_list SET owner = ? WHERE owner IS NULL", (LOCAL_OWNER,))
    # One-time rename: LOCAL_OWNER was "local" before this deployment's single
    # user was named "sumanth_b" ahead of registration/login. Idempotent.
    conn.execute("UPDATE restricted_list SET owner = ? WHERE owner = 'local'", (LOCAL_OWNER,))
    if not _LEGACY_YAML.exists():
        return
    try:
        data = yaml.safe_load(_LEGACY_YAML.read_text()) or {}
    except Exception:
        return
    now = datetime.now(timezone.utc).isoformat()

    restricted = data.get("restricted") or []
    rows = [
        (str(e.get("ticker", "")).upper(), e.get("reason"), now, LOCAL_OWNER)
        for e in restricted if e.get("ticker")
    ]
    if rows:
        conn.executemany(
            "INSERT OR IGNORE INTO restricted_list (ticker, reason, added_at, owner) VALUES (?, ?, ?, ?)",
            rows,
        )

    skip = data.get("pipeline_skip") or {}
    rows = [
        (str(ticker).upper(), to_json_list(cfg.get("phases", [])), cfg.get("reason"), now)
        for ticker, cfg in skip.items()
    ]
    if rows:
        conn.executemany(
            "INSERT OR IGNORE INTO pipeline_skip (ticker, phases, reason, added_at) VALUES (?, ?, ?, ?)",
            rows,
        )

    conn.commit()
    try:
        _LEGACY_YAML.rename(_LEGACY_YAML.with_name(_LEGACY_YAML.name + ".migrated"))
    except OSError:
        pass  # migration itself already succeeded; a rename failure just means we re-check next time


# ── Compliance restricted list ────────────────────────────────────────────────

def list_restricted() -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT ticker, reason, added_at FROM restricted_list ORDER BY ticker"
        ).fetchall()
    return [dict(r) for r in rows]


def is_restricted(ticker: str) -> tuple[bool, str | None]:
    ticker_upper = ticker.upper()
    with _db() as conn:
        row = conn.execute(
            "SELECT reason FROM restricted_list WHERE ticker = ?", (ticker_upper,)
        ).fetchone()
    if row is None:
        return False, None
    return True, row[0] or "No reason specified"


def add_restricted(ticker: str, reason: str | None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _db() as conn:
        conn.execute(
            "INSERT INTO restricted_list (ticker, reason, added_at, owner) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(ticker) DO UPDATE SET reason = excluded.reason",
            (ticker.upper(), reason, now, LOCAL_OWNER),
        )
        conn.commit()


def remove_restricted(ticker: str) -> None:
    with _db() as conn:
        conn.execute("DELETE FROM restricted_list WHERE ticker = ?", (ticker.upper(),))
        conn.commit()


def list_restricted_for(ctx) -> list[dict]:
    """Authorized read: only the restrictions owned by ctx's verified identity."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT ticker, reason, added_at FROM restricted_list WHERE owner = ? ORDER BY ticker",
            (ctx.actor,),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Pipeline phase-skip list ──────────────────────────────────────────────────

def _load_skip() -> dict[str, dict]:
    with _db() as conn:
        rows = conn.execute("SELECT ticker, phases, reason FROM pipeline_skip").fetchall()
    return {
        r["ticker"]: {"phases": from_json_list(r["phases"]), "reason": r["reason"]}
        for r in rows
    }


def list_pipeline_skip() -> dict[str, dict]:
    return _load_skip()


def get_skip_tickers(phase: str) -> set[str]:
    """Return the set of tickers that should be skipped in *phase*."""
    skip: set[str] = set()
    for ticker, cfg in _load_skip().items():
        phases = cfg.get("phases", [])
        if "all" in phases or phase in phases:
            skip.add(ticker.upper())
    return skip


def is_all_phases_skipped(ticker: str) -> bool:
    """True if the ticker is configured to skip every phase (phases: [all] or all three listed)."""
    cfg = _load_skip().get(ticker.upper(), {})
    phases = set(cfg.get("phases", []))
    return "all" in phases or _ALL_PHASES.issubset(phases)


def add_pipeline_skip(ticker: str, phases: list[str], reason: str | None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _db() as conn:
        conn.execute(
            "INSERT INTO pipeline_skip (ticker, phases, reason, added_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(ticker) DO UPDATE SET phases = excluded.phases, reason = excluded.reason",
            (ticker.upper(), to_json_list(phases), reason, now),
        )
        conn.commit()


def remove_pipeline_skip(ticker: str) -> None:
    with _db() as conn:
        conn.execute("DELETE FROM pipeline_skip WHERE ticker = ?", (ticker.upper(),))
        conn.commit()


def log_skip(phase: str, skipped: list[str]) -> None:
    """Log a standardised skip line for the given phase."""
    if not skipped:
        return
    from portfolio_agent.log import get_logger as _get_logger
    _log = _get_logger("main")
    cfg = _load_skip()
    details = []
    for t in sorted(skipped):
        reason = cfg.get(t, {}).get("reason") or "restricted_list"
        details.append(f"{t} ({reason})")
    _log.info(f"  [restricted/{phase}] skipping {len(skipped)}: {', '.join(details)}",
              event_type="restricted_skip", phase=phase,
              tickers=sorted(skipped), count=len(skipped))
