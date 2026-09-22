"""
Validation dashboard data functions.

Contains:
  - _load_metric_series          : per-prediction brier/log-loss series
  - _load_portfolio_tickers      : ticker symbols from the holdings DB
  - _get_model_names             : distinct model_name values from predictions
  - _compute_filtered_metrics    : rolling metrics recomputed from predictions table
  - _load_rolling_metrics_series : historical rows from metrics_rolling table
  - _directional_correct         : outcome -> bool correctness helper
  - _load_evaluated_predictions_df : broad evaluated-prediction rows (no brier requirement)
  - _outcome_breakdown           : outcome bucket counts
  - _returns_by_recommendation   : avg realized return per recommendation bucket
  - _signal_equity_curve         : cumulative "followed the BUY calls" vs SPY curve
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

import pandas as pd

_DB = _ROOT / "data" / "portfolio.db"


def _load_metric_series(
    segment: str = "All",
    portfolio_tickers: list[str] | None = None,
    watchlist_tickers: list[str] | None = None,
) -> pd.DataFrame:
    """Per-prediction brier_score + log_loss for evaluated rows, optionally filtered by segment."""
    if not _DB.exists():
        return pd.DataFrame()

    my_tickers = sorted(set(portfolio_tickers or []) | set(watchlist_tickers or []))

    seg_clause = ""
    seg_params: list = []
    if segment == "My Tickers" and my_tickers:
        ph = ",".join("?" * len(my_tickers))
        seg_clause = f"AND ticker IN ({ph})"
        seg_params = list(my_tickers)
    elif segment == "Portfolio" and portfolio_tickers:
        ph = ",".join("?" * len(portfolio_tickers))
        seg_clause = f"AND ticker IN ({ph})"
        seg_params = list(portfolio_tickers)
    elif segment == "Watchlist" and watchlist_tickers:
        ph = ",".join("?" * len(watchlist_tickers))
        seg_clause = f"AND ticker IN ({ph})"
        seg_params = list(watchlist_tickers)
    elif segment == "New Opportunities":
        seg_clause = "AND trigger_type = 'trending_opportunity'"

    with sqlite3.connect(str(_DB)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(f"""
            SELECT as_of_date AS prediction_date, evaluated_at, ticker, horizon_days,
                   brier_score, log_loss,
                   actual_return, predicted_return_low, predicted_return_high,
                   predicted_direction, actual_direction,
                   conviction_score, composite_score,
                   outcome, excess_return, in_predicted_range,
                   p_strong_down, p_moderate_down, p_flat, p_moderate_up, p_strong_up
            FROM predictions
            WHERE evaluation_status = 'evaluated'
              AND brier_score IS NOT NULL
              {seg_clause}
            ORDER BY as_of_date ASC
        """, seg_params).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    if not df.empty:
        df["prediction_date"] = pd.to_datetime(df["prediction_date"])
        df["horizon_label"]   = df["horizon_days"].apply(
            lambda h: f"{h}d" if pd.notna(h) else "?"
        )
    return df


def _load_portfolio_tickers() -> list[str]:
    """Return unique portfolio ticker symbols from the holdings DB."""
    try:
        from portfolio_agent.tools.holdings_db import get_portfolio_tickers
        return get_portfolio_tickers()
    except Exception:
        return []


def _load_watchlist_tickers() -> list[str]:
    """Return unique watchlist ticker symbols (excludes holdings)."""
    from portfolio_agent.tools.watchlist_db import load_watchlist_tickers
    try:
        watchlist = set(load_watchlist_tickers())
        return list(watchlist - set(_load_portfolio_tickers()))
    except Exception:
        return []


def _get_model_names() -> list[str]:
    """Return distinct model_name values from predictions table, sorted by count desc."""
    if not _DB.exists():
        return []
    with sqlite3.connect(str(_DB)) as conn:
        rows = conn.execute(
            """SELECT model_name, COUNT(*) as cnt FROM predictions
               WHERE model_name IS NOT NULL
               GROUP BY model_name ORDER BY cnt DESC"""
        ).fetchall()
    return [r[0] for r in rows]


def _compute_filtered_metrics(
    model_names: list[str] | None,
    lookback: int,
    segment: str = "All",
    portfolio_tickers: list[str] | None = None,
    watchlist_tickers: list[str] | None = None,
) -> list[dict]:
    """
    Recompute rolling metrics directly from the predictions table, optionally
    filtered by model_name list and/or prediction segment (Portfolio / Watchlist /
    New Opportunities).
    """
    if not _DB.exists():
        return []
    model_clause = ""
    model_params: list = []
    if model_names:
        placeholders = ",".join("?" * len(model_names))
        model_clause = f"AND model_name IN ({placeholders})"
        model_params = list(model_names)

    my_tickers = sorted(set(portfolio_tickers or []) | set(watchlist_tickers or []))

    seg_clause = ""
    seg_params: list = []
    if segment == "My Tickers" and my_tickers:
        ph = ",".join("?" * len(my_tickers))
        seg_clause = f"AND ticker IN ({ph})"
        seg_params = list(my_tickers)
    elif segment == "Portfolio" and portfolio_tickers:
        ph = ",".join("?" * len(portfolio_tickers))
        seg_clause = f"AND ticker IN ({ph})"
        seg_params = list(portfolio_tickers)
    elif segment == "Watchlist" and watchlist_tickers:
        ph = ",".join("?" * len(watchlist_tickers))
        seg_clause = f"AND ticker IN ({ph})"
        seg_params = list(watchlist_tickers)
    elif segment == "New Opportunities":
        seg_clause = "AND trigger_type = 'trending_opportunity'"

    params: list = [lookback] + model_params + seg_params

    sql = f"""
        SELECT
            horizon_days,
            {lookback} AS lookback_days,
            COUNT(*) AS num_predictions,
            AVG(CASE WHEN outcome IN
                ('strong_correct','directionally_correct','flat_correct')
                THEN 1.0 ELSE 0.0 END) AS directional_accuracy,
            AVG(CASE WHEN in_predicted_range = 1 THEN 1.0 ELSE 0.0 END) AS in_range_pct,
            AVG(excess_return)  AS mean_excess_return,
            AVG(brier_score)    AS brier_score,
            AVG(log_loss)       AS mean_log_loss,
            CAST(SUM(CASE WHEN conviction_score >= 7
                          AND outcome IN ('strong_correct','directionally_correct','flat_correct')
                          THEN 1.0 ELSE 0.0 END) AS REAL)
              / NULLIF(SUM(CASE WHEN conviction_score >= 7 THEN 1.0 ELSE 0.0 END), 0)
                AS high_conviction_accuracy,
            CAST(SUM(CASE WHEN conviction_score < 7
                          AND outcome IN ('strong_correct','directionally_correct','flat_correct')
                          THEN 1.0 ELSE 0.0 END) AS REAL)
              / NULLIF(SUM(CASE WHEN conviction_score < 7 THEN 1.0 ELSE 0.0 END), 0)
                AS low_conviction_accuracy
        FROM predictions
        WHERE evaluation_status = 'evaluated'
          AND as_of_date >= DATE('now', '-' || ? || ' days')
          {model_clause}
          {seg_clause}
        GROUP BY horizon_days
    """
    with sqlite3.connect(str(_DB)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


_SEGMENT_TO_BUCKET = {
    "All": "all",
    "Portfolio": "portfolio",
    "Watchlist": "watchlist",
    "New Opportunities": "opportunity",
}


def _load_rolling_metrics_series(segment: str = "All") -> pd.DataFrame:
    """
    Historical rolling metrics from metrics_rolling table, filtered to the
    'all' / 'portfolio' / 'watchlist' / 'opportunity' bucket
    recompute_rolling_metrics() writes per (horizon, lookback, version).
    """
    if not _DB.exists():
        return pd.DataFrame()
    bucket = _SEGMENT_TO_BUCKET.get(segment, "all")
    with sqlite3.connect(str(_DB)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT metric_date, horizon_days, segment, lookback_days,
                   directional_accuracy, in_range_pct, mean_excess_return,
                   brier_score, mean_log_loss, num_predictions,
                   high_conviction_accuracy, low_conviction_accuracy
            FROM metrics_rolling
            WHERE segment = ?
            ORDER BY metric_date ASC
        """, [bucket]).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    if not df.empty:
        df["metric_date"] = pd.to_datetime(df["metric_date"])
    return df


