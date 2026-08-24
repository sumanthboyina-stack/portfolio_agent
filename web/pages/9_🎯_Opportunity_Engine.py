"""Opportunity Engine — ranked, portfolio-aware daily BUY/WATCH picks."""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from web.styles import (
    inject_global_css, page_header, section_title, top_nav, card,
    badge_html, ticker_label, score_color, SUCCESS, WARNING, NEUTRAL, PRIMARY, PRIMARY_LIGHT,
)
from web.data.watchlist import load_watchlist, add_tickers
from portfolio_agent.tools.opportunity_engine import get_daily_opportunities

st.set_page_config(
    page_title="Opportunity Engine — Portfolio Intelligence",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="expanded",
)
inject_global_css()
top_nav("opportunity_engine")

with st.sidebar:
    st.markdown('<p style="font-size:0.7rem;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;color:#475569;margin:0 0 10px">Opportunity Engine</p>', unsafe_allow_html=True)

page_header(
    "Opportunity Engine",
    subtitle="Today's best new opportunities for your portfolio — ranked, scored, and evaluated against your current holdings",
    icon="🎯",
)

st.markdown(
    '<p style="font-size:0.85rem;color:#64748B;margin-bottom:14px">'
    'Candidates that received a full APEX analysis today (fundamentals, research, macro, news) '
    'as either a news-trending name or a top quant-screener pick — not simply "what\'s moving," '
    'but what the panel would actually call a BUY or WATCH, weighed against what it would do to '
    'your portfolio\'s concentration and correlation.</p>',
    unsafe_allow_html=True,
)


@st.cache_data(ttl=1800, show_spinner=False)
def _cached_opportunities(top_n: int) -> list[dict]:
    return get_daily_opportunities(top_n=top_n)


opportunities = _cached_opportunities(10)

if not opportunities:
    st.info(
        "No opportunities scored yet today — the morning batch generates APEX predictions for "
        "trending/screener candidates once per trading day. Check back after the morning run, "
        "or see the raw quant screener feed on the Opportunities page in the meantime.",
        icon="🔍",
    )
    st.stop()

wl_data = load_watchlist()
existing_tickers = {str(t).upper() for t in wl_data.get("tickers", [])}
at_cap = len(existing_tickers) >= 70

section_title(
    "Today's Top Opportunities",
    badge_text=f"{len(opportunities)} ranked",
    badge_color=PRIMARY,
)
if at_cap:
    st.warning("Watchlist is at capacity (70) — remove tickers before promoting more.")

_CALL_STYLE = {
    "BUY":   (SUCCESS, "#F0FDF4", "🟢 BUY"),
    "WATCH": (WARNING, "#FFFBEB", "🟡 WATCH"),
}

for opp in opportunities:
    ticker = opp["ticker"]
    call_col, call_bg, call_label = _CALL_STYLE.get(opp["call"], (NEUTRAL, "#F9FAFB", opp["call"]))
    score = opp["opportunity_score"]

    impact = opp.get("portfolio_impact") or {}
    fit_lines: list[str] = []
    corr = impact.get("correlation_with_portfolio")
    if corr is not None:
        fit_lines.append(f'<span title="Weighted-average correlation of daily returns vs. your current holdings.">corr {corr:.2f}</span>')
    top_sector, top_before, top_after = impact.get("top_sector"), impact.get("top_sector_before_pct"), impact.get("top_sector_after_pct")
    if top_sector and top_before is not None and top_after is not None:
        fit_lines.append(
            f'<span title="Your most concentrated sector today, and its share after adding a ~2% position in {ticker}.">'
            f'{top_sector} {top_before:.0f}%→{top_after:.0f}%</span>'
        )
    beta = impact.get("beta_vs_spy")
    if beta is not None:
        fit_lines.append(f'<span title="Beta vs. SPY over the trailing 6 months.">β {beta:.2f}</span>')
    fit_html = " · ".join(fit_lines) if fit_lines else '<span style="color:#94A3B8">portfolio-fit unavailable</span>'

    row_l, row_r = st.columns([5, 1])
    with row_l:
        card(
            f'<div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap">'
            f'<div style="font-size:1.1rem;font-weight:800;color:#94A3B8;min-width:20px">#{opp["rank"]}</div>'
            f'<div style="font-size:1rem;font-weight:800;color:#0F172A;min-width:64px">{ticker_label(ticker)}</div>'
            f'{badge_html(call_label, call_col, call_bg)}'
            f'<div style="font-size:0.85rem;font-weight:700;color:{score_color(score / 10)};cursor:help" '
            f'title="0-100 blend of APEX\'s fundamentals+research+macro+news synthesis (dominant factor), '
            f'quant-screener conviction, and portfolio fit (correlation + sector diversification).">'
            f'score {score}</div>'
            f'<div style="margin-left:auto;font-size:0.75rem;color:#64748B;cursor:help">{fit_html}</div>'
            f'</div>'
            f'<p style="margin:10px 0 0;font-size:0.85rem;color:#334155;line-height:1.5">{opp["why"]}</p>',
        )
    with row_r:
        if at_cap or ticker in existing_tickers:
            st.button(
                "On list" if ticker in existing_tickers else "Full",
                key=f"oe_promote_{ticker}", disabled=True, use_container_width=True,
            )
        elif st.button("➕ Watchlist", key=f"oe_promote_{ticker}", use_container_width=True):
            add_tickers(wl_data, [ticker])
            st.success(f"✅ Added {ticker} to watchlist.")
            st.rerun()
