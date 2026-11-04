"""
Plan outcomes and validation: the outcome walker (target / stop / same-day / timeout, never past the time stop), the evaluation hook,
the edge / calibration / grid metrics, the logistic blend, the segment keys and the cap recommendation. Offline, synthetic data.
"""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.tools import engine_settings_db as esdb
from portfolio_agent.tools import plan_sim_db as psdb
from portfolio_agent.tools import plan_simulator as ps
from portfolio_agent.tools import plan_validation as pv
from portfolio_agent.tools import prediction_db as pdb
from tests.test_plan_simulator import AS_OF, _bars, _planned   # noqa: F401  (shared fixtures/helpers)

PLAN = SimpleNamespace(entry=100.0, stop=97.0, target=104.5, risk_per_share=3.0, reward_risk=1.5)
STOP_DATE = "2026-11-02"            # five trading days after AS_OF (Monday 2026-10-26)


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(pdb, "_today_cst", lambda: AS_OF)
    monkeypatch.setattr(pdb, "is_trading_day", lambda d: d.weekday() < 5)


def _after(rows, start="2026-10-27"):
    """Bars for the days after AS_OF from (high, low, close) tuples."""
    idx = pd.bdate_range(start=start, periods=len(rows))
    return pd.DataFrame(rows, columns=["High", "Low", "Close"], index=idx)


QUIET = (100.5, 99.5, 100.0)


def _walk(rows, **kw):
    return ps.walk_plan_outcome(_after(rows), PLAN, AS_OF, STOP_DATE, **kw)


# ── outcome walker ────────────────────────────────────────────────────────────

def test_target_day():
    r = _walk([QUIET, QUIET, (104.6, 100.0, 104.0), QUIET, QUIET])
    assert (r.plan_outcome, r.realized_r, r.exit_date) == ("TARGET", 1.5, "2026-10-29")


def test_stop_day():
    r = _walk([QUIET, (100.0, 96.9, 97.5), QUIET, QUIET, QUIET])
    assert (r.plan_outcome, r.realized_r, r.exit_date) == ("STOP", -1.0, "2026-10-28")


def test_same_day_target_and_stop_counts_as_stop():
    r = _walk([QUIET, (105.0, 96.0, 100.0), QUIET, QUIET, QUIET])
    assert (r.plan_outcome, r.realized_r) == ("STOP", -1.0)


def test_stop_first_then_target_is_a_stop_and_target_first_then_stop_is_a_target():
    assert _walk([(100.0, 96.0, 97.0), (106.0, 100.0, 105.0), QUIET, QUIET, QUIET]).plan_outcome == "STOP"
    assert _walk([(105.0, 100.0, 104.0), (100.0, 96.0, 97.0), QUIET, QUIET, QUIET]).plan_outcome == "TARGET"


def test_timeout_marks_to_the_time_stop_close():
    r = _walk([QUIET, QUIET, QUIET, QUIET, (102.0, 99.0, 101.5)])
    assert r.plan_outcome == "TIMEOUT" and r.exit_date == STOP_DATE
    assert r.realized_r == pytest.approx((101.5 - 100.0) / 3.0)
    assert _walk([QUIET, QUIET, QUIET, QUIET, (100.4, 94.0 + 3.1, 98.0)]).realized_r == pytest.approx(-2 / 3)


def test_the_walker_never_reads_past_the_time_stop():
    rows = [QUIET] * 5 + [(130.0, 50.0, 60.0)] * 3                    # a crash AND a spike after the time stop
    r = ps.walk_plan_outcome(_after(rows), PLAN, AS_OF, STOP_DATE)
    assert (r.plan_outcome, r.exit_date) == ("TIMEOUT", STOP_DATE) and r.realized_r == pytest.approx(0.0)


