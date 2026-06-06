"""
Shared SQLite helpers used by all DB modules.

Provides:
  DB_PATH        — canonical path to portfolio.db
  db_conn()      — @contextmanager; calls optional setup(conn) then yields
  migrate_columns(conn, table, cols)  — generic ALTER TABLE helper
  safe_float(v)  — None-safe float that filters NaN
  to_json(v)     — serialize list/dict/str to JSON string (returns "[]" for empty)
  to_json_list(v)    — serialize to JSON list string
  from_json_list(raw) — parse JSON list string, fallback to CSV split
"""

from __future__ import annotations

import json
import math
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Optional

DB_PATH: Path = Path(__file__).resolve().parents[2] / "data" / "portfolio.db"


@contextmanager
def db_conn(path: Path | None = None, *, setup: Callable[[sqlite3.Connection], None] | None = None):
    """
    Open a SQLite connection to *path* (default: DB_PATH), call *setup(conn)* if
    provided (create schema + migrate), commit, yield, then close.
    """
    p = path or DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=10)
    conn.row_factory = sqlite3.Row
    if setup:
        setup(conn)
    try:
        yield conn
    finally:
        conn.close()


def migrate_columns(
    conn: sqlite3.Connection,
    table: str,
    cols: list[tuple[str, str]],
) -> None:
    """
    Add any missing columns to *table*.

    *cols* is a list of (column_name, column_definition) pairs, e.g.
        [("model_name", "TEXT"), ("updated_at", "TEXT DEFAULT (datetime('now'))")]
    Silently skips columns that already exist.
    """
    existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for col, defn in cols:
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {defn}")


def safe_float(v: Any) -> Optional[float]:
    """Convert *v* to float, returning None for None, non-numeric, or NaN."""
    if v is None:
        return None
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def to_json(v: Any) -> str:
    """
    Serialize *v* to a JSON string.

    - list/dict   → json.dumps
    - falsy       → "[]"
    - valid JSON  → unchanged
    - other str   → json.dumps(str(v))
    """
    if isinstance(v, (list, dict)):
        return json.dumps(v)
    if not v:
        return "[]"
    try:
        json.loads(str(v))
        return str(v)
    except (json.JSONDecodeError, ValueError):
        return json.dumps(str(v))


def to_json_list(v: Any) -> str:
    """
    Serialize *v* to a JSON array string.

    - list        → json.dumps
    - falsy       → "[]"
    - valid JSON  → normalized to list
    - other str   → CSV-split into list
    """
    if isinstance(v, list):
        return json.dumps(v)
    if not v:
        return "[]"
    try:
        parsed = json.loads(str(v))
        return json.dumps(parsed if isinstance(parsed, list) else [str(parsed)])
    except (json.JSONDecodeError, ValueError):
        return json.dumps([s.strip() for s in str(v).split(",") if s.strip()])


def from_json_list(raw: str | None) -> list:
    """
    Parse a JSON list string, falling back to CSV split on decode failure.
    Returns [] for falsy input.
    """
    if not raw:
        return []
    try:
        result = json.loads(raw)
        return result if isinstance(result, list) else [str(result)]
    except (json.JSONDecodeError, ValueError):
        return [s.strip() for s in raw.split(",") if s.strip()]
