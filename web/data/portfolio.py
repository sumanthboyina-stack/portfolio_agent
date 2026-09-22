"""
Portfolio data helpers — pure data-shaping functions (no Streamlit calls).

Contains:
  - _enrich_prices          : fetch live prices for holdings missing current_price
  - group_holdings_by_account : group holding rows into per-(broker, account) buckets
  - missing_account_labels  : find accounts in a parsed upload with no account_number
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


def group_holdings_by_account(holdings: list[dict]) -> list[dict]:
    """
    Group holding rows into one entry per (broker, account_number) pair — the
    same bucket boundary upsert_holdings/delete_account_holdings use, so a card
    built from this always matches what a disconnect on it will remove.

    Each entry: {broker, account_number, account_name, holdings, value, count}.
    """
    groups: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []
    for h in holdings:
        broker = h.get("broker") or "unknown"
        account_number = (h.get("account_number") or "").strip()
        key = (broker, account_number)
        if key not in groups:
            groups[key] = {
                "broker": broker,
                "account_number": account_number,
                "account_name": "",
                "holdings": [],
            }
            order.append(key)
        entry = groups[key]
        entry["holdings"].append(h)
        if not entry["account_name"] and h.get("account_name"):
            entry["account_name"] = h["account_name"]

    result = []
    for key in order:
        entry = groups[key]
        entry["value"] = sum(h.get("current_value") or 0 for h in entry["holdings"])
        entry["count"] = len(entry["holdings"])
        result.append(entry)
    return result


def missing_account_labels(holdings: list[dict]) -> list[str]:
    """
    Distinct account_name values among rows with no account_number in a freshly
    parsed upload — each one needs a user-supplied nickname before saving so it
    gets its own account bucket instead of colliding with other unnamed accounts
    at the same broker. "" itself is a valid distinct group (no name either).
    """
    seen: list[str] = []
    for h in holdings:
        if not (h.get("account_number") or "").strip():
            name = (h.get("account_name") or "").strip()
            if name not in seen:
                seen.append(name)
    return seen
