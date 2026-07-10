"""
Shared web utilities — database connection, process health.

Imported by all pages to eliminate duplicated plumbing.
Previously each page had its own _conn() / _is_pid_alive() copy.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parents[1]
_DB   = _ROOT / "data" / "portfolio.db"


@contextmanager
def db_conn(path: Optional[Path] = None):
    """
    Open a read-friendly SQLite connection (row_factory=Row).
    Use as: with db_conn() as c: c.execute(...)
    """
    p = path or _DB
    conn = sqlite3.connect(str(p), timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def pid_alive(pid: int) -> bool:
    """Return True if the process with *pid* is still running."""
    if not pid:
        return False
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
        return done == 0
    except ChildProcessError:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    except OSError:
        return False
