"""
Prediction scheduler — decides whether to generate a prediction for a given
ticker/horizon on a given day, and what trigger_type to assign.

Public API:
  get_scheduled_horizons_v2(today, config)
      → list[int] — which horizon_days are "on schedule" today under event-driven cadence.

  should_generate_prediction(ticker, horizon_days, today, config, event_tickers)
      → (bool, str | None) — (should_run, trigger_type)

  get_trigger_type_for_scheduled(horizon_days, today)
      → str — trigger_type label for a calendar-scheduled prediction.

Cadence rules (from config.event_driven.scheduled_cadence):
  "weekly_monday"             — only on calendar Mondays that are trading days
  "monthly_first_trading_day" — first trading day of the calendar month
  "quarterly_first_trading_day" — first trading day of the first month of each quarter
"""

from __future__ import annotations

from datetime import date, timedelta


# ── Calendar helpers ──────────────────────────────────────────────────────────

def _is_trading_day(dt: date) -> bool:
    from portfolio_agent.tools.prediction_db import is_trading_day
    return is_trading_day(dt)


def _next_trading_day(dt: date) -> date:
    d = dt
    while not _is_trading_day(d):
        d += timedelta(days=1)
    return d


def _is_first_trading_day_of_month(today: date) -> bool:
    month_start = today.replace(day=1)
    return _next_trading_day(month_start) == today


def _is_first_trading_day_of_quarter(today: date) -> bool:
    quarter_month = ((today.month - 1) // 3) * 3 + 1
    quarter_start = today.replace(month=quarter_month, day=1)
    return _next_trading_day(quarter_start) == today


_CADENCE_HORIZON_MAP = {
    "horizon_5d":   5,
    "horizon_21d":  21,
    "horizon_63d":  63,
    "horizon_250d": 250,
}

_TRIGGER_TYPE_MAP = {
    "weekly_monday":               {5: "scheduled_weekly",    21: "scheduled_weekly"},
    "monthly_first_trading_day":   {63: "scheduled_monthly"},
    "quarterly_first_trading_day": {250: "scheduled_quarterly"},
}


def get_scheduled_horizons_v2(today: date, config: dict) -> list[int]:
    """
    Return the list of horizon_days that are on schedule today under event-driven cadence.

    Uses config.scheduled_cadence; if missing, falls back to the legacy behaviour
    (5d every day, 21d Monday, 63d first-of-month).
    """
    if not _is_trading_day(today):
        return []

    cadence = config.get("scheduled_cadence", {})
    if not cadence:
        # Legacy fallback
        from portfolio_agent.tools.prediction_db import get_scheduled_horizons
        return get_scheduled_horizons(today)

    horizons: list[int] = []
    for key, rule in cadence.items():
        h = _CADENCE_HORIZON_MAP.get(key)
        if h is None:
            continue
        if rule == "weekly_monday" and today.weekday() == 0:
            horizons.append(h)
        elif rule == "monthly_first_trading_day" and _is_first_trading_day_of_month(today):
            horizons.append(h)
        elif rule == "quarterly_first_trading_day" and _is_first_trading_day_of_quarter(today):
            horizons.append(h)

    return sorted(set(horizons))


def get_trigger_type_for_scheduled(horizon_days: int, today: date, config: dict) -> str:
    """Return the trigger_type string for a calendar-scheduled horizon."""
    cadence = config.get("scheduled_cadence", {})
    for key, rule in cadence.items():
        h = _CADENCE_HORIZON_MAP.get(key)
        if h != horizon_days:
            continue
        if rule == "weekly_monday":
            return "scheduled_weekly"
        if rule == "monthly_first_trading_day":
            return "scheduled_monthly"
        if rule == "quarterly_first_trading_day":
            return "scheduled_quarterly"
    # Fallback
    if today.weekday() == 0:
        return "scheduled_weekly"
    return "scheduled_weekly"


def should_generate_prediction(
    ticker: str,
    horizon_days: int,
    today: date,
    config: dict,
    event_tickers: set[str] | None = None,
) -> tuple[bool, str | None]:
    """
    Decide whether to generate a prediction for (ticker, horizon_days) on today.

    Returns (should_run, trigger_type).

    Logic:
      1. Is today a scheduled day for this horizon?  → (True, "scheduled_*")
      2. Does ticker have a high-severity event today?  → (True, "event_material_news" etc.)
      3. Otherwise → (False, None)
    """
    if not _is_trading_day(today):
        return False, None

    # Check scheduled cadence
    scheduled = get_scheduled_horizons_v2(today, config)
    if horizon_days in scheduled:
        tt = get_trigger_type_for_scheduled(horizon_days, today, config)
        return True, tt

    # Check event trigger — only for 5d/21d (short-term horizons respond to events)
    if horizon_days <= 21 and event_tickers and ticker.upper() in event_tickers:
        return True, "event_material_news"

    return False, None
