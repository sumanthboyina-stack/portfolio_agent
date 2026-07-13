"""
Daily Risk + Technical specialist flags.

Decoupled from the prediction weight engine on purpose (Approach B — see
project decision): Technical/Risk run alongside APEX and write here, not into
`predictions`. No schema change to predictions, no re-weighting, no
disruption to the composite-score history the Validation/Track Record
tooling analyzes. `flag=1` rows are what the dashboard's Attention Queue
surfaces proactively.

One row per (ticker, as_of_date, source); source is 'risk' or 'technical'.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Optional

from portfolio_agent.tools.db import db_conn, safe_float


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
            flag        INTEGER NOT NULL DEFAULT 0,
            score       REAL,
            summary     TEXT,
            raw_json    TEXT,
            model_used  TEXT,
            fetched_at  TEXT DEFAULT (datetime('now')),
            UNIQUE(ticker, as_of_date, source)
        );

        CREATE INDEX IF NOT EXISTS idx_risk_flags_date
            ON risk_flags(as_of_date, flag);
    """)


def upsert_risk_flag(
    ticker: str,
    as_of_date: str,
    source: str,
    flag: bool,
    score,
    summary: str,
    raw_json: str,
    model_used: str,
) -> None:
    with _db() as c:
        c.execute(
            """
            INSERT INTO risk_flags
                (ticker, as_of_date, source, flag, score, summary, raw_json, model_used)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, as_of_date, source) DO UPDATE SET
                flag       = excluded.flag,
                score      = excluded.score,
                summary    = excluded.summary,
                raw_json   = excluded.raw_json,
                model_used = excluded.model_used,
                fetched_at = datetime('now')
            """,
            (
                ticker.upper(), as_of_date, source, 1 if flag else 0,
                safe_float(score), summary or "", raw_json or "{}", model_used or "",
            ),
        )
        c.commit()


def needs_refresh(ticker: str, as_of_date: str) -> bool:
    """True if either the risk or technical flag is missing for this ticker/date."""
    with _db() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM risk_flags WHERE ticker = ? AND as_of_date = ?",
            (ticker.upper(), as_of_date),
        ).fetchone()
    return (row["n"] if row else 0) < 2


def get_active_flags(as_of_date: Optional[str] = None) -> list[dict]:
    """Return all flag=1 rows for as_of_date (default: most recent date present)."""
    with _db() as c:
        if as_of_date is None:
            row = c.execute("SELECT MAX(as_of_date) AS d FROM risk_flags").fetchone()
            as_of_date = row["d"] if row else None
        if not as_of_date:
            return []
        rows = c.execute(
            """
            SELECT ticker, source, score, summary, raw_json, as_of_date
            FROM risk_flags WHERE as_of_date = ? AND flag = 1
            ORDER BY ticker, source
            """,
            (as_of_date,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_flags_for_ticker(ticker: str, as_of_date: Optional[str] = None) -> dict[str, dict]:
    """Return {source: row} for one ticker on as_of_date (default: latest available)."""
    with _db() as c:
        if as_of_date is None:
            row = c.execute(
                "SELECT MAX(as_of_date) AS d FROM risk_flags WHERE ticker = ?",
                (ticker.upper(),),
            ).fetchone()
            as_of_date = row["d"] if row else None
        if not as_of_date:
            return {}
        rows = c.execute(
            """
            SELECT source, flag, score, summary, raw_json, as_of_date
            FROM risk_flags WHERE ticker = ? AND as_of_date = ?
            """,
            (ticker.upper(), as_of_date),
        ).fetchall()
    return {r["source"]: dict(r) for r in rows}
