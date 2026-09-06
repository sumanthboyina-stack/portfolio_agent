"""
Validation dashboard Plotly chart builders + tier-coloring helpers.

Contains:
  - _METRIC_CFG, _H_COLORS, _H_LABELS, _TILE_CSS : scorecard tile config/constants
  - _get_tier                        : metric value -> (tier_label, hex_color)
  - _brier_tier                      : brier score -> (tier_label, hex_color)
  - build_brier_trend_chart          : per-prediction Brier score + rolling avg
  - build_logloss_trend_chart        : per-prediction log-loss + rolling avg
  - build_directional_accuracy_chart : rolling directional accuracy
  - build_predicted_vs_actual_scatter: predicted return midpoint vs actual return
  - build_accuracy_heatmap           : directional accuracy by segment x horizon
  - build_reliability_diagram        : stated confidence vs actual hit rate
  - build_conviction_calibration_chart : conviction score vs directional accuracy
  - build_brier_histogram            : distribution of Brier scores
  - build_logloss_histogram          : distribution of log-loss values
  - build_drift_accuracy_chart       : 5-day rolling accuracy over time
  - build_volume_by_horizon_chart    : prediction volume stacked by horizon
"""

from __future__ import annotations

import math as _math
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

import pandas as pd
import plotly.graph_objects as go

# ── Metric config: definitions, tiers, scale ──────────────────────────────────

