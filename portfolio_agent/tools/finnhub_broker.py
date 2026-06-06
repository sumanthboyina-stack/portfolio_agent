"""
Finnhub analyst / broker data — recommendation trends and price targets.

Two unique signals not reliably available from yfinance:
  recommendation trend  — monthly buy/hold/sell counts going back 12+ months,
                          shows whether consensus is improving or deteriorating
  price target          — independent PT source for cross-validation against yfinance

The upgrade-downgrade endpoint is skipped: yfinance already provides firm-level
rating changes with firm names, which is more useful than the Finnhub equivalent.

Free tier: 60 calls/min, no daily cap.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import requests

_API_KEY = os.getenv("FINNHUB_API_KEY", "")
_BASE    = "https://finnhub.io/api/v1"
_TIMEOUT = 10


def _params(extra: dict | None = None) -> dict:
    p = {"token": _API_KEY}
    if extra:
        p.update(extra)
    return p


def fetch_recommendation_trend(ticker: str, limit: int = 6) -> list[dict]:
    """
    Monthly snapshot of buy/hold/sell analyst counts from Finnhub.

    Returns the last *limit* monthly periods, newest first.
    Each entry: {period, strongBuy, buy, hold, sell, strongSell, total, pct_bullish}.
    Returns [] if Finnhub key is missing or the call fails.
    """
    if not _API_KEY:
        return []
    try:
        resp = requests.get(
            f"{_BASE}/stock/recommendation",
            params=_params({"symbol": ticker.upper()}),
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        items = resp.json() or []
    except Exception:
        return []

    trend = []
    for item in sorted(items, key=lambda x: x.get("period", ""), reverse=True)[:limit]:
        sb  = int(item.get("strongBuy",  0))
        b   = int(item.get("buy",        0))
        h   = int(item.get("hold",       0))
        s   = int(item.get("sell",       0))
        ss  = int(item.get("strongSell", 0))
        total = sb + b + h + s + ss
        pct_bullish = round((sb + b) / total * 100, 1) if total else None
        trend.append({
            "period":      item.get("period", "")[:7],  # YYYY-MM
            "strongBuy":   sb,
            "buy":         b,
            "hold":        h,
            "sell":        s,
            "strongSell":  ss,
            "total":       total,
            "pct_bullish": pct_bullish,
        })
    return trend


def fetch_finnhub_price_target(ticker: str) -> Optional[dict]:
    """
    Aggregated analyst price target from Finnhub (independent of yfinance).

    Returns {target_high, target_low, target_mean, target_median, last_updated}
    or None on failure.
    """
    if not _API_KEY:
        return None
    try:
        resp = requests.get(
            f"{_BASE}/stock/price-target",
            params=_params({"symbol": ticker.upper()}),
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json() or {}
    except Exception:
        return None

    if not data.get("targetMean"):
        return None
    return {
        "target_high":   data.get("targetHigh"),
        "target_low":    data.get("targetLow"),
        "target_mean":   data.get("targetMean"),
        "target_median": data.get("targetMedian"),
        "last_updated":  data.get("lastUpdated"),
    }


def fetch_broker_bundle(ticker: str) -> dict:
    """
    Fetch both Finnhub broker signals for one ticker.

    Returns {ticker, rec_trend, finnhub_pt} — either or both may be empty/None.
    """
    ticker = ticker.upper()
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_trend = pool.submit(fetch_recommendation_trend, ticker)
        f_pt    = pool.submit(fetch_finnhub_price_target, ticker)
        rec_trend = f_trend.result()
        finnhub_pt = f_pt.result()
    return {"ticker": ticker, "rec_trend": rec_trend, "finnhub_pt": finnhub_pt}


def batch_fetch_finnhub_broker(
    tickers: list[str],
    max_workers: int = 8,
) -> dict[str, dict]:
    """
    Fetch recommendation trend + price target for all tickers in parallel.

    max_workers=8 keeps us well within the 60 calls/min free limit
    (2 calls per ticker × 8 concurrent = 16 simultaneous requests).

    Returns {ticker: {rec_trend, finnhub_pt}}.
    """
    if not _API_KEY:
        return {t: {"ticker": t.upper(), "rec_trend": [], "finnhub_pt": None} for t in tickers}

    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fetch_broker_bundle, t): t for t in tickers}
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                results[ticker.upper()] = future.result()
            except Exception:
                results[ticker.upper()] = {
                    "ticker": ticker.upper(), "rec_trend": [], "finnhub_pt": None,
                }
    return results
