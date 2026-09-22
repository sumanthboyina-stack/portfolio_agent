"""
Predictions page — Streamlit rendering/card functions and their formatting helpers.

Contains:
  - render_system_strip()       : system status strip
  - render_action_item_card()   : single compact action-item card
  - render_action_items()       : Section 1 — high-conviction action items
  - render_edge_indicator()     : edge/validation indicator HTML for a row
  - render_single_horizon_card(): card for a ticker in one horizon
  - render_multi_horizon_card() : card for a ticker across multiple horizons
  - render_all_predictions()    : Section 2 — accordion grouped by recommendation
  - render_drill_down()         : per-ticker drill-down panel
  - helpers: _clean_row, _conviction_meter, _score_arrow, _pred_icon,
             _dir_icon, _multi_horizon_pattern, _score_color
"""

from __future__ import annotations

import json
import math as _math
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from web.styles import (
    section_title, material, icon_html, status_dot_html,
    SUCCESS, SUCCESS_LIGHT, WARNING, WARNING_LIGHT, DANGER, DANGER_LIGHT,
    PRIMARY, PRIMARY_LIGHT, NEUTRAL, NEUTRAL_LIGHT, PURPLE, PURPLE_LIGHT,
    REC_STYLES, PRED_COLORS,
)
from web.lib import db_conn as _conn
from web.data.predictions import _load_edge_data, _load_system_stats, _load_ticker_history, _cname
from web.components.predictions_charts import _plot_price_chart, _plot_prob_full, _plot_score_radar, _prob_strip_html


# ── Pure helper functions ─────────────────────────────────────────────────────

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


# ── Section renderers ─────────────────────────────────────────────────────────

def render_system_strip(df_all: pd.DataFrame, selected_date: date) -> None:
    """Piece 1: System strip showing status, last run, coverage."""
    stats = _load_system_stats(selected_date)
    n_today = int((df_all["as_of_date"] == selected_date.isoformat()).sum()) if not df_all.empty else 0

    if n_today > 0:
        dot = status_dot_html(SUCCESS)
        status_text = f"Predictions available for {selected_date}"
    else:
        dot = status_dot_html(DANGER)
        status_text = f"No predictions for {selected_date}"

    brier_str = f"{stats['brier_5d']:.2f}" if stats["brier_5d"] is not None else "—"
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
        f'{dot}'
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
    comp_str = f"{comp:.2f}" if comp is not None else "—"
    lo = row.get("predicted_return_low")
    hi = row.get("predicted_return_high")
    ret_str = f"{lo:+.2f}% to {hi:+.2f}%" if lo is not None and hi is not None else "—"
    conv = row.get("conviction_score")
    meter = _conviction_meter(conv)
    conv_disp = f"{conv:.2f}" if conv is not None else "—"
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
        st.info("No high-conviction STRONG BUY or STRONG SELL signals for this date.", icon=material("insights"))
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
        return f'<span style="font-size:0.72rem;color:#94A3B8">{icon_html("warning", 12)} insufficient data (n={n})</span>'
    if acc < 0.52:
        return (
            f'<span style="font-size:0.72rem;color:{WARNING}">{icon_html("warning", 12)} no edge '
            f'({h_days}d segment {acc*100:.2f}%)</span>'
        )
    return (
        f'<span style="font-size:0.72rem;color:{SUCCESS}">{icon_html("check_circle", 12)} {h_days}d segment '
        f'{acc*100:.2f}% (n={n})</span>'
    )