# Each entry: key → (display_label, higher_better, bar_max, bar_min, format_fn, definition, tier_table)
# bar values are normalized to 0–100 scale so all bars are visually comparable.
# Brier/Log-Loss are inverted: bar = (1 − val/bar_max) × 100 so higher bar = better.
_METRIC_CFG = {
    "directional_accuracy": dict(
        label="Directional Accuracy",
        higher=True, bar_max=1.0, bar_min=0.0,
        fmt=lambda v: f"{v*100:.2f}%",
        defn="% of predictions where the model's UP / DOWN / FLAT direction matched the actual market move.",
        tiers=[
            (0.75, "#059669", "🌟 Exceptional  (≥ 75%)"),
            (0.65, "#22C55E", "🟢 Strong skill  (65 – 75%)"),
            (0.55, "#EAB308", "🟡 Mild signal   (55 – 65%)"),
            (0.50, "#F97316", "🟠 Random        (50 – 55%)"),
            (0.00, "#EF4444", "🔴 Below random  (< 50%)"),
        ],
        ref=0.5, ref_label="50% random baseline",
    ),
    "in_range_pct": dict(
        label="In-Range %",
        higher=True, bar_max=1.0, bar_min=0.0,
        fmt=lambda v: f"{v*100:.2f}%",
        defn="% of predictions where the actual return landed inside the model's predicted return range [low, high].",
        tiers=[
            (0.80, "#059669", "🌟 Excellent calibration  (≥ 80%)"),
            (0.65, "#22C55E", "🟢 Well calibrated        (65 – 80%)"),
            (0.50, "#EAB308", "🟡 Acceptable             (50 – 65%)"),
            (0.30, "#F97316", "🟠 Narrow / overconfident (30 – 50%)"),
            (0.00, "#EF4444", "🔴 Poorly calibrated      (< 30%)"),
        ],
        ref=None, ref_label="",
    ),
    "mean_excess_return": dict(
        label="Mean Excess Return",
        higher=True, bar_max=0.05, bar_min=-0.02,
        fmt=lambda v: f"{v*100:+.2f}%",
        defn="Average return above / below the S&P 500 benchmark over the prediction horizon. Positive = alpha.",
        tiers=[
            (0.03,  "#059669", "🌟 Strong alpha      (> +3%)"),
            (0.01,  "#22C55E", "🟢 Positive alpha    (+1 – +3%)"),
            (0.00,  "#EAB308", "🟡 Marginal          (0 – +1%)"),
            (-0.02, "#F97316", "🟠 Underperforming   (−2 – 0%)"),
            (-99,   "#EF4444", "🔴 Significant drag  (< −2%)"),
        ],
        ref=0.0, ref_label="0% benchmark neutral",
    ),
    "high_conviction_accuracy": dict(
        label="Hi-Conv Accuracy  (≥ 7/10)",
        higher=True, bar_max=1.0, bar_min=0.0,
        fmt=lambda v: f"{v*100:.2f}%",
        defn="Directional accuracy for predictions with conviction score ≥ 7/10. High-conviction calls should outperform average.",
        tiers=[
            (0.75, "#059669", "🌟 Exceptional  (≥ 75%)"),
            (0.65, "#22C55E", "🟢 Strong skill  (65 – 75%)"),
            (0.58, "#EAB308", "🟡 Mild edge     (58 – 65%)"),
            (0.50, "#F97316", "🟠 No edge       (50 – 58%)"),
            (0.00, "#EF4444", "🔴 Overconfident (< 50%)"),
        ],
        ref=0.5, ref_label="50% random baseline",
    ),
    "low_conviction_accuracy": dict(
        label="Lo-Conv Accuracy  (< 7/10)",
        higher=True, bar_max=1.0, bar_min=0.0,
        fmt=lambda v: f"{v*100:.2f}%",
        defn="Directional accuracy for predictions with conviction score < 7/10. Low-conviction should be closer to random.",
        tiers=[
            (0.70, "#059669", "🌟 Exceptional  (≥ 70%)"),
            (0.60, "#22C55E", "🟢 Skill        (60 – 70%)"),
            (0.55, "#EAB308", "🟡 Mild signal  (55 – 60%)"),
            (0.50, "#F97316", "🟠 Random       (50 – 55%)"),
            (0.00, "#EF4444", "🔴 Below random (< 50%)"),
        ],
        ref=0.5, ref_label="50% random baseline",
    ),
    "brier_score": dict(
        label="Brier Score  ↓ lower = better",
        higher=False, bar_max=0.30, bar_min=0.0,
        fmt=lambda v: f"{v:.2f}",
        defn="Mean squared error of probability forecasts across 5 return buckets (▼▼ / ▼ / → / ▲ / ▲▲). Perfect = 0.0 · Coin flip = 0.25.",
        tiers=[
            (0.15, "#059669", "🌟 Exceptional     (< 0.15)"),
            (0.18, "#22C55E", "🟢 Genuine skill   (0.15 – 0.18)"),
            (0.20, "#EAB308", "🟡 Mild edge       (0.18 – 0.20)"),
            (0.25, "#F97316", "🟠 Near coin flip  (0.20 – 0.25)"),
            (99,   "#EF4444", "🔴 Actively bad    (> 0.25)"),
        ],
        ref=0.25, ref_label="0.25 coin-flip ceiling",
    ),
    "mean_log_loss": dict(
        label="Log-Loss  ↓ lower = better",
        higher=False, bar_max=1.0, bar_min=0.0,
        fmt=lambda v: f"{v:.2f}",
        defn="Cross-entropy loss. Penalises overconfident wrong predictions more than Brier. Perfect = 0.0 · Coin flip ≈ 0.693.",
        tiers=[
            (0.30,  "#059669", "🌟 Excellent    (< 0.30)"),
            (0.50,  "#22C55E", "🟢 Good signal  (0.30 – 0.50)"),
            (0.693, "#EAB308", "🟡 Marginal     (0.50 – 0.693)"),
            (1.0,   "#F97316", "🟠 Coin-flip    (0.693 – 1.0)"),
            (99,    "#EF4444", "🔴 Overconfident / wrong  (> 1.0)"),
        ],
        ref=0.693, ref_label="0.693 coin-flip baseline",
    ),
}

_H_COLORS = {5: "#2563EB", 21: "#7C3AED", 63: "#059669"}
_H_LABELS = {5: "5-Day", 21: "21-Day", 63: "63-Day"}

