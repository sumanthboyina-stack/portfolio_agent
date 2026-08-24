from __future__ import annotations

import pytest

from portfolio_agent.tools.valuation_engine import (
    DEFAULT_REVENUE_GROWTH,
    _current_fcf_margin,
    _revenue_growth_trend,
    compute_wacc,
    relative_valuation,
    run_dcf,
    run_dcf_scenarios,
    sensitivity_grid,
)

_REVENUE_HISTORY = [
    {"period": "2025-09-27", "value": 416_161_000_000},
    {"period": "2024-09-28", "value": 391_035_000_000},
    {"period": "2023-09-30", "value": 383_285_000_000},
    {"period": "2022-09-24", "value": 394_328_000_000},
]
_OPERATING_CF = [{"period": "2025-09-27", "value": 111_482_000_000}]
_CAPEX = [{"period": "2025-09-27", "value": 12_715_000_000}]
_MULTIPLES = {
    "market_cap": 4_500_000_000_000.0,
    "shares_outstanding": 14_600_000_000.0,
    "total_debt": 84_000_000_000.0,
    "total_cash": 62_000_000_000.0,
    "beta": 1.09,
    "current_price": 309.35,
}
_RISK_FREE_RATE_PCT = 4.2  # percentage number, matching FRED's DGS10 convention


def test_revenue_growth_trend_uses_yoy_average():
    trend = _revenue_growth_trend(_REVENUE_HISTORY)
    # oldest->newest: 394.3B -> 383.3B -> 391.0B -> 416.2B: mixed sign, modest average
    assert -0.05 < trend < 0.10


def test_revenue_growth_trend_falls_back_with_insufficient_history():
    assert _revenue_growth_trend([{"period": "2025-01-01", "value": 100}]) == DEFAULT_REVENUE_GROWTH
    assert _revenue_growth_trend([]) == DEFAULT_REVENUE_GROWTH


def test_current_fcf_margin_matches_operating_cf_minus_capex_over_revenue():
    margin = _current_fcf_margin(_REVENUE_HISTORY, _OPERATING_CF, _CAPEX)
    expected = (111_482_000_000 - 12_715_000_000) / 416_161_000_000
    assert margin == pytest.approx(expected)


def test_compute_wacc_blends_by_capital_structure_and_flags_debt_cost_as_approximate():
    result = compute_wacc(market_cap=1000.0, total_debt=0.0, beta=1.0, risk_free_rate_pct=4.0)
    assert result["weight_equity"] == pytest.approx(1.0)
    assert result["cost_of_debt_is_approximate"] is True
    # All-equity WACC should equal cost of equity exactly
    assert result["wacc"] == pytest.approx(result["cost_of_equity"])


def test_compute_wacc_higher_beta_raises_cost_of_equity():
    low_beta = compute_wacc(1000.0, 0.0, beta=0.5, risk_free_rate_pct=4.0)
    high_beta = compute_wacc(1000.0, 0.0, beta=2.0, risk_free_rate_pct=4.0)
    assert high_beta["cost_of_equity"] > low_beta["cost_of_equity"]


def test_run_dcf_returns_none_without_minimum_inputs():
    assert run_dcf([], _OPERATING_CF, _CAPEX, _MULTIPLES, _RISK_FREE_RATE_PCT) is None
    assert run_dcf(_REVENUE_HISTORY, _OPERATING_CF, _CAPEX, {}, _RISK_FREE_RATE_PCT) is None


def test_run_dcf_produces_positive_intrinsic_value():
    result = run_dcf(_REVENUE_HISTORY, _OPERATING_CF, _CAPEX, _MULTIPLES, _RISK_FREE_RATE_PCT)
    assert result is not None
    assert result["intrinsic_value_per_share"] > 0
    assert result["assumptions"]["cost_of_debt_is_approximate"] is True


def test_run_dcf_scenarios_orders_bear_base_bull_and_matches_margin_of_safety_formula():
    scenarios = run_dcf_scenarios(_REVENUE_HISTORY, _OPERATING_CF, _CAPEX, _MULTIPLES, _RISK_FREE_RATE_PCT)
    assert scenarios is not None
    # Bull uses higher growth + margin + lower WACC than bear -> strictly higher intrinsic value
    assert scenarios["intrinsic_bull"] > scenarios["intrinsic_base"] > scenarios["intrinsic_bear"]

    expected_mos = round(
        (scenarios["intrinsic_base"] - scenarios["current_price"]) / scenarios["intrinsic_base"] * 100, 1
    )
    assert scenarios["margin_of_safety_pct"] == pytest.approx(expected_mos)

    expected_ev = round(
        scenarios["probability_bear"] * scenarios["intrinsic_bear"]
        + scenarios["probability_base"] * scenarios["intrinsic_base"]
        + scenarios["probability_bull"] * scenarios["intrinsic_bull"],
        2,
    )
    assert scenarios["expected_value"] == pytest.approx(expected_ev)


def test_run_dcf_scenarios_probabilities_sum_to_one_by_default():
    scenarios = run_dcf_scenarios(_REVENUE_HISTORY, _OPERATING_CF, _CAPEX, _MULTIPLES, _RISK_FREE_RATE_PCT)
    total_prob = (
        scenarios["probability_bear"] + scenarios["probability_base"] + scenarios["probability_bull"]
    )
    assert total_prob == pytest.approx(1.0)


def test_valuation_label_thresholds():
    # current_price far below intrinsic base -> UNDERVALUED
    cheap = dict(_MULTIPLES, current_price=50.0)
    scenarios = run_dcf_scenarios(_REVENUE_HISTORY, _OPERATING_CF, _CAPEX, cheap, _RISK_FREE_RATE_PCT)
    assert scenarios["valuation_label"] == "UNDERVALUED"

    # current_price far above intrinsic base -> OVERVALUED
    expensive = dict(_MULTIPLES, current_price=5000.0)
    scenarios = run_dcf_scenarios(_REVENUE_HISTORY, _OPERATING_CF, _CAPEX, expensive, _RISK_FREE_RATE_PCT)
    assert scenarios["valuation_label"] == "OVERVALUED"


def test_sensitivity_grid_is_monotonic_in_wacc_and_terminal_growth():
    grid = sensitivity_grid(_REVENUE_HISTORY, _OPERATING_CF, _CAPEX, _MULTIPLES, _RISK_FREE_RATE_PCT)
    assert grid is not None
    values = grid["values"]
    # Higher WACC (later rows) -> lower intrinsic value at the same terminal-growth column
    for col in range(len(grid["terminal_growth_deltas_pp"])):
        column = [row[col] for row in values]
        assert column == sorted(column, reverse=True)
    # Higher terminal growth (later columns) -> higher intrinsic value in the same row
    for row in values:
        assert row == sorted(row)


def test_relative_valuation_computes_fcf_yield_and_peer_average():
    multiples = {**_MULTIPLES, "forward_pe": 30.0, "free_cashflow": 100.0, "market_cap": 1000.0}
    peers = [{"forward_pe": 20.0}, {"forward_pe": 10.0}]
    result = relative_valuation(multiples, peers, market_multiples=None)
    assert result["ticker_multiples"]["fcf_yield_pct"] == pytest.approx(10.0)
    assert result["peer_avg_multiples"]["forward_pe"] == pytest.approx(15.0)
    assert result["market_multiples"]["forward_pe_is_fallback"] is True
