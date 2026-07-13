"""
Portfolio data helpers — pure data-shaping functions (no Streamlit calls).

Contains:
  - _enrich_prices : fetch live prices for holdings missing current_price
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))


def _enrich_prices(holdings: list[dict]) -> list[dict]:
    """Fetch live prices for holdings that don't already have current_price."""
    tickers = [h["ticker"] for h in holdings if not h.get("current_price")]
    if not tickers:
        return holdings
    try:
        import yfinance as yf
        prices = {}
        for chunk in [tickers[i:i+10] for i in range(0, len(tickers), 10)]:
            data = yf.download(
                " ".join(chunk), period="1d", auto_adjust=True,
                progress=False, group_by="ticker",
            )
            for t in chunk:
                try:
                    if len(chunk) == 1:
                        prices[t] = float(data["Close"].iloc[-1])
                    else:
                        prices[t] = float(data[t]["Close"].iloc[-1])
                except Exception:
                    pass

        for h in holdings:
            if h["ticker"] in prices and not h.get("current_price"):
                price = prices[h["ticker"]]
                h["current_price"] = round(price, 2)
                if h.get("shares"):
                    h["current_value"] = round(price * h["shares"], 2)
    except Exception:
        pass
    return holdings