_TILE_CSS = """
<style>
.sc-wrap{margin:0 0 28px}
.sc-horizon-hdr{
  font-size:.78rem;font-weight:700;text-transform:uppercase;
  letter-spacing:.08em;margin:0 0 10px;padding:6px 12px;
  border-radius:6px;display:inline-block}
.sc-row{display:flex;gap:10px;flex-wrap:wrap}
.sc-tile{
  position:relative;background:#1E293B;border-radius:10px;
  padding:0 0 12px;min-width:118px;flex:1;cursor:help;
  transition:background .15s, transform .1s;overflow:visible}
.sc-tile:hover{background:#263448;transform:translateY(-2px)}
.sc-tile-bar{height:5px;border-radius:10px 10px 0 0;margin-bottom:12px}
.sc-tile-label{
  font-size:.65rem;color:#94A3B8;text-transform:uppercase;
  letter-spacing:.06em;padding:0 12px;margin-bottom:5px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sc-tile-value{font-size:1.45rem;font-weight:800;padding:0 12px;margin-bottom:3px}
.sc-tile-tier{font-size:.68rem;color:#64748B;padding:0 12px}
/* CSS tooltip */
.sc-tip{
  display:none;position:absolute;bottom:calc(100% + 8px);top:auto;left:50%;
  transform:translateX(-50%);background:#0F172A;border:1px solid #334155;
  border-radius:10px;padding:14px 16px;width:272px;z-index:9999;
  box-shadow:0 -4px 32px rgba(0,0,0,.65);pointer-events:none;
  color:#E2E8F0;font-size:.76rem;line-height:1.55}
.sc-tile:hover .sc-tip{display:block}
.tip-title{font-weight:700;font-size:.86rem;color:#F1F5F9;margin-bottom:3px}
.tip-val{font-size:1.05rem;font-weight:700;margin-bottom:8px}
.tip-defn{color:#94A3B8;font-style:italic;margin-bottom:10px;font-size:.75rem}
.tip-ranges-hdr{font-weight:600;color:#CBD5E1;margin-bottom:5px;font-size:.76rem}
.tip-tier{color:#64748B;padding:1px 0;font-size:.73rem}
.tip-tier.cur{color:#F1F5F9;font-weight:700}
.tip-ref{color:#475569;font-style:italic;margin-top:8px;
  font-size:.71rem;border-top:1px solid #1E293B;padding-top:6px}
</style>
"""


def _get_tier(key: str, val: float) -> tuple[str, str]:
    """Return (label, hex_color) for the tier this value falls into."""
    cfg = _METRIC_CFG[key]
    if cfg["higher"]:
        for threshold, color, label in cfg["tiers"]:
            if val >= threshold:
                return label, color
    else:
        for threshold, color, label in cfg["tiers"]:
            if val <= threshold:
                return label, color
    return cfg["tiers"][-1][2], cfg["tiers"][-1][1]


def _brier_tier(b):
    if b is None:   return "", "#64748B"
    if b < 0.15:    return "exceptional", "#059669"
    if b < 0.18:    return "genuine skill", "#16A34A"
    if b < 0.20:    return "mild edge", "#D97706"
    if b < 0.25:    return "barely better than coin flip", "#DC2626"
    return "actively bad", "#7F1D1D"


# ── Metric trend charts ────────────────────────────────────────────────────────

