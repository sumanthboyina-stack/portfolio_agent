"""Watchlist manager — browse, add, and remove tracked tickers."""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from web.styles import inject_global_css, page_header, section_title, top_nav, SUCCESS, PRIMARY
from web.data.watchlist import (
    WATCHLIST_PATH, load_watchlist, watchlist_db_status,
    add_tickers, remove_tickers,
)
from portfolio_agent.tools.company_names import get_company_names, get_company_name, search_companies

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

page_header("Watchlist Manager", icon="📋")

# ── Top action bar ────────────────────────────────────────────────────────────
ab1, ab2, ab3 = st.columns([2, 2, 5])
with ab1:
    has_fund = sum(1 for v in db_status.values() if v.get("fund"))
    st.metric("With Fundamentals", has_fund)
with ab2:
    has_pred = sum(1 for v in db_status.values() if v.get("pred"))
    st.metric("With Predictions", has_pred)
with ab3:
    cap = 70
    used_pct = int(len(all_tickers) / cap * 100)
    st.progress(used_pct / 100, text=f"Capacity: {len(all_tickers)}/{cap} ({used_pct}%)")

st.divider()

# ── Search / filter ─────────────────────────────────────────────────────────
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

# Scoped CSS: every card container (key="card_*") becomes the positioning
# anchor for its own remove button (key="grid_rm_*"), pinned to the top-right
# corner, shrunk to a bare icon with no border/background of its own so it
# blends into the white card instead of reading as a separate control.
st.markdown(
    """
    <style>
    div[class*="st-key-card_"] { position: relative; }
    div[class*="st-key-grid_rm_"] {
        position: absolute;
        top: 2px;
        right: 2px;
        width: auto;
        z-index: 10;
    }
    /* Extra data-testid/div layer raises specificity above the global
       .stButton background rule in web/styles.py, which also uses !important. */
    div[class*="st-key-grid_rm_"] div[data-testid="stButton"] button,
    div[class*="st-key-grid_rm_"] div[data-testid="stButton"] button:hover,
    div[class*="st-key-grid_rm_"] div[data-testid="stButton"] button:active,
    div[class*="st-key-grid_rm_"] div[data-testid="stButton"] button:focus,
    div[class*="st-key-grid_rm_"] div[data-testid="stButton"] button:focus:not(:active),
    div[class*="st-key-grid_rm_"] div[data-testid="stButton"] button:focus-visible {
        background: transparent !important;
        background-color: transparent !important;
        border: none !important;
        box-shadow: none !important;
        outline: none !important;
        padding: 0 3px;
        min-height: unset;
        height: 20px;
        font-size: 0.7rem;
        line-height: 1;
        color: #CBD5E1 !important;
    }
    div[class*="st-key-grid_rm_"] div[data-testid="stButton"] button:hover,
    div[class*="st-key-grid_rm_"] div[data-testid="stButton"] button:focus:hover {
        color: #DC2626 !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def _render_ticker_card(col, ticker: str, names: dict[str, str]) -> None:
    s = db_status.get(ticker, {})
    fund_dot  = f'<span style="color:{SUCCESS};cursor:help" title="Fundamentals: in DB">●</span>' if s.get("fund") else '<span style="color:#E2E8F0;cursor:help" title="Fundamentals: missing">●</span>'
    res_dot   = f'<span style="color:{PRIMARY};cursor:help" title="Research: in DB">●</span>'      if s.get("res")  else '<span style="color:#E2E8F0;cursor:help" title="Research: missing">●</span>'
    pred_dot  = f'<span style="color:#7C3AED;cursor:help" title="Predictions: in DB">●</span>'     if s.get("pred") else '<span style="color:#E2E8F0;cursor:help" title="Predictions: missing">●</span>'
    cname = names.get(ticker, "")
    if len(cname) > 22:
        cname = cname[:21].rstrip() + "…"
    name_line = f'<div style="font-size:0.68rem;color:#94A3B8;margin-top:1px" title="{names.get(ticker, "")}">{cname}</div>' if cname else ""

    card = col.container(border=True, key=f"card_{ticker}")
    card.markdown(
        f'<div style="text-align:center">'
        f'<div style="font-size:1rem;font-weight:800;color:#0F172A">{ticker}</div>'
        f'{name_line}'
        f'<div style="font-size:0.75rem;margin-top:6px;display:flex;justify-content:center;'
        f'gap:4px">{fund_dot}{res_dot}{pred_dot}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )
    if card.button("🗑️", key=f"grid_rm_{ticker}", help=f"Remove {ticker}"):
        remove_tickers(wl_data, [ticker])
        st.success(f"✅ Removed: {ticker}")
        st.rerun()


def _render_add_tile(col) -> None:
    tile = col.container(border=True, key="card_add_tile")
    tile.markdown(
        '<div style="text-align:center;font-size:0.85rem;font-weight:700;color:#2563EB">➕ Add Ticker</div>',
        unsafe_allow_html=True,
    )
    new_input = tile.text_input(
        "Ticker or company name",
        placeholder="NVDA or Nvidia",
        label_visibility="collapsed",
        key="add_tickers_input",
    )
    if tile.button("Add", key="add_tickers_btn", use_container_width=True):
        to_add = [t.strip().upper() for t in new_input.split(",") if t.strip()]
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

    # Company-name suggestions for the last comma-separated token, only when
    # it doesn't already resolve to a known ticker (avoids noise once the
    # user has typed real symbols like "NVDA, AMD").
    _last_token = new_input.rsplit(",", 1)[-1].strip()
    if len(_last_token) >= 2 and not get_company_name(_last_token):
        _suggestions = [
            (tk, nm) for tk, nm in search_companies(_last_token, limit=4)
            if tk not in all_tickers
        ]
        if _suggestions:
            tile.caption("Did you mean:")
            for _tk, _nm in _suggestions:
                _label = f"{_tk} — {_nm}"
                if len(_label) > 26:
                    _label = _label[:25].rstrip() + "…"
                if tile.button(_label, key=f"sugg_{_tk}", use_container_width=True):
                    added, warning = add_tickers(wl_data, [_tk])
                    if warning:
                        st.warning(warning)
                    elif added:
                        st.success(f"✅ Added: {_tk}")
                        st.rerun()


# Ticker grid (4 per row) — the "➕ Add Ticker" tile is appended as the last
# cell so adding a ticker is part of the same grid, not a separate panel.
cols_per_row = 4
_ADD_TILE = object()
cells = list(display_tickers) + [_ADD_TILE]
rows = [cells[i:i+cols_per_row] for i in range(0, len(cells), cols_per_row)]
names = get_company_names(display_tickers)

for row in rows:
    cols = st.columns(cols_per_row)
    for col, cell in zip(cols, row):
        if cell is _ADD_TILE:
            _render_add_tile(col)
        else:
            _render_ticker_card(col, cell, names)

if not display_tickers:
    st.info("No tickers match your filter — the ➕ Add Ticker tile above still adds to the full watchlist.", icon="🔍")

