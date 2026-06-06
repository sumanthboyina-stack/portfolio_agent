"""SQLite-backed pipeline progress tracker — written by main.py, read by the schedule page."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_DB = Path(__file__).resolve().parents[2] / "data" / "portfolio.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(_DB), timeout=10, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def ensure_tables() -> None:
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS pipeline_runs (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id        TEXT UNIQUE NOT NULL,
                job_type      TEXT NOT NULL,
                pid           INTEGER,
                log_file      TEXT,
                started_at    TEXT NOT NULL,
                finished_at   TEXT,
                status        TEXT DEFAULT 'running',
                total_tickers INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS pipeline_phase_progress (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id         TEXT NOT NULL,
                phase          TEXT NOT NULL,
                status         TEXT DEFAULT 'pending',
                total          INTEGER DEFAULT 0,
                completed      INTEGER DEFAULT 0,
                failed         INTEGER DEFAULT 0,
                current_ticker TEXT DEFAULT '',
                note           TEXT DEFAULT '',
                started_at     TEXT,
                finished_at    TEXT,
                updated_at     TEXT NOT NULL,
                UNIQUE(run_id, phase)
            );
        """)


class PipelineProgressTracker:
    """Thread-safe pipeline progress tracker backed by portfolio.db."""

    def __init__(self, run_id: str, job_type: str, pid: int = 0, log_file: str = "") -> None:
        ensure_tables()
        self.run_id = run_id
        with _get_conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO pipeline_runs
                   (run_id, job_type, pid, log_file, started_at, status, total_tickers)
                   VALUES (?, ?, ?, ?, ?, 'running', 0)""",
                (run_id, job_type, pid or os.getpid(), log_file, _now()),
            )

    def set_total(self, total: int) -> None:
        with _get_conn() as conn:
            conn.execute(
                "UPDATE pipeline_runs SET total_tickers = ? WHERE run_id = ?",
                (total, self.run_id),
            )

    def start_phase(self, phase: str, total: int = 0) -> None:
        with _get_conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO pipeline_phase_progress
                   (run_id, phase, status, total, completed, failed,
                    current_ticker, note, started_at, updated_at)
                   VALUES (?, ?, 'running', ?, 0, 0, '', '', ?, ?)""",
                (self.run_id, phase, total, _now(), _now()),
            )

    def tick(self, phase: str, completed: int, total: int,
             ticker: str = "", note: str = "") -> None:
        with _get_conn() as conn:
            conn.execute(
                """INSERT INTO pipeline_phase_progress
                   (run_id, phase, status, total, completed, current_ticker, note, updated_at)
                   VALUES (?, ?, 'running', ?, ?, ?, ?, ?)
                   ON CONFLICT(run_id, phase) DO UPDATE SET
                     status='running', total=excluded.total,
                     completed=excluded.completed,
                     current_ticker=excluded.current_ticker,
                     note=excluded.note,
                     updated_at=excluded.updated_at""",
                (self.run_id, phase, total, completed, ticker, note, _now()),
            )

    def finish_phase(self, phase: str, completed: int, failed: int = 0,
                     note: str = "") -> None:
        with _get_conn() as conn:
            conn.execute(
                """INSERT INTO pipeline_phase_progress
                   (run_id, phase, status, completed, failed, note, finished_at, updated_at)
                   VALUES (?, ?, 'done', ?, ?, ?, ?, ?)
                   ON CONFLICT(run_id, phase) DO UPDATE SET
                     status='done', completed=excluded.completed,
                     failed=excluded.failed, note=excluded.note,
                     finished_at=excluded.finished_at,
                     updated_at=excluded.updated_at""",
                (self.run_id, phase, completed, failed, note, _now(), _now()),
            )

    def error_phase(self, phase: str, completed: int = 0, failed: int = 0,
                    note: str = "") -> None:
        with _get_conn() as conn:
            conn.execute(
                """INSERT INTO pipeline_phase_progress
                   (run_id, phase, status, completed, failed, note, finished_at, updated_at)
                   VALUES (?, ?, 'error', ?, ?, ?, ?, ?)
                   ON CONFLICT(run_id, phase) DO UPDATE SET
                     status='error', completed=excluded.completed,
                     failed=excluded.failed, note=excluded.note,
                     finished_at=excluded.finished_at,
                     updated_at=excluded.updated_at""",
                (self.run_id, phase, completed, failed, note, _now(), _now()),
            )

    def finish_run(self, status: str = "completed") -> None:
        with _get_conn() as conn:
            conn.execute(
                "UPDATE pipeline_runs SET status = ?, finished_at = ? WHERE run_id = ?",
                (status, _now(), self.run_id),
            )