def build_brier_trend_chart(fdf, horizons_present, horizon_sel, roll_window, rolling_df,
                             h_colors, h_labels, show_points: bool = True,
                             show_aggregate: bool = True) -> go.Figure:
    fig_brier = go.Figure()

    # Horizontal reference lines
    for y_val, label, color in [
        (0.18, "Skill threshold (0.18)", "#16A34A"),
        (0.20, "Coin-flip threshold (0.20)", "#D97706"),
        (0.25, "Worse-than-random (0.25)", "#DC2626"),
    ]:
        fig_brier.add_hline(
            y=y_val, line_dash="dot", line_color=color, opacity=0.5,
            annotation_text=label, annotation_font_size=10,
            annotation_position="right",
        )

    for h in sorted(horizons_present):
        if h not in horizon_sel:
            continue
        sub = fdf[fdf["horizon_days"] == h].sort_values("prediction_date")
        color = h_colors.get(int(h), "#64748B")
        label = h_labels.get(int(h), f"{int(h)}d")

        # Scatter: individual points
        if show_points:
            fig_brier.add_trace(go.Scatter(
                x=sub["prediction_date"], y=sub["brier_score"],
                mode="markers",
                name=f"{label} — each prediction",
                marker=dict(size=7, color=color, opacity=0.5,
                            line=dict(color=color, width=1)),
                hovertemplate=(
                    "<b>%{x|%Y-%m-%d}</b><br>"
                    f"Horizon: {label}<br>"
                    "Brier: %{y:.2f}<extra></extra>"
                ),
            ))

        # Rolling mean
        if len(sub) >= 3:
            roll = sub["brier_score"].rolling(roll_window, min_periods=2).mean()
            fig_brier.add_trace(go.Scatter(
                x=sub["prediction_date"], y=roll,
                mode="lines",
                name=f"{label} — rolling avg",
                line=dict(color=color, width=2.5),
                hovertemplate=(
                    "<b>%{x|%Y-%m-%d}</b><br>"
                    f"Rolling avg ({roll_window}): %{{y:.2f}}<extra></extra>"
                ),
            ))

    # Overlay rolling metrics if available
    if show_aggregate and not rolling_df.empty and "brier_score" in rolling_df.columns:
        for h in sorted(horizons_present):
            rsub = rolling_df[rolling_df["horizon_days"] == h].dropna(subset=["brier_score"])
            if rsub.empty:
                continue
            color = h_colors.get(int(h), "#64748B")
            label = h_labels.get(int(h), f"{int(h)}d")
            fig_brier.add_trace(go.Scatter(
                x=rsub["metric_date"], y=rsub["brier_score"],
                mode="lines+markers",
                name=f"{label} — rolling metric",
                line=dict(color=color, width=2, dash="dashdot"),
                marker=dict(size=5, symbol="diamond"),
                hovertemplate=(
                    "<b>%{x|%Y-%m-%d}</b><br>"
                    f"Aggregate Brier ({label}): %{{y:.2f}}<extra></extra>"
                ),
            ))

    fig_brier.update_layout(
        height=360,
        yaxis=dict(range=[0, 0.5], title="Brier Score", gridcolor="#F1F5F9"),
        xaxis=dict(title="Prediction Date"),
        margin=dict(t=20, b=50, l=60, r=120),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        legend=dict(orientation="h", y=-0.25, font=dict(size=11)),
        hovermode="x unified",
    )
    return fig_brier


