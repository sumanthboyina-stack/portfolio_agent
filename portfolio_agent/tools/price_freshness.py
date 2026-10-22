"""
Is a price out of date? — aware of WHEN each kind of security posts its price.

A stock or ETF has a closing price from 16:00 New York time. A mutual fund (and a target-date fund) is priced ONCE a
day: its NAV is computed after the close and posts in the evening (about 21:00 ET, a few funds later). So at 10:00 on
a trading day, yesterday's close is the correct latest price for a fund — and for a stock too, because today's close
has not happened. A price is stale only when it is older than the latest price that could have been published by now.

A fund with NO market price (a 401(k) collective trust, stable value or plan cash-reserve fund, priced from the plan's quarterly statement) follows a
different rule: its price is as good as the statement it came from, and stays fine for `statement_price_max_age_days` days after the statement date
(an admin setting, seed 100 ≈ one quarter plus slack), then it "needs the next statement". The market-hours / NAV-posting logic does not apply to it.

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
STATEMENT_PRICED_TYPES = {InstrumentType.CIT.value, InstrumentType.STABLE_VALUE.value}
DEFAULT_STATEMENT_PRICE_MAX_AGE_DAYS = 100        # the seed of portfolio_settings.statement_price_max_age_days; the setting is the live value


def is_statement_priced(instrument_type: str | None, ticker: str | None = None) -> bool:
    """True for a fund with no market price: a synthetic "CIT:..." key, or a CIT / stable value instrument. Its price comes from a statement."""
    from portfolio_agent.domain import is_synthetic_ticker
    return is_synthetic_ticker(ticker) or str(instrument_type or "") in STATEMENT_PRICED_TYPES


def _as_date(d) -> date | None:
    if d is None or d == "":
        return None
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    try:
        return date.fromisoformat(str(d)[:10])
    except ValueError:
        return None


def statement_price_age_days(price_date, now: datetime) -> int | None:
    """Calendar days from the statement's price date to `now`; None when the price has no (readable) date."""
    d = _as_date(price_date)
    if d is None:
        return None
    today = (now.astimezone(NY) if now.tzinfo else now.replace(tzinfo=NY)).date()
    return (today - d).days


def statement_price_due_date(price_date, max_age_days: float | None = None) -> date | None:
    """The last day the statement price counts as fresh (price date + max age); the next statement is due after it."""
    d = _as_date(price_date)
    if d is None:
        return None
    return d + timedelta(days=int(max_age_days if max_age_days is not None else DEFAULT_STATEMENT_PRICE_MAX_AGE_DAYS))


def is_statement_price_stale(price_date, now: datetime, max_age_days: float | None = None) -> bool | None:
    """True once the statement price is older than `max_age_days` (exactly that many days old is still fine); None when it has no date (unknown,
    never reported as fresh)."""
    age = statement_price_age_days(price_date, now)
    if age is None:
        return None
    return age > float(max_age_days if max_age_days is not None else DEFAULT_STATEMENT_PRICE_MAX_AGE_DAYS)


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
                   is_trading_day: Callable[[date], bool] | None = None, ticker: str | None = None,
                   statement_max_age_days: float | None = None) -> bool | None:
    """True if the price is older than the latest one that could exist now; None when there is no price date at all
    (unknown — never reported as fresh). A fund priced from a statement (see is_statement_priced; pass its `ticker`) is judged by
    is_statement_price_stale with `statement_max_age_days` instead of the market-hours rule."""
    if is_statement_priced(instrument_type, ticker):
        return is_statement_price_stale(price_date, now, statement_max_age_days)
    if price_date is None or price_date == "":
        return None
    if isinstance(price_date, str):
        try:
            price_date = date.fromisoformat(price_date[:10])
        except ValueError:
            return None
    return price_date < expected_price_date(instrument_type, now, is_trading_day=is_trading_day)
