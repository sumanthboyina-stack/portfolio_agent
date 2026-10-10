"""
Instrument reference data for MARKET-ONLY code — what kind of security a ticker is.

An instrument row (type, asset class, leverage) describes the security itself: it has no owner, no account, no
quantity, no cost. That is why code that must never read a person's holdings (tools.market_context, the shared
forecast path) may read it, through this module and ONLY this module: it exposes the instruments table and nothing
else, so there is no way to reach positions, accounts or cash from here.
"""
from __future__ import annotations

from portfolio_agent.tools import holdings_db as _instruments


def get_instrument_profiles(tickers: list[str]) -> dict[str, dict]:
    """{ticker: {instrument_type, asset_class, leverage_factor}} for tickers that have an instrument row."""
    return _instruments.get_instrument_profiles(tickers)
