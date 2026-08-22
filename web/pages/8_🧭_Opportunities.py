"""Opportunities — quant screener output surfacing new candidates, not yet tracked."""

from __future__ import annotations

import json
import sys
from datetime import date as _date
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
    get_latest_signal_date, get_signals, has_any_universe_data, get_conviction_data,
)
from portfolio_agent.tools.yfinance_tools import get_close


@st.cache_data(ttl=1800, show_spinner=False)
def _price_change_since(ticker: str, since_date: str) -> float | None:
    """Live price change from `since_date`'s close to today's — cached 30min so
    paging around (e.g. clicking Promote) doesn't refetch on every rerun."""
    anchor  = get_close(ticker, since_date)
    current = get_close(ticker, _date.today().isoformat())
    if not anchor or not current:
        return None
    return (current / anchor) - 1.0

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
    '(Nasdaq/NYSE) universes — not currently on your watchlist. Ranked by conviction: '
    'today\'s score adjusted by 30-day history — how often a name keeps getting flagged, '
    'and whether its past flags actually moved.</p>',
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

_DISCOVERED_CAP = 20

# Conviction = today's raw score, adjusted by 30-day history:
#   + persistence: extra days (beyond today) flagged in the trailing 30 days
#   + outcome:     rolling avg forward return of matured historical flags
#                  (from the evening batch's screener outcome scoring), so a
#                  ticker that keeps firing but never actually moved doesn't
#                  outrank one with a genuine track record.
_PERSISTENCE_STEP     = 0.5
_PERSISTENCE_MAX_DAYS = 5     # cap: +2.5 max from persistence alone
_OUTCOME_SCALE        = 20.0  # +10% avg fwd return -> +2.0 conviction points
_OUTCOME_CAP          = 2.0

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

conviction_by_ticker = get_conviction_data(latest_date, lookback_days=30, min_score=2)
for ticker, s in best_by_ticker.items():
    conv = conviction_by_ticker.get(ticker, {})
    days_flagged   = conv.get("days_flagged") or 1
    avg_fwd_return = conv.get("avg_fwd_return")
    n_outcomes     = conv.get("n_outcomes") or 0

    persistence_boost = min(max(days_flagged - 1, 0), _PERSISTENCE_MAX_DAYS) * _PERSISTENCE_STEP
    outcome_adj = 0.0
    if avg_fwd_return is not None and n_outcomes > 0:
        outcome_adj = max(-_OUTCOME_CAP, min(_OUTCOME_CAP, avg_fwd_return * _OUTCOME_SCALE))

    s["_days_flagged"]    = days_flagged
    s["_avg_fwd_return"]  = avg_fwd_return
    s["_n_outcomes"]      = n_outcomes
    s["_first_flag_date"] = conv.get("first_flag_date")
    s["_conviction"]      = (s.get("score") or 0) + persistence_boost + outcome_adj

discovered_all = sorted(best_by_ticker.values(), key=lambda s: s.get("_conviction") or 0, reverse=True)
_n_total_discovered = len(discovered_all)

section_title(
    "Discovered",
    badge_text=f"{_n_total_discovered} candidates today · signals as of {latest_date}",
    badge_color=PRIMARY,
)
st.markdown(
    '<p style="font-size:0.72rem;color:#94A3B8;margin:-8px 0 12px;cursor:help" '
    f'title="Conviction = today\'s raw score '
    f'+ persistence boost (+{_PERSISTENCE_STEP} per extra day flagged in the trailing 30 days, '
    f'capped at +{_PERSISTENCE_STEP * _PERSISTENCE_MAX_DAYS:.1f}) '
    f'+ outcome adjustment (rolling 30-day avg forward return x {_OUTCOME_SCALE:.0f}, capped at '
    f'±{_OUTCOME_CAP:.1f}). Used to rank; filters below narrow it further.">'
    'ⓘ How conviction is scored — hover fields below for definitions</p>',
    unsafe_allow_html=True,
)

# The screener can trip 200+ single-signal candidates on a broad-market day —
# most are score=2 (one weak signal). Default view is still "top 20 by conviction";
# these filters let you narrow *which* 20 (or fewer) get shown.
_MOMENTUM_THRESHOLD = 0.05   # +/-5% since first flagged splits "fresh" from "running"
_MOMENTUM_POOL_CAP  = _DISCOVERED_CAP * 3  # live price checks are bounded to this many candidates

