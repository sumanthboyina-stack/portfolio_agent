from __future__ import annotations

from datetime import date, timedelta

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.tools.scoring_snapshot_db import (
    HORIZONS,
    fill_matured_outcomes,
    get_snapshots_for_calibration,
    get_snapshots_for_date,
    get_snapshots_needing_outcome,
    upsert_snapshot,
)


def test_upsert_and_get_snapshots_for_date(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")

    today = date.today().isoformat()
    upsert_snapshot("NVDA", today, 100.0, 8.0, 5.0, 70.0, 60.0, 65.0)
    upsert_snapshot("JPM", today, 200.0, 7.0, 6.0, 55.0, 90.0, 62.0)

    rows = get_snapshots_for_date(today)
    tickers = {r["ticker"] for r in rows}
    assert tickers == {"NVDA", "JPM"}
    # Ordered by final_score DESC
    assert rows[0]["ticker"] == "NVDA"


def test_upsert_is_idempotent_per_ticker_and_date(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")

    today = date.today().isoformat()
    upsert_snapshot("NVDA", today, 100.0, 8.0, 5.0, 70.0, 60.0, 65.0)
    upsert_snapshot("NVDA", today, 105.0, 8.5, 5.0, 72.0, 61.0, 67.0)  # same day, revised

    rows = get_snapshots_for_date(today)
    assert len(rows) == 1
    assert rows[0]["apex_score"] == 8.5


def test_get_snapshots_needing_outcome_respects_age_window(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")

    too_recent = (date.today() - timedelta(days=10)).isoformat()
    matured = (date.today() - timedelta(days=35)).isoformat()
    too_old = (date.today() - timedelta(days=200)).isoformat()  # past the 30d+90d grace window

    upsert_snapshot("TOORECENT", too_recent, 100.0, 5.0, 5.0, 50.0, 50.0, 50.0)
    upsert_snapshot("MATURED", matured, 100.0, 5.0, 5.0, 50.0, 50.0, 50.0)
    upsert_snapshot("TOOOLD", too_old, 100.0, 5.0, 5.0, 50.0, 50.0, 50.0)

    due = get_snapshots_needing_outcome(30)
    tickers = {r["ticker"] for r in due}
    assert tickers == {"MATURED"}


def test_fill_matured_outcomes_writes_return_and_benchmark(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")

    matured_date = date.today() - timedelta(days=35)
    target_date = (matured_date + timedelta(days=30)).isoformat()
    matured_date_str = matured_date.isoformat()
    upsert_snapshot("NVDA", matured_date_str, 100.0, 8.0, 5.0, 70.0, 60.0, 65.0)

    prices = {
        ("NVDA", target_date): 120.0,
        ("SPY", matured_date_str): 100.0,
        ("SPY", target_date): 110.0,
    }

    def fake_get_close(ticker, date_str):
        return prices.get((ticker, date_str))

    import portfolio_agent.tools.yfinance_tools as yft
    monkeypatch.setattr(yft, "get_close", fake_get_close)

    results = fill_matured_outcomes()
    assert results[30]["evaluated"] == 1

    rows = get_snapshots_for_calibration(30)
    assert len(rows) == 1
    assert rows[0]["return_pct"] == pytest.approx(0.2)  # 120/100 - 1
    assert rows[0]["benchmark_return_pct"] == pytest.approx(0.1)  # 110/100 - 1
