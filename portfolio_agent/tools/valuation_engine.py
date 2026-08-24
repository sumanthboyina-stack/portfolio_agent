"""
Valuation engine — DCF intrinsic value (with bear/base/bull scenarios and a
WACC/terminal-growth sensitivity grid) and relative valuation vs peers/market.

Pure Python/math — no LLM, no new dependencies. Same philosophy as
screener.py / weight_engine.py / portfolio_risk.py: deterministic and
auditable. Every assumption used (WACC components, growth fade, margin) is
returned alongside the result, not hidden — including the two places this
model necessarily approximates because the data doesn't exist anywhere in
this system (cost of debt has no real bond yield to anchor to; "historical"
P/E has no historical EPS-by-period series) — both are labeled `approximate`
in their output rather than presented as precise.

Inputs come from two sources that don't overlap:
  - edgar.get_fundamentals_bundle(...) — ~4yr annual revenue history (for the
    growth trend) and operating_cf/capex (for a same-source-consistent FCF margin)
  - yfinance_tools.get_valuation_multiples(...) — market cap, shares outstanding,
    debt, cash, beta, multiples (EDGAR has none of these)

Units: FRED's DGS10 (risk-free rate input) is a percentage number (e.g. 4.25
meaning 4.25%), not a decimal fraction — converted once at the WACC boundary.
"""

from __future__ import annotations

from typing import Optional

EQUITY_RISK_PREMIUM = 0.05      # CAPM market risk premium (documented assumption)
CREDIT_SPREAD_PROXY = 0.0175    # added to risk-free rate to approximate cost of debt —
                                 # no real bond-yield/interest-expense data exists in this
                                 # system, so this is explicitly an approximation, not a fact
TAX_RATE = 0.21                 # flat statutory rate (no effective-rate data available)
DEFAULT_TERMINAL_GROWTH = 0.025
DEFAULT_REVENUE_GROWTH = 0.05   # fallback when <2 years of revenue history exist
PROJECTION_YEARS = 5
_WACC_FLOOR_OVER_TERMINAL_GROWTH = 0.01  # WACC must exceed terminal growth by at least this much

MARKET_PE_FALLBACK = 20.0       # long-run S&P 500 average, used only if SPY's own .info lacks it

_SCENARIOS = {
    # (growth_multiplier, margin_adj_pp, wacc_adj_pp)
    "bear": (0.5, -0.015, 0.010),
    "base": (1.0, 0.0, 0.0),
    "bull": (1.5, 0.015, -0.010),
}
_DEFAULT_PROBABILITIES = {"bear": 0.25, "base": 0.50, "bull": 0.25}


def _revenue_growth_trend(revenue_history: list[dict]) -> float:
    """Average YoY growth from EDGAR's ~4yr annual revenue series (newest first)."""
    values = [r["value"] for r in revenue_history if r.get("value")]
    if len(values) < 2:
        return DEFAULT_REVENUE_GROWTH
    values = list(reversed(values))  # oldest -> newest
    growth_rates = [
        (values[i] / values[i - 1] - 1) for i in range(1, len(values)) if values[i - 1]
    ]
    if not growth_rates:
        return DEFAULT_REVENUE_GROWTH
    return sum(growth_rates) / len(growth_rates)


def _current_fcf_margin(revenue_history: list[dict], operating_cf: list[dict], capex: list[dict]) -> Optional[float]:
    """FCF margin from the most recent EDGAR annual period — operating_cf and
    capex come from the same source as revenue, so the margin is internally
    consistent rather than blending EDGAR revenue with a yfinance FCF figure."""
    rev = revenue_history[0]["value"] if revenue_history else None
    ocf = operating_cf[0]["value"] if operating_cf else None
    cpx = capex[0]["value"] if capex else None
    if not rev or ocf is None or cpx is None:
        return None
    return (ocf - abs(cpx)) / rev


def _project_fcf(
    current_revenue: float, trend_growth: float, fcf_margin: float,
    terminal_growth: float, years: int = PROJECTION_YEARS,
) -> list[float]:
    """
    Fades trend_growth linearly toward terminal_growth over the projection
    window and applies a flat fcf_margin (the assumption, not hidden) to get
    projected FCF per year.
    """
    projected = []
    revenue = current_revenue
    for y in range(1, years + 1):
        fade = (y - 1) / max(1, years - 1)  # 0 in year 1 -> 1 in the final year
        growth = trend_growth + (terminal_growth - trend_growth) * fade
        revenue = revenue * (1 + growth)
        projected.append(revenue * fcf_margin)
    return projected


