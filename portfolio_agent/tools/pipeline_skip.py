"""
Pipeline phase skip list — thin re-export over portfolio_agent.tools.restricted_list_db.

Usage:
    from portfolio_agent.tools.pipeline_skip import get_skip_tickers

    restricted = get_skip_tickers("research")   # set of tickers to exclude
    tickers = [t for t in all_tickers if t not in restricted]
"""
from __future__ import annotations

from portfolio_agent.tools.restricted_list_db import (
    get_skip_tickers,
    is_all_phases_skipped,
    log_skip,
)

__all__ = ["get_skip_tickers", "is_all_phases_skipped", "log_skip"]
