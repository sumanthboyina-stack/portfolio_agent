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

It also hosts everything that used to sit on the user-facing Validation page before that page was reduced to
four score cards: Baselines, Ranking detail, Calibration, Models, FLAT diagnostic, the legacy metric tiles &
outcome breakdown, the compounded call-return index, and the scoring-snapshot tables.

Rows are filtered by prediction_role (sidebar "Prediction roles", default champion + fallback; legacy
random-model rows on request). The Roles tab is the one place every role — including challenger shadow
rows — is shown side by side, with the champion-vs-challenger comparison on the same tickers and dates.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

import pandas as pd
import streamlit as st
from web.styles import inject_global_css, top_nav, icon_html, material, fmt_pct, section_tile

from web.auth import current_context, current_user, require_login

st.set_page_config(
    page_title="APEX — Validation QA",
    page_icon=str(_ROOT / "web" / "static" / "apex_mark.png"),
    layout="wide",
    initial_sidebar_state="expanded",
)
inject_global_css()
require_login()
top_nav("validation_qa")

if not current_user().is_admin:
    st.error("Admin only.")
    st.stop()

from portfolio_agent.tools.validation_engine import (
    get_calibration_data,
    get_volume_by_horizon,
    get_system_version_comparison,
)
from portfolio_agent.tools.prediction_db import (
    get_validation_reports, CURRENT_SYSTEM_VERSION, REVIEW_ROLES, VALIDATION_ROLES, VALIDATION_ROLES_WITH_LEGACY,
)
from web.data.validation import (
    _load_metric_series,
    _load_portfolio_tickers,
    _load_watchlist_tickers,
    _get_model_names,
    _load_rolling_metrics_series,
    _load_evaluated_predictions_df,
    _compute_filtered_metrics,
    build_horizon_reports,
)
from web.components.validation_cards import (
    render_baselines_body,
    render_ranking_tables,
    render_model_split,
    render_flat_diagnostic,
)
from web.components.validation_legacy import (
    render_calibration_headline,
    render_outcome_breakdown,
    render_legacy_tiles,
    render_equity_curve,
    render_scoring_snapshots,
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
        return "claude" in n or "gpt-4" in n or "gpt-5" in n or "o3" in n

    _all_model_names = _get_model_names(roles=VALIDATION_ROLES_WITH_LEGACY)
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

    st.divider()

    st.markdown(
        '<p style="font-size:0.72rem;font-weight:600;color:#9CA3AF;margin:0 0 8px">'
        'Prediction roles</p>',
        unsafe_allow_html=True,
    )
    _role_sel = st.radio(
        "roles",
        ["Champion + fallback", "+ Legacy (random model)"],
        index=0,
        label_visibility="collapsed",
        key="valqa_roles",
        help=(
            "Champion + fallback: rows from the configured champion model plan (default)  ·  "
            "+ Legacy: also rows from the random-model era, before champion selection. "
            "Challenger shadow rows only appear on the Roles tab."
        ),
    )
_qa_roles = VALIDATION_ROLES if _role_sel == "Champion + fallback" else VALIDATION_ROLES_WITH_LEGACY

# The loaders are scoped to the caller's own holdings/watchlist and need the caller's context
# (this page used to call them with no argument, which raised a TypeError on load).
_ctx = current_context("web:validation_qa")
_portfolio_tickers = _load_portfolio_tickers(_ctx)
_watchlist_tickers = _load_watchlist_tickers(_ctx)

# ── Shared data for the detail tabs (same filters as the sidebar) ─────────────

_H_LABELS_ALL = {5: "5-Day", 21: "21-Day", 63: "63-Day", 250: "250-Day"}
_qa_df = _load_evaluated_predictions_df(lookback, _model_filter, _seg_sel, _portfolio_tickers, _watchlist_tickers,
                                        roles=_qa_roles)
_qa_horizons = sorted(int(h) for h in _qa_df["horizon_days"].dropna().unique()) if not _qa_df.empty else []

_PRICE_AUTO_MAX_TICKERS = 60
_qa_n_tickers = int(_qa_df["ticker"].nunique()) if not _qa_df.empty else 0
_qa_use_prices = _qa_n_tickers <= _PRICE_AUTO_MAX_TICKERS
with st.sidebar:
    st.divider()
    st.markdown(
        '<p style="font-size:0.72rem;font-weight:600;color:#9CA3AF;margin:0 0 8px">'
        'Detail tabs</p>',
        unsafe_allow_html=True,
    )
    if _qa_horizons:
        _qa_h = st.radio("Horizon", _qa_horizons, index=0, horizontal=True, key="valqa_detail_horizon",
                         format_func=lambda h: _H_LABELS_ALL.get(h, f"{h}d"),
                         help="Used by the Baselines, Ranking detail, Models and FLAT diagnostic tabs. Horizons are never mixed.")
    else:
        _qa_h = None
    if not _qa_use_prices:
        _qa_use_prices = st.toggle(
            "Price-based baselines", value=False, key="valqa_price_baselines",
            help=f"Momentum / SPY-prior-window baselines need price history for each of the {_qa_n_tickers} tickers "
                 "in this scope (cached 30 min).",
        )


@st.cache_data(ttl=600, show_spinner="Computing baselines and ranking metrics…")
def _cached_reports(df: pd.DataFrame, use_prices: bool) -> dict:
    return build_horizon_reports(df, use_prices=use_prices)


_reports: dict = {}
if _qa_horizons:
    try:
        _reports = _cached_reports(_qa_df, _qa_use_prices)
    except Exception as _exc:
        st.warning(f"Baselines unavailable: {_exc}", icon=material("warning"))
_report = _reports.get(_qa_h) if _qa_h is not None else None
_horizon_baselines = {
    h: r["comparison"]["best_baseline"]["accuracy"]
    for h, r in _reports.items() if r["comparison"].get("best_baseline")
}

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
    f"{material('compare_arrows')} Baselines",
    f"{material('leaderboard')} Ranking detail",
    f"{material('tune')} Calibration",
    f"{material('model_training')} Models",
    f"{material('flag')} FLAT diagnostic",
    f"{material('grid_view')} Legacy tiles & outcomes",
    f"{material('show_chart')} Equity curve",
    f"{material('calculate')} Scoring snapshots",
    f"{material('swap_horiz')} Roles",
])

