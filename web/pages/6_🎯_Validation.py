"""
APEX Prediction Validation Dashboard — system health panel with metric trend charts.
"""
from __future__ import annotations

import math as _math
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

import pandas as pd
import streamlit as st
from web.styles import inject_global_css, top_nav

st.set_page_config(
    page_title="Validation — Portfolio Intelligence",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="expanded",
)
inject_global_css()
top_nav("validation")

from portfolio_agent.tools.validation_engine import (
    get_recent_evaluated_predictions,
    get_accuracy_heatmap_data,
    get_calibration_data,
    get_drift_data,
    get_volume_by_horizon,
    get_system_version_comparison,
    weekly_pattern_analysis,
)
from portfolio_agent.tools.prediction_db import (
    get_rolling_metrics,
    get_validation_reports,
    CURRENT_SYSTEM_VERSION,
)
from web.lib import pid_alive as _pid_alive
from web.data.validation import (
    _load_metric_series,
    _load_portfolio_tickers,
    _get_model_names,
    _compute_filtered_metrics,
    _load_rolling_metrics_series,
    _directional_correct,
    _start_subprocess,
    _read_log,
)
from web.components.validation_charts import (
    _brier_tier,
    build_brier_trend_chart,
    build_logloss_trend_chart,
    build_directional_accuracy_chart,
    build_predicted_vs_actual_scatter,
    build_accuracy_heatmap,
    build_reliability_diagram,
    build_conviction_calibration_chart,
    build_brier_histogram,
    build_logloss_histogram,
    build_drift_accuracy_chart,
    build_volume_by_horizon_chart,
)
from web.components.validation_cards import _render_scorecard_tiles

# ── Session state ─────────────────────────────────────────────────────────────

for _k, _v in [
    ("val_pid", None), ("val_log", None),
    ("val_weekly_pid", None), ("val_weekly_log", None),
]:
    if _k not in st.session_state:
        st.session_state[_k] = _v


# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown(
        '<p style="font-size:0.7rem;font-weight:700;text-transform:uppercase;'
        'letter-spacing:0.08em;color:#475569;margin:0 0 10px">Validation Controls</p>',
        unsafe_allow_html=True,
    )

    eval_running = _pid_alive(st.session_state.val_pid)
    if eval_running:
        st.warning("⏳ Evaluation running…")
        if st.button("↺ Refresh status", use_container_width=True):
            st.rerun()
    else:
        if st.button("▶ Run Evaluation", use_container_width=True, type="primary"):
            pid, log_path = _start_subprocess("--validate", "validation")
            st.session_state.val_pid = pid
            st.session_state.val_log = str(log_path)
            st.rerun()

    st.divider()

    weekly_running = _pid_alive(st.session_state.val_weekly_pid)
    if weekly_running:
        st.warning("⏳ Weekly analysis running…")
    else:
        if st.button("🔬 Run Weekly Analysis (LLM)", use_container_width=True):
            pid, log_path = _start_subprocess("--weekly-analysis", "validation_weekly")
            st.session_state.val_weekly_pid = pid
            st.session_state.val_weekly_log = str(log_path)
            st.rerun()

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
        icon = "🧠" if _model_sel == "Higher reasoning" else "⚡"
        st.caption(f"{icon} {_model_sel} (GPT-4x / Claude)" if _model_sel == "Higher reasoning" else f"{icon} {_model_sel}")
        # Show individual names so the user knows what's included in the group
        st.markdown(
            '<div style="margin-top:4px">' +
            "".join(
                f'<div style="font-size:0.68rem;color:#6B7280;padding:1px 0">{icon} {m}</div>'
                for m in _model_filter
            ) + '</div>',
            unsafe_allow_html=True,
        )

    st.divider()

    # ── Prediction segment ───────────────────────────────────────────────────
    st.markdown(
        '<p style="font-size:0.72rem;font-weight:600;color:#9CA3AF;margin:0 0 8px">'
        'Prediction segment</p>',
        unsafe_allow_html=True,
    )
    _seg_sel = st.radio(
        "segment",
        ["All", "Portfolio", "New Opportunities"],
        index=0,
        label_visibility="collapsed",
        key="val_segment",
        help=(
            "All: every prediction  ·  "
            "Portfolio: your holdings only  ·  "
            "New Opportunities: trending tickers discovered by the scanner"
        ),
    )

