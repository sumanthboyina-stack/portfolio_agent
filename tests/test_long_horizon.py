"""Long-horizon validation (Step 1.2): beat-the-benchmark accuracy with effective N, provisional tracking kept apart from
final metrics, and trading-day evaluation dates for NEW predictions only."""
from __future__ import annotations

import sqlite3
from datetime import date

import pandas as pd
import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.tools import prediction_db as pdb
from portfolio_agent.tools import validation_engine as ve
from portfolio_agent.tools import validation_metrics as vm
from portfolio_agent.tools.prediction_db import (
    _evaluation_date, get_provisional_marks, trading_day_evaluation_date, upsert_provisional_mark,
)


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")


def _final(i, pred, excess, h=250, ticker=None):
    # one distinct ticker per row so no two windows overlap
    return {"ticker": ticker or f"T{i}", "horizon_days": h, "as_of_date": "2025-01-02", "evaluation_date": "2026-01-02",
            "predicted_direction": pred, "actual_return": excess + 0.05, "benchmark_return": 0.05, "excess_return": excess}


# ── beat-benchmark accuracy, effective N, too early ───────────────────────────

def test_beat_benchmark_accuracy_hand_computed():
    rows = [_final(0, "UP", 0.10), _final(1, "UP", -0.02), _final(2, "DOWN", -0.04), _final(3, "DOWN", 0.03),
            _final(4, "FLAT", 0.50)]                                     # FLAT makes no claim
    r = vm.beat_benchmark_accuracy(pd.DataFrame(rows))
    assert r["final"] is True
    assert (r["raw_n"], r["effective_n"]) == (4, 4)
    assert r["raw_accuracy"] == pytest.approx(0.5) and r["accuracy"] == pytest.approx(0.5)
    assert r["always_up_rate"] == pytest.approx(3 / 5)                   # excess > 0 on 3 of the 5 rows
    assert r["too_early"] is True                                        # 4 < 30


def test_effective_n_counts_non_overlapping_calls_and_gates_too_early():
    # 40 calls on ONE ticker made daily at 250d: they all overlap -> effective n is 1, however many raw rows there are
    same = [dict(_final(0, "UP", 0.1, ticker="AAA"), as_of_date=f"2025-01-{d:02d}") for d in range(1, 29)]
    r = vm.beat_benchmark_accuracy(pd.DataFrame(same))
    assert r["raw_n"] == 28 and r["effective_n"] == 1 and r["too_early"] is True
    # 30 independent tickers reach the threshold
    many = [_final(i, "UP", 0.1) for i in range(30)]
    r = vm.beat_benchmark_accuracy(pd.DataFrame(many))
    assert r["effective_n"] == 30 and r["too_early"] is False and r["accuracy"] == 1.0
    assert r["ci_low"] is not None and r["ci_high"] == pytest.approx(1.0)


def test_beat_benchmark_with_no_rows_is_empty_and_too_early():
    r = vm.beat_benchmark_accuracy(pd.DataFrame())
    assert r["raw_n"] == 0 and r["accuracy"] is None and r["too_early"] is True


def test_horizon_report_carries_beat_benchmark():
    rep = vm.horizon_report(pd.DataFrame([_final(i, "UP", 0.1, h=63) for i in range(3)]))
    assert rep["beat_benchmark"]["raw_n"] == 3


# ── provisional is separate from final ────────────────────────────────────────

def _marks(n, excess=0.02, h=250):
    return pd.DataFrame([{"id": i, "ticker": f"P{i}", "as_of_date": "2026-06-01", "evaluation_date": "2027-05-30",
                          "horizon_days": h, "predicted_direction": "UP", "bucket_edges_version": 2, "trading_days_elapsed": 50,
                          "excess_so_far": excess} for i in range(n)])


def test_provisional_report_is_labelled_provisional_and_uses_its_own_keys():
    p = vm.provisional_beat_benchmark(_marks(5))
    assert p["provisional"] is True and "final" not in p and "accuracy" not in p
    assert p["n"] == 5 and p["accuracy_so_far"] == 1.0 and p["mean_fraction_elapsed"] == pytest.approx(50 / 250)
    assert p["too_early"] is True
    assert vm.provisional_beat_benchmark(pd.DataFrame())["n"] == 0


def _seed_row(ticker, h, status="pending", as_of="2026-06-01", edate="2027-05-30"):
    pdb.insert_prediction(ticker=ticker, prediction="BULLISH", recommendation="BUY", horizon_days=h,
                          predicted_direction="UP", evaluation_date=edate)
    with sqlite3.connect(db_module.DB_PATH) as c:
        rid = c.execute("SELECT MAX(id) FROM predictions").fetchone()[0]
        c.execute("UPDATE predictions SET as_of_date = ?, scope = 'shared_market', evaluation_status = ? WHERE id = ?",
                  [as_of, status, rid])
    return rid


