"""Opportunities — quant screener output surfacing new candidates, not yet tracked."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import streamlit as st

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from web.styles import (
    inject_global_css, page_header, section_title, top_nav,
    badge_html, ticker_label, SUCCESS, WARNING, NEUTRAL, PRIMARY, PRIMARY_LIGHT,
)
from web.data.watchlist import load_watchlist, save_watchlist
from portfolio_agent.tools.universe_db import (
    get_latest_signal_date, get_signals, has_any_universe_data,
)

st.set_page_config(
    page_title="Opportunities — Portfolio Intelligence",
    page_icon="🧭",
    layout="wide",
    initial_sidebar_state="expanded",
)
inject_global_css()
top_nav("opportunities")

with st.sidebar:
    st.markdown('<p style="font-size:0.7rem;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;color:#475569;margin:0 0 10px">Opportunities</p>', unsafe_allow_html=True)

wl_data     = load_watchlist()
all_tickers = [str(t).upper() for t in wl_data.get("tickers", [])]

page_header(
    "Opportunities",
    subtitle="New candidates surfaced by the daily quant screener — not yet on your watchlist",
    icon="🧭",
)

st.markdown(
    '<p style="font-size:0.85rem;color:#64748B;margin-bottom:14px">'
    'Tickers flagged by the daily quant screener (momentum vs 200d SMA, volume spike, '
    'overnight gap, near-52-week-high) across the extended (S&amp;P 1500) and broad '
    '(Nasdaq/NYSE) universes — not currently on your watchlist.</p>',
    unsafe_allow_html=True,
)

if not has_any_universe_data():
    st.info(
        "No universe data cached yet — run `scripts/refresh_universe.py` to build the "
        "extended/broad ticker universe, then a morning pipeline run to generate signals.",
        icon="🔍",
    )
    st.stop()

latest_date = get_latest_signal_date()
if not latest_date:
    st.info(
        "Universe is cached but no screener signals yet — the morning pipeline runs "
        "the screener once per day.",
        icon="🔍",
    )
    st.stop()

signals = get_signals(latest_date, min_score=2)
existing_set = {t.upper() for t in all_tickers}
# A ticker could in principle qualify under more than one tier on
# the same day — keep only its best-scoring row so it renders once.
best_by_ticker: dict[str, dict] = {}
for s in signals:
    if s["ticker"] in existing_set:
        continue
    prev = best_by_ticker.get(s["ticker"])
    if prev is None or (s.get("score") or 0) > (prev.get("score") or 0):
        best_by_ticker[s["ticker"]] = s
discovered = sorted(best_by_ticker.values(), key=lambda s: s.get("score") or 0, reverse=True)

section_title(
    "Discovered",
    badge_text=f"{len(discovered)} candidates · signals as of {latest_date}",
    badge_color=PRIMARY,
)

if not discovered:
    st.info(
        "No new candidates today — either nothing tripped a signal, or "
        "everything flagged is already on your watchlist.",
        icon="✅",
    )
else:
    at_cap = len(all_tickers) >= 70
    if at_cap:
        st.warning("Watchlist is at capacity (70) — remove tickers before promoting more.")

    for sig in discovered:
        ticker = sig["ticker"]
        tier   = sig.get("tier") or "?"
        score  = sig.get("score") or 0
        try:
            fired = json.loads(sig.get("signals_json") or "[]")
        except (TypeError, ValueError):
            fired = []
        signal_text = " · ".join(fired) if fired else "—"
        score_col = SUCCESS if score >= 4 else (WARNING if score >= 2 else NEUTRAL)

        row_l, row_r = st.columns([5, 1])
        with row_l:
            st.markdown(
                f'<div style="background:white;border:1px solid #E2E8F0;border-radius:10px;'
                f'padding:12px 16px;margin-bottom:8px;display:flex;align-items:center;gap:14px">'
                f'<div style="font-size:1rem;font-weight:800;color:#0F172A;min-width:64px">{ticker_label(ticker)}</div>'
                f'{badge_html(tier, PRIMARY, PRIMARY_LIGHT)}'
                f'<div style="font-size:0.85rem;font-weight:700;color:{score_col};min-width:56px">score {score:.0f}</div>'
                f'<div style="font-size:0.8rem;color:#64748B">{signal_text}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
        with row_r:
            if at_cap:
                st.button("Full", key=f"promote_{ticker}", disabled=True, use_container_width=True)
            elif st.button("➕ Core", key=f"promote_{ticker}", use_container_width=True):
                wl_data["tickers"] = list(wl_data.get("tickers", [])) + [ticker]
                save_watchlist(wl_data)
                st.success(f"✅ Promoted {ticker} to core watchlist.")
                st.rerun()
