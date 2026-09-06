from __future__ import annotations

import pytest

from portfolio_agent.tools.calibration_analysis import (
    MIN_SAMPLE,
    SCORE_COLUMNS,
    _pearson_correlation,
    _score_vs_return,
    compute_calibration_report,
)


def test_pearson_correlation_perfect_positive():
    xs = [1, 2, 3, 4, 5]
    ys = [2, 4, 6, 8, 10]
    assert _pearson_correlation(xs, ys) == pytest.approx(1.0)


def test_pearson_correlation_perfect_negative():
    xs = [1, 2, 3, 4, 5]
    ys = [10, 8, 6, 4, 2]
    assert _pearson_correlation(xs, ys) == pytest.approx(-1.0)


def test_pearson_correlation_none_with_no_variance():
    assert _pearson_correlation([5, 5, 5], [1, 2, 3]) is None


def test_score_vs_return_insufficient_data_below_min_sample():
    rows = [{"apex_score": i, "return_pct": i * 0.01} for i in range(MIN_SAMPLE - 1)]
    result = _score_vs_return(rows, "apex_score")
    assert result["insufficient_data"] is True
    assert result["n"] == MIN_SAMPLE - 1


def test_score_vs_return_quintile_spread_matches_known_correlation():
    # Perfectly monotonic: higher score -> higher return.
    rows = [{"apex_score": i, "return_pct": i * 0.01} for i in range(MIN_SAMPLE)]
    result = _score_vs_return(rows, "apex_score")
    assert result["insufficient_data"] is False
    assert result["correlation"] == 1.0
    assert result["spread_pct"] > 0


def test_score_vs_return_ignores_rows_missing_either_field():
    rows = (
        [{"apex_score": i, "return_pct": i * 0.01} for i in range(MIN_SAMPLE)]
        + [{"apex_score": None, "return_pct": 0.5}] * 5
        + [{"apex_score": 3, "return_pct": None}] * 5
    )
    result = _score_vs_return(rows, "apex_score")
    assert result["n"] == MIN_SAMPLE


def test_compute_calibration_report_covers_all_score_columns(monkeypatch):
    import portfolio_agent.tools.scoring_snapshot_db as snap_db

    monkeypatch.setattr(snap_db, "get_snapshots_for_calibration", lambda horizon_days: [])
    report = compute_calibration_report(30)
    assert set(report["scores"].keys()) == set(SCORE_COLUMNS)
    assert all(s["insufficient_data"] for s in report["scores"].values())
