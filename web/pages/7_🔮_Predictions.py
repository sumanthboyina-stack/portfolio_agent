"""APEX Predictions — card-based layout with action items, grouped accordion, and drill-down."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from web.styles import inject_global_css, page_header, section_title, section_tile, top_nav, material, icon_html
from web.data.predictions import _latest_as_of_date, _load_edge_data, _load_predictions
from web.components.predictions_cards import render_action_items, render_all_predictions, render_drill_down, render_system_strip

_DB = _ROOT / "data" / "portfolio.db"

from web.auth import current_context, require_login

st.set_page_config(page_title="APEX — Predictions", page_icon=str(_ROOT / "web" / "static" / "apex_mark.png"), layout="wide")
inject_global_css()
require_login()
top_nav("predictions")


# ── Main page ─────────────────────────────────────────────────────────────────

if not _DB.exists():
    page_header("Predictions", icon="insights")
    st.warning("Database not found. Run `python main.py --daily` to initialise it.", icon=material("warning"))
    st.stop()

# ── Piece 2: Controls Bar ────────────────────────────────────────────────────
ctrl1, ctrl2, ctrl3, ctrl4, ctrl5, ctrl6 = st.columns([1.8, 1, 1.5, 2, 2, 1.4])

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
    if st.button("Refresh", icon=material("refresh"), use_container_width=True):
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
_tile_actions = section_tile("Action Items", badge_text="portfolio", expanded=True, key="pred_actions")
if _tile_actions:
    with _tile_actions:
        render_action_items(df_portfolio)

# ── Section 2: All Portfolio Predictions ──────────────────────────────────────
_tile_all = section_tile("All Portfolio Predictions", badge_text=f"{len(df_portfolio)} rows", expanded=True, key="pred_all")
if _tile_all:
    with _tile_all:
        render_all_predictions(df_portfolio)

# ── Section 3: New Opportunities — moved to the Opportunity Engine page ───────
# Trending-discovery candidates (df_opps, computed above so df_portfolio stays
# clean of them) now render on the Opportunity Engine page instead, alongside
# its portfolio-ranked BUY/WATCH picks, so both opportunity views live on one
# screen rather than being split across two pages.
if not df_opps.empty:
    st.caption(
        f"{len(df_opps['ticker'].unique())} trending-discovery ticker(s) analyzed for {selected_date} — "
        "see the **Opportunity Engine** page for these, ranked against your portfolio."
    )

# ── Piece 5: Drill-down ───────────────────────────────────────────────────────
drill_ticker = st.session_state.get("drill_ticker")
drill_date = st.session_state.get("drill_date")

if drill_ticker:
    render_drill_down(drill_ticker, drill_date)
