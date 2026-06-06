"""
Checkpoint system for resumable daily batch runs.

When a run is interrupted by an API credit error (or any fatal API error),
the remaining ticker queue is saved here so the next run can continue where
it left off before processing fresh tickers.

File: data/batch_checkpoint.json
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Optional

_CHECKPOINT_PATH = Path(__file__).resolve().parents[2] / "data" / "batch_checkpoint.json"


def load_checkpoint() -> Optional[dict]:
    """
    Return the saved checkpoint dict, or None if no checkpoint exists.

    Schema:
        {
          "saved_at": "ISO timestamp",
          "run_date": "YYYY-MM-DD",
          "fundamentals": {
              "completed": [...],
              "pending":   [...],   ← tickers not yet processed
              "failed":    [...],
              "error":     "..."
          },
          "news": { ... same shape ... }
        }
    """
    if not _CHECKPOINT_PATH.exists():
        return None
    try:
        return json.loads(_CHECKPOINT_PATH.read_text())
    except Exception:
        return None


def save_checkpoint(
    run_date: str,
    phase: str,
    completed: list[str],
    pending: list[str],
    failed: list[str],
    error: str = "",
) -> None:
    """
    Persist the current progress for *phase* ("fundamentals" or "news").

    Merges into any existing checkpoint so both phases can be tracked independently.
    """
    _CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)

    existing = load_checkpoint() or {}
    existing.update({
        "saved_at": datetime.now().isoformat(),
        "run_date": run_date,
    })
    existing[phase] = {
        "completed": completed,
        "pending":   pending,
        "failed":    failed,
        "error":     error,
    }
    _CHECKPOINT_PATH.write_text(json.dumps(existing, indent=2))


def clear_phase(phase: str) -> None:
    """Mark *phase* as fully completed in the checkpoint (empties its pending list)."""
    existing = load_checkpoint()
    if not existing:
        return
    if phase in existing:
        existing[phase]["pending"] = []
        existing[phase]["error"]   = ""
    _CHECKPOINT_PATH.write_text(json.dumps(existing, indent=2))


def clear_checkpoint() -> None:
    """Delete the checkpoint file entirely (called when both phases succeed)."""
    if _CHECKPOINT_PATH.exists():
        _CHECKPOINT_PATH.unlink()


def pending_from_yesterday(phase: str) -> list[str]:
    """
    Return pending tickers saved from a *previous* run for *phase*.

    Returns an empty list if the checkpoint is from today (already retried)
    or if there is no checkpoint.
    """
    cp = load_checkpoint()
    if not cp:
        return []
    run_date = cp.get("run_date", "")
    if run_date == date.today().isoformat():
        # Already retried today — don't loop forever
        return []
    phase_data = cp.get(phase, {})
    return list(phase_data.get("pending", []))
