"""Watchlist manager — browse, add, and remove tracked tickers."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import streamlit as st
import yaml

_ROOT      = Path(__file__).resolve().parents[2]
_WATCHLIST = _ROOT / "config" / "watchlist.yaml"
sys.path.insert(0, str(_ROOT))

from web.styles import (
    inject_global_css, page_header, section_title, card, top_nav,
    badge_html, ticker_label, SUCCESS, WARNING, DANGER, PRIMARY, NEUTRAL,
    PRIMARY_LIGHT, SUCCESS_LIGHT,
)
from portfolio_agent.tools.company_names import get_company_names

_DB = _ROOT / "data" / "portfolio.db"

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

if not _WATCHLIST.exists():
    page_header("Watchlist Manager", icon="📋")
    st.error(f"`config/watchlist.yaml` not found at `{_WATCHLIST}`")
    st.stop()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load() -> dict:
    return yaml.safe_load(_WATCHLIST.read_text()) or {}


def _save(data: dict) -> None:
    with open(_WATCHLIST, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


def _db_status(tickers: list[str]) -> dict[str, dict]:
    """For each ticker, check if it has records in fundamentals/research/predictions."""
    if not _DB.exists() or not tickers:
        return {}
    status = {t: {"fund": False, "res": False, "pred": False} for t in tickers}
    ph = ",".join("?"*len(tickers))
    try:
        with sqlite3.connect(str(_DB)) as c:
            for t in c.execute(
                f"SELECT DISTINCT ticker FROM fundamentals WHERE ticker IN ({ph})", tickers
            ).fetchall():
                if t[0] in status:
                    status[t[0]]["fund"] = True
            for t in c.execute(
                f"SELECT DISTINCT ticker FROM research WHERE as_of_date IS NOT NULL AND ticker IN ({ph})", tickers
            ).fetchall():
                if t[0] in status:
                    status[t[0]]["res"] = True
            for t in c.execute(
                f"SELECT DISTINCT ticker FROM predictions WHERE ticker IN ({ph})", tickers
            ).fetchall():
                if t[0] in status:
                    status[t[0]]["pred"] = True
    except Exception:
        pass
    return status


# ── Load data ─────────────────────────────────────────────────────────────────

wl_data   = _load()
all_tickers = [str(t).upper() for t in wl_data.get("tickers", [])]
db_status   = _db_status(all_tickers)

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

tab_watchlist, tab_discovered = st.tabs(["📋 Your Watchlist", "🔍 Discovered"])

# ══════════════════════════════════════════════════════════
# TAB 1 — Your Watchlist (manual add/remove)
# ══════════════════════════════════════════════════════════
with tab_watchlist:
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
                existing = {str(t).upper() for t in all_tickers}
                new_unique = [t for t in to_add if t not in existing]
                if not new_unique:
                    st.info("All tickers already in watchlist.")
                elif len(all_tickers) + len(new_unique) > 70:
                    available = 70 - len(all_tickers)
                    new_unique = new_unique[:available]
                    st.warning(f"Watchlist cap (70) — adding only {len(new_unique)}: {', '.join(new_unique)}")
                if new_unique:
                    wl_data["tickers"] = list(wl_data.get("tickers", [])) + new_unique
                    _save(wl_data)
                    st.success(f"✅ Added: {', '.join(new_unique)}")
                    st.rerun()
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
                remove_set = set(to_remove)
                new_list = [t for t in wl_data.get("tickers", []) if str(t).upper() not in remove_set]
                wl_data["tickers"] = new_list
                _save(wl_data)
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


# ══════════════════════════════════════════════════════════
# TAB 2 — Discovered (quant screener output, not yet tracked)
# ══════════════════════════════════════════════════════════
with tab_discovered:
    from portfolio_agent.tools.universe_db import (
        get_latest_signal_date, get_signals, has_any_universe_data,
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
    else:
        latest_date = get_latest_signal_date()
        if not latest_date:
            st.info(
                "Universe is cached but no screener signals yet — the morning pipeline runs "
                "the screener once per day.",
                icon="🔍",
            )
        else:
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
                            _save(wl_data)
                            st.success(f"✅ Promoted {ticker} to core watchlist.")
                            st.rerun()