# Load portfolio tickers once (used by segment filter throughout)
_portfolio_tickers = _load_portfolio_tickers()

# ── Page header ───────────────────────────────────────────────────────────────

st.title("🎯 APEX Prediction Validation")
st.caption(f"System version: `{CURRENT_SYSTEM_VERSION}` · Scores predictions after they mature")

_active_filters: list[str] = []
if _model_filter:
    _active_filters.append(f"Model: {', '.join(f'`{m}`' for m in _model_filter)}")
if _seg_sel != "All":
    _seg_icon = "💼" if _seg_sel == "Portfolio" else "🌟"
    _active_filters.append(f"Segment: **{_seg_icon} {_seg_sel}**")
if _active_filters:
    st.info(
        "🔍 **Active filters** — " + "  ·  ".join(_active_filters) +
        "  ·  All metrics and tables recalculated for selected filter(s).",
        icon="🔍",
    )

# ── Live log panel ────────────────────────────────────────────────────────────

eval_running   = _pid_alive(st.session_state.val_pid)
weekly_running = _pid_alive(st.session_state.val_weekly_pid)

_active_log, _active_label = None, ""
if eval_running and st.session_state.val_log:
    _active_log, _active_label = st.session_state.val_log, "Evaluation"
elif weekly_running and st.session_state.get("val_weekly_log"):
    _active_log, _active_label = st.session_state.val_weekly_log, "Weekly Analysis"
elif st.session_state.val_log:
    _active_log, _active_label = st.session_state.val_log, "Last Evaluation"
elif st.session_state.get("val_weekly_log"):
    _active_log, _active_label = st.session_state.val_weekly_log, "Last Weekly Analysis"

if _active_log:
    log_text = _read_log(_active_log)
    has_err  = any(k in log_text.lower() for k in ("[error]", "traceback", "exception:"))
    is_done  = (
        "validation complete" in log_text.lower()
        or "weekly analysis complete" in log_text.lower()
    )
    if is_done:
        st.session_state.val_pid        = None
        st.session_state.val_weekly_pid = None
        eval_running   = False
        weekly_running = False

    is_running = eval_running or weekly_running
    if is_running:
        st.info(f"⏳ {_active_label} in progress — logs updating live…")
    elif has_err:
        st.error(f"❌ {_active_label} completed with errors")
    elif is_done:
        st.success(f"✅ {_active_label} complete")

    with st.expander(f"📋 {_active_label} Log", expanded=is_running):
        st.code(log_text or "(no output yet…)", language=None)

    if is_running:
        time.sleep(2)
        st.rerun()

    st.divider()

# ── Tabs ──────────────────────────────────────────────────────────────────────

tabs = st.tabs([
    "📊 Scorecard",
    "📈 Metric Trends",
    "🗺️ Heatmap",
    "🎯 Calibration",
    "📋 Predictions",
    "🔬 Post-mortems",
    "📉 Drift",
    "🔄 Versions",
])

# ── Tab 1: Scorecard ──────────────────────────────────────────────────────────

