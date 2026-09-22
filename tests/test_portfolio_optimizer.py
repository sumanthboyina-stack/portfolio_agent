from __future__ import annotations

from portfolio_agent.tools.portfolio_optimizer import (
    _ISSUER_CONCENTRATION_THRESHOLD,
    _MAX_PER_CANDIDATE_PCT_OF_CASH,
    _MAX_POST_TRADE_POSITION_PCT,
    _SECTOR_CONCENTRATION_THRESHOLD,
    _allocate_cash,
    _overweight_penalty,
    _reduce_candidates,
)


def test_overweight_penalty_defaults_match_module_constants():
    assert _overweight_penalty(30.0, 20.0) == _overweight_penalty(
        30.0, 20.0, _SECTOR_CONCENTRATION_THRESHOLD, _ISSUER_CONCENTRATION_THRESHOLD
    )
    assert _overweight_penalty(24.0, 14.0) == 0.0


def test_overweight_penalty_respects_custom_thresholds():
    # 30% sector is over the 25% default but under a 40% user mandate.
    assert _overweight_penalty(30.0, None) > 0.0
    assert _overweight_penalty(30.0, None, sector_threshold=40.0) == 0.0
    assert _overweight_penalty(None, 20.0, issuer_threshold=30.0) == 0.0


def _candidates():
    return [
        {"ticker": "AAA", "kind": "new", "score": 80.0, "why": "", "composite_score": 8, "recommendation": "BUY"},
        {"ticker": "BBB", "kind": "new", "score": 60.0, "why": "", "composite_score": 7, "recommendation": "BUY"},
    ]


def test_allocate_cash_defaults_match_module_constants():
    default = _allocate_cash(_candidates(), 10_000.0, None)
    explicit = _allocate_cash(
        _candidates(), 10_000.0, None,
        max_per_candidate_pct_of_cash=_MAX_PER_CANDIDATE_PCT_OF_CASH,
        max_post_trade_position_pct=_MAX_POST_TRADE_POSITION_PCT,
    )
    assert default == explicit
    # 40% cap: AAA's proportional share (57%) is capped to $4,000
    assert default[0][0]["amount"] == 4000.0


def test_allocate_cash_respects_custom_per_candidate_cap():
    allocations, _ = _allocate_cash(
        _candidates(), 10_000.0, None, max_per_candidate_pct_of_cash=0.2,
    )
    assert all(a["amount"] <= 2000.0 for a in allocations)


def test_allocate_cash_respects_custom_post_trade_position_cap():
    baseline = {"total_value": 90_000.0}
    loose, _ = _allocate_cash(_candidates(), 10_000.0, baseline, max_post_trade_position_pct=0.5)
    tight, _ = _allocate_cash(_candidates(), 10_000.0, baseline, max_post_trade_position_pct=0.02)
    # 2% of $100k post-trade = $2,000 cap per position
    assert all(a["amount"] <= 2000.0 for a in tight)
    assert sum(a["amount"] for a in loose) > sum(a["amount"] for a in tight)


def test_reduce_candidates_uses_thresholds():
    holdings = [{"ticker": "AAA", "shares": 10}]
    risk_ctx = {"AAA": {"sector_concentration_pct": 30.0, "issuer_concentration_pct": 5.0}}
    prices = {"AAA": 100.0}

    default = _reduce_candidates(holdings, risk_ctx, {}, prices)
    assert len(default) == 1 and ">25% threshold" in default[0]["rationale"]

    relaxed = _reduce_candidates(holdings, risk_ctx, {}, prices, sector_threshold=40.0)
    assert relaxed == []

    strict = _reduce_candidates(holdings, risk_ctx, {}, prices, issuer_threshold=4.0)
    assert any(">4% threshold" in r["rationale"] for r in strict)
