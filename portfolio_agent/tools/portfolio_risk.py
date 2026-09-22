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


def _sectors_of_and_totals(
    holdings: list[dict], weight: dict[str, float]
) -> tuple[dict[str, str], dict[str, float]]:
    """
    Map each ticker to a sector (the holdings table's own sector field first,
    since broker CSV import frequently leaves it blank, falling back to the
    cached S&P/Nasdaq universe sector data), and aggregate position value by
    sector.
    """
    from portfolio_agent.tools.universe_db import get_sectors

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

    return sector_of, sector_totals


def correlation_diversification_bonus(
    correlation: Optional[float], cap: float = 9.0, neutral: float = 0.6
) -> float:
    """
    0..cap: rewards a candidate's low correlation to current holdings.
    At/above *neutral* correlation, no bonus. A rough heuristic — see
    compute_hypothetical_addition_impact for the raw correlation number.
    """
    if correlation is None:
        return 0.0
    return min(cap, max(0.0, (neutral - correlation) * 15))


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

    sector_of, sector_totals = _sectors_of_and_totals(holdings, weight)

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


def _annualized_vol(returns_col) -> Optional[float]:
    """Annualized std dev of a daily-returns series, or None if degenerate."""
    v = float(returns_col.std())
    if not v or v != v:  # NaN check without importing math for one call
        return None
    return v * (252 ** 0.5)


def _implied_return_from_composite(composite_score: Optional[float]) -> Optional[float]:
    """
    Rough fallback expected-return proxy when a prediction has no explicit
    predicted_return range: composite_score 10 -> +9, 5.5 (neutral) -> 0,
    1 -> -9. Only used when predicted_return_low/high are both missing.

    Units: percentage POINTS, matching predicted_return_low/high's own
    convention (e.g. stored value 2.0 means +2%, not the fraction 0.02) —
    confirmed against real prediction rows, not just the prompt spec.
    """
    if composite_score is None:
        return None
    return round((float(composite_score) - 5.5) * 2.0, 2)


def _expected_return(prediction) -> Optional[float]:
    """
    Predicted-return midpoint from a Prediction row (percentage points, e.g.
    2.0 means +2% — predicted_return_low/high's own storage convention),
    falling back to the composite-implied proxy when both are missing.
    """
    if prediction is None:
        return None
    lo = prediction.get("predicted_return_low")
    hi = prediction.get("predicted_return_high")
    if lo is not None and hi is not None:
        return (float(lo) + float(hi)) / 2.0
    return _implied_return_from_composite(prediction.get("composite_score"))


def _risk_adjusted_return(prediction, vol: Optional[float]) -> Optional[float]:
    """predicted 21d return / annualized volatility — a simple Sharpe-like proxy.
    expected_return is in percentage points; vol is a decimal fraction — divide
    by 100 to put both sides on the same (decimal-fraction) scale."""
    if prediction is None or not vol:
        return None
    expected_return = _expected_return(prediction)
    if expected_return is None:
        return None
    return round((expected_return / 100.0) / vol, 3)


