"""validation_scorecard: verdict rule, evidence level, and that every number is the existing metric's number."""
from __future__ import annotations

import re

import numpy as np
import pandas as pd
import pytest

from portfolio_agent.tools import validation_metrics as vm
from portfolio_agent.tools import validation_scorecard as sc

FORBIDDEN = re.compile(r"\bAUC\b|\bIC\b|Brier|bootstrap|Spearman|log-loss|rank correlation|\bCI\b", re.I)
USER_FIELDS = ("title", "plain_question", "display", "baseline_label", "one_line_reason", "subtitle")


# ── verdict rule ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("n_dates,lo,hi,base,expected", [
    (9,  0.01, 0.05, 0.0, "Too early"),          # one date short, even with a clearly positive CI
    (10, 0.01, 0.05, 0.0, "Working"),            # exactly MIN_DATES_FOR_VERDICT may be judged
    (10, -0.05, -0.01, 0.0, "Not working"),
    (10, -0.02, 0.03, 0.0, "Unproven"),          # straddles
    (10, 0.0, 0.05, 0.0, "Unproven"),            # touching the baseline is not "entirely better"
    (10, -0.05, 0.0, 0.0, "Unproven"),
    (25, 0.40, 0.50, 0.38, "Working"),           # non-zero baseline (accuracy vs best naive)
    (25, 0.36, 0.50, 0.38, "Unproven"),
    (25, 0.30, 0.36, 0.38, "Not working"),
    (10, None, None, 0.0, "Unproven"),           # enough dates but no interval
    (3,  None, None, 0.0, "Too early"),
    (0,  0.01, 0.05, 0.0, "Too early"),
])
def test_verdict_boundaries(n_dates, lo, hi, base, expected):
    assert sc.verdict(n_dates, lo, hi, base) == expected


def test_verdict_lower_is_better_flips_direction():
    assert sc.verdict(20, -0.05, -0.01, 0.0, higher_is_better=False) == "Working"
    assert sc.verdict(20, 0.01, 0.05, 0.0, higher_is_better=False) == "Not working"


def test_verdict_threshold_is_configurable(monkeypatch):
    monkeypatch.setattr(sc, "MIN_DATES_FOR_VERDICT", 3)
    assert sc.verdict(3, 0.01, 0.05) == "Working" and sc.verdict(2, 0.01, 0.05) == "Too early"


@pytest.mark.parametrize("n,level", [(0, "Too early"), (9, "Too early"), (10, "Early"), (29, "Early"), (30, "Established"), (200, "Established")])
def test_evidence_level_boundaries(n, level):
    assert sc.evidence_level(n) == level


# ── synthetic frame ───────────────────────────────────────────────────────────

def _frame(n_dates=12, per_date=14, horizon=5, buy_better=True):
    rows = []
    for d in range(n_dates):
        date = (pd.Timestamp("2026-01-05") + pd.Timedelta(days=d)).date().isoformat()
        end = (pd.Timestamp(date) + pd.Timedelta(days=7)).date().isoformat()
        for i in range(per_date):
            hi = i >= per_date // 2
            excess = (0.002 * i if buy_better else -0.002 * i) + 0.0003 * ((d * 7 + i) % 5)
            pu = 0.15 + 0.7 * i / (per_date - 1)
            rows.append({
                "id": len(rows) + 1, "ticker": f"T{d}_{i}", "horizon_days": horizon, "as_of_date": date, "evaluation_date": end,
                "recommendation": "BUY" if hi else "HOLD", "predicted_direction": "UP" if hi else "FLAT",
                "actual_return": excess + 0.003, "benchmark_return": 0.003, "excess_return": excess,
                "composite_score": float(i), "model_name": "m", "in_predicted_range": 1 if i % 4 == 0 else 0,
                "p_strong_down": 5, "p_moderate_down": 10, "p_flat": 100 - 5 - 10 - round(pu * 100) , "p_moderate_up": round(pu * 100) - 5, "p_strong_up": 5,
            })
    return pd.DataFrame(rows)


def test_scorecard_numbers_are_the_existing_metric_numbers():
    df = _frame()
    card = sc.compute_scorecard(df, 5, streak_n=40)
    d = vm.prepare_rows(df)

    # ranking: BUY minus others, and daily rank IC, straight from validation_metrics
    r = card.item("ranking")
    is_buy = d["recommendation"].isin(["BUY", "STRONG_BUY"])
    assert r.value == pytest.approx(d.loc[is_buy, "excess_return"].mean() - d.loc[~is_buy, "excess_return"].mean())
    ic = vm.rank_ic(d, d["composite_score"], 100, np.random.default_rng(0))
    assert r.extras["rank_ic"]["mean"] == pytest.approx(ic["mean"])
    assert r.ci_low is not None and r.ci_low < r.value < r.ci_high or r.ci_low <= r.value <= r.ci_high

    # direction: APEX non-overlapping accuracy and the best baseline on those same rows
    dr = card.item("direction")
    eff = vm.effective_sample(d, 40)
    assert dr.value == pytest.approx(eff["nonoverlap_accuracy"])
    assert (dr.ci_low, dr.ci_high) == (eff["ci_low"], eff["ci_high"])
    assert dr.baseline == pytest.approx(vm.compare_predictors(vm.nonoverlap_rows(d))["best_baseline"]["accuracy"])
    assert dr.n == eff["nonoverlap_n"]

    # probabilities: skill == probability_baselines()'s five-bucket skill, and the CI function's point estimate
    pr = card.item("probabilities")
    pb = vm.probability_baselines(d)["five"]
    assert pr.value == pytest.approx(pb["brier_skill"])
    assert vm.five_bucket_skill_ci(d, 50, np.random.default_rng(1))["skill"] == pytest.approx(pb["brier_skill"])
    assert pr.ci_low is not None and pr.ci_high is not None

    # ranges: the stored flag's mean
    rg = card.item("ranges")
    assert rg.value == pytest.approx(df["in_predicted_range"].mean()) and rg.verdict == "Info only"
    assert rg.ci_low is None and "no stated coverage target" in rg.one_line_reason

    # evidence
    ev = card.evidence
    assert (ev.n_raw, ev.n_streaks, ev.n_nonoverlap, ev.n_dates) == (len(df), 40, len(df), 12)
    assert (ev.first_date, ev.last_date, ev.level) == ("2026-01-05", "2026-01-16", "Early")
    assert len(card.trend) == 12 and card.trend[0][0] == "2026-01-05"
    assert {x["recommendation"]: x["n"] for x in card.by_recommendation}["BUY"] == len(df) // 2


