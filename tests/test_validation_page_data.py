"""
web/data/validation.py — repro tests for the Validation-page review findings:

  #3 predictions queries must be scoped to LEGACY_REVIEW_SCOPES (shared +
     legacy, never private), so a private chat forecast — yours or, now that
     logins are per-owner, someone else's — never gets folded into the
     shared track record.
  #4 a same-ticker/same-horizon rating streak (e.g. BUY for 20 straight days)
     must be counted once as a streak, alongside the raw per-row count.
  #5 SELL/STRONG_SELL calls get a "did they avoid losses" stat, not just one
     bar in the returns-by-recommendation chart.
  #6 _calibration_headline() picks a real, quotable reliability bucket for
     the Validation page's one-line "when APEX says X% confident..." claim.
  #7 portfolio/watchlist ticker loaders are scoped to the CALLER's own
     holdings/watchlist, not the union across every owner.
"""
from __future__ import annotations

import pandas as pd
import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.services import account_service
from portfolio_agent.services.context import RequestContext, _mint_identity_for_tests, local_context
from portfolio_agent.tools.prediction_db import SCOPE_LEGACY, SCOPE_PRIVATE, SCOPE_SHARED
from web.data.validation import (
    _add_streak_ids,
    _calibration_headline,
    _get_model_names,
    _load_evaluated_predictions_df,
    _load_metric_series,
    _load_portfolio_tickers,
    _load_watchlist_tickers,
    _outcome_breakdown,
    _returns_by_recommendation,
    _sell_call_outcomes,
)


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    import web.data.validation as v
    monkeypatch.setattr(v, "_DB", db_path)


def _insert_prediction(scope: str, *, ticker: str = "AAPL", model_name: str = "claude", **overrides) -> None:
    from portfolio_agent.tools.prediction_db import _db as prediction_db_conn
    row = {
        "ticker": ticker, "prediction": "BULLISH", "horizon_days": 5, "as_of_date": "2026-01-01",
        "evaluation_status": "evaluated", "brier_score": 0.2, "log_loss": 0.5,
        "model_name": model_name, "scope": scope, "outcome": "strong_correct",
        "actual_return": 0.02, "excess_return": 0.01, "recommendation": "BUY",
        "conviction_score": 7, "composite_score": 7.0,
    }
    row.update(overrides)
    with prediction_db_conn() as conn:
        cols = list(row)
        conn.execute(
            f"INSERT INTO predictions ({', '.join(cols)}, created_at) VALUES "
            f"({', '.join('?' for _ in cols)}, datetime('now'))",
            list(row.values()),
        )
        conn.commit()


# ── #3: scope filtering ───────────────────────────────────────────────────────

def test_load_metric_series_excludes_private_predictions():
    _insert_prediction(SCOPE_SHARED, ticker="AAPL")
    _insert_prediction(SCOPE_PRIVATE, ticker="TSLA")   # someone's private chat forecast

    df = _load_metric_series()
    assert set(df["ticker"]) == {"AAPL"}


def test_load_metric_series_includes_legacy_but_not_private():
    _insert_prediction(SCOPE_SHARED, ticker="AAPL")
    _insert_prediction(SCOPE_LEGACY, ticker="MSFT")
    _insert_prediction(SCOPE_PRIVATE, ticker="TSLA")

    df = _load_metric_series()
    assert set(df["ticker"]) == {"AAPL", "MSFT"}


def test_compute_filtered_metrics_excludes_private_predictions():
    from web.data.validation import _compute_filtered_metrics
    _insert_prediction(SCOPE_SHARED, ticker="AAPL")
    _insert_prediction(SCOPE_PRIVATE, ticker="TSLA")

    metrics = _compute_filtered_metrics(None, 365, "All", [], [])
    assert len(metrics) == 1
    assert metrics[0]["num_predictions"] == 1


def test_load_evaluated_predictions_df_excludes_private_predictions():
    _insert_prediction(SCOPE_SHARED, ticker="AAPL")
    _insert_prediction(SCOPE_PRIVATE, ticker="TSLA")

    df = _load_evaluated_predictions_df(segment="All")
    assert set(df["ticker"]) == {"AAPL"}


def test_get_model_names_excludes_private_predictions():
    _insert_prediction(SCOPE_SHARED, model_name="claude")
    _insert_prediction(SCOPE_PRIVATE, model_name="a-private-chat-model")

    assert _get_model_names() == ["claude"]


# ── #4: streak dedup ──────────────────────────────────────────────────────────

def test_add_streak_ids_collapses_a_long_same_call_run_to_one_streak():
    rows = [
        {"ticker": "AAPL", "horizon_days": 5, "event_date": pd.Timestamp("2026-01-01") + pd.Timedelta(days=i),
         "recommendation": "BUY", "outcome": "strong_correct", "actual_return": 0.02, "excess_return": 0.01}
        for i in range(20)
    ]
    df = pd.DataFrame(rows)
    streaked = _add_streak_ids(df)
    assert streaked["streak_id"].nunique() == 1


