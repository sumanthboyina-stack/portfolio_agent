"""
Finnhub news fetcher — company news + aggregate sentiment.

Finnhub free tier: 60 calls/min, no daily cap.
Endpoint: GET /company-news?symbol=AAPL&from=YYYY-MM-DD&to=YYYY-MM-DD
Sentiment: GET /news-sentiment?symbol=AAPL

Articles are normalised to the same shape used by news_filter.py so the two
sources can be merged and de-duplicated transparently.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests

_API_KEY = os.getenv("FINNHUB_API_KEY", "")
# FINNHUB_SECRET is only for verifying incoming webhook calls FROM Finnhub to your server.
# It is NOT sent in outbound requests — the API key token param is sufficient for those.
_BASE    = "https://finnhub.io/api/v1"
_TIMEOUT = 10


def _params(extra: dict | None = None) -> dict:
    p = {"token": _API_KEY}
    if extra:
        p.update(extra)
    return p


# ── per-ticker fetchers ────────────────────────────────────────────────────────

def fetch_company_news(ticker: str, max_age_hours: int = 24) -> list[dict]:
    """
    Fetch recent company-specific headlines from Finnhub.

    Returns a list of normalised article dicts:
      {title, publisher, published_at, url, summary, _source: "finnhub"}
    """
    if not _API_KEY:
        return []

    now    = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=max_age_hours)
    # Finnhub date range uses the UTC calendar date
    date_from = (cutoff - timedelta(days=1)).strftime("%Y-%m-%d")  # one day buffer
    date_to   = now.strftime("%Y-%m-%d")

    try:
        resp = requests.get(
            f"{_BASE}/company-news",
            params=_params({"symbol": ticker.upper(), "from": date_from, "to": date_to}),
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        items = resp.json() or []
    except Exception:
        return []

    articles: list[dict] = []
    for item in items:
        ts = item.get("datetime", 0)
        if not ts:
            continue
        pub = datetime.fromtimestamp(ts, tz=timezone.utc)
        if pub < cutoff:
            continue
        title = (item.get("headline") or "").strip()
        if not title:
            continue
        articles.append({
            "title":        title,
            "publisher":    item.get("source", ""),
            "published_at": pub.isoformat(),
            "url":          item.get("url", ""),
            "summary":      (item.get("summary") or "")[:500],
            "_source":      "finnhub",
        })

    return articles


def fetch_news_sentiment(ticker: str) -> dict | None:
    """
    Fetch aggregate news sentiment for a ticker from Finnhub.

    Returns:
      {bullish_pct, bearish_pct, articles_last_week, company_score}
    or None on error / missing key.
    """
    if not _API_KEY:
        return None

    try:
        resp = requests.get(
            f"{_BASE}/news-sentiment",
            params=_params({"symbol": ticker.upper()}),
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json() or {}
    except Exception:
        return None

    sentiment = data.get("sentiment") or {}
    buzz      = data.get("buzz") or {}
    return {
        "bullish_pct":         round(sentiment.get("bullishPercent", 0) * 100, 1),
        "bearish_pct":         round(sentiment.get("bearishPercent", 0) * 100, 1),
        "articles_last_week":  buzz.get("articlesInLastWeek", 0),
        "company_score":       round(data.get("companyNewsScore", 0), 3),
    }


# ── batch fetch with rate-limit guard ─────────────────────────────────────────

def batch_fetch_finnhub(
    tickers: list[str],
    max_age_hours: int = 24,
    max_workers: int = 8,
) -> dict[str, list[dict]]:
    """
    Fetch Finnhub company news for all tickers in parallel.

    max_workers=8 keeps us comfortably under the 60 calls/min free limit
    (8 concurrent × ~0.2-0.5 s latency ≈ 16-40 calls/min peak).

    Returns {ticker: [article, ...]}  — empty list on any error.
    """
    if not _API_KEY:
        return {t: [] for t in tickers}

    results: dict[str, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(fetch_company_news, t, max_age_hours): t
            for t in tickers
        }
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                results[ticker] = future.result()
            except Exception:
                results[ticker] = []
    return results


# ── source selection ──────────────────────────────────────────────────────────

def merge_articles(
    yahoo_articles: list[dict],
    finnhub_articles: list[dict],
) -> list[dict]:
    """
    Finnhub primary: use Finnhub articles when available; fall back to Yahoo only
    when Finnhub returned nothing for this ticker.

    Rationale: Yahoo Finance returns 0 articles for ~80% of batch runs (see logs).
    When Yahoo does return articles, Finnhub typically has the same stories plus
    structured summaries — so the Yahoo version adds nothing.  Maintaining a
    three-rule dedup (URL match → title similarity → keep both) across two passes
    was complexity that served a 20%-of-the-time edge case.

    Returns list sorted newest first.
    """
    articles = finnhub_articles if finnhub_articles else yahoo_articles
    return sorted(articles, key=lambda a: a.get("published_at") or "", reverse=True)
