"""
Daily Risk + Technical specialist flags.

Decoupled from the prediction weight engine on purpose (Approach B — see
project decision): Technical/Risk run alongside APEX and write here, not into
`predictions`. No schema change to predictions, no re-weighting, no
disruption to the composite-score history the Validation/Track Record
tooling analyzes. `flag=1` rows are what the dashboard's Attention Queue
surfaces proactively.

One row per (ticker, as_of_date, source, owner); source is 'risk' or
'technical'.

Ownership — 'technical' is shared, 'risk' is per-owner
--------------------------------------------------------
The technical specialist's output (SMA/RSI/MACD/momentum) is pure market
data: the same for every owner holding a ticker. Duplicating it per owner
would be pure waste, so it is written ONCE per (ticker, date) with the
SHARED_OWNER sentinel ('', never NULL — SQLite's UNIQUE treats every NULL as
distinct, which would silently defeat the upsert's ON CONFLICT).

The risk specialist's output (concentration %, correlation with the rest of
the book, beta contribution) genuinely depends on whose portfolio it is —
computing it once against the combined book of every owner (the bug this
closes: risk_flags used to blend everyone's positions into one flag) gives
every owner a wrong number. So 'risk' rows are keyed per real owner:
upsert_risk_flag(..., owner=X) for a specific owner's row, and every read
(get_active_flags/get_flags_for_ticker) takes an explicit owner and returns
that owner's own 'risk' rows PLUS the shared 'technical' rows — never
another owner's 'risk' row.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Optional

from portfolio_agent.tools.db import db_conn, safe_float

SHARED_OWNER = ""   # sentinel for 'technical' rows — see module docstring


@contextmanager
def _db():
    def _setup(conn: sqlite3.Connection) -> None:
        _create_schema(conn)
        conn.commit()
    with db_conn(setup=_setup) as conn:
        yield conn


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS risk_flags (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker      TEXT NOT NULL,
            as_of_date  TEXT NOT NULL,
            source      TEXT NOT NULL CHECK(source IN ('risk','technical')),
            owner       TEXT NOT NULL DEFAULT '',
            flag        INTEGER NOT NULL DEFAULT 0,
            score       REAL,
            summary     TEXT,
            raw_json    TEXT,
            model_used  TEXT,
            fetched_at  TEXT DEFAULT (datetime('now')),
            UNIQUE(ticker, as_of_date, source, owner)
        );

        CREATE INDEX IF NOT EXISTS idx_risk_flags_date
            ON risk_flags(as_of_date, flag);
    """)
    _migrate_owner_scoping(conn)


def _migrate_owner_scoping(conn: sqlite3.Connection) -> None:
    """
    One-time rebuild from UNIQUE(ticker, as_of_date, source) to UNIQUE(ticker,
    as_of_date, source, owner) — the bug this closes: the daily batch computed
    ONE 'risk' row per ticker against the combined holdings of every owner,
    so every owner's dashboard/chat saw the same (wrong, for anyone but the
    biggest book) concentration/correlation numbers. Pre-existing 'technical'
    rows are shared already (SHARED_OWNER); pre-existing 'risk' rows are
    backfilled to LOCAL_OWNER — this deployment's one real owner at the time
    they were computed, not a guess. Runs once; a no-op once owner exists.
    """
    from portfolio_agent.domain import LOCAL_OWNER

    cols = {r[1] for r in conn.execute("PRAGMA table_info(risk_flags)").fetchall()}
    if not cols or "owner" in cols:
        return
    conn.execute("ALTER TABLE risk_flags RENAME TO risk_flags_legacy_v1")
    conn.execute("DROP INDEX IF EXISTS idx_risk_flags_date")
    conn.executescript("""
        CREATE TABLE risk_flags (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker      TEXT NOT NULL,
            as_of_date  TEXT NOT NULL,
            source      TEXT NOT NULL CHECK(source IN ('risk','technical')),
            owner       TEXT NOT NULL DEFAULT '',
            flag        INTEGER NOT NULL DEFAULT 0,
            score       REAL,
            summary     TEXT,
            raw_json    TEXT,
            model_used  TEXT,
            fetched_at  TEXT DEFAULT (datetime('now')),
            UNIQUE(ticker, as_of_date, source, owner)
        );
        CREATE INDEX IF NOT EXISTS idx_risk_flags_date ON risk_flags(as_of_date, flag);
    """)
    conn.execute(
        """INSERT INTO risk_flags
               (ticker, as_of_date, source, owner, flag, score, summary, raw_json, model_used, fetched_at)
           SELECT ticker, as_of_date, source,
                  CASE WHEN source = 'technical' THEN ? ELSE ? END,
                  flag, score, summary, raw_json, model_used, fetched_at
           FROM risk_flags_legacy_v1""",
        (SHARED_OWNER, LOCAL_OWNER),
    )
    conn.execute("DROP TABLE risk_flags_legacy_v1")


