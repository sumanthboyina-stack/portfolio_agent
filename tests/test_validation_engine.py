from __future__ import annotations

import math

import pytest

from portfolio_agent.tools.validation_engine import (
    _extract_p_up,
    actual_return_bucket,
    compute_distribution_metrics,
    score_outcome,
)


@pytest.mark.parametrize("ret,expected", [
    (-0.10, "strong_down"),
    (-0.06, "strong_down"),
    (-0.05, "moderate_down"),
    (-0.011, "moderate_down"),
    (-0.01, "flat"),
    (0.0, "flat"),
    (0.0099, "flat"),
    (0.01, "moderate_up"),
    (0.049, "moderate_up"),
    (0.05, "strong_up"),
    (0.10, "strong_up"),
])
def test_actual_return_bucket_boundaries(ret, expected):
    assert actual_return_bucket(ret) == expected


def test_extract_p_up_missing_distribution_returns_none():
    assert _extract_p_up({}) is None
    assert _extract_p_up({"p_strong_down": 10, "p_moderate_down": 20}) is None


def test_extract_p_up_computes_upside_probability():
    pred = {
        "p_strong_down": 10, "p_moderate_down": 10, "p_flat": 20,
        "p_moderate_up": 30, "p_strong_up": 30,
    }
    assert _extract_p_up(pred) == pytest.approx(0.6)


def test_extract_p_up_normalizes_against_fractional_total():
    pred = {
        "p_strong_down": 0.1, "p_moderate_down": 0.1, "p_flat": 0.2,
        "p_moderate_up": 0.3, "p_strong_up": 0.3,
    }
    assert _extract_p_up(pred) == pytest.approx(0.6)


def test_compute_distribution_metrics_missing_distribution_returns_none_none():
    brier, log_loss = compute_distribution_metrics({}, actual_ret=0.02)
    assert brier is None
    assert log_loss is None


def test_compute_distribution_metrics_known_values():
    pred = {
        "p_strong_down": 5, "p_moderate_down": 5, "p_flat": 20,
        "p_moderate_up": 35, "p_strong_up": 35,
    }
    # p_up = 0.7, actual_ret > 0.005 so actual_up = 1
    brier, log_loss = compute_distribution_metrics(pred, actual_ret=0.02)
    assert brier == pytest.approx((0.7 - 1.0) ** 2)
    assert log_loss == pytest.approx(-math.log(0.7))


def test_compute_distribution_metrics_actual_down():
    pred = {
        "p_strong_down": 5, "p_moderate_down": 5, "p_flat": 20,
        "p_moderate_up": 35, "p_strong_up": 35,
    }
    # actual_ret below +0.5% threshold -> actual_up = 0
    brier, log_loss = compute_distribution_metrics(pred, actual_ret=-0.02)
    assert brier == pytest.approx((0.7 - 0.0) ** 2)
    assert log_loss == pytest.approx(-math.log(1 - 0.7))


def test_score_outcome_strong_correct():
    pred = {"predicted_direction": "UP", "predicted_return_low": 1.0, "predicted_return_high": 5.0}
    outcome = score_outcome(pred, actual_return=0.03)
    assert outcome.label == "strong_correct"
    assert outcome.score == 1.0


def test_score_outcome_directionally_correct_but_outside_range():
    pred = {"predicted_direction": "UP", "predicted_return_low": 1.0, "predicted_return_high": 2.0}
    outcome = score_outcome(pred, actual_return=0.10)
    assert outcome.label == "directionally_correct"
    assert outcome.score == 0.7


def test_score_outcome_matching_flat_prediction_is_directionally_correct():
    # NOTE: the "flat_correct" branch in score_outcome() (validation_engine.py:118)
    # is unreachable dead code — it requires pred_dir == "FLAT" and actual_dir == "FLAT",
    # but that's a strict subset of the dir_correct check one branch above, which
    # already returns "directionally_correct" (0.7) first. A flat call that matches
    # the flat outcome currently scores 0.7, never the intended 0.8.
    pred = {"predicted_direction": "FLAT", "predicted_return_low": 0.0, "predicted_return_high": 0.0}
    outcome = score_outcome(pred, actual_return=0.001)
    assert outcome.label == "directionally_correct"
    assert outcome.score == 0.7


def test_score_outcome_wrong_significant():
    pred = {"predicted_direction": "UP", "predicted_return_low": 1.0, "predicted_return_high": 5.0}
    outcome = score_outcome(pred, actual_return=-0.05)
    assert outcome.label == "wrong_significant"
    assert outcome.score == 0.0


def test_score_outcome_wrong_minor():
    pred = {"predicted_direction": "UP", "predicted_return_low": 1.0, "predicted_return_high": 5.0}
    outcome = score_outcome(pred, actual_return=-0.005)
    assert outcome.label == "wrong_minor"
    assert outcome.score == 0.3


def test_score_outcome_direction_is_not_flipped():
    """Regression guard: a predicted DOWN with an actual down move must score
    as correct, not wrong — catches a predicted/actual direction sign flip."""
    pred = {"predicted_direction": "DOWN", "predicted_return_low": -5.0, "predicted_return_high": -1.0}
    outcome = score_outcome(pred, actual_return=-0.03)
    assert outcome.label == "strong_correct"
    assert outcome.score == 1.0
