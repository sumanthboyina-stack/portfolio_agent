"""validation_metrics: pure functions, no DB / network. Prices are synthetic."""
from __future__ import annotations

import math

import numpy as np
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


# ── date-aware ranking metrics ────────────────────────────────────────────────

def _r(ticker, date, pred, excess, p_up=0.5, composite=5.0, model="m1", ret=None, p_flat=None):
    """One evaluated row; p_up is placed on moderate_up, the rest on flat/moderate_down."""
    pf = (1 - p_up) / 2 if p_flat is None else p_flat
    pd_ = 1 - p_up - pf
    return {"ticker": ticker, "horizon_days": 5, "as_of_date": date, "evaluation_date": date,
            "predicted_direction": pred, "actual_return": excess if ret is None else ret,
            "benchmark_return": 0.0, "excess_return": excess, "composite_score": composite, "model_name": model,
            "p_strong_down": 0.0, "p_moderate_down": pd_ * 100, "p_flat": pf * 100,
            "p_moderate_up": p_up * 100, "p_strong_up": 0.0}


def test_auc_hand_computed_and_edges():
    assert vm.auc_score([1, 2, 3, 4], [0, 1, 0, 1]) == pytest.approx(3 / 4)
    assert vm.auc_score([1, 2, 3, 4], [0, 0, 1, 1]) == pytest.approx(1.0)
    assert vm.auc_score([1, 2, 3, 4], [1, 1, 0, 0]) == pytest.approx(0.0)
    assert vm.auc_score([5, 5, 5, 5], [1, 0, 1, 0]) == pytest.approx(0.5)          # all tied
    assert vm.auc_score([1, 2], [1, 1]) is None                                    # one class only


def test_spearman_known_and_degenerate():
    assert vm.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert vm.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert vm.spearman([1, 1, 1, 1], [1, 2, 3, 4]) is None
    assert vm.spearman([1, 2], [1, 2]) is None


def _cross_section(date, n, sign=+1):
    """n tickers on `date`; score rises with excess when sign=+1, falls when -1."""
    return [_r(f"T{date[-2:]}{i}", date, "UP", 0.001 * i * sign, p_up=0.05 + 0.9 * i / (n - 1), composite=float(i))
            for i in range(n)]


def test_rank_ic_is_averaged_over_dates_and_short_dates_are_dropped():
    rows = _cross_section("2026-01-05", 12) + _cross_section("2026-01-06", 12) + _cross_section("2026-01-07", 12, -1) \
        + _cross_section("2026-01-08", 5)                                         # < MIN_CS_ROWS: dropped
    rep = vm.ranking_report(pd.DataFrame(rows), n_boot=200, by_model=False)
    ic = rep["rank_ic"]["p_up"]
    assert ic["mean"] == pytest.approx((1 + 1 - 1) / 3)
    assert ic["n_dates"] == 3 and ic["n_dates_dropped"] == 1 and ic["share_positive"] == pytest.approx(2 / 3)
    assert ic["ci_low"] <= ic["mean"] <= ic["ci_high"]
    assert rep["n_dates"] == 4


def test_auc_vs_beat_spy_pooled_and_within_date():
    rows = _cross_section("2026-01-05", 12) + _cross_section("2026-01-06", 12)
    for r in rows:                                   # excess > 0 only for the top half of each date
        r["excess_return"] = 0.01 if r["composite_score"] >= 6 else -0.01
    rep = vm.ranking_report(pd.DataFrame(rows), n_boot=200, by_model=False)
    a = rep["auc"]["p_up"]
    assert a["auc"] == pytest.approx(1.0) and a["within_date"]["mean"] == pytest.approx(1.0)
    assert a["n"] == 24 and a["n_pos"] == 12 and a["n_dates"] == 2