def render_single_horizon_card(row: dict, card_key: str) -> None:
    """Card for a ticker that appears in only one horizon."""
    ticker = row.get("ticker", "")
    h_days = row.get("horizon_days")
    h_str = f"{h_days}d" if h_days else "—"
    pred = row.get("prediction", "")
    pred_icon = _pred_icon(pred)
    conf = row.get("conviction_score")
    conf_str = f"{conf:.2f}" if conf is not None else "—"
    lo = row.get("predicted_return_low")
    hi = row.get("predicted_return_high")
    ret_str = f"{lo:+.2f}% to {hi:+.2f}%" if lo is not None and hi is not None else "—"
    comp = row.get("composite_score")
    comp_str = f"{comp:.2f}" if comp is not None else "—"
    pred_color, pred_bg = PRED_COLORS.get(pred, (NEUTRAL, NEUTRAL_LIGHT))

    f_score = row.get("fundamental_score")
    v_score = row.get("valuation_score")
    r_score = row.get("research_score")
    m_score = row.get("macro_score")
    n_score = row.get("news_score")

    cp = row.get("start_price") or row.get("pt_current_price")
    pt = row.get("pt_mean")
    upside_str = ""
    if cp and pt and cp > 0:
        upside = (pt - cp) / cp * 100
        upside_str = f"${cp:.2f} → PT ${pt:.2f} ({upside:+.2f}%)"

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
            for label, score in [("Fundamentals", f_score), ("Valuation", v_score), ("Research", r_score), ("Macro", m_score), ("News", n_score)]:
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
    v_score = first.get("valuation_score")
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
                c_str = f"{c:.2f}" if c is not None else "—"
                lo = row.get("predicted_return_low")
                hi = row.get("predicted_return_high")
                ret_str = f"{lo:+.2f}% / {hi:+.2f}%" if lo is not None and hi is not None else "—"
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
            for label, score in [("Fundamentals", f_score), ("Valuation", v_score), ("Research", r_score), ("Macro", m_score), ("News", n_score)]:
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
    # Plain labels — color already carries the BUY/HOLD/SELL signal via the
    # per-card badges rendered inside each expander (st.expander headers can't
    # render colored HTML dots, only plain text).
    rec_labels = {rec: REC_STYLES.get(rec, (None, None, rec))[2] for rec in rec_order}
    auto_open = {"STRONG_BUY", "BUY", "STRONG_SELL"}

    if df.empty:
        st.info("No predictions to display for the selected filters.", icon=material("insights"))
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
        st.warning(f"No predictions found for {drill_ticker} on {drill_date}.", icon=material("warning"))
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
        if st.button("Close", icon=material("close"), key="close_drill"):
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
           f'border-radius:6px;font-size:0.82rem;font-weight:600">Conviction {conv:.2f}/10</span>'
           if conv is not None else "")
        + (f'<span style="background:{PURPLE_LIGHT};color:{PURPLE};padding:4px 10px;'
           f'border-radius:6px;font-size:0.82rem;font-weight:600">Composite Score {comp:.2f}</span>'
           if comp is not None else "")
        + f'</div></div>'
    )
    st.markdown(header_html, unsafe_allow_html=True)

    # ── Reasoning ─────────────────────────────────────────────────────────────
    with st.expander("Reasoning", icon=material("notes"), expanded=True):
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
    with st.expander("Inputs at Prediction Time", icon=material("bar_chart"), expanded=True):
        f_score = row.get("fundamental_score")
        r_score = row.get("research_score")
        m_score = row.get("macro_score")
        n_score = row.get("news_score")
        pt = row.get("pt_mean")
        cp = row.get("start_price") or row.get("pt_current_price")
        upside = ((pt - cp) / cp * 100) if (pt and cp and cp > 0) else None
        upside_str = f"(+{upside:.2f}%)" if upside is not None else ""

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

    # ── Price chart — the pre-trade sanity check: where's price relative to the
    # range, breakout or bounce, where's support. Right next to the reasoning/
    # radar/probability blocks, not a separate site you have to tab out to.
    with st.expander("Price Chart", icon=material("trending_up"), expanded=True):
        _period_label = st.radio(
            "Period", ["1mo", "3mo", "6mo", "1y"], index=2,
            horizontal=True, key=f"price_chart_period_{drill_ticker}",
            label_visibility="collapsed",
        )
        fig_price = _plot_price_chart(drill_ticker, period=_period_label)
        if fig_price is not None:
            st.plotly_chart(fig_price, use_container_width=True, config={"displayModeBar": False})
        else:
            st.info(f"No price history available for {drill_ticker}.", icon=material("trending_up"))

    # ── Probability distribution (full) ──────────────────────────────────────
    has_prob = any(
        row.get(k) is not None
        for k in ["p_strong_down", "p_moderate_down", "p_flat", "p_moderate_up", "p_strong_up"]
    )
    with st.expander("Return Probability Distribution", icon=material("insights"), expanded=True):
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
                    f'{icon_html("trending_up", 15)} Bullish {bull_pct:.2f}%</div>'
                    f'<div style="background:{NEUTRAL_LIGHT};color:{NEUTRAL};padding:8px 14px;'
                    f'border-radius:8px;font-size:0.85rem;font-weight:700;text-align:center">'
                    f'{icon_html("trending_flat", 15)} Flat {flat_pct:.2f}%</div>'
                    f'<div style="background:{DANGER_LIGHT};color:{DANGER};padding:8px 14px;'
                    f'border-radius:8px;font-size:0.85rem;font-weight:700;text-align:center">'
                    f'{icon_html("trending_down", 15)} Bearish {bear_pct:.2f}%</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
        else:
            st.info("Probability distribution data not available for this prediction.", icon=material("bar_chart"))

    # ── Score radar ───────────────────────────────────────────────────────────
    with st.expander("Score Radar", icon=material("track_changes"), expanded=False):
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
                ("Valuation",         row.get("valuation_score")),
                ("Analyst Research",  row.get("research_score")),
                ("Macro Environment", row.get("macro_score")),
                ("News Sentiment",    row.get("news_score")),
            ]
            for lbl, val in score_breakdown:
                s_str = f"{val:.2f}" if val is not None else "—"
                pct = min(100, (val / 10) * 100) if val is not None else 0
                bar_color = _score_color(val)
                st.markdown(
                    f'<div style="display:flex;align-items:center;gap:8px;margin:4px 0">'
                    f'<span style="width:90px;font-size:0.75rem;color:#475569">{lbl}</span>'
                    f'<div style="flex:1;height:6px;background:#E2E8F0;border-radius:3px">'
                    f'<div style="width:{pct:.2f}%;height:100%;background:{bar_color};border-radius:3px"></div></div>'
                    f'<span style="font-size:0.78rem;font-weight:700;color:{bar_color};min-width:30px">{s_str}</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

    # ── Agent Panel ───────────────────────────────────────────────────────────
    with st.expander("Agent Panel", icon=material("account_balance"), expanded=True):
        try:
            panel = json.loads(row.get("panel_summary") or "{}")
        except Exception:
            panel = {}

        # The Valuation card gets a second badge (UNDERVALUED/FAIRLY_VALUED/
        # OVERVALUED) alongside its 1-10 score — unlike the other 4 analysts,
        # valuation has a natural cheap/fair/expensive framing that the raw
        # score alone doesn't convey, and it's the same label the Valuation
        # page shows as its headline. Best-effort: absent if not yet computed.
        _VALUATION_LABEL_STYLE = {
            "UNDERVALUED":   (SUCCESS, SUCCESS_LIGHT, "UNDERVALUED"),
            "FAIRLY_VALUED": (WARNING, WARNING_LIGHT, "FAIRLY VALUED"),
            "OVERVALUED":    (DANGER, DANGER_LIGHT, "OVERVALUED"),
        }
        valuation_label_badge = ""
        try:
            from portfolio_agent.tools.valuation_db import get_stored_valuation
            _stored_valuation = get_stored_valuation(drill_ticker)
            _val_label = _stored_valuation.get("valuation_label") if _stored_valuation else None
            if _val_label in _VALUATION_LABEL_STYLE:
                _vl_color, _vl_bg, _vl_text = _VALUATION_LABEL_STYLE[_val_label]
                valuation_label_badge = (
                    f'<span style="background:{_vl_bg};color:{_vl_color};padding:2px 8px;'
                    f'border-radius:10px;font-weight:700;font-size:0.72rem;margin-left:4px;'
                    f'display:inline-flex;align-items:center;gap:4px">'
                    f'{status_dot_html(_vl_color, 6)}{_vl_text}</span>'
                )
        except Exception:
            pass

        if panel and any(panel.get(k) for k in ["chen_verdict", "malhotra_verdict", "webb_verdict", "varga_verdict", "park_verdict"]):
            agents = [
                ("FUNDAMENTALS",  "Earnings, balance sheet & margins",       panel.get("chen_verdict", "—"),     PRIMARY, ""),
                ("VALUATION",     "DCF intrinsic value & margin of safety",  panel.get("malhotra_verdict", "—"), DANGER,  valuation_label_badge),
                ("RESEARCH",      "Analyst targets & broker coverage",       panel.get("webb_verdict", "—"),     PURPLE,  ""),
                ("MACRO",         "Interest rates, inflation & regime",      panel.get("varga_verdict", "—"),    WARNING, ""),
                ("NEWS",          "Sentiment & recent headlines",            panel.get("park_verdict", "—"),     SUCCESS, ""),
            ]
            pcols = st.columns(5)
            for pcol, (name, description, verdict, color, extra_badge) in zip(pcols, agents):
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
                        f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;flex-wrap:wrap;gap:4px">'
                        f'<span style="font-size:0.68rem;font-weight:700;color:{color};letter-spacing:0.06em">'
                        f'{name}</span><span>{score_badge}{extra_badge}</span></div>'
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

    # ── Why did it change? ────────────────────────────────────────────────────
    with st.expander("Why did it change?", icon=material("search"), expanded=False):
        from portfolio_agent.tools.prediction_db import get_score_change_breakdown

        change = get_score_change_breakdown(drill_ticker)
        if change is None:
            st.caption("Not enough prediction history yet to show a day-over-day breakdown.")
        else:
            y, t = change["yesterday"], change["today"]
            st.markdown(
                f'<div style="font-size:0.85rem;color:#334155;margin-bottom:10px">'
                f'<b>{y["recommendation"]} {y["composite_score"]:.2f}</b> ({y["as_of_date"]}) '
                f'&nbsp;→&nbsp; <b>{t["recommendation"]} {t["composite_score"]:.2f}</b> ({t["as_of_date"]})'
                f'</div>',
                unsafe_allow_html=True,
            )
            for b in change["breakdown"]:
                color = SUCCESS if b["contribution"] > 0 else (DANGER if b["contribution"] < 0 else NEUTRAL)
                sign = "+" if b["contribution"] > 0 else ""
                st.markdown(
                    f'<div style="display:flex;align-items:baseline;gap:10px;margin:4px 0">'
                    f'<span style="font-weight:800;color:{color};min-width:44px">{sign}{b["contribution"]:.2f}</span>'
                    f'<span style="font-weight:700;color:#0F172A;min-width:90px">{b["source"]}</span>'
                    f'<span style="font-size:0.82rem;color:#64748B">{b["rationale"] or "—"}</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
            net = change["net_change"]
            net_color = SUCCESS if (net or 0) > 0 else (DANGER if (net or 0) < 0 else NEUTRAL)
            st.markdown(
                f'<p style="margin-top:10px;font-size:0.85rem;font-weight:800;color:{net_color}">'
                f'Net change: {net:+.2f}</p>' if net is not None else '',
                unsafe_allow_html=True,
            )
            if not change["weights_available"]:
                st.caption(
                    "Exact weight mix wasn't recorded for one of these days — showing an "
                    "approximate breakdown using base weights."
                )

    # ── Historical predictions for this ticker ────────────────────────────────
    _drill_hist_name = _cname(drill_ticker)
    _drill_hist_label = f"{drill_ticker} ({_drill_hist_name})" if _drill_hist_name else drill_ticker
    with st.expander(f"Historical Predictions — {_drill_hist_label}", icon=material("calendar_month"), expanded=False):
        hist_df = _load_ticker_history(drill_ticker)
        if hist_df.empty:
            st.info(f"No prediction history found for {drill_ticker}.", icon=material("bar_chart"))
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
            hist_display["Conviction"]       = hist_display["Conviction"].round(2)
            hist_display["Status"]           = hist_display["Status"].fillna("pending")
            hist_display["Outcome"]          = hist_display["Outcome"].fillna("—")
            st.dataframe(hist_display, use_container_width=True, hide_index=True)

    # ── Edge data ─────────────────────────────────────────────────────────────
    with st.expander("Edge & Validation Data", icon=material("track_changes"), expanded=False):
        h_days = row.get("horizon_days")
        segment = row.get("risk_segment")
        edge = _load_edge_data(int(h_days), segment) if h_days else None
        if edge:
            acc = edge.get("directional_accuracy")
            n = edge.get("num_predictions", 0)
            acc_str = f"{acc*100:.2f}%" if acc is not None else "—"
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
                f'n={n} ({icon_html("warning", 12)} below baseline)</div></div>'
            )
            st.markdown(edge_html, unsafe_allow_html=True)
        else:
            st.info(
                "No validation metrics available for this horizon/segment. "
                "Run the evaluation pipeline to generate accuracy data.",
                icon=material("info"),
            )
        st.markdown(
            f'<div style="margin-top:8px;font-size:0.78rem;color:#64748B">'
            f'For full validation history, visit the '
            f'<a href="/Validation" style="color:{PRIMARY}">Validation page</a>.</div>',
            unsafe_allow_html=True,
        )