with tabs[0]:
    metrics = _compute_filtered_metrics(_model_filter, lookback, _seg_sel, _portfolio_tickers)
    if not metrics:
        st.info(
            "No evaluated predictions yet. "
            "Click **▶ Run Evaluation** in the sidebar to score matured predictions.",
            icon="ℹ️",
        )
    else:
        by_horizon: dict[int, dict] = {}
        for row in metrics:
            h = row["horizon_days"]
            if h not in by_horizon:
                by_horizon[h] = row

        horizon_labels = {5: "5-Day", 21: "21-Day", 63: "63-Day"}
        all_horizons = [h for h in [5, 21, 63] if h in by_horizon] + \
                       [h for h in sorted(by_horizon) if h not in [5, 21, 63]]

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
                "Dir Accuracy":   f"{row['directional_accuracy']*100:.1f}%" if row.get("directional_accuracy") is not None else "—",
                "In-Range %":     f"{row['in_range_pct']*100:.1f}%"         if row.get("in_range_pct")          is not None else "—",
                "Excess Return":  f"{row['mean_excess_return']*100:+.2f}%"  if row.get("mean_excess_return")     is not None else "—",
                "Hi-Conv Acc":    f"{row['high_conviction_accuracy']*100:.1f}%" if row.get("high_conviction_accuracy") is not None else "—",
                "Lo-Conv Acc":    f"{row['low_conviction_accuracy']*100:.1f}%"  if row.get("low_conviction_accuracy")  is not None else "—",
                "Brier Score":    f"{row['brier_score']:.4f}"                if row.get("brier_score")            is not None else "—",
                "Log-Loss":       f"{row['mean_log_loss']:.4f}"              if row.get("mean_log_loss")           is not None else "—",
            })
        st.dataframe(pd.DataFrame(raw_rows), hide_index=True, use_container_width=True)

# ── Tab 2: Metric Trends ──────────────────────────────────────────────────────