def test_bars_on_or_before_the_forecast_date_are_not_part_of_the_walk():
    before = pd.DataFrame([(200.0, 1.0, 100.0)], columns=["High", "Low", "Close"], index=pd.DatetimeIndex([AS_OF]))
    bars = pd.concat([before, _after([QUIET] * 5)])
    assert ps.walk_plan_outcome(bars, PLAN, AS_OF, STOP_DATE).plan_outcome == "TIMEOUT"


def test_not_yet_knowable_returns_none():
    assert ps.walk_plan_outcome(None, PLAN, AS_OF, STOP_DATE) is None
    assert _walk([]) is None if False else ps.walk_plan_outcome(_after([QUIET])[:0], PLAN, AS_OF, STOP_DATE) is None
    assert _walk([QUIET, QUIET]) is None                              # data stops three days short of the time stop: a timeout is not known
    assert _walk([QUIET, (100.0, 96.0, 97.0)]).plan_outcome == "STOP"  # but a touched stop is final even with later bars missing


# ── evaluation hook ───────────────────────────────────────────────────────────

def _bars_for(rows):
    return lambda t: _after(rows)


def test_matured_plans_get_an_outcome_and_a_rerun_changes_nothing():
    pid = _planned()
    out = psdb.evaluate_matured_plans("2026-11-02", fetch_bars=_bars_for([QUIET, (105.0, 100.0, 104.0), QUIET, QUIET, QUIET]))
    assert out["evaluated"] == 1
    with sqlite3.connect(str(db_module.DB_PATH)) as c:
        c.row_factory = sqlite3.Row
        row = dict(c.execute("SELECT * FROM plan_outcomes WHERE prediction_id = ?", [pid]).fetchone())
    assert (row["plan_outcome"], row["realized_r"], row["exit_date"]) == ("TARGET", 1.5, "2026-10-28") and row["evaluated_at"]
    again = psdb.evaluate_matured_plans("2026-11-02", fetch_bars=_bars_for([(100.0, 90.0, 91.0)] * 5))
    assert again["candidates"] == 0


def test_plans_before_their_time_stop_or_without_data_are_left_alone():
    _planned()
    assert psdb.evaluate_matured_plans("2026-11-01", fetch_bars=_bars_for([QUIET] * 5))["candidates"] == 0
    out = psdb.evaluate_matured_plans("2026-11-02", fetch_bars=lambda t: None)
    assert (out["evaluated"], out["pending"]) == (0, 1)
    out = psdb.evaluate_matured_plans("2026-11-02", fetch_bars=lambda t: 1 / 0)
    assert out["errors"] == 1


def test_evaluate_matured_predictions_records_plan_outcomes_without_touching_direction_scoring(monkeypatch):
    from datetime import date
    from portfolio_agent.tools import validation_engine as ve
    pid = _planned()
    monkeypatch.setattr(psdb, "_default_fetch_bars", _bars_for([QUIET, QUIET, (100.0, 96.0, 97.0), QUIET, QUIET]))
    res = ve.evaluate_matured_predictions(today=date(2026, 11, 2), force=True)
    assert res["plan_outcomes"]["evaluated"] == 1
    with sqlite3.connect(str(db_module.DB_PATH)) as c:
        assert c.execute("SELECT plan_outcome FROM plan_outcomes WHERE prediction_id = ?", [pid]).fetchone()[0] == "STOP"


def test_a_plan_outcome_failure_never_breaks_direction_evaluation(monkeypatch):
    from datetime import date
    from portfolio_agent.tools import validation_engine as ve
    monkeypatch.setattr(psdb, "evaluate_matured_plans", lambda *a, **k: 1 / 0)
    res = ve.evaluate_matured_predictions(today=date(2026, 11, 2), force=True)
    assert res["evaluated"] == 0 and res["plan_outcomes"] == {"errors": 1}


# ── metrics on synthetic data ─────────────────────────────────────────────────

