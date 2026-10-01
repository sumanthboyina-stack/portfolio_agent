"""
Validation dashboard data functions.

Contains:
  - _load_metric_series          : per-prediction brier/log-loss series
  - _load_portfolio_tickers      : ticker symbols from the CALLER's own holdings
  - _load_watchlist_tickers      : ticker symbols from the CALLER's own watchlist
  - _get_model_names             : distinct model_name values from predictions
  - _compute_filtered_metrics    : rolling metrics recomputed from predictions table
  - _load_rolling_metrics_series : historical rows from metrics_rolling table
  - _directional_correct         : outcome -> bool correctness helper
  - _load_evaluated_predictions_df : broad evaluated-prediction rows (no brier requirement)
  - _add_streak_ids              : tag consecutive same-ticker-same-horizon-same-call runs
  - _outcome_breakdown           : outcome bucket counts (raw + unique-streak)
  - _returns_by_recommendation   : avg realized return per recommendation bucket (raw + unique-streak)
  - _sell_call_outcomes          : did SELL/STRONG_SELL calls avoid losses
  - load_price_history           : cached bulk close fetch for the prior-window baselines
  - build_horizon_reports        : per-horizon baselines / skill / effective-N (validation_metrics)
  - _signal_equity_curve         : cumulative "followed the BUY calls" vs SPY curve
  - equity_curve_date_span       : date range to bulk-fetch/cache index closes over
  - _add_index_benchmarks        : + Dow/Nasdaq cumulative return, same per-call windows as SPY
  - _add_portfolio_benchmark     : + the viewer's own recorded portfolio value, re-based to the curve's start

Every query against `predictions` below is restricted to LEGACY_REVIEW_SCOPES
(shared_market + legacy_unclassified, never private) — the same scope
validation_engine.py uses for historical evaluation/dashboards. Without it, a
private chat forecast (yours or, now that logins are per-owner, someone
else's) would get folded into what reads as the shared track record.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

import pandas as pd

from portfolio_agent.tools.prediction_db import LEGACY_REVIEW_SCOPES, scope_clause

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

    _sc, _sp = scope_clause(LEGACY_REVIEW_SCOPES)
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
              AND {_sc}
              {seg_clause}
            ORDER BY as_of_date ASC
        """, [*_sp, *seg_params]).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    if not df.empty:
        df["prediction_date"] = pd.to_datetime(df["prediction_date"])
        df["horizon_label"]   = df["horizon_days"].apply(
            lambda h: f"{h}d" if pd.notna(h) else "?"
        )
    return df


def _load_portfolio_tickers(ctx) -> list[str]:
    """Return unique ticker symbols from ctx's OWN holdings — not
    get_portfolio_tickers()'s default union across every portfolio, which
    would make "My Portfolio" mean "everyone's portfolio" once more than one
    owner exists."""
    try:
        from portfolio_agent.services.account_service import list_holdings_for
        return sorted({str(h["ticker"]).upper() for h in list_holdings_for(ctx) if h.get("ticker")})
    except Exception:
        return []


def _load_watchlist_tickers(ctx) -> list[str]:
    """Return unique watchlist ticker symbols owned by ctx (excludes holdings)."""
    from portfolio_agent.tools.watchlist_db import load_watchlist_tickers_for
    try:
        watchlist = set(load_watchlist_tickers_for(ctx))
        return list(watchlist - set(_load_portfolio_tickers(ctx)))
    except Exception:
        return []


