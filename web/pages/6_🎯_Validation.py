"""
APEX Prediction Validation Dashboard — system health panel with metric trend charts.
"""
from __future__ import annotations

import os
import sys
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
    get_volume_by_horizon,
    get_system_version_comparison,
    weekly_pattern_analysis,
)
from portfolio_agent.tools.prediction_db import (
    get_rolling_metrics,
    get_validation_reports,
    CURRENT_SYSTEM_VERSION,
)
from web.data.validation import (
    _load_metric_series,
    _load_portfolio_tickers,
    _load_watchlist_tickers,
    _get_model_names,
    _compute_filtered_metrics,
    _load_rolling_metrics_series,
    _directional_correct,
)
from web.components.validation_charts import (
    build_brier_trend_chart,
    build_logloss_trend_chart,
    build_directional_accuracy_chart,
    build_predicted_vs_actual_scatter,
    build_accuracy_heatmap,
    build_reliability_diagram,
    build_conviction_calibration_chart,
    build_brier_histogram,
    build_logloss_histogram,
    build_volume_by_horizon_chart,
)
from web.components.validation_cards import _render_scorecard_tiles

# ── Session state ─────────────────────────────────────────────────────────────

# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown(
        '<p style="font-size:0.7rem;font-weight:700;text-transform:uppercase;'
        'letter-spacing:0.08em;color:#475569;margin:0 0 10px">Filters</p>',
        unsafe_allow_html=True,
    )
    st.caption("Run Evening (evaluation) and Weekly Analysis from the 🗓️ Schedule page.")

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
        ["All", "Portfolio", "Watchlist", "New Opportunities"],
        index=0,
        label_visibility="collapsed",
        key="val_segment",
        help=(
            "All: every prediction  ·  "
            "Portfolio: your holdings only  ·  "
            "Watchlist: tracked but not held  ·  "
            "New Opportunities: trending tickers discovered by the scanner"
        ),
    )

# Load portfolio/watchlist tickers once (used by segment filter throughout)
_portfolio_tickers = _load_portfolio_tickers()
_watchlist_tickers = _load_watchlist_tickers()

# ── Page header ───────────────────────────────────────────────────────────────

st.title("🎯 APEX Prediction Validation")
st.caption(f"System version: `{CURRENT_SYSTEM_VERSION}` · Scores predictions after they mature")

_active_filters: list[str] = []
if _model_filter:
    _active_filters.append(f"Model: {', '.join(f'`{m}`' for m in _model_filter)}")
if _seg_sel != "All":
    _seg_icon = {"Portfolio": "💼", "Watchlist": "📋"}.get(_seg_sel, "🌟")
    _active_filters.append(f"Segment: **{_seg_icon} {_seg_sel}**")
if _active_filters:
    st.info(
        "🔍 **Active filters** — " + "  ·  ".join(_active_filters) +
        "  ·  All metrics and tables recalculated for selected filter(s).",
        icon="🔍",
    )

# ── Live log panel ────────────────────────────────────────────────────────────

# ── Tabs ──────────────────────────────────────────────────────────────────────

tabs = st.tabs([
    "📊 Scorecard",
    "📈 Metric Trends",
    "🗺️ Heatmap",
    "🎯 Calibration",
    "📋 Prediction History",
    "🔬 Post-mortems",
    "🔄 Versions",
    "📐 Score Calibration",
])

# ── Tab 1: Scorecard ──────────────────────────────────────────────────────────

with tabs[0]:
    metrics = _compute_filtered_metrics(_model_filter, lookback, _seg_sel, _portfolio_tickers, _watchlist_tickers)
    if not metrics:
        st.info(
            "No evaluated predictions yet. "
            "Click **🌆 Evening** on the 🗓️ Schedule page to score matured predictions.",
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
                "Dir Accuracy":   f"{row['directional_accuracy']*100:.2f}%" if row.get("directional_accuracy") is not None else "—",
                "In-Range %":     f"{row['in_range_pct']*100:.2f}%"         if row.get("in_range_pct")          is not None else "—",
                "Excess Return":  f"{row['mean_excess_return']*100:+.2f}%"  if row.get("mean_excess_return")     is not None else "—",
                "Hi-Conv Acc":    f"{row['high_conviction_accuracy']*100:.2f}%" if row.get("high_conviction_accuracy") is not None else "—",
                "Lo-Conv Acc":    f"{row['low_conviction_accuracy']*100:.2f}%"  if row.get("low_conviction_accuracy")  is not None else "—",
                "Brier Score":    f"{row['brier_score']:.2f}"                if row.get("brier_score")            is not None else "—",
                "Log-Loss":       f"{row['mean_log_loss']:.2f}"              if row.get("mean_log_loss")           is not None else "—",
            })
        st.dataframe(pd.DataFrame(raw_rows), hide_index=True, use_container_width=True)

# ── Tab 2: Metric Trends ──────────────────────────────────────────────────────