def test_working_verdict_when_buy_clearly_better_and_enough_dates():
    card = sc.compute_scorecard(_frame(n_dates=12), 5)
    r = card.item("ranking")
    assert r.verdict == "Working" and r.ci_low > 0 and "beat the others" in r.one_line_reason


def test_not_working_when_buy_clearly_worse():
    r = sc.compute_scorecard(_frame(n_dates=12, buy_better=False), 5).item("ranking")
    assert r.verdict == "Not working" and r.ci_high < 0 and "trailed" in r.one_line_reason


def test_too_early_below_min_dates_but_numbers_still_shown():
    card = sc.compute_scorecard(_frame(n_dates=9), 5)
    assert card.evidence.level == "Too early"
    r = card.item("ranking")
    assert r.verdict == "Too early" and r.value is not None and "9 prediction dates" in r.one_line_reason


def test_no_non_buy_group_is_unproven_and_says_so():
    df = _frame(n_dates=12)
    df["recommendation"] = "BUY"
    r = sc.compute_scorecard(df, 5).item("ranking")
    assert r.verdict == "Unproven" and r.value is None and "no non-BUY" in r.one_line_reason


def test_only_the_requested_horizon_is_used():
    df = pd.concat([_frame(n_dates=12, horizon=5), _frame(n_dates=3, horizon=21)], ignore_index=True)
    assert sc.compute_scorecard(df, 5).evidence.n_raw == len(_frame(n_dates=12))
    assert sc.compute_scorecard(df, 21).evidence.n_dates == 3
    assert sc.compute_scorecard(df, 63).evidence.n_raw == 0


def test_user_facing_strings_have_no_statistics_jargon():
    for df in (_frame(n_dates=12), _frame(n_dates=4), _frame(n_dates=12, buy_better=False), pd.DataFrame()):
        for item in sc.compute_scorecard(df, 5).items:
            for f in USER_FIELDS:
                assert not FORBIDDEN.search(getattr(item, f)), (item.key, f, getattr(item, f))
            assert item.help_text                       # the technical definition lives here


# ── degraded input never raises ───────────────────────────────────────────────

@pytest.mark.parametrize("df", [None, pd.DataFrame(), pd.DataFrame({"x": [1]}),
                                pd.DataFrame([{"horizon_days": 5, "as_of_date": "2026-01-05", "predicted_direction": "UP", "actual_return": 0.01}])])
def test_empty_or_short_or_malformed_input_yields_too_early_items(df):
    card = sc.compute_scorecard(df, 5)
    assert [i.key for i in card.items] == ["ranking", "direction", "probabilities", "ranges"]
    for i in card.items:
        assert i.verdict in ("Too early", "Info only", "Unproven")
        assert i.one_line_reason and i.help_text
    assert card.evidence.level == "Too early" and card.evidence.n_dates <= 1


def test_missing_optional_columns_degrade_per_item():
    df = _frame(n_dates=12).drop(columns=["composite_score", "recommendation", "in_predicted_range"] + vm.P_COLUMNS)
    card = sc.compute_scorecard(df, 5)
    assert card.item("probabilities").verdict == "Too early" and card.item("probabilities").value is None
    assert card.item("direction").value is not None                       # still computable


# ── additions to validation_metrics used by the scorecard ────────────────────

def test_group_excess_contrast_hand_computed():
    d = pd.DataFrame({"as_of_date": ["2026-01-05"] * 2 + ["2026-01-06"] * 2, "excess_return": [0.04, 0.02, -0.01, 0.01]})
    out = vm.group_excess_contrast(d, pd.Series([True, False, True, False]), 200, np.random.default_rng(0))
    assert out["mean_a"] == pytest.approx(0.015) and out["mean_b"] == pytest.approx(0.015) and out["diff"] == pytest.approx(0.0)
    assert out["n_a"] == 2 and out["n_b"] == 2 and out["n_dates"] == 2
    assert vm.group_excess_contrast(d, pd.Series([False] * 4), 50, np.random.default_rng(0))["diff"] is None


def test_rank_ic_by_date_matches_rank_ic_mean():
    df = _frame(n_dates=5)
    d = vm.prepare_rows(df)
    by_date = vm.rank_ic_by_date(d, d["composite_score"])
    assert len(by_date) == 5
    assert np.mean(list(by_date.values())) == pytest.approx(vm.rank_ic(d, d["composite_score"], 50, np.random.default_rng(0))["mean"])
