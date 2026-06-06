"""
Structured logging for the portfolio agent.

Two handlers on every portfolio_agent.* logger:
  • PlainHandler  → sys.stdout   (message text only — preserves existing .log capture
                                   format so the schedule page regex parsers keep working)
  • JSONHandler   → logs/portfolio_agent.jsonl  (appendable structured file)

Every JSON record contains: ts, level, logger, phase, event_type, plus any
contextual fields passed by the caller.

Usage
-----
    from portfolio_agent.log import get_logger

    log = get_logger("news")

    log.info("Fetching articles",      event_type="fetch_start",    ticker_count=78)
    log.info("Batch 1/9 complete",     event_type="batch_end",      batch=1, total=9,
                                                                     saved=8, failed=0)
    log.warning("LLM fallback",        event_type="llm_fallback",   model="groq-70b",
                                                                     provider="groq",
                                                                     error_type="RateLimitError")
    log.error("All models exhausted",  event_type="checkpoint_save", remaining=37)

Event-type convention
---------------------
  phase_start / phase_end
  batch_start / batch_end
  fetch_start / fetch_end
  llm_call_start / llm_call_success / llm_call_error / llm_fallback
  db_write
  ticker_progress
  filter_summary
  restricted_skip
  checkpoint_save
  rate_limit
  resume
  summary / warning / error / info

Queryable with jq
-----------------
  jq 'select(.phase=="news") | select(.level=="ERROR")' logs/portfolio_agent.jsonl
  jq 'select(.event_type=="llm_call_error") | {ts,model,provider,error_type}' logs/portfolio_agent.jsonl
  jq 'select(.event_type=="checkpoint_save")' logs/portfolio_agent.jsonl
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── Formatters ────────────────────────────────────────────────────────────────

class _PlainFormatter(logging.Formatter):
    """Emit [HH:MM:SS] local-time timestamp + message — readable in .log files."""
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.now().strftime("%H:%M:%S")
        return f"[{ts}] {record.getMessage()}"


class _JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        obj: dict[str, Any] = {
            "ts":     datetime.now(timezone.utc).isoformat(),
            "level":  record.levelname,
            "logger": record.name,
            "msg":    record.getMessage(),
        }
        if hasattr(record, "extra_fields"):
            obj.update(record.extra_fields)
        return json.dumps(obj)


# ── Global setup (idempotent) ─────────────────────────────────────────────────

_JSONL_PATH  = Path(__file__).resolve().parents[1] / "logs" / "portfolio_agent.jsonl"
_initialized = False


def _ensure_setup() -> None:
    global _initialized
    if _initialized:
        return
    root = logging.getLogger("portfolio_agent")
    if root.handlers:       # already configured (tests, re-import)
        _initialized = True
        return

    root.setLevel(logging.DEBUG)
    root.propagate = False  # don't bubble up to the root Python logger

    # 1. Plain stdout (text captured to .log files by shell redirect / schedule page)
    plain_h = logging.StreamHandler(sys.stdout)
    plain_h.setLevel(logging.DEBUG)
    plain_h.setFormatter(_PlainFormatter())
    root.addHandler(plain_h)

    # 2. JSON file (structured, appendable, queryable with jq)
    _JSONL_PATH.parent.mkdir(parents=True, exist_ok=True)
    json_h = logging.FileHandler(_JSONL_PATH, encoding="utf-8")
    json_h.setLevel(logging.DEBUG)
    json_h.setFormatter(_JSONFormatter())
    root.addHandler(json_h)

    _initialized = True


# ── PhaseLogger ───────────────────────────────────────────────────────────────

class PhaseLogger:
    """
    Wrapper around logging.Logger that auto-injects *phase* into every record.

    Every call accepts *msg* (the human-readable string that was formerly
    passed to print()) plus keyword fields that go into the JSON record.
    *event_type* is mandatory for structure; callers should always supply it.
    """

    __slots__ = ("_phase", "_log")

    def __init__(self, phase: str) -> None:
        _ensure_setup()
        self._phase = phase
        self._log   = logging.getLogger(f"portfolio_agent.{phase}")

    def _emit(self, level: int, msg: str, event_type: str, **fields: Any) -> None:
        self._log.log(
            level, msg,
            extra={"extra_fields": {"phase": self._phase, "event_type": event_type, **fields}},
        )

    def debug(self, msg: str, *, event_type: str = "debug", **fields: Any) -> None:
        self._emit(logging.DEBUG, msg, event_type, **fields)

    def info(self, msg: str, *, event_type: str = "info", **fields: Any) -> None:
        self._emit(logging.INFO, msg, event_type, **fields)

    def warning(self, msg: str, *, event_type: str = "warning", **fields: Any) -> None:
        self._emit(logging.WARNING, msg, event_type, **fields)

    def error(self, msg: str, *, event_type: str = "error", **fields: Any) -> None:
        self._emit(logging.ERROR, msg, event_type, **fields)


def get_logger(phase: str) -> PhaseLogger:
    """Return a PhaseLogger scoped to *phase*."""
    return PhaseLogger(phase)
