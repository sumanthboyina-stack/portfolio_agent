"""Opportunity Engine — ranked, portfolio-aware daily BUY/WATCH picks, plus the
raw quant-screener feed for everything that hasn't earned a full APEX call yet."""

from __future__ import annotations

import json
import sys
from datetime import date as _date
from pathlib import Path

import streamlit as st

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from web.styles import (
    inject_global_css, page_header, section_title, top_nav, card, material, icon_html,
    badge_html, ticker_label, score_color, SUCCESS, WARNING, NEUTRAL, PRIMARY, PRIMARY_LIGHT,
)
from web.data.watchlist import load_watchlist_tickers, add_tickers
from portfolio_agent.tools.opportunity_engine import get_daily_opportunities, get_opportunity_history
from portfolio_agent.tools.universe_db import (
    get_latest_signal_date, get_signals, has_any_universe_data, get_conviction_data,
    rank_by_conviction, PERSISTENCE_STEP as _PERSISTENCE_STEP,
    PERSISTENCE_MAX_DAYS as _PERSISTENCE_MAX_DAYS,
    OUTCOME_SCALE as _OUTCOME_SCALE, OUTCOME_CAP as _OUTCOME_CAP,
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
    page_title="Opportunity Engine — Portfolio Intelligence",
    page_icon=material("track_changes"),
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
    icon="track_changes",
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


@st.cache_data(ttl=1800, show_spinner=False)
def _cached_history(ticker: str) -> dict:
    return get_opportunity_history(ticker, window_days=30)


opportunities = _cached_opportunities(10)

existing_tickers = set(load_watchlist_tickers())
at_cap = len(existing_tickers) >= 70

# ── Section 1: Today's Top Opportunities (APEX-vetted BUY/WATCH) ──────────────

if not opportunities:
    st.info(
        "No opportunities scored yet today — the morning batch generates APEX predictions for "
        "trending/screener candidates once per trading day. Check back after the morning run, "
        "or see what's been flagged but not yet analyzed below.",
        icon=material("search"),
    )
else:
    section_title(
        "Today's Top Opportunities",
        badge_text=f"{len(opportunities)} ranked",
        badge_color=PRIMARY,
    )
    if at_cap:
        st.warning("Watchlist is at capacity (70) — remove tickers before promoting more.")

    _CALL_STYLE = {
        "BUY":   (SUCCESS, "#F0FDF4", "BUY"),
        "WATCH": (WARNING, "#FFFBEB", "WATCH"),
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
                f'{top_sector} {top_before:.2f}%→{top_after:.2f}%</span>'
            )
        beta = impact.get("beta_vs_spy")
        if beta is not None:
            fit_lines.append(f'<span title="Beta vs. SPY over the trailing 6 months.">β {beta:.2f}</span>')
        fit_html = " · ".join(fit_lines) if fit_lines else '<span style="color:#94A3B8">portfolio-fit unavailable</span>'

        hist = _cached_history(ticker)
        hist_parts: list[str] = []
        if hist["times_identified"] > 0:
            hist_parts.append(
                f'<span title="Distinct days in the trailing 30 this ticker qualified as a BUY/WATCH '
                f'opportunity (including today).">{icon_html("repeat", 12)} {hist["times_identified"]}x in 30d</span>'
            )
            first_date = hist["first_identified_date"]
            if first_date and first_date != _date.today().isoformat():
                since_pct = _price_change_since(ticker, first_date)
                if since_pct is not None:
                    since_col = SUCCESS if since_pct >= 0 else WARNING
                    hist_parts.append(
                        f'<span style="color:{since_col};font-weight:600" '
                        f'title="Price change from {first_date} (first day this ticker qualified in the '
                        f'trailing 30 days) to today\'s close.">{since_pct * 100:+.2f}% since {first_date}</span>'
                    )
                first_cs, current_cs = hist["first_composite_score"], hist["current_composite_score"]
                if hist["score_change"] is not None and first_cs is not None and current_cs is not None:
                    sc_col = SUCCESS if hist["score_change"] >= 0 else WARNING
                    hist_parts.append(
                        f'<span style="color:{sc_col};font-weight:600" '
                        f'title="APEX composite score ({first_cs:.2f} on {first_date} → {current_cs:.2f} today).">'
                        f'score {hist["score_change"]:+.2f}</span>'
                    )
        hist_html = (
            f'<div style="margin-top:6px;font-size:0.75rem;color:#94A3B8;cursor:help">'
            + " · ".join(hist_parts) + "</div>"
            if hist_parts else ""
        )

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
                f'<p style="margin:10px 0 0;font-size:0.85rem;color:#334155;line-height:1.5">{opp["why"]}</p>'
                f'{hist_html}',
            )
        with row_r:
            if at_cap or ticker in existing_tickers:
                st.button(
                    "On list" if ticker in existing_tickers else "Full",
                    key=f"oe_promote_{ticker}", disabled=True, use_container_width=True,
                )
            elif st.button("Watchlist", icon=material("add"), key=f"oe_promote_{ticker}", use_container_width=True):
                add_tickers([ticker])
                st.success(f"Added {ticker} to watchlist.", icon=material("check_circle"))
                st.rerun()

