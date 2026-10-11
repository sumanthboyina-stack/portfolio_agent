"""
Is a price out of date? — aware of WHEN each kind of security posts its price.

A stock or ETF has a closing price from 16:00 New York time. A mutual fund (and a target-date fund) is priced ONCE a
day: its NAV is computed after the close and posts in the evening (about 21:00 ET, a few funds later). So at 10:00 on
a trading day, yesterday's close is the correct latest price for a fund — and for a stock too, because today's close
has not happened. A price is stale only when it is older than the latest price that could have been published by now.

Pure: the clock and the trading-day test are parameters.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from portfolio_agent.domain import InstrumentType

NY = ZoneInfo("America/New_York")
MARKET_CLOSE = time(16, 0)
NAV_POSTED = time(21, 0)                                     # mutual-fund NAVs are normally out by this time

_NAV_PRICED = {InstrumentType.MUTUAL_FUND.value, InstrumentType.TARGET_DATE_FUND.value}


def _default_trading_day(d: date) -> bool:
    from portfolio_agent.tools.prediction_db import is_trading_day
    return is_trading_day(d)


def _previous_trading_day(d: date, is_trading_day: Callable[[date], bool]) -> date:
    d -= timedelta(days=1)
    for _ in range(14):                                      # a holiday run is never longer than this
        if is_trading_day(d):
            return d
        d -= timedelta(days=1)
    return d


def posts_nav_once_daily(instrument_type: str | None) -> bool:
    return str(instrument_type or "") in _NAV_PRICED


def expected_price_date(instrument_type: str | None, now: datetime, *,
                        is_trading_day: Callable[[date], bool] | None = None) -> date:
    """The date of the newest price that should exist at `now` for this kind of security."""
    is_trading_day = is_trading_day or _default_trading_day
    local = now.astimezone(NY) if now.tzinfo else now.replace(tzinfo=NY)
    today = local.date()
    posted_at = NAV_POSTED if posts_nav_once_daily(instrument_type) else MARKET_CLOSE
    if is_trading_day(today) and local.time() >= posted_at:
        return today
    return _previous_trading_day(today, is_trading_day)


def is_price_stale(price_date: date | str | None, instrument_type: str | None, now: datetime, *,
                   is_trading_day: Callable[[date], bool] | None = None) -> bool | None:
    """True if the price is older than the latest one that could exist now; None when there is no price date at all
    (unknown — never reported as fresh)."""
    if price_date is None or price_date == "":
        return None
    if isinstance(price_date, str):
        try:
            price_date = date.fromisoformat(price_date[:10])
        except ValueError:
            return None
    return price_date < expected_price_date(instrument_type, now, is_trading_day=is_trading_day)