def upsert_risk_flag(
    ticker: str,
    as_of_date: str,
    source: str,
    flag: bool,
    score,
    summary: str,
    raw_json: str,
    model_used: str,
    owner: str = SHARED_OWNER,
) -> None:
    """owner is REQUIRED for source='risk' (whose portfolio this was computed
    against); leave it as SHARED_OWNER only for source='technical'."""
    if source == "risk" and owner == SHARED_OWNER:
        raise ValueError("upsert_risk_flag: a 'risk' row needs a real owner, not the shared sentinel")
    with _db() as c:
        c.execute(
            """
            INSERT INTO risk_flags
                (ticker, as_of_date, source, owner, flag, score, summary, raw_json, model_used)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, as_of_date, source, owner) DO UPDATE SET
                flag       = excluded.flag,
                score      = excluded.score,
                summary    = excluded.summary,
                raw_json   = excluded.raw_json,
                model_used = excluded.model_used,
                fetched_at = datetime('now')
            """,
            (
                ticker.upper(), as_of_date, source, owner, 1 if flag else 0,
                safe_float(score), summary or "", raw_json or "{}", model_used or "",
            ),
        )
        c.commit()


def needs_technical_refresh(ticker: str, as_of_date: str) -> bool:
    """True if the shared technical row is missing for this ticker/date."""
    with _db() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM risk_flags WHERE ticker = ? AND as_of_date = ? "
            "AND source = 'technical' AND owner = ?",
            (ticker.upper(), as_of_date, SHARED_OWNER),
        ).fetchone()
    return (row["n"] if row else 0) < 1


def needs_risk_refresh(ticker: str, as_of_date: str, owner: str) -> bool:
    """True if *owner*'s own risk row is missing for this ticker/date."""
    with _db() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM risk_flags WHERE ticker = ? AND as_of_date = ? "
            "AND source = 'risk' AND owner = ?",
            (ticker.upper(), as_of_date, owner),
        ).fetchone()
    return (row["n"] if row else 0) < 1


def get_active_flags(owner: str, as_of_date: Optional[str] = None) -> list[dict]:
    """
    Return flag=1 rows for as_of_date (default: most recent date present) —
    *owner*'s own 'risk' rows plus every shared 'technical' row. Never
    another owner's 'risk' row.
    """
    with _db() as c:
        if as_of_date is None:
            row = c.execute("SELECT MAX(as_of_date) AS d FROM risk_flags").fetchone()
            as_of_date = row["d"] if row else None
        if not as_of_date:
            return []
        rows = c.execute(
            """
            SELECT ticker, source, score, summary, raw_json, as_of_date
            FROM risk_flags
            WHERE as_of_date = ? AND flag = 1 AND (owner = ? OR owner = ?)
            ORDER BY ticker, source
            """,
            (as_of_date, owner, SHARED_OWNER),
        ).fetchall()
    return [dict(r) for r in rows]


def get_flags_for_ticker(ticker: str, owner: str, as_of_date: Optional[str] = None) -> dict[str, dict]:
    """Return {source: row} for one ticker on as_of_date (default: latest
    available) — *owner*'s own 'risk' row plus the shared 'technical' row."""
    with _db() as c:
        if as_of_date is None:
            row = c.execute(
                "SELECT MAX(as_of_date) AS d FROM risk_flags WHERE ticker = ? AND (owner = ? OR owner = ?)",
                (ticker.upper(), owner, SHARED_OWNER),
            ).fetchone()
            as_of_date = row["d"] if row else None
        if not as_of_date:
            return {}
        rows = c.execute(
            """
            SELECT source, flag, score, summary, raw_json, as_of_date
            FROM risk_flags WHERE ticker = ? AND as_of_date = ? AND (owner = ? OR owner = ?)
            """,
            (ticker.upper(), as_of_date, owner, SHARED_OWNER),
        ).fetchall()
    return {r["source"]: dict(r) for r in rows}