def build_logloss_trend_chart(fdf, horizons_present, horizon_sel, roll_window, rolling_df,
                               h_colors, h_labels, show_points: bool = True,
                               show_aggregate: bool = True) -> go.Figure:
    fig_ll = go.Figure()
    ll_baseline = _math.log(2)

    fig_ll.add_hline(
        y=ll_baseline, line_dash="dot", line_color="#94A3B8", opacity=0.6,
        annotation_text=f"Coin-flip baseline ({ll_baseline:.2f})",
        annotation_font_size=10, annotation_position="right",
    )
    fig_ll.add_hline(
        y=0.5, line_dash="dot", line_color="#16A34A", opacity=0.5,
        annotation_text="Useful signal (0.50)",
        annotation_font_size=10, annotation_position="right",
    )

    for h in sorted(horizons_present):
        if h not in horizon_sel:
            continue
        sub = fdf[fdf["horizon_days"] == h].sort_values("prediction_date").dropna(subset=["log_loss"])
        if sub.empty:
            continue
        color = h_colors.get(int(h), "#64748B")
        label = h_labels.get(int(h), f"{int(h)}d")

        if show_points:
            fig_ll.add_trace(go.Scatter(
                x=sub["prediction_date"], y=sub["log_loss"],
                mode="markers",
                name=f"{label} — each prediction",
                marker=dict(size=7, color=color, opacity=0.5,
                            line=dict(color=color, width=1)),
                hovertemplate=(
                    "<b>%{x|%Y-%m-%d}</b><br>"
                    f"Horizon: {label}<br>"
                    "Log-Loss: %{y:.2f}<extra></extra>"
                ),
            ))

        if len(sub) >= 3:
            roll = sub["log_loss"].rolling(roll_window, min_periods=2).mean()
            fig_ll.add_trace(go.Scatter(
                x=sub["prediction_date"], y=roll,
                mode="lines",
                name=f"{label} — rolling avg",
                line=dict(color=color, width=2.5),
                hovertemplate=(
                    "<b>%{x|%Y-%m-%d}</b><br>"
                    f"Rolling avg ({roll_window}): %{{y:.2f}}<extra></extra>"
                ),
            ))

    if show_aggregate and not rolling_df.empty and "mean_log_loss" in rolling_df.columns:
        for h in sorted(horizons_present):
            rsub = rolling_df[rolling_df["horizon_days"] == h].dropna(subset=["mean_log_loss"])
            if rsub.empty:
                continue
            color = h_colors.get(int(h), "#64748B")
            label = h_labels.get(int(h), f"{int(h)}d")
            fig_ll.add_trace(go.Scatter(
                x=rsub["metric_date"], y=rsub["mean_log_loss"],
                mode="lines+markers",
                name=f"{label} — rolling metric",
                line=dict(color=color, width=2, dash="dashdot"),
                marker=dict(size=5, symbol="diamond"),
                hovertemplate=(
                    "<b>%{x|%Y-%m-%d}</b><br>"
                    f"Aggregate Log-Loss ({label}): %{{y:.2f}}<extra></extra>"
                ),
            ))

    fig_ll.update_layout(
        height=360,
        yaxis=dict(range=[0, 1.2], title="Log-Loss", gridcolor="#F1F5F9"),
        xaxis=dict(title="Prediction Date"),
        margin=dict(t=20, b=50, l=60, r=140),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        legend=dict(orientation="h", y=-0.25, font=dict(size=11)),
        hovermode="x unified",
    )
    return fig_ll


def build_directional_accuracy_chart(fdf, horizons_present, horizon_sel, roll_window, rolling_df,
                                      h_colors, h_labels, show_aggregate: bool = True) -> go.Figure:
    fig_dir = go.Figure()
    fig_dir.add_hline(
        y=0.5, line_dash="dot", line_color="#EF4444", opacity=0.6,
        annotation_text="50% baseline (random)", annotation_font_size=10,
    )
    fig_dir.add_hline(
        y=0.6, line_dash="dot", line_color="#16A34A", opacity=0.4,
        annotation_text="60% target", annotation_font_size=10,
    )

    fdf2 = fdf.copy()
    fdf2["dir_correct"] = fdf2["outcome"].apply(
        lambda o: 1 if o in ("strong_correct", "directionally_correct", "flat_correct") else 0
    )

    for h in sorted(horizons_present):
        if h not in horizon_sel:
            continue
        sub = fdf2[fdf2["horizon_days"] == h].sort_values("prediction_date")
        if len(sub) < 2:
            continue
        color = h_colors.get(int(h), "#64748B")
        label = h_labels.get(int(h), f"{int(h)}d")
        roll  = sub["dir_correct"].rolling(roll_window, min_periods=2).mean()
        fig_dir.add_trace(go.Scatter(
            x=sub["prediction_date"], y=roll,
            mode="lines+markers",
            name=f"{label}",
            line=dict(color=color, width=2.5),
            marker=dict(size=5, color=color),
            hovertemplate=(
                "<b>%{x|%Y-%m-%d}</b><br>"
                f"Rolling dir accuracy ({label}): %{{y:.2%}}<extra></extra>"
            ),
        ))

    if show_aggregate and not rolling_df.empty and "directional_accuracy" in rolling_df.columns:
        for h in sorted(horizons_present):
            rsub = rolling_df[rolling_df["horizon_days"] == h].dropna(subset=["directional_accuracy"])
            if rsub.empty:
                continue
            color = h_colors.get(int(h), "#64748B")
            label = h_labels.get(int(h), f"{int(h)}d")
            fig_dir.add_trace(go.Scatter(
                x=rsub["metric_date"], y=rsub["directional_accuracy"],
                mode="lines+markers",
                name=f"{label} — rolling metric",
                line=dict(color=color, width=2, dash="dashdot"),
                marker=dict(size=5, symbol="diamond"),
                hovertemplate=(
                    "<b>%{x|%Y-%m-%d}</b><br>"
                    f"Aggregate dir acc ({label}): %{{y:.2%}}<extra></extra>"
                ),
            ))

    fig_dir.update_layout(
        height=320,
        yaxis=dict(range=[0, 1.05], title="Directional Accuracy", tickformat=".2%", gridcolor="#F1F5F9"),
        xaxis=dict(title="Prediction Date"),
        margin=dict(t=20, b=50, l=60, r=120),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        legend=dict(orientation="h", y=-0.25, font=dict(size=11)),
        hovermode="x unified",
    )
    return fig_dir