def compute_portfolio_aggregate_metrics(holdings: list[dict]) -> Optional[dict]:
    """
    Single-number portfolio-level metrics (as opposed to compute_portfolio_risk_context's
    per-holding breakdown): weighted beta, most-concentrated sector, annualized
    portfolio volatility, expected 21d return, and average pairwise correlation
    ("correlation concentration"). Used to project before/after impact when
    proposing trades (see portfolio_optimizer.py).

    Returns None if fewer than 2 tickers have fetchable price history (same
    degrade-gracefully contract as compute_portfolio_risk_context).

    Units: expected_return_21d and top_sector_pct are percentage points
    (3.8 means +3.8%); portfolio_volatility_annualized and
    avg_pairwise_correlation are decimal fractions (0.15 means 15%).
    """
    import numpy as np
    import pandas as pd
    import yfinance as yf

    from portfolio_agent.tools.prediction_db import get_all_latest_predictions

    tickers = sorted({str(h["ticker"]).upper() for h in holdings if h.get("ticker")})
    if len(tickers) < 2:
        return None

    fetch_list = tickers + [_BENCHMARK]
    try:
        raw = yf.download(
            fetch_list, period=_LOOKBACK, interval="1d",
            auto_adjust=True, progress=False, group_by="ticker",
        )
    except Exception:
        return None

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
        return None

    cols = valid + ([_BENCHMARK] if _BENCHMARK in closes else [])
    price_df = pd.DataFrame({t: closes[t] for t in cols}).dropna(how="any")
    if len(price_df) < 20:
        return None
    returns = price_df.pct_change().dropna()

    weight: dict[str, float] = {}
    for h in holdings:
        t = str(h.get("ticker", "")).upper()
        if t in closes:
            price = float(closes[t].iloc[-1])
            weight[t] = weight.get(t, 0.0) + float(h.get("shares") or 0) * price
    total_w = sum(weight.get(t, 0.0) for t in valid)
    if not total_w:
        return None
    weights_norm = {t: weight.get(t, 0.0) / total_w for t in valid}

    # Portfolio-level daily return series = weight-sum of each holding's own
    # daily return — this reflects real diversification effects, not a heuristic.
    portfolio_returns = sum(returns[t] * weights_norm[t] for t in valid)
    portfolio_volatility_annualized = _annualized_vol(portfolio_returns)

    _BETA_BOUND = 5.0
    weighted_beta: Optional[float] = None
    if _BENCHMARK in returns.columns:
        bench_var = returns[_BENCHMARK].var()
        if bench_var and bench_var > 0:
            betas = {}
            for t in valid:
                b = float(returns[t].cov(returns[_BENCHMARK]) / bench_var)
                if abs(b) <= _BETA_BOUND:
                    betas[t] = b
            if betas:
                # Beta is linear in weights: beta_portfolio = Σ weight_i · beta_i
                # (exact given the same aligned return series used for both).
                bw_total = sum(weights_norm[t] for t in betas)
                weighted_beta = round(
                    sum(weights_norm[t] * betas[t] for t in betas) / bw_total, 2
                ) if bw_total else None

    # Value-weighted average of the off-diagonal correlation matrix — a single
    # scalar for how much diversification benefit the book currently has.
    avg_pairwise_correlation: Optional[float] = None
    corr = returns[valid].corr()
    w_arr = np.array([weights_norm[t] for t in valid])
    weight_matrix = np.outer(w_arr, w_arr)
    off_diag = ~np.eye(len(valid), dtype=bool)
    denom = weight_matrix[off_diag].sum()
    if denom:
        avg_pairwise_correlation = round(
            float((corr.values * weight_matrix)[off_diag].sum() / denom), 2
        )

    # Expected return: weighted average of each holding's latest APEX prediction
    # (predicted_return midpoint, falling back to the composite-implied proxy).
    latest_preds = get_all_latest_predictions()
    expected_return_21d: Optional[float] = None
    contributions = [
        (weights_norm[t], _expected_return(latest_preds.get(t)))
        for t in valid
    ]
    contributions = [(w, r) for w, r in contributions if r is not None]
    if contributions:
        w_sum = sum(w for w, _ in contributions)
        expected_return_21d = round(sum(w * r for w, r in contributions) / w_sum, 4) if w_sum else None

    _, sector_totals = _sectors_of_and_totals(holdings, weight)
    top_sector = max(sector_totals, key=sector_totals.get) if sector_totals else None
    top_sector_pct = round(sector_totals.get(top_sector, 0.0) / total_w * 100, 1) if top_sector else None

    return {
        "total_value": total_w,
        "weighted_beta": weighted_beta,
        "top_sector": top_sector,
        "top_sector_pct": top_sector_pct,
        "portfolio_volatility_annualized": (
            round(portfolio_volatility_annualized, 3) if portfolio_volatility_annualized else None
        ),
        "expected_return_21d": expected_return_21d,
        "avg_pairwise_correlation": avg_pairwise_correlation,
        # Latest close used for each holding's weight — exposed so callers projecting
        # a post-trade "after" state can convert $ allocations to shares without a
        # second yfinance fetch (see portfolio_optimizer.py).
        "prices": {t: float(closes[t].iloc[-1]) for t in valid},
    }