st.divider()

# ── Section 2: raw quant-screener feed — everything flagged, not yet analyzed ─
#
# Opportunity Engine above only ever covers ~10 names/day (whatever got a full
# APEX call). This surfaces the much wider screener universe (often 50-200+
# candidates) so nothing flagged by the daily quant screen goes unseen just
# because APEX hasn't gotten to it yet — merged in from the old standalone
# Opportunities page rather than dropped when that page was retired.

section_title(
    "Also Flagged Today — Not Yet Analyzed",
    badge_text="raw quant screener feed",
    badge_color=NEUTRAL,
)
st.markdown(
    '<p style="font-size:0.82rem;color:#64748B;margin:-6px 0 12px">'
    'Tickers flagged by the daily quant screener (momentum vs 200d SMA, volume spike, overnight '
    'gap, near-52-week-high) that have not (yet) received a full APEX analysis above. Ranked by '
    'conviction — today\'s score adjusted by 30-day persistence and outcome history.</p>',
    unsafe_allow_html=True,
)

if not has_any_universe_data():
    st.caption(
        "No universe data cached yet — run `scripts/refresh_universe.py` to build the "
        "extended/broad ticker universe, then a morning pipeline run to generate signals."
    )
else:
    latest_date = get_latest_signal_date()
    if not latest_date:
        st.caption("Universe is cached but no screener signals yet — the morning pipeline runs the screener once per day.")
    else:
        _DISCOVERED_CAP = 20
        _MOMENTUM_THRESHOLD = 0.05
        _MOMENTUM_POOL_CAP = _DISCOVERED_CAP * 3

        # Exclude anything already shown in Section 1 above (already APEX-vetted)
        # and anything already on the watchlist — same dedup logic the old page used.
        already_shown = {o["ticker"] for o in opportunities} | existing_tickers
        signals = get_signals(latest_date, min_score=2)
        conviction_by_ticker = get_conviction_data(latest_date, lookback_days=30, min_score=2)
        discovered_all = rank_by_conviction(signals, conviction_by_ticker, exclude=already_shown)
        _n_total_discovered = len(discovered_all)

        st.caption(
            f"{_n_total_discovered} candidate(s) today · signals as of {latest_date}"
        )
        st.markdown(
            '<p style="font-size:0.72rem;color:#94A3B8;margin:-8px 0 12px;cursor:help" '
            f'title="Conviction = today\'s raw score '
            f'+ persistence boost (+{_PERSISTENCE_STEP} per extra day flagged in the trailing 30 days, '
            f'capped at +{_PERSISTENCE_STEP * _PERSISTENCE_MAX_DAYS:.2f}) '
            f'+ outcome adjustment (rolling 30-day avg forward return x {_OUTCOME_SCALE:.2f}, capped at '
            f'±{_OUTCOME_CAP:.2f}). Used to rank; filters below narrow it further.">'
            'ⓘ How conviction is scored — hover fields below for definitions</p>',
            unsafe_allow_html=True,
        )

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
                "No candidates match these filters — either nothing tripped a signal, or "
                "everything flagged is already shown above or on your watchlist.",
                icon=material("check_circle"),
            )
        else:
            _TIER_DEFS = {
                "extended": "extended tier — S&amp;P 1500 constituent (500+400+600). Standard screener thresholds.",
                "broad": "broad tier — Nasdaq/NYSE listed, outside the S&amp;P 1500. Stricter thresholds (fewer false positives on noisier names).",
            }
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
                return f'<span style="cursor:help;border-bottom:1px dotted #94A3B8" title="{tip}">{token}</span>'

            for sig in discovered:
                d_ticker = sig["ticker"]
                tier     = sig.get("tier") or "?"
                tier_key = tier if tier in _SIGNAL_DEFS else "extended"
                d_score  = sig.get("score") or 0
                try:
                    fired = json.loads(sig.get("signals_json") or "[]")
                except (TypeError, ValueError):
                    fired = []
                signal_text = " · ".join(_signal_chip_html(t, tier_key) for t in fired) if fired else "—"
                score_col = SUCCESS if d_score >= 4 else (WARNING if d_score >= 2 else NEUTRAL)
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
                    since_pct = sig["_since_pct"] if "_since_pct" in sig else _price_change_since(d_ticker, first_flag_date)
                    if since_pct is not None:
                        since_col = SUCCESS if since_pct >= 0 else WARNING
                        meta_lines.append(
                            f'<span style="color:{since_col};font-weight:600" '
                            f'title="Price change from {first_flag_date} (the first day this ticker was '
                            f'flagged in the trailing 30 days) to today\'s close. Live yfinance lookup, cached 30min.">'
                            f'{since_pct * 100:+.2f}% since {first_flag_date}</span>'
                        )
                if n_outcomes > 0 and avg_fwd_return is not None:
                    hist_col = SUCCESS if avg_fwd_return >= 0 else WARNING
                    meta_lines.append(
                        f'<span style="color:{hist_col};font-weight:600" '
                        f'title="Rolling 30-day average forward return across this ticker\'s past flags that have '
                        f'matured (~1 trading week after firing). {n_outcomes} matured flag(s) in window.">'
                        f'{avg_fwd_return * 100:+.2f}% avg fwd</span>'
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

                d_row_l, d_row_r = st.columns([5, 1])
                with d_row_l:
                    st.markdown(
                        f'<div style="background:white;border:1px solid #E2E8F0;border-radius:10px;'
                        f'padding:12px 16px;margin-bottom:8px;display:flex;align-items:center;gap:14px">'
                        f'<div style="font-size:1rem;font-weight:800;color:#0F172A;min-width:64px" '
                        f'title="Flagged by today\'s quant screener, not yet given a full APEX analysis.">'
                        f'{ticker_label(d_ticker)}</div>'
                        f'<span title="{tier_title}" style="cursor:help">{badge_html(tier, PRIMARY, PRIMARY_LIGHT)}</span>'
                        f'<div style="font-size:0.85rem;font-weight:700;color:{score_col};min-width:56px;cursor:help" '
                        f'title="Signals fired today, summed (higher = more conditions tripped at once). {score_tip}">'
                        f'score {d_score:.2f}</div>'
                        f'<div style="font-size:0.8rem;color:#64748B">'
                        f'{signal_text}</div>'
                        f'{meta_html}'
                        f'</div>',
                        unsafe_allow_html=True,
                    )
                with d_row_r:
                    if at_cap:
                        st.button("Full", key=f"promote_{d_ticker}", disabled=True, use_container_width=True)
                    elif st.button("Watchlist", icon=material("add"), key=f"promote_{d_ticker}", use_container_width=True):
                        add_tickers([d_ticker])
                        st.success(f"Added {d_ticker} to watchlist.", icon=material("check_circle"))
                        st.rerun()
