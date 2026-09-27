"""Portfolio tools — holdings reader, concentration analysis, and restricted-list check."""

import json
from typing import Optional

import yfinance as yf

from google.adk.tools import ToolContext


def _owner_from_tool_context(tool_context: Optional[ToolContext]) -> str | None:
    """
    The logged-in owner for this ADK run, if the session state carries one —
    set server-side (see web/chat/orchestration.py's create_session(state=...)),
    never something the model can set itself. None outside a chat run (batch
    jobs, the Valuation page, etc.), where _load_portfolio() falls back to the
    single-portfolio/system-wide read.
    """
    if tool_context is None:
        return None
    return tool_context.state.get("owner")


def _load_portfolio(owner: str | None = None) -> dict:
    from portfolio_agent.tools.holdings_db import get_holdings, get_portfolio_by_owner

    if owner is None:
        holdings = get_holdings()
    else:
        portfolio = get_portfolio_by_owner(owner)
        holdings = get_holdings(portfolio_id=portfolio["portfolio_id"]) if portfolio else []
    return {"holdings": [h.to_dict() for h in holdings]}


def get_portfolio_holdings(tool_context: Optional[ToolContext] = None) -> str:
    """
    Return current portfolio holdings.

    Returns:
        JSON string with a list of holdings: ticker, shares, avg_cost, sector.
    """
    data = _load_portfolio(_owner_from_tool_context(tool_context))
    return json.dumps(data)


def get_portfolio_concentration(ticker: str, tool_context: Optional[ToolContext] = None) -> str:
    """
    Compute how much of the current portfolio is in *ticker* and show sector exposure.

    Args:
        ticker: Stock ticker symbol to evaluate.

    Returns:
        JSON string with portfolio_total_value, ticker_weight_pct,
        sector_exposure_pct, concentration_flag (>10 %), and holdings_count.
    """
    data = _load_portfolio(_owner_from_tool_context(tool_context))
    holdings = data.get("holdings", [])

    if not holdings:
        return json.dumps({
            "ticker": ticker.upper(),
            "portfolio_total_value": 0,
            "ticker_current_value": 0,
            "ticker_weight_pct": 0,
            "ticker_sector": "Unknown",
            "sector_exposure_pct": {},
            "holdings_count": 0,
            "concentration_flag": False,
        })

    all_tickers = list({h["ticker"].upper() for h in holdings} | {ticker.upper()})
    prices: dict[str, float] = {}
    for t in all_tickers:
        try:
            info = yf.Ticker(t).fast_info
            prices[t] = float(getattr(info, "last_price", 0) or 0)
        except Exception:
            prices[t] = 0.0

    total_value = sum(
        h["shares"] * prices.get(h["ticker"].upper(), 0) for h in holdings
    )

    ticker_upper = ticker.upper()
    ticker_holding = next((h for h in holdings if h["ticker"].upper() == ticker_upper), None)
    ticker_value = (ticker_holding["shares"] * prices.get(ticker_upper, 0)) if ticker_holding else 0.0
    ticker_weight = (ticker_value / total_value * 100) if total_value > 0 else 0.0

    sector_values: dict[str, float] = {}
    for h in holdings:
        sector = h.get("sector", "Unknown")
        val = h["shares"] * prices.get(h["ticker"].upper(), 0)
        sector_values[sector] = sector_values.get(sector, 0) + val
    sector_exposure = {
        s: round(v / total_value * 100, 2) if total_value > 0 else 0.0
        for s, v in sector_values.items()
    }

    ticker_sector = ticker_holding.get("sector", "Unknown") if ticker_holding else "Unknown"

    return json.dumps({
        "ticker": ticker_upper,
        "portfolio_total_value": round(total_value, 2),
        "ticker_current_value": round(ticker_value, 2),
        "ticker_weight_pct": round(ticker_weight, 2),
        "ticker_sector": ticker_sector,
        "sector_exposure_pct": sector_exposure,
        "holdings_count": len(holdings),
        "concentration_flag": ticker_weight > 10.0,
    })


def check_restricted_list(ticker: str, tool_context: Optional[ToolContext] = None) -> str:
    """
    Check whether *ticker* appears on the caller's OWN compliance restricted
    list (restricted_list is personal-only, no shared/global scope).

    Args:
        ticker: Stock ticker symbol to check.

    Returns:
        JSON string with is_restricted (bool) and reason (string or null).
    """
    from portfolio_agent.tools.restricted_list_db import is_restricted

    ticker_upper = ticker.upper()
    owner = _owner_from_tool_context(tool_context)
    if not owner:
        return json.dumps({"ticker": ticker_upper, "is_restricted": None,
                           "reason": "no owner to check the restricted list against"})
    restricted, reason = is_restricted(ticker_upper, owner)
    return json.dumps({"ticker": ticker_upper, "is_restricted": restricted, "reason": reason})