def compute_hypothetical_addition_impact(
    ticker: str,
    holdings: list[dict],
    position_pct: float = 0.02,
) -> Optional[dict]:
    """
    Best-effort answer to "what happens if I add *ticker* to this portfolio":
    correlation vs current holdings, sector concentration before/after adding
    a position_pct-of-portfolio position, beta vs SPY, and a risk-adjusted
    return comparison against the portfolio's current weakest-conviction
    holding (lowest APEX composite_score among holdings with a prediction).

    Returns None if there isn't enough fetchable price history to say
    anything meaningful (mirrors compute_portfolio_risk_context's contract).
    Individual fields inside the dict may still be None on partial data.
    """
    import pandas as pd
    import yfinance as yf

    from portfolio_agent.tools.universe_db import get_sectors
    from portfolio_agent.tools.prediction_db import get_all_latest_predictions

    ticker = ticker.upper()
    # Excluding the ticker from its own comparison set matters when it's already a
    # holding (e.g. scoring an existing position's fit) — otherwise it's compared
    # against a portfolio that includes itself, degenerately inflating its own
    # correlation to 1.0. Harmless no-op when the ticker isn't currently held.
    holdings = [h for h in holdings if str(h.get("ticker", "")).upper() != ticker]
    holding_tickers = sorted({str(h["ticker"]).upper() for h in holdings if h.get("ticker")})
    if not holding_tickers:
        return None

    fetch_list = list(dict.fromkeys(holding_tickers + [ticker, _BENCHMARK]))
    try:
        raw = yf.download(
            fetch_list, period=_LOOKBACK, interval="1d",
            auto_adjust=True, progress=False, group_by="ticker",
        )
    except Exception:
        return None

    closes: dict[str, "pd.Series"] = {}
    for t in fetch_list:
        try:
            s = raw[t]["Close"].dropna()
            if len(s) >= 20:
                closes[t] = s
        except Exception:
            continue

    if ticker not in closes:
        return None
    valid_holdings = [t for t in holding_tickers if t in closes]
    if len(valid_holdings) < 2:
        return None

    cols = valid_holdings + [ticker] + ([_BENCHMARK] if _BENCHMARK in closes else [])
    price_df = pd.DataFrame({t: closes[t] for t in cols}).dropna(how="any")
    if len(price_df) < 20:
        return None
    returns = price_df.pct_change().dropna()

    # Position value per existing holding (last close * shares) for weighted correlation.
    weight: dict[str, float] = {}
    for h in holdings:
        t = str(h.get("ticker", "")).upper()
        if t in closes:
            price = float(closes[t].iloc[-1])
            weight[t] = weight.get(t, 0.0) + float(h.get("shares") or 0) * price
    total_w = sum(weight.values())
    if not total_w:
        return None

    corr_with_candidate = returns[valid_holdings].corrwith(returns[ticker])
    w_avg_corr = round(
        float(sum(corr_with_candidate[t] * weight.get(t, 0.0) for t in valid_holdings) / total_w), 2
    )

    _, sector_totals = _sectors_of_and_totals(holdings, weight)
    candidate_sector = get_sectors([ticker]).get(ticker, "Unknown")

    position_value = total_w * position_pct
    new_total_w = total_w + position_value

    def _sector_pct_after(sector: str) -> float:
        bumped = sector_totals.get(sector, 0.0) + (position_value if sector == candidate_sector else 0.0)
        return round(bumped / new_total_w * 100, 1)

    candidate_sector_before_pct = round(sector_totals.get(candidate_sector, 0.0) / total_w * 100, 1)
    candidate_sector_after_pct = _sector_pct_after(candidate_sector)

    # The portfolio's most concentrated sector today — a position OUTSIDE it
    # dilutes this down (grows the denominator without growing this sector's
    # numerator); a position inside it is the same bucket as above.
    top_sector = max(sector_totals, key=sector_totals.get) if sector_totals else None
    top_sector_before_pct = round(sector_totals.get(top_sector, 0.0) / total_w * 100, 1) if top_sector else None
    top_sector_after_pct = _sector_pct_after(top_sector) if top_sector else None

    _BETA_BOUND = 5.0
    beta: Optional[float] = None
    if _BENCHMARK in returns.columns:
        bench_var = returns[_BENCHMARK].var()
        if bench_var and bench_var > 0:
            b = round(float(returns[ticker].cov(returns[_BENCHMARK]) / bench_var), 2)
            if abs(b) <= _BETA_BOUND:
                beta = b

    # Risk-adjusted return: candidate vs. the current holding with the
    # weakest APEX conviction (lowest composite_score among holdings that
    # have a recent prediction) — the "marginal position" it would compete with.
    latest_preds = get_all_latest_predictions()
    weakest_ticker: Optional[str] = None
    weakest_score: Optional[float] = None
    for t in holding_tickers:
        pred = latest_preds.get(t)
        score = getattr(pred, "composite_score", None) if pred else None
        if score is not None and (weakest_score is None or score < weakest_score):
            weakest_score = score
            weakest_ticker = t

    weakest_holding: Optional[dict] = None
    if weakest_ticker and weakest_ticker in returns.columns:
        weakest_holding = {
            "ticker": weakest_ticker,
            "risk_adjusted_return": _risk_adjusted_return(
                latest_preds.get(weakest_ticker), _annualized_vol(returns[weakest_ticker])
            ),
        }

    candidate_risk_adjusted_return = _risk_adjusted_return(
        latest_preds.get(ticker), _annualized_vol(returns[ticker])
    )

    return {
        "correlation_with_portfolio": w_avg_corr,
        "candidate_sector": candidate_sector,
        "candidate_sector_before_pct": candidate_sector_before_pct,
        "candidate_sector_after_pct": candidate_sector_after_pct,
        "top_sector": top_sector,
        "top_sector_before_pct": top_sector_before_pct,
        "top_sector_after_pct": top_sector_after_pct,
        "beta_vs_spy": beta,
        "weakest_holding": weakest_holding,
        "candidate_risk_adjusted_return": candidate_risk_adjusted_return,
        # Latest close for the candidate — lets callers convert a $ allocation to
        # shares without a second yfinance fetch (see portfolio_optimizer.py).
        "candidate_price": float(closes[ticker].iloc[-1]),
    }