def _directional_correct(outcome: str | None) -> bool | None:
    if outcome is None:
        return None
    return outcome in ("strong_correct", "directionally_correct", "flat_correct")


OUTCOME_ORDER = [
    "strong_correct", "directionally_correct", "flat_correct",
    "wrong_minor", "wrong_significant",
]
RECOMMENDATION_ORDER = ["STRONG_BUY", "BUY", "HOLD", "SELL", "STRONG_SELL"]


def _load_evaluated_predictions_df(
    lookback_days: int | None = None,
    model_names: list[str] | None = None,
    segment: str = "My Tickers",
    portfolio_tickers: list[str] | None = None,
    watchlist_tickers: list[str] | None = None,
) -> pd.DataFrame:
    """
    Every evaluated prediction (recommendation, outcome, actual_return,
    excess_return, dates) — unlike _load_metric_series, does NOT require
    brier_score, since the outcome-breakdown / returns-by-recommendation /
    equity-curve views don't depend on the probability-distribution columns
    and would otherwise silently drop older or distribution-less predictions.
    """
    if not _DB.exists():
        return pd.DataFrame()

    my_tickers = sorted(set(portfolio_tickers or []) | set(watchlist_tickers or []))

    clauses = ["evaluation_status = 'evaluated'"]
    params: list = []
    if lookback_days:
        clauses.append("as_of_date >= DATE('now', '-' || ? || ' days')")
        params.append(lookback_days)
    if model_names:
        ph = ",".join("?" * len(model_names))
        clauses.append(f"model_name IN ({ph})")
        params.extend(model_names)

    if segment == "My Tickers" and my_tickers:
        ph = ",".join("?" * len(my_tickers))
        clauses.append(f"ticker IN ({ph})")
        params.extend(my_tickers)
    elif segment == "Portfolio" and portfolio_tickers:
        ph = ",".join("?" * len(portfolio_tickers))
        clauses.append(f"ticker IN ({ph})")
        params.extend(portfolio_tickers)
    elif segment == "Watchlist" and watchlist_tickers:
        ph = ",".join("?" * len(watchlist_tickers))
        clauses.append(f"ticker IN ({ph})")
        params.extend(watchlist_tickers)
    elif segment == "New Opportunities":
        clauses.append("trigger_type = 'trending_opportunity'")

    where = "WHERE " + " AND ".join(clauses)
    with sqlite3.connect(str(_DB)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(f"""
            SELECT as_of_date, evaluation_date, evaluated_at, ticker, horizon_days,
                   recommendation, outcome, actual_return, excess_return,
                   conviction_score, composite_score, trigger_type, model_name
            FROM predictions
            {where}
            ORDER BY COALESCE(evaluation_date, as_of_date) ASC
        """, params).fetchall()

    df = pd.DataFrame([dict(r) for r in rows])
    if not df.empty:
        df["event_date"] = pd.to_datetime(df["evaluation_date"].fillna(df["as_of_date"]))
    return df


def _outcome_breakdown(df: pd.DataFrame) -> list[dict]:
    """Count of evaluated predictions per outcome bucket, in fixed strong->wrong order."""
    if df.empty or "outcome" not in df.columns:
        return []
    counts = df["outcome"].value_counts().to_dict()
    return [{"outcome": o, "count": int(counts.get(o, 0))} for o in OUTCOME_ORDER]


def _returns_by_recommendation(df: pd.DataFrame) -> list[dict]:
    """Average realized return per recommendation bucket, in STRONG_BUY->STRONG_SELL order."""
    if df.empty:
        return []
    sub = df.dropna(subset=["actual_return", "recommendation"])
    out = []
    for rec in RECOMMENDATION_ORDER:
        rows = sub[sub["recommendation"].str.upper() == rec]
        out.append({
            "recommendation": rec,
            "avg_return": float(rows["actual_return"].mean()) if len(rows) else None,
            "n": int(len(rows)),
        })
    return out


def _signal_equity_curve(df: pd.DataFrame, buy_recs: tuple[str, ...] = ("STRONG_BUY", "BUY")) -> pd.DataFrame:
    """
    Hypothetical cumulative return from mechanically following every BUY /
    STRONG_BUY call, vs. a same-window SPY benchmark derived from the already-
    stored excess_return (benchmark_return = actual_return - excess_return).

    Each step is one closed call, sequenced by evaluation date — not a daily-
    compounded curve, since calls overlap across tickers and horizons. That
    makes this a "did mechanically following the calls beat SPY over the same
    stretches of time" comparison, not a literal portfolio simulation.
    """
    if df.empty or "recommendation" not in df.columns:
        return pd.DataFrame()
    sub = df[df["recommendation"].str.upper().isin(buy_recs)].dropna(
        subset=["actual_return", "excess_return"]
    ).copy()
    if sub.empty:
        return pd.DataFrame()
    sub = sub.sort_values("event_date")
    sub["benchmark_return"] = sub["actual_return"] - sub["excess_return"]
    sub["strategy_cum"]  = (1 + sub["actual_return"]).cumprod()
    sub["benchmark_cum"] = (1 + sub["benchmark_return"]).cumprod()
    sub["call_num"] = range(1, len(sub) + 1)
    return sub[[
        "event_date", "call_num", "ticker", "recommendation",
        "actual_return", "benchmark_return", "strategy_cum", "benchmark_cum",
    ]]
