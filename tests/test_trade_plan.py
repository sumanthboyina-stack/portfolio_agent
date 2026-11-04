"""
Deterministic trade plans: the pure rules (build_plan / size_position), the storage + look-ahead guarantees, the pipeline and
backfill wiring, the settings, and the composer / UI additions. No network: ATR history comes from injected fixtures.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.tools import engine_settings_db as esdb
from portfolio_agent.tools import prediction_db as pdb
from portfolio_agent.tools import trade_plan as tp
from portfolio_agent.tools import trade_plan_db as tpdb

AS_OF = "2026-10-26"            # a Monday
S = tp.seed_settings()


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(pdb, "_today_cst", lambda: AS_OF)
    monkeypatch.setattr(pdb, "is_trading_day", lambda d: d.weekday() < 5)


# ── build_plan: hand-computed ─────────────────────────────────────────────────

@pytest.mark.parametrize("h, atr, stop, target, rr, time_stop", [
    (5,  2.0, 97.0,  104.5, 1.5, "2026-11-02"),    # risk 1.5*2 = 3;  target 100 + 1.5*3 = 104.5
    (21, 2.0, 95.0,  110.0, 2.0, "2026-11-24"),    # risk 2.5*2 = 5;  target 100 + 2.0*5 = 110
    (63, 2.0, 92.0,  120.0, 2.5, "2027-01-26"),    # risk 4.0*2 = 8;  target 100 + 2.5*8 = 120
])
def test_build_plan_math(h, atr, stop, target, rr, time_stop):
    p = tp.build_plan(100.0, atr, h, "UP", S, as_of_date=AS_OF, predicted_return_high=0.03)
    assert isinstance(p, tp.TradePlan)
    assert (p.entry, p.stop, p.target, p.reward_risk, p.atr_14) == (100.0, stop, target, rr, atr)
    assert p.risk_per_share == pytest.approx(100.0 - stop)
    assert p.time_stop_date == _trading_days_after(AS_OF, h)
    assert p.llm_target == pytest.approx(103.0)


def _trading_days_after(start: str, n: int) -> str:
    d, k = date.fromisoformat(start), 0
    while k < n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            k += 1
    return d.isoformat()


def test_llm_target_is_recorded_but_never_moves_the_plan():
    a = tp.build_plan(100.0, 2.0, 5, "UP", S, as_of_date=AS_OF, predicted_return_high=0.01)
    b = tp.build_plan(100.0, 2.0, 5, "UP", S, as_of_date=AS_OF, predicted_return_high=0.50)
    assert (a.stop, a.target, a.reward_risk) == (b.stop, b.target, b.reward_risk)
    assert (a.llm_target, b.llm_target) == (pytest.approx(101.0), pytest.approx(150.0))


def test_every_no_plan_reason():
    def reason(*a, **kw):
        r = tp.build_plan(*a, **kw)
        assert isinstance(r, tp.NoPlan)
        return r.reason
    kw = dict(as_of_date=AS_OF)
    assert reason(100.0, 2.0, 5, "DOWN", S, **kw) == "not_up"
    assert reason(100.0, 2.0, 5, "FLAT", S, **kw) == "not_up"
    assert reason(100.0, 2.0, 5, None, S, **kw) == "not_up"
    assert reason(100.0, 2.0, 250, "UP", S, **kw) == "unsupported_horizon"
    assert reason(100.0, 2.0, None, "UP", S, **kw) == "unsupported_horizon"
    assert reason(None, 2.0, 5, "UP", S, **kw) == "missing_price"
    assert reason(0, 2.0, 5, "UP", S, **kw) == "missing_price"
    assert reason(100.0, None, 5, "UP", S, **kw) == "missing_atr"
    assert reason(100.0, float("nan"), 5, "UP", S, **kw) == "missing_atr"
    assert reason(100.0, 0.0, 5, "UP", S, **kw) == "missing_atr"
    assert reason(100.0, 6.0, 5, "UP", S, **kw) == "too_volatile"           # risk 9 = 9% of entry > 8%
    assert reason(100.0, 40.0, 63, "UP", {**S, "plan_max_stop_pct": {**S["plan_max_stop_pct"], 63: 1.0}}, **kw) == "too_volatile"   # stop below zero
    low_r = {**S, "plan_target_r_multiple": {5: 1.0, 21: 2.0, 63: 2.5}}
    assert reason(100.0, 2.0, 5, "UP", low_r, **kw) == "below_min_rr"


def test_stop_exactly_at_the_widest_allowed_is_still_a_plan():
    # risk = 1.5 * (16/3) = 8.0 = exactly 8% of 100
    assert isinstance(tp.build_plan(100.0, 16 / 3, 5, "UP", S, as_of_date=AS_OF), tp.TradePlan)


# ── size_position ─────────────────────────────────────────────────────────────

PLAN = SimpleNamespace(entry=100.0, risk_per_share=3.0)


def test_sizing_loss_cap_binding():
    # moderate 1% of 100k = $1000 -> floor(1000/3) = 333; a 50% position limit allows 500
    z = tp.size_position(PLAN, 100_000, "moderate", 50.0, S)
    assert (z.shares, z.binding_cap) == (333, "loss")
    assert z.loss_if_stopped == pytest.approx(999.0) and z.position_value == pytest.approx(33_300.0)
    assert z.max_loss_usd == pytest.approx(1000.0)


def test_sizing_position_cap_binding():
    # same loss budget, but a 10% limit = $10,000 = 100 shares
    z = tp.size_position(PLAN, 100_000, "moderate", 10.0, S)
    assert (z.shares, z.binding_cap) == (100, "position")
    assert z.position_value == pytest.approx(10_000.0) and z.loss_if_stopped == pytest.approx(300.0)


def test_sizing_by_risk_profile():
    assert tp.size_position(PLAN, 100_000, "conservative", 100.0, S).shares == 166     # 500 / 3
    assert tp.size_position(PLAN, 100_000, "AGGRESSIVE", 100.0, S).shares == 500       # 1500 / 3


@pytest.mark.parametrize("pv, profile, plan", [
    (0, "moderate", PLAN), (-5_000, "moderate", PLAN), (None, "moderate", PLAN), (float("nan"), "moderate", PLAN),
    (100_000, "moderate", SimpleNamespace(entry=100.0, risk_per_share=0.0)),
    (100_000, "moderate", SimpleNamespace(entry=100.0, risk_per_share=-1.0)),
    (100_000, "moderate", SimpleNamespace(entry=0.0, risk_per_share=3.0)),
    (100_000, "nonsense", PLAN), (100_000, None, PLAN),
])
def test_sizing_zero_or_negative_inputs_buy_nothing(pv, profile, plan):
    z = tp.size_position(plan, pv, profile, 10.0, S)
    assert (z.shares, z.position_value, z.loss_if_stopped) == (0, 0.0, 0.0)


def test_sizing_too_small_a_budget_or_limit_buys_nothing():
    assert tp.size_position(PLAN, 100, "moderate", 100.0, S).shares == 0               # $1 budget < $3 risk
    assert tp.size_position(PLAN, 100_000, "moderate", 0.0, S).shares == 0             # a 0% position limit
    assert tp.size_position(PLAN, 100_000, "moderate", -3.0, S).shares == 333          # a nonsense limit is ignored, not obeyed


# ── storage fixtures ──────────────────────────────────────────────────────────

def _store_snapshot(ticker, snapshot, atr):
    from portfolio_agent.tools import technical_features as tf
    tf.store_technical_features(ticker, snapshot, {"bars_available": 250, "coverage": {}, "features": {"atr_14": atr}})


def _shared(ticker="AAPL", horizon=5, direction="UP", start_price=100.0, ret_high=3.0):
    from portfolio_agent.tools.forecast_writer import ForecastProvenance, write_shared_forecast
    from portfolio_agent.tools.market_context import build_market_context
    ctx = build_market_context(ticker, [horizon], loaders={
        "fundamentals": lambda t: {"as_of_date": "2026-08-01"}, "research": lambda t: {"as_of_date": "2026-09-15"},
        "news": lambda t: [], "valuation": lambda t: {"as_of_date": "2026-09-01"}, "macro": lambda: {"vix": 20.0},
        "history": lambda t: [], "failure_patterns": lambda: []}, private_terms=frozenset())
    res = write_shared_forecast(ctx, ForecastProvenance(origin="pipeline.apex", model_used="x:y"), ticker=ticker,
                                prediction="BULLISH", recommendation="BUY", horizon_days=horizon, reasoning="steady",
                                predicted_direction=direction, predicted_return_low=1.0, predicted_return_high=ret_high,
                                start_price=start_price)
    assert res["saved"], res
    return res["id"]


def _plan_rows():
    with sqlite3.connect(str(db_module.DB_PATH)) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute("SELECT * FROM trade_plans ORDER BY prediction_id")]


def test_plan_is_stored_for_a_shared_up_row():
    _store_snapshot("AAPL", AS_OF, 2.0)
    pid = _shared()
    row = tpdb.create_plan_for_prediction(pid)
    assert row["plan_status"] == "planned" and row["no_plan_reason"] is None
    assert (row["entry"], row["stop"], row["target"], row["reward_risk"]) == (100.0, 97.0, 104.5, 1.5)
    assert row["time_stop_date"] == "2026-11-02" and row["atr_14"] == 2.0
    assert row["llm_target"] == pytest.approx(103.0)             # predictions stores 3.0 (percent); the plan stores $103
    assert row["rules_version"] == tp.RULES_VERSION and row["created_at"]


def test_non_up_and_missing_atr_rows_store_the_reason():
    down = _shared("MSFT", direction="DOWN")
    noatr = _shared("NVDA")                                       # no snapshot, fetch blocked in tests
    assert tpdb.create_plan_for_prediction(down)["no_plan_reason"] == "not_up"
    r = tpdb.create_plan_for_prediction(noatr)
    assert (r["plan_status"], r["no_plan_reason"], r["stop"]) == ("no_plan", "missing_atr", None)


def test_private_rows_never_get_a_plan():
    from portfolio_agent.tools.forecast_writer import ForecastProvenance, write_private_forecast
    _store_snapshot("AAPL", AS_OF, 2.0)
    res = write_private_forecast("sumanth", ForecastProvenance(origin="chat.ticker_prediction", model_used="x:y"),
                                 ticker="AAPL", prediction="BULLISH", recommendation="BUY", horizon_days=5,
                                 predicted_direction="UP", predicted_return_high=3.0, start_price=100.0)
    assert res["saved"]
    assert tpdb.create_plan_for_prediction(res["id"]) is None
    assert _plan_rows() == []
    assert tpdb.backfill_plans("2026-01-01")["candidates"] == 0


def test_second_call_is_a_no_op_and_rows_are_immutable():
    _store_snapshot("AAPL", AS_OF, 2.0)
    pid = _shared()
    first = tpdb.create_plan_for_prediction(pid)
    esdb.update_engine_settings(updated_by="t", trade_plan={"plan_stop_atr_mult": {5: 3.0}})   # rules change afterwards
    _store_snapshot("AAPL", AS_OF, 9.0)                                                        # and so does the data
    assert tpdb.create_plan_for_prediction(pid) == first
    assert len(_plan_rows()) == 1
    with sqlite3.connect(str(db_module.DB_PATH)) as c:
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            c.execute("UPDATE trade_plans SET stop = 1 WHERE prediction_id = ?", [pid])
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            c.execute("DELETE FROM trade_plans WHERE prediction_id = ?", [pid])


# ── look-ahead ────────────────────────────────────────────────────────────────

def _ohlc(spike_after: bool):
    idx = pd.bdate_range(end=AS_OF, periods=40)                          # last bar = as_of
    close = pd.Series(100.0, index=idx)
    high, low = close + 1.0, close - 1.0                                  # daily range 2 -> ATR14 = 2.0
    if spike_after:
        nxt = pd.bdate_range(start=idx[-1] + pd.Timedelta(days=1), periods=3)
        close = pd.concat([close, pd.Series([150.0, 150.0, 150.0], index=nxt)])
        high = pd.concat([high, pd.Series([200.0, 160.0, 160.0], index=nxt)])
        low = pd.concat([low, pd.Series([90.0, 140.0, 140.0], index=nxt)])
    return pd.DataFrame({"Close": close, "High": high, "Low": low})


def test_atr_ignores_bars_after_the_forecast_date():
    quiet = tpdb.atr_as_of("AAPL", AS_OF, fetch_history=lambda t, d: _ohlc(False))
    spiked = tpdb.atr_as_of("AAPL", AS_OF, fetch_history=lambda t, d: _ohlc(True))
    assert quiet == pytest.approx(2.0)
    assert spiked == quiet


def test_plan_does_not_change_when_the_next_day_spikes():
    a = _shared("AAPL")
    plan_a = tpdb.create_plan_for_prediction(a, fetch_history=lambda t, d: _ohlc(False))
    b = _shared("MSFT")
    plan_b = tpdb.create_plan_for_prediction(b, fetch_history=lambda t, d: _ohlc(True))
    keys = ("plan_status", "entry", "stop", "target", "risk_per_share", "reward_risk", "time_stop_date", "atr_14")
    assert {k: plan_a[k] for k in keys} == {k: plan_b[k] for k in keys}
    assert plan_a["atr_14"] == pytest.approx(2.0) and plan_a["stop"] == pytest.approx(97.0)


def test_a_stored_snapshot_dated_after_the_forecast_is_ignored():
    _store_snapshot("AAPL", "2026-10-27", 50.0)                           # the day AFTER as_of
    _store_snapshot("AAPL", "2026-10-23", 2.0)                            # the Friday before
    assert tpdb.atr_as_of("AAPL", AS_OF, fetch_history=lambda t, d: None) == 2.0
    assert tpdb.atr_as_of("MSFT", AS_OF, fetch_history=lambda t, d: None) is None


def test_stale_data_is_not_used():
    _store_snapshot("AAPL", "2026-10-01", 2.0)                            # 25 days old
    assert tpdb.atr_as_of("AAPL", AS_OF, fetch_history=lambda t, d: None) is None


def test_backfill_uses_each_rows_own_as_of_date(monkeypatch):
    first = _shared("AAPL")
    monkeypatch.setattr(pdb, "_today_cst", lambda: "2026-10-27")
    second = _shared("AAPL", horizon=21)
    seen = []

    def fetch(ticker, as_of):
        seen.append(as_of)
        return _ohlc(False)
    out = tpdb.backfill_plans("2026-10-01", fetch_history=fetch)
    assert out == {"candidates": 2, "planned": 2, "no_plan": 0, "failed": 0}
    assert sorted(seen) == [date(2026, 10, 26), date(2026, 10, 27)]
    assert {r["prediction_id"] for r in _plan_rows()} == {first, second}
    assert tpdb.backfill_plans("2026-10-01", fetch_history=fetch)["candidates"] == 0       # re-run: nothing left
    assert tpdb.backfill_plans("2026-10-27", fetch_history=fetch)["candidates"] == 0


# ── pipeline wiring ───────────────────────────────────────────────────────────

def _pipeline(monkeypatch):
    from tests.test_apex_champion import _patch_pipeline, _result, _run
    _patch_pipeline(monkeypatch, champion=_result("BUY", "Claude-Sonnet", pdb.ROLE_CHAMPION, horizons=(5, 21)))
    return _run


def test_pipeline_writes_a_plan_after_each_shared_forecast(monkeypatch):
    _store_snapshot("AAPL", AS_OF, 2.0)
    _pipeline(monkeypatch)(["AAPL"], [5, 21])
    rows = {r["time_stop_date"]: r for r in _plan_rows()}
    assert [r["stop"] for r in rows.values()] == [97.0, 95.0]
    assert all(r["plan_status"] == "planned" for r in rows.values())


def test_a_plan_failure_never_breaks_the_pipeline(monkeypatch):
    def boom(pid, **kw):
        raise RuntimeError("plan store down")
    monkeypatch.setattr(tpdb, "create_plan_for_prediction", boom)
    _pipeline(monkeypatch)(["AAPL"], [5, 21])
    with sqlite3.connect(str(db_module.DB_PATH)) as c:
        assert c.execute("SELECT COUNT(*) FROM predictions WHERE scope = 'shared_market'").fetchone()[0] == 2
    assert _plan_rows() == []


# ── settings ──────────────────────────────────────────────────────────────────

def test_settings_are_seeded_and_updatable_and_validated():
    got = esdb.get_trade_plan_settings()
    assert got == tp.seed_settings()
    new = esdb.get_trade_plan_settings(esdb.update_engine_settings(
        updated_by="t", trade_plan={"plan_min_reward_risk": 1.8, "plan_max_stop_pct": {21: 0.2},
                                    "plan_max_loss_pct_by_risk_profile": {"moderate": 0.012}}))
    assert new["plan_min_reward_risk"] == 1.8 and new["plan_max_stop_pct"] == {5: 0.08, 21: 0.2, 63: 0.25}
    assert new["plan_max_loss_pct_by_risk_profile"]["moderate"] == 0.012
    for bad in ({"plan_stop_atr_mult": {5: 0}}, {"plan_stop_atr_mult": {7: 1.0}}, {"plan_max_stop_pct": {5: 1.5}},
                {"plan_max_loss_pct_by_risk_profile": {"reckless": 0.01}}, {"plan_max_loss_pct_by_risk_profile": {"moderate": 0.5}},
                {"plan_min_reward_risk": -1}, {"nonsense": 1}):
        with pytest.raises(ValueError):
            esdb.update_engine_settings(updated_by="t", trade_plan=bad)
    assert esdb.get_trade_plan_settings() == new                       # a rejected patch changed nothing
    esdb.reset_engine_settings_to_defaults(updated_by="t")
    assert esdb.get_trade_plan_settings() == tp.seed_settings()


def test_changed_settings_drive_new_plans():
    _store_snapshot("AAPL", AS_OF, 2.0)
    esdb.update_engine_settings(updated_by="t", trade_plan={"plan_stop_atr_mult": {5: 2.0}})
    row = tpdb.create_plan_for_prediction(_shared())
    assert row["stop"] == 96.0 and row["target"] == 106.0                # risk 4, R 1.5 -> +6


# ── composer ──────────────────────────────────────────────────────────────────

def test_market_outlook_carries_the_shared_plan_and_fit_carries_private_sizing(monkeypatch):
    from portfolio_agent.services import account_service
    from portfolio_agent.services.context import local_context
    from portfolio_agent.tools import decision_composer as dc
    from tests.test_decision_composer import _no_restriction, _seed_holding

    _no_restriction(monkeypatch)
    _store_snapshot("AAPL", AS_OF, 2.0)
    tpdb.create_plan_for_prediction(_shared("AAPL"))
    ctx = local_context("test")
    _seed_holding(ctx, "MSFT", 100, 1000.0)                               # $100k portfolio
    from portfolio_agent.tools import portfolio_risk as risk
    monkeypatch.setattr(risk, "compute_portfolio_risk_context", lambda *a, **k: {})
    d = dc.compose_decision(ctx, "AAPL", horizon_days=5).to_dict()
    plan = d["market_outlook"]["plan"]
    assert plan["plan_status"] == "planned" and plan["stop"] == 97.0
    z = d["portfolio_fit"]["plan_sizing"]
    assert z is not None and z["shares"] > 0 and z["loss_if_stopped"] <= z["max_loss_usd"] + 1e-6
    assert z["portfolio_value"] == pytest.approx(100_000.0)
    assert "plan_sizing" not in d["market_outlook"] and "shares" not in plan      # sizing is private: never in the shared piece


def test_plan_sizing_is_none_without_a_planned_trade():
    from portfolio_agent.services.context import local_context
    from portfolio_agent.tools import decision_composer as dc
    ctx = local_context("test")
    assert dc.plan_sizing(ctx, None) is None
    assert dc.plan_sizing(ctx, {"plan_status": "no_plan", "no_plan_reason": "not_up"}) is None


# ── UI block ──────────────────────────────────────────────────────────────────

def test_plan_block_html_for_planned_no_plan_and_missing_rows():
    from web.components.trade_plan_view import plan_block_html
    row = {"plan_status": "planned", "entry": 100.0, "stop": 97.0, "target": 104.5, "reward_risk": 1.5,
           "time_stop_date": "2026-11-02", "llm_target": 103.0}
    h = plan_block_html(row, horizon_days=5)
    for needle in ("$100.00", "$97.00", "$104.50", "2026-11-02", "1.50", "TRADE PLAN", "5d"):
        assert needle in h
    sized = plan_block_html(row, sizing={"shares": 100, "max_loss_usd": 1000.0, "position_value": 10000.0,
                                         "loss_if_stopped": 300.0, "binding_cap": "position"})
    assert "Max loss" in sized and "100 shares" in sized and "Max loss" not in h
    nope = plan_block_html({"plan_status": "no_plan", "no_plan_reason": "too_volatile"})
    assert "No trade plan" in nope and "wider than the maximum" in nope
    assert plan_block_html(None) == ""
