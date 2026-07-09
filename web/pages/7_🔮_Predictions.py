"""APEX Predictions — card-based layout with action items, grouped accordion, and drill-down."""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from web.styles import (
    inject_global_css, page_header, section_title, top_nav,
    SUCCESS, SUCCESS_LIGHT, WARNING, WARNING_LIGHT, DANGER, DANGER_LIGHT,
    PRIMARY, PRIMARY_LIGHT, NEUTRAL, NEUTRAL_LIGHT, PURPLE, PURPLE_LIGHT,
    REC_STYLES, PRED_COLORS,
)

_DB = _ROOT / "data" / "portfolio.db"

st.set_page_config(page_title="APEX Predictions", page_icon="🔮", layout="wide")
inject_global_css()
top_nav("predictions")


# ── DB helpers ────────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(_DB))
    c.row_factory = sqlite3.Row
    return c


def _load_predictions(
    selected_date: date | None = None,
    horizon_days: int | None = None,
    min_conviction: float = 0,
) -> pd.DataFrame:
    """Load latest prediction per ticker/horizon for the given date (or all dates if None)."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT id, ticker, as_of_date, recommendation, prediction,
                      confidence, composite_score, horizon_days, prediction_type,
                      predicted_direction, predicted_return_low, predicted_return_high,
                      conviction_score, fundamental_score, research_score, macro_score,
                      news_score, reasoning, reasoning_text, panel_summary,
                      model_name, model_provider, evaluation_status, outcome,
                      actual_return, actual_direction, pt_current_price, pt_mean,
                      pt_num_analysts, start_price, changed_from_previous,
                      previous_prediction, p_strong_down, p_moderate_down, p_flat,
                      p_moderate_up, p_strong_up, created_at, evaluation_date,
                      risk_segment, system_version, snapshot_fundamentals_score,
                      snapshot_news_score, snapshot_research_score, snapshot_macro_score,
                      snapshot_news_headlines, brier_score,
                      trigger_type, trigger_event_id
               FROM predictions
               WHERE as_of_date IS NOT NULL
               ORDER BY as_of_date DESC, created_at DESC""",
        ).fetchall()

    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df

    if selected_date is not None:
        df = df[df["as_of_date"] == selected_date.isoformat()]

    if horizon_days is not None:
        df = df[df["horizon_days"] == horizon_days]

    if min_conviction > 0:
        df = df[df["conviction_score"].fillna(0) >= min_conviction]

    # Latest per ticker+horizon
    df = (
        df.sort_values("created_at", ascending=False)
          .drop_duplicates(subset=["ticker", "horizon_days"])
          .sort_values(["ticker", "horizon_days"])
    )
    return df.reset_index(drop=True)


def _load_ticker_history(ticker: str) -> pd.DataFrame:
    with _conn() as conn:
        rows = conn.execute(
            """SELECT id, ticker, as_of_date, recommendation, prediction,
                      confidence, composite_score, horizon_days,
                      predicted_direction, predicted_return_low, predicted_return_high,
                      conviction_score, evaluation_status, outcome, actual_return,
                      start_price, created_at
               FROM predictions
               WHERE ticker = ? AND as_of_date IS NOT NULL
               ORDER BY as_of_date ASC, created_at ASC""",
            (ticker,),
        ).fetchall()
    return pd.DataFrame([dict(r) for r in rows])


