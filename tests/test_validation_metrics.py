"""validation_metrics: pure functions, no DB / network. Prices are synthetic."""
from __future__ import annotations

import math

import pandas as pd
import pytest

from portfolio_agent.tools import validation_metrics as vm

P5 = {"p_strong_down": 10, "p_moderate_down": 10, "p_flat": 20, "p_moderate_up": 40, "p_strong_up": 20}


def _frame(rows, horizon=5):
    """rows: (pred_dir, actual_return, benchmark_return, excess_return)"""
    return pd.DataFrame([
        {"ticker": "X", "horizon_days": horizon, "as_of_date": f"2026-01-{i + 1:02d}",
         "evaluation_date": f"2026-01-{i + 1:02d}", "predicted_direction": p, "actual_return": r,
         "benchmark_return": b, "excess_return": e}
        for i, (p, r, b, e) in enumerate(rows)
    ])


ROWS = [
    ("UP",   0.03,  0.02,  0.01),    # UP   actual UP
    ("UP",  -0.03, -0.02, -0.01),    # UP   actual DOWN
    ("DOWN",-0.02,  0.0,  -0.02),    # DOWN actual DOWN
    ("FLAT", 0.004, 0.005,-0.001),   # FLAT actual FLAT
    ("UP",   0.02,  0.03, -0.01),    # UP   actual UP
    ("FLAT", 0.05,  0.02,  0.03),    # FLAT actual UP
]


# ── labels / helpers ──────────────────────────────────────────────────────────

def test_direction_from_return_uses_flat_threshold():
    assert vm.direction_from_return(0.0101) == "UP"
    assert vm.direction_from_return(0.01) == "FLAT"        # boundary is FLAT (strict >)
    assert vm.direction_from_return(-0.01) == "FLAT"
    assert vm.direction_from_return(-0.0101) == "DOWN"
    assert vm.direction_from_return(None) is None


def test_mixed_horizons_raise():
    df = pd.concat([_frame(ROWS, 5), _frame(ROWS, 21)])
    for fn in (vm.compare_predictors, vm.confusion_matrix, vm.beat_market, vm.effective_sample):
        with pytest.raises(ValueError):
            fn(df)


def test_empty_frames_do_not_error():
    empty = pd.DataFrame()
    assert vm.compare_predictors(empty)["rows"] == []
    assert vm.confusion_matrix(empty)["n"] == 0
    assert vm.beat_market(empty)["hit_rate"] is None
    assert vm.probability_baselines(empty)["n"] == 0
    assert vm.effective_sample(empty)["nonoverlap_n"] == 0
    assert vm.price_requests(empty) == {}
    assert vm.horizon_report(empty)["comparison"]["n_total"] == 0


# ── trailing return: trading days by position, not calendar ───────────────────

DATES = [d.date().isoformat() for d in pd.bdate_range("2026-01-05", periods=20)]


def _closes(fn):
    return {d: fn(i) for i, d in enumerate(DATES)}


def test_trailing_return_counts_trading_days_by_position():
    closes = _closes(lambda i: 100.0 + i)           # 100,101,...
    # as_of = DATES[10] (a Monday after a weekend); 5 trading days back is DATES[5]
    assert vm.trailing_return(closes, DATES[10], 5) == pytest.approx(110 / 105 - 1)


def test_trailing_return_on_non_trading_day_uses_prior_close():
    closes = _closes(lambda i: 100.0 + i)
    saturday = "2026-01-17"                         # DATES[9] is Fri 2026-01-16
    assert DATES[9] == "2026-01-16"
    assert vm.trailing_return(closes, saturday, 5) == pytest.approx(109 / 104 - 1)


def test_trailing_return_missing_short_or_stale_is_none():
    closes = _closes(lambda i: 100.0 + i)
    assert vm.trailing_return({}, DATES[10], 5) is None
    assert vm.trailing_return(closes, DATES[3], 5) is None                  # not enough history
    assert vm.trailing_return(closes, "2026-03-01", 5) is None              # series ends long before as_of


# ── baselines ─────────────────────────────────────────────────────────────────

