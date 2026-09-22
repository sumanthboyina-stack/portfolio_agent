"""
APEX Prediction Validation — user-facing trust page: "is this reliable" and
"did it make money." Opens scoped to the viewer's own holdings + watchlist by
default, with an explicit toggle to widen to the full shared prediction
universe (predictions themselves are shared across everyone; this default
just keeps what's on screen relevant to the person looking at it).

Operator/engineering diagnostics that used to live here — raw Metric Trends,
the full reliability-diagram Calibration tab, Post-mortems, and Version
comparison — now live on the separate Validation QA admin page instead. None
of those help a friend decide whether to trust a BUY call; they help tune the
shared prediction engine, which is an operator concern.

Privacy note: every filter here (segment radio, horizon/outcome multiselects)
narrows by *criteria*, never by an enumerable list of other people's specific
tickers — so there's nothing on this page that could let one viewer page
through a dropdown to infer what somebody else holds or watches. That
property needs to be preserved if/when this becomes genuinely multi-user;
today there's a single shared holdings/watchlist DB, so the question is
moot in practice but the filter design already assumes the stricter future.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

import pandas as pd
import streamlit as st
from web.styles import inject_global_css, top_nav, icon_html, material, fmt_pct, fmt_money

st.set_page_config(
    page_title="Validation — Portfolio Intelligence",
    page_icon=material("track_changes"),
    layout="wide",
    initial_sidebar_state="expanded",
)
inject_global_css()
top_nav("validation")

from portfolio_agent.tools.prediction_db import CURRENT_SYSTEM_VERSION
from web.data.validation import (
    _load_portfolio_tickers,
    _load_watchlist_tickers,
    _get_model_names,
    _compute_filtered_metrics,
    _load_evaluated_predictions_df,
    _outcome_breakdown,
    _returns_by_recommendation,
    _signal_equity_curve,
)
from web.components.validation_charts import (
    build_outcome_breakdown_chart,
    build_returns_by_recommendation_chart,
    build_signal_equity_curve_chart,
    build_score_vs_returns_chart,
    build_horizon_reliability_chart,
    _METRIC_CFG,
    OUTCOME_LABELS,
)
from web.components.validation_cards import _render_scorecard_tiles

# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown(
        '<p style="font-size:0.7rem;font-weight:700;text-transform:uppercase;'
        'letter-spacing:0.08em;color:#475569;margin:0 0 10px">Filters</p>',
        unsafe_allow_html=True,
    )
    st.caption(f"Run Evening (evaluation) and Weekly Analysis from the {material('calendar_month')} Schedule page.")

    st.divider()
    lookback = st.selectbox(
        "Lookback period",
        [30, 90, 365],
        index=1,
        format_func=lambda x: f"{x} days",
    )

    st.divider()

    # ── Model filter ─────────────────────────────────────────────────────────
    st.markdown(
        '<p style="font-size:0.72rem;font-weight:600;color:#9CA3AF;margin:0 0 8px">'
        'Model filter</p>',
        unsafe_allow_html=True,
    )
    def _is_higher_reasoning(model_name: str) -> bool:
        # Substring match (not an exact-string set) so ensemble label variants —
        # e.g. "Claude-Sonnet+GPT-4o" vs "Claude-Sonnet + GPT-4o" — and future
        # GPT-4x releases are all still classified correctly.
        n = model_name.lower()
        return "claude" in n or "gpt-4" in n

    _all_model_names = _get_model_names()
    _higher_names = [m for m in _all_model_names if _is_higher_reasoning(m)]
    _lower_names  = [m for m in _all_model_names if not _is_higher_reasoning(m)]

    _radio_options = ["All models"]
    if _higher_names:
        _radio_options.append("Higher reasoning")
    if _lower_names:
        _radio_options.append("Lower reasoning")

    _model_sel = st.selectbox(
        "model_filter",
        _radio_options,
        index=0,
        label_visibility="collapsed",
        key="val_model_radio",
    )

    _GROUP_NAMES = {"Higher reasoning": _higher_names, "Lower reasoning": _lower_names}

    if _model_sel == "All models":
        _model_filter: list[str] | None = None
        st.caption("All predictions — no model filter applied")
    else:
        _model_filter = _GROUP_NAMES[_model_sel]
        _icon_name = "psychology" if _model_sel == "Higher reasoning" else "bolt"
        st.caption(
            f"{material(_icon_name)} {_model_sel} (GPT-4x / Claude)"
            if _model_sel == "Higher reasoning" else f"{material(_icon_name)} {_model_sel}"
        )
        # Show individual names so the user knows what's included in the group
        st.markdown(
            '<div style="margin-top:4px">' +
            "".join(
                f'<div style="font-size:0.68rem;color:#6B7280;padding:1px 0">{icon_html(_icon_name, 12)} {m}</div>'
                for m in _model_filter
            ) + '</div>',
            unsafe_allow_html=True,
        )

    st.divider()

    # ── Scope — defaults to the viewer's own tickers, not the shared universe ──
    st.markdown(
        '<p style="font-size:0.72rem;font-weight:600;color:#9CA3AF;margin:0 0 8px">'
        'Scope</p>',
        unsafe_allow_html=True,
    )
    _seg_sel = st.radio(
        "segment",
        ["My Tickers", "All", "Portfolio", "Watchlist", "New Opportunities"],
        index=0,
        label_visibility="collapsed",
        key="val_segment",
        help=(
            "My Tickers (default): your holdings + watchlist combined  ·  "
            "All: everyone's shared prediction universe, for context  ·  "
            "Portfolio: your holdings only  ·  "
            "Watchlist: tracked but not held  ·  "
            "New Opportunities: trending tickers discovered by the scanner"
        ),
    )
    if _seg_sel == "All":
        st.caption(f"{material('public')} Widened to the full shared universe.")

# Load portfolio/watchlist tickers once (used by segment filter throughout)
_portfolio_tickers = _load_portfolio_tickers()
_watchlist_tickers = _load_watchlist_tickers()

# ── Page header ───────────────────────────────────────────────────────────────

st.title(f"{material('track_changes')} APEX Prediction Validation")
st.caption(f"System version: `{CURRENT_SYSTEM_VERSION}` · Scores predictions after they mature")

_SEG_ICONS = {
    "My Tickers":        material("person"),
    "All":               material("public"),
    "Portfolio":         material("work"),
    "Watchlist":         material("list_alt"),
    "New Opportunities": material("star"),
}

_active_filters: list[str] = []
if _model_filter:
    _active_filters.append(f"Model: {', '.join(f'`{m}`' for m in _model_filter)}")
if _seg_sel != "My Tickers":
    _active_filters.append(f"Scope: **{_SEG_ICONS.get(_seg_sel, material('search'))} {_seg_sel}**")
if _active_filters:
    st.info(
        "**Active filters** — " + "  ·  ".join(_active_filters) +
        "  ·  All metrics and tables recalculated for selected filter(s).",
        icon=material("search"),
    )

# ── Shared per-horizon metrics (used by both Scorecard and Heatmap tabs) ──────

metrics = _compute_filtered_metrics(_model_filter, lookback, _seg_sel, _portfolio_tickers, _watchlist_tickers)
by_horizon: dict[int, dict] = {}
for row in metrics:
    h = row["horizon_days"]
    if h not in by_horizon:
        by_horizon[h] = row

horizon_labels = {5: "5-Day", 21: "21-Day", 63: "63-Day"}
all_horizons = [h for h in [5, 21, 63] if h in by_horizon] + \
               [h for h in sorted(by_horizon) if h not in [5, 21, 63]]

# ── Tabs ──────────────────────────────────────────────────────────────────────

tabs = st.tabs([
    f"{material('bar_chart')} Scorecard",
    f"{material('list_alt')} Prediction History",
    f"{material('calculate')} Score vs Returns",
    f"{material('map')} Heatmap",
])

# ── Tab 1: Scorecard ──────────────────────────────────────────────────────────

with tabs[0]:
    if not metrics:
        st.info(
            "No evaluated predictions yet. "
            f"Click {material('nights_stay')} **Evening** on the {material('calendar_month')} "
            "Schedule page to score matured predictions.",
            icon=material("info"),
        )
    else:
        # ── "Did this actually work?" — outcome breakdown, returns by call,
        # and a hypothetical equity curve, all first, before the tiles ────────
        st.subheader("Did This Actually Work?")
        eval_df = _load_evaluated_predictions_df(
            lookback, _model_filter, _seg_sel, _portfolio_tickers, _watchlist_tickers
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
            st.caption("Avg realized return by recommendation")
            rec_returns = _returns_by_recommendation(eval_df)
            if rec_returns and any(r["n"] for r in rec_returns):
                st.plotly_chart(build_returns_by_recommendation_chart(rec_returns), use_container_width=True)
            else:
                st.info("No evaluated predictions yet.", icon=material("info"))

        st.caption("If you'd mechanically followed every BUY / STRONG_BUY call, vs. SPY over the same windows")
        curve_df = _signal_equity_curve(eval_df)
        if not curve_df.empty:
            st.plotly_chart(build_signal_equity_curve_chart(curve_df), use_container_width=True)
        else:
            st.info("Not enough evaluated BUY / STRONG_BUY calls yet to plot a cumulative curve.", icon=material("info"))

        st.divider()

        # ── Metric tiles ──────────────────────────────────────────────────────
        st.subheader("All Metrics by Horizon")
        st.caption("Hover any tile for the full definition, performance tiers, and reference values.")
        _render_scorecard_tiles(by_horizon, all_horizons, horizon_labels)

        st.divider()

        # ── Raw values table ──────────────────────────────────────────────────
        st.subheader("Raw Values")
        raw_rows = []
        for h_days in all_horizons:
            row  = by_horizon[h_days]
            hlbl = horizon_labels.get(h_days, f"{h_days}d")
            n    = row.get("num_predictions") or 0
            raw_rows.append({
                "Horizon":           hlbl,
                "Evaluated Preds":   n,
                "Dir Accuracy":   fmt_pct(row["directional_accuracy"]*100) if row.get("directional_accuracy") is not None else "—",
                "In-Range %":     fmt_pct(row["in_range_pct"]*100)         if row.get("in_range_pct")          is not None else "—",
                "Excess Return":  fmt_pct(row["mean_excess_return"]*100, signed=True)  if row.get("mean_excess_return")     is not None else "—",
                "Hi-Conv Acc":    fmt_pct(row["high_conviction_accuracy"]*100) if row.get("high_conviction_accuracy") is not None else "—",
                "Lo-Conv Acc":    fmt_pct(row["low_conviction_accuracy"]*100)  if row.get("low_conviction_accuracy")  is not None else "—",
                "Brier Score":    f"{row['brier_score']:.2f}"                if row.get("brier_score")            is not None else "—",
                "Log-Loss":       f"{row['mean_log_loss']:.2f}"              if row.get("mean_log_loss")           is not None else "—",
            })
        st.dataframe(
            pd.DataFrame(raw_rows),
            hide_index=True,
            use_container_width=True,
            column_config={
                "Horizon":         st.column_config.Column(help="Prediction horizon — how many trading days ahead this call was for."),
                "Evaluated Preds": st.column_config.Column(help="Number of matured, scored predictions at this horizon over the selected lookback period."),
                "Dir Accuracy":    st.column_config.Column(help=_METRIC_CFG["directional_accuracy"]["defn"]),
                "In-Range %":      st.column_config.Column(help=_METRIC_CFG["in_range_pct"]["defn"]),
                "Excess Return":   st.column_config.Column(help=_METRIC_CFG["mean_excess_return"]["defn"]),
                "Hi-Conv Acc":     st.column_config.Column(help=_METRIC_CFG["high_conviction_accuracy"]["defn"]),
                "Lo-Conv Acc":     st.column_config.Column(help=_METRIC_CFG["low_conviction_accuracy"]["defn"]),
                "Brier Score":     st.column_config.Column(help=_METRIC_CFG["brier_score"]["defn"]),
                "Log-Loss":        st.column_config.Column(help=_METRIC_CFG["mean_log_loss"]["defn"]),
            },
        )

# ── Tab 2: Prediction History — promoted right after Scorecard; once scoped
# to "My Tickers" by default, this is every call made on tickers the viewer
# actually cares about, and how each one turned out ───────────────────────────

with tabs[1]:
    from portfolio_agent.tools.validation_engine import get_recent_evaluated_predictions

    preds = get_recent_evaluated_predictions(limit=500, lookback_days=lookback)
    # Apply global model filter
    if _model_filter and preds:
        preds = [p for p in preds if p.get("model_name") in _model_filter]
    # Apply segment filter
    _my_set = set(_portfolio_tickers) | set(_watchlist_tickers)
    if _seg_sel == "My Tickers" and _my_set and preds:
        preds = [p for p in preds if p.get("ticker", "").upper() in _my_set]
    elif _seg_sel == "Portfolio" and _portfolio_tickers and preds:
        _pt_set = set(_portfolio_tickers)
        preds = [p for p in preds if p.get("ticker", "").upper() in _pt_set]
    elif _seg_sel == "Watchlist" and _watchlist_tickers and preds:
        _wt_set = set(_watchlist_tickers)
        preds = [p for p in preds if p.get("ticker", "").upper() in _wt_set]
    elif _seg_sel == "New Opportunities" and preds:
        preds = [p for p in preds if p.get("trigger_type") == "trending_opportunity"]
    if not preds:
        _no_pred_msg = "No evaluated predictions yet."
        if _model_filter:
            _no_pred_msg = f"No evaluated predictions for model(s): {', '.join(_model_filter)}"
        if _seg_sel != "All":
            _no_pred_msg += f" (scope: {_seg_sel})"
        st.info(_no_pred_msg, icon=material("info"))
    else:
        f1, f2 = st.columns(2)
        with f1:
            h_opts = sorted({p["horizon_days"] for p in preds if p.get("horizon_days")})
            h_filter = st.multiselect(
                "Horizon", h_opts, default=[],
                format_func=lambda x: f"{x}d", key="pred_tab_h",
                placeholder="All horizons — pick to narrow",
            )
        with f2:
            o_opts = sorted({p["outcome"] for p in preds if p.get("outcome")})
            o_filter = st.multiselect(
                "Outcome", o_opts, default=[],
                format_func=lambda o: OUTCOME_LABELS.get(o, o), key="pred_tab_o",
                placeholder="All outcomes — pick to narrow",
            )

        filtered = sorted(
            [p for p in preds
             if (not h_filter or p.get("horizon_days") in h_filter)
             and (not o_filter or p.get("outcome") in o_filter)],
            key=lambda p: p.get("as_of_date") or p.get("prediction_date") or "",
            reverse=True,
        )

        rows = []
        for p in filtered:
            ar    = p.get("actual_return")        # decimal  e.g. -0.0063
            rlo   = p.get("predicted_return_low")  # percent  e.g. 1.5
            rhi   = p.get("predicted_return_high") # percent  e.g. 4.0
            sp    = p.get("start_price")           # dollars
            oc    = p.get("outcome") or p.get("evaluation_status") or "—"
            er    = p.get("excess_return")

            # Return % strings
            rng_pct = f"{fmt_pct(rlo, signed=True)} – {fmt_pct(rhi, signed=True)}" if rlo is not None and rhi is not None else "—"
            act_pct = fmt_pct(ar*100, signed=True) if ar is not None else "—"

            # Dollar price strings (only when start_price available)
            if sp and sp > 0:
                if rlo is not None and rhi is not None:
                    p_lo = sp * (1 + rlo / 100)
                    p_hi = sp * (1 + rhi / 100)
                    rng_price = f"{fmt_money(p_lo)} – {fmt_money(p_hi)}"
                else:
                    rng_price = "—"
                act_price = fmt_money(sp * (1 + ar)) if ar is not None else "—"
                entry_price = fmt_money(sp)
            else:
                rng_price = "—"
                act_price = "—"
                entry_price = "—"

            dist_parts = []
            for col, label in [("p_strong_down","▼▼"),("p_moderate_down","▼"),
                                ("p_flat","→"),("p_moderate_up","▲"),("p_strong_up","▲▲")]:
                v = p.get(col)
                if v is not None:
                    dist_parts.append(f"{label}{int(v)}%")

            rows.append({
                "Date":            p.get("as_of_date") or p.get("prediction_date") or "",
                "Ticker":          p.get("ticker") or "",
                "Horizon":         f"{p['horizon_days']}d" if p.get("horizon_days") else "—",
                "Predicted":       p.get("predicted_direction") or "—",
                "Entry Price":     entry_price,
                "Pred Range %":    rng_pct,
                "Pred Price Range":rng_price,
                "Actual %":        act_pct,
                "Actual Price":    act_price,
                "Distribution":    " ".join(dist_parts) if dist_parts else "—",
                "Bucket":          p.get("actual_bucket") or "—",
                "Outcome":         oc.replace("_", " ").title() if isinstance(oc, str) else oc,
                "Conviction":      p.get("conviction_score"),
                "Excess vs SPY":   fmt_pct(er*100, signed=True) if er is not None else "—",
                "Segment":         p.get("risk_segment") or "unknown",
            })

        st.dataframe(
            pd.DataFrame(rows),
            use_container_width=True,
            hide_index=True,
            column_config={
                "Date":             st.column_config.Column(help="Date the prediction was made."),
                "Ticker":           st.column_config.Column(help="Stock ticker symbol."),
                "Horizon":          st.column_config.Column(help="How many trading days ahead this call was for."),
                "Predicted":        st.column_config.Column(help="Predicted direction: UP, DOWN, or FLAT."),
                "Entry Price":      st.column_config.Column(help="Price at the time the prediction was made."),
                "Pred Range %":     st.column_config.Column(help="Predicted return range (low – high) over the horizon."),
                "Pred Price Range": st.column_config.Column(help="Predicted return range converted to a dollar price window off the entry price."),
                "Actual %":         st.column_config.Column(help="Actual realized return over the horizon."),
                "Actual Price":     st.column_config.Column(help="Realized price at the end of the horizon."),
                "Distribution":     st.column_config.Column(help="Model's stated probability across 5 return buckets: ▼▼ strong down · ▼ moderate down · → flat · ▲ moderate up · ▲▲ strong up."),
                "Bucket":           st.column_config.Column(help="Which of the 5 return buckets the actual return landed in."),
                "Outcome":          st.column_config.Column(help="Whether the call was scored correct or wrong, and by how much (e.g. directionally correct vs. wrong significant)."),
                "Conviction":       st.column_config.Column(help="Model's self-reported confidence in this call, 1 (low) – 10 (high)."),
                "Excess vs SPY":    st.column_config.Column(help="Actual return minus the S&P 500's return over the same window. Positive = beat the market."),
                "Segment":          st.column_config.Column(help="Risk segment this ticker was classified under at prediction time."),
            },
        )
        st.caption(
            f"{len(filtered)} of {len(preds)} evaluated predictions shown.  "
            "Entry Price = price at prediction date · Pred Price Range = estimated price window · "
            "Actual Price = realized price at horizon.  "
            "Distribution: ▼▼=strong_down ▼=moderate_down →=flat ▲=moderate_up ▲▲=strong_up"
        )

# ── Tab 3: Score vs Returns — renamed from "Score Calibration"; the
# correlation table became the top-vs-bottom-quintile return chart, since
# that's the plain-English version of the same underlying calibration_analysis
# computation. Kept user-facing: this directly answers "should I trust a
# high score," unlike the full reliability-diagram Calibration tab (moved to
# Validation QA as "Confidence Calibration" to avoid name collision) ─────────

with tabs[2]:
    from portfolio_agent.tools.scoring_snapshot_db import get_latest_snapshot_date, get_snapshots_for_date
    from portfolio_agent.tools.calibration_analysis import compute_calibration_report, SCORE_LABELS

    st.subheader(f"{material('calculate')} Today's Scoring Table")
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

    st.divider()

    st.subheader(f"{material('bar_chart')} Score vs Returns")
    st.caption(
        "For each scoring system: the average realized return of the top-20% vs. bottom-20% "
        "scored names at this horizon — did the names we scored highly actually do better?"
    )
    horizon_choice = st.radio(
        "Horizon", [30, 60, 90, 250], format_func=lambda h: f"{h}d", horizontal=True, key="calib_horizon",
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

# ── Tab 4: Heatmap — simplified to horizon-only. Segment × horizon detail
# moved conceptually to Validation QA territory (not wired there either,
# since no one asked for it back — the underlying accuracy-heatmap functions
# stay in the codebase, just unused, so it can come back easily if needed) ───

with tabs[3]:
    st.subheader("Which Horizon Is More Reliable?")
    st.caption(
        "Directional accuracy by prediction horizon — useful for deciding how much to weight "
        "a 5-day call vs. a 63-day one. Segment-level detail lives on Validation QA."
    )
    if not metrics:
        st.info(
            f"No evaluated predictions yet. Run {material('nights_stay')} **Evening** on the "
            f"{material('calendar_month')} Schedule page to generate this view.",
            icon=material("info"),
        )
    else:
        st.plotly_chart(
            build_horizon_reliability_chart(by_horizon, all_horizons, horizon_labels),
            use_container_width=True,
        )