# ── Tab 1: Metric Trends ──────────────────────────────────────────────────────

with tabs[0]:
    _tile_1 = section_tile(f"{material('trending_up')} Prediction Quality Metrics Over Time", expanded=True, key="validation_qa_1")
    if _tile_1:
        with _tile_1:
            st.caption(
                "Each data point is one evaluated prediction. Rolling average smooths the trend. "
                f"Populates after {material('nights_stay')} **Evening** ({material('calendar_month')} "
                "Schedule page) scores matured predictions."
            )

            metric_df  = _load_metric_series(_seg_sel, _portfolio_tickers, _watchlist_tickers, roles=_qa_roles)
            rolling_df = _load_rolling_metrics_series(_seg_sel)

            if _model_filter and not metric_df.empty and "model_name" in metric_df.columns:
                metric_df = metric_df[metric_df["model_name"].isin(_model_filter)]
            if _model_filter and not rolling_df.empty:
                st.caption(f"{material('info')} Aggregate rolling metrics (dashed lines) are not model-filtered — they reflect all models.")
            if not rolling_df.empty:
                st.caption(f"{material('info')} The nightly aggregate (dashed lines) excludes challenger rows but keeps "
                           "legacy random-model rows; it is not filtered by the Prediction roles selector.")

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
                st.caption("Rolling % of predictions where predicted direction matched actual direction (3 classes: UP / DOWN / FLAT). Compare against the naive baselines on the Baselines tab — there is no fixed random line.")
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
                vol_data = get_volume_by_horizon(lookback_days=lookback, roles=_qa_roles)
                if not vol_data:
                    st.info("No volume data yet.")
                else:
                    fig_vol = build_volume_by_horizon_chart(vol_data)
                    st.plotly_chart(fig_vol, use_container_width=True)
# ── Tab 2: Confidence Calibration ─────────────────────────────────────────────

