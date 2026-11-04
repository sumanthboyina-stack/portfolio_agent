"""
Plan simulation: the pure simulator (synthetic bars, offline), the historical replay, storage + look-ahead, the sim_disagrees flag in
shadow and cap mode, the pipeline / backfill wiring, settings and the UI block.
"""
from __future__ import annotations

import json
import sqlite3
import time
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.tools import engine_settings_db as esdb
from portfolio_agent.tools import plan_sim_db as psdb
from portfolio_agent.tools import plan_simulator as ps
from portfolio_agent.tools import prediction_db as pdb
from portfolio_agent.tools import trade_plan as tp
from portfolio_agent.tools import trade_plan_db as tpdb
from tests.test_trade_plan import AS_OF, _plan_rows, _shared, _store_snapshot   # noqa: F401  (shared fixtures/helpers)

PLAN = SimpleNamespace(entry=100.0, stop=97.0, target=104.5, risk_per_share=3.0, reward_risk=1.5, horizon_days=5)


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(pdb, "_today_cst", lambda: AS_OF)
    monkeypatch.setattr(pdb, "is_trading_day", lambda d: d.weekday() < 5)


def _bars(close, *, up=0.0, down=0.0, end=AS_OF):
    """Daily bars ending on `end`: each high/low sits `up`/`down` (a fraction) around its own close."""
    close = np.asarray(close, dtype=float)
    idx = pd.bdate_range(end=end, periods=len(close))
    return pd.DataFrame({"Close": close, "High": close * (1 + up), "Low": close * (1 - down)}, index=idx)


def _trend(daily, n=300, **kw):
    return _bars(100.0 * (1 + daily) ** np.arange(n), **kw)


def _noisy(n=500, seed=3, sigma=0.015):
    rng = np.random.default_rng(seed)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0.0003, sigma, n)))
    return _bars(close, up=0.006, down=0.006)


def _sim(bars, plan=PLAN, paths=2000, block=5, seed=1):
    return ps.simulate(bars, plan, paths, block, seed)


# ── simulate: limiting cases ──────────────────────────────────────────────────

def test_steady_rise_reaches_the_target_first():
    r = _sim(_trend(0.01, up=0.0001, down=0.0001))
    assert r.p_target_first == pytest.approx(1.0) and r.p_stop_first == 0.0 and r.p_timeout == 0.0
    assert r.expected_r == pytest.approx(1.5) and r.p_close_above_entry == 1.0


def test_steady_fall_hits_the_stop_first():
    r = _sim(_trend(-0.01, up=0.0001, down=0.0001))
    assert r.p_stop_first == pytest.approx(1.0) and r.p_target_first == 0.0
    assert r.expected_r == pytest.approx(-1.0) and r.p_close_above_entry == 0.0


def test_a_flat_zero_volatility_series_always_times_out():
    r = _sim(_bars(np.full(300, 100.0)))
    assert (r.p_timeout, r.p_target_first, r.p_stop_first) == (1.0, 0.0, 0.0)
    assert r.expected_r == pytest.approx(0.0) and r.end_return_p50 == pytest.approx(0.0)
    assert r.p_close_above_entry == 0.0 and r.p_target_first_zero_drift == 0.0


def test_same_day_stop_and_target_counts_as_the_stop():
    # every day trades 10% above and below the previous close, so both levels are touched on day one
    r = _sim(_bars(np.full(300, 100.0), up=0.10, down=0.10))
    assert r.p_stop_first == 1.0 and r.p_target_first == 0.0 and r.expected_r == pytest.approx(-1.0)


def test_probabilities_add_up_and_percentiles_are_ordered():
    r = _sim(_noisy())
    assert r.p_target_first + r.p_stop_first + r.p_timeout == pytest.approx(1.0)
    assert r.end_return_p05 <= r.end_return_p50 <= r.end_return_p95
    assert 0.0 < r.p_target_first < 1.0 and 0.0 < r.p_stop_first < 1.0


def test_zero_drift_baseline_removes_the_trend():
    bars = _trend(0.004, up=0.002, down=0.002)               # a smooth uptrend with a little intraday range
    plan = SimpleNamespace(entry=100.0, stop=98.0, target=102.0, risk_per_share=2.0, reward_risk=1.0, horizon_days=5)
    r = _sim(bars, plan)
    assert r.p_target_first == pytest.approx(1.0)            # drifts up 2% in 5 days
    assert r.p_target_first_zero_drift < 0.5                 # strip the drift and it just wiggles inside its range