def compute_wacc(
    market_cap: float, total_debt: Optional[float], beta: Optional[float],
    risk_free_rate_pct: float, wacc_adj: float = 0.0,
) -> dict:
    """
    CAPM cost of equity + approximated cost of debt, blended by capital
    structure weight. risk_free_rate_pct is a percentage number (e.g. 4.25),
    matching FRED's DGS10 convention — converted to a decimal here.

    Returns the full component breakdown (not just the final number) so the
    result is auditable, including cost_of_debt_is_approximate: True, since
    no real bond-yield or interest-expense data exists anywhere in this system.
    """
    rf = risk_free_rate_pct / 100.0
    beta = beta if beta is not None else 1.0
    total_debt = total_debt or 0.0

    cost_of_equity = rf + beta * EQUITY_RISK_PREMIUM
    cost_of_debt_pretax = rf + CREDIT_SPREAD_PROXY
    cost_of_debt_after_tax = cost_of_debt_pretax * (1 - TAX_RATE)

    total_capital = market_cap + total_debt
    weight_equity = market_cap / total_capital if total_capital else 1.0
    weight_debt = 1 - weight_equity

    wacc = weight_equity * cost_of_equity + weight_debt * cost_of_debt_after_tax + wacc_adj
    return {
        "wacc": round(wacc, 4),
        "cost_of_equity": round(cost_of_equity, 4),
        "cost_of_debt_after_tax": round(cost_of_debt_after_tax, 4),
        "cost_of_debt_is_approximate": True,
        "risk_free_rate": round(rf, 4),
        "beta_used": beta,
        "weight_equity": round(weight_equity, 3),
        "weight_debt": round(weight_debt, 3),
        "tax_rate": TAX_RATE,
    }


def run_dcf(
    revenue_history: list[dict], operating_cf: list[dict], capex: list[dict],
    multiples: dict, risk_free_rate_pct: float,
    growth_multiplier: float = 1.0, margin_adj: float = 0.0, wacc_adj: float = 0.0,
    terminal_growth: float = DEFAULT_TERMINAL_GROWTH,
) -> Optional[dict]:
    """
    One DCF run. growth_multiplier/margin_adj/wacc_adj are how bear/base/bull
    scenarios perturb this same engine (see run_dcf_scenarios) rather than
    three unrelated code paths.

    Returns None if the minimum required inputs (current revenue, FCF margin,
    market cap, shares outstanding) aren't available.
    """
    if not revenue_history:
        return None
    current_revenue = revenue_history[0]["value"]
    fcf_margin = _current_fcf_margin(revenue_history, operating_cf, capex)
    market_cap = multiples.get("market_cap")
    shares_outstanding = multiples.get("shares_outstanding")
    if not current_revenue or fcf_margin is None or not market_cap or not shares_outstanding:
        return None

    trend_growth = _revenue_growth_trend(revenue_history) * growth_multiplier
    scenario_margin = fcf_margin + margin_adj

    wacc_result = compute_wacc(
        market_cap, multiples.get("total_debt"), multiples.get("beta"),
        risk_free_rate_pct, wacc_adj=wacc_adj,
    )
    wacc = max(wacc_result["wacc"], terminal_growth + _WACC_FLOOR_OVER_TERMINAL_GROWTH)

    projected_fcf = _project_fcf(current_revenue, trend_growth, scenario_margin, terminal_growth)

    pv_fcf = sum(fcf / (1 + wacc) ** y for y, fcf in enumerate(projected_fcf, start=1))
    terminal_value = projected_fcf[-1] * (1 + terminal_growth) / (wacc - terminal_growth)
    pv_terminal = terminal_value / (1 + wacc) ** len(projected_fcf)

    enterprise_value = pv_fcf + pv_terminal
    equity_value = enterprise_value - (multiples.get("total_debt") or 0.0) + (multiples.get("total_cash") or 0.0)
    intrinsic_value_per_share = equity_value / shares_outstanding

    return {
        "intrinsic_value_per_share": round(intrinsic_value_per_share, 2),
        "enterprise_value": round(enterprise_value, 0),
        "equity_value": round(equity_value, 0),
        "assumptions": {
            "trend_revenue_growth": round(trend_growth, 4),
            "fcf_margin": round(scenario_margin, 4),
            "terminal_growth": terminal_growth,
            "projection_years": len(projected_fcf),
            **wacc_result,
        },
    }


