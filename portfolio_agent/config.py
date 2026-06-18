"""
Config loader for the portfolio agent.

Reads portfolio.yaml at the project root and returns typed sub-sections.
All public functions are cheap (cached after first read) and safe to call
from any module — they never raise; missing keys fall back to defaults.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_PORTFOLIO_YAML = _PROJECT_ROOT / "config" / "portfolio.yaml"

_DEFAULT_EVENT_DRIVEN: dict = {
    "enabled": True,
    "morning_batch_time": "06:30",
    "intraday_check_times": ["11:00", "13:00", "15:00"],
    "evening_batch_time": "17:30",
    "intraday_severity_threshold": 3,
    "watchlist_severity_threshold": 3,
    "scheduled_cadence": {
        "horizon_5d":   "weekly_monday",
        "horizon_21d":  "weekly_monday",
        "horizon_63d":  "monthly_first_trading_day",
        "horizon_250d": "quarterly_first_trading_day",
    },
    "detectors": {
        "earnings":       True,
        "sec_8k":         True,
        "rating_changes": True,
        "material_news":  True,
        "macro_events":   True,
    },
}

_cache: dict[str, Any] = {}


def _load_yaml() -> dict:
    if "_raw" not in _cache:
        try:
            with open(_PORTFOLIO_YAML) as f:
                _cache["_raw"] = yaml.safe_load(f) or {}
        except FileNotFoundError:
            _cache["_raw"] = {}
    return _cache["_raw"]


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base, returning a new dict."""
    result = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def get_event_driven_config() -> dict:
    """
    Return the event_driven config section from portfolio.yaml,
    merged with defaults so all keys are always present.
    """
    raw = _load_yaml().get("event_driven", {})
    return _deep_merge(_DEFAULT_EVENT_DRIVEN, raw)
