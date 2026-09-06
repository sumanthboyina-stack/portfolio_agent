"""
Validation dashboard data functions.

Contains:
  - _load_metric_series          : per-prediction brier/log-loss series
  - _load_portfolio_tickers      : ticker symbols from portfolio.yaml
  - _get_model_names             : distinct model_name values from predictions
  - _compute_filtered_metrics    : rolling metrics recomputed from predictions table
  - _load_rolling_metrics_series : historical rows from metrics_rolling table
  - _directional_correct         : outcome -> bool correctness helper
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import yaml as _yaml

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

    seg_clause = ""
    seg_params: list = []
    if segment == "Portfolio" and portfolio_tickers:
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
    """Return unique portfolio ticker symbols from portfolio.yaml."""
    try:
        data = _yaml.safe_load((_ROOT / "config" / "portfolio.yaml").read_text()) or {}
        return list({h["ticker"].upper() for h in data.get("holdings", []) if "ticker" in h})
    except Exception:
        return []


def _load_watchlist_tickers() -> list[str]:
    """Return unique watchlist ticker symbols from watchlist.yaml (excludes holdings)."""
    try:
        data = _yaml.safe_load((_ROOT / "config" / "watchlist.yaml").read_text()) or {}
        watchlist = {str(t).upper() for t in data.get("tickers", [])}
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

    seg_clause = ""
    seg_params: list = []
    if segment == "Portfolio" and portfolio_tickers:
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