filt_col1, filt_col2, filt_col3 = st.columns([1, 1.3, 1.5])
with filt_col1:
    tier_choice = st.selectbox(
        "Tier", ["All", "Extended", "Broad"], key="opp_tier_filter",
        help="Extended = S&P 1500 (standard thresholds). Broad = outside S&P 1500 (stricter thresholds, noisier universe).",
    )
with filt_col2:
    min_persistence = st.slider(
        "Min days flagged (30d)", 1, 10, 1, key="opp_min_persistence",
        help="Only show tickers flagged this many or more of the trailing 30 days — filters out one-off noise.",
    )
with filt_col3:
    momentum_choice = st.selectbox(
        "Since first flagged",
        ["All", "Fresh (< ±5% so far)", "Running (up 5%+ already)"],
        key="opp_momentum_filter",
        help="Fresh = just flagged / hasn't moved much yet — earlier entry, less confirmed. "
             "Running = already up meaningfully since first flagged — momentum confirmed, but you're buying after the move.",
    )

filtered = [
    s for s in discovered_all
    if (tier_choice == "All" or (s.get("tier") or "").lower() == tier_choice.lower())
    and (s.get("_days_flagged") or 1) >= min_persistence
]

_momentum_pool_note = ""
if momentum_choice == "All":
    pool = filtered[:_DISCOVERED_CAP]
else:
    pool_source = filtered[:_MOMENTUM_POOL_CAP]
    for s in pool_source:
        ffd = s.get("_first_flag_date")
        s["_since_pct"] = _price_change_since(s["ticker"], ffd) if ffd and ffd != latest_date else None
    if momentum_choice.startswith("Fresh"):
        pool = [s for s in pool_source if s["_since_pct"] is None or abs(s["_since_pct"]) < _MOMENTUM_THRESHOLD]
    else:
        pool = [s for s in pool_source if s["_since_pct"] is not None and s["_since_pct"] >= _MOMENTUM_THRESHOLD]
    if len(filtered) > _MOMENTUM_POOL_CAP:
        _momentum_pool_note = f" (momentum match searched top {_MOMENTUM_POOL_CAP} by conviction, not all {len(filtered)})"