def build_predicted_vs_actual_scatter(scatter_df: pd.DataFrame) -> go.Figure:
    scatter_df = scatter_df.copy()
    scatter_df["pred_mid"] = (
        scatter_df["predicted_return_low"] + scatter_df["predicted_return_high"]
    ) / 2.0

    OUTCOME_COLORS = {
        "strong_correct":        "#059669",
        "directionally_correct": "#22C55E",
        "flat_correct":          "#3B82F6",
        "wrong_minor":           "#F59E0B",
        "wrong_significant":     "#EF4444",
        "data_missing":          "#94A3B8",
    }

    fig_scatter = go.Figure()

    # Perfect diagonal
    all_vals = pd.concat([scatter_df["pred_mid"], scatter_df["actual_return"]]).dropna()
    if not all_vals.empty:
        v_min, v_max = all_vals.min() * 100 - 1, all_vals.max() * 100 + 1
        fig_scatter.add_trace(go.Scatter(
            x=[v_min, v_max], y=[v_min, v_max],
            mode="lines",
            line=dict(color="#94A3B8", dash="dash", width=1.5),
            name="Perfect prediction",
            hoverinfo="skip",
        ))
        fig_scatter.add_hline(y=0, line_color="#64748B", line_width=1, opacity=0.4)
        fig_scatter.add_vline(x=0, line_color="#64748B", line_width=1, opacity=0.4)

    for outcome, grp in scatter_df.groupby("outcome"):
        color = OUTCOME_COLORS.get(str(outcome), "#94A3B8")
        fig_scatter.add_trace(go.Scatter(
            x=grp["pred_mid"] * 100,
            y=grp["actual_return"] * 100,
            mode="markers",
            name=str(outcome).replace("_", " ").title(),
            marker=dict(
                size=grp["conviction_score"].fillna(5).apply(lambda s: max(6, min(18, s * 1.5))),
                color=color,
                opacity=0.8,
                line=dict(color=color, width=1),
            ),
            hovertemplate=(
                "<b>%{customdata[0]}</b> — %{customdata[1]}<br>"
                "Predicted: %{x:.2f}%<br>"
                "Actual: %{y:.2f}%<br>"
                "Conviction: %{customdata[2]:.2f}/10<extra></extra>"
            ),
            customdata=list(zip(
                grp["ticker"].fillna("?"),
                grp["prediction_date"].dt.strftime("%Y-%m-%d"),
                grp["conviction_score"].fillna(0),
            )),
        ))

    fig_scatter.update_layout(
        height=420,
        xaxis=dict(title="Predicted Return Midpoint (%)", gridcolor="#F1F5F9", zeroline=False),
        yaxis=dict(title="Actual Return (%)", gridcolor="#F1F5F9", zeroline=False),
        margin=dict(t=20, b=60, l=60, r=30),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        legend=dict(orientation="h", y=-0.22, font=dict(size=11)),
    )
    return fig_scatter


# ── Heatmap ─────────────────────────────────────────────────────────────────

