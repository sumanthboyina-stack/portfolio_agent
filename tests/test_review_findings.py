"""
Fixes for 4 findings from a manual code review of the phased work in this
session:

  1. Guardrail caps reached conviction_score but not composite_score, so a
     capped bounce/no-news call could still rank #1 in the Opportunity
     Engine (which sorts by composite_score, never conviction_score).
  2. Failure-pattern mining bucketed by the POST-cap conviction_score, so a
     capped call landed in the "low conviction" bucket and could never
     surface as an instance of the exact high-conviction failure pattern
     the mining is trying to catch.
  3. opportunity_engine.py and scoring_snapshot.py read `predictions` with
     no scope filter — safe today only because chat's private writes never
     set trigger_type, an accidental non-overlap, not a guarantee.
  4. tools.technical_features stored data (via risk_technical.py) that
     nothing else in the codebase ever read — including market_context.py,
     so APEX's own prompt never saw it.

Each is fixed in the module it was found in; see that module's diff/
docstring for the mechanism. This file tests the fixes directly.
"""
from __future__ import annotations

from datetime import date

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.tools import prediction_db as pdb


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")


NEWS = [{"headline": "steady", "sentiment": "NEUTRAL", "sentiment_score": 0.0,
        "published_at": "2026-09-20", "date": "2026-09-20"}]
RESEARCH = {"price_target_avg": 120.0, "current_price": 100.0, "num_analysts": 10, "as_of_date": "2026-09-15"}
MACRO = {"vix": 20.0, "regime": "NEUTRAL"}
FUND = {"as_of_date": "2026-08-01", "fundamental_score": 7}
VAL = {"as_of_date": "2026-09-01", "intrinsic_value": 110.0}


def _fake_loaders(**over):
    base = {"fundamentals": lambda t: FUND, "research": lambda t: RESEARCH, "news": lambda t: NEWS,
            "valuation": lambda t: VAL, "macro": lambda: MACRO, "history": lambda t: [], "failure_patterns": lambda: []}
    base.update(over)
    return base


# ── Finding 4: technical_features now reaches APEX's market context ──────────

def test_technical_features_reach_the_market_context_payload(monkeypatch):
    from portfolio_agent.tools.technical_features import get_latest_technical_features, store_technical_features
    result = {"bars_available": 250, "coverage": {"sma_200": True}, "features": {"sma_200": 150.0, "rsi_14": 55.0}}
    store_technical_features("AAPL", "2026-09-20", result)

    from portfolio_agent.tools.market_context import build_market_context
    ctx = build_market_context("AAPL", [5], loaders=_fake_loaders(technical=get_latest_technical_features),
                               private_terms=frozenset())
    assert ctx.payload["technical"]["features"]["sma_200"] == 150.0
    assert ctx.evidence_refs["technical_as_of"] == "2026-09-20"
    assert "technical_instruction" in ctx.payload


def test_missing_technical_features_is_explicit_not_silently_dropped(monkeypatch):
    from portfolio_agent.tools.market_context import build_market_context
    ctx = build_market_context("ZZZZ", [5], loaders=_fake_loaders(), private_terms=frozenset())
    assert ctx.payload["technical"] is None
    assert ctx.evidence_refs["technical_as_of"] is None
    assert "No stored technical indicators" in ctx.payload["technical_instruction"]


def test_custom_loaders_without_a_technical_key_still_work():
    """Backward compatible: every existing test file's custom `loaders=`
    override predates the "technical" key and must not break."""
    from portfolio_agent.tools.market_context import build_market_context
    ctx = build_market_context("AAPL", [5], loaders=_fake_loaders(), private_terms=frozenset())
    assert ctx.payload["technical"] is None   # no KeyError, just absent


def test_default_loaders_include_technical_and_wire_to_the_real_store():
    """The actual production path (no loaders= override): default_loaders()
    must include "technical", pointed at technical_features' real reader."""
    from portfolio_agent.tools.market_context import default_loaders
    from portfolio_agent.tools.technical_features import store_technical_features
    store_technical_features("AAPL", "2026-09-20", {"bars_available": 50, "coverage": {}, "features": {"rsi_14": 42.0}})

    L = default_loaders()
    assert "technical" in L
    assert L["technical"]("AAPL")["features"]["rsi_14"] == 42.0


def test_get_latest_technical_features_finds_the_most_recent_snapshot():
    from portfolio_agent.tools.technical_features import get_latest_technical_features, store_technical_features
    store_technical_features("AAPL", "2026-09-10", {"bars_available": 100, "coverage": {}, "features": {"rsi_14": 40.0}})
    store_technical_features("AAPL", "2026-09-20", {"bars_available": 110, "coverage": {}, "features": {"rsi_14": 60.0}})
    latest = get_latest_technical_features("AAPL")
    assert latest["data_snapshot"] == "2026-09-20" and latest["features"]["rsi_14"] == 60.0


