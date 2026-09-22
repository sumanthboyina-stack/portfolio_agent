"""News and calendar tools — RSS feeds, Yahoo Finance news, Finnhub news, earnings calendar."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import yfinance as yf

from portfolio_agent.tools.finnhub_news import (
    fetch_company_news as _finnhub_company_news,
    fetch_news_sentiment as _finnhub_sentiment,
    merge_articles as _merge_articles,
)

_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"

_RSS_FEEDS = {
    "reuters_business": "https://feeds.reuters.com/reuters/businessNews",
    "seeking_alpha":    "https://seekingalpha.com/market_currents.xml",
    "marketwatch":      "http://feeds.marketwatch.com/marketwatch/topstories",
    "yahoo_finance":    "https://finance.yahoo.com/news/rssindex",
    "cnbc":             "https://www.cnbc.com/id/100003114/device/rss/rss.html",
}


def get_ticker_news(ticker: str, limit: int = 10) -> str:
    """
    Return the most recent news articles for a ticker from Yahoo Finance.

    Args:
        ticker: Stock ticker symbol.
        limit:  Maximum number of articles to return.

    Returns:
        JSON string with a list of {title, publisher, link, published_at} articles.
    """
    t = yf.Ticker(ticker)
    news = t.news or []
    articles = []
    for item in news[:limit]:
        articles.append({
            "title": item.get("title"),
            "publisher": item.get("publisher"),
            "link": item.get("link"),
            "published_at": datetime.fromtimestamp(
                item.get("providerPublishTime", 0), tz=timezone.utc
            ).isoformat() if item.get("providerPublishTime") else None,
        })
    return json.dumps({"ticker": ticker.upper(), "articles": articles})


def get_ticker_news_merged(ticker: str, limit: int = 15, max_age_hours: int = 48) -> str:
    """
    Return the most recent news for a ticker merged from Yahoo Finance AND Finnhub.

    Finnhub aggregates Reuters, Bloomberg, Seeking Alpha, and MarketWatch, so this
    gives significantly more coverage than Yahoo alone. Duplicate articles are removed.
    Finnhub articles include a full summary, not just a headline.

    Args:
        ticker:        Stock ticker symbol.
        limit:         Maximum number of merged articles to return.
        max_age_hours: Only return articles published within this window (default 48h).

    Returns:
        JSON string with {ticker, total, sources:{yahoo,finnhub}, articles:[...]}.
        Each article: {title, publisher, published_at, url, summary, _source}.
    """
    ticker = ticker.upper()

    # Yahoo Finance
    try:
        t = yf.Ticker(ticker)
        raw_yahoo = t.news or []
        cutoff_ts = datetime.now(timezone.utc).timestamp() - max_age_hours * 3600
        yahoo_articles = []
        for item in raw_yahoo:
            ts = item.get("providerPublishTime", 0)
            if ts >= cutoff_ts:
                yahoo_articles.append({
                    "title":        (item.get("title") or "").strip(),
                    "publisher":    item.get("publisher", ""),
                    "published_at": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
                    "url":          item.get("link", ""),
                    "summary":      "",
                    "_source":      "yahoo",
                })
    except Exception:
        yahoo_articles = []

    # Finnhub
    finnhub_articles = _finnhub_company_news(ticker, max_age_hours=max_age_hours)

    merged = _merge_articles(yahoo_articles, finnhub_articles)[:limit]

    return json.dumps({
        "ticker":  ticker,
        "total":   len(merged),
        "sources": {
            "yahoo":   len(yahoo_articles),
            "finnhub": len(finnhub_articles),
        },
        "articles": merged,
    })


def get_finnhub_sentiment(ticker: str) -> str:
    """
    Return aggregate news sentiment for a ticker from Finnhub.

    Finnhub's sentiment service tracks article volume and bullish/bearish ratio
    over the past week, giving a quantitative signal beyond keyword matching.

    Args:
        ticker: Stock ticker symbol.

    Returns:
        JSON string with {ticker, bullish_pct, bearish_pct, articles_last_week,
        company_score, signal}.
        signal: "BULLISH" / "BEARISH" / "NEUTRAL" based on bullish_pct threshold.
    """
    ticker = ticker.upper()
    data = _finnhub_sentiment(ticker)
    if not data:
        return json.dumps({"ticker": ticker, "error": "unavailable"})

    bullish = data["bullish_pct"]
    bearish = data["bearish_pct"]
    if bullish >= 60:
        signal = "BULLISH"
    elif bearish >= 60:
        signal = "BEARISH"
    else:
        signal = "NEUTRAL"

    return json.dumps({
        "ticker":             ticker,
        "bullish_pct":        bullish,
        "bearish_pct":        bearish,
        "articles_last_week": data["articles_last_week"],
        "company_score":      data["company_score"],
        "signal":             signal,
    })


def get_market_news(limit: int = 20) -> str:
    """
    Return recent financial headlines aggregated across all RSS feeds.

    Pulls from Reuters, Yahoo Finance, CNBC, MarketWatch, and Seeking Alpha,
    deduplicates by title, and returns the most recent *limit* headlines.

    Args:
        limit: Total headlines to return across all feeds.

    Returns:
        JSON string with {total, articles:[{title, source, link, published, summary}]}.
    """
    per_feed = max(4, limit // len(_RSS_FEEDS) + 2)
    seen: set[str] = set()
    all_entries: list[dict] = []

    for feed_name, url in _RSS_FEEDS.items():
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:per_feed]:
                title = (entry.get("title") or "").strip()
                if not title or title in seen:
                    continue
                seen.add(title)
                all_entries.append({
                    "title": title,
                    "source": feed_name,
                    "link": entry.get("link"),
                    "published": entry.get("published"),
                    "summary": (entry.get("summary") or "")[:300],
                })
        except Exception:
            continue

    return json.dumps({"total": len(all_entries[:limit]), "articles": all_entries[:limit]})


def get_sp100_news(tickers: list[str] | None = None, limit_per_ticker: int = 3) -> str:
    """
    Return recent news for a list of tickers (defaults to the watchlist).

    Fetches up to *limit_per_ticker* headlines per ticker using Yahoo Finance.
    Cap at 30 tickers per call to avoid excessive API usage.

    Args:
        tickers:           List of ticker symbols; None → load from the watchlist.
        limit_per_ticker:  Headlines to return per ticker (default 3).

    Returns:
        JSON string with {tickers_fetched, results:{TICKER:[{title, published_at}]}}.
    """
    if tickers is None:
        from portfolio_agent.tools.watchlist_db import load_watchlist_tickers
        tickers = load_watchlist_tickers()
        if not tickers:
            import yaml
            path = _CONFIG_DIR / "sp100.yaml"
            if path.exists():
                with open(path) as f:
                    data = yaml.safe_load(f) or {}
                tickers = data.get("tickers", [])
            else:
                tickers = []

    tickers = [t.upper() for t in tickers[:30]]
    results: dict[str, list] = {}

    for ticker in tickers:
        try:
            t = yf.Ticker(ticker)
            news = t.news or []
            results[ticker] = [
                {
                    "title": item.get("title"),
                    "published_at": datetime.fromtimestamp(
                        item.get("providerPublishTime", 0), tz=timezone.utc
                    ).isoformat() if item.get("providerPublishTime") else None,
                }
                for item in news[:limit_per_ticker]
            ]
        except Exception:
            results[ticker] = []

    return json.dumps({"tickers_fetched": len(results), "results": results})


def get_trending_tickers(limit: int = 30) -> str:
    """
    Scan recent market news headlines for mentioned ticker symbols.

    Looks for $TICKER patterns and common financial-news ticker formats across
    all RSS feeds. Useful for expanding daily batch runs beyond a fixed watchlist.

    Args:
        limit: Maximum number of unique tickers to return.

    Returns:
        JSON string: {tickers_found: int, tickers: [...]}.
    """
    import re

    raw = json.loads(get_market_news(limit * 3))
    articles = raw.get("articles", [])

    found: set[str] = set()
    # $TICKER or "TICKER stock/shares/shares" patterns common in financial headlines
    dollar_pattern = re.compile(r'\$([A-Z]{1,5})\b')
    word_pattern = re.compile(r'\b([A-Z]{2,5})\s+(?:stock|shares|shares|Corp|Inc|Ltd)\b')

    for art in articles:
        text = (art.get("title", "") + " " + art.get("summary", ""))
        for m in dollar_pattern.finditer(text):
            found.add(m.group(1))
        for m in word_pattern.finditer(text):
            found.add(m.group(1))

    # Strip common non-ticker uppercase words
    _STOP = {
        "IT", "OR", "IN", "AT", "AS", "BE", "US", "BY", "IS", "IF", "ON", "NO",
        "DO", "SO", "UP", "TO", "AN", "AM", "PM", "AI", "UK", "EU", "IPO", "CEO",
        "CFO", "CTO", "COO", "ETF", "GDP", "CPI", "PPI", "FED", "SEC", "IRS",
        "IMF", "WHO", "CDC", "NYSE", "NASDAQ", "SP", "DOW", "NEW", "THE",
    }
    tickers = sorted(found - _STOP)[:limit]
    return json.dumps({"tickers_found": len(tickers), "tickers": tickers})


def get_rss_headlines(feed_name: str = "reuters_business", limit: int = 15) -> str:
    """
    Return recent headlines from a named RSS feed.

    Args:
        feed_name: One of: reuters_business, seeking_alpha, marketwatch,
                   yahoo_finance, cnbc.
        limit:     Maximum number of entries to return.

    Returns:
        JSON string with feed_name and list of {title, link, published, summary} entries.
    """
    url = _RSS_FEEDS.get(feed_name)
    if not url:
        return json.dumps({"error": f"Unknown feed: {feed_name}. "
                           f"Choose from {list(_RSS_FEEDS.keys())}"})
    feed = feedparser.parse(url)
    entries = [
        {
            "title": entry.get("title"),
            "link": entry.get("link"),
            "published": entry.get("published"),
            "summary": (entry.get("summary") or "")[:300],
        }
        for entry in feed.entries[:limit]
    ]
    return json.dumps({"feed": feed_name, "entries": entries})


def get_earnings_calendar(ticker: str) -> str:
    """
    Return upcoming earnings dates and EPS/revenue estimates for a ticker.

    Args:
        ticker: Stock ticker symbol.

    Returns:
        JSON string with next_earnings_date, eps_estimate, revenue_estimate,
        trailing/forward EPS, and trailing/forward PE.
    """
    t = yf.Ticker(ticker)
    info = t.info
    cal = t.calendar or {}

    earnings_date = None
    if isinstance(cal, dict):
        ed = cal.get("Earnings Date")
        if ed is not None:
            if hasattr(ed, "__iter__") and not isinstance(ed, str):
                earnings_date = [str(d) for d in ed]
            else:
                earnings_date = str(ed)

    return json.dumps({
        "ticker": ticker.upper(),
        "next_earnings_date": earnings_date,
        "eps_estimate": cal.get("EPS Estimate") if isinstance(cal, dict) else None,
        "revenue_estimate": cal.get("Revenue Estimate") if isinstance(cal, dict) else None,
        "trailing_eps": info.get("trailingEps"),
        "forward_eps": info.get("forwardEps"),
        "trailing_pe": info.get("trailingPE"),
        "forward_pe": info.get("forwardPE"),
    })
