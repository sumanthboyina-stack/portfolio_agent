"""The NYSE trading calendar behind is_trading_day / the schedules / evaluation dates: its full-day closures and how weekend holidays are observed."""
from __future__ import annotations

from datetime import date

import pytest

from portfolio_agent.tools.prediction_db import _us_market_holidays, is_trading_day, trading_day_evaluation_date

HOLIDAYS = {
    2024: ["2024-01-01", "2024-01-15", "2024-02-19", "2024-03-29", "2024-05-27", "2024-06-19", "2024-07-04", "2024-09-02", "2024-11-28", "2024-12-25"],
    2025: ["2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26", "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25"],
    2026: ["2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25"],
    # 2027: July 4 is a Sunday (-> Mon 5), Juneteenth and Christmas are Saturdays (-> Fri 18 June, Fri 24 Dec)
    2027: ["2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"],
}


@pytest.mark.parametrize("year,expected", HOLIDAYS.items())
def test_the_nyse_closures_for_a_year(year, expected):
    assert sorted(d.isoformat() for d in _us_market_holidays(year)) == expected


def test_columbus_and_veterans_day_are_trading_days_and_good_friday_is_not():
    assert is_trading_day(date(2026, 10, 12)) and is_trading_day(date(2026, 11, 11)) and is_trading_day(date(2025, 10, 13))
    assert not is_trading_day(date(2026, 4, 3)) and not is_trading_day(date(2025, 4, 18))


def test_new_year_on_a_saturday_is_not_observed_on_the_friday_and_a_sunday_one_moves_to_monday():
    assert is_trading_day(date(2027, 12, 31)) and not _us_market_holidays(2028) & {date(2027, 12, 31), date(2028, 1, 1)}      # Jan 1 2028 is a Saturday
    assert date(2023, 1, 2) in _us_market_holidays(2023)                                                                    # Jan 1 2023 was a Sunday
    assert is_trading_day(date(2021, 12, 31))


def test_juneteenth_only_from_2022():
    assert date(2022, 6, 20) in _us_market_holidays(2022) and not any(d.month == 6 for d in _us_market_holidays(2021))


def test_weekends_and_evaluation_dates_use_the_real_calendar():
    assert not is_trading_day(date(2026, 9, 26)) and is_trading_day(date(2026, 9, 21))
    # 3 trading days after Tue 2026-11-24 skip Thanksgiving and the weekend; 2 after Wed 2026-04-01 skip Good Friday
    assert trading_day_evaluation_date(date(2026, 11, 24), 3) == "2026-11-30"
    assert trading_day_evaluation_date(date(2026, 4, 1), 2) == "2026-04-06"
    # the Columbus Day Monday now counts as a session
    assert trading_day_evaluation_date(date(2026, 10, 9), 1) == "2026-10-12"
