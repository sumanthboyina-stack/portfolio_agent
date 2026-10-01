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
    _signal_equity_curve,
    equity_curve_date_span,
    _add_index_benchmarks,
    _add_portfolio_benchmark,
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


# ── Equity curve: Dow/Nasdaq + portfolio comparison lines ────────────────────

def _equity_curve_fixture() -> pd.DataFrame:
    rows = [
        {"ticker": "AAPL", "horizon_days": 5, "event_date": pd.Timestamp("2026-01-05"),
         "recommendation": "BUY", "outcome": "strong_correct", "actual_return": 0.03, "excess_return": 0.01,
         "as_of_date": "2026-01-01", "evaluation_date": "2026-01-08"},
        {"ticker": "MSFT", "horizon_days": 5, "event_date": pd.Timestamp("2026-01-12"),
         "recommendation": "BUY", "outcome": "wrong_minor", "actual_return": -0.01, "excess_return": -0.02,
         "as_of_date": "2026-01-08", "evaluation_date": "2026-01-15"},
    ]
    return _signal_equity_curve(pd.DataFrame(rows))


def test_signal_equity_curve_carries_call_windows_for_index_lookups():
    curve = _equity_curve_fixture()
    assert list(curve["as_of_date"]) == ["2026-01-01", "2026-01-08"]
    assert list(curve["evaluation_date"]) == ["2026-01-08", "2026-01-15"]


def test_equity_curve_date_span_covers_every_calls_window():
    assert equity_curve_date_span(_equity_curve_fixture()) == ("2026-01-01", "2026-01-15")


def test_equity_curve_date_span_none_when_curve_is_empty():
    assert equity_curve_date_span(pd.DataFrame()) is None


def test_add_index_benchmarks_computes_cumulative_return_over_each_calls_own_window():
    curve = _equity_curve_fixture()
    closes = {
        "^DJI":  {"2026-01-01": 100.0, "2026-01-08": 102.0, "2026-01-15": 101.0},
        "^IXIC": {"2026-01-01": 200.0, "2026-01-08": 210.0, "2026-01-15": 205.0},
    }
    out = _add_index_benchmarks(curve, closes)

    # Call 1: DJI 100->102 = +2%; call 2: DJI 102->101 compounds onto that.
    assert out["dow_cum"].iloc[0] == pytest.approx(1.02)
    assert out["dow_cum"].iloc[1] == pytest.approx(1.02 * (101 / 102))
    assert out["nasdaq_cum"].iloc[0] == pytest.approx(1.05)


def test_add_index_benchmarks_handles_a_missing_symbol_gracefully():
    curve = _equity_curve_fixture()
    out = _add_index_benchmarks(curve, {"^DJI": {}})
    assert out["dow_cum"].isna().all()
    assert out["nasdaq_cum"].isna().all()


def test_add_portfolio_benchmark_rebases_to_the_curves_start_date(monkeypatch):
    import portfolio_agent.tools.holdings_db as hdb

    def fake_get_price_history(*a, **kw):
        return [
            {"date": "2026-01-01", "broker": "fidelity", "account_number": "X1", "ticker": "AAPL",
             "market_value": 1000.0, "cost_basis": 900.0, "source": "snapshot"},
            {"date": "2026-01-08", "broker": "fidelity", "account_number": "X1", "ticker": "AAPL",
             "market_value": 1050.0, "cost_basis": 900.0, "source": "snapshot"},
        ]
    monkeypatch.setattr(hdb, "get_price_history", fake_get_price_history)

    out = _add_portfolio_benchmark(_equity_curve_fixture())
    assert out["portfolio_cum"].iloc[0] == pytest.approx(1.0)     # baseline at the curve's own start
    assert out["portfolio_cum"].iloc[1] == pytest.approx(1.05)    # 1050 / 1000


def test_add_portfolio_benchmark_empty_when_no_recorded_history(monkeypatch):
    import portfolio_agent.tools.holdings_db as hdb
    monkeypatch.setattr(hdb, "get_price_history", lambda *a, **kw: [])

    out = _add_portfolio_benchmark(_equity_curve_fixture())
    assert out["portfolio_cum"].isna().all()


# ── Validation yardstick: baselines / skill / effective N wiring ──────────────

_P = {"p_strong_down": 5, "p_moderate_down": 10, "p_flat": 25, "p_moderate_up": 40, "p_strong_up": 20}