with tabs[1]:
    st.subheader("📈 Prediction Quality Metrics Over Time")
    st.caption(
        "Each data point is one evaluated prediction. Rolling average smooths the trend. "
        "Populates after **🌆 Evening** (🗓️ Schedule page) scores matured predictions."
    )

    metric_df  = _load_metric_series(_seg_sel, _portfolio_tickers, _watchlist_tickers)
    rolling_df = _load_rolling_metrics_series(_seg_sel)

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
            "Click **🌆 Evening** on the 🗓️ Schedule page to score any matured predictions.",
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

        # Both default off — with 4 horizons now scored (5/21/63/250d), showing
        # every layer (raw points + rolling avg + a separate unfiltered
        # aggregate line) per horizon adds up to 12 traces on one chart. The
        # rolling-average line is the one that actually reads as a trend;
        # these add detail back in on request instead of by default.
        d1, d2 = st.columns(2)
        with d1:
            show_points = st.checkbox(
                "Show individual predictions", value=False, key="trend_show_points",
                help="Raw per-prediction dots behind the rolling average — adds one layer of detail, and clutter.",
            )
        with d2:
            show_aggregate = st.checkbox(
                "Show unfiltered aggregate overlay", value=False, key="trend_show_aggregate",
                help="A second line from the nightly metrics_rolling table, computed across ALL models "
                     "regardless of the model filter above — useful to compare filtered vs. unfiltered, "
                     "otherwise mostly duplicates the rolling-average line already shown.",
            )

        fdf = metric_df[metric_df["horizon_days"].isin(horizon_sel)] if horizon_sel else metric_df
        st.caption(f"Showing {len(fdf)} of {n_eval} evaluated predictions")

        # ── Brier Score trend ──────────────────────────────────────────────────
        st.subheader("Brier Score — Rolling Average")
        st.caption(
            "Lower is better. Baseline (uniform guesser) ≈ 0.32. "
            "Below 0.20 = mild edge. Below 0.18 = genuine skill."
        )

        fig_brier = build_brier_trend_chart(fdf, horizons_present, horizon_sel, roll_window, rolling_df,
                                             H_COLORS, H_LABELS, show_points=show_points, show_aggregate=show_aggregate)
        st.plotly_chart(fig_brier, use_container_width=True)

        st.divider()

        # ── Log-Loss trend ─────────────────────────────────────────────────────
        st.subheader("Log-Loss — Rolling Average")
        st.caption(
            "Lower is better. Baseline (coin flip / uniform) ≈ 0.693. "
            "Below 0.5 = model provides useful probability estimates."
        )

        fig_ll = build_logloss_trend_chart(fdf, horizons_present, horizon_sel, roll_window, rolling_df,
                                            H_COLORS, H_LABELS, show_points=show_points, show_aggregate=show_aggregate)
        st.plotly_chart(fig_ll, use_container_width=True)

        st.divider()

        # ── Directional accuracy rolling trend ────────────────────────────────
        st.subheader("Directional Accuracy — Rolling Average")
        st.caption("Rolling % of predictions where predicted direction matched actual direction. 50% = random baseline.")

        fig_dir = build_directional_accuracy_chart(fdf, horizons_present, horizon_sel, roll_window, rolling_df,
                                                     H_COLORS, H_LABELS, show_aggregate=show_aggregate)
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

        # ── Prediction volume by horizon (moved here from the old Drift tab —
        # its other chart, 5d rolling accuracy, was a strict subset of the
        # Directional Accuracy chart above, which already covers every horizon) ──
        st.subheader("Prediction Volume by Horizon")
        vol_data = get_volume_by_horizon(lookback_days=lookback)
        if not vol_data:
            st.info("No volume data yet.")
        else:
            fig_vol = build_volume_by_horizon_chart(vol_data)
            st.plotly_chart(fig_vol, use_container_width=True)

# ── Tab 3: Accuracy Heatmap ───────────────────────────────────────────────────

with tabs[2]:
    heatmap_data = get_accuracy_heatmap_data(lookback_days=lookback, model_names=_model_filter)
    if not heatmap_data:
        st.info("No evaluated predictions yet. Run **🌆 Evening** on the 🗓️ Schedule page to generate heatmap data.")
    else:
        fig = build_accuracy_heatmap(heatmap_data)
        st.plotly_chart(fig, use_container_width=True)
        st.caption("Grey = fewer than 5 evaluated predictions. "
                   "Green = stronger signal, Red = weaker signal than expected.")

# ── Tab 4: Calibration ────────────────────────────────────────────────────────

with tabs[3]:
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

    # Overall/per-horizon Brier & Log-Loss numbers live on the Scorecard tab —
    # not restated here. This tab's job is the reliability diagram, bucket
    # sample sizes, and conviction calibration, none of which Scorecard shows.
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
    st.caption("Per-horizon Brier/log-loss breakdown lives on the Scorecard tab.")

    st.divider()
    st.subheader("Conviction Score vs Directional Accuracy")
    st.caption("Conviction (1–10) vs whether the predicted direction was correct.")
    if len(conviction_cal) < 2:
        st.info("Insufficient data — needs predictions across at least 2 conviction buckets.")
    else:
        fig2 = build_conviction_calibration_chart(conviction_cal)
        st.plotly_chart(fig2, use_container_width=True)