# ── determinism ───────────────────────────────────────────────────────────────

def test_same_inputs_give_identical_results_and_seeds_differ_by_key():
    bars = _noisy()
    seed = ps.sim_seed("AAPL", AS_OF, 5, tp.RULES_VERSION)
    assert _sim(bars, seed=seed) == _sim(bars, seed=seed)
    assert ps.sim_seed("AAPL", AS_OF, 5, tp.RULES_VERSION) == seed
    others = {ps.sim_seed("MSFT", AS_OF, 5, tp.RULES_VERSION), ps.sim_seed("AAPL", "2026-10-27", 5, tp.RULES_VERSION),
              ps.sim_seed("AAPL", AS_OF, 21, tp.RULES_VERSION), ps.sim_seed("AAPL", AS_OF, 5, "trade-plan-v2")}
    assert seed not in others and len(others) == 4
    assert _sim(bars, seed=seed) != _sim(bars, seed=seed + 1)


# ── look-ahead ────────────────────────────────────────────────────────────────

def test_bars_after_the_forecast_date_never_enter_the_pool():
    base = _noisy(400)
    crash_idx = pd.bdate_range(start=base.index[-1] + pd.Timedelta(days=1), periods=5)
    crash = pd.DataFrame({"Close": [50.0] * 5, "High": [60.0] * 5, "Low": [10.0] * 5}, index=crash_idx)
    with_crash = pd.concat([base, crash])
    a, b = ps.prepare_bars(base, AS_OF, 500), ps.prepare_bars(with_crash, AS_OF, 500)
    assert a.index[-1] == b.index[-1] == pd.Timestamp(AS_OF) and len(a) == len(b)
    assert _sim(a, seed=9) == _sim(b, seed=9)
    rule = ps.rule_from_settings(tp.seed_settings(), 5)
    assert ps.historical_replay(a, rule, 5) == ps.historical_replay(b, rule, 5)


def test_prepare_bars_takes_the_latest_lookback_and_needs_250():
    bars = _noisy(600)
    pool = ps.prepare_bars(bars, AS_OF, 500)
    assert len(pool) == 500 and pool.index[-1] == pd.Timestamp(AS_OF)
    assert ps.prepare_bars(_noisy(249), AS_OF, 500) is None
    assert ps.prepare_bars(None, AS_OF, 500) is None
    messy = bars.copy()
    messy.iloc[100, 0] = np.nan                                # an unusable bar is dropped, not trusted
    assert len(ps.prepare_bars(messy, AS_OF, 600)) == 599


# ── historical_replay: hand-checked ───────────────────────────────────────────

def test_historical_replay_counts_on_a_small_series():
    # 20 bars, close 100 throughout, daily range 99..101 (ATR 2). Rule: stop 1x ATR, target 2x ATR (R = 2), 2-day horizon.
    # Valid entry days are 13..17 (ATR needs 14 bars; two bars must follow).  Bar 16 dips to 97.5; bar 18 spikes to 105.
    close = np.full(20, 100.0)
    bars = _bars(close, up=0.01, down=0.01)
    bars.iloc[16, bars.columns.get_loc("Low")] = 97.5
    bars.iloc[18, bars.columns.get_loc("High")] = 105.0
    rule = {"stop_atr_mult": 1.0, "target_r_multiple": 2.0, "max_stop_pct": 0.5, "min_reward_risk": 1.5}
    r = ps.historical_replay(bars, rule, 2)
    # d=13 (bars 14,15): nothing -> timeout r 0 | d=14 (15,16): bar 16 low 97.5 <= 98 -> stop | d=15 (16,17): stop
    # d=16 (17,18): bar 18 high 105 >= 104.2 (ATR 2.107 after the dip) -> target | d=17 (18,19): target
    assert (r.n, r.hits_target, r.hits_stop, r.timeouts) == (5, 2, 2, 1)
    assert r.avg_r == pytest.approx((2 + 2 - 1 - 1 + 0) / 5)


