"""
Pipeline phase skip list — reads the pipeline_skip section of restricted_list.yaml.

Usage:
    from portfolio_agent.tools.pipeline_skip import get_skip_tickers

    restricted = get_skip_tickers("research")   # set of tickers to exclude
    tickers = [t for t in all_tickers if t not in restricted]
"""
from __future__ import annotations

from pathlib import Path

import yaml

from portfolio_agent.log import get_logger as _get_logger

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "restricted_list.yaml"
_log = _get_logger("main")
_ALL_PHASES  = frozenset({"news", "research", "fundamentals"})


def _load() -> dict[str, dict]:
    """Return the pipeline_skip mapping (ticker → {phases, reason})."""
    if not _CONFIG_PATH.exists():
        return {}
    try:
        data = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
        return data.get("pipeline_skip") or {}
    except Exception:
        return {}


def get_skip_tickers(phase: str) -> set[str]:
    """Return the set of tickers that should be skipped in *phase*."""
    skip: set[str] = set()
    for ticker, cfg in _load().items():
        phases = cfg.get("phases", [])
        if "all" in phases or phase in phases:
            skip.add(ticker.upper())
    return skip


def is_all_phases_skipped(ticker: str) -> bool:
    """True if the ticker is configured to skip every phase (phases: [all] or all three listed)."""
    cfg = _load().get(ticker.upper(), {})
    phases = set(cfg.get("phases", []))
    return "all" in phases or _ALL_PHASES.issubset(phases)


def log_skip(phase: str, skipped: list[str]) -> None:
    """Log a standardised skip line for the given phase."""
    if not skipped:
        return
    cfg = _load()
    details = []
    for t in sorted(skipped):
        reason = cfg.get(t, {}).get("reason", "restricted_list.yaml")
        details.append(f"{t} ({reason})")
    _log.info(f"  [restricted/{phase}] skipping {len(skipped)}: {', '.join(details)}",
              event_type="restricted_skip", phase=phase,
              tickers=sorted(skipped), count=len(skipped))