# ── Tab 5: Prediction History (evaluated/matured predictions — distinct from
# the live/pending predictions the separate Predictions page shows) ───────────

with tabs[4]:
    preds = get_recent_evaluated_predictions(limit=500, lookback_days=lookback)
    # Apply global model filter
    if _model_filter and preds:
        preds = [p for p in preds if p.get("model_name") in _model_filter]
    # Apply segment filter
    if _seg_sel == "Portfolio" and _portfolio_tickers and preds:
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
            return f"${p:,.2f}" if p >= 10 else f"${p:.2f}"

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
            rng_pct = f"{rlo:+.2f}% – {rhi:+.2f}%" if rlo is not None and rhi is not None else "—"
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
                "Brier":           f"{bs:.2f}" if bs is not None else "—",
                "Log-Loss":        f"{ll:.2f}" if ll is not None else "—",
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
        st.info("No post-mortem reports yet. Click **🔬 Weekly Analysis** on the 🗓️ Schedule page.")
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

# ── Tab 7: Version Comparison ─────────────────────────────────────────────────
# (the old Drift tab was removed here — its rolling-accuracy chart was a strict
# subset of Metric Trends' Directional Accuracy chart, and its volume-by-horizon
# chart now lives at the bottom of Metric Trends instead)

with tabs[6]:
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
            "Directional Acc.":   f"{r['dir_acc']*100:.2f}%" if r.get("dir_acc") is not None else "—",
            "Mean Excess Return": f"{r['mean_excess']*100:.2f}%" if r.get("mean_excess") is not None else "—",
        } for r in ver_data]
        st.dataframe(pd.DataFrame(rows_ver), use_container_width=True, hide_index=True)

# ── Tab 8: Score Calibration ───────────────────────────────────────────────────
#
# Answers "is the Opportunity Engine's 0-100 score actually better than plain
# APEX at predicting returns" — empirically, not by assertion. See
# portfolio_agent/tools/scoring_snapshot_db.py and calibration_analysis.py.

with tabs[7]:
    from portfolio_agent.tools.scoring_snapshot_db import get_latest_snapshot_date, get_snapshots_for_date
    from portfolio_agent.tools.calibration_analysis import compute_calibration_report, SCORE_LABELS

    st.subheader("📐 Today's Scoring Table")
    st.caption(
        "Every ticker scored today across all four parallel systems, plus a documented fixed "
        "blend (Final) — captured daily so it can be checked against realized returns later."
    )
    snap_date = get_latest_snapshot_date()
    if not snap_date:
        st.info(
            "No scoring snapshots yet — this table populates once the morning batch's "
            "Score Calibration phase runs.",
            icon="📐",
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
        st.dataframe(pd.DataFrame(table_rows), use_container_width=True, hide_index=True)

    st.divider()

    st.subheader("🔬 Calibration Results")
    st.caption(
        "For each scoring system: correlation with the realized return at this horizon, and "
        "the average return of the top-20% vs. bottom-20% scored names — the more legible "
        "\"did our top picks actually do better\" view."
    )
    horizon_choice = st.radio(
        "Horizon", [30, 60, 90, 250], format_func=lambda h: f"{h}d", horizontal=True, key="calib_horizon",
    )
    report = compute_calibration_report(horizon_choice)
    calib_rows = []
    for col, stats in report["scores"].items():
        label = SCORE_LABELS[col]
        if stats.get("insufficient_data"):
            calib_rows.append({
                "Score": label, "N": stats["n"],
                "Correlation": "—", "Top 20% Avg Return": "—", "Bottom 20% Avg Return": "—", "Spread": "—",
            })
        else:
            calib_rows.append({
                "Score": label,
                "N": stats["n"],
                "Correlation": f"{stats['correlation']:+.2f}" if stats["correlation"] is not None else "—",
                "Top 20% Avg Return": f"{stats['top_quintile_avg_return_pct']:+.2f}%",
                "Bottom 20% Avg Return": f"{stats['bottom_quintile_avg_return_pct']:+.2f}%",
                "Spread": f"{stats['spread_pct']:+.2f}%",
            })
    st.dataframe(pd.DataFrame(calib_rows), use_container_width=True, hide_index=True)

    any_insufficient = any(s.get("insufficient_data") for s in report["scores"].values())
    if any_insufficient:
        st.info(
            f"Some scores don't have {report['min_sample']} matured {horizon_choice}-day snapshots yet — "
            f"a {horizon_choice}-day horizon needs {horizon_choice} days of history before the first row "
            "can even mature, so this fills in gradually. Check back as more days pass.",
            icon="⏳",
        )