def test_historical_replay_skips_invalid_days_and_unfinished_horizons():
    bars = _bars(np.full(20, 100.0), up=0.01, down=0.01)
    wide = {"stop_atr_mult": 1.0, "target_r_multiple": 2.0, "max_stop_pct": 0.01, "min_reward_risk": 1.5}   # a 2% stop is too wide
    assert ps.historical_replay(bars, wide, 2).n == 0 and ps.historical_replay(bars, wide, 2).avg_r is None
    low_rr = {"stop_atr_mult": 1.0, "target_r_multiple": 1.0, "max_stop_pct": 0.5, "min_reward_risk": 1.5}
    assert ps.historical_replay(bars, low_rr, 2).n == 0
    ok = {"stop_atr_mult": 1.0, "target_r_multiple": 2.0, "max_stop_pct": 0.5, "min_reward_risk": 1.5}
    assert ps.historical_replay(bars, ok, 10).n == 0 + len(range(13, 10))              # no day has 10 bars after it
    assert ps.historical_replay(bars, ok, 6).n == 1                                    # only day 13 has 6 bars after it


def test_replay_atr_matches_the_technical_features_formula():
    from portfolio_agent.tools.technical_features import compute_technical_features
    b = _noisy(60)
    got = ps._atr_series(b["High"].values, b["Low"].values, b["Close"].values)[-1]
    want = compute_technical_features(b["Close"], b["High"], b["Low"])["features"]["atr_14"]
    assert got == pytest.approx(want, abs=1e-3)


# ── performance ───────────────────────────────────────────────────────────────

def test_ten_thousand_paths_over_63_days_fits_the_budget():
    bars = _noisy(500)
    plan = SimpleNamespace(entry=100.0, stop=92.0, target=120.0, risk_per_share=8.0, reward_risk=2.5, horizon_days=63)
    rule = ps.rule_from_settings(tp.seed_settings(), 63)
    t0 = time.perf_counter()
    r = ps.simulate(bars, plan, 10_000, 5, 7)
    ps.historical_replay(bars, rule, 63)
    assert time.perf_counter() - t0 < 1.0
    assert r.paths == 10_000 and r.horizon_days == 63


# ── flag logic ────────────────────────────────────────────────────────────────

def test_sim_disagrees_rule():
    assert ps.sim_disagrees(8, 0.20, 7, 0.35) is True
    assert ps.sim_disagrees(7, 0.34, 7, 0.35) is True            # exactly at the conviction bar counts
    assert ps.sim_disagrees(6.9, 0.10, 7, 0.35) is False         # not confident enough
    assert ps.sim_disagrees(9, 0.35, 7, 0.35) is False           # exactly at the probability bar does not
    assert ps.sim_disagrees(None, 0.10, 7, 0.35) is False and ps.sim_disagrees(9, None, 7, 0.35) is False
    assert ps.sim_disagrees(9, float("nan"), 7, 0.35) is False


def _row(pid):
    with sqlite3.connect(str(db_module.DB_PATH)) as c:
        c.row_factory = sqlite3.Row
        return dict(c.execute("SELECT conviction_score, raw_conviction_score, composite_score, guardrail_flags FROM predictions WHERE id = ?",
                              [pid]).fetchone())


def _forecast(ticker="AAPL", horizon=5, **over):
    from portfolio_agent.tools.forecast_writer import ForecastProvenance, write_shared_forecast
    from portfolio_agent.tools.market_context import build_market_context
    ctx = build_market_context(ticker, [horizon], loaders={
        "fundamentals": lambda t: {"as_of_date": "2026-08-01"}, "research": lambda t: {"as_of_date": "2026-09-15"},
        "news": lambda t: [], "valuation": lambda t: {"as_of_date": "2026-09-01"}, "macro": lambda: {"vix": 20.0},
        "history": lambda t: [], "failure_patterns": lambda: []}, private_terms=frozenset())
    row = dict(ticker=ticker, prediction="BULLISH", recommendation="BUY", horizon_days=horizon, reasoning="steady",
               predicted_direction="UP", predicted_return_low=1.0, predicted_return_high=3.0, start_price=100.0,
               conviction_score=8.0, composite_score=8.0)
    row.update(over)
    res = write_shared_forecast(ctx, ForecastProvenance(origin="pipeline.apex", model_used="x:y"), **row)
    assert res["saved"], res
    return res["id"]


