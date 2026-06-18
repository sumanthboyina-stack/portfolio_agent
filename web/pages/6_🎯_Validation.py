"""
APEX Prediction Validation Dashboard — system health panel with metric trend charts.
"""
from __future__ import annotations

import math as _math
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

import pandas as pd
import plotly.graph_objects as go
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

_DB = _ROOT / "data" / "portfolio.db"
_LOGS = _ROOT / "logs"
_LOGS.mkdir(parents=True, exist_ok=True)
_MAIN = _ROOT / "main.py"

# ── Session state ─────────────────────────────────────────────────────────────

for _k, _v in [
    ("val_pid", None), ("val_log", None),
    ("val_weekly_pid", None), ("val_weekly_log", None),
]:
    if _k not in st.session_state:
        st.session_state[_k] = _v


def _pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
        return done == 0
    except ChildProcessError:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    except OSError:
        return False


def _start_subprocess(flag: str, label: str) -> tuple[int, Path]:
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = _LOGS / f"{ts}_{label}.log"
    lf       = open(log_path, "w", buffering=1)
    proc     = subprocess.Popen(
        ["python", "-u", str(_MAIN), flag],
        cwd=str(_ROOT),
        stdout=lf,
        stderr=subprocess.STDOUT,
    )
    return proc.pid, log_path


def _read_log(path) -> str:
    if not path or not Path(path).exists():
        return ""
    text = Path(path).read_text(errors="replace")
    if len(text) > 8000:
        lines = text.splitlines()
        return f"[… {len(lines) - 200} earlier lines omitted …]\n" + "\n".join(lines[-200:])
    return text


# ── DB helpers ────────────────────────────────────────────────────────────────

