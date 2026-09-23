"""
Portfolio data helpers — pure data-shaping functions (no Streamlit calls).

Contains:
  - _enrich_prices          : fetch live prices for holdings missing current_price (via market_data)
  - summarize_holdings      : re-exported from portfolio_agent.domain
  - is_fund / sector_breakdown : security type and sector split for the allocation charts
  - group_holdings_by_account : group holding rows into per-(broker, account) buckets
  - missing_account_labels  : find accounts in a parsed upload with no account_number
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))


def _enrich_prices(holdings: list[dict], refresh: bool = False) -> list[dict]:
    """
    Fetch live prices for holdings missing current_price (or for all of them
    with refresh=True) and stamp price_as_of with the trading date observed.
    Rows that cannot be priced keep current_price/current_value = None — an
    unknown value stays unknown rather than becoming $0.
    """
    tickers = sorted({h["ticker"] for h in holdings if refresh or not h.get("current_price")})
    if not tickers:
        return holdings
    try:
        from portfolio_agent.tools.market_data import fetch_last_closes
        prices, as_of = fetch_last_closes(tickers)
        for h in holdings:
            if h["ticker"] in prices and (refresh or not h.get("current_price")):
                price = prices[h["ticker"]]
                h["current_price"] = round(price, 4)
                h["current_value"] = round(price * h["shares"], 2) if h.get("shares") else None
                h["price_as_of"] = as_of
    except Exception:
        pass
    return holdings


def account_id(broker: str | None, account_number: str | None) -> str:
    """
    Immutable selection identity for an account: the (broker, account_number)
    bucket every holdings write/delete is scoped to. Display labels are derived
    separately so two accounts with the same nickname never collide.
    """
    return f"{broker or 'unknown'}:{(account_number or '').strip()}"


def masked_account_number(account_number: str | None) -> str:
    n = (account_number or "").strip()
    if not n:
        return ""
    return f"···{n[-4:]}" if len(n) > 4 else n


def scope_holdings(holdings: list, scope: str) -> list:
    """Filter holdings to one account_id, or return all for scope == 'all'."""
    if scope == "all":
        return list(holdings)
    return [h for h in holdings if account_id(h.get("broker"), h.get("account_number")) == scope]


FUND_KEYWORDS = ("ETF", " FUND", "INDEX FUND", "TRUST", " TR ", "SPDR", "ISHARES", "VANGUARD S&P")
FUND_SECTOR = "Funds & ETFs"
UNKNOWN_SECTOR = "Unknown"


def is_fund(holding) -> bool:
    """Fund/ETF vs single stock, from the broker description (the only signal an import carries)."""
    desc = f" {(holding.get('description') or '').upper()} "
    return any(k in desc for k in FUND_KEYWORDS)


def sector_breakdown(holdings: list, sectors: dict[str, str] | None = None) -> dict:
    """
    Value by sector for the allocation chart. Sector comes from the holding row
    when the import carried one, else from the cached universe data (*sectors*,
    looked up here when not passed); a fund with no sector is 'Funds & ETFs',
    anything else 'Unknown'. Unvalued positions are excluded and counted.
    Returns {"total", "rows": [{sector, value, pct, tickers}], "unvalued": n}.
    """
    valued = [h for h in holdings if h.get("current_value") is not None and h.get("ticker")]
    tickers = sorted({h["ticker"].upper() for h in valued})
    if sectors is None:
        try:
            from portfolio_agent.tools.universe_db import get_sectors
            sectors = get_sectors(tickers)
        except Exception:
            sectors = {}
    by_sector: dict[str, dict] = {}
    for h in valued:
        t = h["ticker"].upper()
        sector = (h.get("sector") or sectors.get(t) or "").strip()
        if not sector or sector == UNKNOWN_SECTOR:
            sector = FUND_SECTOR if is_fund(h) else UNKNOWN_SECTOR
        row = by_sector.setdefault(sector, {"sector": sector, "value": 0.0, "tickers": set()})
        row["value"] += float(h["current_value"])
        row["tickers"].add(t)
    total = sum(r["value"] for r in by_sector.values())
    rows = sorted(by_sector.values(), key=lambda r: -r["value"])
    for r in rows:
        r["value"] = round(r["value"], 2)
        r["pct"] = round(r["value"] / total * 100, 2) if total else 0.0
        r["tickers"] = sorted(r["tickers"])
    return {"total": round(total, 2), "rows": rows, "unvalued": len(holdings) - len(valued)}


def holdings_table_rows(holdings: list, names: dict[str, str] | None = None) -> list[dict]:
    """
    Typed rows for the interactive holdings table: stable position id, raw
    numeric values (None where unknown — never 0), and identity fields kept
    apart from display labels.
    """
    names = names or {}
    rows = []
    for h in holdings:
        cost = h.get("cost_basis_total")
        value = h.get("current_value")
        gain = (value - cost) if (value is not None and cost is not None) else None
        rows.append({
            "id": h.get("id"),
            "ticker": h["ticker"],
            "description": h.get("description") or names.get(h["ticker"].upper(), ""),
            "shares": h.get("shares"),
            "avg_cost": h.get("avg_cost"),
            "current_price": h.get("current_price"),
            "current_value": value,
            "cost_basis_total": cost,
            "gain": round(gain, 2) if gain is not None else None,
            "gain_pct": round(gain / cost * 100, 2) if (gain is not None and cost) else None,
            "broker": h.get("broker") or "manual",
            "account": h.get("account_name") or h.get("account_number") or "Unnamed account",
            "account_masked": masked_account_number(h.get("account_number")),
            "account_id": account_id(h.get("broker"), h.get("account_number")),
            "price_as_of": h.get("price_as_of"),
        })
    return rows


def group_holdings_by_account(holdings: list[dict]) -> list[dict]:
    """
    Group holding rows into one entry per (broker, account_number) pair — the
    same bucket boundary the account service scopes imports and deletes to, so a card
    built from this always matches what a disconnect on it will remove.

    Each entry: {account_id, broker, account_number, account_name, masked,
    holdings, value, count, unvalued}. Select accounts by account_id, never by
    the display label.
    """
    groups: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []
    for h in holdings:
        broker = h.get("broker") or "unknown"
        account_number = (h.get("account_number") or "").strip()
        key = (broker, account_number)
        if key not in groups:
            groups[key] = {
                "account_id": account_id(broker, account_number),   # UI selection key (stable string)
                "account_pk": h.get("account_id"),                  # accounts.account_id for service calls
                "broker": broker,
                "account_number": account_number,
                "masked": masked_account_number(account_number),
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
        entry["unvalued"] = sum(1 for h in entry["holdings"] if h.get("current_value") is None)
        result.append(entry)
    return result


from portfolio_agent.domain import summarize_holdings  # noqa: E402,F401  (re-export: pure summary math lives in the domain)
from portfolio_agent.tools.holdings_import import missing_account_labels  # noqa: E402,F401  (re-export)