def _planned(ticker="AAPL", **over):
    _store_snapshot(ticker, AS_OF, 2.0)
    pid = _forecast(ticker, **over)
    assert tpdb.create_plan_for_prediction(pid)["plan_status"] == "planned"
    return pid


FALLING = lambda t: _trend(-0.004, 300, up=0.001, down=0.001)       # noqa: E731  steady slide: the target is almost never first
RISING = lambda t: _trend(0.01, 300, up=0.0001, down=0.0001)        # noqa: E731


def test_shadow_mode_flags_but_changes_nothing_else():
    pid = _planned()
    row = psdb.create_simulation_for_prediction(pid, fetch_bars=FALLING)
    assert row["sim_status"] == "ok" and row["p_target_first"] < 0.35
    after = _row(pid)
    assert json.loads(after["guardrail_flags"]) == ["sim_disagrees"]
    assert (after["conviction_score"], after["composite_score"], after["raw_conviction_score"]) == (8.0, 8.0, None)
    assert pid in psdb.get_flagged_ids([pid])


def test_cap_mode_caps_conviction_and_composite_and_keeps_the_raw_value():
    esdb.update_engine_settings(updated_by="t", sim={"sim_guardrail_mode": "cap"})
    pid = _planned(guardrail_flags=["no_news"])
    psdb.create_simulation_for_prediction(pid, fetch_bars=FALLING)
    after = _row(pid)
    assert json.loads(after["guardrail_flags"]) == ["no_news", "sim_disagrees"]          # appended, not overwritten
    assert (after["conviction_score"], after["composite_score"]) == (5.0, 5.0)
    assert after["raw_conviction_score"] == 8.0                                           # the model's own number survives


def test_cap_mode_never_touches_an_existing_raw_value_and_never_raises_a_low_score():
    esdb.update_engine_settings(updated_by="t", sim={"sim_guardrail_mode": "cap"})
    a = _planned("AAPL", conviction_score=6.0, raw_conviction_score=9.0, composite_score=4.0)   # already capped to 6 by another guardrail
    psdb.create_simulation_for_prediction(a, fetch_bars=FALLING)
    r = _row(a)
    assert (r["conviction_score"], r["composite_score"], r["raw_conviction_score"]) == (5.0, 4.0, 9.0)
    b = _planned("MSFT", conviction_score=4.0, raw_conviction_score=None, composite_score=4.0)  # raw 4 < 7: no flag, no cap
    psdb.create_simulation_for_prediction(b, fetch_bars=FALLING)
    assert _row(b) == {"conviction_score": 4.0, "raw_conviction_score": None, "composite_score": 4.0, "guardrail_flags": None}


def test_no_flag_when_the_history_agrees_or_conviction_is_low():
    up = _planned("AAPL")
    psdb.create_simulation_for_prediction(up, fetch_bars=RISING)
    assert _row(up)["guardrail_flags"] is None
    low = _planned("MSFT", conviction_score=5.0, composite_score=5.0)
    psdb.create_simulation_for_prediction(low, fetch_bars=FALLING)
    assert _row(low)["guardrail_flags"] is None


def test_the_flag_uses_the_pre_cap_conviction_when_one_exists():
    pid = _planned(conviction_score=5.0, raw_conviction_score=9.0)
    psdb.create_simulation_for_prediction(pid, fetch_bars=FALLING)
    assert _row(pid)["raw_conviction_score"] == 9.0 and "sim_disagrees" in json.loads(_row(pid)["guardrail_flags"])


# ── storage ───────────────────────────────────────────────────────────────────

def test_simulation_row_is_stored_with_its_provenance():
    pid = _planned()
    row = psdb.create_simulation_for_prediction(pid, fetch_bars=lambda t: _noisy(500))
    assert row["sim_status"] == "ok" and row["paths"] == 10_000 and row["block_len"] == 5 and row["lookback_days"] == 500
    assert row["sim_version"] == ps.SIM_VERSION and row["seed"] == ps.sim_seed("AAPL", AS_OF, 5, tp.RULES_VERSION)
    assert row["p_target_first"] + row["p_stop_first"] + row["p_timeout"] == pytest.approx(1.0)
    assert row["hist_n"] >= 0 and row["hist_target"] + row["hist_stop"] + row["hist_timeout"] == row["hist_n"]
    assert row["end_ret_p05"] <= row["end_ret_p50"] <= row["end_ret_p95"]


