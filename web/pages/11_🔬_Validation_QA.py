"""
Validation QA — operator-only diagnostics for the prediction engine itself.

Moved off the user-facing Validation page: Metric Trends (raw Brier/log-loss
trend lines), Confidence Calibration (reliability diagram + conviction
calibration — renamed from "Calibration" to avoid collision with the
user-facing "Score vs Returns" tab), Post-mortems (weekly LLM failure-pattern
analysis), and Versions (system-version A/B comparison). All four are
engineering QA tools for tuning the shared prediction engine, not something a
friend deciding whether to trust a BUY call needs to see — hence living behind
the admin drawer instead of on the main Validation page.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

import pandas as pd
import streamlit as st
from web.styles import inject_global_css, top_nav, icon_html, material, fmt_pct

st.set_page_config(
    page_title="Validation QA — Portfolio Intelligence",
    page_icon=material("science"),
    layout="wide",
    initial_sidebar_state="expanded",
)
inject_global_css()
top_nav("validation_qa")

from portfolio_agent.tools.validation_engine import (
    get_calibration_data,
    get_volume_by_horizon,
    get_system_version_comparison,
)
from portfolio_agent.tools.prediction_db import get_validation_reports, CURRENT_SYSTEM_VERSION
from web.data.validation import (
    _load_metric_series,
    _load_portfolio_tickers,
    _load_watchlist_tickers,
    _get_model_names,
    _load_rolling_metrics_series,
)
from web.components.validation_charts import (
    build_brier_trend_chart,
    build_logloss_trend_chart,
    build_directional_accuracy_chart,
    build_predicted_vs_actual_scatter,
    build_reliability_diagram,
    build_conviction_calibration_chart,
    build_brier_histogram,
    build_logloss_histogram,
    build_volume_by_horizon_chart,
)

# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown(
        '<p style="font-size:0.7rem;font-weight:700;text-transform:uppercase;'
        'letter-spacing:0.08em;color:#475569;margin:0 0 10px">Filters</p>',
        unsafe_allow_html=True,
    )
    st.caption(f"Operator-only diagnostics. See {material('track_changes')} Validation for the user-facing page.")

    st.divider()
    lookback = st.selectbox(
        "Lookback period",
        [30, 90, 365],
        index=1,
        format_func=lambda x: f"{x} days",
    )

    st.divider()

    st.markdown(
        '<p style="font-size:0.72rem;font-weight:600;color:#9CA3AF;margin:0 0 8px">'
        'Model filter</p>',
        unsafe_allow_html=True,
    )

    def _is_higher_reasoning(model_name: str) -> bool:
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
        "model_filter", _radio_options, index=0,
        label_visibility="collapsed", key="valqa_model_radio",
    )
    _GROUP_NAMES = {"Higher reasoning": _higher_names, "Lower reasoning": _lower_names}

    if _model_sel == "All models":
        _model_filter: list[str] | None = None
        st.caption("All predictions — no model filter applied")
    else:
        _model_filter = _GROUP_NAMES[_model_sel]
        _icon_name = "psychology" if _model_sel == "Higher reasoning" else "bolt"
        st.caption(f"{material(_icon_name)} {_model_sel}")

    st.divider()

    st.markdown(
        '<p style="font-size:0.72rem;font-weight:600;color:#9CA3AF;margin:0 0 8px">'
        'Prediction segment</p>',
        unsafe_allow_html=True,
    )
    _seg_sel = st.radio(
        "segment",
        ["All", "Portfolio", "Watchlist", "New Opportunities"],
        index=0,
        label_visibility="collapsed",
        key="valqa_segment",
        help=(
            "All: every prediction  ·  "
            "Portfolio: holdings only  ·  "
            "Watchlist: tracked but not held  ·  "
            "New Opportunities: trending tickers discovered by the scanner"
        ),
    )

_portfolio_tickers = _load_portfolio_tickers()
_watchlist_tickers = _load_watchlist_tickers()

# ── Page header ───────────────────────────────────────────────────────────────

st.title(f"{material('science')} Validation QA")
st.caption(
    f"System version: `{CURRENT_SYSTEM_VERSION}` · Engineering diagnostics for tuning the prediction engine — "
    "not intended for end users."
)

tabs = st.tabs([
    f"{material('trending_up')} Metric Trends",
    f"{material('track_changes')} Confidence Calibration",
    f"{material('science')} Post-mortems",
    f"{material('history')} Versions",
])

# ── Tab 1: Metric Trends ──────────────────────────────────────────────────────

with tabs[0]:
    st.subheader(f"{material('trending_up')} Prediction Quality Metrics Over Time")
    st.caption(
        "Each data point is one evaluated prediction. Rolling average smooths the trend. "
        f"Populates after {material('nights_stay')} **Evening** ({material('calendar_month')} "
        "Schedule page) scores matured predictions."
    )

    metric_df  = _load_metric_series(_seg_sel, _portfolio_tickers, _watchlist_tickers)
    rolling_df = _load_rolling_metrics_series(_seg_sel)

    if _model_filter and not metric_df.empty and "model_name" in metric_df.columns:
        metric_df = metric_df[metric_df["model_name"].isin(_model_filter)]
    if _model_filter and not rolling_df.empty:
        st.caption(f"{material('info')} Aggregate rolling metrics (dashed lines) are not model-filtered — they reflect all models.")

    H_COLORS = {5: "#2563EB", 21: "#7C3AED", 63: "#059669"}
    H_LABELS = {5: "5-Day", 21: "21-Day", 63: "63-Day"}

    if metric_df.empty:
        st.info(
            "No evaluated predictions with Brier/log-loss values yet.\n\n"
            "Predictions become evaluable once their horizon has elapsed "
            "(e.g., a 5-day prediction from Monday is scored the following Monday). "
            f"Click {material('nights_stay')} **Evening** on the {material('calendar_month')} "
            "Schedule page to score any matured predictions.",
            icon=material("bar_chart"),
        )
    else:
        n_eval = len(metric_df)
        horizons_present = sorted(metric_df["horizon_days"].dropna().unique())

        m1, m2 = st.columns(2)
        with m1:
            horizon_sel = st.multiselect(
                "Horizon filter",
                options=horizons_present,
                default=list(horizons_present),
                format_func=lambda h: H_LABELS.get(int(h), f"{int(h)}d"),
                key="valqa_trend_horizon",
            )
        with m2:
            roll_window = st.select_slider(
                "Rolling average window",
                options=[5, 10, 20],
                value=10,
                key="valqa_trend_roll",
            )

        d1, d2 = st.columns(2)
        with d1:
            show_points = st.checkbox(
                "Show individual predictions", value=False, key="valqa_trend_show_points",
                help="Raw per-prediction dots behind the rolling average — adds one layer of detail, and clutter.",
            )
        with d2:
            show_aggregate = st.checkbox(
                "Show unfiltered aggregate overlay", value=False, key="valqa_trend_show_aggregate",
                help="A second line from the nightly metrics_rolling table, computed across ALL models "
                     "regardless of the model filter above.",
            )

        fdf = metric_df[metric_df["horizon_days"].isin(horizon_sel)] if horizon_sel else metric_df
        st.caption(f"Showing {len(fdf)} of {n_eval} evaluated predictions")

        st.subheader("Brier Score — Rolling Average")
        st.caption(
            "Lower is better. Baseline (uniform guesser) ≈ 0.32. "
            "Below 0.20 = mild edge. Below 0.18 = genuine skill."
        )
        fig_brier = build_brier_trend_chart(fdf, horizons_present, horizon_sel, roll_window, rolling_df,
                                             H_COLORS, H_LABELS, show_points=show_points, show_aggregate=show_aggregate)
        st.plotly_chart(fig_brier, use_container_width=True)

        st.divider()

        st.subheader("Log-Loss — Rolling Average")
        st.caption(
            "Lower is better. Baseline (coin flip / uniform) ≈ 0.693. "
            "Below 0.5 = model provides useful probability estimates."
        )
        fig_ll = build_logloss_trend_chart(fdf, horizons_present, horizon_sel, roll_window, rolling_df,
                                            H_COLORS, H_LABELS, show_points=show_points, show_aggregate=show_aggregate)
        st.plotly_chart(fig_ll, use_container_width=True)

        st.divider()

        st.subheader("Directional Accuracy — Rolling Average")
        st.caption("Rolling % of predictions where predicted direction matched actual direction. 50% = random baseline.")
        fig_dir = build_directional_accuracy_chart(fdf, horizons_present, horizon_sel, roll_window, rolling_df,
                                                     H_COLORS, H_LABELS, show_aggregate=show_aggregate)
        st.plotly_chart(fig_dir, use_container_width=True)

        st.divider()

        st.subheader("Predicted Return Midpoint vs Actual Return")
        st.caption(
            "X = midpoint of predicted return range. Y = actual return. "
            "Points on the diagonal = perfect predictions. Color = prediction outcome."
        )
        scatter_df = fdf.dropna(subset=["actual_return", "predicted_return_low", "predicted_return_high"]).copy()
        if scatter_df.empty:
            st.info("No predictions with both predicted range and actual return yet.", icon=material("trending_down"))
        else:
            fig_scatter = build_predicted_vs_actual_scatter(scatter_df)
            st.plotly_chart(fig_scatter, use_container_width=True)
            st.caption("Marker size = conviction score (larger = higher conviction).")

        st.divider()

        st.subheader("Prediction Volume by Horizon")
        vol_data = get_volume_by_horizon(lookback_days=lookback)
        if not vol_data:
            st.info("No volume data yet.")
        else:
            fig_vol = build_volume_by_horizon_chart(vol_data)
            st.plotly_chart(fig_vol, use_container_width=True)

# ── Tab 2: Confidence Calibration ─────────────────────────────────────────────

with tabs[1]:
    _cal_raw = get_calibration_data(lookback_days=lookback, model_names=_model_filter)
    if isinstance(_cal_raw, list):
        _cal_raw = {"conviction": _cal_raw, "summary": {}, "reliability": [], "bucket_table": []}
    cal            = _cal_raw
    summary        = cal.get("summary", {})
    reliability    = cal.get("reliability", [])
    bucket_table   = cal.get("bucket_table", [])
    conviction_cal = cal.get("conviction", [])

    n_total = summary.get("n", 0)

    if n_total == 0:
        st.info(
            "No probability-distribution predictions evaluated yet. "
            "Once predictions with the 5-bucket distribution are scored, "
            "this tab will populate with the full reliability diagram.",
            icon=material("info"),
        )
    else:
        if n_total < 30:
            st.warning(f"Only {n_total} evaluated predictions — metrics completely unreliable.", icon=material("warning"))
        elif n_total < 100:
            st.warning(f"{n_total} predictions — rough direction visible only.", icon=material("warning"))
        elif n_total < 300:
            st.info(f"{n_total} predictions — overall Brier/log-loss meaningful; calibration bins sparse.", icon=material("info"))
        else:
            st.success(f"{n_total} predictions — calibration is interpretable.", icon=material("check_circle"))

    st.markdown(f"""