with tabs[1]:
    st.subheader("📈 Prediction Quality Metrics Over Time")
    st.caption(
        "Each data point is one evaluated prediction. Rolling average smooths the trend. "
        "Populates after **▶ Run Evaluation** scores matured predictions."
    )

    metric_df  = _load_metric_series(_seg_sel, _portfolio_tickers)
    rolling_df = _load_rolling_metrics_series()

    # Apply model filter to per-prediction series
    if _model_filter and not metric_df.empty and "model_name" in metric_df.columns:
        metric_df = metric_df[metric_df["model_name"].isin(_model_filter)]
    # rolling_df comes from metrics_rolling which has no model field — show note
    if _model_filter and not rolling_df.empty:
        st.caption("ℹ️ Aggregate rolling metrics (dashed lines) are not model-filtered — they reflect all models.")

    H_COLORS = {5: "#2563EB", 21: "#7C3AED", 63: "#059669"}
    H_LABELS = {5: "5-Day", 21: "21-Day", 63: "63-Day"}

    if metric_df.empty:
        st.info(
            "No evaluated predictions with Brier/log-loss values yet.\n\n"
            "Predictions become evaluable once their horizon has elapsed "
            "(e.g., a 5-day prediction from Monday is scored the following Monday). "
            "Click **▶ Run Evaluation** in the sidebar to score any matured predictions.",
            icon="📊",
        )
        with st.expander("What these charts will show once data is available", expanded=True):
            st.markdown("""
| Chart | What it measures |
|---|---|
| **Brier Score trend** | Per-prediction probability accuracy (0 = perfect, 0.25 = coin flip). Plots each evaluated prediction's brier_score over time with a rolling 10-prediction average per horizon. |
| **Log-Loss trend** | Information-theoretic accuracy. Lower = better calibrated probability estimates. Baseline (coin flip) ≈ 0.693. |
| **Directional accuracy** | Rolling % of predictions where the predicted direction (UP/DOWN/FLAT) matched the actual direction. 50% = random. |
| **Predicted vs Actual return** | Scatter plot comparing expected return midpoint vs actual return. Points on the diagonal = perfect. Color = outcome. |
| **Rolling metrics** | Aggregate metrics from `metrics_rolling` table — recomputed nightly, showing system-level accuracy by horizon and lookback window. |
""")
    else:
        n_eval = len(metric_df)
        horizons_present = sorted(metric_df["horizon_days"].dropna().unique())

        # ── Metric selector ────────────────────────────────────────────────────
        m1, m2 = st.columns(2)
        with m1:
            horizon_sel = st.multiselect(
                "Horizon filter",
                options=horizons_present,
                default=list(horizons_present),
                format_func=lambda h: H_LABELS.get(int(h), f"{int(h)}d"),
                key="trend_horizon",
            )
        with m2:
            roll_window = st.select_slider(
                "Rolling average window",
                options=[5, 10, 20],
                value=10,
                key="trend_roll",
            )

        fdf = metric_df[metric_df["horizon_days"].isin(horizon_sel)] if horizon_sel else metric_df
        st.caption(f"Showing {len(fdf)} of {n_eval} evaluated predictions")

        # ── Brier Score trend ──────────────────────────────────────────────────
        st.subheader("Brier Score — Per Prediction + Rolling Average")
        st.caption(
            "Lower is better. Baseline (uniform guesser) ≈ 0.32. "
            "Below 0.20 = mild edge. Below 0.18 = genuine skill."
        )

        fig_brier = build_brier_trend_chart(fdf, horizons_present, horizon_sel, roll_window, rolling_df,
                                             H_COLORS, H_LABELS)
        st.plotly_chart(fig_brier, use_container_width=True)

        st.divider()

        # ── Log-Loss trend ─────────────────────────────────────────────────────
        st.subheader("Log-Loss — Per Prediction + Rolling Average")
        st.caption(
            "Lower is better. Baseline (coin flip / uniform) ≈ 0.693. "
            "Below 0.5 = model provides useful probability estimates."
        )

        fig_ll = build_logloss_trend_chart(fdf, horizons_present, horizon_sel, roll_window, rolling_df,
                                            H_COLORS, H_LABELS)
        st.plotly_chart(fig_ll, use_container_width=True)

        st.divider()

        # ── Directional accuracy rolling trend ────────────────────────────────
        st.subheader("Directional Accuracy — Rolling Average")
        st.caption("Rolling % of predictions where predicted direction matched actual direction. 50% = random baseline.")

        fig_dir = build_directional_accuracy_chart(fdf, horizons_present, horizon_sel, roll_window, rolling_df,
                                                     H_COLORS, H_LABELS)
        st.plotly_chart(fig_dir, use_container_width=True)

        st.divider()

        # ── Predicted vs Actual return scatter ────────────────────────────────
        st.subheader("Predicted Return Midpoint vs Actual Return")
        st.caption(
            "X = midpoint of predicted return range. Y = actual return. "
            "Points on the diagonal = perfect predictions. "
            "Color = prediction outcome."
        )

        scatter_df = fdf.dropna(subset=["actual_return", "predicted_return_low", "predicted_return_high"]).copy()
        if scatter_df.empty:
            st.info("No predictions with both predicted range and actual return yet.", icon="📉")
        else:
            fig_scatter = build_predicted_vs_actual_scatter(scatter_df)
            st.plotly_chart(fig_scatter, use_container_width=True)
            st.caption("Marker size = conviction score (larger = higher conviction).")

        st.divider()

        # ── Rolling metrics from metrics_rolling table ────────────────────────
        st.subheader("Aggregate Rolling Metrics (metrics_rolling)")
        if rolling_df.empty:
            st.info(
                "The `metrics_rolling` table is empty — it is populated nightly by the "
                "`--validate` pipeline step once evaluated predictions accumulate. "
                "Run **▶ Run Evaluation** after predictions mature.",
                icon="📋",
            )
        else:
            # Pivot: latest row per horizon
            latest_rolling = (
                rolling_df.sort_values("metric_date", ascending=False)
                          .drop_duplicates("horizon_days")
                          .sort_values("horizon_days")
            )
            rm_cols = ["horizon_days", "lookback_days", "directional_accuracy",
                       "in_range_pct", "mean_excess_return", "brier_score",
                       "mean_log_loss", "num_predictions"]
            rm_show = latest_rolling[[c for c in rm_cols if c in latest_rolling.columns]].copy()
            for pct_col in ["directional_accuracy", "in_range_pct", "mean_excess_return"]:
                if pct_col in rm_show.columns:
                    rm_show[pct_col] = rm_show[pct_col].apply(
                        lambda v: f"{v*100:.1f}%" if pd.notna(v) else "—"
                    )
            for f_col in ["brier_score", "mean_log_loss"]:
                if f_col in rm_show.columns:
                    rm_show[f_col] = rm_show[f_col].apply(
                        lambda v: f"{v:.4f}" if pd.notna(v) else "—"
                    )
            st.dataframe(rm_show, use_container_width=True, hide_index=True)

# ── Tab 3: Accuracy Heatmap ───────────────────────────────────────────────────