def test_add_streak_ids_splits_on_a_rating_change():
    rows = [
        {"ticker": "AAPL", "horizon_days": 5, "event_date": pd.Timestamp("2026-01-01"),
         "recommendation": "BUY", "outcome": "strong_correct", "actual_return": 0.02, "excess_return": 0.01},
        {"ticker": "AAPL", "horizon_days": 5, "event_date": pd.Timestamp("2026-01-02"),
         "recommendation": "SELL", "outcome": "wrong_minor", "actual_return": -0.01, "excess_return": -0.01},
    ]
    df = pd.DataFrame(rows)
    streaked = _add_streak_ids(df)
    assert streaked["streak_id"].nunique() == 2


def test_outcome_breakdown_reports_raw_and_streak_counts_separately():
    rows = [
        {"ticker": "AAPL", "horizon_days": 5, "event_date": pd.Timestamp("2026-01-01") + pd.Timedelta(days=i),
         "recommendation": "BUY", "outcome": "strong_correct", "actual_return": 0.02, "excess_return": 0.01}
        for i in range(20)
    ]
    df = pd.DataFrame(rows)
    breakdown = {b["outcome"]: b for b in _outcome_breakdown(df)}
    assert breakdown["strong_correct"]["count"] == 20
    assert breakdown["strong_correct"]["streak_count"] == 1


def test_returns_by_recommendation_reports_raw_and_streak_counts_separately():
    rows = [
        {"ticker": "AAPL", "horizon_days": 5, "event_date": pd.Timestamp("2026-01-01") + pd.Timedelta(days=i),
         "recommendation": "BUY", "outcome": "strong_correct", "actual_return": 0.02, "excess_return": 0.01}
        for i in range(20)
    ]
    df = pd.DataFrame(rows)
    buy_row = next(r for r in _returns_by_recommendation(df) if r["recommendation"] == "BUY")
    assert buy_row["n"] == 20
    assert buy_row["streak_n"] == 1


# ── #5: SELL call outcomes ────────────────────────────────────────────────────

def test_sell_call_outcomes_reports_pct_fell_and_avg_excess_return():
    rows = [
        {"ticker": "TSLA", "recommendation": "SELL", "actual_return": -0.03, "excess_return": -0.02},
        {"ticker": "NVDA", "recommendation": "STRONG_SELL", "actual_return": 0.05, "excess_return": 0.06},
    ]
    df = pd.DataFrame(rows)
    stats = _sell_call_outcomes(df)
    assert stats["n"] == 2
    assert stats["pct_fell"] == pytest.approx(0.5)
    assert stats["avg_excess_return"] == pytest.approx(0.02)


def test_sell_call_outcomes_empty_when_no_sell_calls():
    df = pd.DataFrame([{"ticker": "AAPL", "recommendation": "BUY", "actual_return": 0.02, "excess_return": 0.01}])
    stats = _sell_call_outcomes(df)
    assert stats == {"n": 0, "pct_fell": None, "avg_excess_return": None}


# ── #6: calibration headline ──────────────────────────────────────────────────

def test_calibration_headline_prefers_the_bucket_closest_to_70pct_with_enough_samples():
    reliability = [
        {"center": 0.65, "mean_stated": 0.66, "actual_hit_rate": 0.70, "n": 40},
        {"center": 0.95, "mean_stated": 0.97, "actual_hit_rate": 0.99, "n": 5},   # closer numerically but thin
    ]
    headline = _calibration_headline(reliability)
    assert headline["n"] == 40


def test_calibration_headline_none_when_no_reliability_data():
    assert _calibration_headline([]) is None


# ── #7: owner-scoped ticker loaders ───────────────────────────────────────────

def _seed_second_owner(owner: str) -> None:
    """Directly insert a portfolios row for a second, test-only owner — there is
    no production path to create one yet (see test_trusted_context_scoping.py's
    identical helper); purely to exercise scoping against a real second row."""
    from portfolio_agent.tools import holdings_db as repo
    with repo.unit_of_work() as conn:
        conn.execute("INSERT INTO portfolios (owner) VALUES (?)", (owner,))


def test_load_portfolio_tickers_scopes_to_the_caller_not_every_owner():
    from tests.helpers_holdings import seed_holding
    seed_holding({"ticker": "AAPL", "shares": 10, "avg_cost": 100.0, "cost_basis_total": 1000.0,
                 "account_name": "Individual", "account_number": "X1", "account_type": "TAXABLE"},
                "fidelity", "2026-09-01")

    _seed_second_owner("bob")
    bob = RequestContext(identity=_mint_identity_for_tests("bob"), source="test")
    bob_acct = account_service.create_account(bob, broker="manual", display_name="Bob's account")
    account_service.add_position(bob, bob_acct["account_id"],
                                 {"ticker": "TSLA", "shares": 5, "avg_cost": 200.0, "cost_basis_total": 1000.0})

    assert _load_portfolio_tickers(local_context("test")) == ["AAPL"]
    assert _load_portfolio_tickers(bob) == ["TSLA"]


def test_load_watchlist_tickers_scopes_to_the_caller_not_every_owner():
    from portfolio_agent.tools.watchlist_db import add_tickers as add_watchlist_tickers
    add_watchlist_tickers(["NVDA"])   # unscoped write path -- still stamps LOCAL_OWNER

    bob = RequestContext(identity=_mint_identity_for_tests("bob"), source="test")

    assert _load_watchlist_tickers(local_context("test")) == ["NVDA"]
    assert _load_watchlist_tickers(bob) == []