def _synthetic(n_dates=60, per_date=12, seed=1, lift=0.0, conviction_lift=0.0, horizon=5):
    """Plans whose true TARGET probability is the simulator's p (+ lift) (+ conviction_lift per point above 5)."""
    rng = np.random.default_rng(seed)
    rows = []
    for k, day in enumerate(pd.bdate_range("2026-01-05", periods=n_dates)):
        for j in range(per_date):
            p = float(rng.uniform(0.05, 0.6))
            conv = float(rng.integers(1, 11))
            true_p = min(max(p + lift + conviction_lift * (conv - 5), 0.01), 0.99)
            hit = rng.random() < true_p
            outcome = "TARGET" if hit else ("STOP" if rng.random() < 0.6 else "TIMEOUT")
            rows.append({"prediction_id": k * 100 + j, "ticker": f"T{j}", "as_of_date": day.date().isoformat(), "horizon_days": horizon,
                         "conviction": conv, "guardrail_flags": None, "p_target_first": p, "p_stop_first": (1 - p) * 0.6,
                         "p_timeout": (1 - p) * 0.4, "p_target_first_zero_drift": p * 0.9, "plan_outcome": outcome,
                         "realized_r": 1.5 if hit else (-1.0 if outcome == "STOP" else 0.1)})
    return pd.DataFrame(rows)


def test_edge_is_about_zero_when_reality_matches_the_simulator():
    e = pv.edge_over_simulator(_synthetic(seed=2))
    assert abs(e["edge"]) < 0.03 and e["lo"] < 0 < e["hi"] and e["n"] == 720 and e["n_dates"] == 60


def test_edge_is_positive_when_the_model_beats_the_simulator():
    e = pv.edge_over_simulator(_synthetic(seed=3, lift=0.12))
    assert e["edge"] == pytest.approx(0.12, abs=0.04) and e["lo"] > 0


def test_edge_by_band_rises_with_conviction_when_conviction_has_edge():
    by = {b["band"]: b for b in pv.edge_over_simulator(_synthetic(n_dates=120, seed=4, conviction_lift=0.04))["by_band"]}
    assert by["1-4"]["edge"] < by["5-6"]["edge"] < by["7-8"]["edge"] < by["9-10"]["edge"]
    assert all(b["n"] > 0 for b in by.values())


def test_the_interval_resamples_dates_not_rows():
    df = _synthetic(n_dates=40, per_date=12, seed=5)
    wide = pv.edge_over_simulator(df)
    rows_only = df.copy()
    rows_only["as_of_date"] = [f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(len(df))]       # every row its own "date"
    narrow = pv.edge_over_simulator(rows_only)
    assert wide["n_dates"] == 40 and narrow["n_dates"] == len(df)
    assert (wide["hi"] - wide["lo"]) > 0 and (narrow["hi"] - narrow["lo"]) > 0


def test_reliability_table_and_brier_on_calibrated_data():
    df = _synthetic(n_dates=150, seed=6)
    rel = pv.reliability_table(df)
    assert sum(r["n"] for r in rel) == len(df)
    for r in rel:
        if r["n"] >= 100:
            assert r["realized"] == pytest.approx(r["mean_pred"], abs=0.08)
    b = pv.simulator_brier(df)
    assert b["brier_sim"] < b["brier_base"] and b["skill"] > 0 and b["gain_lo"] is not None


def test_conviction_bands_and_grids():
    df = _synthetic(n_dates=60, seed=7)
    cb = {c["band"]: c for c in pv.conviction_bands(df)}
    assert sum(c["n"] for c in cb.values()) == len(df) and set(cb) == {"1-4", "5-6", "7-8", "9-10"}
    g = pv.agreement_grid(df, pv.CONVICTION_BANDS, pv.P_BANDS)
    assert len(g["cells"]) == 16 and sum(c["n"] for c in g["cells"]) == len(df)
    cells = pv.two_by_two(df, 7.0, 0.35)
    assert sum(c["n"] for c in cells.values()) == len(df)