with tabs[1]:
    _cal_raw = get_calibration_data(lookback_days=lookback, model_names=_model_filter, roles=_qa_roles)
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
    _tile_2 = section_tile("Reliability Diagram", expanded=True, key="validation_qa_2")
    if _tile_2:
        with _tile_2:
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
    _tile_3 = section_tile("Prediction Count per Confidence Bucket", expanded=False, key="validation_qa_3")
    if _tile_3:
        with _tile_3:
            st.caption("Sample size per bin is critical — thin bins (< 30) are unreliable.")
            st.dataframe(pd.DataFrame(bucket_table), use_container_width=True, hide_index=True)
            st.caption("Per-horizon Brier/log-loss breakdown lives on the user-facing Validation page's Scorecard tab.")
    _tile_4 = section_tile("Conviction Score vs Directional Accuracy", expanded=False, key="validation_qa_4")
    if _tile_4:
        with _tile_4:
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
            _hist_preds = get_recent_evaluated_predictions(limit=1000, lookback_days=lookback, roles=_qa_roles)
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
    from portfolio_agent.tools.validation_engine import get_active_failure_patterns

    # Deterministic, statistically-confirmed patterns -- not the LLM's own
    # narrative below. These are exactly what protects predictions before
    # weekly_pattern_analysis() has 20+ shared evaluated rows recurring across
    # 2+ weekly reports to promote something here: until then this list stays
    # empty and apex.py's hand-coded conviction caps (no-news-bullish, buy-
    # the-dip bounce) are the only protection -- see get_active_failure_patterns().
    active_patterns = get_active_failure_patterns(limit=10)
    _tile_active = section_tile("Active Failure Patterns (feeding every APEX prompt)",
                                expanded=True, key="validation_qa_active_patterns")
    if _tile_active:
        with _tile_active:
            if not active_patterns:
                st.info(
                    "No recurring failure pattern has cleared the bar yet (needs >=20 shared "
                    "evaluated predictions in a segment, recurring across 2+ weekly reports). "
                    "The hand-coded conviction guardrails in apex.py are the only protection "
                    "against known bad patterns until one does."
                )
            else:
                st.caption(
                    f"{len(active_patterns)} pattern(s) currently injected into every APEX/chat "
                    "prompt as a known recurring failure mode."
                )
                for pat in active_patterns:
                    st.write(f"- **{pat.get('description', pat.get('key', '?'))}** "
                            f"— seen in {pat.get('weeks_seen', '?')} of the last weekly reports")

    reports = get_validation_reports(limit=5)
    if not reports:
        st.info(
            f"No post-mortem reports yet. Click {material('science')} **Weekly Analysis** on the "
            f"{material('calendar_month')} Schedule page."
        )
    else:
        for report in reports:
            period = (report.get("period") or "weekly").capitalize()
            _tile_5 = section_tile(f"{period} — {report['report_date']}", expanded=True, key="validation_qa_5")
            if _tile_5:
                with _tile_5:
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
                    # Structured, deterministic fields weekly_pattern_analysis() always
                    # computes and stores alongside the LLM's free-form summary above --
                    # previously never rendered anywhere (only consumed programmatically
                    # via get_active_failure_patterns()).
                    seg_flags = pi.get("segment_flags") or []
                    if seg_flags:
                        st.write("**Statistically confirmed failure segments this week:**")
                        for seg in seg_flags:
                            _acc  = seg.get("accuracy")
                            _base = seg.get("baseline_accuracy")
                            st.write(
                                f"- {seg.get('description', seg.get('key'))}: "
                                f"{fmt_pct(_acc * 100 if _acc is not None else None)} accuracy vs "
                                f"{fmt_pct(_base * 100 if _base is not None else None)} baseline "
                                f"(n={seg.get('n')}, p={seg.get('p_value')})"
                            )
                    cand_guardrails = pi.get("candidate_guardrails") or []
                    if cand_guardrails:
                        st.write("**Recurring across multiple weekly reports (candidate guardrails):**")
                        for c in cand_guardrails:
                            st.write(f"- {c.get('description', c.get('key'))} "
                                    f"— seen in {c.get('weeks_seen', '?')} reports")
                    snap = report.get("metrics_snapshot") or {}
                    if snap.get("wrong_count"):
                        st.caption(f"Analyzed {snap['wrong_count']} wrong predictions.")
# ── Tab 4: Version Comparison ─────────────────────────────────────────────────

