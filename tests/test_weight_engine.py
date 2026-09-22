from __future__ import annotations

import pytest

from portfolio_agent.tools.weight_engine import (
    BASE,
    DEFAULT_BASE_WEIGHTS,
    HORIZON_BASE_WEIGHTS,
    WEIGHT_FLOOR,
    _fundamentals_penalty,
    _macro_signal,
    _news_signal,
    _normalize,
    _valuation_penalty,
    compute_dynamic_weights,
    compute_dynamic_weights_for_horizon,
)


def test_horizon_base_weights_sum_to_one():
    for horizon, weights in HORIZON_BASE_WEIGHTS.items():
        assert sum(weights.values()) == pytest.approx(1.0), horizon


def test_base_weights_sum_to_one():
    assert sum(BASE.values()) == pytest.approx(1.0)


def test_default_base_weights_sum_to_one():
    assert sum(DEFAULT_BASE_WEIGHTS.values()) == pytest.approx(1.0)


def test_valuation_weight_share_increases_with_horizon():
    # A DCF says little about a 5-day move and a lot about a 250-day one.
    horizons = sorted(HORIZON_BASE_WEIGHTS.keys())
    shares = [HORIZON_BASE_WEIGHTS[h]["valuation"] for h in horizons]
    assert shares == sorted(shares)


def test_normalize_sums_to_one_without_floor():
    raw = {"fundamentals": 0.4, "research": 0.3, "macro": 0.2, "news": 0.1}
    weights = _normalize(raw, use_floor=False)
    assert sum(weights.values()) == pytest.approx(1.0, abs=1e-3)


def test_normalize_applies_floor_to_zeroed_source():
    # The floor clamps raw (pre-normalization) values, so it holds in the final
    # weights as long as no single source swamps the total after clamping.
    raw = {"fundamentals": 0.3, "research": 0.0, "macro": 0.3, "news": 0.3}
    weights = _normalize(raw, use_floor=True)
    assert weights["research"] >= WEIGHT_FLOOR
    assert sum(weights.values()) == pytest.approx(1.0, abs=1e-3)


def test_normalize_floor_is_pre_normalization_only():
    # NOTE: WEIGHT_FLOOR clamps raw inputs before the division, it does not
    # guarantee a post-normalization minimum share. When one source dominates
    # the raw total, a floored-to-0.05 source can still normalize below 0.05
    # (here: 0.05 / 1.15 ~= 0.043). This is documenting current behavior, not
    # asserting it's the desired contract.
    raw = {"fundamentals": 1.0, "research": 0.0, "macro": 0.0, "news": 0.0}
    weights = _normalize(raw, use_floor=True)
    assert weights["research"] < WEIGHT_FLOOR
    assert sum(weights.values()) == pytest.approx(1.0, abs=1e-3)


def test_news_signal_no_data_returns_none():
    boost, event, penalty, _ = _news_signal([])
    assert event == "NONE"
    assert boost == 0.0


def test_news_signal_no_data_applies_real_penalty():
    # Regression test: missing news used to only skip the boost (leaving the
    # base weight fully intact) despite the rationale claiming "minimum
    # weight" -- it must now actually apply a nonzero penalty so the raw
    # weight shrinks, matching how fundamentals/research already behave.
    _, _, penalty, _ = _news_signal([])
    assert penalty > 0.0


def test_news_signal_detects_earnings_release():
    news = [{"date": "2099-01-01", "headline_1": "Company beat estimates", "headline_2": "", "summary": ""}]
    boost, event, penalty, _ = _news_signal(news)
    assert event == "EARNINGS_RELEASE"
    assert boost > 0
    assert penalty == 0.0


def test_news_signal_detects_leadership_change():
    news = [{"date": "2099-01-01", "headline_1": "CEO resigns amid controversy", "headline_2": "", "summary": ""}]
    boost, event, penalty, _ = _news_signal(news)
    assert event == "LEADERSHIP_CHANGE"


def test_macro_signal_extreme_volatility():
    boost, event, _ = _macro_signal({"vix": 35})
    assert event == "EXTREME_VOLATILITY"
    assert boost > 0


def test_macro_signal_elevated_volatility():
    boost, event, _ = _macro_signal({"vix": 25})
    assert event == "ELEVATED_VOLATILITY"


def test_macro_signal_neutral():
    boost, event, _ = _macro_signal({"vix": 15, "macro_regime_hint": "NEUTRAL"})
    assert event == "NEUTRAL"
    assert boost == 0.0


def test_macro_signal_no_data():
    boost, event, _ = _macro_signal(None)
    assert event == "NO_DATA"
    assert boost == 0.0