def _eval_df(n=6, horizon=5, ticker="AAPL"):
    rows = []
    for i in range(n):
        up = i % 2 == 0
        rows.append({
            "id": i + 1, "ticker": ticker, "horizon_days": horizon, "recommendation": "BUY" if up else "SELL",
            "as_of_date": f"2026-02-{i * 8 + 1:02d}" if i < 3 else f"2026-03-{(i - 3) * 8 + 1:02d}",
            "evaluation_date": f"2026-02-{i * 8 + 7:02d}" if i < 3 else f"2026-03-{(i - 3) * 8 + 7:02d}",
            "predicted_direction": "UP" if up else "DOWN", "actual_return": 0.03 if up else -0.03,
            "benchmark_return": 0.01, "excess_return": 0.02 if up else -0.04, **_P,
        })
    df = pd.DataFrame(rows)
    df["event_date"] = pd.to_datetime(df["evaluation_date"])
    return df


def test_load_evaluated_predictions_df_carries_columns_the_metrics_need():
    _insert_prediction(SCOPE_SHARED, ticker="AAPL", predicted_direction="UP", benchmark_return=0.01,
                       p_strong_down=5, p_moderate_down=10, p_flat=25, p_moderate_up=40, p_strong_up=20)
    df = _load_evaluated_predictions_df(segment="All")
    for col in ("id", "predicted_direction", "benchmark_return", "p_flat", "p_strong_up",
                "predicted_return_low", "predicted_return_high"):
        assert col in df.columns
    assert df.iloc[0]["predicted_direction"] == "UP"


def test_build_horizon_reports_is_per_horizon_with_streak_n_and_mocked_prices():
    from web.data.validation import build_horizon_reports
    df = pd.concat([_eval_df(6, 5), _eval_df(4, 21)], ignore_index=True)
    calls = []

    def fake_fetch(sym, start, end):
        calls.append(sym)
        return {}                                   # no price data at all

    reports = build_horizon_reports(df, fetch=fake_fetch)
    assert set(reports) == {5, 21}
    assert reports[5]["comparison"]["n_total"] == 6 and reports[21]["comparison"]["n_total"] == 4
    assert reports[5]["effective_sample"]["streak_n"] == 6          # BUY/SELL alternates -> every row its own streak
    assert set(reports[5]["comparison"]["unavailable"]) == {"spy_prior_window", "momentum"}
    assert {"AAPL", "SPY"} <= set(calls)


def test_build_horizon_reports_degrades_when_fetch_raises_or_prices_off_or_empty():
    from web.data.validation import build_horizon_reports
    df = _eval_df(6, 5)

    def boom(*a):
        raise RuntimeError("yfinance down")

    assert build_horizon_reports(df, fetch=boom)[5]["comparison"]["n_total"] == 6
    assert build_horizon_reports(df, use_prices=False)[5]["comparison"]["unavailable"]
    assert build_horizon_reports(pd.DataFrame()) == {}


def test_load_price_history_caches_successes_but_not_failures():
    import web.data.validation as v
    v._price_cache.clear()
    n_calls = {"ok": 0, "bad": 0}

    def fetch(sym, start, end):
        key = "ok" if sym == "AAPL" else "bad"
        n_calls[key] += 1
        return {"2026-01-02": 1.0} if sym == "AAPL" else {}

    req = {"AAPL": ("2026-01-01", "2026-02-01"), "ZZZ": ("2026-01-01", "2026-02-01")}
    v.load_price_history(req, fetch=fetch, now=1000.0)
    out = v.load_price_history(req, fetch=fetch, now=1100.0)
    assert out["AAPL"] and out["ZZZ"] == {}
    assert n_calls == {"ok": 1, "bad": 2}              # AAPL cached; ZZZ retried
    v.load_price_history(req, fetch=fetch, now=1000.0 + v._PRICE_TTL_SECONDS + 1)
    assert n_calls["ok"] == 2                          # TTL expiry refetches
    v._price_cache.clear()


def test_outcome_breakdown_and_chart_show_nonzero_flat_correct():
    from web.components.validation_charts import build_outcome_breakdown_chart
    df = _eval_df(4)
    df["outcome"] = ["flat_correct", "flat_correct", "strong_correct", "wrong_minor"]
    breakdown = {b["outcome"]: b for b in _outcome_breakdown(df)}
    assert breakdown["flat_correct"]["count"] == 2
    fig = build_outcome_breakdown_chart(list(breakdown.values()))
    assert fig.data[0].x[2] == "Flat Correct" and fig.data[0].y[2] == 2
    assert len(set(fig.data[0].marker.color)) == 5       # every outcome keeps its own colour


