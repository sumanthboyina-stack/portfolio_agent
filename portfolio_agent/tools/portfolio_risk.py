"""
Portfolio-level risk math: correlation between holdings, aggregate sector
exposure, beta vs a market benchmark, and issuer concentration (multiple
tickers mapping to the same SEC-registered issuer, e.g. GOOG/GOOGL).

Pure computation (pandas, one yfinance pull) — no LLM. The per-ticker Risk
specialist's tools can only see one ticker at a time and can't derive any of
this; this module computes it once for the whole portfolio and the daily
pipeline folds the result into the Risk specialist's prompt as pre-computed
context.
"""

from __future__ import annotations

from typing import Optional

_BENCHMARK = "SPY"
_LOOKBACK  = "6mo"


def compute_portfolio_risk_context(holdings: list[dict]) -> dict[str, dict]:
    """
    Given holdings [{ticker, shares, sector, ...}], return:
        {TICKER: {correlation_with_portfolio, most_correlated_peer,
                  sector_concentration_pct, beta_vs_spy,
                  issuer_concentration_pct, issuer_peers}}

    Best-effort: needs at least 2 holdings with fetchable price history.
    Returns {} on any failure — caller falls back to per-ticker-only analysis.
    """
    import pandas as pd
    import yfinance as yf

    from portfolio_agent.tools.edgar_check import _load_cik_map
    from portfolio_agent.tools.universe_db import get_sectors

    tickers = sorted({str(h["ticker"]).upper() for h in holdings if h.get("ticker")})
    if len(tickers) < 2:
        return {}

    fetch_list = tickers + [_BENCHMARK]
    try:
        raw = yf.download(
            fetch_list, period=_LOOKBACK, interval="1d",
            auto_adjust=True, progress=False, group_by="ticker",
        )
    except Exception:
        return {}

    closes: dict[str, "pd.Series"] = {}
    for t in fetch_list:
        try:
            s = raw[t]["Close"].dropna()
            if len(s) >= 20:
                closes[t] = s
        except Exception:
            continue

    valid = [t for t in tickers if t in closes]
    if len(valid) < 2:
        return {}

    cols = valid + ([_BENCHMARK] if _BENCHMARK in closes else [])
    price_df = pd.DataFrame({t: closes[t] for t in cols}).dropna(how="any")
    if len(price_df) < 20:
        return {}
    returns = price_df.pct_change().dropna()
    corr = returns[valid].corr()

    # Position value per ticker (last close * shares) for weighted aggregation.
    weight: dict[str, float] = {}
    for h in holdings:
        t = str(h.get("ticker", "")).upper()
        if t in closes:
            price = float(closes[t].iloc[-1])
            weight[t] = weight.get(t, 0.0) + float(h.get("shares") or 0) * price
    total_w = sum(weight.values()) or 1.0

    # portfolio.yaml's own sector field is frequently blank (broker CSV import
    # often doesn't populate it) — fall back to the cached S&P/Nasdaq universe
    # sector data, which is free (no network call) and usually populated.
    sector_of: dict[str, str] = {
        str(h["ticker"]).upper(): h.get("sector") for h in holdings if h.get("sector")
    }
    cached_sectors = get_sectors([str(h["ticker"]).upper() for h in holdings])
    for h in holdings:
        t = str(h["ticker"]).upper()
        if t not in sector_of:
            sector_of[t] = cached_sectors.get(t, "Unknown")
    sector_totals: dict[str, float] = {}
    for t, w in weight.items():
        s = sector_of.get(t, "Unknown")
        sector_totals[s] = sector_totals.get(s, 0.0) + w

    try:
        cik_map = _load_cik_map()
    except Exception:
        cik_map = {}
    issuer_of: dict[str, str] = {t: cik_map.get(t, t) for t in weight}
    issuer_totals: dict[str, float] = {}
    for t, w in weight.items():
        issuer_totals[issuer_of[t]] = issuer_totals.get(issuer_of[t], 0.0) + w

    # Betas outside roughly [-5, 5] are a data-quality artifact (illiquid/gappy
    # tickers, corporate-action mispricing in the window) rather than a real
    # market-risk reading — drop them instead of surfacing a fabricated-looking
    # number (e.g. an untraded foreign ADR can produce a "beta" of -30+).
    _BETA_BOUND = 5.0
    betas: dict[str, float] = {}
    if _BENCHMARK in returns.columns:
        bench_var = returns[_BENCHMARK].var()
        if bench_var and bench_var > 0:
            for t in valid:
                b = round(float(returns[t].cov(returns[_BENCHMARK]) / bench_var), 2)
                if abs(b) <= _BETA_BOUND:
                    betas[t] = b

    result: dict[str, dict] = {}
    for t in valid:
        peers = corr[t].drop(t).sort_values(ascending=False)
        most_correlated: Optional[str] = None
        if not peers.empty:
            most_correlated = f"{peers.index[0]} ({peers.iloc[0]:.2f})"

        others = [p for p in valid if p != t and p in weight]
        w_avg_corr: Optional[float] = None
        if others:
            wsum = sum(weight[p] for p in others) or 1.0
            w_avg_corr = round(float(sum(corr.loc[t, p] * weight[p] for p in others) / wsum), 2)

        sector = sector_of.get(t, "Unknown")
        sector_pct = round(sector_totals.get(sector, 0.0) / total_w * 100, 1)

        cik = issuer_of.get(t, t)
        issuer_pct = round(issuer_totals.get(cik, 0.0) / total_w * 100, 1)
        issuer_peers = sorted(p for p, c in issuer_of.items() if c == cik and p != t)

        result[t] = {
            "correlation_with_portfolio": w_avg_corr,
            "most_correlated_peer": most_correlated,
            "sector_concentration_pct": sector_pct,
            "beta_vs_spy": betas.get(t),
            "issuer_concentration_pct": issuer_pct,
            "issuer_peers": issuer_peers,
        }
    return result