def _get_model_names() -> list[str]:
    """Return distinct model_name values from predictions table, sorted by count desc."""
    if not _DB.exists():
        return []
    _sc, _sp = scope_clause(LEGACY_REVIEW_SCOPES)
    with sqlite3.connect(str(_DB)) as conn:
        rows = conn.execute(
            f"""SELECT model_name, COUNT(*) as cnt FROM predictions
               WHERE model_name IS NOT NULL
                 AND {_sc}
               GROUP BY model_name ORDER BY cnt DESC""",
            _sp,
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

    _sc, _sp = scope_clause(LEGACY_REVIEW_SCOPES)
    params: list = [lookback] + list(_sp) + model_params + seg_params

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
          AND {_sc}
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

    _sc, _sp = scope_clause(LEGACY_REVIEW_SCOPES)
    clauses = ["evaluation_status = 'evaluated'", _sc]
    params: list = list(_sp)
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
            SELECT id, as_of_date, evaluation_date, evaluated_at, ticker, horizon_days,
                   recommendation, outcome, actual_return, excess_return, benchmark_return,
                   predicted_direction, actual_direction,
                   predicted_return_low, predicted_return_high,
                   p_strong_down, p_moderate_down, p_flat, p_moderate_up, p_strong_up,
                   conviction_score, composite_score, trigger_type, model_name
            FROM predictions
            {where}
            ORDER BY COALESCE(evaluation_date, as_of_date) ASC
        """, params).fetchall()

    df = pd.DataFrame([dict(r) for r in rows])
    if not df.empty:
        df["event_date"] = pd.to_datetime(df["evaluation_date"].fillna(df["as_of_date"]))
    return df


def _add_streak_ids(df: pd.DataFrame) -> pd.DataFrame:
    """
    Tag each row with a streak id: consecutive predictions for the same
    (ticker, horizon_days), ordered by event_date, that share the same
    recommendation. A ticker rated BUY for 20 straight days is one streak of
    20 highly-correlated rows, not 20 independent calls -- exposed so callers
    can report both the raw row count and the deduplicated "how many distinct
    calls" count.
    """
    if df.empty or "recommendation" not in df.columns:
        return df
    d = df.sort_values(["ticker", "horizon_days", "event_date"]).reset_index(drop=True).copy()
    grp_key = d["ticker"].astype(str) + "|" + d["horizon_days"].astype(str)
    new_streak = (grp_key != grp_key.shift()) | (d["recommendation"] != d["recommendation"].shift())
    d["streak_id"] = new_streak.cumsum()
    return d


def _outcome_breakdown(df: pd.DataFrame) -> list[dict]:
    """
    Count of evaluated predictions per outcome bucket, in fixed
    strong->wrong order -- both the raw row count and the unique-streak
    count (one representative row -- the last -- per consecutive same-call
    run), so a ticker rated BUY for 20 straight days doesn't read as 20
    independent data points.
    """
    if df.empty or "outcome" not in df.columns:
        return []
    counts = df["outcome"].value_counts().to_dict()
    streaked = _add_streak_ids(df)
    streak_reps = streaked.groupby("streak_id").tail(1) if "streak_id" in streaked.columns else df
    streak_counts = streak_reps["outcome"].value_counts().to_dict()
    return [
        {"outcome": o, "count": int(counts.get(o, 0)), "streak_count": int(streak_counts.get(o, 0))}
        for o in OUTCOME_ORDER
    ]


def _returns_by_recommendation(df: pd.DataFrame) -> list[dict]:
    """
    Average realized return per recommendation bucket, in
    STRONG_BUY->STRONG_SELL order -- both the raw call count (n) and the
    unique-streak count (streak_n): a ticker rated the same way for many
    days running is one streak, not one independent call per day.
    """
    if df.empty:
        return []
    sub = df.dropna(subset=["actual_return", "recommendation"])
    streaked = _add_streak_ids(sub)
    out = []
    for rec in RECOMMENDATION_ORDER:
        rows = sub[sub["recommendation"].str.upper() == rec]
        streak_n = (
            streaked.loc[streaked["recommendation"].str.upper() == rec, "streak_id"].nunique()
            if "streak_id" in streaked.columns else len(rows)
        )
        out.append({
            "recommendation": rec,
            "avg_return": float(rows["actual_return"].mean()) if len(rows) else None,
            "n": int(len(rows)),
            "streak_n": int(streak_n),
        })
    return out


_PRICE_TTL_SECONDS = 1800
_price_cache: dict[tuple[str, str, str], tuple[float, dict[str, float]]] = {}


def load_price_history(
    requests: dict[str, tuple[str, str]],
    fetch=None,
    now=None,
) -> dict[str, dict[str, float]]:
    """
    {symbol: {date_iso: close}} for {symbol: (start, end)}, fetched once per
    symbol via yfinance_tools.get_closes_in_range and cached in-process for
    30 minutes (the cache outlives Streamlit reruns and sessions). A failed or
    empty fetch yields {} for that symbol -- the baselines that need it are
    then reported as unavailable rather than raising. `fetch`/`now` are
    injectable for tests.
    """
    import time

    if fetch is None:
        from portfolio_agent.tools.yfinance_tools import get_closes_in_range as fetch
    now = time.time() if now is None else now
    out: dict[str, dict[str, float]] = {}
    todo: dict[str, tuple[str, str]] = {}
    for sym, (start, end) in requests.items():
        hit = _price_cache.get((sym, start, end))
        if hit and now - hit[0] < _PRICE_TTL_SECONDS:
            out[sym] = hit[1]
        else:
            todo[sym] = (start, end)

    def _one(item):
        sym, (start, end) = item
        try:
            return sym, (fetch(sym, start, end) or {})
        except Exception:
            return sym, {}

    if todo:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(8, len(todo))) as pool:
            for sym, closes in pool.map(_one, todo.items()):
                if closes:               # don't cache failures
                    _price_cache[(sym, *todo[sym])] = (now, closes)
                out[sym] = closes
    return out


def build_horizon_reports(df: pd.DataFrame, fetch=None, use_prices: bool = True) -> dict[int, dict]:
    """
    {horizon_days: validation_metrics.horizon_report(...)} for an evaluated-
    predictions frame (see _load_evaluated_predictions_df). Horizons are never
    mixed. streak_n uses _add_streak_ids on that horizon's rows, the same
    streak logic as the rest of this page. use_prices=False skips the price
    fetch (prior-window baselines then show as unavailable); price data
    failures degrade the same way; an empty frame yields {}.
    """
    from portfolio_agent.tools import validation_metrics as vm

    if df is None or df.empty or "horizon_days" not in df.columns:
        return {}
    reports: dict[int, dict] = {}
    for h, sub in df.dropna(subset=["horizon_days"]).groupby("horizon_days"):
        sub = sub.reset_index(drop=True)
        streaked = _add_streak_ids(sub)
        streak_n = int(streaked["streak_id"].nunique()) if "streak_id" in streaked.columns else None
        closes: dict = {}
        if use_prices:
            try:
                closes = load_price_history(vm.price_requests(sub), fetch=fetch)
            except Exception:
                closes = {}
        reports[int(h)] = vm.horizon_report(sub, closes, streak_n=streak_n)
    return reports


def _sell_call_outcomes(df: pd.DataFrame, sell_recs: tuple[str, ...] = ("SELL", "STRONG_SELL")) -> dict:
    """
    "Did SELL calls avoid losses?" -- the % of evaluated SELL/STRONG_SELL
    calls whose ticker actually fell, and the average return vs SPY
    (excess_return) for those calls. Answers the question a single
    returns-by-recommendation bar can't: a SELL call isn't "wrong" just
    because the price still rose, it's wrong if it rose faster than the
    market it was implicitly telling you to rotate into.
    """
    if df.empty or "recommendation" not in df.columns:
        return {"n": 0, "pct_fell": None, "avg_excess_return": None}
    sub = df[df["recommendation"].str.upper().isin(sell_recs)].dropna(subset=["actual_return"])
    if sub.empty:
        return {"n": 0, "pct_fell": None, "avg_excess_return": None}
    excess = sub["excess_return"].dropna()
    return {
        "n": int(len(sub)),
        "pct_fell": float((sub["actual_return"] < 0).mean()),
        "avg_excess_return": float(excess.mean()) if not excess.empty else None,
    }


def _calibration_headline(reliability: list[dict], min_n: int = 20) -> dict | None:
    """
    Pick the reliability bucket (from validation_engine.get_calibration_data's
    "reliability" list) closest to a round, headline-worthy stated confidence
    (~70%) with enough samples to be worth quoting; falls back to whichever
    bucket has the most samples if none clears min_n. Returns None if there's
    no reliability data at all -- the page then shows "not enough data yet"
    instead of fabricating a number.
    """
    if not reliability:
        return None
    candidates = [b for b in reliability if b["n"] >= min_n]
    pool = candidates or reliability
    return min(pool, key=lambda b: (abs(b["center"] - 0.70), -b["n"]))


def _signal_equity_curve(df: pd.DataFrame, buy_recs: tuple[str, ...] = ("STRONG_BUY", "BUY")) -> pd.DataFrame:
    """
    Hypothetical cumulative return from mechanically following every BUY /
    STRONG_BUY call, vs. a same-window SPY benchmark derived from the already-
    stored excess_return (benchmark_return = actual_return - excess_return).

    Each step is one closed call, sequenced by evaluation date — not a daily-
    compounded curve, since calls overlap across tickers and horizons. That
    makes this a "did mechanically following the calls beat SPY over the same
    stretches of time" comparison, not a literal portfolio simulation.

    as_of_date/evaluation_date (each call's own window) are carried through
    so _add_index_benchmarks() can fetch OTHER indices (Dow, Nasdaq) over the
    exact same per-call windows SPY's benchmark_return already uses.
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
        "as_of_date", "evaluation_date",
    ]].reset_index(drop=True)