_FIT_CORR_MAX_POINTS = 40.0
_FIT_CORR_NEUTRAL = 0.6          # correlation at/above this earns no diversification points
_FIT_CORR_RANGE = 1.2            # points scale linearly from _FIT_CORR_NEUTRAL down to -0.6
_FIT_CONCENTRATION_MAX_POINTS = 30.0
_FIT_CONCENTRATION_SCALE = 15.0  # percentage-point relief -> points
_FIT_RISK_ADJUSTED_MAX_POINTS = 30.0
_FIT_RISK_ADJUSTED_CENTER = 15.0  # parity with the weakest holding
_FIT_RISK_ADJUSTED_SCALE = 100.0


def compute_portfolio_fit_score(ticker: str, holdings: list[dict], position_pct: float = 0.02) -> Optional[float]:
    """
    Standalone 0-100 "how well does this ticker fit the portfolio" score — a
    full-range companion to opportunity_engine's small 0-15 fit *bonus* (which
    stays as-is; this is a separate, independently-legible metric for the
    scoring-calibration snapshot, not a replacement).

    Works uniformly whether *ticker* is already a holding or a new candidate —
    compute_hypothetical_addition_impact excludes the ticker from its own
    comparison set, so "fit" always means "vs. the rest of the portfolio."

    Three components, each contributing points independently (not multiplied):
      - correlation (0-40): lower correlation to current holdings scores higher
      - sector-concentration relief (0-30): how much this dilutes the portfolio's
        most-concentrated sector (0 if it doesn't, never negative)
      - risk-adjusted return vs. the weakest current holding (0-30, centered at
        15 = parity; missing data defaults to the neutral center, not 0)

    Returns None if compute_hypothetical_addition_impact can't compute anything
    (same degrade-gracefully contract as the rest of this module).
    """
    impact = compute_hypothetical_addition_impact(ticker, holdings, position_pct=position_pct)
    if impact is None:
        return None

    corr = impact.get("correlation_with_portfolio")
    corr_points = (
        min(_FIT_CORR_MAX_POINTS, max(0.0, (_FIT_CORR_NEUTRAL - corr) * (_FIT_CORR_MAX_POINTS / _FIT_CORR_RANGE)))
        if corr is not None else _FIT_CORR_MAX_POINTS / 2
    )

    top_before = impact.get("top_sector_before_pct")
    top_after = impact.get("top_sector_after_pct")
    concentration_points = 0.0
    if top_before is not None and top_after is not None and top_before > top_after:
        relief = top_before - top_after
        concentration_points = min(_FIT_CONCENTRATION_MAX_POINTS, relief * _FIT_CONCENTRATION_SCALE)

    weakest = impact.get("weakest_holding") or {}
    candidate_rar = impact.get("candidate_risk_adjusted_return")
    weakest_rar = weakest.get("risk_adjusted_return")
    if candidate_rar is not None and weakest_rar is not None:
        diff = candidate_rar - weakest_rar
        risk_adjusted_points = min(
            _FIT_RISK_ADJUSTED_MAX_POINTS,
            max(0.0, _FIT_RISK_ADJUSTED_CENTER + diff * _FIT_RISK_ADJUSTED_SCALE),
        )
    else:
        risk_adjusted_points = _FIT_RISK_ADJUSTED_CENTER

    return round(min(100.0, corr_points + concentration_points + risk_adjusted_points), 1)