def _load_edge_data(horizon_days: int, risk_segment: str | None) -> dict | None:
    """Query metrics_rolling for accuracy at given horizon + segment."""
    with _conn() as conn:
        if risk_segment:
            row = conn.execute(
                """SELECT directional_accuracy, num_predictions
                   FROM metrics_rolling
                   WHERE horizon_days = ? AND segment = ?
                   ORDER BY computed_at DESC LIMIT 1""",
                (horizon_days, risk_segment),
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT directional_accuracy, num_predictions
                   FROM metrics_rolling
                   WHERE horizon_days = ?
                   ORDER BY computed_at DESC LIMIT 1""",
                (horizon_days,),
            ).fetchone()
    return dict(row) if row else None


def _load_system_stats(selected_date: date) -> dict:
    """Load stats for the system strip."""
    date_str = selected_date.isoformat()
    with _conn() as conn:
        total_today = conn.execute(
            "SELECT COUNT(DISTINCT ticker) FROM predictions WHERE as_of_date = ?",
            (date_str,),
        ).fetchone()[0]

        latest_row = conn.execute(
            "SELECT created_at, system_version FROM predictions WHERE as_of_date IS NOT NULL ORDER BY created_at DESC LIMIT 1"
        ).fetchone()

        total_tickers = conn.execute(
            "SELECT COUNT(DISTINCT ticker) FROM predictions WHERE as_of_date IS NOT NULL"
        ).fetchone()[0]

        n_matured = conn.execute(
            "SELECT COUNT(*) FROM predictions WHERE evaluation_status = 'evaluated'"
        ).fetchone()[0]

        brier_row = conn.execute(
            "SELECT AVG(brier_score) FROM predictions WHERE horizon_days = 5 AND brier_score IS NOT NULL"
        ).fetchone()

    return {
        "latest_created_at": dict(latest_row)["created_at"] if latest_row else "—",
        "system_version": dict(latest_row)["system_version"] if latest_row else "v1.0",
        "total_today": total_today,
        "total_tickers": total_tickers,
        "n_matured": n_matured,
        "brier_5d": brier_row[0] if brier_row and brier_row[0] else None,
    }


def _latest_as_of_date() -> date:
    """Return the most recent as_of_date in the DB, falling back to today."""
    try:
        with _conn() as conn:
            row = conn.execute(
                "SELECT MAX(as_of_date) FROM predictions WHERE as_of_date IS NOT NULL"
            ).fetchone()
        if row and row[0]:
            return date.fromisoformat(row[0])
    except Exception:
        pass
    return date.today()


# ── Company name lookup ───────────────────────────────────────────────────────

_COMPANY_NAMES: dict[str, str] = {
    # Portfolio
    "AAPL":  "Apple",
    "AJG":   "Arthur J. Gallagher",
    "AMZN":  "Amazon",
    "COST":  "Costco",
    "GOOG":  "Alphabet (C)",
    "GOOGL": "Alphabet (A)",
    "IVV":   "iShares S&P 500 ETF",
    "KO":    "Coca-Cola",
    "LLY":   "Eli Lilly",
    "LULU":  "Lululemon",
    "META":  "Meta Platforms",
    "MSFT":  "Microsoft",
    "NVDA":  "NVIDIA",
    "TSLA":  "Tesla",
    "VOO":   "Vanguard S&P 500 ETF",
    "WMT":   "Walmart",
    "XOM":   "ExxonMobil",
    # Watchlist — semis & hardware
    "AMD":   "AMD",
    "INTC":  "Intel",
    "ASML":  "ASML Holding",
    "MU":    "Micron Technology",
    "AMAT":  "Applied Materials",
    "KLAC":  "KLA Corporation",
    "LRCX":  "Lam Research",
    "MRVL":  "Marvell Technology",
    "MCHP":  "Microchip Technology",
    "NXPI":  "NXP Semiconductors",
    "ON":    "ON Semiconductor",
    "MPWR":  "Monolithic Power",
    "ARM":   "Arm Holdings",
    "SMCI":  "Super Micro Computer",
    "QCOM":  "Qualcomm",
    # Watchlist — software & cloud
    "PANW":  "Palo Alto Networks",
    "CRWD":  "CrowdStrike",
    "FTNT":  "Fortinet",
    "ZS":    "Zscaler",
    "NET":   "Cloudflare",
    "SNPS":  "Synopsys",
    "CDNS":  "Cadence Design",
    "WDAY":  "Workday",
    "ADBE":  "Adobe",
    "CRM":   "Salesforce",
    "ORCL":  "Oracle",
    "NOW":   "ServiceNow",
    "INTU":  "Intuit",
    "SNOW":  "Snowflake",
    "DDOG":  "Datadog",
    "HUBS":  "HubSpot",
    # Watchlist — fintech & payments
    "SHOP":  "Shopify",
    "SQ":    "Block",
    "PYPL":  "PayPal",
    "COIN":  "Coinbase",
    "HOOD":  "Robinhood",
    "SOFI":  "SoFi Technologies",
    "AFRM":  "Affirm",
    "UPST":  "Upstart",
    # Watchlist — consumer & travel
    "ABNB":  "Airbnb",
    "UBER":  "Uber",
    "DASH":  "DoorDash",
    "RIVN":  "Rivian",
    "NIO":   "NIO",
    # Watchlist — media & social
    "PLTR":  "Palantir",
    "RBLX":  "Roblox",
    "APP":   "Applovin",
    "DUOL":  "Duolingo",
    "ROKU":  "Roku",
    "PINS":  "Pinterest",
    "SNAP":  "Snap",
    "SPOT":  "Spotify",
    "TTD":   "The Trade Desk",
    "SE":    "Sea Limited",
    # Watchlist — other
    "IONQ":  "IonQ",
    "AGNC":  "AGNC Investment",
    "AXT":   "AXT Inc.",
    "AAR":   "AAR Corp",
    "BP":    "BP",
    "RXO":   "RXO Inc.",
    "HP":    "Helmerich & Payne",
    "BMW":   "BMW AG",
    "SPCX":  "SPCX",
}


def _cname(ticker: str) -> str:
    """Return company name for display, or empty string if unknown."""
    return _COMPANY_NAMES.get(ticker.upper(), "")


# ── Pure helper functions ─────────────────────────────────────────────────────

import math as _math

def _clean_row(d: dict) -> dict:
    """Replace float NaN (from pandas .to_dict()) with None so guard clauses work correctly."""
    return {k: (None if isinstance(v, float) and _math.isnan(v) else v) for k, v in d.items()}


def _conviction_meter(conv: float | None) -> str:
    if conv is None:
        return "░" * 10
    n = max(0, min(10, int(round(conv))))
    return "█" * n + "░" * (10 - n)


def _score_arrow(score: float | None) -> str:
    if score is None:
        return ""
    if score >= 8:
        return "▲▲"
    if score >= 7:
        return "▲"
    if score <= 4:
        return "▼"
    return ""


def _pred_icon(prediction: str) -> str:
    if prediction == "BULLISH":
        return "▲"
    if prediction == "BEARISH":
        return "▼"
    return "◐"


def _dir_icon(direction: str) -> str:
    if direction == "UP":
        return "▲"
    if direction == "DOWN":
        return "▼"
    return "◐"


def _multi_horizon_pattern(horizon_rows: list[dict]) -> str:
    predictions = [r.get("prediction", "") for r in horizon_rows]
    bullish = [p for p in predictions if p == "BULLISH"]
    bearish = [p for p in predictions if p == "BEARISH"]
    if len(bullish) == len(predictions):
        return "Consistent bullish across horizons"
    if len(bearish) == len(predictions):
        return "Consistent bearish across horizons"
    if predictions and predictions[0] == "BULLISH" and all(
        p in ("NEUTRAL", "BEARISH") for p in predictions[1:]
    ):
        return "Near-term bullish, long-term uncertain"
    if len(set(predictions)) > 1:
        return "Mixed signals across horizons"
    return "Consistent outlook across horizons"


def _score_color(score: float | None) -> str:
    if score is None:
        return NEUTRAL
    if score >= 7:
        return SUCCESS
    if score >= 5:
        return WARNING
    return DANGER


# ── Plotly charts ─────────────────────────────────────────────────────────────

_PROB_BUCKETS = [
    ("p_strong_down",   "#EF4444", "Significant Drop",  "< −5%"),
    ("p_moderate_down", "#F97316", "Mild Decline",       "−5% to −1%"),
    ("p_flat",          "#94A3B8", "Flat / Sideways",    "−1% to +1%"),
    ("p_moderate_up",   "#22C55E", "Moderate Gain",      "+1% to +5%"),
    ("p_strong_up",     "#059669", "Strong Rally",       "> +5%"),
]


def _prob_strip_html(row: dict, height: int = 14) -> str:
    """Render a gradient color strip for the probability distribution.

    Replaces mini bar charts — reads left-to-right as bearish → bullish.
    The dominant bucket gets a small label above it.
    """
    vals = [row.get(key) or 0 for key, *_ in _PROB_BUCKETS]
    total = sum(vals) or 100
    pcts  = [v / total * 100 for v in vals]

    # Dominant bucket
    max_idx = pcts.index(max(pcts))

    # Build strip segments
    segments = "".join(
        f'<div title="{label} ({range_}): {v:.0f}%" '
        f'style="width:{pct:.1f}%;background:{color};height:{height}px"></div>'
        for (_, color, label, range_), pct, v in zip(_PROB_BUCKETS, pcts, vals)
    )

    # Percentage labels below the strip
    label_cells = "".join(
        f'<div style="width:{pct:.1f}%;text-align:center;font-size:0.6rem;'
        f'color:{color};font-weight:{"700" if i == max_idx else "400"}">'
        f'{v:.0f}%</div>'
        for i, ((_, color, *_rest), pct, v) in enumerate(zip(_PROB_BUCKETS, pcts, vals))
    )

    # Bullish / bearish summary chips
    p_up   = vals[3] + vals[4]
    p_down = vals[0] + vals[1]
    p_flat = vals[2]

    return (
        f'<div style="margin:4px 0">'
        f'<div style="display:flex;border-radius:6px;overflow:hidden">{segments}</div>'
        f'<div style="display:flex;margin-top:1px">{label_cells}</div>'
        f'<div style="display:flex;gap:8px;margin-top:4px">'
        f'<span style="font-size:0.65rem;color:#059669">📈 {p_up:.0f}% bullish</span>'
        f'<span style="font-size:0.65rem;color:#94A3B8">➡ {p_flat:.0f}% flat</span>'
        f'<span style="font-size:0.65rem;color:#EF4444">📉 {p_down:.0f}% bearish</span>'
        f'</div>'
        f'</div>'
    )


def _plot_prob_full(row: dict) -> go.Figure:
    """Full probability distribution — horizontal bars with meaningful labels and % annotations."""
    data = [(key, color, label, range_) for key, color, label, range_ in _PROB_BUCKETS]
    display_labels = [f"{label}  {range_}" for _, _, label, range_ in data[::-1]]
    values_r       = [(row.get(key) or 0) for key, *_ in data[::-1]]
    colors_r       = [color for _, color, *_ in data[::-1]]

    fig = go.Figure()
    for y_label, val, color in zip(display_labels, values_r, colors_r):
        fig.add_trace(go.Bar(
            y=[y_label],
            x=[val],
            orientation="h",
            marker_color=color,
            marker_line=dict(color=color, width=0),
            text=[f"<b>{val:.0f}%</b>"],
            textposition="outside",
            textfont=dict(size=12, color="#1E293B"),
            showlegend=False,
            hovertemplate=f"<b>{y_label}</b>: {val:.0f}%<extra></extra>",
        ))

    p_up   = (row.get("p_moderate_up") or 0) + (row.get("p_strong_up") or 0)
    p_down = (row.get("p_strong_down") or 0) + (row.get("p_moderate_down") or 0)

    fig.update_layout(
        height=260,
        margin=dict(l=10, r=65, t=30, b=10),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        xaxis=dict(range=[0, 120], showgrid=False, showticklabels=False, zeroline=False),
        yaxis=dict(showgrid=False, tickfont=dict(size=11, color="#334155")),
        bargap=0.3,
        title=dict(
            text=f"📈 <b>{p_up:.0f}%</b> bullish   ➡ <b>{row.get('p_flat') or 0:.0f}%</b> flat   📉 <b>{p_down:.0f}%</b> bearish",
            font=dict(size=12, color="#475569"),
            x=0, xanchor="left",
        ),
    )
    return fig


def _plot_score_radar(row: dict) -> go.Figure:
    categories = ["Fundamentals", "Research", "Macro", "News Sentiment"]
    values = [
        row.get("fundamental_score") or 0,
        row.get("research_score") or 0,
        row.get("macro_score") or 0,
        row.get("news_score") or 0,
    ]
    cats_closed = categories + [categories[0]]
    vals_closed = values + [values[0]]

    fig = go.Figure()
    fig.add_trace(go.Scatterpolar(
        r=vals_closed,
        theta=cats_closed,
        fill="toself",
        fillcolor="rgba(37, 99, 235, 0.12)",
        line=dict(color="#2563EB", width=2),
        marker=dict(size=5, color="#2563EB"),
        hovertemplate="<b>%{theta}</b>: %{r:.1f}/10<extra></extra>",
    ))
    fig.update_layout(
        height=220,
        margin=dict(l=20, r=20, t=20, b=20),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        polar=dict(
            bgcolor="rgba(0,0,0,0)",
            radialaxis=dict(
                visible=True, range=[0, 10],
                tickfont=dict(size=8, color="#94A3B8"),
                gridcolor="#E2E8F0", linecolor="#E2E8F0",
            ),
            angularaxis=dict(
                tickfont=dict(size=10, color="#475569"),
                linecolor="#E2E8F0", gridcolor="#E2E8F0",
            ),
        ),
        showlegend=False,
    )
    return fig


# ── Section renderers ─────────────────────────────────────────────────────────

def render_system_strip(df_all: pd.DataFrame, selected_date: date) -> None:
    """Piece 1: System strip showing status, last run, coverage."""
    stats = _load_system_stats(selected_date)
    n_today = int((df_all["as_of_date"] == selected_date.isoformat()).sum()) if not df_all.empty else 0

    if n_today > 0:
        dot = "🟢"
        status_text = f"Predictions available for {selected_date}"
    else:
        dot = "🔴"
        status_text = f"No predictions for {selected_date}"

    brier_str = f"{stats['brier_5d']:.3f}" if stats["brier_5d"] is not None else "—"
    last_run = stats["latest_created_at"]
    version = stats["system_version"] or "v1.0"
    n_pred = stats["total_today"]
    total_t = stats["total_tickers"]
    n_matured = stats["n_matured"]

    strip_html = (
        f'<div style="background:white;border:1px solid #E2E8F0;border-radius:10px;'
        f'padding:10px 18px;margin-bottom:16px;display:flex;align-items:center;'
        f'justify-content:space-between;box-shadow:0 1px 3px rgba(0,0,0,0.05)">'
        f'<div style="display:flex;align-items:center;gap:10px">'
        f'<span style="font-size:1rem">{dot}</span>'
        f'<span style="font-size:0.82rem;color:#475569">{status_text}</span>'
        f'<span style="color:#CBD5E1">|</span>'
        f'<span style="font-size:0.82rem;color:#64748B">Last run: <b style="color:#0F172A">{last_run}</b></span>'
        f'<span style="color:#CBD5E1">·</span>'
        f'<span style="font-size:0.82rem;color:#64748B"><b style="color:#0F172A">{n_pred}</b> predictions today</span>'
        f'<span style="color:#CBD5E1">·</span>'
        f'<span style="font-size:0.82rem;color:#64748B"><b style="color:#0F172A">{version}</b></span>'
        f'</div>'
        f'<div style="display:flex;align-items:center;gap:6px">'
        f'<span style="font-size:0.78rem;color:#64748B">Coverage: <b style="color:#0F172A">{n_today}/{total_t}</b> tickers</span>'
        f'<span style="color:#CBD5E1">·</span>'
        f'<span style="font-size:0.78rem;color:#64748B">Matured: <b style="color:#0F172A">{n_matured}</b></span>'
        f'<span style="color:#CBD5E1">·</span>'
        f'<span style="font-size:0.78rem;color:#64748B">Brier Score (5d): <b style="color:#0F172A">{brier_str}</b></span>'
        f'</div>'
        f'</div>'
    )
    st.markdown(strip_html, unsafe_allow_html=True)


def render_action_item_card(row: dict, card_key: str) -> None:
    """Single compact card for Section 1 action items."""
    ticker = row.get("ticker", "")
    h_days = row.get("horizon_days")
    h_str = f"{h_days}d" if h_days else "—"
    conf = row.get("confidence") or row.get("conviction_score")
    conf_str = str(int(conf)) if conf is not None else "—"
    comp = row.get("composite_score")
    comp_str = f"{comp:.1f}" if comp is not None else "—"
    lo = row.get("predicted_return_low")
    hi = row.get("predicted_return_high")
    ret_str = f"{lo:+.1f}% to {hi:+.1f}%" if lo is not None and hi is not None else "—"
    conv = row.get("conviction_score")
    meter = _conviction_meter(conv)
    conv_disp = f"{conv:.0f}" if conv is not None else "—"
    pred = row.get("prediction", "")
    rec = row.get("recommendation", "")
    pred_icon = _pred_icon(pred)
    rec_color, rec_bg, rec_label = REC_STYLES.get(rec, (NEUTRAL, NEUTRAL_LIGHT, rec))
    pred_color, pred_bg = PRED_COLORS.get(pred, (NEUTRAL, NEUTRAL_LIGHT))

    with st.container(border=True):
        c1, c2 = st.columns([4, 1.5])
        with c1:
            _name = _cname(ticker)
            _name_html = f'<span style="font-size:0.78rem;color:#94A3B8;font-weight:400">{_name}</span>' if _name else ''
            st.markdown(
                f'<div style="display:flex;align-items:baseline;gap:10px;flex-wrap:wrap">'
                f'<span style="font-size:1.1rem;font-weight:800;color:#0F172A">{ticker}</span>'
                f'{_name_html}'
                f'<span style="background:{PRIMARY_LIGHT};color:{PRIMARY};padding:1px 7px;'
                f'border-radius:10px;font-size:0.72rem;font-weight:700">{h_str}</span>'
                f'<span style="font-size:0.78rem;color:#64748B">Confidence <b>{conf_str}</b></span>'
                f'<span style="font-size:0.78rem;color:#64748B">Composite Score <b>{comp_str}</b></span>'
                f'</div>',
                unsafe_allow_html=True,
            )
            st.markdown(
                f'<div style="font-size:0.8rem;color:#475569;margin:3px 0">'
                f'Predicted: <b>{ret_str}</b></div>',
                unsafe_allow_html=True,
            )
            st.markdown(
                f'<div style="font-family:monospace;font-size:0.82rem;color:{_score_color(conv)};'
                f'margin:2px 0">{meter} ({conv_disp}/10)</div>',
                unsafe_allow_html=True,
            )
            st.markdown(
                f'<span style="background:{pred_bg};color:{pred_color};padding:2px 9px;'
                f'border-radius:4px;font-size:0.75rem;font-weight:700;margin-right:6px">'
                f'{pred_icon} {pred}</span>'
                f'<span style="background:{rec_bg};color:{rec_color};padding:2px 9px;'
                f'border-radius:4px;font-size:0.75rem;font-weight:700">{rec_label}</span>',
                unsafe_allow_html=True,
            )
        with c2:
            if st.button("View Details", key=card_key, use_container_width=True):
                st.session_state["drill_ticker"] = ticker
                st.session_state["drill_date"] = row.get("as_of_date")
                st.rerun()


def render_action_items(df: pd.DataFrame, title: str = "Today's Action Items") -> None:
    """Piece 3: Section 1 — Today's Action Items (STRONG_BUY + STRONG_SELL, conv >= 7)."""
    section_title(title, badge_text="High Conviction", badge_color=PRIMARY)

    action_df = df[
        df["recommendation"].isin(["STRONG_BUY", "STRONG_SELL"]) &
        (df["conviction_score"].fillna(0) >= 7)
    ]

    if action_df.empty:
        st.info("No high-conviction STRONG BUY or STRONG SELL signals for this date.", icon="🔮")
        return

    strong_buy = action_df[action_df["recommendation"] == "STRONG_BUY"]
    strong_sell = action_df[action_df["recommendation"] == "STRONG_SELL"]

    if not strong_buy.empty:
        st.markdown(
            f'<div style="font-size:0.88rem;font-weight:700;color:{SUCCESS};margin:8px 0 6px">'
            f'▲ STRONG BUY ({len(strong_buy)})</div>',
            unsafe_allow_html=True,
        )
        cols = st.columns(min(len(strong_buy), 3))
        for i, (_, row_s) in enumerate(strong_buy.iterrows()):
            with cols[i % 3]:
                render_action_item_card(
                    _clean_row(row_s.to_dict()),
                    card_key=f"action_sb_{row_s['ticker']}_{row_s.get('horizon_days', 0)}_{i}",
                )

    if not strong_sell.empty:
        st.markdown(
            f'<div style="font-size:0.88rem;font-weight:700;color:{DANGER};margin:12px 0 6px">'
            f'▼ STRONG SELL ({len(strong_sell)})</div>',
            unsafe_allow_html=True,
        )
        cols = st.columns(min(len(strong_sell), 3))
        for i, (_, row_s) in enumerate(strong_sell.iterrows()):
            with cols[i % 3]:
                render_action_item_card(
                    _clean_row(row_s.to_dict()),
                    card_key=f"action_ss_{row_s['ticker']}_{row_s.get('horizon_days', 0)}_{i}",
                )


def render_edge_indicator(row: dict) -> str:
    """Return edge indicator HTML string for a prediction row."""
    h_days = row.get("horizon_days")
    segment = row.get("risk_segment")
    if h_days is None:
        return '<span style="font-size:0.72rem;color:#94A3B8">ⓘ no horizon data</span>'
    edge = _load_edge_data(int(h_days), segment)
    if edge is None:
        return '<span style="font-size:0.72rem;color:#94A3B8">ⓘ insufficient validation data</span>'
    acc = edge.get("directional_accuracy")
    n = edge.get("num_predictions", 0)
    if acc is None:
        return '<span style="font-size:0.72rem;color:#94A3B8">ⓘ insufficient validation data</span>'
    seg_label = segment or "all"
    if n < 10:
        return f'<span style="font-size:0.72rem;color:#94A3B8">⚠ insufficient data (n={n})</span>'
    if acc < 0.52:
        return (
            f'<span style="font-size:0.72rem;color:{WARNING}">⚠ no edge '
            f'({h_days}d segment {acc*100:.0f}%)</span>'
        )
    return (
        f'<span style="font-size:0.72rem;color:{SUCCESS}">✓ {h_days}d segment '
        f'{acc*100:.0f}% (n={n})</span>'
    )


def render_single_horizon_card(row: dict, card_key: str) -> None:
    """Card for a ticker that appears in only one horizon."""
    ticker = row.get("ticker", "")
    h_days = row.get("horizon_days")
    h_str = f"{h_days}d" if h_days else "—"
    pred = row.get("prediction", "")
    pred_icon = _pred_icon(pred)
    conf = row.get("conviction_score")
    conf_str = f"{conf:.0f}" if conf is not None else "—"
    lo = row.get("predicted_return_low")
    hi = row.get("predicted_return_high")
    ret_str = f"{lo:+.1f}% to {hi:+.1f}%" if lo is not None and hi is not None else "—"
    comp = row.get("composite_score")
    comp_str = f"{comp:.1f}" if comp is not None else "—"
    pred_color, pred_bg = PRED_COLORS.get(pred, (NEUTRAL, NEUTRAL_LIGHT))

    f_score = row.get("fundamental_score")
    r_score = row.get("research_score")
    m_score = row.get("macro_score")
    n_score = row.get("news_score")

    cp = row.get("start_price") or row.get("pt_current_price")
    pt = row.get("pt_mean")
    upside_str = ""
    if cp and pt and cp > 0:
        upside = (pt - cp) / cp * 100
        upside_str = f"${cp:.2f} → PT ${pt:.2f} ({upside:+.1f}%)"

    edge_html = render_edge_indicator(row)

    has_prob = any(
        row.get(k) is not None
        for k in ["p_strong_down", "p_moderate_down", "p_flat", "p_moderate_up", "p_strong_up"]
    )

    with st.container(border=True):
        top_c, btn_c = st.columns([4.5, 1.5])
        with top_c:
            _name = _cname(ticker)
            _name_html = f'<span style="font-size:0.75rem;color:#94A3B8;font-weight:400">{_name}</span>' if _name else ''
            st.markdown(
                f'<div style="display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;margin-bottom:4px">'
                f'<span style="font-size:1rem;font-weight:800;color:#0F172A">{ticker}</span>'
                f'{_name_html}'
                f'<span style="background:{PRIMARY_LIGHT};color:{PRIMARY};padding:1px 7px;'
                f'border-radius:10px;font-size:0.7rem;font-weight:700">{h_str}</span>'
                f'<span style="background:{pred_bg};color:{pred_color};padding:1px 8px;'
                f'border-radius:4px;font-size:0.72rem;font-weight:700">{pred_icon} {pred}</span>'
                f'<span style="font-size:0.75rem;color:#64748B">Confidence <b>{conf_str}</b></span>'
                f'</div>',
                unsafe_allow_html=True,
            )
            st.markdown(
                f'<div style="font-size:0.78rem;color:#475569;margin:2px 0">'
                f'Predicted: <b>{ret_str}</b>'
                + (f' &nbsp;|&nbsp; {upside_str}' if upside_str else "")
                + f'</div>',
                unsafe_allow_html=True,
            )
            if has_prob:
                st.markdown(_prob_strip_html(row), unsafe_allow_html=True)

            driver_parts = []
            for label, score in [("Fundamentals", f_score), ("Research", r_score), ("Macro", m_score), ("News", n_score)]:
                arrow = _score_arrow(score)
                s_str = str(int(score)) if score is not None else "—"
                driver_parts.append(f'<span style="font-size:0.72rem;color:#475569">{label} <b>{s_str}</b>{arrow}</span>')
            st.markdown(
                '<div style="display:flex;gap:10px;margin-top:4px">' + "".join(driver_parts) + "</div>",
                unsafe_allow_html=True,
            )
            st.markdown(
                f'<div style="margin-top:3px">Edge: {edge_html}</div>',
                unsafe_allow_html=True,
            )
        with btn_c:
            if st.button("View Details", key=card_key, use_container_width=True):
                st.session_state["drill_ticker"] = ticker
                st.session_state["drill_date"] = row.get("as_of_date")
                st.rerun()


def render_multi_horizon_card(ticker: str, horizon_rows: list[dict], card_key: str) -> None:
    """Card for a ticker that appears in multiple horizons."""
    first = horizon_rows[0]
    pred = first.get("prediction", "")
    f_score = first.get("fundamental_score")
    r_score = first.get("research_score")
    m_score = first.get("macro_score")
    n_score = first.get("news_score")

    pattern = _multi_horizon_pattern(horizon_rows)

    with st.container(border=True):
        top_c, btn_c = st.columns([4.5, 1.5])
        with top_c:
            _name = _cname(ticker)
            _name_html = f'<span style="font-size:0.75rem;color:#94A3B8;font-weight:400">{_name}</span>' if _name else ''
            st.markdown(
                f'<div style="display:flex;align-items:baseline;gap:8px;margin-bottom:2px">'
                f'<span style="font-size:1rem;font-weight:800;color:#0F172A">{ticker}</span>'
                f'{_name_html}'
                f'</div>',
                unsafe_allow_html=True,
            )
            for row in horizon_rows:
                h_days = row.get("horizon_days")
                h_str = f"{h_days}d" if h_days else "—"
                p = row.get("prediction", "")
                p_icon = _pred_icon(p)
                p_color, p_bg = PRED_COLORS.get(p, (NEUTRAL, NEUTRAL_LIGHT))
                c = row.get("conviction_score")
                c_str = f"{c:.0f}" if c is not None else "—"
                lo = row.get("predicted_return_low")
                hi = row.get("predicted_return_high")
                ret_str = f"{lo:+.1f}% / {hi:+.1f}%" if lo is not None and hi is not None else "—"
                meter = _conviction_meter(c)
                st.markdown(
                    f'<div style="display:flex;align-items:center;gap:8px;margin:3px 0;'
                    f'padding:3px 6px;background:#F8FAFC;border-radius:5px">'
                    f'<span style="background:{PRIMARY_LIGHT};color:{PRIMARY};padding:1px 6px;'
                    f'border-radius:8px;font-size:0.68rem;font-weight:700;min-width:28px;text-align:center">{h_str}</span>'
                    f'<span style="background:{p_bg};color:{p_color};padding:1px 6px;'
                    f'border-radius:4px;font-size:0.68rem;font-weight:700">{p_icon} {p}</span>'
                    f'<span style="font-size:0.7rem;color:#64748B">Confidence <b>{c_str}</b></span>'
                    f'<span style="font-size:0.7rem;color:#475569">{ret_str}</span>'
                    f'<span style="font-family:monospace;font-size:0.68rem;color:#94A3B8">{meter}</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
            st.markdown(
                f'<div style="font-size:0.75rem;color:{PRIMARY};font-style:italic;margin:4px 0 2px">'
                f'Pattern: {pattern}</div>',
                unsafe_allow_html=True,
            )
            driver_parts = []
            for label, score in [("Fundamentals", f_score), ("Research", r_score), ("Macro", m_score), ("News", n_score)]:
                arrow = _score_arrow(score)
                s_str = str(int(score)) if score is not None else "—"
                driver_parts.append(
                    f'<span style="font-size:0.72rem;color:#475569">{label} <b>{s_str}</b>{arrow}</span>'
                )
            st.markdown(
                '<div style="display:flex;gap:10px;margin-top:4px">' + "".join(driver_parts) + "</div>",
                unsafe_allow_html=True,
            )
        with btn_c:
            if st.button("View Details", key=card_key, use_container_width=True):
                st.session_state["drill_ticker"] = ticker
                st.session_state["drill_date"] = first.get("as_of_date")
                st.rerun()


def render_all_predictions(df: pd.DataFrame, title: str = "All Predictions by Recommendation") -> None:
    """Piece 4: Section 2 — All Predictions grouped by recommendation in accordion."""
    section_title(title)

    rec_order = ["STRONG_BUY", "BUY", "HOLD", "SELL", "STRONG_SELL"]
    rec_labels = {
        "STRONG_BUY": "🟢 STRONG BUY",
        "BUY": "🟢 BUY",
        "HOLD": "🟡 HOLD",
        "SELL": "🔴 SELL",
        "STRONG_SELL": "🔴 STRONG SELL",
    }
    auto_open = {"STRONG_BUY", "BUY", "STRONG_SELL"}

    if df.empty:
        st.info("No predictions to display for the selected filters.", icon="🔮")
        return

    for rec in rec_order:
        rec_df = df[df["recommendation"] == rec]
        if rec_df.empty:
            continue

        n = len(rec_df["ticker"].unique())
        label = rec_labels.get(rec, rec)
        expanded = rec in auto_open

        with st.expander(f"{label} ({n})", expanded=expanded):
            # Group by ticker — show multi-horizon cards where applicable
            ticker_groups = rec_df.groupby("ticker")
            card_idx = 0
            for ticker, grp in ticker_groups:
                rows_list = [_clean_row(r) for r in grp.sort_values("horizon_days").to_dict("records")]
                if len(rows_list) > 1:
                    render_multi_horizon_card(
                        ticker=str(ticker),
                        horizon_rows=rows_list,
                        card_key=f"card_{rec}_{ticker}_{card_idx}",
                    )
                else:
                    render_single_horizon_card(
                        row=rows_list[0],
                        card_key=f"card_{rec}_{ticker}_{card_idx}",
                    )
                card_idx += 1


# ── Drill-down ────────────────────────────────────────────────────────────────

def render_drill_down(drill_ticker: str, drill_date: str | None) -> None:
    """Piece 5: Per-ticker drill-down panel."""
    st.markdown("---")
    _drill_name = _cname(drill_ticker)
    _drill_label = f"{drill_ticker} — {_drill_name}" if _drill_name else drill_ticker
    section_title(f"Drill-down: {_drill_label}", badge_text="Detail View", badge_color=PURPLE)

    # Load all rows for this ticker on this date (all horizons)
    with _conn() as conn:
        rows = conn.execute(
            """SELECT * FROM predictions
               WHERE ticker = ? AND (as_of_date = ? OR ? IS NULL)
               ORDER BY horizon_days ASC, created_at DESC""",
            (drill_ticker, drill_date, drill_date),
        ).fetchall()

    if not rows:
        st.warning(f"No predictions found for {drill_ticker} on {drill_date}.", icon="⚠️")
        if st.button("Clear selection", key="clear_drill"):
            del st.session_state["drill_ticker"]
            st.session_state.pop("drill_date", None)
            st.rerun()
        return

    # De-duplicate to latest per horizon
    seen: dict[Any, dict] = {}
    for r in rows:
        d = dict(r)
        h = d.get("horizon_days")
        if h not in seen:
            seen[h] = d
    drill_rows = list(seen.values())
    row = drill_rows[0]  # primary row for single-horizon fields

    # ── Header ────────────────────────────────────────────────────────────────
    rec = row.get("recommendation", "")
    pred = row.get("prediction", "")
    rec_color, rec_bg, rec_label = REC_STYLES.get(rec, (NEUTRAL, NEUTRAL_LIGHT, rec))
    pred_color, pred_bg = PRED_COLORS.get(pred, (NEUTRAL, NEUTRAL_LIGHT))
    h_days = row.get("horizon_days")
    h_str = f"{h_days}d" if h_days else "—"
    conv = row.get("conviction_score")
    comp = row.get("composite_score")
    pred_icon = _pred_icon(pred)

    close_col, _ = st.columns([1, 9])
    with close_col:
        if st.button("✕ Close", key="close_drill"):
            del st.session_state["drill_ticker"]
            st.session_state.pop("drill_date", None)
            st.rerun()

    _drill_cname = _cname(drill_ticker)
    _drill_cname_html = (
        f'<span style="font-size:1rem;color:#94A3B8;font-weight:400">{_drill_cname}</span>'
        if _drill_cname else ''
    )
    header_html = (
        f'<div style="background:white;border:1px solid #E2E8F0;border-radius:12px;'
        f'padding:16px 20px;margin-bottom:12px;box-shadow:0 1px 3px rgba(0,0,0,0.05)">'
        f'<div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap">'
        f'<span style="font-size:2rem;font-weight:900;color:#0F172A">{drill_ticker}</span>'
        f'{_drill_cname_html}'
        f'<span style="background:{rec_bg};color:{rec_color};padding:4px 14px;'
        f'border-radius:6px;font-size:0.88rem;font-weight:700">{rec_label}</span>'
        f'<span style="background:{pred_bg};color:{pred_color};padding:4px 14px;'
        f'border-radius:6px;font-size:0.88rem;font-weight:700">{pred_icon} {pred}</span>'
        f'<span style="background:{PRIMARY_LIGHT};color:{PRIMARY};padding:4px 10px;'
        f'border-radius:6px;font-size:0.82rem;font-weight:600">⏱ {h_str}</span>'
        + (f'<span style="background:{NEUTRAL_LIGHT};color:#334155;padding:4px 10px;'
           f'border-radius:6px;font-size:0.82rem;font-weight:600">Conviction {conv:.1f}/10</span>'
           if conv is not None else "")
        + (f'<span style="background:{PURPLE_LIGHT};color:{PURPLE};padding:4px 10px;'
           f'border-radius:6px;font-size:0.82rem;font-weight:600">Composite Score {comp:.2f}</span>'
           if comp is not None else "")
        + f'</div></div>'
    )
    st.markdown(header_html, unsafe_allow_html=True)

    # ── Reasoning ─────────────────────────────────────────────────────────────
    with st.expander("📝 Reasoning", expanded=True):
        reasoning = row.get("reasoning") or row.get("reasoning_text") or "—"
        st.markdown(
            f'<div style="background:#F8FAFC;border:1px solid #E2E8F0;padding:14px 18px;'
            f'border-radius:8px;font-size:0.85rem;color:#334155;line-height:1.65">'
            f'{reasoning}</div>',
            unsafe_allow_html=True,
        )
        st.caption(
            f"Model: **{row.get('model_name', '—')}** ({row.get('model_provider', '—')})  "
            f"· Changed from previous: {'Yes' if row.get('changed_from_previous') else 'No'}"
            + (f"  · Prev: {row['previous_prediction']}" if row.get("previous_prediction") else "")
        )

    # ── Inputs at prediction time ─────────────────────────────────────────────
    with st.expander("📊 Inputs at Prediction Time", expanded=True):
        f_score = row.get("fundamental_score")
        r_score = row.get("research_score")
        m_score = row.get("macro_score")
        n_score = row.get("news_score")
        pt = row.get("pt_mean")
        cp = row.get("start_price") or row.get("pt_current_price")
        upside = ((pt - cp) / cp * 100) if (pt and cp and cp > 0) else None
        upside_str = f"(+{upside:.1f}%)" if upside is not None else ""

        ic1, ic2, ic3, ic4 = st.columns(4)
        score_items = [
            (ic1, "Fundamentals", f_score, f"Snapshot score: {row.get('snapshot_fundamentals_score', '—')}"),
            (ic2, "Analyst Research", r_score, f"Price target: ${pt:.2f} {upside_str}" if pt else "Price target: —"),
            (ic3, "Macro Environment", m_score, "Interest rates, inflation, risk regime"),
            (ic4, "News Sentiment", n_score, f"Sentiment score: {row.get('snapshot_news_score', '—')}"),
        ]
        for col, label, score, detail in score_items:
            with col:
                s_color = _score_color(score)
                s_str = str(int(score)) if score is not None else "—"
                arrow = _score_arrow(score)
                st.markdown(
                    f'<div style="background:white;border:1px solid #E2E8F0;border-radius:8px;'
                    f'padding:12px 14px;text-align:center">'
                    f'<div style="font-size:0.72rem;font-weight:700;color:#94A3B8;'
                    f'text-transform:uppercase;margin-bottom:6px">{label}</div>'
                    f'<div style="font-size:1.8rem;font-weight:800;color:{s_color}">'
                    f'{s_str}<span style="font-size:1rem">{arrow}</span></div>'
                    f'<div style="font-size:0.72rem;color:#64748B;margin-top:4px">{detail}</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

    # ── Probability distribution (full) ──────────────────────────────────────
    has_prob = any(
        row.get(k) is not None
        for k in ["p_strong_down", "p_moderate_down", "p_flat", "p_moderate_up", "p_strong_up"]
    )
    with st.expander("📈 Return Probability Distribution", expanded=True):
        if has_prob:
            pc1, pc2 = st.columns([2, 1])
            with pc1:
                fig_prob = _plot_prob_full(row)
                st.plotly_chart(fig_prob, use_container_width=True, config={"displayModeBar": False})
            with pc2:
                bull_pct = (row.get("p_moderate_up") or 0) + (row.get("p_strong_up") or 0)
                bear_pct = (row.get("p_strong_down") or 0) + (row.get("p_moderate_down") or 0)
                flat_pct = row.get("p_flat") or 0
                st.markdown(
                    f'<div style="display:flex;flex-direction:column;gap:8px;margin-top:20px">'
                    f'<div style="background:{SUCCESS_LIGHT};color:{SUCCESS};padding:8px 14px;'
                    f'border-radius:8px;font-size:0.85rem;font-weight:700;text-align:center">'
                    f'📈 Bullish {bull_pct:.0f}%</div>'
                    f'<div style="background:{NEUTRAL_LIGHT};color:{NEUTRAL};padding:8px 14px;'
                    f'border-radius:8px;font-size:0.85rem;font-weight:700;text-align:center">'
                    f'➡️ Flat {flat_pct:.0f}%</div>'
                    f'<div style="background:{DANGER_LIGHT};color:{DANGER};padding:8px 14px;'
                    f'border-radius:8px;font-size:0.85rem;font-weight:700;text-align:center">'
                    f'📉 Bearish {bear_pct:.0f}%</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
        else:
            st.info("Probability distribution data not available for this prediction.", icon="📊")

    # ── Score radar ───────────────────────────────────────────────────────────
    with st.expander("🎯 Score Radar", expanded=False):
        rc1, rc2 = st.columns([1, 1])
        with rc1:
            fig_radar = _plot_score_radar(row)
            st.plotly_chart(fig_radar, use_container_width=True, config={"displayModeBar": False})
        with rc2:
            st.markdown(
                '<p style="font-size:0.82rem;font-weight:700;color:#0F172A;margin-bottom:8px">'
                'Score Breakdown</p>',
                unsafe_allow_html=True,
            )
            score_breakdown = [
                ("Composite Score",   row.get("composite_score")),
                ("Fundamentals",      row.get("fundamental_score")),
                ("Analyst Research",  row.get("research_score")),
                ("Macro Environment", row.get("macro_score")),
                ("News Sentiment",    row.get("news_score")),
            ]
            for lbl, val in score_breakdown:
                s_str = f"{val:.1f}" if val is not None else "—"
                pct = min(100, (val / 10) * 100) if val is not None else 0
                bar_color = _score_color(val)
                st.markdown(
                    f'<div style="display:flex;align-items:center;gap:8px;margin:4px 0">'
                    f'<span style="width:90px;font-size:0.75rem;color:#475569">{lbl}</span>'
                    f'<div style="flex:1;height:6px;background:#E2E8F0;border-radius:3px">'
                    f'<div style="width:{pct:.0f}%;height:100%;background:{bar_color};border-radius:3px"></div></div>'
                    f'<span style="font-size:0.78rem;font-weight:700;color:{bar_color};min-width:30px">{s_str}</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

    # ── Agent Panel ───────────────────────────────────────────────────────────
    with st.expander("🏛️ Agent Panel", expanded=True):
        try:
            panel = json.loads(row.get("panel_summary") or "{}")
        except Exception:
            panel = {}

        if panel and any(panel.get(k) for k in ["chen_verdict", "webb_verdict", "varga_verdict", "park_verdict"]):
            agents = [
                ("FUNDAMENTALS",  "Earnings, balance sheet & valuation", panel.get("chen_verdict", "—"),  PRIMARY),
                ("RESEARCH",      "Analyst targets & broker coverage",   panel.get("webb_verdict", "—"),  PURPLE),
                ("MACRO",         "Interest rates, inflation & regime",  panel.get("varga_verdict", "—"), WARNING),
                ("NEWS",          "Sentiment & recent headlines",        panel.get("park_verdict", "—"),  SUCCESS),
            ]
            pcols = st.columns(4)
            for pcol, (name, description, verdict, color) in zip(pcols, agents):
                with pcol:
                    m = re.search(r"\((\d+(?:\.\d+)?)/10\)", verdict)
                    score_val = m.group(1) if m else None
                    clean_verdict = re.sub(r"\(\d+(?:\.\d+)?/10\)\s*[—\-]?\s*", "", verdict).strip()
                    score_badge = (
                        f'<span style="background:{color}22;color:{color};padding:2px 8px;'
                        f'border-radius:10px;font-weight:700;font-size:0.78rem">{score_val}/10</span>'
                        if score_val else ""
                    )
                    st.markdown(
                        f'<div style="border:1px solid {color}44;border-top:3px solid {color};'
                        f'padding:10px 12px;border-radius:8px;background:{color}08;min-height:100px">'
                        f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">'
                        f'<span style="font-size:0.68rem;font-weight:700;color:{color};letter-spacing:0.06em">'
                        f'{name}</span>{score_badge}</div>'
                        f'<div style="font-size:0.68rem;color:#64748B;margin-bottom:4px">{description}</div>'
                        f'<div style="font-size:0.8rem;color:#1E293B;line-height:1.4">{clean_verdict}</div>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )
            if panel.get("key_debate"):
                st.markdown(
                    f'<div style="background:#F8FAFC;border:1px solid #E2E8F0;border-left:3px solid {PRIMARY};'
                    f'padding:8px 12px;border-radius:4px;margin-top:8px;font-size:0.82rem;color:#334155">'
                    f'<b>Key debate:</b> {panel["key_debate"]}</div>',
                    unsafe_allow_html=True,
                )
        else:
            st.caption("Agent panel summary not available for this prediction.")

    # ── Historical predictions for this ticker ────────────────────────────────
    with st.expander(f"📅 Historical Predictions — {drill_ticker}", expanded=False):
        hist_df = _load_ticker_history(drill_ticker)
        if hist_df.empty:
            st.info(f"No prediction history found for {drill_ticker}.", icon="📊")
        else:
            display_cols = [
                "as_of_date", "horizon_days", "recommendation", "prediction",
                "composite_score", "conviction_score", "evaluation_status", "outcome",
            ]
            rename_map = {
                "as_of_date":  "Prediction Date",
                "horizon_days":     "Forecast Horizon",
                "recommendation":   "Recommendation",
                "prediction":       "Sentiment",
                "composite_score":  "Composite Score",
                "conviction_score": "Conviction",
                "evaluation_status":"Status",
                "outcome":          "Outcome",
            }
            hist_display = hist_df[display_cols].rename(columns=rename_map).copy()
            hist_display["Forecast Horizon"] = hist_display["Forecast Horizon"].apply(
                lambda h: f"{int(h)}d" if pd.notna(h) else "—"
            )
            hist_display["Composite Score"] = hist_display["Composite Score"].round(2)
            hist_display["Conviction"]       = hist_display["Conviction"].round(1)
            hist_display["Status"]           = hist_display["Status"].fillna("pending")
            hist_display["Outcome"]          = hist_display["Outcome"].fillna("—")
            st.dataframe(hist_display, use_container_width=True, hide_index=True)

    # ── Edge data ─────────────────────────────────────────────────────────────
    with st.expander("🎯 Edge & Validation Data", expanded=False):
        h_days = row.get("horizon_days")
        segment = row.get("risk_segment")
        edge = _load_edge_data(int(h_days), segment) if h_days else None
        if edge:
            acc = edge.get("directional_accuracy")
            n = edge.get("num_predictions", 0)
            acc_str = f"{acc*100:.1f}%" if acc is not None else "—"
            edge_html = (
                f'<div style="background:{SUCCESS_LIGHT};border:1px solid {SUCCESS}44;'
                f'padding:12px 16px;border-radius:8px;">'
                f'<div style="font-size:0.82rem;color:#0F172A">'
                f'<b>{h_days}d horizon</b> · Segment: {segment or "all"} · '
                f'Directional accuracy: <b style="color:{SUCCESS}">{acc_str}</b> · '
                f'n={n} predictions</div></div>'
            ) if (acc is not None and (acc >= 0.52)) else (
                f'<div style="background:{WARNING_LIGHT};border:1px solid {WARNING}44;'
                f'padding:12px 16px;border-radius:8px;">'
                f'<div style="font-size:0.82rem;color:#0F172A">'
                f'<b>{h_days}d horizon</b> · Segment: {segment or "all"} · '
                f'Directional accuracy: <b style="color:{WARNING}">{acc_str}</b> · '
                f'n={n} (⚠ below baseline)</div></div>'
            )
            st.markdown(edge_html, unsafe_allow_html=True)
        else:
            st.info(
                "No validation metrics available for this horizon/segment. "
                "Run the evaluation pipeline to generate accuracy data.",
                icon="ℹ️",
            )
        st.markdown(
            f'<div style="margin-top:8px;font-size:0.78rem;color:#64748B">'
            f'For full validation history, visit the '
            f'<a href="/6_🎯_Validation" style="color:{PRIMARY}">Validation page</a>.</div>',
            unsafe_allow_html=True,
        )


# ── Main page ─────────────────────────────────────────────────────────────────

if not _DB.exists():
    page_header("APEX Predictions", icon="🔮")
    st.warning("Database not found. Run `python main.py --daily` to initialise it.", icon="⚠️")
    st.stop()

# ── Piece 2: Controls Bar ────────────────────────────────────────────────────
ctrl1, ctrl2, ctrl3, ctrl4, ctrl5, ctrl6 = st.columns([2, 1, 1.5, 2, 2, 1])

with ctrl1:
    _default_date = _latest_as_of_date()
    selected_date = st.date_input(
        "Prediction date",
        value=_default_date,
        max_value=max(_default_date, date.today()),
        help="Show predictions for this date",
    )

with ctrl2:
    horizon_choice = st.selectbox(
        "Horizon",
        options=["All", "5d", "21d", "63d"],
        index=0,
        help="Filter by prediction horizon",
    )

with ctrl3:
    trigger_choice = st.selectbox(
        "Trigger",
        options=["All", "scheduled", "event"],
        index=0,
        help="Filter by how the prediction was triggered",
    )

with ctrl4:
    min_conv = st.slider(
        "Min Conviction Score",
        min_value=0.0,
        max_value=10.0,
        value=0.0,
        step=0.5,
        help="Minimum conviction score (0 = show all)",
    )

with ctrl5:
    hide_no_edge = st.checkbox(
        "Hide low-edge segments",
        value=False,
        help="Hide predictions where validation accuracy < 52%",
    )

with ctrl6:
    if st.button("🔄 Refresh", use_container_width=True):
        st.rerun()

# Map horizon choice to int or None
_HORIZON_MAP = {"All": None, "5d": 5, "21d": 21, "63d": 63}
horizon_days_filter = _HORIZON_MAP[horizon_choice]

# ── Load data ─────────────────────────────────────────────────────────────────
df_today = _load_predictions(
    selected_date=selected_date,
    horizon_days=horizon_days_filter,
    min_conviction=min_conv,
)

# Load all-date data for the system strip denominator
df_all = _load_predictions(selected_date=None)

# Apply trigger filter
if trigger_choice != "All" and not df_today.empty and "trigger_type" in df_today.columns:
    if trigger_choice == "scheduled":
        df_today = df_today[df_today["trigger_type"].fillna("").str.startswith("scheduled")]
    elif trigger_choice == "event":
        df_today = df_today[df_today["trigger_type"].fillna("").str.startswith("event")]
    df_today = df_today.reset_index(drop=True)

# Optionally filter out low-edge rows
if hide_no_edge and not df_today.empty:
    def _has_edge(row: pd.Series) -> bool:
        h = row.get("horizon_days")
        seg = row.get("risk_segment")
        if h is None:
            return True
        edge = _load_edge_data(int(h), seg)
        if edge is None:
            return True  # keep if no data (don't hide)
        acc = edge.get("directional_accuracy")
        n = edge.get("num_predictions", 0)
        if acc is None or n < 10:
            return True
        return acc >= 0.52

    df_today = df_today[df_today.apply(_has_edge, axis=1)].reset_index(drop=True)

# ── Piece 1: System Strip ─────────────────────────────────────────────────────
render_system_strip(df_all, selected_date)

# ── Split portfolio vs new opportunities ────────────────────────────────────
if not df_today.empty and "trigger_type" in df_today.columns:
    _opp_mask    = df_today["trigger_type"].fillna("") == "trending_opportunity"
    df_portfolio = df_today[~_opp_mask].reset_index(drop=True)
    df_opps      = df_today[_opp_mask].reset_index(drop=True)
else:
    df_portfolio = df_today
    df_opps      = pd.DataFrame()

# ── Section 1: Action Items (portfolio) ───────────────────────────────────────
render_action_items(df_portfolio)

st.markdown("---")

# ── Section 2: All Portfolio Predictions ──────────────────────────────────────
render_all_predictions(df_portfolio)

# ── Section 3: New Opportunities ──────────────────────────────────────────────
if not df_opps.empty:
    st.markdown("---")
    section_title("🌟 New Opportunities", badge_text="Trending Discovery", badge_color=PURPLE)
    n_opp_tickers = len(df_opps["ticker"].unique())
    st.caption(
        f"{n_opp_tickers} trending ticker{'s' if n_opp_tickers != 1 else ''} analyzed today — "
        "same full pipeline as portfolio: News · Research · Fundamentals · APEX predictions"
    )
    render_action_items(df_opps, title="High-Conviction Opportunities")
    render_all_predictions(df_opps, title="All Opportunities by Recommendation")

# ── Piece 5: Drill-down ───────────────────────────────────────────────────────
drill_ticker = st.session_state.get("drill_ticker")
drill_date = st.session_state.get("drill_date")

if drill_ticker:
    render_drill_down(drill_ticker, drill_date)