def test_second_call_is_a_no_op():
    pid = _planned()
    first = psdb.create_simulation_for_prediction(pid, fetch_bars=lambda t: _noisy(500))
    assert psdb.create_simulation_for_prediction(pid, fetch_bars=lambda t: _trend(-0.01, 300)) == first


def test_only_planned_shared_rows_are_simulated():
    from portfolio_agent.tools.forecast_writer import ForecastProvenance, write_private_forecast
    noplan = _forecast("MSFT", predicted_direction="DOWN")
    tpdb.create_plan_for_prediction(noplan)
    priv = write_private_forecast("sumanth", ForecastProvenance(origin="chat.ticker_prediction", model_used="x:y"), ticker="AAPL",
                                  prediction="BULLISH", recommendation="BUY", horizon_days=5, predicted_direction="UP",
                                  predicted_return_high=3.0, start_price=100.0)["id"]
    unplanned = _forecast("NVDA")                                # UP but no plan was ever made
    for pid in (noplan, priv, unplanned, 99999):
        assert psdb.create_simulation_for_prediction(pid, fetch_bars=lambda t: _noisy(500)) is None
    assert psdb.get_sims([noplan, priv, unplanned]) == {}


def test_short_history_is_recorded_as_insufficient_and_a_fetch_failure_as_error():
    a, b = _planned("AAPL"), _planned("MSFT")
    assert psdb.create_simulation_for_prediction(a, fetch_bars=lambda t: _noisy(200))["sim_status"] == "insufficient_history"
    assert psdb.create_simulation_for_prediction(b, fetch_bars=lambda t: 1 / 0)["sim_status"] == "error"
    assert _row(a)["guardrail_flags"] is None


def test_the_simulation_ignores_a_crash_on_the_day_after_the_forecast():
    quiet = _noisy(400)
    crash_idx = pd.bdate_range(start=quiet.index[-1] + pd.Timedelta(days=1), periods=4)
    crashed = pd.concat([quiet, pd.DataFrame({"Close": [10.0] * 4, "High": [11.0] * 4, "Low": [5.0] * 4}, index=crash_idx)])
    pid = _planned("TSLA")
    row = psdb.create_simulation_for_prediction(pid, fetch_bars=lambda t: crashed)       # the fetch returns the crash too
    clean = ps.simulate(ps.prepare_bars(quiet, AS_OF, 500), SimpleNamespace(**{**tpdb.get_plan(pid), "horizon_days": 5}), 10_000, 5, row["seed"])
    assert (row["p_target_first"], row["p_stop_first"], row["expected_r"]) == (clean.p_target_first, clean.p_stop_first, clean.expected_r)


# ── backfill & pipeline ───────────────────────────────────────────────────────

def test_backfill_flags_but_never_caps_and_retries_errors():
    esdb.update_engine_settings(updated_by="t", sim={"sim_guardrail_mode": "cap"})
    a, b = _planned("AAPL"), _planned("MSFT")
    psdb.create_simulation_for_prediction(b, fetch_bars=lambda t: 1 / 0)                 # b starts as an error row
    out = psdb.backfill_sims("2026-10-01", fetch_bars=FALLING)
    assert out["candidates"] == 2 and out["ok"] == 2 and out["error"] == 0 and out["flagged"] == 2
    for pid in (a, b):
        r = _row(pid)
        assert (r["conviction_score"], r["composite_score"]) == (8.0, 8.0) and "sim_disagrees" in json.loads(r["guardrail_flags"])
    assert psdb.backfill_sims("2026-10-01", fetch_bars=FALLING)["candidates"] == 0
    with pytest.raises(ValueError):
        psdb.backfill_sims("not-a-date")


def _pipeline(monkeypatch):
    from tests.test_trade_plan import _pipeline as p
    return p(monkeypatch)


