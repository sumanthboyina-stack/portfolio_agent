"""
Watchlist database — manually curated tickers tracked for daily pipeline coverage.

Table (watchlist):
  ticker, added_at, owner

The watchlist is manual-only: tickers are added/removed via the Watchlist
Manager screen or the Opportunity Engine's "Add to Watchlist" button.
Nothing in the pipeline auto-promotes tickers here.

owner is backfilled to LOCAL_OWNER on any pre-existing row (idempotent). The
existing global functions below are unchanged (still what the pipeline and UI
use); load_watchlist_tickers_for(ctx) is the new authorized read path — see
the Phase 2 report's inventory for what still reads the unscoped path.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import yaml

from portfolio_agent.domain import LOCAL_OWNER
from portfolio_agent.tools.db import DB_PATH, db_conn, migrate_columns

_LEGACY_YAML = Path(__file__).resolve().parents[2] / "config" / "watchlist.yaml"


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
        CREATE TABLE IF NOT EXISTS watchlist (
            ticker   TEXT PRIMARY KEY,
            added_at TEXT NOT NULL,
            owner    TEXT
        )
    """)


def _migrate(conn: sqlite3.Connection) -> None:
    """
    One-time seed from the legacy config/watchlist.yaml — runs at most once,
    ever. The DB is the source of truth from here on: after a successful seed
    the YAML is renamed to `.migrated` so a later run never re-reads it.
    Gating this on "table is empty" instead would silently resurrect tickers
    from the stale YAML the moment someone removed the last one via the UI.

    Also backfills owner on any pre-existing row to LOCAL_OWNER — idempotent,
    a no-op once set.
    """
    migrate_columns(conn, "watchlist", [("owner", "TEXT")])
    conn.execute("UPDATE watchlist SET owner = ? WHERE owner IS NULL", (LOCAL_OWNER,))
    # One-time rename: LOCAL_OWNER was "local" before this deployment's single
    # user was named "sumanth_b" ahead of registration/login. Idempotent.
    conn.execute("UPDATE watchlist SET owner = ? WHERE owner = 'local'", (LOCAL_OWNER,))
    if not _LEGACY_YAML.exists():
        return
    try:
        data = yaml.safe_load(_LEGACY_YAML.read_text()) or {}
    except Exception:
        return
    tickers = {str(t).upper() for t in data.get("tickers", [])}
    now = datetime.now(timezone.utc).isoformat()
    if tickers:
        conn.executemany(
            "INSERT OR IGNORE INTO watchlist (ticker, added_at, owner) VALUES (?, ?, ?)",
            [(t, now, LOCAL_OWNER) for t in sorted(tickers)],
        )
    conn.commit()
    try:
        _LEGACY_YAML.rename(_LEGACY_YAML.with_name(_LEGACY_YAML.name + ".migrated"))
    except OSError:
        pass  # migration itself already succeeded; a rename failure just means we re-check next time


def load_watchlist_tickers() -> list[str]:
    """Return all watchlist tickers, sorted. Unscoped — see load_watchlist_tickers_for."""
    with _db() as conn:
        rows = conn.execute("SELECT ticker FROM watchlist ORDER BY ticker").fetchall()
    return [r[0] for r in rows]


def load_watchlist_tickers_for(ctx) -> list[str]:
    """Authorized read: only the tickers owned by ctx's verified identity."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT ticker FROM watchlist WHERE owner = ? ORDER BY ticker", (ctx.actor,)
        ).fetchall()
    return [r[0] for r in rows]


def add_tickers(to_add: list[str], cap: int = 70) -> tuple[list[str], str | None]:
    """Add tickers to the watchlist (respecting cap). Returns (added, warning_or_none)."""
    to_add = [str(t).upper() for t in to_add]
    with _db() as conn:
        existing = {r[0] for r in conn.execute("SELECT ticker FROM watchlist").fetchall()}
        new_unique = [t for t in dict.fromkeys(to_add) if t not in existing]
        warning = None
        if new_unique and len(existing) + len(new_unique) > cap:
            available = cap - len(existing)
            new_unique = new_unique[:available]
            warning = f"Watchlist cap ({cap}) — adding only {len(new_unique)}: {', '.join(new_unique)}"
        if new_unique:
            now = datetime.now(timezone.utc).isoformat()
            conn.executemany(
                "INSERT OR IGNORE INTO watchlist (ticker, added_at, owner) VALUES (?, ?, ?)",
                [(t, now, LOCAL_OWNER) for t in new_unique],
            )
            conn.commit()
    return new_unique, warning


def remove_tickers(to_remove: list[str]) -> None:
    remove_set = {str(t).upper() for t in to_remove}
    if not remove_set:
        return
    with _db() as conn:
        ph = ",".join("?" * len(remove_set))
        conn.execute(f"DELETE FROM watchlist WHERE ticker IN ({ph})", list(remove_set))
        conn.commit()


def watchlist_db_status(tickers: list[str]) -> dict[str, dict]:
    """For each ticker, check if it has records in fundamentals/research/predictions."""
    if not DB_PATH.exists() or not tickers:
        return {}
    status = {t: {"fund": False, "res": False, "pred": False} for t in tickers}
    ph = ",".join("?" * len(tickers))
    try:
        with sqlite3.connect(str(DB_PATH)) as c:
            for t in c.execute(
                f"SELECT DISTINCT ticker FROM fundamentals WHERE ticker IN ({ph})", tickers
            ).fetchall():
                if t[0] in status:
                    status[t[0]]["fund"] = True
            for t in c.execute(
                f"SELECT DISTINCT ticker FROM research WHERE as_of_date IS NOT NULL AND ticker IN ({ph})", tickers
            ).fetchall():
                if t[0] in status:
                    status[t[0]]["res"] = True
            for t in c.execute(
                f"SELECT DISTINCT ticker FROM predictions WHERE ticker IN ({ph})", tickers
            ).fetchall():
                if t[0] in status:
                    status[t[0]]["pred"] = True
    except Exception:
        pass
    return status