def test_always_and_majority_and_spy_same_window_baselines():
    lab = vm.baseline_labels(_frame(ROWS))
    assert set(lab["always_up"]) == {"UP"} and set(lab["always_down"]) == {"DOWN"} and set(lab["always_flat"]) == {"FLAT"}
    assert set(lab["majority_class"]) == {"UP"}                              # 3 UP, 2 DOWN, 1 FLAT
    assert lab["spy_same_window"].tolist() == ["UP", "DOWN", "FLAT", "FLAT", "UP", "UP"]
    assert lab["spy_prior_window"].isna().all() and lab["momentum"].isna().all()   # no prices given


def test_prior_window_baselines_use_prior_trading_days():
    df = pd.DataFrame([
        {"ticker": "X", "horizon_days": 5, "as_of_date": DATES[10], "evaluation_date": DATES[15],
         "predicted_direction": "UP", "actual_return": 0.03, "benchmark_return": 0.0, "excess_return": 0.03},
        {"ticker": "X", "horizon_days": 5, "as_of_date": DATES[12], "evaluation_date": DATES[17],
         "predicted_direction": "UP", "actual_return": 0.03, "benchmark_return": 0.0, "excess_return": 0.03},
    ])
    x = _closes(lambda i: 100.0 if i <= 5 else 102.0)       # +2% into idx 10; flat into idx 12
    spy = _closes(lambda i: 100.0 if i <= 5 else 98.0)      # -2% into idx 10; flat into idx 12
    lab = vm.baseline_labels(df, {"X": x, "SPY": spy})
    assert lab["momentum"].tolist() == ["UP", "FLAT"]
    assert lab["spy_prior_window"].tolist() == ["DOWN", "FLAT"]


def test_price_requests_one_per_ticker_plus_spy_with_padding():
    df = _frame(ROWS)
    req = vm.price_requests(df)
    assert set(req) == {"X", "SPY"}
    start, end = req["X"]
    assert end == "2026-01-06" and start < "2026-01-01"


# ── classification metrics ────────────────────────────────────────────────────

def test_apex_metrics_hand_computed():
    d = vm.prepare_rows(_frame(ROWS))
    m = vm.classification_metrics(d["actual_dir"], d["pred_dir"])
    assert m["n"] == 6
    assert m["accuracy"] == pytest.approx(4 / 6)
    assert m["balanced_accuracy"] == pytest.approx((2 / 3 + 1 / 2 + 1) / 3)
    assert m["macro_f1"] == pytest.approx(2 / 3)


def test_expected_random_baselines():
    d = vm.prepare_rows(_frame(ROWS))                       # freq UP .5, DOWN 1/3, FLAT 1/6
    cf = vm.expected_random_metrics(d["actual_dir"], "class_frequency_random")
    assert cf["accuracy"] == pytest.approx(14 / 36)
    assert cf["balanced_accuracy"] == pytest.approx(1 / 3)
    un = vm.expected_random_metrics(d["actual_dir"], "uniform_random")
    assert un["accuracy"] == pytest.approx(1 / 3) and un["balanced_accuracy"] == pytest.approx(1 / 3)


def test_compare_predictors_common_subset_and_best_baseline():
    cmp = vm.compare_predictors(_frame(ROWS))
    assert cmp["n_total"] == 6 and cmp["n_common"] == 6
    assert set(cmp["unavailable"]) == {"spy_prior_window", "momentum"}      # no prices -> dropped, not fatal
    by = {r["key"]: r for r in cmp["rows"]}
    assert by["always_up"]["accuracy"] == pytest.approx(0.5)
    assert by["majority_class"]["accuracy"] == pytest.approx(0.5)
    assert by["spy_same_window"]["accuracy"] == pytest.approx(5 / 6)
    assert by["apex"]["accuracy"] == pytest.approx(4 / 6)
    # the look-ahead SPY diagnostic never counts as "best baseline"
    assert cmp["best_baseline"]["key"] in {"always_up", "majority_class"}
    assert cmp["best_baseline"]["accuracy"] == pytest.approx(0.5)


def test_compare_predictors_restricts_to_rows_where_all_baselines_defined():
    df = _frame(ROWS)
    df.loc[0, "benchmark_return"] = None                    # row 0 loses spy_same_window
    cmp = vm.compare_predictors(df)
    assert cmp["n_total"] == 6 and cmp["n_common"] == 5
    assert {r["n"] for r in cmp["rows"]} == {5}
    assert cmp["apex_all_rows"]["n"] == 6