def test_marks_live_in_their_own_table_and_never_touch_final_columns():
    rid = _seed_row("AAA", 250)
    before = _row(rid)
    upsert_provisional_mark(rid, "2026-09-01", 65, 0.07, 0.03, 0.04)
    assert _row(rid) == before                                           # nothing in `predictions` moved
    m = get_provisional_marks(250)
    assert len(m) == 1 and m[0]["excess_so_far"] == pytest.approx(0.04)
    upsert_provisional_mark(rid, "2026-09-02", 66, 0.08, 0.03, 0.05)     # replaces, not appends
    assert [x["excess_so_far"] for x in get_provisional_marks(250)] == [pytest.approx(0.05)]


def _row(rid):
    with sqlite3.connect(db_module.DB_PATH) as c:
        c.row_factory = sqlite3.Row
        return dict(c.execute("SELECT * FROM predictions WHERE id = ?", [rid]).fetchone())


def test_a_matured_row_no_longer_shows_a_provisional_mark():
    rid = _seed_row("BBB", 63)
    upsert_provisional_mark(rid, "2026-09-01", 30, 0.01, 0.0, 0.01)
    assert len(get_provisional_marks(63)) == 1
    with sqlite3.connect(db_module.DB_PATH) as c:
        c.execute("UPDATE predictions SET evaluation_status = 'evaluated' WHERE id = ?", [rid])
    assert get_provisional_marks(63) == []                               # its final result is the only one that counts


def test_provisional_marks_do_not_change_the_final_report():
    final = pd.DataFrame([_final(i, "UP", 0.1) for i in range(5)])
    before = vm.horizon_report(final)["beat_benchmark"]
    rid = _seed_row("CCC", 250)
    upsert_provisional_mark(rid, "2026-09-01", 65, 0.5, 0.0, 0.5)
    assert vm.horizon_report(final)["beat_benchmark"] == before


def test_update_provisional_marks_marks_pending_long_rows_only_with_no_live_prices(monkeypatch):
    long_id = _seed_row("DDD", 250)
    _seed_row("EEE", 5, edate="2026-06-08")
    prices = {("DDD", "2026-06-01"): 100.0, ("DDD", "2026-09-01"): 110.0, ("SPY", "2026-06-01"): 500.0, ("SPY", "2026-09-01"): 510.0}
    monkeypatch.setattr(ve, "get_close", lambda t, d: prices.get((t, d)))
    out = ve.update_provisional_marks(date(2026, 9, 1))
    assert out == {"marked": 1, "skipped": 0}
    (m,) = get_provisional_marks(250)
    assert m["id"] == long_id and m["return_so_far"] == pytest.approx(0.10)
    assert m["excess_so_far"] == pytest.approx(0.10 - 0.02) and m["trading_days_elapsed"] > 50
    assert get_provisional_marks(5) == []


# ── trading-day evaluation dates, new predictions only ────────────────────────

def test_trading_day_evaluation_date_skips_weekends_and_holidays():
    assert trading_day_evaluation_date(date(2026, 9, 21), 5) == "2026-09-28"          # plain week
    # Thanksgiving (Thu 2026-11-26) and the weekend are not counted: 3 trading days from Tue 11-24 are Wed, Fri, Mon
    assert trading_day_evaluation_date(date(2026, 11, 24), 3) == "2026-11-30"
    # the weekday-offset rule would count the holiday
    assert _evaluation_date(date(2026, 11, 24), 3) == "2026-11-27"
    # 250 trading days is strictly later than 250 weekdays once holidays intervene
    assert trading_day_evaluation_date(date(2026, 9, 21), 250) > _evaluation_date(date(2026, 9, 21), 250)


def test_new_predictions_use_the_trading_day_rule_and_old_rows_keep_their_date():
    pdb.insert_prediction(ticker="OLD", prediction="BULLISH", recommendation="BUY", horizon_days=63, evaluation_date="2026-12-17")
    pdb.insert_prediction(ticker="NEW", prediction="BULLISH", recommendation="BUY", horizon_days=63)
    with sqlite3.connect(db_module.DB_PATH) as c:
        old, new = (c.execute("SELECT evaluation_date, as_of_date FROM predictions WHERE ticker = ?", [t]).fetchone()
                    for t in ("OLD", "NEW"))
    assert old[0] == "2026-12-17"                                       # an explicit / stored date is never recomputed
    assert new[0] == trading_day_evaluation_date(date.fromisoformat(new[1]), 63)
    # and opening the DB again does not rewrite anything
    pdb.get_scheduled_horizons(date(2026, 9, 21))
    with sqlite3.connect(db_module.DB_PATH) as c:
        assert c.execute("SELECT evaluation_date FROM predictions WHERE ticker = 'OLD'").fetchone()[0] == "2026-12-17"
