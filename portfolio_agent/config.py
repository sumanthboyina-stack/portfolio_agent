"""
Config loader for the portfolio agent — event-driven scheduling defaults.

Holdings moved to the DB-backed portfolio_agent.tools.holdings_db; this
module no longer reads a YAML config file. The event_driven section was
never actually overridden by anyone (config/portfolio.yaml only ever held
`holdings:`), so these are now just the defaults directly — if per-deployment
overrides are needed again later, they belong in a small settings table, not
a reintroduced config file.
"""

from __future__ import annotations

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


def get_event_driven_config() -> dict:
    """Return the event-driven scheduling config (currently just the defaults)."""
    return dict(_DEFAULT_EVENT_DRIVEN)