<div style="background:#F8FAFC;border:1px solid #E2E8F0;border-radius:10px;padding:14px 18px;margin:12px 0">
<strong style="color:#0F172A;font-size:0.88rem">{icon_html('calculate', 16, color='#0F172A')} Binary Brier score — p_up vs actual up/not-up</strong>
<div style="display:grid;grid-template-columns:repeat(5,1fr);gap:5px;margin-top:8px">
  <div style="background:#D1FAE5;border-radius:6px;padding:7px;text-align:center">
    <div style="font-weight:700;color:#065F46;font-size:0.82rem">&lt; 0.15</div>
    <div style="font-size:0.72rem;color:#047857">Exceptional</div>
  </div>
  <div style="background:#DCFCE7;border-radius:6px;padding:7px;text-align:center">
    <div style="font-weight:700;color:#166534;font-size:0.82rem">0.15–0.18</div>
    <div style="font-size:0.72rem;color:#15803D">Genuine skill</div>
  </div>
  <div style="background:#FEF3C7;border-radius:6px;padding:7px;text-align:center">
    <div style="font-weight:700;color:#92400E;font-size:0.82rem">0.18–0.20</div>
    <div style="font-size:0.72rem;color:#B45309">Mild edge</div>
  </div>
  <div style="background:#FEE2E2;border-radius:6px;padding:7px;text-align:center">
    <div style="font-weight:700;color:#991B1B;font-size:0.82rem">0.20–0.25</div>
    <div style="font-size:0.72rem;color:#DC2626">Barely beats coin flip</div>
  </div>
  <div style="background:#FCA5A5;border-radius:6px;padding:7px;text-align:center">
    <div style="font-weight:700;color:#7F1D1D;font-size:0.82rem">&gt; 0.25</div>
    <div style="font-size:0.72rem;color:#991B1B">Actively bad</div>
  </div>
