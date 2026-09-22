"""Valuation — DCF intrinsic value (bear/base/bull), margin of safety, and relative comps."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import streamlit as st

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from web.styles import (
    inject_global_css, page_header, section_title, top_nav, card, material, icon_html,
    badge_html, ticker_label, stat_card_html, status_dot_html, fmt_money, fmt_pct,
    SUCCESS, SUCCESS_LIGHT, WARNING, WARNING_LIGHT, DANGER, DANGER_LIGHT, NEUTRAL, PRIMARY, PRIMARY_LIGHT,
)

st.set_page_config(
    page_title="Valuation — Portfolio Intelligence",
    page_icon=material("calculate"),
    layout="wide",
    initial_sidebar_state="expanded",
)
inject_global_css()
top_nav("valuation")

page_header(
    "Valuation",
    subtitle="DCF intrinsic value with explicit bear/base/bull scenarios, a WACC/terminal-growth "
             "sensitivity grid, and relative valuation vs peers and market — not a single fake-precise target",
    icon="calculate",
)

_LABEL_STYLE = {
    "UNDERVALUED":   (SUCCESS, SUCCESS_LIGHT, "UNDERVALUED"),
    "FAIRLY_VALUED": (WARNING, WARNING_LIGHT, "FAIRLY VALUED"),
    "OVERVALUED":    (DANGER, DANGER_LIGHT, "OVERVALUED"),
}


@st.cache_data(ttl=1800, show_spinner=False)
def _cached_stored_valuation(ticker: str):
    from portfolio_agent.tools.valuation_db import get_stored_valuation
    return get_stored_valuation(ticker)


@st.cache_data(ttl=1800, show_spinner=False)
def _cached_valuations_for_tickers(tickers: tuple[str, ...]) -> dict:
    from portfolio_agent.tools.valuation_db import get_valuations_for_tickers
    return get_valuations_for_tickers(list(tickers))


@st.cache_data(ttl=1800, show_spinner=False)
def _cached_holdings_tickers() -> list[str]:
    from portfolio_agent.tools.portfolio_tools import get_portfolio_holdings
    try:
        holdings = json.loads(get_portfolio_holdings()).get("holdings", [])
    except Exception:
        holdings = []
    return sorted({str(h["ticker"]).upper() for h in holdings if h.get("ticker")})


@st.cache_data(ttl=1800, show_spinner=False)
def _cached_opportunity_tickers() -> list[str]:
    from portfolio_agent.tools.opportunity_engine import get_daily_opportunities
    return [o["ticker"] for o in get_daily_opportunities(top_n=10)]


@st.cache_data(ttl=1800, show_spinner=False)
def _cached_watchlist_tickers() -> list[str]:
    """Watchlist tickers excluding portfolio holdings —
    matches the "Watchlist" segment definition used on the Validation page."""
    from portfolio_agent.tools.watchlist_db import load_watchlist_tickers
    try:
        watchlist = set(load_watchlist_tickers())
    except Exception:
        return []
    return sorted(watchlist - set(_cached_holdings_tickers()))


@st.cache_data(ttl=1800, show_spinner="Running DCF (EDGAR + market data, no LLM)…")
def _cached_compute_valuation(ticker: str):
    """On-demand compute for a ticker the daily batch hasn't covered yet."""
    from portfolio_agent.tools.edgar_check import _load_cik_map
    from portfolio_agent.tools.edgar import get_fundamentals_bundle
    from portfolio_agent.tools.yfinance_tools import get_valuation_multiples
    from portfolio_agent.tools.universe_db import get_peers
    from portfolio_agent.macro.fred_client import get_latest_value
    from portfolio_agent.tools.valuation_engine import (
        run_dcf_scenarios, sensitivity_grid, relative_valuation,
    )
    from portfolio_agent.tools.valuation_db import upsert_valuation, get_stored_valuation

    cik_map = _load_cik_map()
    bundle = get_fundamentals_bundle(ticker, cik_map)
    if bundle.get("error"):
        return None, f"No EDGAR data for {ticker} (ETF, foreign listing, or unrecognized ticker)."

    multiples = get_valuation_multiples(ticker)
    rf = get_latest_value("DGS10")
    risk_free_rate_pct = rf.get("value") if "error" not in rf else 4.0

    revenue = bundle["income"]["revenue"]
    operating_cf = bundle["cashflow"]["operating_cf"]
    capex = bundle["cashflow"]["capex"]

    scenarios = run_dcf_scenarios(revenue, operating_cf, capex, multiples, risk_free_rate_pct)
    if scenarios is None:
        return None, f"Insufficient data to run a DCF for {ticker} (needs revenue history, market cap, and shares outstanding)."

    peers = get_peers(ticker, limit=5)
    peer_multiples = [get_valuation_multiples(p) for p in peers]
    market_multiples = get_valuation_multiples("SPY")
    rel_val = relative_valuation(multiples, peer_multiples, market_multiples)
    grid = sensitivity_grid(revenue, operating_cf, capex, multiples, risk_free_rate_pct)

    upsert_valuation(ticker, scenarios, relative_valuation=rel_val, sensitivity_grid=grid)
    return get_stored_valuation(ticker), None