def run_dcf_scenarios(
    revenue_history: list[dict], operating_cf: list[dict], capex: list[dict],
    multiples: dict, risk_free_rate_pct: float,
    probabilities: Optional[dict] = None,
) -> Optional[dict]:
    """
    Bear/Base/Bull DCF — deliberately not a single point estimate. Returns
    None if the base case can't be computed (see run_dcf's requirements).
    """
    probabilities = probabilities or _DEFAULT_PROBABILITIES
    results = {}
    for scenario, (growth_mult, margin_adj, wacc_adj) in _SCENARIOS.items():
        results[scenario] = run_dcf(
            revenue_history, operating_cf, capex, multiples, risk_free_rate_pct,
            growth_multiplier=growth_mult, margin_adj=margin_adj, wacc_adj=wacc_adj,
        )
    if results["base"] is None:
        return None
    # If a perturbed scenario fails (e.g. missing inputs mid-calc) fall back to
    # base rather than dropping the scenario silently from the expected value.
    for scenario in ("bear", "bull"):
        if results[scenario] is None:
            results[scenario] = results["base"]

    current_price = multiples.get("current_price")
    base_intrinsic = results["base"]["intrinsic_value_per_share"]
    expected_value = round(sum(
        probabilities[s] * results[s]["intrinsic_value_per_share"] for s in _SCENARIOS
    ), 2)

    margin_of_safety_pct = (
        round((base_intrinsic - current_price) / base_intrinsic * 100, 1)
        if current_price and base_intrinsic else None
    )
    if margin_of_safety_pct is None:
        valuation_label = None
    elif margin_of_safety_pct > 10:
        valuation_label = "UNDERVALUED"
    elif margin_of_safety_pct < -10:
        valuation_label = "OVERVALUED"
    else:
        valuation_label = "FAIRLY_VALUED"

    return {
        "current_price": current_price,
        "intrinsic_bear": results["bear"]["intrinsic_value_per_share"],
        "intrinsic_base": base_intrinsic,
        "intrinsic_bull": results["bull"]["intrinsic_value_per_share"],
        "probability_bear": probabilities["bear"],
        "probability_base": probabilities["base"],
        "probability_bull": probabilities["bull"],
        "expected_value": expected_value,
        "margin_of_safety_pct": margin_of_safety_pct,
        "valuation_label": valuation_label,
        "assumptions": {s: results[s]["assumptions"] for s in _SCENARIOS},
    }


def sensitivity_grid(
    revenue_history: list[dict], operating_cf: list[dict], capex: list[dict],
    multiples: dict, risk_free_rate_pct: float,
    wacc_steps_pp: tuple[float, ...] = (-1.0, -0.5, 0.0, 0.5, 1.0),
    terminal_growth_steps_pp: tuple[float, ...] = (-0.5, -0.25, 0.0, 0.25, 0.5),
) -> Optional[dict]:
    """
    Intrinsic value at a small WACC x terminal-growth grid around the base
    case — cheap, reuses run_dcf. Returns None under the same conditions as
    run_dcf (base case unavailable).
    """
    base = run_dcf(revenue_history, operating_cf, capex, multiples, risk_free_rate_pct)
    if base is None:
        return None
    base_terminal_growth = base["assumptions"]["terminal_growth"]

    grid = []
    for wacc_step in wacc_steps_pp:
        row = []
        for tg_step in terminal_growth_steps_pp:
            result = run_dcf(
                revenue_history, operating_cf, capex, multiples, risk_free_rate_pct,
                wacc_adj=wacc_step / 100.0,
                terminal_growth=base_terminal_growth + tg_step / 100.0,
            )
            row.append(result["intrinsic_value_per_share"] if result else None)
        grid.append(row)

    return {
        "wacc_deltas_pp": list(wacc_steps_pp),
        "terminal_growth_deltas_pp": list(terminal_growth_steps_pp),
        "values": grid,
    }


def relative_valuation(multiples: dict, peers_multiples: list[dict], market_multiples: Optional[dict]) -> dict:
    """
    ticker's own multiples vs peer average, market, and an approximate
    historical band. peers_multiples/market_multiples are
    yfinance_tools.get_valuation_multiples() results for each peer / SPY.
    """
    _COMPARISON_KEYS = ["forward_pe", "ev_to_ebitda", "ev_to_revenue", "price_to_sales", "peg_ratio"]

    fcf = multiples.get("free_cashflow")
    mcap = multiples.get("market_cap")
    ticker_multiples = {k: multiples.get(k) for k in _COMPARISON_KEYS}
    ticker_multiples["fcf_yield_pct"] = round(fcf / mcap * 100, 2) if fcf and mcap else None

    peer_avg: dict[str, Optional[float]] = {}
    for k in _COMPARISON_KEYS:
        vals = [p[k] for p in peers_multiples if p.get(k) is not None]
        peer_avg[k] = round(sum(vals) / len(vals), 2) if vals else None

    market: dict[str, Optional[float]] = {}
    for k in _COMPARISON_KEYS:
        v = market_multiples.get(k) if market_multiples else None
        market[k] = v
    if market.get("forward_pe") is None:
        market["forward_pe"] = MARKET_PE_FALLBACK
        market["forward_pe_is_fallback"] = True

    low = multiples.get("fifty_two_week_low")
    high = multiples.get("fifty_two_week_high")
    eps = multiples.get("trailing_eps")
    historical_pe_range = (
        {"low": round(low / eps, 1), "high": round(high / eps, 1), "approximate": True}
        if low and high and eps and eps > 0 else None
    )

    return {
        "ticker_multiples": ticker_multiples,
        "peer_avg_multiples": peer_avg,
        "peers_used": [p.get("ticker") for p in peers_multiples],
        "market_multiples": market,
        "approx_historical_pe_range": historical_pe_range,
    }