</div>
<p style="margin:6px 0 0;font-size:0.76rem;color:#94A3B8">
perfect = 0.0 · coin flip = 0.25 · worst = 1.0
</p>
</div>
""", unsafe_allow_html=True)

    st.divider()
    st.subheader("Reliability Diagram")
    st.caption(
        "Stated p_up confidence (x-axis) vs actual 'up' hit rate (y-axis). "
        "Points on the diagonal = perfectly calibrated. "
        "**Below** diagonal = overconfident. **Above** = underconfident."
    )
    if not reliability:
        st.info("No reliability data yet — needs evaluated predictions with distribution.", icon=material("info"))
    else:
        fig = build_reliability_diagram(reliability)
        st.plotly_chart(fig, use_container_width=True)

    st.subheader("Prediction Count per Confidence Bucket")
    st.caption("Sample size per bin is critical — thin bins (< 30) are unreliable.")
    st.dataframe(pd.DataFrame(bucket_table), use_container_width=True, hide_index=True)
    st.caption("Per-horizon Brier/log-loss breakdown lives on the user-facing Validation page's Scorecard tab.")

    st.divider()
    st.subheader("Conviction Score vs Directional Accuracy")
    st.caption("Conviction (1–10) vs whether the predicted direction was correct.")
    if len(conviction_cal) < 2:
        st.info("Insufficient data — needs predictions across at least 2 conviction buckets.")
    else:
        fig2 = build_conviction_calibration_chart(conviction_cal)
        st.plotly_chart(fig2, use_container_width=True)

    # ── Brier + Log-Loss distribution — moved here from the user-facing
    # Prediction History tab, since it's the same probability-calibration
    # detail this tab already covers, not something a friend needs to see ──
    from portfolio_agent.tools.validation_engine import get_recent_evaluated_predictions
    _hist_preds = get_recent_evaluated_predictions(limit=1000, lookback_days=lookback)
    if _model_filter:
        _hist_preds = [p for p in _hist_preds if p.get("model_name") in _model_filter]
    b_vals  = [p["brier_score"] for p in _hist_preds if p.get("brier_score") is not None]
    ll_vals = [p["log_loss"] for p in _hist_preds if p.get("log_loss") is not None]
    if b_vals or ll_vals:
        st.divider()
        st.subheader("Distribution of Brier Score & Log-Loss (evaluated set)")
        hc1, hc2 = st.columns(2)
        with hc1:
            if b_vals:
                st.plotly_chart(build_brier_histogram(b_vals), use_container_width=True)
        with hc2:
            if ll_vals:
                st.plotly_chart(build_logloss_histogram(ll_vals), use_container_width=True)

# ── Tab 3: Post-mortems ───────────────────────────────────────────────────────

with tabs[2]:
    reports = get_validation_reports(limit=5)
    if not reports:
        st.info(
            f"No post-mortem reports yet. Click {material('science')} **Weekly Analysis** on the "
            f"{material('calendar_month')} Schedule page."
        )
    else:
        for report in reports:
            period = (report.get("period") or "weekly").capitalize()
            st.subheader(f"{period} — {report['report_date']}")
            if report.get("llm_summary"):
                st.write(report["llm_summary"])
            pi   = report.get("patterns_identified") or {}
            pats = pi.get("patterns", [])
            sugs = pi.get("suggestions", [])
            if pats:
                st.write("**Patterns detected:**")
                for pat in pats:
                    st.write(f"- {pat}")
            if sugs:
                st.write("**Suggested improvements:**")
                for sug in sugs:
                    st.write(f"- {sug}")
            snap = report.get("metrics_snapshot") or {}
            if snap.get("wrong_count"):
                st.caption(f"Analyzed {snap['wrong_count']} wrong predictions.")
            st.divider()

# ── Tab 4: Version Comparison ─────────────────────────────────────────────────

with tabs[3]:
    ver_data = get_system_version_comparison(lookback_days=lookback, model_names=_model_filter)
    if not ver_data:
        st.info("No version comparison data yet — needs evaluated predictions.")
    else:
        unique_ver = {r["sys_ver"] for r in ver_data}
        if len(unique_ver) <= 1:
            st.info("Only one system version in history. "
                    "Comparison becomes available after prompt or model changes are tagged.")
            st.subheader(f"Current baseline — version {next(iter(unique_ver), CURRENT_SYSTEM_VERSION)}")

        rows_ver = [{
            "Version":            r.get("sys_ver") or "v1.0",
            "Horizon":            f"{r.get('horizon_days')}d" if r.get("horizon_days") else "—",
            "N":                  r.get("n"),
            "Directional Acc.":   fmt_pct(r["dir_acc"]*100) if r.get("dir_acc") is not None else "—",
            "Mean Excess Return": fmt_pct(r["mean_excess"]*100) if r.get("mean_excess") is not None else "—",
        } for r in ver_data]
        st.dataframe(pd.DataFrame(rows_ver), use_container_width=True, hide_index=True)