def test_fundamentals_penalty_fresh_data():
    from datetime import date
    penalty, _ = _fundamentals_penalty({"filing_date": date.today().isoformat()}, "NONE")
    assert penalty == 0.0


def test_fundamentals_penalty_stale_data():
    penalty, _ = _fundamentals_penalty({"filing_date": "2000-01-01"}, "NONE")
    assert penalty > 0.0


def test_fundamentals_penalty_missing_data():
    penalty, _ = _fundamentals_penalty(None, "NONE")
    assert penalty > 0.0


def test_fundamentals_penalty_heavy_during_earnings_without_fresh_filing():
    penalty, _ = _fundamentals_penalty({"filing_date": "2000-01-01"}, "EARNINGS_RELEASE")
    assert penalty > _fundamentals_penalty({"filing_date": "2000-01-01"}, "NONE")[0]


def test_valuation_penalty_fresh_data():
    from datetime import date
    penalty, _ = _valuation_penalty({"as_of_date": date.today().isoformat()}, "NONE")
    assert penalty == 0.0


def test_valuation_penalty_stale_data():
    penalty, _ = _valuation_penalty({"as_of_date": "2000-01-01"}, "NONE")
    assert penalty > 0.0


def test_valuation_penalty_missing_data():
    penalty, _ = _valuation_penalty(None, "NONE")
    assert penalty > 0.0


def test_valuation_penalty_heavy_during_earnings_without_fresh_dcf():
    penalty, _ = _valuation_penalty({"as_of_date": "2000-01-01"}, "EARNINGS_RELEASE")
    assert penalty > _valuation_penalty({"as_of_date": "2000-01-01"}, "NONE")[0]


def test_compute_dynamic_weights_for_horizon_sums_to_one_and_respects_floor():
    for horizon in HORIZON_BASE_WEIGHTS:
        weights = compute_dynamic_weights_for_horizon(
            "AAPL", horizon, db_context={}, macro_snapshot={}
        )
        assert sum(weights.values()) == pytest.approx(1.0, abs=1e-3)
        assert all(v >= WEIGHT_FLOOR - 1e-9 for v in weights.values())


def test_compute_dynamic_weights_legacy_includes_all_horizons_when_requested(tmp_path, monkeypatch):
    # weights_by_horizon is filtered by user_profile.preferred_horizons, so read
    # from a fresh temp DB (default = all horizons) rather than the live profile.
    import portfolio_agent.tools.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    result = compute_dynamic_weights(
        "AAPL",
        news_data=[],
        research_data=None,
        macro_snapshot=None,
        fundamentals_data=None,
        horizons=list(HORIZON_BASE_WEIGHTS.keys()),
    )
    assert sum(result["weights"].values()) == pytest.approx(1.0, abs=1e-3)
    assert set(result["weights_by_horizon"].keys()) == set(HORIZON_BASE_WEIGHTS.keys())
    

# ── preferred_horizons filter (display/generation filter, not weighting math) ──

def test_all_horizons_filtered_to_preferred_without_changing_weights(tmp_path, monkeypatch):
    import portfolio_agent.tools.db as db_module
    from portfolio_agent.tools.user_profile_db import update_user_profile
    from portfolio_agent.tools.weight_engine import (
        compute_dynamic_weights_all_horizons, filter_preferred_horizons,
    )
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")

    ctx = {"news": [], "research": None, "fundamentals": None, "valuation": None}
    full = compute_dynamic_weights_all_horizons("AAPL", [5, 21, 63, 250], ctx, {})
    assert set(full) == {5, 21, 63, 250}   # default preference = all → unchanged

    update_user_profile(preferred_horizons=["5d", "63d"])
    assert filter_preferred_horizons([5, 21, 63, 250]) == [5, 63]
    filtered = compute_dynamic_weights_all_horizons("AAPL", [5, 21, 63, 250], ctx, {})
    assert set(filtered) == {5, 63}
    for h in (5, 63):
        assert filtered[h] == full[h]      # weights for remaining horizons untouched

    result = compute_dynamic_weights("AAPL", [], None, None, None, horizons=[5, 21, 63])
    assert set(result["weights_by_horizon"]) == {5, 63}
    assert result["weights"] == compute_dynamic_weights("AAPL", [], None, None, None)["weights"]


def test_empty_preferred_horizons_means_no_filtering(tmp_path, monkeypatch):
    import portfolio_agent.tools.db as db_module
    from portfolio_agent.tools.user_profile_db import update_user_profile
    from portfolio_agent.tools.weight_engine import filter_preferred_horizons
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")

    update_user_profile(preferred_horizons=[])
    assert filter_preferred_horizons([5, 21]) == [5, 21]