def _load_metric_series() -> pd.DataFrame:
    """Per-prediction brier_score + log_loss for evaluated rows."""
    if not _DB.exists():
        return pd.DataFrame()
    with sqlite3.connect(str(_DB)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
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
            ORDER BY as_of_date ASC
        """).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    if not df.empty:
        df["prediction_date"] = pd.to_datetime(df["prediction_date"])
        df["horizon_label"]   = df["horizon_days"].apply(
            lambda h: f"{h}d" if pd.notna(h) else "?"
        )
    return df


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


def _compute_filtered_metrics(model_names: list[str] | None, lookback: int) -> list[dict]:
    """
    Recompute rolling metrics directly from the predictions table, optionally
    filtered by model_name list.  Returns the same shape as get_rolling_metrics().
    """
    if not _DB.exists():
        return []
    model_clause = ""
    params: list = [lookback]
    if model_names:
        placeholders = ",".join("?" * len(model_names))
        model_clause = f"AND model_name IN ({placeholders})"
        params = [lookback] + list(model_names)

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
        GROUP BY horizon_days
    """
    with sqlite3.connect(str(_DB)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def _load_rolling_metrics_series() -> pd.DataFrame:
    """Historical rolling metrics from metrics_rolling table."""
    if not _DB.exists():
        return pd.DataFrame()
    with sqlite3.connect(str(_DB)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT metric_date, horizon_days, segment, lookback_days,
                   directional_accuracy, in_range_pct, mean_excess_return,
                   brier_score, mean_log_loss, num_predictions,
                   high_conviction_accuracy, low_conviction_accuracy
            FROM metrics_rolling
            ORDER BY metric_date ASC
        """).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    if not df.empty:
        df["metric_date"] = pd.to_datetime(df["metric_date"])
    return df


def _directional_correct(outcome: str | None) -> bool | None:
    if outcome is None:
        return None
    return outcome in ("strong_correct", "directionally_correct", "flat_correct")


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
    _HIGHER_MODELS = frozenset({'Claude-Sonnet', 'GPT-4o', 'Claude-Sonnet + GPT-4o'})

    _all_model_names = _get_model_names()
    _higher_names = [m for m in _all_model_names if m in _HIGHER_MODELS]
    _lower_names  = [m for m in _all_model_names if m not in _HIGHER_MODELS]

    # Options: "All models" + each higher model individually + "Lower reasoning" (grouped)
    _radio_options = ["All models"] + _higher_names + (["Lower reasoning"] if _lower_names else [])

    _model_sel = st.selectbox(
        "model_filter",
        _radio_options,
        index=0,
        label_visibility="collapsed",
        key="val_model_radio",
    )

    if _model_sel == "All models":
        _model_filter: list[str] | None = None
        st.caption("All predictions — no model filter applied")
    elif _model_sel == "Lower reasoning":
        _model_filter = _lower_names
        # Show individual names so user knows what's included in the group
        st.markdown(
            '<div style="margin-top:4px">' +
            "".join(
                f'<div style="font-size:0.68rem;color:#6B7280;padding:1px 0">⚡ {m}</div>'
                for m in _lower_names
            ) + '</div>',
            unsafe_allow_html=True,
        )
    else:
        # Individual higher-reasoning model selected
        _model_filter = [_model_sel]
        st.caption(f"🧠 Higher reasoning model")

# ── Page header ───────────────────────────────────────────────────────────────

st.title("🎯 APEX Prediction Validation")
st.caption(f"System version: `{CURRENT_SYSTEM_VERSION}` · Scores predictions after they mature")

if _model_filter:
    st.info(
        f"🔍 **Model filter active:** {', '.join(f'`{m}`' for m in _model_filter)}  ·  "
        f"All metrics and tables recalculated for selected model(s) only.",
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

# ── Metric config: definitions, tiers, scale ──────────────────────────────────

# Each entry: key → (display_label, higher_better, bar_max, bar_min, format_fn, definition, tier_table)
# bar values are normalized to 0–100 scale so all bars are visually comparable.
# Brier/Log-Loss are inverted: bar = (1 − val/bar_max) × 100 so higher bar = better.
_METRIC_CFG = {
    "directional_accuracy": dict(
        label="Directional Accuracy",
        higher=True, bar_max=1.0, bar_min=0.0,
        fmt=lambda v: f"{v*100:.1f}%",
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
        fmt=lambda v: f"{v*100:.1f}%",
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
        fmt=lambda v: f"{v*100:.1f}%",
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
        fmt=lambda v: f"{v*100:.1f}%",
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
        fmt=lambda v: f"{v:.4f}",
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
        fmt=lambda v: f"{v:.4f}",
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


def _render_scorecard_tiles(by_horizon: dict, all_horizons: list, horizon_labels: dict) -> None:
    """Render horizontal metric tiles — one row of cards per horizon, CSS hover tooltips."""
    metric_keys = list(_METRIC_CFG.keys())
    html = _TILE_CSS

    for h_days in all_horizons:
        row    = by_horizon[h_days]
        n      = row.get("num_predictions") or 0
        hlbl   = horizon_labels.get(h_days, f"{h_days}d")
        hcolor = _H_COLORS.get(h_days, "#64748B")
        if n == 0:
            n_label = "no evaluated predictions yet"
            n_warn  = ""
        elif n < 30:
            n_label = f"{n} evaluated predictions"
            n_warn  = "  ⚠️ fewer than 30 — metrics unreliable"
        else:
            n_label = f"{n} evaluated predictions"
            n_warn  = ""

        html += (
            f'<div class="sc-wrap">'
            f'<div class="sc-horizon-hdr" style="color:{hcolor};background:{hcolor}18;border-left:3px solid {hcolor}">'
            f'{hlbl} Horizon &nbsp;·&nbsp; {n_label}{n_warn}</div>'
            f'<div class="sc-row">'
        )

        for key in metric_keys:
            cfg  = _METRIC_CFG[key]
            val  = row.get(key)
            short_label = cfg["label"].split("  ↓")[0].split("  (")[0]

            if val is None:
                color, val_text, tier_lbl = "#475569", "—", "No data"
            else:
                tier_lbl, color = _get_tier(key, val)
                val_text = cfg["fmt"](val)

            # Tier rows inside tooltip — bold + white for the active one
            range_rows = "".join(
                f'<div class="tip-tier{" cur" if lbl == tier_lbl else ""}">'
                f'<span style="color:{tc}">●</span> {lbl}</div>'
                for _, tc, lbl in cfg["tiers"]
            )
            lower_note = " &nbsp;↓ lower is better" if not cfg["higher"] else ""
            ref_html   = (
                f'<div class="tip-ref">Reference: {cfg["ref_label"]}</div>'
                if cfg.get("ref_label") else ""
            )
            tip = (
                f'<div class="sc-tip">'
                f'<div class="tip-title">{cfg["label"]}</div>'
                f'<div class="tip-val" style="color:{color}">{val_text}{lower_note}</div>'
                f'<div class="tip-defn">{cfg["defn"]}</div>'
                f'<div class="tip-ranges-hdr">Performance tiers:</div>'
                f'{range_rows}{ref_html}</div>'
            )

            tier_short = tier_lbl.split("(")[0].strip()
            html += (
                f'<div class="sc-tile">'
                f'<div class="sc-tile-bar" style="background:{color}"></div>'
                f'<div class="sc-tile-label">{short_label}</div>'
                f'<div class="sc-tile-value" style="color:{color}">{val_text}</div>'
                f'<div class="sc-tile-tier">{tier_short}</div>'
                f'{tip}</div>'
            )

        html += '</div></div>'

    st.markdown(html, unsafe_allow_html=True)


# ── Tab 1: Scorecard ──────────────────────────────────────────────────────────

with tabs[0]:
    metrics = _compute_filtered_metrics(_model_filter, lookback)
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

    metric_df  = _load_metric_series()
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
            color = H_COLORS.get(int(h), "#64748B")
            label = H_LABELS.get(int(h), f"{int(h)}d")

            # Scatter: individual points
            fig_brier.add_trace(go.Scatter(
                x=sub["prediction_date"], y=sub["brier_score"],
                mode="markers",
                name=f"{label} — each prediction",
                marker=dict(size=7, color=color, opacity=0.5,
                            line=dict(color=color, width=1)),
                hovertemplate=(
                    "<b>%{x|%Y-%m-%d}</b><br>"
                    f"Horizon: {label}<br>"
                    "Brier: %{y:.4f}<extra></extra>"
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
                        f"Rolling avg ({roll_window}): %{{y:.4f}}<extra></extra>"
                    ),
                ))

        # Overlay rolling metrics if available
        if not rolling_df.empty and "brier_score" in rolling_df.columns:
            for h in sorted(horizons_present):
                rsub = rolling_df[rolling_df["horizon_days"] == h].dropna(subset=["brier_score"])
                if rsub.empty:
                    continue
                color = H_COLORS.get(int(h), "#64748B")
                label = H_LABELS.get(int(h), f"{int(h)}d")
                fig_brier.add_trace(go.Scatter(
                    x=rsub["metric_date"], y=rsub["brier_score"],
                    mode="lines+markers",
                    name=f"{label} — rolling metric",
                    line=dict(color=color, width=2, dash="dashdot"),
                    marker=dict(size=5, symbol="diamond"),
                    hovertemplate=(
                        "<b>%{x|%Y-%m-%d}</b><br>"
                        f"Aggregate Brier ({label}): %{{y:.4f}}<extra></extra>"
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
        st.plotly_chart(fig_brier, use_container_width=True)

        st.divider()

        # ── Log-Loss trend ─────────────────────────────────────────────────────
        st.subheader("Log-Loss — Per Prediction + Rolling Average")
        st.caption(
            "Lower is better. Baseline (coin flip / uniform) ≈ 0.693. "
            "Below 0.5 = model provides useful probability estimates."
        )

        fig_ll = go.Figure()
        ll_baseline = _math.log(2)

        fig_ll.add_hline(
            y=ll_baseline, line_dash="dot", line_color="#94A3B8", opacity=0.6,
            annotation_text=f"Coin-flip baseline ({ll_baseline:.3f})",
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
            color = H_COLORS.get(int(h), "#64748B")
            label = H_LABELS.get(int(h), f"{int(h)}d")

            fig_ll.add_trace(go.Scatter(
                x=sub["prediction_date"], y=sub["log_loss"],
                mode="markers",
                name=f"{label} — each prediction",
                marker=dict(size=7, color=color, opacity=0.5,
                            line=dict(color=color, width=1)),
                hovertemplate=(
                    "<b>%{x|%Y-%m-%d}</b><br>"
                    f"Horizon: {label}<br>"
                    "Log-Loss: %{y:.4f}<extra></extra>"
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
                        f"Rolling avg ({roll_window}): %{{y:.4f}}<extra></extra>"
                    ),
                ))

        if not rolling_df.empty and "mean_log_loss" in rolling_df.columns:
            for h in sorted(horizons_present):
                rsub = rolling_df[rolling_df["horizon_days"] == h].dropna(subset=["mean_log_loss"])
                if rsub.empty:
                    continue
                color = H_COLORS.get(int(h), "#64748B")
                label = H_LABELS.get(int(h), f"{int(h)}d")
                fig_ll.add_trace(go.Scatter(
                    x=rsub["metric_date"], y=rsub["mean_log_loss"],
                    mode="lines+markers",
                    name=f"{label} — rolling metric",
                    line=dict(color=color, width=2, dash="dashdot"),
                    marker=dict(size=5, symbol="diamond"),
                    hovertemplate=(
                        "<b>%{x|%Y-%m-%d}</b><br>"
                        f"Aggregate Log-Loss ({label}): %{{y:.4f}}<extra></extra>"
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
        st.plotly_chart(fig_ll, use_container_width=True)

        st.divider()

        # ── Directional accuracy rolling trend ────────────────────────────────
        st.subheader("Directional Accuracy — Rolling Average")
        st.caption("Rolling % of predictions where predicted direction matched actual direction. 50% = random baseline.")

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
            color = H_COLORS.get(int(h), "#64748B")
            label = H_LABELS.get(int(h), f"{int(h)}d")
            roll  = sub["dir_correct"].rolling(roll_window, min_periods=2).mean()
            fig_dir.add_trace(go.Scatter(
                x=sub["prediction_date"], y=roll,
                mode="lines+markers",
                name=f"{label}",
                line=dict(color=color, width=2.5),
                marker=dict(size=5, color=color),
                hovertemplate=(
                    "<b>%{x|%Y-%m-%d}</b><br>"
                    f"Rolling dir accuracy ({label}): %{{y:.1%}}<extra></extra>"
                ),
            ))

        if not rolling_df.empty and "directional_accuracy" in rolling_df.columns:
            for h in sorted(horizons_present):
                rsub = rolling_df[rolling_df["horizon_days"] == h].dropna(subset=["directional_accuracy"])
                if rsub.empty:
                    continue
                color = H_COLORS.get(int(h), "#64748B")
                label = H_LABELS.get(int(h), f"{int(h)}d")
                fig_dir.add_trace(go.Scatter(
                    x=rsub["metric_date"], y=rsub["directional_accuracy"],
                    mode="lines+markers",
                    name=f"{label} — rolling metric",
                    line=dict(color=color, width=2, dash="dashdot"),
                    marker=dict(size=5, symbol="diamond"),
                    hovertemplate=(
                        "<b>%{x|%Y-%m-%d}</b><br>"
                        f"Aggregate dir acc ({label}): %{{y:.1%}}<extra></extra>"
                    ),
                ))

        fig_dir.update_layout(
            height=320,
            yaxis=dict(range=[0, 1.05], title="Directional Accuracy", tickformat=".0%", gridcolor="#F1F5F9"),
            xaxis=dict(title="Prediction Date"),
            margin=dict(t=20, b=50, l=60, r=120),
            plot_bgcolor="rgba(0,0,0,0)",
            paper_bgcolor="rgba(0,0,0,0)",
            legend=dict(orientation="h", y=-0.25, font=dict(size=11)),
            hovermode="x unified",
        )
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
                        "Predicted: %{x:.1f}%<br>"
                        "Actual: %{y:.1f}%<br>"
                        "Conviction: %{customdata[2]:.0f}/10<extra></extra>"
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
                    t_row.append(f"{(acc or 0)*100:.0f}%\nN={cell['n']}")
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

    def _brier_tier(b):
        if b is None:   return "", "#64748B"
        if b < 0.15:    return "exceptional", "#059669"
        if b < 0.18:    return "genuine skill", "#16A34A"
        if b < 0.20:    return "mild edge", "#D97706"
        if b < 0.25:    return "barely better than coin flip", "#DC2626"
        return "actively bad", "#7F1D1D"

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
            text=[f"bin {d['bin_label']}<br>N={d['n']}<br>stated {d['mean_stated']:.0%}<br>actual {d['actual_hit_rate']:.0%}"
                  for d in reliability],
            hovertemplate="%{text}<extra></extra>",
            name="APEX calibration",
        ))
        fig.update_layout(
            xaxis_title="Stated p_up Confidence",
            yaxis_title="Actual 'Up' Hit Rate",
            xaxis=dict(range=[-0.02, 1.02], tickformat=".0%"),
            yaxis=dict(range=[-0.02, 1.12], tickformat=".0%"),
            height=480,
            legend=dict(orientation="h", y=-0.18),
            margin=dict(t=20),
        )
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
            yaxis=dict(range=[0, 1.15], tickformat=".0%"),
            xaxis=dict(range=[0, 10]),
            height=360,
            legend=dict(orientation="h", y=-0.2),
            margin=dict(t=10),
        )
        st.plotly_chart(fig2, use_container_width=True)

# ── Tab 5: Recent Predictions ─────────────────────────────────────────────────

with tabs[4]:
    preds = get_recent_evaluated_predictions(limit=500, lookback_days=lookback)
    # Apply global model filter first
    if _model_filter and preds:
        preds = [p for p in preds if p.get("model_name") in _model_filter]
    if not preds:
        st.info("No evaluated predictions yet." if not _model_filter else
                f"No evaluated predictions for model(s): {', '.join(_model_filter)}")
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
                        fig_bh = go.Figure(go.Histogram(
                            x=b_vals, nbinsx=20,
                            marker_color="#2563EB", opacity=0.75,
                            hovertemplate="Brier: %{x:.3f}<br>Count: %{y}<extra></extra>",
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
                        st.plotly_chart(fig_bh, use_container_width=True)
                with hc2:
                    if ll_vals:
                        fig_llh = go.Figure(go.Histogram(
                            x=ll_vals, nbinsx=20,
                            marker_color="#7C3AED", opacity=0.75,
                            hovertemplate="Log-Loss: %{x:.3f}<br>Count: %{y}<extra></extra>",
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
                hovertemplate="%{x}<br>Accuracy: %{y:.1%}<br>%{text}",
                marker=dict(size=6, color="#2563EB"),
                line=dict(color="#2563EB", width=2),
                name="Rolling accuracy",
            ))
            fig.update_layout(
                yaxis=dict(range=[0, 1], tickformat=".0%"),
                height=350, margin=dict(t=20, b=40),
            )
            st.plotly_chart(fig, use_container_width=True)

    with col_r:
        st.subheader("Prediction Volume by Horizon")
        if not vol_data:
            st.info("No volume data yet.")
        else:
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
