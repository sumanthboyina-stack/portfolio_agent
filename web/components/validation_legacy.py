"""
Blocks that used to live on the user-facing Validation page, moved here unchanged in behaviour so the
admin-only Validation QA page can host them (the user page now shows only the scorecard).

Contains:
  - render_calibration_headline : the one-line "when APEX says X% confident…" statement
  - render_outcome_breakdown    : outcome-breakdown chart, avg realized return by recommendation, SELL-call stats
  - render_legacy_tiles         : the metric-tile grid (headline row + secondary tiles), Raw Values table, horizon chart
  - render_equity_curve         : cumulative "followed every BUY call" index vs SPY / Dow / Nasdaq / own portfolio
  - render_scoring_snapshots    : today's four-system scoring table + score-vs-returns quintile chart

The inner "Model" multiselect the old page had is gone: the QA sidebar's model filter already narrows the data
these functions receive.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

import pandas as pd
import streamlit as st

from web.styles import material, fmt_pct, section_tile
from web.data.validation import (
    _add_index_benchmarks,
    _add_portfolio_benchmark,
    _add_streak_ids,
    _calibration_headline,
    _outcome_breakdown,
    _returns_by_recommendation,
    _sell_call_outcomes,
    _signal_equity_curve,
    equity_curve_date_span,
)
from web.components.validation_charts import (
    _METRIC_CFG,
    build_horizon_reliability_chart,
    build_outcome_breakdown_chart,
    build_returns_by_recommendation_chart,
    build_score_vs_returns_chart,
    build_signal_equity_curve_chart,
)
from web.components.validation_cards import _render_scorecard_tiles


def render_calibration_headline(lookback: int, model_names: list[str] | None = None) -> None:
    from portfolio_agent.tools.validation_engine import get_calibration_data

    headline = _calibration_headline(
        get_calibration_data(lookback_days=lookback, model_names=model_names).get("reliability", [])
    )
    if headline:
        st.info(
            f"When APEX says it's about **{headline['mean_stated']*100:.0f}% confident** a stock will rise, "
            f"it's actually right **{headline['actual_hit_rate']*100:.0f}%** of the time "
            f"(based on {headline['n']} evaluated calls in this confidence range).",
            icon=material("track_changes"),
        )
    else:
        st.caption(
            f"{material('hourglass_top')} Calibration line will appear once enough predictions with a "
            "probability distribution have been evaluated."
        )


def render_outcome_breakdown(eval_df: pd.DataFrame) -> None:
    """Old "Did This Actually Work?" top half: outcome breakdown + avg realized return by recommendation + SELL stats."""
    if not eval_df.empty:
        _n_raw = len(eval_df)
        _streaked_df = _add_streak_ids(eval_df)
        _n_streaks = _streaked_df["streak_id"].nunique() if "streak_id" in _streaked_df.columns else _n_raw
        if _n_streaks < _n_raw:
            st.caption(
                f"{_n_raw} evaluated calls below, but only {_n_streaks} unique rating streaks — "
                "a ticker held at the same call for many days running counts once per streak, "
                "not once per day, in that count."
            )

    oc1, oc2 = st.columns(2)
    with oc1:
        st.caption("Outcome breakdown")
        breakdown = _outcome_breakdown(eval_df)
        if breakdown and sum(b["count"] for b in breakdown):
            st.plotly_chart(build_outcome_breakdown_chart(breakdown), use_container_width=True)
        else:
            st.info("No evaluated predictions yet.", icon=material("info"))
    with oc2:
        st.caption("Avg realized return by recommendation (all horizons pooled — the user page splits by horizon)")
        rec_returns = _returns_by_recommendation(eval_df)
        if rec_returns and any(r["n"] for r in rec_returns):
            st.plotly_chart(build_returns_by_recommendation_chart(rec_returns), use_container_width=True)
        else:
            st.info("No evaluated predictions yet.", icon=material("info"))

    sell_stats = _sell_call_outcomes(eval_df)
    if sell_stats["n"]:
        sc1, sc2, sc3 = st.columns(3)
        sc1.metric("SELL / STRONG_SELL calls evaluated", sell_stats["n"])
        sc2.metric(
            "% that actually fell",
            fmt_pct(sell_stats["pct_fell"] * 100) if sell_stats["pct_fell"] is not None else "—",
        )
        sc3.metric(
            "Avg return vs SPY",
            fmt_pct(sell_stats["avg_excess_return"] * 100, signed=True)
            if sell_stats["avg_excess_return"] is not None else "—",
            help="Negative = the ticker underperformed SPY over the same window, "
                 "i.e. selling and rotating into the market would have been the better call.",
        )
        if sell_stats["n"] < 30:
            st.caption(f"{material('warning')} Only {sell_stats['n']} evaluated SELL calls — read with caution.")
    else:
        st.caption("No evaluated SELL / STRONG_SELL calls yet.")


def render_legacy_tiles(by_horizon: dict, all_horizons: list, horizon_labels: dict, reports: dict,
                        horizon_baselines: dict) -> None:
    """Old metric-tile grid (ranking headline row + secondary tiles per horizon), Raw Values table and the
    directional-accuracy-by-horizon chart. The Baselines / Ranking detail / FLAT sections that used to hang off
    each horizon now have their own QA tabs, so no expanders are attached here."""
    if not by_horizon:
        st.info(
            "No evaluated predictions yet. "
            f"Click {material('nights_stay')} **Evening** on the {material('calendar_month')} "
            "Schedule page to score matured predictions.",
            icon=material("info"),
        )
        return

    _tile_2 = section_tile("Ranking Skill & All Metrics by Horizon", expanded=True, key="valqa_legacy_tiles")
    if _tile_2:
        with _tile_2:
            st.caption(
                "Headline row per horizon: does a higher score go with beating SPY, and do UP-labelled names beat "
                "FLAT-labelled ones? Intervals resample whole dates. The row below it (3-class accuracy, calibration) "
                "is secondary. Hover any tile for the definition and how to read it.")
            _render_scorecard_tiles(by_horizon, all_horizons, horizon_labels, reports=reports)

    _tile_3 = section_tile("Raw Values", expanded=False, key="valqa_legacy_raw")
    if _tile_3:
        with _tile_3:
            raw_rows = []
            for h_days in all_horizons:
                row  = by_horizon[h_days]
                hlbl = horizon_labels.get(h_days, f"{h_days}d")
                n    = row.get("num_predictions") or 0
                _five = ((reports.get(h_days) or {}).get("probability") or {}).get("five")
                raw_rows.append({
                    "Horizon":           hlbl,
                    "Evaluated Preds":   f"{n} ⚠" if n < 30 else n,
                    "Dir Accuracy":   fmt_pct(row["directional_accuracy"]*100) if row.get("directional_accuracy") is not None else "—",
                    "In-Range %":     fmt_pct(row["in_range_pct"]*100)         if row.get("in_range_pct")          is not None else "—",
                    "Excess Return":  fmt_pct(row["mean_excess_return"]*100, signed=True)  if row.get("mean_excess_return")     is not None else "—",
                    "Hi-Conv Acc":    fmt_pct(row["high_conviction_accuracy"]*100) if row.get("high_conviction_accuracy") is not None else "—",
                    "Lo-Conv Acc":    fmt_pct(row["low_conviction_accuracy"]*100)  if row.get("low_conviction_accuracy")  is not None else "—",
                    "Brier (UP vs not-UP)":    f"{row['brier_score']:.3f}"   if row.get("brier_score")  is not None else "—",
                    "Log-Loss (UP vs not-UP)": f"{row['mean_log_loss']:.3f}" if row.get("mean_log_loss") is not None else "—",
                    "Brier (5-bucket)":    f"{_five['brier']:.3f}"    if _five else "—",
                    "Log-Loss (5-bucket)": f"{_five['log_loss']:.3f}" if _five else "—",
                })
            st.dataframe(
                pd.DataFrame(raw_rows),
                hide_index=True,
                use_container_width=True,
                column_config={
                    "Horizon":         st.column_config.Column(help="Prediction horizon — how many trading days ahead this call was for."),
                    "Evaluated Preds": st.column_config.Column(help="Number of matured, scored predictions at this horizon over the selected lookback period. ⚠ = fewer than 30 — every metric in this row is not yet statistically reliable."),
                    "Dir Accuracy":    st.column_config.Column(help=_METRIC_CFG["directional_accuracy"]["defn"]),
                    "In-Range %":      st.column_config.Column(help=_METRIC_CFG["in_range_pct"]["defn"]),
                    "Excess Return":   st.column_config.Column(help=_METRIC_CFG["mean_excess_return"]["defn"]),
                    "Hi-Conv Acc":     st.column_config.Column(help=_METRIC_CFG["high_conviction_accuracy"]["defn"]),
                    "Lo-Conv Acc":     st.column_config.Column(help=_METRIC_CFG["low_conviction_accuracy"]["defn"]),
                    "Brier (UP vs not-UP)":    st.column_config.Column(help=_METRIC_CFG["brier_score"]["defn"]),
                    "Log-Loss (UP vs not-UP)": st.column_config.Column(help=_METRIC_CFG["mean_log_loss"]["defn"]),
                    "Brier (5-bucket)":        st.column_config.Column(help=_METRIC_CFG["brier_5bucket"]["defn"]),
                    "Log-Loss (5-bucket)":     st.column_config.Column(help=_METRIC_CFG["logloss_5bucket"]["defn"]),
                },
            )

    _tile_6 = section_tile("Which Horizon Is More Reliable?", expanded=True, key="valqa_legacy_horizon")
    if _tile_6:
        with _tile_6:
            st.caption(
                "Directional accuracy by prediction horizon — useful for deciding how much to weight "
                "a 5-day call vs. a 63-day one."
            )
            st.plotly_chart(
                build_horizon_reliability_chart(by_horizon, all_horizons, horizon_labels, horizon_baselines),
                use_container_width=True,
            )


@st.cache_data(ttl=1800, show_spinner="Fetching Dow/Nasdaq…")
def _cached_index_closes(start: str, end: str) -> dict[str, dict[str, float]]:
    from portfolio_agent.tools.yfinance_tools import get_closes_in_range
    return {s: get_closes_in_range(s, start, end) for s in ("^DJI", "^IXIC")}


def render_equity_curve(eval_df: pd.DataFrame) -> None:
    """Cumulative return from mechanically following every BUY / STRONG_BUY call, vs SPY / Dow / Nasdaq / own portfolio."""
    st.info(
        "**This is a compounded call-return index, not a portfolio.** Each step is one closed BUY / STRONG_BUY call, "
        "multiplied together in order of evaluation date. Calls overlap in time, are not sized, and are not capital-"
        "constrained, so the line shows whether following the calls mechanically beat SPY over the same stretches — "
        "not what an account would have earned.",
        icon=material("info"),
    )
    st.caption(
        "If you'd mechanically followed every BUY / STRONG_BUY call, vs. SPY, Dow Jones, "
        "Nasdaq (each over the same per-call windows) and your own portfolio's actual performance"
    )
    curve_df = _signal_equity_curve(eval_df)
    if not curve_df.empty:
        _span = equity_curve_date_span(curve_df)
        if _span:
            curve_df = _add_index_benchmarks(curve_df, _cached_index_closes(*_span))
        curve_df = _add_portfolio_benchmark(curve_df)
        st.plotly_chart(build_signal_equity_curve_chart(curve_df), use_container_width=True)
    else:
        st.info("Not enough evaluated BUY / STRONG_BUY calls yet to plot a cumulative curve.", icon=material("info"))


def render_scoring_snapshots() -> None:
    """Today's scoring table across the four parallel systems + top-vs-bottom-quintile returns."""
    from portfolio_agent.tools.scoring_snapshot_db import get_latest_snapshot_date, get_snapshots_for_date
    from portfolio_agent.tools.calibration_analysis import compute_calibration_report, SCORE_LABELS

    _tile_4 = section_tile(f"{material('calculate')} Today's Scoring Table", expanded=True, key="valqa_snap_table")
    if _tile_4:
        with _tile_4:
            st.caption(
                "Every ticker scored today across all four parallel systems, plus a documented fixed "
                "blend (Final) — captured daily so it can be checked against realized returns later."
            )
            snap_date = get_latest_snapshot_date()
            if not snap_date:
                st.info(
                    "No scoring snapshots yet — this table populates once the morning batch's "
                    "Score Calibration phase runs.",
                    icon=material("calculate"),
                )
            else:
                st.caption(f"As of {snap_date}")
                snap_rows = get_snapshots_for_date(snap_date)
                table_rows = [{
                    "Ticker":        r["ticker"],
                    "APEX":          f"{r['apex_score']:.2f}" if r.get("apex_score") is not None else "—",
                    "Valuation":     f"{r['valuation_score']:.2f}" if r.get("valuation_score") is not None else "—",
                    "Opportunity":   f"{r['opportunity_score']:.2f}" if r.get("opportunity_score") is not None else "—",
                    "Portfolio Fit": f"{r['portfolio_fit_score']:.2f}" if r.get("portfolio_fit_score") is not None else "—",
                    "Final":         f"{r['final_score']:.2f}" if r.get("final_score") is not None else "—",
                } for r in snap_rows]
                st.dataframe(
                    pd.DataFrame(table_rows),
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "Ticker":        st.column_config.Column(help="Stock ticker symbol."),
                        "APEX":          st.column_config.Column(help="4-analyst panel composite score (0–10) — Fundamentals, Research, Macro, and News blended."),
                        "Valuation":     st.column_config.Column(help="DCF-based valuation score (0–10) — how attractively priced vs. intrinsic value."),
                        "Opportunity":   st.column_config.Column(help="Opportunity Engine score (0–100) — momentum, correlation, and portfolio fit blended for new candidates."),
                        "Portfolio Fit": st.column_config.Column(help="Position-sizing / correlation fit score (0–100) — how well this ticker complements existing holdings."),
                        "Final":         st.column_config.Column(help="Weighted blend, normalized to 0–100: 30% APEX + 20% Valuation + 30% Opportunity + 20% Portfolio Fit."),
                    },
                )
    _tile_5 = section_tile(f"{material('bar_chart')} Score vs Returns", expanded=False, key="valqa_snap_returns")
    if _tile_5:
        with _tile_5:
            st.caption(
                "For each scoring system: the average realized return of the top-20% vs. bottom-20% "
                "scored names at this horizon — did the names we scored highly actually do better?"
            )
            horizon_choice = st.radio(
                "Horizon", [30, 60, 90, 250], format_func=lambda h: f"{h}d", horizontal=True, key="valqa_calib_horizon",
            )
            report = compute_calibration_report(horizon_choice)
            _has_any = any(not s.get("insufficient_data") for s in report["scores"].values())
            if not _has_any:
                st.info(
                    f"Not enough matured {horizon_choice}-day snapshots yet to compare scores against returns.",
                    icon=material("info"),
                )
            else:
                st.plotly_chart(build_score_vs_returns_chart(report, SCORE_LABELS), use_container_width=True)

            any_insufficient = any(s.get("insufficient_data") for s in report["scores"].values())
            if any_insufficient:
                st.info(
                    f"Some scores don't have {report['min_sample']} matured {horizon_choice}-day snapshots yet — "
                    f"a {horizon_choice}-day horizon needs {horizon_choice} days of history before the first row "
                    "can even mature, so this fills in gradually. Check back as more days pass.",
                    icon=material("hourglass_top"),
                )