with tabs[2]:
    heatmap_data = get_accuracy_heatmap_data(lookback_days=lookback, model_names=_model_filter)
    if not heatmap_data:
        st.info("No evaluated predictions yet. Run Evaluation to generate heatmap data.")
    else:
        fig = build_accuracy_heatmap(heatmap_data)
        st.plotly_chart(fig, use_container_width=True)
        st.caption("Grey = fewer than 5 evaluated predictions. "
                   "Green = stronger signal, Red = weaker signal than expected.")

# ── Tab 4: Calibration ────────────────────────────────────────────────────────

with tabs[3]:
    _cal_raw = get_calibration_data(lookback_days=lookback, model_names=_model_filter)
    if isinstance(_cal_raw, list):
        _cal_raw = {"conviction": _cal_raw, "summary": {}, "reliability": [],
                    "bucket_table": [], "horizon_breakdown": {}}
    cal            = _cal_raw
    summary        = cal.get("summary", {})
    reliability    = cal.get("reliability", [])
    bucket_table   = cal.get("bucket_table", [])
    conviction_cal = cal.get("conviction", [])
    h_breakdown    = cal.get("horizon_breakdown", {})

    n_total = summary.get("n", 0)
    brier   = summary.get("brier")
    ll      = summary.get("log_loss")
    B_BASE  = 0.25
    LL_BASE = _math.log(2)

    if n_total == 0:
        st.info(
            "No probability-distribution predictions evaluated yet. "
            "Once predictions with the 5-bucket distribution are scored, "
            "this tab will populate with the full reliability diagram.",
            icon="ℹ️",
        )
    else:
        if n_total < 30:
            st.warning(f"⚠️ Only {n_total} evaluated predictions — metrics completely unreliable.", icon="⚠️")
        elif n_total < 100:
            st.warning(f"⚠️ {n_total} predictions — rough direction visible only.", icon="⚠️")
        elif n_total < 300:
            st.info(f"ℹ️ {n_total} predictions — overall Brier/log-loss meaningful; calibration bins sparse.", icon="ℹ️")
        else:
            st.success(f"✅ {n_total} predictions — calibration is interpretable.", icon="✅")

    tier_label, tier_color = _brier_tier(brier)

    sc1, sc2, sc3 = st.columns(3)
    with sc1:
        delta_b = f"−{(B_BASE - brier)*100:.1f}pp vs baseline" if brier is not None else None
        st.metric("Brier Score", f"{brier:.3f}" if brier is not None else "—",
                  delta=delta_b, delta_color="normal")
        if brier is not None:
            st.markdown(
                f'<span style="background:{tier_color}22;color:{tier_color};'
                f'padding:2px 10px;border-radius:20px;font-size:0.8rem;font-weight:600">'
                f'{tier_label}</span>',
                unsafe_allow_html=True,
            )
    with sc2:
        delta_ll = f"−{(LL_BASE - ll):.3f} vs baseline" if ll is not None else None
        st.metric("Log-Loss", f"{ll:.3f}" if ll is not None else "—",
                  delta=delta_ll, delta_color="normal")
        st.caption(f"baseline (coin flip) = {LL_BASE:.3f}")
    with sc3:
        st.metric("Sample Size", f"N = {n_total}")
        st.caption(f"Brier baseline = {B_BASE:.2f}")

    st.markdown("""
<div style="background:#F8FAFC;border:1px solid #E2E8F0;border-radius:10px;padding:14px 18px;margin:12px 0">
<strong style="color:#0F172A;font-size:0.88rem">📐 Binary Brier score — p_up vs actual up/not-up</strong>
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
        st.info("No reliability data yet — needs evaluated predictions with distribution.", icon="ℹ️")
    else:
        fig = build_reliability_diagram(reliability)
        st.plotly_chart(fig, use_container_width=True)

    st.subheader("Prediction Count per Confidence Bucket")
    st.caption("Sample size per bin is critical — thin bins (< 30) are unreliable.")
    st.dataframe(pd.DataFrame(bucket_table), use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("Breakdown by Horizon")
    if not h_breakdown:
        st.info("No per-horizon distribution data yet.")
    else:
        h_labels = {5: "5-Day", 21: "21-Day", 63: "63-Day"}
        h_cols = st.columns(len(h_breakdown))
        for col, (h, hd) in zip(h_cols, sorted(h_breakdown.items())):
            b = hd["brier"]
            tier, tc = _brier_tier(b)
            with col:
                st.markdown(
                    f'<div style="background:#F8FAFC;border:1px solid #E2E8F0;border-radius:10px;'
                    f'padding:14px 16px;text-align:center">'
                    f'<div style="font-size:0.95rem;font-weight:700;color:#0F172A">{h_labels.get(h, f"{h}d")}</div>'
                    f'<div style="font-size:1.6rem;font-weight:800;color:#0F172A;margin:6px 0">{b:.3f}</div>'
                    f'<div style="font-size:0.75rem;color:{tc};font-weight:600">{tier}</div>'
                    f'<div style="font-size:0.75rem;color:#64748B;margin-top:4px">'
                    f'log-loss {hd["log_loss"]:.3f} · N={hd["n"]}</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

    st.divider()
    st.subheader("Conviction Score vs Directional Accuracy")
    st.caption("Conviction (1–10) vs whether the predicted direction was correct.")
    if len(conviction_cal) < 2:
        st.info("Insufficient data — needs predictions across at least 2 conviction buckets.")
    else:
        fig2 = build_conviction_calibration_chart(conviction_cal)
        st.plotly_chart(fig2, use_container_width=True)

# ── Tab 5: Recent Predictions ─────────────────────────────────────────────────

with tabs[4]:
    preds = get_recent_evaluated_predictions(limit=500, lookback_days=lookback)
    # Apply global model filter
    if _model_filter and preds:
        preds = [p for p in preds if p.get("model_name") in _model_filter]
    # Apply segment filter
    if _seg_sel == "Portfolio" and _portfolio_tickers and preds:
        _pt_set = set(_portfolio_tickers)
        preds = [p for p in preds if p.get("ticker", "").upper() in _pt_set]
    elif _seg_sel == "New Opportunities" and preds:
        preds = [p for p in preds if p.get("trigger_type") == "trending_opportunity"]
    if not preds:
        _no_pred_msg = "No evaluated predictions yet."
        if _model_filter:
            _no_pred_msg = f"No evaluated predictions for model(s): {', '.join(_model_filter)}"
        if _seg_sel != "All":
            _no_pred_msg += f" (segment: {_seg_sel})"
        st.info(_no_pred_msg)
    else:
        f1, f2 = st.columns(2)
        with f1:
            h_opts = sorted({p["horizon_days"] for p in preds if p.get("horizon_days")})
            h_filter = st.multiselect("Horizon", h_opts, default=h_opts,
                                      format_func=lambda x: f"{x}d", key="pred_tab_h")
        with f2:
            o_opts = sorted({p["outcome"] for p in preds if p.get("outcome")})
            o_filter = st.multiselect("Outcome", o_opts, default=o_opts, key="pred_tab_o")

        filtered = sorted(
            [p for p in preds
             if (not h_filter or p.get("horizon_days") in h_filter)
             and (not o_filter or p.get("outcome") in o_filter)],
            key=lambda p: p.get("as_of_date") or p.get("prediction_date") or "",
            reverse=True,
        )

        outcome_icon = {
            "strong_correct":       "✅",
            "directionally_correct":"🟢",
            "flat_correct":         "🔵",
            "wrong_minor":          "🟡",
            "wrong_significant":    "🔴",
            "data_missing":         "⬜",
        }
        def _fmt_price(p: float) -> str:
            return f"${p:,.2f}" if p >= 10 else f"${p:.3f}"

        rows = []
        for p in filtered:
            ar    = p.get("actual_return")        # decimal  e.g. -0.0063
            rlo   = p.get("predicted_return_low")  # percent  e.g. 1.5
            rhi   = p.get("predicted_return_high") # percent  e.g. 4.0
            sp    = p.get("start_price")           # dollars
            oc    = p.get("outcome") or p.get("evaluation_status") or "—"
            bs    = p.get("brier_score")
            ll    = p.get("log_loss")
            er    = p.get("excess_return")

            # Return % strings
            rng_pct = f"{rlo:+.1f}% – {rhi:+.1f}%" if rlo is not None and rhi is not None else "—"
            act_pct = f"{ar*100:+.2f}%" if ar is not None else "—"

            # Dollar price strings (only when start_price available)
            if sp and sp > 0:
                if rlo is not None and rhi is not None:
                    p_lo = sp * (1 + rlo / 100)
                    p_hi = sp * (1 + rhi / 100)
                    rng_price = f"{_fmt_price(p_lo)} – {_fmt_price(p_hi)}"
                else:
                    rng_price = "—"
                act_price = _fmt_price(sp * (1 + ar)) if ar is not None else "—"
                entry_price = _fmt_price(sp)
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
                "Outcome":         f"{outcome_icon.get(oc,'')} {oc}",
                "Brier":           f"{bs:.4f}" if bs is not None else "—",
                "Log-Loss":        f"{ll:.4f}" if ll is not None else "—",
                "Conviction":      p.get("conviction_score"),
                "Excess vs SPY":   f"{er*100:+.2f}%" if er is not None else "—",
                "Segment":         p.get("risk_segment") or "unknown",
            })

        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        st.caption(
            f"{len(filtered)} of {len(preds)} evaluated predictions shown.  "
            "Entry Price = price at prediction date · Pred Price Range = estimated price window · "
            "Actual Price = realized price at horizon.  "
            "Distribution: ▼▼=strong_down ▼=moderate_down →=flat ▲=moderate_up ▲▲=strong_up"
        )

        # ── Brier + Log-Loss distribution among evaluated predictions ─────────
        if filtered:
            eval_df = pd.DataFrame(rows)
            b_vals  = [p["brier_score"] for p in filtered if p.get("brier_score") is not None]
            ll_vals = [p["log_loss"] for p in filtered if p.get("log_loss") is not None]

            if b_vals or ll_vals:
                st.divider()
                st.subheader("Distribution of Brier Score & Log-Loss (evaluated set)")
                hc1, hc2 = st.columns(2)
                with hc1:
                    if b_vals:
                        fig_bh = build_brier_histogram(b_vals)
                        st.plotly_chart(fig_bh, use_container_width=True)
                with hc2:
                    if ll_vals:
                        fig_llh = build_logloss_histogram(ll_vals)
                        st.plotly_chart(fig_llh, use_container_width=True)

# ── Tab 6: Post-mortems ───────────────────────────────────────────────────────

with tabs[5]:
    reports = get_validation_reports(limit=5)
    if not reports:
        st.info("No post-mortem reports yet. Click **🔬 Run Weekly Analysis** in the sidebar.")
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

# ── Tab 7: Drift ──────────────────────────────────────────────────────────────

with tabs[6]:
    drift_5d = get_drift_data(horizon_days=5, window=30, lookback_days=lookback, model_names=_model_filter)
    vol_data  = get_volume_by_horizon(lookback_days=lookback)

    col_l, col_r = st.columns(2)

    with col_l:
        st.subheader("5-Day Rolling Accuracy (30d window)")
        if not drift_5d:
            st.info("No drift data yet — needs evaluated 5d predictions.")
        else:
            fig = build_drift_accuracy_chart(drift_5d)
            st.plotly_chart(fig, use_container_width=True)

    with col_r:
        st.subheader("Prediction Volume by Horizon")
        if not vol_data:
            st.info("No volume data yet.")
        else:
            fig = build_volume_by_horizon_chart(vol_data)
            st.plotly_chart(fig, use_container_width=True)

# ── Tab 8: Version Comparison ─────────────────────────────────────────────────

with tabs[7]:
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
            "Directional Acc.":   f"{r['dir_acc']*100:.1f}%" if r.get("dir_acc") is not None else "—",
            "Mean Excess Return": f"{r['mean_excess']*100:.2f}%" if r.get("mean_excess") is not None else "—",
        } for r in ver_data]
        st.dataframe(pd.DataFrame(rows_ver), use_container_width=True, hide_index=True)
