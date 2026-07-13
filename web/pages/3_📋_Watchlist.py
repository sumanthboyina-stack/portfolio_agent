"""Watchlist manager — browse, add, and remove tracked tickers."""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from web.styles import (
    inject_global_css, page_header, section_title, card, top_nav,
    badge_html, ticker_label, SUCCESS, WARNING, DANGER, PRIMARY, NEUTRAL,
    PRIMARY_LIGHT, SUCCESS_LIGHT,
)
from web.data.watchlist import (
    WATCHLIST_PATH, load_watchlist, save_watchlist, watchlist_db_status,
    add_tickers, remove_tickers,
)
from portfolio_agent.tools.company_names import get_company_names

st.set_page_config(
    page_title="Watchlist — Portfolio Intelligence",
    page_icon="📋",
    layout="wide",
    initial_sidebar_state="expanded",
)
inject_global_css()
top_nav("watchlist")

with st.sidebar:
    st.markdown('<p style="font-size:0.7rem;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;color:#475569;margin:0 0 10px">Watchlist</p>', unsafe_allow_html=True)

if not WATCHLIST_PATH.exists():
    page_header("Watchlist Manager", icon="📋")
    st.error(f"`config/watchlist.yaml` not found at `{WATCHLIST_PATH}`")
    st.stop()


# ── Load data ─────────────────────────────────────────────────────────────────

wl_data   = load_watchlist()
all_tickers = [str(t).upper() for t in wl_data.get("tickers", [])]
db_status   = watchlist_db_status(all_tickers)

page_header(
    "Watchlist Manager",
    subtitle=f"{len(all_tickers)} tickers tracked · Max 70",
    icon="📋",
)

# ── Category counts ───────────────────────────────────────────────────────────
categories = wl_data.get("categories", {})
cat_counts: dict[str, int] = {}
if categories:
    for cat, tlist in categories.items():
        # Support both old format (list) and new format ({tier, tickers: [...]})
        if isinstance(tlist, dict):
            tlist = tlist.get("tickers", [])
        cat_counts[cat] = len([t for t in tlist if str(t).upper() in all_tickers])

# ── Top action bar ────────────────────────────────────────────────────────────
ab1, ab2, ab3, ab4 = st.columns([2, 2, 2, 4])
with ab1:
    st.metric("Total Tickers", len(all_tickers))
with ab2:
    has_fund = sum(1 for v in db_status.values() if v.get("fund"))
    st.metric("With Fundamentals", has_fund)
with ab3:
    has_pred = sum(1 for v in db_status.values() if v.get("pred"))
    st.metric("With Predictions", has_pred)
with ab4:
    cap = 70
    used_pct = int(len(all_tickers) / cap * 100)
    st.progress(used_pct / 100, text=f"Capacity: {len(all_tickers)}/{cap} ({used_pct}%)")

st.divider()

left, right = st.columns([3, 1], gap="large")

with right:
    # ── Add tickers ───────────────────────────────────────────────────────
    st.markdown(
        '<div style="background:white;border:1px solid #E2E8F0;border-radius:12px;'
        'padding:18px 20px;box-shadow:0 1px 3px rgba(0,0,0,0.05)">'
        '<h4 style="margin:0 0 12px;color:#0F172A;font-size:0.95rem">➕ Add Tickers</h4>',
        unsafe_allow_html=True,
    )
    new_tickers_input = st.text_input(
        "Ticker symbols",
        placeholder="NVDA, AMD, TSLA",
        label_visibility="collapsed",
        key="add_tickers_input",
    )
    if st.button("Add to Watchlist", type="primary", use_container_width=True):
        to_add = [t.strip().upper() for t in new_tickers_input.split(",") if t.strip()]
        if not to_add:
            st.warning("Enter at least one ticker.")
        else:
            added, warning = add_tickers(wl_data, to_add)
            if warning:
                st.warning(warning)
            if added:
                st.success(f"✅ Added: {', '.join(added)}")
                st.rerun()
            elif not warning:
                st.info("All tickers already in watchlist.")
    st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Remove tickers ───────────────────────────────────────────────────
    st.markdown(
        '<div style="background:white;border:1px solid #E2E8F0;border-radius:12px;'
        'padding:18px 20px;box-shadow:0 1px 3px rgba(0,0,0,0.05)">'
        '<h4 style="margin:0 0 12px;color:#0F172A;font-size:0.95rem">🗑️ Remove Tickers</h4>',
        unsafe_allow_html=True,
    )
    to_remove = st.multiselect(
        "Select to remove",
        all_tickers,
        label_visibility="collapsed",
        key="remove_tickers",
    )
    if to_remove:
        if st.button(f"Remove {len(to_remove)} ticker(s)", use_container_width=True, type="primary"):
            remove_tickers(wl_data, to_remove)
            st.success(f"✅ Removed: {', '.join(to_remove)}")
            st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)


with left:
    # ── Search / filter ───────────────────────────────────────────────────
    search = st.text_input("🔍 Search tickers", placeholder="Type to filter…",
                           label_visibility="collapsed", key="wl_search")
    filter_missing = st.checkbox("Show tickers missing fundamentals", key="wl_filter_missing")

    display_tickers = all_tickers
    if search:
        display_tickers = [t for t in all_tickers if search.upper() in t]
    if filter_missing:
        display_tickers = [t for t in display_tickers if not db_status.get(t, {}).get("fund")]

    section_title(
        f"Tickers",
        badge_text=f"{len(display_tickers)} shown",
        badge_color=PRIMARY,
    )

    # Ticker grid (4 per row)
    cols_per_row = 4
    rows = [display_tickers[i:i+cols_per_row]
            for i in range(0, len(display_tickers), cols_per_row)]
    names = get_company_names(display_tickers)

    for row in rows:
        cols = st.columns(cols_per_row)
        for col, ticker in zip(cols, row):
            s = db_status.get(ticker, {})
            fund_dot  = f'<span style="color:{SUCCESS}" title="Fundamentals in DB">●</span>' if s.get("fund") else '<span style="color:#E2E8F0" title="No fundamentals">●</span>'
            res_dot   = f'<span style="color:{PRIMARY}" title="Research in DB">●</span>' if s.get("res")  else '<span style="color:#E2E8F0" title="No research">●</span>'
            pred_dot  = f'<span style="color:#7C3AED" title="Predictions in DB">●</span>' if s.get("pred") else '<span style="color:#E2E8F0" title="No prediction">●</span>'
            cname = names.get(ticker, "")
            if len(cname) > 22:
                cname = cname[:21].rstrip() + "…"
            name_line = f'<div style="font-size:0.68rem;color:#94A3B8;margin-top:1px" title="{names.get(ticker, "")}">{cname}</div>' if cname else ""

            col.markdown(
                f'<div style="background:white;border:1px solid #E2E8F0;border-radius:10px;'
                f'padding:12px 14px;text-align:center;'
                f'box-shadow:0 1px 2px rgba(0,0,0,0.04);margin-bottom:2px">'
                f'<div style="font-size:1rem;font-weight:800;color:#0F172A">{ticker}</div>'
                f'{name_line}'
                f'<div style="font-size:0.75rem;margin-top:6px;display:flex;justify-content:center;'
                f'gap:4px">{fund_dot}{res_dot}{pred_dot}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

    if not display_tickers:
        st.info("No tickers match your filter.", icon="🔍")

    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown(
        '<p style="font-size:0.78rem;color:#94A3B8">'
        '● Green = Fundamentals in DB &nbsp;·&nbsp; ● Blue = Research &nbsp;·&nbsp; '
        '● Purple = Predictions</p>',
        unsafe_allow_html=True,
    )