def _closest_close(closes: dict[str, float], target: str, *, direction: str = "after",
                   max_lookahead_days: int = 7) -> float | None:
    """
    closes: {date_iso: close} from yfinance_tools.get_closes_in_range (only
    actual trading days are keys). direction='after' mirrors get_close()'s
    own "on or just after" semantics (a prediction's as_of/evaluation date is
    always a trading day, but the exact date is looked up defensively);
    direction='before' is carry-forward, for a less-frequently-snapshotted
    series like portfolio value against a daily index calendar.
    """
    if target in closes:
        return closes[target]
    from datetime import date, timedelta
    d = date.fromisoformat(target)
    if direction == "after":
        for i in range(1, max_lookahead_days + 1):
            cand = (d + timedelta(days=i)).isoformat()
            if cand in closes:
                return closes[cand]
        return None
    candidates = [c for c in closes if c <= target]
    return closes[max(candidates)] if candidates else None


def equity_curve_date_span(curve_df: pd.DataFrame) -> tuple[str, str] | None:
    """(start, end) covering every call's window in curve_df, for a caller
    (the page) to bulk-fetch and cache index closes keyed on this hashable
    pair, instead of re-fetching on every Streamlit rerun."""
    if curve_df.empty:
        return None
    start = min(curve_df["as_of_date"].dropna(), default=None)
    end   = max(curve_df["evaluation_date"].dropna(), default=None)
    return (start, end) if (start and end) else None