def test_stale_technical_data_is_detected_as_a_reuse_staleness_signal(monkeypatch):
    """run_planner.plan_reuse (Phase 4) must treat a technical_features update
    the same way it already treats fundamentals/research/valuation updates."""
    from portfolio_agent.tools import run_planner as rp

    stored = {"policy_version": "v1", "guardrail_version": "g1",
             "evidence_refs": {"context_version": "market-context/1.1", "technical_as_of": "2026-09-10"}}
    current_stale = {"context_version": "market-context/1.1", "technical_as_of": "2026-09-20"}
    ok, reason, _ = rp.plan_reuse(latest_shared_row=stored, current_evidence_refs=current_stale,
                                  current_policy_version="v1", current_guardrail_version="g1")
    assert ok is False and "technical_as_of" in reason


# ── Finding 3: opportunity-engine / scoring-snapshot queries now scope-filtered ──

def _write_shared_opportunity_row(ticker="AAPL", composite_score=9.0, recommendation="STRONG_BUY", **over):
    from portfolio_agent.tools.market_context import build_market_context
    from portfolio_agent.tools.forecast_writer import ForecastProvenance, write_shared_forecast
    ctx = build_market_context(ticker, [5], loaders=_fake_loaders(), private_terms=frozenset())
    row = dict(ticker=ticker, prediction="BULLISH", recommendation=recommendation, horizon_days=5,
              reasoning="x", composite_score=composite_score, trigger_type="trending_opportunity")
    row.update(over)
    return write_shared_forecast(ctx, ForecastProvenance(origin="pipeline.apex", model_used="m"), **row)


def _write_private_row_with_trigger_type(ticker="AAPL", trigger_type="trending_opportunity", **over):
    """Simulates the 'fragile' scenario the review warned about: a hypothetical
    future private writer that (unlike today's actual chat call site) DOES pass
    a trigger_type. Nothing in production does this today -- the point is that
    the scope filter, not an accidental absence of this kwarg, is what keeps it out."""
    from portfolio_agent.tools.forecast_writer import ForecastProvenance, write_private_forecast
    row = dict(ticker=ticker, prediction="BULLISH", recommendation="STRONG_BUY", horizon_days=5,
              reasoning="private", composite_score=9.9, trigger_type=trigger_type)
    row.update(over)
    return write_private_forecast("alice", ForecastProvenance(origin="chat.ticker_prediction", model_used="m"), **row)


def test_opportunity_engine_excludes_a_private_row_even_with_a_matching_trigger_type():
    from portfolio_agent.tools.opportunity_engine import _latest_trending_opportunity_predictions
    _write_shared_opportunity_row("AAPL", composite_score=6.0)
    _write_private_row_with_trigger_type("MSFT")   # would rank #1 by composite_score if not scope-filtered

    rows = _latest_trending_opportunity_predictions()
    tickers = {r["ticker"] for r in rows}
    assert tickers == {"AAPL"}
    assert "MSFT" not in tickers


def test_opportunity_history_excludes_private_rows():
    from portfolio_agent.tools.opportunity_engine import get_opportunity_history
    _write_shared_opportunity_row("AAPL")
    _write_private_row_with_trigger_type("AAPL")

    history = get_opportunity_history("AAPL", window_days=30)
    assert history["times_identified"] == 1   # only the shared row counts


def test_scoring_snapshot_excludes_private_rows_even_without_a_trigger_type():
    """scoring_snapshot.py had NO filter at all (not even trigger_type) -- the
    most exposed of the four queries. A private row with today's date must
    not appear."""
    from portfolio_agent.pipeline.daily.scoring_snapshot import _todays_prediction_rows
    today = date.today().isoformat()
    _write_shared_opportunity_row("AAPL")
    from portfolio_agent.tools.forecast_writer import ForecastProvenance, write_private_forecast
    write_private_forecast("alice", ForecastProvenance(origin="chat.ticker_prediction", model_used="m"),
                           ticker="MSFT", prediction="BULLISH", recommendation="STRONG_BUY", horizon_days=5,
                           reasoning="private", composite_score=9.9)

    rows = _todays_prediction_rows(today)
    tickers = {r["ticker"] for r in rows}
    assert tickers == {"AAPL"}


# ── Findings 1 & 2: guardrail caps reach composite_score; raw conviction preserved for mining ──

