"""
Watchlist loading helper — reads the watchlist table in data/portfolio.db.

The watchlist is manual-only: tickers are added/removed via the Watchlist
Manager screen or the Opportunity Engine's "Add to Watchlist" button
(portfolio_agent/tools/watchlist_db.py). Nothing in the pipeline auto-promotes
tickers here.
"""

from __future__ import annotations

import json

from portfolio_agent.log import get_logger as _get_logger


def load_all_tickers(
    extra_tickers: list[str] | None,
) -> tuple[list[str], list[str], list[str], list[str]]:
    """
    Merge extra, portfolio, market-trending, and watchlist tickers.
    Returns (all_tickers, watchlist, portfolio_tickers, trending_from_news).
    """
    log = _get_logger("main")
    from portfolio_agent.tools.news_sources import get_trending_tickers
    from portfolio_agent.tools.watchlist_db import load_watchlist_tickers
    from portfolio_agent.tools.holdings_db import get_portfolio_tickers

    watchlist = load_watchlist_tickers()
    portfolio_tickers = get_portfolio_tickers()

    log.info("  Scanning market news for trending tickers...", event_type="fetch_start")
    try:
        trending_from_news = [
            t.upper()
            for t in json.loads(get_trending_tickers(30)).get("tickers", [])
        ]
    except Exception as exc:
        log.warning(f"  [warn] Could not extract trending tickers: {exc}", event_type="warning")
        trending_from_news = []

    # Priority: portfolio holdings first (you own these), then explicit extras,
    # then watchlist, then trending.  dict.fromkeys deduplicates while preserving order.
    all_tickers = list(dict.fromkeys(
        portfolio_tickers + (extra_tickers or []) + watchlist + trending_from_news
    ))
    return all_tickers, watchlist, portfolio_tickers, trending_from_news