def test_label_excess_means_ns_dates_and_diffs():
    rows = [
        _r("A", "2026-01-05", "UP", 0.02), _r("B", "2026-01-05", "UP", 0.04), _r("C", "2026-01-05", "FLAT", -0.02),
        _r("D", "2026-01-06", "UP", 0.00), _r("E", "2026-01-06", "FLAT", -0.04), _r("F", "2026-01-06", "DOWN", -0.06),
    ]
    le = vm.ranking_report(pd.DataFrame(rows), n_boot=300, by_model=False)["label_excess"]
    assert le["n_dates"] == 2
    assert le["labels"]["UP"]["mean"] == pytest.approx(0.02) and le["labels"]["UP"]["n"] == 3
    assert le["labels"]["FLAT"]["mean"] == pytest.approx(-0.03) and le["labels"]["FLAT"]["n_dates"] == 2
    assert le["labels"]["DOWN"]["n"] == 1 and le["labels"]["DOWN"]["n_dates"] == 1
    assert le["diffs"]["UP-FLAT"]["diff"] == pytest.approx(0.05)
    for lab in ("UP", "FLAT"):
        x = le["labels"][lab]
        assert x["ci_low"] <= x["mean"] <= x["ci_high"]


def test_bootstrap_is_date_blocked_not_row_level():
    # 4 dates x 50 rows; each date has its own constant excess (a "market" day effect). A row-level
    # bootstrap would give a near-zero-width CI; resampling whole dates must be wide.
    rows = [_r(f"T{d}{i}", f"2026-01-0{d}", "UP", e) for d, e in ((1, 0.05), (2, -0.05), (3, 0.03), (4, -0.03)) for i in range(50)]
    x = vm.ranking_report(pd.DataFrame(rows), n_boot=500, by_model=False)["label_excess"]["labels"]["UP"]
    assert x["n"] == 200 and x["n_dates"] == 4 and (x["ci_high"] - x["ci_low"]) > 0.03


def test_single_date_ci_collapses_and_is_flagged_low_dates():
    rows = [_r(f"T{i}", "2026-01-05", "UP", 0.01 * (i % 3)) for i in range(15)]
    rep = vm.ranking_report(pd.DataFrame(rows), n_boot=100, by_model=False)
    x = rep["label_excess"]["labels"]["UP"]
    assert x["ci_low"] == pytest.approx(x["mean"]) and x["ci_high"] == pytest.approx(x["mean"])
    assert rep["low_dates"] is True and rep["n_dates"] == 1


def test_within_ticker_up_vs_flat_removes_composition():
    rows = [
        _r("A", "2026-01-05", "UP", 0.02), _r("A", "2026-01-06", "FLAT", -0.01),     # diff +0.03
        _r("C", "2026-01-05", "UP", 0.05), _r("C", "2026-01-07", "FLAT", 0.01),      # diff +0.04
        _r("B", "2026-01-05", "UP", 0.09),                                           # no FLAT -> excluded
        _r("Z", "2026-01-06", "FLAT", -0.20),                                        # no UP -> excluded
    ]
    wt = vm.ranking_report(pd.DataFrame(rows), n_boot=200, by_model=False)["within_ticker"]
    assert wt["n_tickers"] == 2 and wt["n_obs"] == 4
    assert wt["mean_diff"] == pytest.approx(0.035)


def test_within_ticker_without_overlap_is_empty_not_an_error():
    rows = [_r("A", "2026-01-05", "UP", 0.02), _r("B", "2026-01-06", "FLAT", -0.01)]
    wt = vm.ranking_report(pd.DataFrame(rows), n_boot=50, by_model=False)["within_ticker"]
    assert wt["mean_diff"] is None and wt["n_tickers"] == 0