with tabs[3]:
    ver_data = get_system_version_comparison(lookback_days=lookback, model_names=_model_filter, roles=_qa_roles)
    if not ver_data:
        st.info("No version comparison data yet — needs evaluated predictions.")
    else:
        unique_ver = {r["sys_ver"] for r in ver_data}
        if len(unique_ver) <= 1:
            st.info("Only one system version in history. "
                    "Comparison becomes available after prompt or model changes are tagged.")
            _tile_6 = section_tile(f"Current baseline — version {next(iter(unique_ver), CURRENT_SYSTEM_VERSION)}", expanded=True, key="validation_qa_6")
        rows_ver = [{
            "Version":            r.get("sys_ver") or "v1.0",
            "Horizon":            f"{r.get('horizon_days')}d" if r.get("horizon_days") else "—",
            "N":                  r.get("n"),
            "Directional Acc.":   fmt_pct(r["dir_acc"]*100) if r.get("dir_acc") is not None else "—",
            "Mean Excess Return": fmt_pct(r["mean_excess"]*100) if r.get("mean_excess") is not None else "—",
        } for r in ver_data]
        st.dataframe(pd.DataFrame(rows_ver), use_container_width=True, hide_index=True)

# ── Tab 5: Baselines ──────────────────────────────────────────────────────────

_NO_REPORT = "No evaluated predictions at this horizon yet (check the sidebar filters)."

with tabs[4]:
    st.caption(f"Horizon: **{_H_LABELS_ALL.get(_qa_h, '—')}** (sidebar). APEX against naive baselines scored on the same rows, "
               "the confusion matrix, the beat-the-market table and raw / streak / non-overlapping N.")
    if _report is None:
        st.info(_NO_REPORT, icon=material("info"))
    else:
        render_baselines_body(_report)

# ── Tab 6: Ranking detail ─────────────────────────────────────────────────────

with tabs[5]:
    st.caption(f"Horizon: **{_H_LABELS_ALL.get(_qa_h, '—')}** (sidebar). The numbers behind the Validation page's first card, "
               "with every interval resampling whole prediction dates.")
    if _report is None:
        st.info(_NO_REPORT, icon=material("info"))
    else:
        render_ranking_tables(_report)

# ── Tab 7: Calibration (probability skill vs sample baselines) ────────────────

with tabs[6]:
    render_calibration_headline(lookback, _model_filter, _qa_roles)
    st.caption("The reliability diagram and conviction calibration are on the Confidence Calibration tab.")
    _cal_rows = []
    for _h, _rep in sorted(_reports.items()):
        _prob = _rep.get("probability") or {}
        for _kind, _label in (("binary", "UP vs not-UP"), ("five", "5-bucket")):
            _b = _prob.get(_kind)
            if not _b:
                continue
            _cal_rows.append({
                "Horizon": _H_LABELS_ALL.get(_h, f"{_h}d"), "Event": _label, "n": _prob["n"],
                "Brier": f"{_b['brier']:.3f}", "Baseline Brier": f"{_b['baseline_brier']:.3f}",
                "Brier skill": "—" if _b["brier_skill"] is None else f"{_b['brier_skill']:+.1%}",
                "Log-loss": f"{_b['log_loss']:.3f}", "Baseline log-loss": f"{_b['baseline_log_loss']:.3f}",
                "Log-loss skill": "—" if _b["log_loss_skill"] is None else f"{_b['log_loss_skill']:+.1%}",
            })
    if _cal_rows:
        st.markdown("**Probability scores against the sample's own base-rate forecast**")
        st.dataframe(pd.DataFrame(_cal_rows), hide_index=True, use_container_width=True)
        st.caption("Baseline = always forecasting this sample's own base rates (binary: UP rate; 5-bucket: bucket frequencies). "
                   "Skill = 1 − score / baseline; above 0 beats the base rate.")
    else:
        st.info("No evaluated predictions with a full probability distribution yet.", icon=material("info"))

# ── Tab 8: Models ─────────────────────────────────────────────────────────────

with tabs[7]:
    st.caption(f"Horizon: **{_H_LABELS_ALL.get(_qa_h, '—')}** (sidebar). Ranking numbers split by model_name.")
    if _report is None:
        st.info(_NO_REPORT, icon=material("info"))
    else:
        render_model_split(_report)

# ── Tab 9: FLAT diagnostic ────────────────────────────────────────────────────

with tabs[8]:
    st.caption(f"Horizon: **{_H_LABELS_ALL.get(_qa_h, '—')}** (sidebar).")
    if _report is None:
        st.info(_NO_REPORT, icon=material("info"))
    else:
        render_flat_diagnostic(_report)

# ── Tab 10: Legacy tiles & outcome breakdown ──────────────────────────────────