def test_pipeline_simulates_each_plan_after_creating_it(monkeypatch):
    _store_snapshot("AAPL", AS_OF, 2.0)
    monkeypatch.setattr(psdb, "_default_fetch_bars", lambda t: _noisy(500))
    _pipeline(monkeypatch)(["AAPL"], [5, 21])
    with sqlite3.connect(str(db_module.DB_PATH)) as c:
        rows = c.execute("SELECT sim_status FROM plan_simulations").fetchall()
    assert [r[0] for r in rows] == ["ok", "ok"]


def test_a_simulation_failure_never_breaks_the_pipeline(monkeypatch):
    _store_snapshot("AAPL", AS_OF, 2.0)

    def boom(pid, **kw):
        raise RuntimeError("sim exploded")
    monkeypatch.setattr(psdb, "create_simulation_for_prediction", boom)
    _pipeline(monkeypatch)(["AAPL"], [5, 21])
    with sqlite3.connect(str(db_module.DB_PATH)) as c:
        assert c.execute("SELECT COUNT(*) FROM predictions WHERE scope = 'shared_market'").fetchone()[0] == 2
        assert c.execute("SELECT COUNT(*) FROM trade_plans WHERE plan_status = 'planned'").fetchone()[0] == 2    # the plans still landed


# ── settings ──────────────────────────────────────────────────────────────────

def test_sim_settings_defaults_update_validation_and_reset():
    assert esdb.get_sim_settings() == {"sim_paths": 10000, "sim_lookback_days": 500, "sim_block_len": 5,
                                       "sim_flag_min_conviction": 7.0, "sim_flag_max_p_target": 0.35, "sim_guardrail_mode": "shadow",
                                       "blend_min_n": 150}
    new = esdb.get_sim_settings(esdb.update_engine_settings(updated_by="t", sim={"sim_paths": 2000, "sim_guardrail_mode": "cap"}))
    assert new["sim_paths"] == 2000 and new["sim_guardrail_mode"] == "cap" and new["sim_block_len"] == 5
    for bad in ({"sim_paths": 10}, {"sim_paths": 1500.5}, {"sim_lookback_days": 100}, {"sim_block_len": 0},
                {"sim_flag_min_conviction": 11}, {"sim_flag_max_p_target": 0}, {"sim_guardrail_mode": "yolo"}, {"nonsense": 1}):
        with pytest.raises(ValueError):
            esdb.update_engine_settings(updated_by="t", sim=bad)
    assert esdb.get_sim_settings() == new
    esdb.reset_engine_settings_to_defaults(updated_by="t")
    assert esdb.get_sim_settings()["sim_guardrail_mode"] == "shadow" and esdb.get_sim_settings()["sim_paths"] == 10000


def test_settings_drive_the_simulation():
    esdb.update_engine_settings(updated_by="t", sim={"sim_paths": 500, "sim_block_len": 3, "sim_lookback_days": 300})
    row = psdb.create_simulation_for_prediction(_planned(), fetch_bars=lambda t: _noisy(600))
    assert (row["paths"], row["block_len"], row["lookback_days"]) == (500, 3, 300)


# ── UI ────────────────────────────────────────────────────────────────────────

def test_sim_block_html():
    from web.components.trade_plan_view import plan_block_html, sim_block_html
    ok = {"sim_status": "ok", "p_target_first": 0.30, "p_stop_first": 0.45, "p_timeout": 0.25, "expected_r": -0.12, "paths": 10000,
          "hist_n": 33, "hist_target": 13, "hist_stop": 15, "hist_timeout": 5}
    h = sim_block_html(ok, flagged=True)
    for needle in ("target first <b>30%", "stop first <b>45%", "neither <b>25%", "-0.12", "13</b> target, <b>15</b> stop, <b>5</b> neither",
                   "Sim disagrees", "Not a prediction", "10,000"):
        assert needle in h, needle
    assert "Sim disagrees" not in sim_block_html(ok)
    assert "Not enough price history" in sim_block_html({"sim_status": "insufficient_history"})
    assert sim_block_html(None) == ""
    plan = {"plan_status": "planned", "entry": 100.0, "stop": 97.0, "target": 104.5, "reward_risk": 1.5, "time_stop_date": "2026-11-02"}
    assert "HISTORY REPLAY" in plan_block_html(plan, sim=ok) and "HISTORY REPLAY" not in plan_block_html(plan)
