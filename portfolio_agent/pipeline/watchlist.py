"""
Watchlist management helpers — read/write config/watchlist.yaml.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from portfolio_agent.log import get_logger as _get_logger

_WATCHLIST_MAX = 70


def update_watchlist(watchlist_path: Path, new_tickers: list[str]) -> None:
    """
    Append genuinely new tickers to watchlist.yaml, capped at _WATCHLIST_MAX total.

    Trending tickers are added at the end, so the base lists always take priority.
    """
    log = _get_logger("main")
    with open(watchlist_path) as f:
        data = yaml.safe_load(f) or {}
    current = list(data.get("tickers", []))
    existing = {str(t).upper() for t in current}
    to_add = [t for t in new_tickers if t not in existing]
    if not to_add:
        return

    available_slots = max(0, _WATCHLIST_MAX - len(current))
    if available_slots == 0:
        log.warning(
            f"  Watchlist at cap ({_WATCHLIST_MAX}) — skipping {len(to_add)} trending ticker(s)",
            event_type="warning",
        )
        return

    adding  = to_add[:available_slots]
    dropped = to_add[available_slots:]

    data["tickers"] = current + adding
    with open(watchlist_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    log.info(
        f"  Watchlist updated — added {len(adding)} ticker(s): "
        f"{', '.join(adding[:15])}{'...' if len(adding) > 15 else ''}",
        event_type="info",
    )
    if dropped:
        log.info(
            f"  Watchlist cap reached — dropped {len(dropped)} ticker(s): "
            f"{', '.join(dropped[:10])}",
            event_type="info",
        )


def load_all_tickers(
    extra_tickers: list[str] | None,
    watchlist_path: Path,
    portfolio_path: Path,
) -> tuple[list[str], list[str], list[str], list[str]]:
    """
    Merge extra, portfolio, market-trending, and watchlist tickers.
    Returns (all_tickers, watchlist, portfolio_tickers, trending_from_news).
    """
    log = _get_logger("main")
    from portfolio_agent.tools.news_sources import get_trending_tickers

    watchlist = [
        str(t).upper()
        for t in (yaml.safe_load(watchlist_path.read_text()) or {}).get("tickers", [])
    ]

    portfolio_tickers: list[str] = []
    if portfolio_path.exists():
        port_data = yaml.safe_load(portfolio_path.read_text()) or {}
        portfolio_tickers = [
            h["ticker"].upper()
            for h in port_data.get("holdings", [])
            if "ticker" in h
        ]

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