def _highlight_line(ticker: str, valuation: dict) -> str:
    # Note: this string is used as an st.expander() label, which only renders
    # plain/limited markdown (no raw HTML) — so status_dot_html() can't be used
    # here. label_words already states the direction in plain English instead.
    label = valuation.get("valuation_label") or "FAIRLY_VALUED"
    mos = valuation.get("margin_of_safety_pct")
    mos_str = fmt_pct(mos, signed=True)
    intrinsic = fmt_money(valuation.get("intrinsic_base"))
    current = fmt_money(valuation.get("current_price"))
    label_words = (label or "").replace("_", " ").title()
    return f"{ticker_label(ticker)}  —  {label_words}  ·  MoS {mos_str}  ·  {intrinsic} vs {current}"


def _render_detail(valuation: dict, ticker: str) -> None:
    """Full DCF/scenario/relative-valuation detail for one ticker — used both
    by the grouped list's expanders and the manual ticker lookup below."""
    st.caption(f"As of {valuation['as_of_date']} · model: {valuation.get('model_name', 'deterministic')}")

    label = valuation.get("valuation_label") or "FAIRLY_VALUED"
    label_color, label_bg, label_text = _LABEL_STYLE.get(label, (NEUTRAL, "#F9FAFB", label))

    hcol1, hcol2, hcol3, hcol4 = st.columns(4)
    with hcol1:
        st.markdown(stat_card_html(fmt_money(valuation["intrinsic_base"]), "Intrinsic Value (Base)", "track_changes", PRIMARY), unsafe_allow_html=True)
    with hcol2:
        st.markdown(stat_card_html(fmt_money(valuation["current_price"]), "Current Price", "attach_money", NEUTRAL), unsafe_allow_html=True)
    with hcol3:
        mos = valuation.get("margin_of_safety_pct")
        mos_color = SUCCESS if (mos or 0) > 0 else DANGER
        st.markdown(stat_card_html(fmt_pct(mos, signed=True), "Margin of Safety", "shield", mos_color), unsafe_allow_html=True)
    with hcol4:
        st.markdown(
            f'<div style="background:white;border:1px solid #F3F4F6;border-radius:14px;padding:18px 20px;'
            f'border-top:3px solid {label_color};text-align:center">'
            f'<p style="margin:0;font-size:0.68rem;font-weight:700;text-transform:uppercase;'
            f'letter-spacing:0.08em;color:#9CA3AF">Valuation</p>'
            f'<p style="margin:10px 0 0;font-size:1.1rem;font-weight:800;color:{label_color};'
            f'display:flex;align-items:center;justify-content:center;gap:6px">'
            f'{status_dot_html(label_color)}{label_text}</p>'
            f'</div>',
            unsafe_allow_html=True,
        )

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Bear / Base / Bull scenarios ──────────────────────────────────────────
    section_title(
        "Scenario Range",
        badge_text=f"expected value {fmt_money(valuation.get('expected_value'))}",
        badge_color=PRIMARY,
    )
    st.markdown(
        '<p style="font-size:0.78rem;color:#94A3B8;margin:-8px 0 12px">'
        'A probability-weighted range, not a single point estimate — the DCF\'s own uncertainty, '
        'shown rather than hidden behind one number.</p>',
        unsafe_allow_html=True,
    )
    scen_cols = st.columns(3)
    _SCENARIO_DEFS = [
        ("bear", "Bear", DANGER, DANGER_LIGHT),
        ("base", "Base", PRIMARY, PRIMARY_LIGHT),
        ("bull", "Bull", SUCCESS, SUCCESS_LIGHT),
    ]
    for col, (key, label_txt, color, bg) in zip(scen_cols, _SCENARIO_DEFS):
        with col:
            prob = valuation.get(f"probability_{key}")
            card(
                f'<div style="text-align:center">'
                f'<div style="font-size:0.72rem;font-weight:700;text-transform:uppercase;'
                f'letter-spacing:0.06em;color:{color}">{label_txt}</div>'
                f'<div style="font-size:1.6rem;font-weight:800;color:#0F172A;margin-top:6px">'
                f'{fmt_money(valuation.get(f"intrinsic_{key}"))}</div>'
                f'<div style="margin-top:6px">{badge_html(f"{prob*100:.2f}% probability", color, bg)}</div>'
                f'</div>',
                extra_style=f"border-top:3px solid {color}",
            )

    # ── Assumptions (auditable, not hidden) ───────────────────────────────────
    with st.expander("Assumptions used (base case)", icon=material("list_alt")):
        base_assumptions = (valuation.get("assumptions") or {}).get("base", {})
        if base_assumptions:
            acol1, acol2 = st.columns(2)
            with acol1:
                st.markdown(
                    f"- Trend revenue growth: **{base_assumptions.get('trend_revenue_growth', 0)*100:.2f}%**\n"
                    f"- FCF margin: **{base_assumptions.get('fcf_margin', 0)*100:.2f}%**\n"
                    f"- Terminal growth: **{base_assumptions.get('terminal_growth', 0)*100:.2f}%**\n"
                    f"- Projection years: **{base_assumptions.get('projection_years', 5)}**"
                )
            with acol2:
                st.markdown(
                    f"- WACC: **{base_assumptions.get('wacc', 0)*100:.2f}%** "
                    f"(cost of equity {base_assumptions.get('cost_of_equity', 0)*100:.2f}%, "
                    f"cost of debt {base_assumptions.get('cost_of_debt_after_tax', 0)*100:.2f}% *approximate*)\n"
                    f"- Risk-free rate: **{base_assumptions.get('risk_free_rate', 0)*100:.2f}%**\n"
                    f"- Beta used: **{base_assumptions.get('beta_used', 'n/a')}**\n"
                    f"- Tax rate: **{base_assumptions.get('tax_rate', 0)*100:.2f}%**"
                )
            st.caption(
                "Cost of debt is approximated (risk-free rate + a documented credit-spread proxy) — "
                "no real bond-yield or interest-expense data exists for this. FCF margin is held flat "
                "at its most recent EDGAR-derived value across the projection window."
            )
        else:
            st.caption("No assumption detail stored for this valuation.")

    # ── Sensitivity grid ───────────────────────────────────────────────────────
    grid = valuation.get("sensitivity_grid") or {}
    if grid.get("values"):
        section_title("Sensitivity — Intrinsic Value by WACC × Terminal Growth")
        wacc_deltas = grid["wacc_deltas_pp"]
        tg_deltas = grid["terminal_growth_deltas_pp"]
        header_cells = "".join(f'<th style="padding:6px 10px;font-size:0.75rem;color:#64748B">{tg:+.2f}pp</th>' for tg in tg_deltas)
        rows_html = ""
        for wacc_delta, row in zip(wacc_deltas, grid["values"]):
            is_center_row = wacc_delta == 0.0
            cells = "".join(
                f'<td style="padding:6px 10px;text-align:center;font-weight:{"800" if (wacc_delta==0.0 and tg==0.0) else "500"};'
                f'background:{"#EFF6FF" if (wacc_delta==0.0 and tg==0.0) else "transparent"}">'
                f'{fmt_money(v)}</td>'
                for tg, v in zip(tg_deltas, row)
            )
            rows_html += (
                f'<tr><td style="padding:6px 10px;font-size:0.75rem;color:#64748B;font-weight:{"800" if is_center_row else "500"}">'
                f'{wacc_delta:+.2f}pp</td>{cells}</tr>'
            )
        st.markdown(
            f'<div style="overflow-x:auto"><table style="border-collapse:collapse;width:100%;font-size:0.85rem">'
            f'<tr><th style="padding:6px 10px"></th>{header_cells}</tr>{rows_html}</table></div>'
            f'<p style="font-size:0.72rem;color:#94A3B8;margin-top:6px">Rows = WACC delta from base · '
            f'Columns = terminal growth delta from base · center cell (highlighted) = base case</p>',
            unsafe_allow_html=True,
        )

    # ── Relative valuation ─────────────────────────────────────────────────────
    rel = valuation.get("relative_valuation") or {}
    if rel.get("ticker_multiples"):
        section_title(
            "Relative Valuation",
            badge_text=f"vs {len(rel.get('peers_used', []))} peers",
            badge_color=PRIMARY,
        )
        tm, pm, mm = rel["ticker_multiples"], rel.get("peer_avg_multiples", {}), rel.get("market_multiples", {})
        _MULTIPLE_LABELS = [
            ("forward_pe", "Forward P/E"),
            ("ev_to_ebitda", "EV/EBITDA"),
            ("ev_to_revenue", "EV/Sales"),
            ("price_to_sales", "Price/Sales"),
            ("peg_ratio", "PEG"),
        ]

        def _fmt_mult(v):
            return f"{v:.2f}x" if v is not None else "—"

        rows_html = "".join(
            f'<tr><td style="padding:8px 10px;font-weight:600;color:#0F172A">{lbl}</td>'
            f'<td style="padding:8px 10px;text-align:right;font-weight:700">{_fmt_mult(tm.get(key))}</td>'
            f'<td style="padding:8px 10px;text-align:right;color:#64748B">{_fmt_mult(pm.get(key))}</td>'
            f'<td style="padding:8px 10px;text-align:right;color:#64748B">{_fmt_mult(mm.get(key))}</td></tr>'
            for key, lbl in _MULTIPLE_LABELS
        )
        if tm.get("fcf_yield_pct") is not None:
            rows_html += (
                f'<tr><td style="padding:8px 10px;font-weight:600;color:#0F172A">FCF Yield</td>'
                f'<td style="padding:8px 10px;text-align:right;font-weight:700">{tm["fcf_yield_pct"]:.2f}%</td>'
                f'<td style="padding:8px 10px;text-align:right;color:#64748B">—</td>'
                f'<td style="padding:8px 10px;text-align:right;color:#64748B">—</td></tr>'
            )
        market_note = " (fallback ~20x)" if mm.get("forward_pe_is_fallback") else ""
        st.markdown(
            f'<div style="overflow-x:auto"><table style="border-collapse:collapse;width:100%;font-size:0.85rem">'
            f'<tr><th style="text-align:left;padding:8px 10px;font-size:0.72rem;color:#94A3B8">Metric</th>'
            f'<th style="text-align:right;padding:8px 10px;font-size:0.72rem;color:#94A3B8">{ticker}</th>'
            f'<th style="text-align:right;padding:8px 10px;font-size:0.72rem;color:#94A3B8">Peer Avg</th>'
            f'<th style="text-align:right;padding:8px 10px;font-size:0.72rem;color:#94A3B8">Market{market_note}</th></tr>'
            f'{rows_html}</table></div>',
            unsafe_allow_html=True,
        )
        peers_used = rel.get("peers_used") or []
        if peers_used:
            st.caption(f"Peers (same sector, closest market cap): {', '.join(peers_used)}")

        hist = rel.get("approx_historical_pe_range")
        if hist:
            st.caption(
                f"Approximate 52-week P/E range: {hist['low']:.2f}x – {hist['high']:.2f}x "
                f"(52-week price range ÷ current TTM EPS — a simplification, not a true historical "
                f"P/E series, since historical EPS-by-period data isn't available in this system)."
            )


