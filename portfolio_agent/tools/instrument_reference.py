"""
Instrument reference data for MARKET-ONLY code — what kind of security a ticker is.

An instrument row (type, asset class, leverage) describes the security itself: it has no owner, no account, no
quantity, no cost. That is why code that must never read a person's holdings (tools.market_context, the shared
forecast path) may read it, through this module and ONLY this module: it exposes the instruments table and nothing
else, so there is no way to reach positions, accounts or cash from here. The same goes for the SHARED fund tables
(tools.fund_data_db: a fund's holdings, sector and asset-class mix, expense ratio): market data about the fund itself.
A person's manual fund breakdown and a ticker-less fund's proxy are private and are never read here.
"""
from __future__ import annotations

from portfolio_agent.tools import holdings_db as _instruments


def get_instrument_profiles(tickers: list[str]) -> dict[str, dict]:
    """{ticker: {instrument_type, asset_class, leverage_factor}} for tickers that have an instrument row."""
    return _instruments.get_instrument_profiles(tickers)


RC_FUND_CONTEXT_UNAVAILABLE = "FUND_DATA_UNAVAILABLE"
_TOP_HOLDINGS, _TOP_SECTORS = 10, 6


def get_fund_context(ticker: str) -> dict:
    """
    A compact, deterministic summary of one fund from SHARED fund data only: top holdings, sector mix, asset-class mix,
    expense ratio, and concentration of what is known (top10 %, HHI, effective N — normalized by how much of the fund
    the stored holdings cover). With nothing stored: {"status": "UNKNOWN", "reason_code": ...} — never a guess.
    """
    from portfolio_agent.tools import fund_data_db as fdb
    from portfolio_agent.tools.lookthrough import concentration_stats
    t = (ticker or "").upper()
    inst = _instruments.find_instrument(t)
    data = fdb.get_fund_data(inst["instrument_id"], t) if inst else None
    if data is None or not data.ok:
        return {"status": "UNKNOWN", "reason_code": RC_FUND_CONTEXT_UNAVAILABLE}
    cov = data.holdings_coverage or 0.0
    conc = concentration_stats([h.weight for h in data.holdings], explained_value=cov if cov > 0 else None) if data.holdings else {}
    return {
        "status": "OK", "as_of": data.as_of, "source": data.source,
        "expense_ratio_pct": round(data.profile.expense_ratio * 100, 4) if data.profile and data.profile.expense_ratio is not None else None,
        "category": data.profile.category if data.profile else None,
        "top_holdings": [{"symbol": h.symbol, "name": h.name, "weight_pct": round(h.weight * 100, 2)}
                         for h in data.holdings[:_TOP_HOLDINGS]],
        "holdings_coverage_pct": round(cov * 100, 1),
        "sector_mix_pct": {k: round(v * 100, 1) for k, v in sorted(data.sector_weights.items(), key=lambda kv: -kv[1])[:_TOP_SECTORS]},
        "asset_class_mix_pct": {k: round(v * 100, 1) for k, v in sorted(data.asset_class_weights.items(), key=lambda kv: -kv[1])},
        "concentration_of_known_holdings": {k: conc.get(k) for k in ("top10_pct", "hhi", "effective_n")} if conc else None,
        "reason_codes": list(data.reason_codes),
    }