def build_accuracy_heatmap(heatmap_data: list[dict]) -> go.Figure:
    segments = sorted({r["segment"] for r in heatmap_data})
    h_order  = [h for h in [5, 21, 63]
                if h in {r["horizon_days"] for r in heatmap_data}]

    z_matrix, text_matrix = [], []
    for seg in segments:
        z_row, t_row = [], []
        for h in h_order:
            cell = next((r for r in heatmap_data
                         if r["segment"] == seg and r["horizon_days"] == h), None)
            if cell and (cell.get("n") or 0) >= 5:
                acc = cell["dir_acc"]
                z_row.append(acc if acc is not None else None)
                t_row.append(f"{(acc or 0)*100:.2f}%\nN={cell['n']}")
            else:
                z_row.append(None)
                t_row.append("N/A")
        z_matrix.append(z_row)
        text_matrix.append(t_row)

    fig = go.Figure(go.Heatmap(
        z=z_matrix,
        x=[f"{h}d" for h in h_order],
        y=segments,
        text=text_matrix,
        texttemplate="%{text}",
        colorscale="RdYlGn",
        zmin=0.4, zmax=0.7,
        colorbar=dict(title="Directional<br>Accuracy"),
    ))
    fig.update_layout(
        title="Directional Accuracy by Segment × Horizon",
        xaxis_title="Horizon",
        yaxis_title="Segment",
        height=max(320, len(segments) * 60 + 160),
    )
    return fig


# ── Calibration charts ────────────────────────────────────────────────────────

def build_reliability_diagram(reliability: list[dict]) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=[0, 1], y=[0, 1],
        mode="lines",
        line=dict(color="#94A3B8", dash="dash", width=1.5),
        name="Perfect calibration",
    ))
    fig.add_trace(go.Scatter(
        x=[0, 1, 1, 0], y=[0, 0, 1, 0],
        fill="toself", fillcolor="rgba(239,68,68,0.06)",
        line=dict(width=0), name="Overconfident zone",
    ))
    fig.add_trace(go.Scatter(
        x=[0, 0, 1, 0], y=[0, 1, 1, 0],
        fill="toself", fillcolor="rgba(34,197,94,0.06)",
        line=dict(width=0), name="Underconfident zone",
    ))
    fig.add_trace(go.Scatter(
        x=[d["mean_stated"] for d in reliability],
        y=[d["actual_hit_rate"] for d in reliability],
        mode="lines+markers",
        marker=dict(
            size=[max(8, min(24, d["n"] / 3)) for d in reliability],
            color="#2563EB",
            line=dict(color="#1D4ED8", width=1),
        ),
        line=dict(color="#2563EB", width=2),
        text=[f"bin {d['bin_label']}<br>N={d['n']}<br>stated {d['mean_stated']:.2%}<br>actual {d['actual_hit_rate']:.2%}"
              for d in reliability],
        hovertemplate="%{text}<extra></extra>",
        name="APEX calibration",
    ))
    fig.update_layout(
        xaxis_title="Stated p_up Confidence",
        yaxis_title="Actual 'Up' Hit Rate",
        xaxis=dict(range=[-0.02, 1.02], tickformat=".2%"),
        yaxis=dict(range=[-0.02, 1.12], tickformat=".2%"),
        height=480,
        legend=dict(orientation="h", y=-0.18),
        margin=dict(t=20),
    )
    return fig


def build_conviction_calibration_chart(conviction_cal: list[dict]) -> go.Figure:
    fig2 = go.Figure()
    fig2.add_trace(go.Scatter(
        x=[0, 10], y=[0.5, 1.0],
        mode="lines", line=dict(color="#94A3B8", dash="dash", width=1),
        name="Perfect calibration",
    ))
    fig2.add_trace(go.Scatter(
        x=[0, 10], y=[0.5, 0.5],
        mode="lines", line=dict(color="#EF4444", dash="dot", width=1),
        name="50% random baseline",
    ))
    fig2.add_trace(go.Scatter(
        x=[d["mid"] for d in conviction_cal],
        y=[d["accuracy"] for d in conviction_cal],
        mode="lines+markers+text",
        text=[f"N={d['n']}" for d in conviction_cal],
        textposition="top center",
        marker=dict(size=10, color="#2563EB"),
        line=dict(color="#2563EB", width=2),
        name="Observed accuracy",
    ))
    fig2.update_layout(
        xaxis_title="Conviction Score Bucket (0–10)",
        yaxis_title="Directional Accuracy",
        yaxis=dict(range=[0, 1.15], tickformat=".2%"),
        xaxis=dict(range=[0, 10]),
        height=360,
        legend=dict(orientation="h", y=-0.2),
        margin=dict(t=10),
    )
    return fig2