# ── tile tiers are baseline-relative; no fixed 50% "random" ───────────────────

def _report(best_acc=0.50, apex_acc=0.60, skill=0.12):
    return {
        "comparison": {"n_total": 40, "n_common": 40, "best_baseline": {"key": "always_up", "name": "Always UP", "accuracy": best_acc},
                       "rows": [{"key": "apex", "name": "APEX", "accuracy": apex_acc, "n": 40},
                                {"key": "always_up", "name": "Always UP", "accuracy": best_acc, "n": 40}]},
        "probability": {"n": 40, "binary": {"brier": 0.22, "baseline_brier": 0.25, "brier_skill": skill, "log_loss": 0.6,
                                            "baseline_log_loss": 0.69, "log_loss_skill": 0.13, "base_rate": 0.5},
                        "five": {"brier": 0.7, "baseline_brier": 0.75, "brier_skill": 0.07, "log_loss": 1.4,
                                 "baseline_log_loss": 1.5, "log_loss_skill": 0.07}},
    }


def test_tile_state_directional_accuracy_is_relative_to_best_baseline():
    from web.components.validation_cards import _tile_state
    from web.components.validation_charts import _METRIC_CFG
    cfg = _METRIC_CFG["directional_accuracy"]
    above = _tile_state("directional_accuracy", cfg, {"directional_accuracy": 0.60}, _report(0.50, 0.60))
    near = _tile_state("directional_accuracy", cfg, {"directional_accuracy": 0.51}, _report(0.50, 0.51))
    below = _tile_state("directional_accuracy", cfg, {"directional_accuracy": 0.40}, _report(0.50, 0.40))
    assert above["tier"].startswith("Above") and near["tier"].startswith("Near") and below["tier"].startswith("Below")
    assert "random" not in above["tier"].lower() and "Always UP" in " ".join(above["extra"])
    assert _tile_state("directional_accuracy", cfg, {"directional_accuracy": 0.6}, None)["tier"] == "No baseline"


def test_tile_state_brier_tiles_show_baseline_and_skill():
    from web.components.validation_cards import _tile_state
    from web.components.validation_charts import _METRIC_CFG
    st5 = _tile_state("brier_5bucket", _METRIC_CFG["brier_5bucket"], {}, _report())
    assert st5["val"] == pytest.approx(0.7) and "0.750" in st5["sub"] and st5["tier"].startswith("Some skill")
    stb = _tile_state("brier_score", _METRIC_CFG["brier_score"], {"brier_score": 0.9}, _report(skill=0.12))
    assert stb["val"] == pytest.approx(0.22) and stb["tier"].startswith("Clear skill")      # same rows as the baseline
    fallback = _tile_state("brier_score", _METRIC_CFG["brier_score"], {"brier_score": 0.2}, None)
    assert fallback["val"] == 0.2 and fallback["tier"] == "No baseline"
    assert _tile_state("brier_5bucket", _METRIC_CFG["brier_5bucket"], {}, None)["val"] is None


def test_metric_cfg_has_no_fixed_random_wording_for_direction_and_labels_binary_vs_5bucket():
    from web.components.validation_charts import _METRIC_CFG
    for key in ("directional_accuracy", "high_conviction_accuracy", "low_conviction_accuracy"):
        text = repr(_METRIC_CFG[key]["tiers"]) + _METRIC_CFG[key]["ref_label"] + _METRIC_CFG[key]["defn"]
        assert "random" not in text.lower() and "50%" not in text
    assert "UP vs not-UP" in _METRIC_CFG["brier_score"]["label"] and "UP vs not-UP" in _METRIC_CFG["mean_log_loss"]["label"]
    assert "0.25" not in _METRIC_CFG["brier_score"]["defn"].replace("0.25 only", "")
    assert "5-bucket" in _METRIC_CFG["brier_5bucket"]["label"] and "5-bucket" in _METRIC_CFG["logloss_5bucket"]["label"]


def test_horizon_reliability_chart_uses_baselines_and_has_no_random_line():
    from web.components.validation_charts import build_horizon_reliability_chart
    by_h = {5: {"directional_accuracy": 0.6, "num_predictions": 50}}
    fig = build_horizon_reliability_chart(by_h, [5], {5: "5-Day"}, {5: 0.5})
    assert not any("random" in (getattr(a, "text", "") or "").lower() for a in fig.layout.annotations)
    assert len(fig.data) == 2
    assert build_horizon_reliability_chart(by_h, [5], {5: "5-Day"}).data[0].marker.color[0] == "#94A3B8"