# ── confusion matrix ──────────────────────────────────────────────────────────

def test_confusion_matrix_and_mixes():
    cm = vm.confusion_matrix(_frame(ROWS))
    assert cm["matrix"]["UP"] == {"UP": 2, "DOWN": 1, "FLAT": 0}
    assert cm["matrix"]["DOWN"] == {"UP": 0, "DOWN": 1, "FLAT": 0}
    assert cm["matrix"]["FLAT"] == {"UP": 1, "DOWN": 0, "FLAT": 1}
    assert cm["predicted_mix"] == {"UP": pytest.approx(0.5), "DOWN": pytest.approx(1 / 6), "FLAT": pytest.approx(1 / 3)}
    assert cm["actual_mix"]["UP"] == pytest.approx(0.5)


# ── beat-market ───────────────────────────────────────────────────────────────

def test_beat_market_hit_rate_and_always_up_baseline():
    bm = vm.beat_market(_frame(ROWS))
    assert bm["up"]["n"] == 3 and bm["up"]["hit_rate"] == pytest.approx(1 / 3)
    assert bm["up"]["mean_excess"] == pytest.approx((0.01 - 0.01 - 0.01) / 3)
    assert bm["down"]["n"] == 1 and bm["down"]["hit_rate"] == pytest.approx(1.0)     # excess -2% < 0
    assert bm["n_calls"] == 4 and bm["hit_rate"] == pytest.approx(2 / 4)
    assert bm["always_up_hit_rate"] == pytest.approx(2 / 6)
    assert bm["flat"]["n"] == 2 and bm["flat"]["mean_excess"] == pytest.approx((-0.001 + 0.03) / 2)


def test_beat_market_skips_rows_without_excess_return():
    df = _frame(ROWS)
    df["excess_return"] = None
    bm = vm.beat_market(df)
    assert bm["n_rows"] == 0 and bm["hit_rate"] is None


# ── probability baselines ─────────────────────────────────────────────────────

def _prob_frame():
    return pd.DataFrame([
        {"ticker": "X", "horizon_days": 5, "actual_return": 0.03, **P5},     # moderate_up
        {"ticker": "Y", "horizon_days": 5, "actual_return": -0.08, **P5},    # strong_down
    ])


def test_probability_baselines_hand_computed():
    pb = vm.probability_baselines(_prob_frame())
    assert pb["n"] == 2
    b = pb["binary"]                                        # p_up = .6; actual_up = 1, 0
    assert b["brier"] == pytest.approx((0.16 + 0.36) / 2)
    assert b["base_rate"] == pytest.approx(0.5)
    assert b["baseline_brier"] == pytest.approx(0.25)
    assert b["brier_skill"] == pytest.approx(1 - 0.26 / 0.25)
    assert b["baseline_log_loss"] == pytest.approx(math.log(2))
    assert b["log_loss"] == pytest.approx((-math.log(0.6) - math.log(0.4)) / 2)
    f = pb["five"]
    assert f["brier"] == pytest.approx((0.46 + 1.06) / 2)
    assert f["baseline_brier"] == pytest.approx(0.5)                        # q = .5 / .5 -> .25 + .25
    assert f["brier_skill"] == pytest.approx(1 - 0.76 / 0.5)
    assert f["baseline_log_loss"] == pytest.approx(math.log(2))
    assert f["log_loss"] == pytest.approx((-math.log(0.4) - math.log(0.1)) / 2)


def test_probability_baselines_skip_rows_without_distribution():
    df = _prob_frame()
    df.loc[1, "p_flat"] = None
    assert vm.probability_baselines(df)["n"] == 1
    assert vm.probability_baselines(df.drop(columns=["p_flat"]))["n"] == 0