# ── Grouped ticker list — line item, expand for full detail ──────────────────

group_choice = st.radio(
    "Group", ["All", "Portfolio", "Watchlist", "New Opportunities"], horizontal=True, key="valuation_group",
)

if group_choice == "Portfolio":
    group_tickers = _cached_holdings_tickers()
elif group_choice == "Watchlist":
    group_tickers = _cached_watchlist_tickers()
elif group_choice == "New Opportunities":
    group_tickers = _cached_opportunity_tickers()
else:
    group_tickers = sorted(
        set(_cached_holdings_tickers())
        | set(_cached_watchlist_tickers())
        | set(_cached_opportunity_tickers())
    )

if not group_tickers:
    st.info(
        "No portfolio holdings found." if group_choice == "Portfolio"
        else "No tickers in this group yet.",
        icon=material("calculate"),
    )
else:
    valuations = _cached_valuations_for_tickers(tuple(group_tickers))
    valued_tickers = [t for t in group_tickers if t in valuations]
    unvalued_tickers = [t for t in group_tickers if t not in valuations]

    valued_tickers.sort(key=lambda t: valuations[t].get("margin_of_safety_pct") if valuations[t].get("margin_of_safety_pct") is not None else -1e9, reverse=True)

    section_title(
        f"{group_choice} — Valuation Summary",
        badge_text=f"{len(valued_tickers)} valued",
        badge_color=PRIMARY,
    )

    for ticker in valued_tickers:
        valuation = valuations[ticker]
        with st.expander(_highlight_line(ticker, valuation)):
            _render_detail(valuation, ticker)

    if unvalued_tickers:
        st.caption(
            f"No valuation yet for: {', '.join(unvalued_tickers)} — use \"Look up any ticker\" below to compute one."
        )

st.divider()

# ── Manual lookup — any ticker, on-demand compute if not yet covered ─────────

with st.expander("Look up any ticker", icon=material("search")):
    col1, col2 = st.columns([2, 1])
    with col1:
        ticker_input = st.text_input("Ticker", value="AAPL", key="valuation_ticker").strip().upper()
    with col2:
        st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
        force_recompute = st.button("Recompute now", icon=material("refresh"), use_container_width=True)

    if ticker_input:
        lookup_valuation = None if force_recompute else _cached_stored_valuation(ticker_input)
        lookup_error = None
        if lookup_valuation is None:
            lookup_valuation, lookup_error = _cached_compute_valuation(ticker_input)

        if lookup_error:
            st.error(lookup_error, icon=material("warning"))
        elif lookup_valuation is None:
            st.info(f"No valuation data for {ticker_input} yet.", icon=material("calculate"))
        else:
            _render_detail(lookup_valuation, ticker_input)