def _add_index_benchmarks(
    curve_df: pd.DataFrame,
    closes_by_symbol: dict[str, dict[str, float]],
    symbols: dict[str, str] | None = None,
) -> pd.DataFrame:
    """
    Add a cumulative-return column per extra index (Dow Jones ^DJI, Nasdaq
    ^IXIC by default), each computed over the EXACT same per-call window
    (as_of_date -> evaluation_date) SPY's own benchmark_return already uses —
    so "did the calls beat the market" is judged consistently across every
    line on the chart, not SPY-over-its-real-window vs. Dow-over-some-other-
    window.

    closes_by_symbol: {symbol: {date_iso: close}}, already bulk-fetched (see
    equity_curve_date_span()) — this function does no I/O itself, so the
    caller can cache the fetch across Streamlit reruns.
    """
    if curve_df.empty:
        return curve_df
    symbols = symbols or {"^DJI": "dow_cum", "^IXIC": "nasdaq_cum"}
    out = curve_df.copy()
    for symbol, col in symbols.items():
        closes = closes_by_symbol.get(symbol) or {}
        if not closes:
            out[col] = None
            continue
        rets = []
        for _, row in out.iterrows():
            s = _closest_close(closes, row["as_of_date"], direction="after")
            e = _closest_close(closes, row["evaluation_date"], direction="after")
            rets.append((e / s - 1) if (s and e) else None)
        cum, running = [], 1.0
        for r in rets:
            if r is not None:
                running *= (1 + r)
            cum.append(running)
        out[col] = cum
    return out


def _add_portfolio_benchmark(curve_df: pd.DataFrame) -> pd.DataFrame:
    """
    Add the viewer's own recorded portfolio value as a comparison line,
    re-based to 1.0 at the equity curve's own starting date so it's visually
    comparable to the strategy/SPY/index cumulative-return lines.

    portfolio_price_history has no owner/portfolio-scoping column yet (a
    pre-existing gap shared with the Portfolio page's own trend chart, which
    reads the same table the same unscoped way) — in a genuinely
    multi-portfolio deployment this would read every portfolio's combined
    value, not just the viewer's own.
    """
    if curve_df.empty:
        return curve_df
    from portfolio_agent.tools.holdings_db import get_price_history
    from web.data.performance import build_trend_series

    series = build_trend_series(get_price_history())
    out = curve_df.copy()
    if not series["dates"] or not series["value"]:
        out["portfolio_cum"] = None
        return out
    by_date = dict(zip(series["dates"], series["value"]))
    base = _closest_close(by_date, out.iloc[0]["as_of_date"], direction="before") or next(iter(by_date.values()))
    out["portfolio_cum"] = [
        (_closest_close(by_date, d, direction="before") or base) / base if base else None
        for d in out["as_of_date"]
    ]
    return out