def test_probability_baseline_skill_sign():
    up = {"p_strong_down": 0, "p_moderate_down": 5, "p_flat": 5, "p_moderate_up": 80, "p_strong_up": 10}
    down = {"p_strong_down": 10, "p_moderate_down": 80, "p_flat": 5, "p_moderate_up": 5, "p_strong_up": 0}
    skilled = [{"ticker": "X", "horizon_days": 5, "actual_return": r, **(up if r > 0 else down)}
               for r in (0.03, 0.02, -0.03, -0.04)]
    pb = vm.probability_baselines(pd.DataFrame(skilled))
    assert pb["binary"]["brier_skill"] > 0 and pb["five"]["brier_skill"] > 0
    # a constant, overconfident forecast on a 50/50 sample has negative skill
    overconf = [{"ticker": "X", "horizon_days": 5, "actual_return": r, **up} for r in (0.03, 0.02, -0.03, -0.04)]
    assert vm.probability_baselines(pd.DataFrame(overconf))["binary"]["brier_skill"] < 0


# ── effective sample ──────────────────────────────────────────────────────────

def _win(ticker, start, end, horizon=5, pred="UP", ret=0.03):
    return {"ticker": ticker, "horizon_days": horizon, "as_of_date": start, "evaluation_date": end,
            "predicted_direction": pred, "actual_return": ret, "benchmark_return": 0.0, "excess_return": ret}


def test_nonoverlap_drops_overlapping_and_keeps_boundary_touching():
    df = pd.DataFrame([
        _win("A", "2026-01-05", "2026-01-12"),
        _win("A", "2026-01-08", "2026-01-15"),      # starts before 01-12 -> dropped
        _win("A", "2026-01-12", "2026-01-19"),      # starts ON previous kept end -> kept
        _win("B", "2026-01-05", "2026-01-12"),      # other ticker is independent
    ])
    kept = vm.nonoverlap_rows(df)
    assert sorted(zip(kept["ticker"], kept["as_of_date"])) == [
        ("A", "2026-01-05"), ("A", "2026-01-12"), ("B", "2026-01-05")]


def test_nonoverlap_all_distinct_windows_keeps_everything():
    df = pd.DataFrame([_win("A", f"2026-0{m}-01", f"2026-0{m}-08") for m in range(1, 5)])
    assert len(vm.nonoverlap_rows(df)) == 4


def test_nonoverlap_missing_evaluation_date_falls_back_to_horizon():
    df = pd.DataFrame([_win("A", "2026-01-05", None), _win("A", "2026-01-08", None), _win("A", "2026-01-20", None)])
    kept = vm.nonoverlap_rows(df)                   # 5d -> ceil(5*7/5)=7 calendar days
    assert kept["as_of_date"].tolist() == ["2026-01-05", "2026-01-20"]


def test_effective_sample_counts_accuracy_and_warning():
    df = pd.DataFrame([
        _win("A", "2026-01-05", "2026-01-12", pred="UP", ret=0.03),     # correct
        _win("A", "2026-01-08", "2026-01-15", pred="UP", ret=0.03),     # overlap, dropped
        _win("A", "2026-01-12", "2026-01-19", pred="DOWN", ret=0.03),   # wrong
    ])
    eff = vm.effective_sample(df, streak_n=2)
    assert (eff["raw_n"], eff["streak_n"], eff["nonoverlap_n"]) == (3, 2, 2)
    assert eff["nonoverlap_accuracy"] == pytest.approx(0.5)
    assert eff["low_sample"] is True and eff["ci_low"] < 0.5 < eff["ci_high"]


# ── Wilson ────────────────────────────────────────────────────────────────────

def test_wilson_ci_known_values():
    lo, hi = vm.wilson_ci(50, 100)
    assert lo == pytest.approx(0.4038, abs=1e-3) and hi == pytest.approx(0.5962, abs=1e-3)
    lo, hi = vm.wilson_ci(10, 10)
    assert lo == pytest.approx(0.7225, abs=1e-3) and hi == pytest.approx(1.0)
    lo, hi = vm.wilson_ci(0, 10)
    assert lo == 0.0 and hi == pytest.approx(0.2775, abs=1e-3)
    assert vm.wilson_ci(0, 0) == (None, None)


def test_wilson_ci_narrows_with_n():
    w_small = vm.wilson_ci(6, 10); w_big = vm.wilson_ci(600, 1000)
    assert (w_big[1] - w_big[0]) < (w_small[1] - w_small[0])
