"""
Config loader for the portfolio agent — event-driven scheduling defaults.

Holdings moved to the DB-backed portfolio_agent.tools.holdings_db; this
module no longer reads a YAML config file. The event_driven section was
never actually overridden by anyone (config/portfolio.yaml only ever held
`holdings:`), so these are now just the defaults directly — if per-deployment
overrides are needed again later, they belong in a small settings table, not
a reintroduced config file. (The scheduled cadence now is: engine_settings.horizon_cadence_json.)
"""

from __future__ import annotations

_DEFAULT_EVENT_DRIVEN: dict = {
    "enabled": True,
    "morning_batch_time": "06:30",
    "intraday_check_times": ["11:00", "13:00", "15:00"],
    "evening_batch_time": "17:30",
    "intraday_severity_threshold": 3,
    "watchlist_severity_threshold": 3,
    "detectors": {
        "earnings":       True,
        "sec_8k":         True,
        "rating_changes": True,
        "material_news":  True,
        "macro_events":   True,
    },
}


def get_event_driven_config() -> dict:
    """The event-driven scheduling config: the defaults above, with `scheduled_cadence` read from the database
    (engine_settings.horizon_cadence_json, admin-editable) on every call, so a change applies on the next run. If the
    database cannot be read the seed cadence is used, so a scheduled run never dies on settings."""
    cfg = dict(_DEFAULT_EVENT_DRIVEN)
    from portfolio_agent.tools import engine_settings_db as esdb
    try:
        cfg["scheduled_cadence"] = esdb.scheduled_cadence()
    except Exception:
        cfg["scheduled_cadence"] = {f"horizon_{h}d": r for h, r in esdb.SEED_HORIZON_CADENCE.items()}
    return cfg
