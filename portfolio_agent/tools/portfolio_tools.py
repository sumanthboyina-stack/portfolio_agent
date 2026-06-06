"""Portfolio tools — holdings reader, concentration analysis, and restricted-list check."""

import json
from pathlib import Path

import yaml
import yfinance as yf

_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def _load_portfolio() -> dict:
    path = _CONFIG_DIR / "portfolio.yaml"
    if not path.exists():
        return {"holdings": []}
    with open(path) as f:
        return yaml.safe_load(f) or {"holdings": []}


def get_portfolio_holdings() -> str:
    """
    Return current portfolio holdings from config/portfolio.yaml.

    Returns:
        JSON string with a list of holdings: ticker, shares, avg_cost, sector.
    """
    data = _load_portfolio()
    return json.dumps(data)


def get_portfolio_concentration(ticker: str) -> str:
    """
    Compute how much of the current portfolio is in *ticker* and show sector exposure.

    Args:
        ticker: Stock ticker symbol to evaluate.

    Returns:
        JSON string with portfolio_total_value, ticker_weight_pct,
        sector_exposure_pct, concentration_flag (>10 %), and holdings_count.
    """
    data = _load_portfolio()
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


def check_restricted_list(ticker: str) -> str:
    """
    Check whether *ticker* appears on the compliance restricted list.

    Args:
        ticker: Stock ticker symbol to check.

    Returns:
        JSON string with is_restricted (bool) and reason (string or null).
    """
    path = _CONFIG_DIR / "restricted_list.yaml"
    if not path.exists():
        return json.dumps({"ticker": ticker.upper(), "is_restricted": False, "reason": None})

    with open(path) as f:
        data = yaml.safe_load(f) or {}

    restricted: list[dict] = data.get("restricted", [])
    ticker_upper = ticker.upper()
    for entry in restricted:
        if entry.get("ticker", "").upper() == ticker_upper:
            return json.dumps({
                "ticker": ticker_upper,
                "is_restricted": True,
                "reason": entry.get("reason", "No reason specified"),
            })

    return json.dumps({"ticker": ticker_upper, "is_restricted": False, "reason": None})


