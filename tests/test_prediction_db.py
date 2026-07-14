from __future__ import annotations

import portfolio_agent.tools.db as db_module
from portfolio_agent.tools.prediction_db import (
    _db,
    get_matured_pending_predictions,
    insert_prediction,
    update_prediction_outcome,
)


def test_get_matured_pending_predictions_catches_up_on_missed_days(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")

    insert_prediction(
        ticker="OVERDUE", prediction="NEUTRAL", recommendation="HOLD",
        horizon_days=5, evaluation_date="2026-06-30",
    )
    insert_prediction(
        ticker="DUETODAY", prediction="NEUTRAL", recommendation="HOLD",
        horizon_days=5, evaluation_date="2026-07-13",
    )
    insert_prediction(
        ticker="NOTYETDUE", prediction="NEUTRAL", recommendation="HOLD",
        horizon_days=5, evaluation_date="2026-07-20",
    )

    pending = get_matured_pending_predictions("2026-07-13")
    tickers = {p["ticker"] for p in pending}

    # A prediction whose evaluation_date fell on a day the pipeline never ran
    # (2026-06-30) must still be caught by a later run, not just the exact day.
    assert "OVERDUE" in tickers
    assert "DUETODAY" in tickers
    assert "NOTYETDUE" not in tickers


def test_get_matured_pending_predictions_excludes_already_evaluated(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")

    saved = insert_prediction(
        ticker="DONE", prediction="NEUTRAL", recommendation="HOLD",
        horizon_days=5, evaluation_date="2026-06-30",
    )
    with _db() as c:
        row = c.execute("SELECT id FROM predictions WHERE ticker='DONE'").fetchone()
    update_prediction_outcome(row["id"], 0.01, "UP", None, None, "strong_correct", 1.0, 0.0, True)

    pending = get_matured_pending_predictions("2026-07-13")
    assert "DONE" not in {p["ticker"] for p in pending}


def test_get_matured_pending_predictions_ignores_rows_with_no_evaluation_date(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")

    insert_prediction(
        ticker="NODATE", prediction="NEUTRAL", recommendation="HOLD",
        horizon_days=5, evaluation_date="",
    )

    pending = get_matured_pending_predictions("2026-07-13")
    assert "NODATE" not in {p["ticker"] for p in pending}