def test_ranking_report_splits_by_model_and_is_seeded():
    rows = [_r(f"A{i}", "2026-01-05", "UP", 0.01 * i, model="gpt") for i in range(12)] \
        + [_r(f"B{i}", "2026-01-05", "FLAT", -0.01 * i, model="claude") for i in range(12)]
    df = pd.DataFrame(rows)
    rep = vm.ranking_report(df, n_boot=150)
    assert set(rep["by_model"]) == {"gpt", "claude"}
    assert rep["by_model"]["gpt"]["n_rows"] == 12
    assert rep["by_model"]["gpt"]["label_excess"]["labels"]["UP"]["n"] == 12
    assert rep["by_model"]["claude"]["label_excess"]["labels"]["UP"]["n"] == 0
    assert vm.ranking_report(df, n_boot=150)["label_excess"] == rep["label_excess"]      # same seed -> same result


def test_ranking_report_degrades_on_empty_and_missing_columns():
    assert vm.ranking_report(pd.DataFrame())["n_rows"] == 0
    bare = pd.DataFrame([{"ticker": "A", "horizon_days": 5, "as_of_date": "2026-01-05", "predicted_direction": "UP",
                          "actual_return": 0.01, "excess_return": 0.01}])
    rep = vm.ranking_report(bare, n_boot=50)           # no p_*, no composite_score, no model_name
    assert rep["auc"]["p_up"]["auc"] is None and rep["rank_ic"]["composite"]["mean"] is None


def test_p_up_series_normalises_and_flags_missing():
    df = pd.DataFrame([_r("A", "2026-01-05", "UP", 0.0, p_up=0.6), _r("B", "2026-01-05", "UP", 0.0, p_up=0.2)])
    df.loc[1, "p_flat"] = None
    s = vm.p_up_series(df)
    assert s[0] == pytest.approx(0.6) and np.isnan(s[1])


# ── FLAT diagnostic ───────────────────────────────────────────────────────────

def _dist(pred, sd, md, fl, mu, su, ret=0.0):
    return {"ticker": "X", "horizon_days": 5, "as_of_date": "2026-01-05", "predicted_direction": pred,
            "actual_return": ret, "excess_return": ret, "p_strong_down": sd, "p_moderate_down": md,
            "p_flat": fl, "p_moderate_up": mu, "p_strong_up": su}


def test_flat_diagnostic_hand_computed():
    rows = [
        _dist("FLAT", 5, 15, 40, 25, 15),      # p_flat strict max (40); mass3: flat 40 vs up 40 vs down 20 -> tie
        _dist("FLAT", 10, 20, 25, 35, 10),     # flat NOT max (35 is); mass3: up 45 > flat 25
        _dist("UP", 0, 10, 20, 50, 20),        # derived UP, label UP agrees; actual +3% = UP
        _dist("FLAT", 10, 10, 30, 30, 20),     # flat tied with moderate_up? 30 vs 30 -> tied max; mass3 up 50
    ]
    rows[2]["actual_return"] = 0.03
    out = vm.flat_diagnostic(pd.DataFrame(rows))
    fc = out["flat_calls"]
    assert fc["n"] == 3 and fc["p_flat_strict_max"] == 1 and fc["p_flat_tied_max"] == 1 and fc["p_flat_not_max"] == 1
    assert fc["mean_p_flat"] == pytest.approx((0.40 + 0.25 + 0.30) / 3)
    a5 = out["derived_rules"]["argmax5"]
    assert a5["n_ambiguous"] == 1 and a5["n"] == 3                # the tied row is excluded
    assert a5["agrees_with_label"] == pytest.approx(2 / 3)        # row2 derives UP != FLAT
    assert out["calibration"]["mean_p_flat_all_rows"] == pytest.approx((0.40 + 0.25 + 0.20 + 0.30) / 4)
    assert out["calibration"]["actual_flat_rate"] == pytest.approx(3 / 4)


def test_flat_diagnostic_empty_or_no_distribution():
    assert vm.flat_diagnostic(pd.DataFrame())["n_rows"] == 0
    no_p = pd.DataFrame([{"ticker": "A", "horizon_days": 5, "as_of_date": "2026-01-05", "predicted_direction": "FLAT", "actual_return": 0.0}])
    assert vm.flat_diagnostic(no_p)["n_rows"] == 0
