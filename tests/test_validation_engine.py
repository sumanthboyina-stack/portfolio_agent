from __future__ import annotations

import math

import pytest

from portfolio_agent.tools.validation_engine import (
    CORRECT_OUTCOMES,
    FLAT_THRESHOLD,
    _extract_p_up,
    actual_return_bucket,
    brier_5bucket,
    logloss_5bucket,
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


def test_score_outcome_matching_flat_out_of_range_is_flat_correct():
    pred = {"predicted_direction": "FLAT", "predicted_return_low": 0.0, "predicted_return_high": 0.0}
    outcome = score_outcome(pred, actual_return=0.001)
    assert outcome.label == "flat_correct"
    assert outcome.score == 0.8


def test_score_outcome_matching_flat_with_range_miss_is_flat_correct():
    pred = {"predicted_direction": "FLAT", "predicted_return_low": -0.5, "predicted_return_high": 0.5}
    assert score_outcome(pred, actual_return=0.008).label == "flat_correct"   # flat band, outside +-0.5%


def test_score_outcome_matching_flat_in_range_is_strong_correct():
    pred = {"predicted_direction": "FLAT", "predicted_return_low": -1.0, "predicted_return_high": 1.0}
    outcome = score_outcome(pred, actual_return=0.002)
    assert outcome.label == "strong_correct"
    assert outcome.score == 1.0


def test_score_outcome_flat_call_on_a_move_is_wrong():
    pred = {"predicted_direction": "FLAT", "predicted_return_low": 0.0, "predicted_return_high": 0.0}
    assert score_outcome(pred, actual_return=0.03).label == "wrong_significant"
    assert score_outcome(pred, actual_return=0.015).label == "wrong_minor"


def _old_score_outcome_label(pred, actual_return):
    """Pre-fix implementation (flat_correct branch unreachable), kept as an oracle."""
    actual_dir = "UP" if actual_return > FLAT_THRESHOLD else "DOWN" if actual_return < -FLAT_THRESHOLD else "FLAT"
    pred_dir = (pred.get("predicted_direction") or "").upper()
    lo = (pred.get("predicted_return_low") or 0.0) / 100
    hi = (pred.get("predicted_return_high") or 0.0) / 100
    in_range = bool(lo != 0 or hi != 0) and (lo <= actual_return <= hi)
    if pred_dir == actual_dir and in_range:
        return "strong_correct"
    if pred_dir == actual_dir:
        return "directionally_correct"
    if actual_dir == "FLAT" and pred_dir == "FLAT":
        return "flat_correct"
    return "wrong_significant" if abs(actual_return) > 0.02 else "wrong_minor"


def test_label_fix_does_not_change_headline_accuracy():
    """Right/wrong status (what headline directional accuracy counts) must be
    identical before and after the flat_correct fix, and equal to
    predicted_direction == actual_direction."""
    rets = [-0.08, -0.03, -0.015, -0.01, -0.004, 0.0, 0.004, 0.01, 0.012, 0.03, 0.09]
    ranges = [(0.0, 0.0), (-1.0, 1.0), (1.0, 5.0), (-5.0, -1.0), (-0.5, 0.5)]
    for d in ("UP", "DOWN", "FLAT", None):
        for lo, hi in ranges:
            for r in rets:
                pred = {"predicted_direction": d, "predicted_return_low": lo, "predicted_return_high": hi}
                new = score_outcome(pred, r).label
                actual = "UP" if r > FLAT_THRESHOLD else "DOWN" if r < -FLAT_THRESHOLD else "FLAT"
                assert (new in CORRECT_OUTCOMES) == (_old_score_outcome_label(pred, r) in CORRECT_OUTCOMES)
                assert (new in CORRECT_OUTCOMES) == ((d or "") == actual)


# ── 5-bucket Brier / log-loss ─────────────────────────────────────────────────

_P100 = {"p_strong_down": 10, "p_moderate_down": 10, "p_flat": 20, "p_moderate_up": 40, "p_strong_up": 20}


def test_brier_5bucket_hand_computed():
    # actual +3% -> moderate_up. (0.1)^2+(0.1)^2+(0.2)^2+(0.4-1)^2+(0.2)^2 = 0.01+0.01+0.04+0.36+0.04
    assert brier_5bucket(_P100, 0.03) == pytest.approx(0.46)
    # actual -8% -> strong_down: (0.1-1)^2 + 0.01 + 0.04 + 0.16 + 0.04 = 1.06
    assert brier_5bucket(_P100, -0.08) == pytest.approx(1.06)


def test_5bucket_scores_normalise_totals():
    as_fractions = {k: v / 100 for k, v in _P100.items()}
    off_total = {k: v * 0.9 for k, v in _P100.items()}          # sums to 90, same shape
    for pred in (as_fractions, off_total):
        assert brier_5bucket(pred, 0.03) == pytest.approx(0.46)
        assert logloss_5bucket(pred, 0.03) == pytest.approx(-math.log(0.4))


def test_5bucket_logloss_hand_computed_and_clipped():
    assert logloss_5bucket(_P100, 0.0) == pytest.approx(-math.log(0.2))        # flat bucket
    zero = {**_P100, "p_strong_up": 0, "p_moderate_up": 60}
    assert logloss_5bucket(zero, 0.09) == pytest.approx(-math.log(1e-6))      # p=0 -> clipped


def test_5bucket_missing_bucket_is_none():
    partial = {k: v for k, v in _P100.items() if k != "p_flat"}
    assert brier_5bucket(partial, 0.01) is None
    assert logloss_5bucket(partial, 0.01) is None
    assert brier_5bucket({**_P100, "p_flat": None}, 0.01) is None
    assert brier_5bucket({k: 0 for k in _P100}, 0.01) is None


# ── recompute_outcome_labels ──────────────────────────────────────────────────

def test_recompute_outcome_labels_is_idempotent_and_guards_correctness(tmp_path, monkeypatch):
    import portfolio_agent.tools.db as db_module
    from portfolio_agent.tools import prediction_db as pdb
    from portfolio_agent.tools import validation_engine as ve

    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "t.db")
    with pdb._db() as c:
        def ins(direction, lo, hi, ret, outcome, score):
            c.execute(
                """INSERT INTO predictions (ticker, created_at, prediction, recommendation, horizon_days, predicted_direction,
                       predicted_return_low, predicted_return_high, actual_return, outcome,
                       outcome_score, evaluation_status)
                   VALUES ('T','2026-01-01','x','HOLD',5,?,?,?,?,?,?, 'evaluated')""",
                [direction, lo, hi, ret, outcome, score])
        ins("FLAT", 0, 0, 0.002, "directionally_correct", 0.7)   # -> flat_correct
        ins("UP", 1, 5, 0.03, "strong_correct", 1.0)             # unchanged
        ins("UP", 1, 5, 0.03, "wrong_minor", 0.3)                # would flip wrong->right: skipped
        c.commit()
    first = ve.recompute_outcome_labels()
    assert first["changed"] == 1 and first["skipped_correctness_flip"] == 1
    assert first["transitions"] == {"directionally_correct -> flat_correct": 1}
    second = ve.recompute_outcome_labels()
    assert second["changed"] == 0
    with pdb._db() as c:
        row = c.execute("SELECT outcome, outcome_score FROM predictions WHERE predicted_return_low = 0").fetchone()
    assert (row["outcome"], row["outcome_score"]) == ("flat_correct", 0.8)
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
