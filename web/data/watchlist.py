"""Watchlist accessors for the web app — thin re-export over portfolio_agent.tools.watchlist_db."""

from __future__ import annotations

from portfolio_agent.tools.watchlist_db import (
    add_tickers,
    load_watchlist_tickers,
    remove_tickers,
    watchlist_db_status,
)

__all__ = ["load_watchlist_tickers", "watchlist_db_status", "add_tickers", "remove_tickers"]