def test_disagreement_test_flags_a_significantly_worse_cell_and_respects_min_n():
    rng = np.random.default_rng(8)

    def cell(n, conv, p, rate):
        return [{"as_of_date": f"2026-02-{1 + i % 25:02d}", "horizon_days": 5, "conviction": conv, "p_target_first": p,
                 "plan_outcome": "TARGET" if rng.random() < rate else "STOP", "realized_r": 0.0} for i in range(n)]
    df = pd.DataFrame(cell(60, 9, 0.20, 0.10) + cell(60, 9, 0.50, 0.50) + cell(40, 3, 0.5, 0.3))
    t = pv.disagreement_test(df, 7.0, 0.35)
    assert (t["n_disagree"], t["n_confident"]) == (60, 60) and t["p_value"] < 0.001 and t["significantly_worse"]
    thin = pd.DataFrame(cell(20, 9, 0.20, 0.0) + cell(60, 9, 0.50, 0.6))
    assert pv.disagreement_test(thin, 7.0, 0.35)["significantly_worse"] is False         # 20 < 30 rows in the disagree cell
    same = pd.DataFrame(cell(60, 9, 0.20, 0.3) + cell(60, 9, 0.50, 0.3))
    assert pv.disagreement_test(same, 7.0, 0.35)["significantly_worse"] is False


# ── logistic blend ────────────────────────────────────────────────────────────

def _blend_data(n, a, b, c, seed=0):
    rng = np.random.default_rng(seed)
    p = rng.uniform(0.05, 0.7, n)
    conv = rng.integers(1, 11, n).astype(float)
    true = pv._sigmoid(a + b * pv._logit(p) + c * conv)
    y = rng.random(n) < true
    return p, conv, y


def test_logistic_fit_recovers_known_coefficients():
    p, conv, y = _blend_data(20000, -0.8, 0.9, 0.12, seed=1)
    coef = pv.fit_logistic(pv._design(p, conv), y.astype(float))
    assert coef == pytest.approx([-0.8, 0.9, 0.12], abs=0.08)