discovered = pool[:_DISCOVERED_CAP]
st.caption(f"Showing {len(discovered)} of {len(filtered)} matching filters{_momentum_pool_note}.")

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

    _TIER_DEFS = {
        "extended": "extended tier — S&amp;P 1500 constituent (500+400+600). Standard screener thresholds.",
        "broad": "broad tier — Nasdaq/NYSE listed, outside the S&amp;P 1500. Stricter thresholds (fewer false positives on noisier names).",
    }

    # Per-signal-type definitions, tier-aware since extended/broad use different
    # trip thresholds (portfolio_agent/tools/screener.py _THRESHOLDS). Each fired
    # token (e.g. "vol_spike:5.6x") is keyed by the part before ":".
    _SIGNAL_DEFS = {
        "extended": {
            "vol_spike":     "Volume spike (+2 pts): today's volume ÷ 20-day avg volume. Fires at ≥2.5x — unusual trading interest.",
            "momentum":      "Momentum (+1 pt): price ÷ 200-day SMA. Fires at ≥1.08 (8%+ above its long-term trend).",
            "gap":           "Overnight gap (+2 pts): |today's close vs prior close| ÷ prior close. Fires at ≥3% — a material single-day move.",
            "near_52w_high": "Near 52-week high (+1 pt): price ÷ 52-week high. Fires at ≥98% — breakout proximity.",
        },
        "broad": {
            "vol_spike":     "Volume spike (+2 pts): today's volume ÷ 20-day avg volume. Fires at ≥3.0x (higher bar than extended — noisier universe).",
            "momentum":      "Momentum (+1 pt): price ÷ 200-day SMA. Fires at ≥1.15 (15%+ above trend, higher bar than extended).",
            "gap":           "Overnight gap (+2 pts): |today's close vs prior close| ÷ prior close. Fires at ≥5% (higher bar than extended).",
            "near_52w_high": "Near 52-week high (+1 pt): price ÷ 52-week high. Fires at ≥99% (tighter than extended).",
        },
    }

    def _signal_chip_html(token: str, tier_key: str) -> str:
        key = token.split(":", 1)[0]
        tip = _SIGNAL_DEFS.get(tier_key, _SIGNAL_DEFS["extended"]).get(key, token)
        return (
            f'<span style="cursor:help;border-bottom:1px dotted #94A3B8" title="{tip}">{token}</span>'
        )

    for sig in discovered:
        ticker   = sig["ticker"]
        tier     = sig.get("tier") or "?"
        tier_key = tier if tier in _SIGNAL_DEFS else "extended"
        score    = sig.get("score") or 0
        try:
            fired = json.loads(sig.get("signals_json") or "[]")
        except (TypeError, ValueError):
            fired = []
        signal_text = " · ".join(_signal_chip_html(t, tier_key) for t in fired) if fired else "—"
        score_col = SUCCESS if score >= 4 else (WARNING if score >= 2 else NEUTRAL)
        score_tip = (
            "Score: sum of points from today's tripped conditions (0-6).\n"
            "Higher = more conditions fired at once.\n"
            "Hover a signal below for what it means."
        )

        days_flagged    = sig.get("_days_flagged") or 1
        avg_fwd_return  = sig.get("_avg_fwd_return")
        n_outcomes      = sig.get("_n_outcomes") or 0
        first_flag_date = sig.get("_first_flag_date")

        meta_lines: list[str] = []

        if first_flag_date and first_flag_date != latest_date:
            since_pct = sig["_since_pct"] if "_since_pct" in sig else _price_change_since(ticker, first_flag_date)
            if since_pct is not None:
                since_col = SUCCESS if since_pct >= 0 else WARNING
                meta_lines.append(
                    f'<span style="color:{since_col};font-weight:600" '
                    f'title="Price change from {first_flag_date} (the first day this ticker was '
                    f'flagged in the trailing 30 days) to today\'s close. Live yfinance lookup, cached 30min.">'
                    f'{since_pct * 100:+.1f}% since {first_flag_date}</span>'
                )

        if n_outcomes > 0 and avg_fwd_return is not None:
            hist_col = SUCCESS if avg_fwd_return >= 0 else WARNING
            meta_lines.append(
                f'<span style="color:{hist_col};font-weight:600" '
                f'title="Rolling 30-day average forward return across this ticker\'s past flags that have '
                f'matured (~1 trading week after firing). {n_outcomes} matured flag(s) in window.">'
                f'{avg_fwd_return * 100:+.1f}% avg fwd</span>'
                f'<span style="color:#94A3B8" '
                f'title="Days in the trailing 30 this ticker fired a score >= 2 signal — persistence, not quality.">'
                f' · {days_flagged}/30d flagged</span>'
            )
        elif days_flagged > 1:
            meta_lines.append(
                f'<span style="color:#94A3B8" '
                f'title="Days in the trailing 30 this ticker fired a score >= 2 signal. No matured forward-return '
                f'data yet — flags need ~1 trading week to evaluate.">'
                f'{days_flagged}/30d flagged</span>'
            )

        meta_html = (
            f'<div style="margin-left:auto;text-align:right;font-size:0.75rem;'
            f'line-height:1.5;white-space:nowrap;cursor:help">' + "<br>".join(meta_lines) + "</div>"
            if meta_lines else ""
        )

        tier_title = _TIER_DEFS.get(tier, "")

        row_l, row_r = st.columns([5, 1])
        with row_l:
            st.markdown(
                f'<div style="background:white;border:1px solid #E2E8F0;border-radius:10px;'
                f'padding:12px 16px;margin-bottom:8px;display:flex;align-items:center;gap:14px">'
                f'<div style="font-size:1rem;font-weight:800;color:#0F172A;min-width:64px" '
                f'title="Flagged by today\'s quant screener, not currently on your watchlist.">'
                f'{ticker_label(ticker)}</div>'
                f'<span title="{tier_title}" style="cursor:help">{badge_html(tier, PRIMARY, PRIMARY_LIGHT)}</span>'
                f'<div style="font-size:0.85rem;font-weight:700;color:{score_col};min-width:56px;cursor:help" '
                f'title="Signals fired today, summed (higher = more conditions tripped at once). {score_tip}">'
                f'score {score:.0f}</div>'
                f'<div style="font-size:0.8rem;color:#64748B">'
                f'{signal_text}</div>'
                f'{meta_html}'
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