def _fake_apex_pipeline(monkeypatch, *, composite_score, conviction_score, has_news=False, trailing_10d=None):
    from portfolio_agent.tools import market_context as market_context_mod
    from portfolio_agent.tools import reasoning_tools as rt_mod
    monkeypatch.setattr(market_context_mod, "default_loaders",
                        lambda: _fake_loaders(news=(lambda t: NEWS) if has_news else (lambda t: [])))
    monkeypatch.setattr(rt_mod, "get_macro_snapshot", lambda: '{"vix": 20.0}')

    from portfolio_agent.tools import yfinance_tools as yft
    monkeypatch.setattr(yft, "get_close", lambda t, d: 100.0)
    monkeypatch.setattr(yft, "get_trailing_return", lambda t, d: trailing_10d)

    async def _fake_run_apex(ticker, prompt):
        from portfolio_agent.tools.apex_dual_run import ModelResult, SinglePrediction
        data = {"prediction": "BULLISH", "recommendation": "STRONG_BUY", "confidence": 7,
                "composite_score": composite_score, "horizons": [
                    {"horizon_days": 5, "predicted_direction": "UP", "predicted_return_low": 1.0,
                    "predicted_return_high": 3.0, "conviction_score": conviction_score, "reasoning_text": "x"},
                ]}
        return ModelResult(merged=data, individuals=[SinglePrediction("m", "anthropic", "Claude", data, 10, True)],
                           model_sources=["Claude"], used_fallback=False)

    import portfolio_agent.tools.apex_dual_run as apex_dual_run_mod
    monkeypatch.setattr(apex_dual_run_mod, "run_apex", _fake_run_apex)


def test_guardrail_cap_reaches_composite_score_not_just_conviction(monkeypatch):
    """The core Finding 1 fix: a no-news bullish call with conviction 9 and
    composite_score 9 must not survive with composite_score untouched at 9 --
    that's exactly what let a capped bounce/no-news call still rank #1 by
    composite_score in the Opportunity Engine."""
    import asyncio
    import portfolio_agent.pipeline.daily.apex as apex_mod
    _fake_apex_pipeline(monkeypatch, composite_score=9.0, conviction_score=9.0, has_news=False, trailing_10d=0.01)

    asyncio.run(apex_mod._run_daily_apex(["AAPL"], scheduled_horizons_override=[5]))
    row = pdb.get_latest_prediction("AAPL", horizon_days=5, scopes=pdb.SHARED_SCOPES)
    assert row.conviction_score <= 5.0   # the existing cap, unchanged
    assert row.composite_score <= 5.0    # Finding 1: now ALSO capped -- this is what was missing
    assert row.guardrail_flags and "no_news_bullish_capped" in row.guardrail_flags


def test_guardrail_cap_preserves_raw_conviction_for_mining(monkeypatch):
    """Finding 2: the pre-cap conviction (9.0) must still be recoverable even
    though the displayed/stored conviction_score is capped to 5 -- otherwise
    this row silently vanishes from the 'high conviction' bucket pattern
    mining is trying to catch it in."""
    import asyncio
    import portfolio_agent.pipeline.daily.apex as apex_mod
    _fake_apex_pipeline(monkeypatch, composite_score=9.0, conviction_score=9.0, has_news=False, trailing_10d=0.01)

    asyncio.run(apex_mod._run_daily_apex(["AAPL"], scheduled_horizons_override=[5]))
    row = pdb.get_latest_prediction("AAPL", horizon_days=5, scopes=pdb.SHARED_SCOPES)
    assert row.conviction_score <= 5.0
    assert row.raw_conviction_score == 9.0   # recoverable despite the display cap


def test_uncapped_call_leaves_raw_conviction_unset(monkeypatch):
    """raw_conviction_score is None when nothing was capped -- not merely
    equal to conviction_score -- so a reader can tell "never capped" apart
    from "capped to exactly its own raw value" by coincidence."""
    import asyncio
    import portfolio_agent.pipeline.daily.apex as apex_mod
    _fake_apex_pipeline(monkeypatch, composite_score=7.0, conviction_score=6.0, has_news=True, trailing_10d=0.01)

    asyncio.run(apex_mod._run_daily_apex(["AAPL"], scheduled_horizons_override=[5]))
    row = pdb.get_latest_prediction("AAPL", horizon_days=5, scopes=pdb.SHARED_SCOPES)
    assert row.conviction_score == 6.0
    assert row.raw_conviction_score is None
    assert row.composite_score == 7.0   # not capped -- no guardrail fired


def test_pattern_mining_buckets_by_raw_conviction_not_the_capped_value():
    """The exact Finding 2 scenario: a capped high-conviction no-news bounce
    call (raw=9, capped display=5) must land in the mining's HIGH bucket, not
    LOW -- otherwise the very calls the guardrail exists to catch become
    invisible to the pattern miner that justifies it."""
    from portfolio_agent.tools.validation_engine import _conv_bucket

    capped_high = {"horizon_days": 5, "raw_conviction_score": 9.0, "conviction_score": 5.0}
    genuinely_low = {"horizon_days": 5, "raw_conviction_score": 3.0, "conviction_score": 3.0}
    never_capped = {"horizon_days": 5, "raw_conviction_score": None, "conviction_score": 8.0}
    missing_horizon = {"horizon_days": None, "raw_conviction_score": 9.0, "conviction_score": 9.0}

    assert _conv_bucket(capped_high) == ("conviction", 5, "high")
    assert _conv_bucket(genuinely_low) == ("conviction", 5, "low")
    assert _conv_bucket(never_capped) == ("conviction", 5, "high")   # falls back to conviction_score
    assert _conv_bucket(missing_horizon) is None