# ── Distribution histograms ───────────────────────────────────────────────────

def build_brier_histogram(b_vals: list[float]) -> go.Figure:
    fig_bh = go.Figure(go.Histogram(
        x=b_vals, nbinsx=20,
        marker_color="#2563EB", opacity=0.75,
        hovertemplate="Brier: %{x:.2f}<br>Count: %{y}<extra></extra>",
    ))
    fig_bh.add_vline(x=0.20, line_dash="dot", line_color="#D97706",
                     annotation_text="0.20 threshold")
    fig_bh.add_vline(x=0.18, line_dash="dot", line_color="#16A34A",
                     annotation_text="0.18 skill")
    fig_bh.update_layout(
        title="Brier Score Distribution",
        xaxis_title="Brier Score", yaxis_title="Count",
        height=300, margin=dict(t=40, b=40),
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
    )
    return fig_bh


def build_logloss_histogram(ll_vals: list[float]) -> go.Figure:
    fig_llh = go.Figure(go.Histogram(
        x=ll_vals, nbinsx=20,
        marker_color="#7C3AED", opacity=0.75,
        hovertemplate="Log-Loss: %{x:.2f}<br>Count: %{y}<extra></extra>",
    ))
    fig_llh.add_vline(x=_math.log(2), line_dash="dot", line_color="#94A3B8",
                      annotation_text="Coin-flip baseline")
    fig_llh.add_vline(x=0.5, line_dash="dot", line_color="#16A34A",
                      annotation_text="0.50 useful signal")
    fig_llh.update_layout(
        title="Log-Loss Distribution",
        xaxis_title="Log-Loss", yaxis_title="Count",
        height=300, margin=dict(t=40, b=40),
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
    )
    return fig_llh


# ── Drift / volume charts ─────────────────────────────────────────────────────

def build_drift_accuracy_chart(drift_5d: list[dict]) -> go.Figure:
    dates = [d["date"] for d in drift_5d]
    accs  = [d["accuracy"] for d in drift_5d]
    ns    = [d["n"] for d in drift_5d]
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=dates, y=[0.5]*len(dates),
        mode="lines", line=dict(color="#EF4444", dash="dot"),
        name="50% baseline",
    ))
    fig.add_trace(go.Scatter(
        x=dates, y=accs,
        mode="lines+markers",
        text=[f"N={n}" for n in ns],
        hovertemplate="%{x}<br>Accuracy: %{y:.2%}<br>%{text}",
        marker=dict(size=6, color="#2563EB"),
        line=dict(color="#2563EB", width=2),
        name="Rolling accuracy",
    ))
    fig.update_layout(
        yaxis=dict(range=[0, 1], tickformat=".2%"),
        height=350, margin=dict(t=20, b=40),
    )
    return fig


def build_volume_by_horizon_chart(vol_data: list[dict]) -> go.Figure:
    df_vol = pd.DataFrame(vol_data)
    h_in_vol = sorted(df_vol["horizon_days"].unique())
    colors = {5: "#2563EB", 21: "#7C3AED", 63: "#059669"}
    fig = go.Figure()
    for h in h_in_vol:
        sub = df_vol[df_vol["horizon_days"] == h]
        fig.add_trace(go.Bar(
            x=sub["pred_date"], y=sub["n"],
            name=f"{h}d",
            marker_color=colors.get(h, "#64748B"),
        ))
    fig.update_layout(
        barmode="stack",
        xaxis_title="Date", yaxis_title="Predictions",
        height=350, margin=dict(t=20, b=40),
    )
    return fig