def _frame(p, conv, y, per_date=10):
    n = len(p)
    dates = [pd.bdate_range("2026-01-05", periods=n // per_date + 1)[i // per_date].date().isoformat() for i in range(n)]
    return pd.DataFrame({"as_of_date": dates, "horizon_days": 5, "conviction": conv, "p_target_first": p,
                         "plan_outcome": np.where(y, "TARGET", "STOP"), "realized_r": np.where(y, 1.5, -1.0)})


def test_blend_is_withheld_below_the_minimum_n():
    p, conv, y = _blend_data(120, -0.8, 0.9, 0.2, seed=2)
    r = pv.fit_blend(_frame(p, conv, y), 150)
    assert r["status"] == "insufficient_data" and r["n"] == 120 and r["min_n"] == 150


def test_blend_is_used_when_conviction_adds_information():
    p, conv, y = _blend_data(3000, -1.0, 0.8, 0.25, seed=3)
    r = pv.fit_blend(_frame(p, conv, y), 150)
    assert r["status"] == "ok" and r["valid"] and r["brier_blend"] < r["brier_sim_only"]
    assert r["c"] == pytest.approx(0.25, abs=0.08) and r["pts_per_conviction"] > 0


def test_blend_is_not_valid_when_it_does_not_beat_the_simulator_alone():
    rng = np.random.default_rng(1)                                    # (a fixed draw: with noise a lucky blend can edge ahead by chance)
    p = rng.uniform(0.05, 0.7, 300)
    conv = rng.integers(1, 11, 300).astype(float)
    y = rng.random(300) < p                                           # the simulator is exactly right; conviction is noise
    r = pv.fit_blend(_frame(p, conv, y), 150)
    assert r["status"] == "ok" and r["valid"] is False and r["brier_blend"] >= r["brier_sim_only"]


def test_only_a_valid_blend_is_stored_for_use_and_p_blend_reads_it():
    p, conv, y = _blend_data(3000, -1.0, 0.8, 0.25, seed=5)
    fits = pv.fit_and_store_blends(_frame(p, conv, y), {"blend_min_n": 150})
    assert fits[5]["valid"]
    lo, hi = pv.p_blend(5, 0.3, 2.0), pv.p_blend(5, 0.3, 9.0)
    assert 0 < lo < hi < 1
    assert pv.p_blend(21, 0.3, 9.0) is None and pv.p_blend(5, None, 9.0) is None and pv.p_blend(5, 0.3, None) is None
    with sqlite3.connect(str(db_module.DB_PATH)) as c:                 # now make it look worse than the simulator alone
        c.execute("UPDATE blend_models SET brier_blend = brier_sim_only + 0.01")
        c.commit()
    assert pv.get_valid_blend(5) is None and pv.p_blend(5, 0.3, 9.0) is None


def test_p_clip_keeps_extreme_probabilities_finite():
    assert np.isfinite(pv._logit(np.array([0.0, 1.0, 0.5]))).all()
    assert pv.blend_probability((0.0, 1.0, 0.0), np.array([0.0, 1.0]), np.array([5.0, 5.0])) == pytest.approx([0.01, 0.99])


# ── feed-back into the existing systems ───────────────────────────────────────

def test_new_segment_keys_appear_in_the_pattern_miner():
    from portfolio_agent.tools import validation_engine as ve
    rng = np.random.default_rng(9)
    rows = []
    for i in range(120):
        flagged = i < 40
        rows.append({"horizon_days": 5, "predicted_direction": "UP", "actual_direction": "DOWN" if flagged else
                     ("UP" if rng.random() < 0.9 else "DOWN"), "conviction_score": 6.0, "raw_conviction_score": None,
                     "weight_regime": None, "model_name": "x", "trigger_type": "scheduled",
                     "guardrail_flags": json.dumps(["sim_disagrees"]) if flagged else None,
                     "sim_p_target": 0.1 if flagged else 0.4})
    keys = {seg["key"] for seg in ve._mine_segment_patterns(rows)}
    assert ("sim_disagrees", 5, "flagged") in keys and ("sim_p_target", 5, "low") in keys
    assert "flagged" in ve._describe_segment(("sim_disagrees", 5, "flagged")) or "disagrees" in ve._describe_segment(("sim_disagrees", 5, "flagged"))
    assert "simulated" in ve._describe_segment(("sim_p_target", 5, "low"))
    unsimulated = [{**r, "sim_p_target": None} for r in rows]
    assert not [k for k in (s["key"] for s in ve._mine_segment_patterns(unsimulated)) if k[0] in ("sim_disagrees", "sim_p_target")]


def _weekly(significant: bool, date="2026-10-01"):
    with sqlite3.connect(str(db_module.DB_PATH)) as c:
        pdb._create_schema(c)
        c.execute("INSERT INTO validation_reports (report_date, period, patterns_identified, llm_summary, metrics_snapshot, created_at) "
                  "VALUES (?, 'weekly', ?, '', '{}', ?)",
                  [date, json.dumps({"sim_check": {"significantly_worse": significant, "per_horizon": {
                      "5": {"significantly_worse": significant, "rate_disagree": 0.1, "rate_confident": 0.4, "n_disagree": 40,
                            "n_confident": 50, "p_value": 0.01}}}}), date])
        c.commit()


def test_cap_recommendation_needs_two_significant_weeks_of_the_last_four():
    for day, sig in (("2026-09-01", True), ("2026-09-08", False), ("2026-09-15", False), ("2026-09-22", True)):
        _weekly(sig, day)
    r = pv.sim_cap_recommendation()
    assert r["recommend"] and (r["weeks_significant"], r["weeks_checked"]) == (2, 4)
    _weekly(False, "2026-09-29")                                       # the oldest significant week falls out of the window
    r = pv.sim_cap_recommendation()
    assert not r["recommend"] and (r["weeks_significant"], r["weeks_checked"]) == (1, 4)


def test_cap_recommendation_is_quiet_with_no_reports():
    assert pv.sim_cap_recommendation() == {"recommend": False, "weeks_significant": 0, "weeks_checked": 0, "latest": None}


def test_one_click_switch_sets_cap_and_requires_admin(monkeypatch):
    from portfolio_agent.services import investor_service as svc
    from portfolio_agent.services.context import local_context
    ctx = local_context("test")
    with pytest.raises(svc.NotAdmin):
        svc.update_sim_settings(ctx, {"sim_guardrail_mode": "cap"})
    monkeypatch.setattr(svc, "require_admin", lambda c: None)
    assert svc.update_sim_settings(ctx, {"sim_guardrail_mode": "cap"})["sim_guardrail_mode"] == "cap"
    assert esdb.get_sim_settings()["sim_guardrail_mode"] == "cap"


def test_weekly_analysis_refits_blends_and_records_the_sim_check(monkeypatch):
    from portfolio_agent.tools import validation_engine as ve
    seen = {}
    monkeypatch.setattr(pv, "load_plan_rows", lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(pv, "fit_and_store_blends", lambda df, *a, **k: seen.setdefault("fit", {}) or {})
    out = ve._refit_blends_and_check_sim()
    assert "fit" in seen and out == {"per_horizon": {}, "significantly_worse": False}
    monkeypatch.setattr(pv, "load_plan_rows", lambda *a, **k: 1 / 0)
    assert ve._refit_blends_and_check_sim() is None                    # a failure never breaks the weekly analysis


# ── UI ────────────────────────────────────────────────────────────────────────

def test_view_helpers_on_a_full_report():
    from web.components import plan_validation_view as v
    report = pv.plan_report(_synthetic(n_dates=80, seed=10), {"sim_flag_min_conviction": 7.0, "sim_flag_max_p_target": 0.35, "blend_min_n": 150})
    r = report[5]
    assert r["blend"]["status"] == "ok" and len(r["grid4"]["cells"]) == 16
    assert v._reliability_chart(r["reliability"]).data and v._grid_heatmap(r["grid4"]).data
    head, reading = v.edge_headline(r["edge"])
    assert head.endswith("pts") and reading
    assert v.edge_headline({"n": 0, "edge": None})[0] == "—"
    assert "beat" in v.edge_headline({"n": 50, "edge": 0.1, "lo": 0.05, "hi": 0.15})[1]
    assert "worse" in v.edge_headline({"n": 50, "edge": -0.1, "lo": -0.15, "hi": -0.05})[1]
    assert "Indistinguishable" in v.edge_headline({"n": 50, "edge": 0.01, "lo": -0.04, "hi": 0.06})[1]
    assert pv.plan_report(pd.DataFrame(columns=_synthetic(2, 2).columns)) == {}


def test_blended_odds_on_cards_only_with_a_valid_blend():
    from web.components.trade_plan_view import blended_odds, sim_block_html
    sim = {"sim_status": "ok", "p_target_first": 0.3}
    assert blended_odds(5, sim, 8.0) is None and blended_odds(None, sim, 8.0) is None and blended_odds(5, None, 8.0) is None
    p, conv, y = _blend_data(3000, -1.0, 0.8, 0.25, seed=11)
    pv.fit_and_store_blends(_frame(p, conv, y), {"blend_min_n": 150})
    b = blended_odds(5, sim, 8.0)
    assert 0 < b < 1
    html = sim_block_html({**sim, "p_stop_first": 0.4, "p_timeout": 0.3, "expected_r": 0.1, "paths": 10000}, blend_p=b)
    assert "blended target odds" in html and "blended target odds" not in sim_block_html({**sim, "p_stop_first": 0.4, "p_timeout": 0.3,
                                                                                       "expected_r": 0.1, "paths": 10000})
