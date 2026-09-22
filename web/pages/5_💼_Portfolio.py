"""
Portfolio Manager — import holdings from Fidelity / Vanguard CSV exports and
view current positions, all DB-backed (portfolio_agent.tools.holdings_db).
Also hosts the Watchlist tab (tracked-but-not-owned tickers) alongside
Holdings, sharing this screen.

Upload flow (ad-hoc, user-initiated):
  1. Click "Upload CSV" on the broker card
  2. Export Holdings CSV from your broker (Fidelity: Positions → Download CSV;
     Vanguard: My Accounts → Holdings → Download)
  3. Drop the file — parser auto-detects broker, imports rows, enriches with
     live prices from yfinance, writes to the holdings DB
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import streamlit as st

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from web.styles import (
    inject_global_css, page_header, section_title, badge_html, top_nav, card,
    ticker_label, icon_html, material, status_dot_html, fmt_money, fmt_pct,
    SUCCESS, WARNING, DANGER, PRIMARY, NEUTRAL,
    SUCCESS_LIGHT, WARNING_LIGHT, PRIMARY_LIGHT,
)
from web.data.portfolio import _enrich_prices, group_holdings_by_account, missing_account_labels
from web.data.watchlist import (
    load_watchlist_tickers, watchlist_db_status,
    add_tickers as wl_add_tickers, remove_tickers as wl_remove_tickers,
)
from web.components.portfolio_cards import _fmt_shares, _pnl_html
from web.components.portfolio_charts import build_portfolio_trend_chart
from portfolio_agent.tools.company_names import get_company_names, get_company_name, search_companies

st.set_page_config(
    page_title="Portfolio — Portfolio Intelligence",
    page_icon=material("work"),
    layout="wide",
    initial_sidebar_state="expanded",
)
inject_global_css()
top_nav("portfolio")

with st.sidebar:
    st.markdown('<p style="font-size:0.68rem;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;color:#6B7280;margin:0 0 10px">Portfolio</p>', unsafe_allow_html=True)

page_header(
    "Portfolio Manager",
    subtitle="Import holdings from Fidelity or Vanguard",
    icon="work",
)

# ── Imports ───────────────────────────────────────────────────────────────────
from portfolio_agent.tools.holdings_db import (
    upsert_holdings, get_holdings, get_holdings_summary,
    delete_broker_holdings, delete_account_holdings, get_price_history,
)
from portfolio_agent.tools.holdings_parser import parse_csv
from portfolio_agent.tools.llm_holdings_parser import is_excel, parse_holdings_with_llm


BROKER_META = {
    "fidelity": {
        "label": "Fidelity",
        "color": "#22863A",
        "bg": "#F0FDF4",
        "border": "#BBF7D0",
    },
    "vanguard": {
        "label": "Vanguard",
        "color": "#9B1C1C",
        "bg": "#FFF5F5",
        "border": "#FECACA",
    },
    "other": {
        "label": "Other broker",
        "color": "#1D4ED8",
        "bg": "#EFF6FF",
        "border": "#BFDBFE",
        "blurb": (
            "Schwab, E*TRADE, Robinhood, Merrill, Webull, Chase, or any other broker. "
            "Upload a holdings / positions export (CSV or Excel) and it will be read automatically — "
            "no need to pick a format."
        ),
        "instructions": (
            "1. Log into your broker's website\n"
            "2. Open your **Holdings** / **Positions** page\n"
            "3. Look for **Download**, **Export**, or a spreadsheet icon — CSV or Excel (.xlsx) both work\n"
            "4. Upload the downloaded file below. Multi-sheet workbooks are fine; "
            "cash, money-market and total rows are skipped automatically"
        ),
    },
}


def _handle_upload(broker_key: str, meta: dict, uploaded) -> None:
    """
    Parse an uploaded CSV and save it, scoped to just the account(s) it
    contains. If any account in the file has no account number (some
    single-account exports omit it), pause and ask for a nickname first —
    applies to whatever parser produced the rows (Fidelity, Vanguard, or a
    future generic one), since they all share the same holdings-row shape.
    """
    state_key = f"pending_upload_{broker_key}"
    file_sig = (uploaded.name, uploaded.size)
    pending = st.session_state.get(state_key)

    if pending is None or pending["sig"] != file_sig:
        with st.spinner(f"Parsing {meta['label']} holdings file…"):
            try:
                content = uploaded.read()
                # LLM-extracted files are saved under the card they were uploaded
                # through ("other" for the generic card), so the accounts show up
                # on that card and can be disconnected from it.
                if is_excel(content, uploaded.name):
                    # Spreadsheets always go through the generic LLM extractor —
                    # the hardcoded Fidelity/Vanguard parsers only understand CSV text.
                    detected_broker, parsed = broker_key, parse_holdings_with_llm(content, uploaded.name)
                else:
                    try:
                        detected_broker, parsed = parse_csv(content)
                    except ValueError:
                        # Unrecognised CSV layout → fall back to the LLM extractor.
                        detected_broker, parsed = broker_key, parse_holdings_with_llm(content, uploaded.name)
            except Exception as e:
                st.error(f"Import failed: {e}")
                return

        if not parsed:
            st.error("No valid holdings found in this file. Check that it is a holdings/positions export (CSV or Excel).")
            return

        pending = {"sig": file_sig, "broker": detected_broker, "parsed": parsed, "labels": {}, "saved": False}
        st.session_state[state_key] = pending

    # st.rerun() after a successful save re-executes this whole script, and the
    # file stays selected in the uploader widget across that rerun — without
    # this guard we'd re-parse and re-show the nickname prompt forever instead
    # of leaving the just-imported state alone.
    if pending["saved"]:
        return

    unnamed_groups = missing_account_labels(pending["parsed"])

    if unnamed_groups:
        st.warning(
            f"{len(unnamed_groups)} account(s) in this file have no account number. "
            "Give each one a nickname so it's tracked as its own account.",
            icon=material("warning"),
        )
        for group in unnamed_groups:
            suggestion = pending["labels"].get(group) or group or f"{meta['label']} account"
            prompt_target = f"'{group}'" if group else "the unnamed account"
            pending["labels"][group] = st.text_input(
                f"Nickname for {prompt_target}",
                value=suggestion,
                key=f"acctlabel_{broker_key}_{file_sig[0]}_{file_sig[1]}_{group}",
            ).strip()

        ready = all(pending["labels"].get(g) for g in unnamed_groups)
        if not ready:
            st.info("Enter a nickname for every unnamed account above to continue.")
            return
        if not st.button(f"Confirm & save {meta['label']} import", key=f"confirm_{broker_key}"):
            return

    final_rows = []
    for h in pending["parsed"]:
        h = dict(h)
        if not (h.get("account_number") or "").strip():
            group = (h.get("account_name") or "").strip()
            label = pending["labels"].get(group, group)
            h["account_number"] = label
            if not h.get("account_name"):
                h["account_name"] = label
        final_rows.append(h)

    with st.spinner(f"Fetching live prices for {len(final_rows)} positions…"):
        final_rows = _enrich_prices(final_rows)

    today = date.today().isoformat()
    result = upsert_holdings(final_rows, pending["broker"], today)
    pending["saved"] = True

    st.success(f"Imported **{result['saved']} positions** from {meta['label']}.",
               icon=material("check_circle"))
    st.rerun()


@st.dialog("Remove holdings?")
def _confirm_remove(scope: str, broker: str, account_number: str, label: str,
                    n_positions: int, n_accounts: int = 1) -> None:
    """
    Warning screen shown when a trash button is clicked, so an accidental
    click never deletes anything. Nothing is removed until "Yes, remove".
    """
    if scope == "broker":
        what = (f"**all {n_positions} position{'s' if n_positions != 1 else ''}** across "
                f"**{n_accounts} account{'s' if n_accounts != 1 else ''}** at **{label}**")
    else:
        what = f"**{n_positions} position{'s' if n_positions != 1 else ''}** in **{label}**"

    st.warning(
        f"You're about to remove {what} from your portfolio.\n\n"
        "This can't be undone — to restore them you'll need to re-upload the holdings file.",
        icon=material("warning"),
    )
    c1, c2 = st.columns(2)
    if c1.button("Cancel", key="rm_cancel", use_container_width=True):
        st.rerun()
    if c2.button("Yes, remove", key="rm_confirm", type="primary",
                 icon=material("delete"), use_container_width=True):
        if scope == "broker":
            n = delete_broker_holdings(broker)
        else:
            n = delete_account_holdings(broker, account_number)
        st.session_state["holdings_flash"] = f"Removed {n} position{'s' if n != 1 else ''} from {label}."
        st.rerun()


def _render_holdings_tab() -> None:
    if flash := st.session_state.pop("holdings_flash", None):
        st.success(flash, icon=material("check_circle"))

    # ── Summary stats ─────────────────────────────────────────────────────────
    summary = get_holdings_summary()
    all_holdings = get_holdings()
    total_val = summary.get("total_value", 0)
    holding_count = summary.get("holding_count", 0)
    brokers_connected = summary.get("brokers", [])

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total Portfolio Value", fmt_money(total_val) if total_val else "—")
    col2.metric("Positions", holding_count)
    col3.metric("Brokers Connected", len(brokers_connected))
    col4.metric(
        "Last Synced",
        (summary.get("last_synced_at") or "Never")[:16].replace("T", " "),
    )

    st.divider()

    # ══════════════════════════════════════════════════════════════════════════
    # PORTFOLIO TREND CHART
    # ══════════════════════════════════════════════════════════════════════════

    history_rows = get_price_history()

    if not history_rows:
        st.info(
            "**Portfolio trend chart** — no history yet. "
            "The evening pipeline snapshots closing prices daily. "
            "Data will appear here after the first evening run.",
            icon=material("trending_up"),
        )
    else:
        from collections import defaultdict

        section_title("Portfolio Trend", badge_text="closing prices · daily snapshots")

        # ── Security-type classification from holdings descriptions ──────────
        _FUND_KW = ("ETF", " FUND", "INDEX FUND", "TRUST SHARES", "TRUST ETF")
        ticker_type: dict[str, str] = {}
        for h in all_holdings:
            desc = (h.get("description") or "").upper()
            ticker_type[h["ticker"]] = "Fund" if any(kw in desc for kw in _FUND_KW) else "Stock"

        # ── Sidebar filters ───────────────────────────────────────────────────
        all_brokers = sorted({r["broker"] or "manual" for r in history_rows})
        with st.sidebar:
            st.markdown(
                '<p style="font-size:0.68rem;font-weight:700;text-transform:uppercase;'
                'letter-spacing:0.08em;color:#6B7280;margin:16px 0 6px">Chart Filters</p>',
                unsafe_allow_html=True,
            )
            sel_broker = st.selectbox(
                "Broker", ["All"] + all_brokers, key="trend_broker",
                label_visibility="collapsed",
            )
            sel_type = st.radio(
                "Security type", ["All", "Stocks only", "Funds only"],
                key="trend_type",
            )

        def _type_ok(ticker: str) -> bool:
            if sel_type == "All":
                return True
            t = ticker_type.get(ticker, "Stock")
            return (sel_type == "Stocks only" and t == "Stock") or \
                   (sel_type == "Funds only"  and t == "Fund")

        filtered = [
            r for r in history_rows
            if (sel_broker == "All" or (r["broker"] or "manual") == sel_broker)
            and _type_ok(r["ticker"])
        ]

        # ── Total portfolio series ─────────────────────────────────────────────
        day_mv: dict[str, float] = defaultdict(float)
        day_cb: dict[str, float] = defaultdict(float)
        for r in filtered:
            if r["market_value"]:
                day_mv[r["date"]] += r["market_value"]
            if r["cost_basis"]:
                day_cb[r["date"]] += r["cost_basis"]

        total_dates = sorted(day_mv)
        total_mv    = [day_mv[d] for d in total_dates]
        total_cb    = [day_cb.get(d, 0) for d in total_dates]
        gain_abs    = [mv - cb for mv, cb in zip(total_mv, total_cb)]
        gain_pct_s  = [g / cb * 100 if cb else 0 for g, cb in zip(gain_abs, total_cb)]

        # ── Per-ticker series (normalized to % change from first date) ────────
        ticker_day: dict[str, dict[str, float]] = defaultdict(dict)
        for r in filtered:
            if r["market_value"]:
                t = r["ticker"]
                ticker_day[t][r["date"]] = ticker_day[t].get(r["date"], 0) + r["market_value"]

        ticker_list = sorted(ticker_day.keys())

        @st.cache_data(ttl=1800, show_spinner=False)
        def _load_index_series(start_date: str, end_date: str) -> dict[str, dict[str, float]]:
            from portfolio_agent.tools.yfinance_tools import get_closes_in_range
            return {
                symbol: get_closes_in_range(symbol, start_date, end_date)
                for symbol in ("^GSPC", "^IXIC", "^DJI")
            }

        index_series = _load_index_series(total_dates[0], total_dates[-1]) if total_dates else {}

        fig = build_portfolio_trend_chart(
            total_dates, total_mv, total_cb, gain_abs, gain_pct_s,
            ticker_list, ticker_day,
            index_series=index_series,
        )

        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

        # ── Stats strip below chart ────────────────────────────────────────────
        if filtered and total_dates:
            last_date  = total_dates[-1]
            first_date = total_dates[0]
            last_mv    = total_mv[-1]
            last_cb_v  = total_cb[-1]
            gain       = last_mv - last_cb_v
            gain_pct   = gain / last_cb_v * 100 if last_cb_v else 0
            gain_col   = SUCCESS if gain >= 0 else DANGER
            days_tracked = (datetime.fromisoformat(last_date) - datetime.fromisoformat(first_date)).days

            st.markdown(
                f'<div style="display:flex;flex-wrap:wrap;gap:24px 40px;padding:8px 2px 16px;'
                f'font-size:0.82rem;border-top:1px solid #F3F4F6;margin-top:-4px">'
                f'<span style="color:#9CA3AF">Period&nbsp;&nbsp;'
                f'<b style="color:#374151">{first_date} → {last_date}</b>'
                f'<span style="color:#D1D5DB"> · {days_tracked}d</span></span>'
                f'<span style="color:#9CA3AF">Value&nbsp;&nbsp;'
                f'<b style="color:#111827">{fmt_money(last_mv)}</b></span>'
                f'<span style="color:#9CA3AF">Unrealized&nbsp;&nbsp;'
                f'<b style="color:{gain_col}">{fmt_money(gain, signed=True)}&nbsp;({fmt_pct(gain_pct, signed=True)})</b></span>'
                f'</div>',
                unsafe_allow_html=True,
            )

    st.divider()

    # ══════════════════════════════════════════════════════════════════════════
    # BROKER CONNECTION CARDS
    # ══════════════════════════════════════════════════════════════════════════

    section_title("Broker Connections", badge_text="CSV / Excel upload · ad-hoc")

    # Per-broker tile tint — keyed containers expose a st-key-<key> class.
    st.markdown(
        "<style>" + "".join(
            f'[class*="st-key-broker_tile_{k}"] {{ background:{m["bg"]} !important; '
            f'border-color:{m["border"]} !important; border-radius:14px !important; }}'
            for k, m in BROKER_META.items()
        ) + "</style>",
        unsafe_allow_html=True,
    )

    broker_cols = st.columns(len(BROKER_META))

    for col, (broker_key, meta) in zip(broker_cols, BROKER_META.items()):
        connected = broker_key in brokers_connected
        broker_holdings = [h for h in all_holdings if h.get("broker") == broker_key]
        broker_val = sum(h.get("current_value") or 0 for h in broker_holdings)
        broker_accounts = group_holdings_by_account(broker_holdings)

        status_color = SUCCESS if connected else "#94A3B8"
        status_label = (
            f"{icon_html('check_circle', 13, color=SUCCESS)} Connected · "
            f"{len(broker_accounts)} account{'s' if len(broker_accounts) != 1 else ''} · "
            f"{len(broker_holdings)} positions"
            if connected else f"{status_dot_html('#94A3B8')} Not connected"
        )
        connect_blurb = meta.get("blurb") or f"Upload your {meta['label']} holdings export (CSV or Excel) to connect."

        with col, st.container(border=True, key=f"broker_tile_{broker_key}"):
            hdr_l, hdr_r = st.columns([6, 1], vertical_alignment="top")
            with hdr_l:
                st.markdown(
                    f'<div>{status_dot_html(meta["color"], 12)}'
                    f'<span style="font-size:1.1rem;font-weight:800;color:{meta["color"]};margin-left:8px">'
                    f'{meta["label"]}</span></div>'
                    f'<div style="font-size:0.78rem;color:{status_color};font-weight:600;margin-top:4px">'
                    f'{status_label}</div>',
                    unsafe_allow_html=True,
                )
            if connected:
                # Trash icon in the tile's top-right corner → opens the warning dialog.
                with hdr_r, st.container(key=f"tile_del_{broker_key}"):
                    if st.button("", key=f"del_{broker_key}", icon=material("delete"),
                                 help=f"Remove all {meta['label']} holdings"):
                        _confirm_remove("broker", broker_key, "", meta["label"],
                                        len(broker_holdings), len(broker_accounts))

            st.markdown(
                (
                    f'<div style="margin-top:4px;font-size:1.2rem;font-weight:800;color:#0F172A">'
                    f'{fmt_money(broker_val)}</div>'
                    f'<div style="font-size:0.75rem;color:#64748B;margin-bottom:8px">portfolio value</div>'
                    if connected else
                    f'<div style="margin-top:8px;font-size:0.8rem;color:#64748B;line-height:1.5;margin-bottom:8px">'
                    f'{connect_blurb}</div>'
                ),
                unsafe_allow_html=True,
            )

            # Account rows inside the tile — one line per account (replaces the
            # old separate "Connected Accounts" grid). Each row gets its own trash
            # icon only when the broker has 2+ accounts; with a single account the
            # tile's top-right icon already removes exactly that account.
            show_row_trash = len(broker_accounts) > 1
            for j, acct in enumerate(sorted(broker_accounts, key=lambda a: a["account_name"] or a["account_number"])):
                acct_label = acct["account_name"] or acct["account_number"] or "Unnamed account"
                acct_sub = acct["account_number"] if acct["account_name"] and acct["account_number"] else ""
                row_l, row_r = st.columns([6, 1], vertical_alignment="center")
                with row_l:
                    st.markdown(
                        f'<div style="display:flex;justify-content:space-between;align-items:baseline;'
                        f'gap:8px;padding:6px 0;border-top:1px solid rgba(0,0,0,0.06)">'
                        f'<span style="font-size:0.85rem;font-weight:700;color:#0F172A;white-space:nowrap;'
                        f'overflow:hidden;text-overflow:ellipsis" title="{acct_label}">{acct_label}'
                        f'{f"<span style=&quot;font-weight:500;color:#94A3B8;margin-left:6px&quot;>{acct_sub}</span>" if acct_sub else ""}'
                        f'</span>'
                        f'<span style="font-size:0.82rem;color:#374151;white-space:nowrap">'
                        f'<b>{fmt_money(acct["value"])}</b>'
                        f'<span style="color:#94A3B8"> · {acct["count"]} pos</span></span>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )
                if show_row_trash:
                    with row_r, st.container(key=f"tile_del_acct_{broker_key}_{j}"):
                        if st.button("", key=f"del_acct_{broker_key}_{j}", icon=material("delete"),
                                     help=f"Remove holdings from {acct_label} only"):
                            _confirm_remove("account", broker_key, acct["account_number"], acct_label, acct["count"])

            uploaded = st.file_uploader(
                f"{'Re-upload / add account' if connected else 'Upload'} {meta['label']} holdings (CSV or Excel)",
                type=["csv", "xlsx", "xls"],
                key=f"upload_{broker_key}",
            )
            if connected:
                st.caption("Uploading only adds/updates the account(s) in this file — other accounts are untouched.")

            if meta.get("instructions"):
                with st.expander("How to export from your broker"):
                    st.markdown(
                        f'<div style="font-size:0.82rem;color:#374151;white-space:pre-line">'
                        f'{meta["instructions"]}</div>',
                        unsafe_allow_html=True,
                    )

            if uploaded:
                _handle_upload(broker_key, meta, uploaded)

    st.divider()

    # Still needed below for the holdings-table account filter.
    all_accounts = group_holdings_by_account(all_holdings)

    # ── Manual add ────────────────────────────────────────────────────────────
    with st.expander("Add holding manually", icon=material("add")):
        mc1, mc2, mc3, mc4, mc5 = st.columns([2, 2, 2, 2, 2])
        with mc1:
            m_ticker = st.text_input("Ticker", placeholder="AAPL", key="m_ticker").upper().strip()
        with mc2:
            m_shares = st.number_input("Shares", min_value=0.0, step=0.001, format="%.2f", key="m_shares")
        with mc3:
            m_cost   = st.number_input("Avg cost / share ($)", min_value=0.0, step=0.01, key="m_cost")
        with mc4:
            m_acct   = st.text_input("Account name", placeholder="Brokerage", key="m_acct")
        with mc5:
            m_atype  = st.selectbox("Account type", ["TAXABLE","IRA","ROTH_IRA","401K","HSA"],
                                    key="m_atype")

        if st.button("Add", type="primary", key="m_add"):
            if not m_ticker:
                st.warning("Enter a ticker symbol.")
            else:
                holding = {
                    "ticker": m_ticker,
                    "description": "",
                    "shares": float(m_shares) if m_shares else None,
                    "avg_cost": float(m_cost) if m_cost else None,
                    "cost_basis_total": (m_shares * m_cost) if m_shares and m_cost else None,
                    "current_price": None,
                    "current_value": None,
                    "account_name": m_acct or "Manual",
                    "account_number": "",
                    "account_type": m_atype,
                    "sector": "",
                }
                # Enrich with live price
                enriched = _enrich_prices([holding])
                upsert_holdings(enriched, "manual", date.today().isoformat())
                st.success(f"Added {m_ticker} to portfolio.")
                st.rerun()

    # ══════════════════════════════════════════════════════════════════════════
    # HOLDINGS TABLE
    # ══════════════════════════════════════════════════════════════════════════

    if all_holdings:
        with st.expander(f"Current Holdings — {holding_count} positions", expanded=False):
            # Account filter — broker alone no longer disambiguates once a broker
            # has more than one account, so filter by (broker, account) instead.
            acct_option_labels = [
                f'{BROKER_META.get(a["broker"], {}).get("label", a["broker"].title())} · '
                f'{a["account_name"] or a["account_number"] or "Unnamed account"}'
                for a in all_accounts
            ]
            acct_lookup = dict(zip(acct_option_labels, all_accounts))
            filter_account = st.selectbox(
                "Filter by account", ["All"] + acct_option_labels,
                key="hld_account_filter",
                label_visibility="collapsed",
            )
            if filter_account != "All":
                sel = acct_lookup[filter_account]
                display_holdings = [
                    h for h in all_holdings
                    if (h.get("broker") or "unknown") == sel["broker"]
                    and (h.get("account_number") or "").strip() == sel["account_number"]
                ]
            else:
                display_holdings = all_holdings

            # Table header
            st.markdown(
                '<div style="display:grid;grid-template-columns:100px 220px 90px 100px 100px 110px 130px 90px;'
                'gap:8px;padding:6px 12px;background:#F1F5F9;border-radius:8px;'
                'font-size:0.72rem;font-weight:700;text-transform:uppercase;letter-spacing:0.05em;'
                'color:#475569;margin-bottom:4px">'
                '<span>Ticker</span><span>Description</span><span>Shares</span>'
                '<span>Avg Cost</span><span>Price</span><span>Value</span>'
                '<span>P&amp;L</span><span>Account</span>'
                '</div>',
                unsafe_allow_html=True,
            )

            holdings_names = get_company_names([h["ticker"] for h in display_holdings])
            for h in display_holdings:
                pnl = _pnl_html(h.get("cost_basis_total"), h.get("current_value"))
                broker_label = (h.get("broker") or "manual").title()
                account_label = h.get("account_name") or h.get("account_number") or (h.get("account_type") or "")
                # Broker CSV import already provides a description for most rows;
                # manually-added holdings don't have one, so fall back to the
                # SEC-registry company name in that case.
                desc_text = h.get("description") or holdings_names.get(h["ticker"].upper(), "")

                st.markdown(
                    f'<div style="display:grid;grid-template-columns:100px 220px 90px 100px 100px 110px 130px 90px;'
                    f'gap:8px;padding:10px 12px;background:white;border:1px solid #F1F5F9;'
                    f'border-radius:8px;margin-bottom:3px;font-size:0.85rem;align-items:center">'
                    f'<span style="font-weight:800;color:#0F172A">{h["ticker"]}</span>'
                    f'<span style="color:#64748B;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" '
                    f'title="{desc_text}">{desc_text[:28]}</span>'
                    f'<span>{_fmt_shares(h.get("shares"))}</span>'
                    f'<span>{fmt_money(h.get("avg_cost"))}</span>'
                    f'<span>{fmt_money(h.get("current_price"))}</span>'
                    f'<span style="font-weight:600">{fmt_money(h.get("current_value"))}</span>'
                    f'<span>{pnl}</span>'
                    f'<span style="font-size:0.75rem;color:#64748B">{broker_label}<br>'
                    f'<span style="color:#94A3B8">{account_label}</span></span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

            # Totals row
            total_cost = sum(h.get("cost_basis_total") or 0 for h in display_holdings)
            total_mkt  = sum(h.get("current_value") or 0 for h in display_holdings)
            total_pnl  = _pnl_html(total_cost, total_mkt) if total_cost and total_mkt else "—"
            st.markdown(
                f'<div style="display:grid;grid-template-columns:100px 220px 90px 100px 100px 110px 130px 90px;'
                f'gap:8px;padding:10px 12px;background:#F8FAFC;border:1px solid #E2E8F0;'
                f'border-radius:8px;margin-top:4px;font-size:0.85rem;font-weight:700;align-items:center">'
                f'<span style="color:#0F172A">Total</span><span></span><span></span>'
                f'<span>{fmt_money(total_cost) if total_cost else "—"}</span>'
                f'<span></span>'
                f'<span style="color:#0F172A">{fmt_money(total_mkt) if total_mkt else "—"}</span>'
                f'<span>{total_pnl}</span><span></span>'
                f'</div>',
                unsafe_allow_html=True,
            )

            st.markdown("<br>", unsafe_allow_html=True)

            # ── Sector breakdown ────────────────────────────────────────────────
            by_sector = summary.get("by_sector", {})
            if by_sector and total_val > 0:
                section_title("Sector Allocation")
                sector_cols = st.columns(min(len(by_sector), 4))
                sorted_sectors = sorted(by_sector.items(), key=lambda x: x[1], reverse=True)
                for i, (sector, val) in enumerate(sorted_sectors):
                    pct = val / total_val * 100 if total_val else 0
                    with sector_cols[i % len(sector_cols)]:
                        st.markdown(
                            f'<div style="background:white;border:1px solid #E2E8F0;border-radius:10px;'
                            f'padding:12px 14px;margin-bottom:8px">'
                            f'<div style="font-size:0.78rem;font-weight:600;color:#64748B">{sector or "Unknown"}</div>'
                            f'<div style="font-size:1rem;font-weight:800;color:#0F172A;margin-top:2px">'
                            f'{fmt_money(val)}</div>'
                            f'<div style="background:#E2E8F0;border-radius:4px;height:4px;margin-top:6px">'
                            f'<div style="background:{PRIMARY};border-radius:4px;height:4px;'
                            f'width:{min(pct, 100):.2f}%"></div></div>'
                            f'<div style="font-size:0.72rem;color:#94A3B8;margin-top:3px">'
                            f'{pct:.2f}% of portfolio</div>'
                            f'</div>',
                            unsafe_allow_html=True,
                        )

            # ── Rebalance ───────────────────────────────────────────────────────
            st.divider()
            section_title(
                "Rebalance",
                badge_text="new cash allocation · reduce candidates",
                badge_color=PRIMARY,
            )
            st.markdown(
                '<p style="font-size:0.82rem;color:#64748B;margin:-6px 0 14px">'
                'Where new cash should go — topping up existing conviction holdings or buying today\'s '
                'Opportunity Engine picks — plus which positions are worth trimming for concentration or '
                'a SELL signal. Deterministic scoring off stored APEX predictions and portfolio risk math, '
                'no live LLM calls.</p>',
                unsafe_allow_html=True,
            )

            cash_amount = st.number_input(
                "Cash available to invest ($)", min_value=0, value=10000, step=500, key="rebal_cash",
            )

            @st.cache_data(ttl=1800, show_spinner=False)
            def _cached_recommend_allocation(cash: float) -> dict:
                from portfolio_agent.tools.portfolio_optimizer import recommend_allocation
                return recommend_allocation(cash)

            if cash_amount <= 0:
                st.info("Enter a cash amount above to see an allocation recommendation.", icon=material("attach_money"))
            else:
                with st.spinner("Scoring candidates against your portfolio…"):
                    rebal = _cached_recommend_allocation(float(cash_amount))

                alloc_col, reduce_col = st.columns(2)

                with alloc_col:
                    st.markdown('<p style="font-size:0.8rem;font-weight:700;color:#0F172A;margin-bottom:8px">Recommended Allocation</p>', unsafe_allow_html=True)
                    if not rebal["allocation"]:
                        st.caption("No candidate cleared the conviction bar today — cash stays uninvested.")
                    for a in rebal["allocation"]:
                        kind_badge = badge_html("existing", PRIMARY, PRIMARY_LIGHT) if a["kind"] == "existing" else badge_html("new", SUCCESS, SUCCESS_LIGHT)
                        card(
                            f'<div style="display:flex;align-items:center;gap:10px">'
                            f'<div style="font-weight:800;color:#0F172A">{ticker_label(a["ticker"])}</div>'
                            f'{kind_badge}'
                            f'<div style="margin-left:auto;font-weight:800;color:{SUCCESS}">{fmt_money(a["amount"])}</div>'
                            f'</div>'
                            f'<p style="margin:8px 0 0;font-size:0.78rem;color:#64748B;line-height:1.4">{a["why"]}</p>',
                        )
                    if rebal["cash_reserved"] > 0:
                        card(
                            f'<div style="display:flex;align-items:center;gap:10px">'
                            f'<div style="font-weight:800;color:#0F172A">CASH</div>'
                            f'<div style="margin-left:auto;font-weight:800;color:{NEUTRAL}">{fmt_money(rebal["cash_reserved"])}</div>'
                            f'</div>'
                            f'<p style="margin:8px 0 0;font-size:0.78rem;color:#64748B">'
                            f'Undeployed — no further candidate cleared the conviction bar or position-size cap.</p>',
                        )

                with reduce_col:
                    st.markdown('<p style="font-size:0.8rem;font-weight:700;color:#0F172A;margin-bottom:8px">Consider Reducing</p>', unsafe_allow_html=True)
                    if not rebal["reduce"]:
                        st.caption("No current holding trips a SELL signal or concentration threshold today.")
                    for r in rebal["reduce"]:
                        card(
                            f'<div style="display:flex;align-items:center;gap:10px">'
                            f'<div style="font-weight:800;color:#0F172A">{ticker_label(r["ticker"])}</div>'
                            f'<div style="margin-left:auto;font-weight:800;color:{DANGER}">-{fmt_money(r["suggested_trim_dollars"])}</div>'
                            f'</div>'
                            f'<p style="margin:8px 0 0;font-size:0.78rem;color:#64748B;line-height:1.4">{r["rationale"]}</p>',
                        )

                before, after = rebal["impact"]["before"], rebal["impact"]["after"]
                if before and after:
                    st.markdown('<p style="font-size:0.8rem;font-weight:700;color:#0F172A;margin:18px 0 8px">Expected Portfolio Impact</p>', unsafe_allow_html=True)

                    # (metric key, label, unit, format fn, lower_is_better)
                    _IMPACT_METRICS = [
                        ("top_sector_pct", f"{before.get('top_sector') or 'Top sector'} %", True),
                        ("weighted_beta", "Portfolio beta", True),
                        ("portfolio_volatility_annualized", "Volatility (ann.)", True),
                        ("expected_return_21d", "Expected 21d return", False),
                        ("avg_pairwise_correlation", "Correlation concentration", True),
                    ]

                    def _fmt_metric(key: str, v) -> str:
                        if v is None:
                            return "—"
                        if key in ("top_sector_pct", "expected_return_21d"):
                            return f"{v:+.2f}%" if key == "expected_return_21d" else f"{v:.2f}%"
                        return f"{v:.2f}"

                    impact_cols = st.columns(len(_IMPACT_METRICS))
                    for col, (key, label, lower_is_better) in zip(impact_cols, _IMPACT_METRICS):
                        b_val, a_val = before.get(key), after.get(key)
                        delta_str = "—"
                        color = NEUTRAL
                        if b_val is not None and a_val is not None:
                            diff = a_val - b_val
                            improved = (diff < 0) if lower_is_better else (diff > 0)
                            color = SUCCESS if improved else (NEUTRAL if abs(diff) < 1e-6 else DANGER)
                            arrow = "↓" if diff < 0 else ("↑" if diff > 0 else "→")
                            delta_str = f"{arrow} {_fmt_metric(key, b_val)} → {_fmt_metric(key, a_val)}"
                        with col:
                            st.markdown(
                                f'<div style="background:white;border:1px solid #F3F4F6;border-radius:12px;'
                                f'padding:14px 16px;border-top:3px solid {color}">'
                                f'<p style="margin:0;font-size:0.66rem;font-weight:700;text-transform:uppercase;'
                                f'letter-spacing:0.06em;color:#9CA3AF">{label}</p>'
                                f'<p style="margin:6px 0 0;font-size:0.95rem;font-weight:800;color:{color}">{delta_str}</p>'
                                f'</div>',
                                unsafe_allow_html=True,
                            )
                elif rebal["allocation"] or rebal["reduce"]:
                    st.caption("Portfolio impact projection unavailable (price history fetch failed) — allocation and reduce recommendations above are unaffected.")

    else:
        st.info(
            "No holdings imported yet. Upload a Fidelity or Vanguard CSV above, "
            "or add positions manually.",
            icon=material("work"),
        )
        st.markdown(
            '<div style="background:#F8FAFC;border:1px solid #E2E8F0;border-radius:10px;'
            'padding:16px 20px;margin-top:12px">'
            '<h4 style="margin:0 0 8px;color:#0F172A;font-size:0.95rem">How it works</h4>'
            '<ol style="margin:0;color:#475569;font-size:0.85rem;line-height:2">'
            '<li>Export your holdings CSV from Fidelity or Vanguard (instructions in the cards above)</li>'
            '<li>Upload the file — broker is auto-detected, positions are parsed and prices fetched live</li>'
            '<li>Holdings are saved to the portfolio database</li>'
            '<li>The daily pipeline prioritises your portfolio tickers for analysis</li>'
            '</ol></div>',
            unsafe_allow_html=True,
        )


def _render_watchlist_tab() -> None:
    all_tickers = load_watchlist_tickers()
    db_status   = watchlist_db_status(all_tickers)

    # ── Top action bar ────────────────────────────────────────────────────────
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

    # ── Search / filter ───────────────────────────────────────────────────────
    search = st.text_input("Search tickers", placeholder="Type to filter…",
                           label_visibility="collapsed", key="wl_search")
    filter_missing = st.checkbox("Show tickers missing fundamentals", key="wl_filter_missing")

    display_tickers = all_tickers
    if search:
        display_tickers = [t for t in all_tickers if search.upper() in t]
    if filter_missing:
        display_tickers = [t for t in display_tickers if not db_status.get(t, {}).get("fund")]

    section_title(
        "Tickers",
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

        card_ = col.container(border=True, key=f"card_{ticker}")
        card_.markdown(
            f'<div style="text-align:center">'
            f'<div style="font-size:1rem;font-weight:800;color:#0F172A">{ticker}</div>'
            f'{name_line}'
            f'<div style="font-size:0.75rem;margin-top:6px;display:flex;justify-content:center;'
            f'gap:4px">{fund_dot}{res_dot}{pred_dot}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )
        if card_.button("", icon=material("delete"), key=f"grid_rm_{ticker}", help=f"Remove {ticker}"):
            wl_remove_tickers([ticker])
            st.success(f"Removed: {ticker}", icon=material("check_circle"))
            st.rerun()

    def _render_add_tile(col) -> None:
        tile = col.container(border=True, key="card_add_tile")
        tile.markdown(
            f'<div style="text-align:center;font-size:0.85rem;font-weight:700;color:#2563EB">'
            f'{icon_html("add", 14)} Add Ticker</div>',
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
                added, warning = wl_add_tickers(to_add)
                if warning:
                    st.warning(warning)
                if added:
                    st.success(f"Added: {', '.join(added)}", icon=material("check_circle"))
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
                        added, warning = wl_add_tickers([_tk])
                        if warning:
                            st.warning(warning)
                        elif added:
                            st.success(f"Added: {_tk}", icon=material("check_circle"))
                            st.rerun()

    # Ticker grid (4 per row) — the "Add Ticker" tile is appended as the last
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
        st.info("No tickers match your filter — the Add Ticker tile above still adds to the full watchlist.", icon=material("search"))


tab_holdings, tab_watchlist = st.tabs([
    f"{material('work')} Holdings", f"{material('list_alt')} Watchlist",
])
with tab_holdings:
    _render_holdings_tab()
with tab_watchlist:
    _render_watchlist_tab()
