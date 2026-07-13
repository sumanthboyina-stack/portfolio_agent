"""Watchlist storage — read/write config/watchlist.yaml, DB-presence lookups."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import yaml

_ROOT      = Path(__file__).resolve().parents[2]
WATCHLIST_PATH = _ROOT / "config" / "watchlist.yaml"
_DB        = _ROOT / "data" / "portfolio.db"


def load_watchlist() -> dict:
    if not WATCHLIST_PATH.exists():
        return {}
    return yaml.safe_load(WATCHLIST_PATH.read_text()) or {}


def save_watchlist(data: dict) -> None:
    with open(WATCHLIST_PATH, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


def watchlist_db_status(tickers: list[str]) -> dict[str, dict]:
    """For each ticker, check if it has records in fundamentals/research/predictions."""
    if not _DB.exists() or not tickers:
        return {}
    status = {t: {"fund": False, "res": False, "pred": False} for t in tickers}
    ph = ",".join("?" * len(tickers))
    try:
        with sqlite3.connect(str(_DB)) as c:
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


def add_tickers(data: dict, to_add: list[str], cap: int = 70) -> tuple[list[str], str | None]:
    """Add tickers to the watchlist (respecting cap). Returns (added, warning_or_none)."""
    existing = {str(t).upper() for t in data.get("tickers", [])}
    new_unique = [t for t in to_add if t not in existing]
    warning = None
    if new_unique and len(existing) + len(new_unique) > cap:
        available = cap - len(existing)
        new_unique = new_unique[:available]
        warning = f"Watchlist cap ({cap}) — adding only {len(new_unique)}: {', '.join(new_unique)}"
    if new_unique:
        data["tickers"] = list(data.get("tickers", [])) + new_unique
        save_watchlist(data)
    return new_unique, warning


def remove_tickers(data: dict, to_remove: list[str]) -> None:
    remove_set = set(to_remove)
    data["tickers"] = [t for t in data.get("tickers", []) if str(t).upper() not in remove_set]
    save_watchlist(data)