with tabs[9]:
    _qa_metrics = _compute_filtered_metrics(_model_filter, lookback, _seg_sel, _portfolio_tickers, _watchlist_tickers,
                                            roles=_qa_roles)
    _qa_by_h: dict[int, dict] = {}
    for _row in _qa_metrics:
        _qa_by_h.setdefault(_row["horizon_days"], _row)
    _qa_all_h = [h for h in [5, 21, 63, 250] if h in _qa_by_h] + [h for h in sorted(_qa_by_h) if h not in [5, 21, 63, 250]]
    render_legacy_tiles(_qa_by_h, _qa_all_h, _H_LABELS_ALL, _reports, _horizon_baselines)
    st.divider()
    st.markdown("**Outcome breakdown & returns by recommendation**")
    render_outcome_breakdown(_qa_df)

# ── Tab 11: Equity curve ──────────────────────────────────────────────────────

with tabs[10]:
    render_equity_curve(_qa_df)

# ── Tab 12: Scoring snapshots ─────────────────────────────────────────────────

with tabs[11]:
    render_scoring_snapshots()

# ── Tab 13: Roles (champion / fallback / challenger / legacy) ─────────────────

def _fmt_acc(v) -> str:
    return "—" if v is None else f"{v:.1%}"


def _fmt_skill(v) -> str:
    return "—" if v is None else f"{v:+.1%}"


with tabs[12]:
    from portfolio_agent.tools.validation_roles import champion_vs_challenger, role_breakdown

    st.caption("Every evaluated row split by how its model was chosen. Lookback, model and segment filters from the "
               "sidebar apply; the Prediction roles selector does not (every role is shown here). Baselines here use "
               "no price history, so momentum / SPY-prior-window comparisons are left out.")
    _roles_df = _load_evaluated_predictions_df(lookback, _model_filter, _seg_sel, _portfolio_tickers, _watchlist_tickers,
                                               roles=REVIEW_ROLES)
    if _roles_df.empty:
        st.info("No evaluated predictions in this scope yet.", icon=material("info"))
    else:
        _rb = role_breakdown(_roles_df)
        st.markdown("**By prediction role**")
        st.dataframe(pd.DataFrame([{
            "Horizon": _H_LABELS_ALL.get(r["horizon_days"], f"{r['horizon_days']}d"), "Role": r["label"], "n": r["n"],
            "Accuracy": _fmt_acc(r["accuracy"]), "Best naive baseline": r["best_baseline"] or "—",
            "Baseline accuracy": _fmt_acc(r["best_baseline_accuracy"]),
            "Brier skill (UP vs not)": _fmt_skill(r["brier_skill_binary"]),
            "Brier skill (5-bucket)": _fmt_skill(r["brier_skill_five"]),
        } for r in _rb]), hide_index=True, use_container_width=True)
        st.caption("Legacy (random model) rows mix four models and are shown for reference only.")

        st.divider()
        st.markdown("**Champion vs challenger — same tickers, same dates**")
        _cvc = champion_vs_challenger(_roles_df)
        if not _cvc:
            st.info("No challenger rows have matured yet. A challenger runs only when engine_settings has "
                    "apex_challenger_model set and apex_challenger_sample_pct above 0 (off by default).",
                    icon=material("info"))
        else:
            st.dataframe(pd.DataFrame([{
                "Horizon": _H_LABELS_ALL.get(h, f"{h}d"), "Pairs": c["n_pairs"], "Dates": c["n_dates"],
                "Production model(s)": ", ".join(c["production_models"]) or "—",
                "Challenger model(s)": ", ".join(c["challenger_models"]) or "—",
                "Production accuracy": _fmt_acc(c["production"]["accuracy"]),
                "Challenger accuracy": _fmt_acc(c["challenger"]["accuracy"]),
                "Production Brier skill (5-bucket)": _fmt_skill(c["production"]["brier_skill_five"]),
                "Challenger Brier skill (5-bucket)": _fmt_skill(c["challenger"]["brier_skill_five"]),
                "Both right": c["head_to_head"]["both_right"],
                "Only production right": c["head_to_head"]["production_only"],
                "Only challenger right": c["head_to_head"]["challenger_only"],
                "Both wrong": c["head_to_head"]["both_wrong"],
            } for h, c in sorted(_cvc.items())]), hide_index=True, use_container_width=True)
            st.caption("Production = the champion row, or the fallback row on runs where the champion failed. Only "
                       "(ticker, date) pairs with BOTH a production and a challenger row count. A handful of pairs or "
                       "dates cannot separate two models — treat small samples as anecdotes.")
