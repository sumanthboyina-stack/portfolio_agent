"""
Portfolio Manager — import holdings from Fidelity / Vanguard CSV exports and
view current positions, all DB-backed (portfolio_agent.tools.holdings_db).
Also hosts the Watchlist tab (tracked-but-not-owned tickers) alongside
Holdings, sharing this screen.

Page order: scope selector → header (Update holdings · Manage accounts) →
value & data-quality summary → holdings table → allocation & performance →
rebalance scenario. Imports and account administration live on the Accounts &
Imports page (web/pages/13_🗂️_Accounts.py); "Update holdings" opens the same
shared import workflow (web/components/holdings_import.py) in a dialog. On an
empty portfolio the page shows an onboarding state instead. Everything follows
the selected scope unless labelled "All accounts".
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import streamlit as st

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from web.styles import (
    section_tile,
    inject_global_css, page_header, section_title, badge_html, top_nav, card,
    ticker_label, icon_html, material, status_dot_html, fmt_money, fmt_pct,
    SUCCESS, WARNING, DANGER, PRIMARY, NEUTRAL,
    SUCCESS_LIGHT, WARNING_LIGHT, PRIMARY_LIGHT,
)
from web.data.portfolio import (
    group_holdings_by_account, account_id, scope_holdings, summarize_holdings, holdings_table_rows,    is_fund, sector_breakdown, UNKNOWN_SECTOR,
)
from web.data.performance import build_trend_series, normalize_index_series
from web.data.watchlist import (
    load_watchlist_tickers, watchlist_db_status,
    add_tickers as wl_add_tickers, remove_tickers as wl_remove_tickers,
)
from web.components.portfolio_cards import _fmt_shares, _pnl_html
from web.components.portfolio_charts import (
    build_portfolio_trend_chart, build_sector_pie, build_value_stack_chart, VIEW_CHANGE, VIEW_GAIN, VIEW_RETURN,
)
from portfolio_agent.tools.company_names import get_company_names, get_company_name, search_companies
from portfolio_agent.services.account_service import set_position, remove_position, NotFound, VersionConflict
from portfolio_agent.tools.holdings_db import (
    get_holdings, get_cash_balances, get_holdings_version, get_price_history,
    get_history_coverage, repair_legacy_history,
    HISTORY_SOURCE_LEGACY, HISTORY_SOURCE_RECONSTRUCTED, HISTORY_SOURCE_BACKFILL,
    HISTORY_SOURCE_SNAPSHOT, HISTORY_SOURCE_ATTRIBUTED,
)
from portfolio_agent.tools.holdings_prices import refresh_holding_prices, backfill_missing_price_snapshots
from web.components.holdings_import import (
    BROKER_META, render_import_flow, render_add_position_form, bump_holdings_version as _bump_holdings_version,
    esc as _esc, broker_label as _broker_label,
)

from web.auth import current_context, require_login

st.set_page_config(
    page_title="APEX — Portfolio",
    page_icon=str(_ROOT / "web" / "static" / "apex_mark.png"),
    layout="wide",
    initial_sidebar_state="expanded",
)
inject_global_css()
require_login()
top_nav("portfolio")

with st.sidebar:
    st.markdown('<p style="font-size:0.68rem;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;color:#6B7280;margin:0 0 10px">Portfolio</p>', unsafe_allow_html=True)

page_header(
    "Portfolio Manager",
    subtitle="Your imported holdings, by account",
    icon="work",
)






# ── Account identity helpers ──────────────────────────────────────────────────



def _account_label(acct: dict) -> str:
    """Display label only — selection always goes through acct['account_id']."""
    name = acct["account_name"] or acct["account_number"] or "Unnamed account"
    suffix = f" ({acct['masked']})" if acct.get("masked") and acct["masked"] != name else ""
    return f"{_broker_label(acct['broker'])} · {name}{suffix}"


# ── 1. Scope selector ─────────────────────────────────────────────────────────

def _render_scope_selector(accounts: list[dict]) -> tuple[str, str]:
    by_id = {a["account_id"]: a for a in accounts}
    options = ["all"] + [a["account_id"] for a in sorted(accounts, key=_account_label)]
    if st.session_state.get("scope_account") not in options:
        st.session_state["scope_account"] = "all"
    scope = st.selectbox(
        "Portfolio scope", options, key="scope_account",
        format_func=lambda k: (f"All accounts ({len(accounts)})" if k == "all"
                               else _account_label(by_id[k]) if k in by_id else str(k)),
        help="Every section below follows this selection unless it says otherwise.",
    )
    label = "All accounts" if scope == "all" else _account_label(by_id[scope])
    return scope, label


# ── 2. Value & data-quality summary ───────────────────────────────────────────

def _render_value_summary(summary: dict, scope_label: str) -> None:
    fr = summary["freshness"]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Imported securities value", fmt_money(summary["securities_value"]) if summary["valued_count"] else "—",
              help="Sum of positions that have a current price. Cash is not included.")
    c2.metric("Cash (separate)", fmt_money(summary["cash_balance"]) if summary["cash_accounts"] else "—",
              help="Cash / money-market balances read from broker files. Kept apart from securities value.")
    unreal = summary["unrealized"]
    c3.metric("Unrealized gain/loss", fmt_money(unreal, signed=True) if unreal is not None else "—",
              delta=fmt_pct(summary["unrealized_pct"], signed=True) if summary["unrealized_pct"] is not None else None,
              help="Securities value minus recorded cost basis, for positions with both.")
    c4.metric("Positions", summary["holding_count"])

    n, valued = summary["holding_count"], summary["valued_count"]
    parts = [f"<b>{valued} of {n}</b> positions valued"]
    if summary["cash_accounts"]:
        parts.append(f"cash from {summary['cash_accounts']} account{'s' if summary['cash_accounts'] != 1 else ''} shown separately")
    else:
        parts.append("cash excluded (no cash lines in the imports)")
    if summary["unvalued_count"]:
        parts.append(f'<span style="color:{WARNING}">{summary["unvalued_count"]} price{"s" if summary["unvalued_count"] != 1 else ""} '
                     f'unavailable: {_esc(", ".join(summary["unvalued_tickers"]))}</span>')
    if summary["cost_known_count"] < valued:
        parts.append(f"cost basis known for {summary['cost_known_count']} of {valued}")

    def _rng(mm: dict, n_chars: int = 10) -> str:
        o, nw = (mm.get("oldest") or "")[:n_chars], (mm.get("newest") or "")[:n_chars]
        if not nw:
            return "—"
        return nw.replace("T", " ") if o == nw else f"{o} → {nw}".replace("T", " ")

    fresh = [
        f"Statement date <b>{_rng(fr['statement_date'])}</b>",
        f"Last imported <b>{_rng(fr['imported_at'], 16)}</b>",
        f"Prices as of <b>{_rng(fr['price_as_of'])}</b>"
        + (f' <span style="color:{WARNING}">({fr["price_missing"]} missing)</span>' if fr["price_missing"] else ""),
    ]
    st.markdown(
        f'<div style="font-size:0.8rem;color:#64748B;margin:-4px 0 6px">'
        f'<span style="color:#94A3B8">{_esc(scope_label)} · </span>{" · ".join(parts)}</div>'
        f'<div style="font-size:0.78rem;color:#94A3B8;margin-bottom:6px">{" &nbsp;·&nbsp; ".join(fresh)}</div>',
        unsafe_allow_html=True,
    )
    if st.button("Refresh prices", key="refresh_prices", icon=material("refresh"),
                 help="Re-fetch the latest close for every position (imported statement prices are not refreshed otherwise)."):
        with st.spinner("Fetching latest closes…"):
            r = refresh_holding_prices()
        _bump_holdings_version()
        st.session_state["holdings_flash"] = (
            f"Refreshed {r['updated']} prices as of {r['price_as_of'] or 'n/a'}."
            + (f" Unavailable: {', '.join(r['failed'])}." if r["failed"] else "")
        )
        st.rerun()


# ── 3. Holdings table ─────────────────────────────────────────────────────────

_TABLE_COLUMNS = {
    "ticker": st.column_config.TextColumn("Ticker", width="small"),
    "description": st.column_config.TextColumn("Description", width="medium"),
    "shares": st.column_config.NumberColumn("Shares", format="%.3f"),
    "avg_cost": st.column_config.NumberColumn("Avg cost", format="$%.2f"),
    "current_price": st.column_config.NumberColumn("Price", format="$%.2f"),
    "current_value": st.column_config.NumberColumn("Value", format="$%,.2f"),
    "cost_basis_total": st.column_config.NumberColumn("Cost basis", format="$%,.2f"),
    "gain": st.column_config.NumberColumn("Unrealized $", format="$%,.2f"),
    "gain_pct": st.column_config.NumberColumn("Unrealized %", format="%.2f%%"),
    "broker": st.column_config.TextColumn("Broker", width="small"),
    "account": st.column_config.TextColumn("Account", width="small"),
    "account_masked": st.column_config.TextColumn("Acct #", width="small"),
    "price_as_of": st.column_config.TextColumn("Price as of", width="small"),
}
_DEFAULT_COLS = ["ticker", "description", "shares", "avg_cost", "current_price", "current_value", "gain", "gain_pct", "account"]
_COMPACT_COLS = ["ticker", "shares", "current_value", "gain_pct"]


def _render_holdings_table(scoped: list, scope_label: str) -> None:
    names = get_company_names([h["ticker"] for h in scoped])
    rows = holdings_table_rows(scoped, names)
    df = pd.DataFrame(rows)

    f1, f2, f3, f4 = st.columns([3, 1.4, 1.4, 3])
    query = f1.text_input("Search holdings", placeholder="Ticker, description or account…",
                          key="hld_search", label_visibility="collapsed").strip().lower()
    group_by_account = f2.toggle("Group by account", key="hld_group", value=False)
    compact = f3.toggle("Compact", key="hld_compact", value=False,
                        help="Fewer columns for narrow screens.")
    if compact:
        cols = list(_COMPACT_COLS)
        f4.caption("Compact: ticker, shares, value, unrealized %")
    else:
        cols = f4.multiselect("Columns", list(_TABLE_COLUMNS), key="hld_cols",
                              default=_DEFAULT_COLS, label_visibility="collapsed")
    if group_by_account:
        cols = [c for c in cols if c not in ("account", "account_masked", "broker")]
    if query:
        mask = (df["ticker"].str.lower().str.contains(query, regex=False)
                | df["description"].str.lower().str.contains(query, regex=False)
                | df["account"].str.lower().str.contains(query, regex=False))
        df = df[mask]

    selected_id = None
    def _table(frame: pd.DataFrame, key: str):
        nonlocal selected_id
        ev = st.dataframe(
            frame, column_order=cols or _COMPACT_COLS, column_config=_TABLE_COLUMNS, hide_index=True,
            width="stretch", on_select="rerun", selection_mode="single-row", key=key,
        )
        sel = getattr(getattr(ev, "selection", None), "rows", []) or []
        if sel:
            selected_id = int(frame.iloc[sel[0]]["id"])

    if df.empty:
        st.caption("No positions match that search.")
    elif group_by_account:
        for acct_id, frame in df.groupby("account_id", sort=True):
            first = frame.iloc[0]
            v = frame["current_value"].sum(min_count=1)
            st.markdown(
                f'<div style="font-size:0.85rem;font-weight:700;color:#0F172A;margin:10px 0 2px">'
                f'{_esc(_broker_label(first["broker"]))} · {_esc(first["account"])} '
                f'<span style="font-weight:500;color:#94A3B8">{_esc(first["account_masked"])}</span>'
                f'<span style="float:right;font-weight:600;color:#374151">{fmt_money(None if pd.isna(v) else v)}'
                f'<span style="color:#94A3B8"> · {len(frame)} pos</span></span></div>',
                unsafe_allow_html=True,
            )
            _table(frame.reset_index(drop=True), key=f"hld_tbl_{acct_id}")
    else:
        _table(df.reset_index(drop=True), key="hld_tbl_all")

    # Totals — only over rows with known values; coverage stated, never $0.
    valued = df[df["current_value"].notna()] if not df.empty else df
    total_mkt = float(valued["current_value"].sum()) if len(valued) else None
    costed = df[df["cost_basis_total"].notna() & df["current_value"].notna()] if not df.empty else df
    total_cost = float(costed["cost_basis_total"].sum()) if len(costed) else None
    total_gain = (float(costed["current_value"].sum()) - total_cost) if total_cost is not None else None
    st.markdown(
        f'<div style="display:flex;flex-wrap:wrap;gap:8px 28px;font-size:0.82rem;color:#64748B;margin-top:4px">'
        f'<span>Value <b style="color:#0F172A">{fmt_money(total_mkt)}</b> '
        f'<span style="color:#94A3B8">({len(valued)} of {len(df)} valued)</span></span>'
        f'<span>Cost <b style="color:#0F172A">{fmt_money(total_cost)}</b> '
        f'<span style="color:#94A3B8">({len(costed)} with cost basis)</span></span>'
        f'<span>Unrealized {_pnl_html(total_cost, float(costed["current_value"].sum()) if len(costed) else None) if total_gain is not None else "—"}</span>'
        f'<span style="color:#94A3B8">Select a row to edit or remove that position.</span>'
        f'</div>',
        unsafe_allow_html=True,
    )

    if selected_id is not None:
        h = next((x for x in scoped if x.get("id") == selected_id), None)
        if h is not None:
            with st.container(border=True):
                _render_edit_position_form(h)


def _render_edit_position_form(h) -> None:
    """Edit shares / avg cost of exactly one position by id, or remove just it."""
    hid = h["id"]
    acct = h.get("account_name") or h.get("account_number") or "account"
    st.markdown(f"**Edit position — {_esc(h['ticker'])}** · {_esc(_broker_label(h.get('broker')))} · {_esc(acct)}")
    ec1, ec2 = st.columns(2)
    with ec1:
        e_shares = st.number_input("Shares", min_value=0.0, step=0.001, format="%.3f",
                                   value=float(h.get("shares") or 0.0), key=f"edit_shares_{hid}")
    with ec2:
        e_cost = st.number_input("Avg cost / share ($)", min_value=0.0, step=0.01,
                                 value=float(h.get("avg_cost") or 0.0), key=f"edit_cost_{hid}")
    b1, b2 = st.columns(2)
    if b1.button("Save changes", type="primary", key=f"edit_save_{hid}", icon=material("save"), width="stretch"):
        try:
            set_position(current_context("web:portfolio"), hid, expected_version=h.get("version"),
                         shares=float(e_shares) if e_shares else None, avg_cost=float(e_cost) if e_cost else None,
                         reason="edited in Portfolio")
            flash = f"Updated {h['ticker']}."
        except VersionConflict as exc:
            flash = f"{h['ticker']} was not saved: {exc}"
        except NotFound:
            flash = f"Could not find {h['ticker']} — it may have been removed."
        _bump_holdings_version()
        st.session_state["holdings_flash"] = flash
        st.rerun()
    if b2.button("Remove this position", key=f"edit_remove_{hid}", icon=material("delete"), width="stretch"):
        try:
            remove_position(current_context("web:portfolio"), hid, expected_version=h.get("version"),
                            reason="removed in Portfolio")
            flash = f"Removed {h['ticker']} from {acct}."
        except NotFound:
            flash = f"{h['ticker']} was already removed."
        except VersionConflict as exc:
            flash = f"{h['ticker']} was not removed: {exc}"
        _bump_holdings_version()
        st.session_state["holdings_flash"] = flash
        st.rerun()


# ── 4. Allocation & performance ───────────────────────────────────────────────

def _render_allocation(summary: dict, scope_label: str, scoped: list) -> None:
    breakdown = sector_breakdown(scoped)
    rows, total = breakdown["rows"], breakdown["total"]
    if not rows or total <= 0:
        st.caption("No valued positions to allocate.")
        return
    pie_col, tbl_col = st.columns([3, 2], vertical_alignment="center")
    with pie_col:
        st.plotly_chart(build_sector_pie(breakdown), width="stretch", config={"displayModeBar": False})
    with tbl_col:
        lines = "".join(
            f'<tr><td style="padding:5px 8px 5px 0;white-space:nowrap"><span style="display:inline-block;width:10px;height:10px;'
            f'border-radius:3px;background:{color};margin-right:8px;vertical-align:middle"></span>'
            f'<span style="font-weight:600;color:#0F172A">{_esc(r["sector"])}</span>'
            f'<div style="font-size:0.7rem;color:#94A3B8;margin-left:18px">{_esc(", ".join(r["tickers"][:6]))}{"…" if len(r["tickers"]) > 6 else ""}</div></td>'
            f'<td style="padding:5px 8px;text-align:right;font-weight:700;color:#0F172A;white-space:nowrap">{fmt_money(r["value"])}</td>'
            f'<td style="padding:5px 0;text-align:right;color:#64748B;white-space:nowrap">{r["pct"]:.1f}%</td></tr>'
            for r, color in zip(rows, _sector_colors(len(rows)))
        )
        st.markdown(f'<table style="width:100%;border-collapse:collapse;font-size:0.8rem">{lines}</table>', unsafe_allow_html=True)
        notes = []
        if any(r["sector"] == UNKNOWN_SECTOR for r in rows):
            notes.append("Unknown = no sector on the import and none in the cached universe data.")
        if breakdown["unvalued"]:
            notes.append(f"{breakdown['unvalued']} unvalued position(s) excluded.")
        if notes:
            st.caption(" ".join(notes))


def _sector_colors(n: int) -> list[str]:
    from web.components.portfolio_charts import OTHER_COLOR, PALETTE
    return [PALETTE[i] if i < 7 else OTHER_COLOR for i in range(n)]


def _render_trend(history_rows: list[dict], scoped: list, scope: str, scope_label: str) -> None:
    if scope != "all":
        history_rows = [r for r in history_rows
                        if account_id(r.get("broker"), r.get("account_number")) == scope]
    if not history_rows:
        st.info("No price history for this scope yet. The evening pipeline snapshots closing prices daily; "
                "history from before the per-account migration is only visible under All accounts.",
                icon=material("trending_up"))
        return

    ticker_type = {h["ticker"]: ("Fund" if is_fund(h) else "Stock") for h in scoped}
    ctl1, ctl2 = st.columns([2, 3])
    sel_type = ctl1.radio("Security type", ["All", "Stocks only", "Funds only"], key="trend_type",
                          horizontal=True, label_visibility="collapsed")
    def _type_ok(t: str) -> bool:
        if sel_type == "All":
            return True
        kind = ticker_type.get(t, "Stock")
        return (sel_type == "Stocks only" and kind == "Stock") or (sel_type == "Funds only" and kind == "Fund")
    filtered = [r for r in history_rows if _type_ok(r["ticker"])]
    series = build_trend_series(filtered)
    if not series["dates"]:
        st.caption("No history rows match this filter.")
        return

    has_return = any(v is not None for v in series.get("investment_return_pct", []))
    views = {"Value ($)": "value", "Unrealized gain/loss ($)": VIEW_GAIN, "Recorded value change (%)": VIEW_CHANGE}
    if has_return:
        views["Investment return (%)"] = VIEW_RETURN
    view_label = ctl2.segmented_control("View", list(views), default="Value ($)", key="trend_view",
                                        label_visibility="collapsed") or "Value ($)"
    fund_tickers = frozenset(t for t, k in ticker_type.items() if k == "Fund")

    if views[view_label] == "value":
        periods = {"1W": 5, "1M": 21, "3M": 63, "6M": 126, "All": None}
        period = st.segmented_control("Period", list(periods), default="1M", key="trend_period",
                                      label_visibility="collapsed") or "1M"
        fig, legend_items = build_value_stack_chart(series, fund_tickers, days=periods[period])
        st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
        with st.expander(f"Legend · {len(legend_items)} series, largest at the bottom of each bar", expanded=False):
            hatch = "background-image:repeating-linear-gradient(45deg,rgba(255,255,255,.6) 0 2px,transparent 2px 5px);"
            items = []
            for it in legend_items:
                swatch = (f'<span style="display:inline-block;width:11px;height:11px;border-radius:3px;background:{it["color"]};'
                          f'margin-right:8px;vertical-align:middle;{hatch if it["fund"] else ""}"></span>')
                suffix = ' <span style="color:#94A3B8">· fund</span>' if it["fund"] else ""
                items.append(f'<div>{swatch}<span style="color:#374151">{_esc(it["label"])}</span>{suffix}</div>')
            st.markdown('<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:6px 18px;'
                        'font-size:0.8rem">' + "".join(items) + "</div>", unsafe_allow_html=True)
    else:
        @st.cache_data(ttl=1800, show_spinner=False)
        def _load_index_series(start_date: str, end_date: str) -> dict[str, dict[str, float]]:
            from portfolio_agent.tools.yfinance_tools import get_closes_in_range
            return {s: get_closes_in_range(s, start_date, end_date) for s in ("^GSPC", "^IXIC", "^DJI")}
        index_pct = (normalize_index_series(_load_index_series(series["dates"][0], series["dates"][-1]))
                     if views[view_label] != VIEW_GAIN else {})
        st.plotly_chart(build_portfolio_trend_chart(series, index_pct, view=views[view_label]), width="stretch",
                        config={"displayModeBar": False})

    ret = [v for v in series["investment_return_pct"] if v is not None]
    first, last = series["dates"][0], series["dates"][-1]
    days = (datetime.fromisoformat(last) - datetime.fromisoformat(first)).days
    gain, gain_pct = series["gain_abs"][-1], series["gain_pct"][-1]
    gcol = SUCCESS if gain >= 0 else DANGER
    ret_html = (f'<span style="color:#9CA3AF">Investment return&nbsp;&nbsp;<b style="color:{SUCCESS if ret[-1] >= 0 else DANGER}">'
                f'{fmt_pct(ret[-1], signed=True)}</b><span style="color:#D1D5DB"> · {series["return_intervals"]} observed days</span></span>'
                if ret else
                '<span style="color:#9CA3AF">Investment return&nbsp;&nbsp;<b style="color:#374151">not yet computable</b>'
                '<span style="color:#D1D5DB"> · needs consecutive observed snapshots</span></span>')
    st.markdown(
        f'<div style="display:flex;flex-wrap:wrap;gap:24px 40px;padding:8px 2px 4px;font-size:0.82rem;'
        f'border-top:1px solid #F3F4F6;margin-top:-4px">'
        f'<span style="color:#9CA3AF">Period&nbsp;&nbsp;<b style="color:#374151">{first} → {last}</b>'
        f'<span style="color:#D1D5DB"> · {days}d</span></span>'
        f'<span style="color:#9CA3AF">Recorded value&nbsp;&nbsp;<b style="color:#111827">{fmt_money(series["value"][-1])}</b>'
        f'<span style="color:#D1D5DB"> · {fmt_pct(series["recorded_change_pct"][-1], signed=True)} incl. buys/sells</span></span>'
        f'<span style="color:#9CA3AF">Unrealized&nbsp;&nbsp;<b style="color:{gcol}">{fmt_money(gain, signed=True)}&nbsp;({fmt_pct(gain_pct, signed=True)})</b></span>'
        f'{ret_html}</div>',
        unsafe_allow_html=True,
    )
    st.caption(series["return_method"] + (f" {len(series['estimated_dates'])} of {len(series['dates'])} days are estimated "
               "(today's shares × that day's price) and are excluded from the return." if series["estimated_dates"] else ""))
    _render_history_coverage()


def _render_history_coverage() -> None:
    """Historical-data coverage (All accounts) with warnings for legacy / estimated rows."""
    cov = get_history_coverage()
    if not cov["days"]:
        return
    by_src = cov["by_source"]
    n_legacy = by_src.get(HISTORY_SOURCE_LEGACY, 0)
    if n_legacy:
        ld = cov["legacy_dates"]
        st.warning(
            f"**{len(ld)} day{'s' if len(ld) != 1 else ''} of history ({ld[0]} → {ld[-1]}) were stored per broker, "
            f"not per account.** On those days a ticker held in more than one account at the same broker kept only "
            f"one account's value, so totals undercount them. Repair attributes rows to the account that holds the "
            f"ticker where that is unambiguous, rebuilds collapsed rows from the recorded close × each account's "
            f"current shares (estimated), and leaves the rest flagged.",
            icon=material("warning"),
        )
        if st.button("Repair legacy history", key="hist_repair", icon=material("build"), type="primary"):
            r = repair_legacy_history()
            st.session_state["holdings_flash"] = (
                f"History repaired: {r['rekeyed']} rows attributed to their account "
                f"({r['estimates_replaced']} replaced an estimate), {r['rebuilt']} collapsed rows rebuilt per account "
                f"(estimated), {r['phantoms_removed']} estimated rows removed from snapshot days, {r['unresolved']} left flagged.")
            st.rerun()
    if cov["partial_days"]:
        pd_ = cov["partial_dates"]
        st.info(
            f"**{cov['partial_days']} day{'s' if cov['partial_days'] != 1 else ''}** ({pd_[0]} → {pd_[-1]}) have snapshots "
            f"for only some of your {cov['positions']} current positions — often because a position did not exist yet. "
            f"Filling them uses today's share counts, so filled rows are marked estimated and never count as observed.",
            icon=material("info"),
        )
        if st.button("Estimate missing history (today's shares × past price)", key="hist_backfill", icon=material("history")):
            with st.spinner("Fetching historical closes…"):
                r = backfill_missing_price_snapshots()
            st.session_state["holdings_flash"] = (
                f"Estimated {r['backfilled']} of {r.get('missing_rows', 0)} missing position-days."
                + (f" Error: {r['error']}" if r.get("error") else ""))
            st.rerun()
    labels = {HISTORY_SOURCE_SNAPSHOT: "observed snapshot", HISTORY_SOURCE_ATTRIBUTED: "observed (attributed)",
              HISTORY_SOURCE_BACKFILL: "estimated (backfill)", HISTORY_SOURCE_RECONSTRUCTED: "estimated (reconstructed)",
              HISTORY_SOURCE_LEGACY: "legacy broker-grain"}
    colors = {HISTORY_SOURCE_LEGACY: DANGER, HISTORY_SOURCE_BACKFILL: WARNING, HISTORY_SOURCE_RECONSTRUCTED: WARNING}
    src_html = " · ".join(f'<span style="color:{colors.get(s, "#374151")}">{n:,} {labels.get(s, s)}</span>'
                          for s, n in sorted(by_src.items(), key=lambda kv: -kv[1]))
    st.markdown(
        f'<div style="display:flex;flex-wrap:wrap;gap:8px 28px;padding:6px 2px 4px;font-size:0.78rem;color:#9CA3AF">'
        f'<span>Coverage (All accounts)&nbsp;&nbsp;<b style="color:#374151">{cov["complete_days"]}/{cov["days"]} days complete</b>'
        f'<span style="color:#D1D5DB"> · {cov["positions"]} positions · per account</span></span>'
        f'<span>Rows&nbsp;&nbsp;{src_html}</span></div>',
        unsafe_allow_html=True,
    )


# ── 5. Research / scenarios ───────────────────────────────────────────────────

def _research_version() -> str:
    try:
        from portfolio_agent.tools.db import db_conn
        with db_conn() as c:
            return str(c.execute("SELECT MAX(created_at) FROM predictions").fetchone()[0])
    except Exception:
        return "n/a"


def _constraints_version() -> str:
    try:
        from portfolio_agent.tools.user_profile_db import get_user_profile
        p = get_user_profile()
        return f"{p.max_sector_pct}|{p.max_issuer_pct}"
    except Exception:
        return "n/a"


def _render_rebalance(scoped: list, scope: str, scope_label: str) -> None:
    st.markdown(
        '<p style="font-size:0.82rem;color:#64748B;margin:-6px 0 14px">Where new cash should go and which positions '
        'are worth trimming, scored off stored APEX predictions and portfolio risk math. Results are kept until you '
        'recalculate; they are flagged stale when holdings, prices, research or constraints change.</p>',
        unsafe_allow_html=True,
    )
    cash_amount = st.number_input("Cash available to invest ($)", min_value=0, value=10000, step=500, key="rebal_cash")
    key = {
        "scope": scope, "holdings": get_holdings_version(),
        "mutations": st.session_state.get("holdings_mutation_seq", 0),
        "research": _research_version(), "constraints": _constraints_version(), "cash": float(cash_amount),
    }
    stored = st.session_state.get("rebal_scenario")
    stale = bool(stored) and stored["key"] != key
    changed = [k for k in key if stored and stored["key"].get(k) != key[k]]

    b1, b2 = st.columns([1.2, 4])
    label = "Recalculate" if stored else "Generate scenario"
    if b1.button(label, key="rebal_run", type="primary", icon=material("calculate"), disabled=cash_amount <= 0):
        with st.status("Scoring candidates against your portfolio…", expanded=True) as status:
            try:
                from portfolio_agent.tools.portfolio_optimizer import recommend_allocation
                st.write("Loading holdings, predictions and risk context…")
                result = recommend_allocation(float(cash_amount), holdings=[dict(h.items()) for h in scoped])
                status.update(label="Scenario ready", state="complete", expanded=False)
                st.session_state["rebal_scenario"] = {"key": key, "result": result, "error": None,
                                                      "computed_at": datetime.now().isoformat(timespec="minutes")}
            except Exception as exc:
                status.update(label="Scenario failed", state="error")
                st.session_state["rebal_scenario"] = {"key": key, "result": None, "error": str(exc),
                                                      "computed_at": datetime.now().isoformat(timespec="minutes")}
        st.rerun()
    if cash_amount <= 0:
        b2.info("Enter a cash amount to generate a scenario.", icon=material("attach_money"))
    elif not stored:
        b2.caption("Nothing calculated yet for this scope.")
    elif stale:
        b2.warning(f"Stale — {', '.join(changed)} changed since {stored['computed_at'].replace('T', ' ')}. Recalculate to refresh.",
                   icon=material("update"))
    else:
        b2.caption(f"Calculated {stored['computed_at'].replace('T', ' ')} for {scope_label}.")

    if not stored:
        return
    if stored.get("error"):
        st.error(f"Last run failed: {_esc(stored['error'])}", icon=material("error"))
        return
    rebal = stored["result"]
    alloc_col, reduce_col = st.columns(2)
    with alloc_col:
        st.markdown('<p style="font-size:0.8rem;font-weight:700;color:#0F172A;margin-bottom:8px">Recommended Allocation</p>', unsafe_allow_html=True)
        if not rebal["allocation"]:
            st.caption("No candidate cleared the conviction bar — cash stays uninvested.")
        for a in rebal["allocation"]:
            kind_badge = badge_html("existing", PRIMARY, PRIMARY_LIGHT) if a["kind"] == "existing" else badge_html("new", SUCCESS, SUCCESS_LIGHT)
            card(f'<div style="display:flex;align-items:center;gap:10px"><div style="font-weight:800;color:#0F172A">{_esc(ticker_label(a["ticker"]))}</div>'
                 f'{kind_badge}<div style="margin-left:auto;font-weight:800;color:{SUCCESS}">{fmt_money(a["amount"])}</div></div>'
                 f'<p style="margin:8px 0 0;font-size:0.78rem;color:#64748B;line-height:1.4">{_esc(a["why"])}</p>')
        if rebal["cash_reserved"] > 0:
            card(f'<div style="display:flex;align-items:center;gap:10px"><div style="font-weight:800;color:#0F172A">CASH</div>'
                 f'<div style="margin-left:auto;font-weight:800;color:{NEUTRAL}">{fmt_money(rebal["cash_reserved"])}</div></div>'
                 f'<p style="margin:8px 0 0;font-size:0.78rem;color:#64748B">Undeployed — no further candidate cleared the conviction bar or position-size cap.</p>')
    with reduce_col:
        st.markdown('<p style="font-size:0.8rem;font-weight:700;color:#0F172A;margin-bottom:8px">Consider Reducing</p>', unsafe_allow_html=True)
        if not rebal["reduce"]:
            st.caption("No holding trips a SELL signal or concentration threshold.")
        for r in rebal["reduce"]:
            card(f'<div style="display:flex;align-items:center;gap:10px"><div style="font-weight:800;color:#0F172A">{_esc(ticker_label(r["ticker"]))}</div>'
                 f'<div style="margin-left:auto;font-weight:800;color:{DANGER}">-{fmt_money(r["suggested_trim_dollars"])}</div></div>'
                 f'<p style="margin:8px 0 0;font-size:0.78rem;color:#64748B;line-height:1.4">{_esc(r["rationale"])}</p>')

    before, after = (rebal.get("impact") or {}).get("before"), (rebal.get("impact") or {}).get("after")
    if before and after:
        st.markdown('<p style="font-size:0.8rem;font-weight:700;color:#0F172A;margin:18px 0 8px">Expected Portfolio Impact</p>', unsafe_allow_html=True)
        metrics = [("top_sector_pct", f"{before.get('top_sector') or 'Top sector'} %", True), ("weighted_beta", "Portfolio beta", True),
                   ("portfolio_volatility_annualized", "Volatility (ann.)", True), ("expected_return_21d", "Expected 21d return", False),
                   ("avg_pairwise_correlation", "Correlation concentration", True)]
        def _fmt_metric(key, v):
            if v is None:
                return "—"
            if key == "expected_return_21d":
                return f"{v:+.2f}%"
            return f"{v:.2f}%" if key == "top_sector_pct" else f"{v:.2f}"
        for col, (k, lbl, lower_better) in zip(st.columns(len(metrics)), metrics):
            b_val, a_val = before.get(k), after.get(k)
            delta_str, color = "—", NEUTRAL
            if b_val is not None and a_val is not None:
                diff = a_val - b_val
                improved = (diff < 0) if lower_better else (diff > 0)
                color = SUCCESS if improved else (NEUTRAL if abs(diff) < 1e-6 else DANGER)
                delta_str = f"{_fmt_metric(k, b_val)} → {_fmt_metric(k, a_val)}"
            with col:
                st.markdown(f'<div style="background:white;border:1px solid #F3F4F6;border-radius:12px;padding:14px 16px;border-top:3px solid {color}">'
                            f'<p style="margin:0;font-size:0.66rem;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:#9CA3AF">{_esc(lbl)}</p>'
                            f'<p style="margin:6px 0 0;font-size:0.95rem;font-weight:800;color:{color}">{delta_str}</p></div>', unsafe_allow_html=True)
    elif rebal["allocation"] or rebal["reduce"]:
        st.caption("Portfolio impact projection unavailable (price history fetch failed) — recommendations above are unaffected.")


# ── 6. Header actions: Update holdings / Manage accounts ─────────────────────
# Imports and account administration live on the Accounts & Imports page; the
# Portfolio page keeps a prominent entry point into the SAME import workflow.

_ACCOUNTS_PAGE = "pages/13_🗂️_Accounts.py"


def _close_dialogs() -> None:
    st.session_state["show_import_dialog"] = False
    st.session_state["show_add_dialog"] = False


@st.dialog("Update holdings", width="large")
def _update_holdings_dialog() -> None:
    render_import_flow(key="portfolio", on_close=_close_dialogs)


@st.dialog("Add a position")
def _add_position_dialog(accounts: list[dict]) -> None:
    render_add_position_form(accounts, key="portfolio_add", on_close=_close_dialogs)


def _render_portfolio_header(scope_label: str, summary: dict, accounts: list[dict]) -> None:
    fr = summary["freshness"]
    imported = (fr["imported_at"]["newest"] or "")[:10] or "never"
    prices = fr["price_as_of"]
    price_txt = ("—" if not prices["newest"] else prices["newest"][:10] if prices["oldest"] == prices["newest"]
                 else f"{prices['oldest'][:10]} → {prices['newest'][:10]}")
    h1, h2, h3 = st.columns([4, 1.7, 1.7], vertical_alignment="center")
    with h1:
        st.markdown(
            f'<div style="font-size:1.05rem;font-weight:800;color:#0F172A">My Portfolio '
            f'<span style="color:#94A3B8;font-weight:600">· {_esc(scope_label)}</span></div>'
            f'<div style="font-size:0.8rem;color:#64748B">Holdings last imported <b>{_esc(imported)}</b>'
            f'<span style="color:#CBD5E1"> · </span>Quotes as of <b>{_esc(price_txt)}</b>'
            + (f'<span style="color:{WARNING}"> ({fr["price_missing"]} missing)</span>' if fr["price_missing"] else "")
            + '</div>', unsafe_allow_html=True)
    if h2.button("Update holdings", key="open_import", type="primary", icon=material("upload"), width="stretch",
                 help="Import a broker holdings file — the same workflow as Accounts & Imports."):
        st.session_state["show_import_dialog"] = True
    with h3:
        st.page_link(_ACCOUNTS_PAGE, label="Manage accounts", icon=material("account_balance"), width="stretch")
    if st.session_state.get("show_import_dialog"):
        _update_holdings_dialog()
    if st.session_state.get("show_add_dialog"):
        _add_position_dialog(accounts)


def _render_onboarding(accounts: list[dict]) -> None:
    """First visit: importing is the primary action, right here on the Portfolio page."""
    st.markdown(
        '<div style="background:#F8FAFC;border:1px solid #E2E8F0;border-radius:14px;padding:28px 28px 8px;margin:8px 0 0;text-align:center">'
        '<div style="font-size:1.3rem;font-weight:800;color:#0F172A">Add your portfolio</div>'
        '<div style="font-size:0.9rem;color:#64748B;margin:6px 0 16px">Import a brokerage holdings file or enter positions manually.</div></div>',
        unsafe_allow_html=True)
    _sp, b1, b2, _sp2 = st.columns([2, 1.4, 1.4, 2])
    if b1.button("Import holdings", key="onboard_import", type="primary", icon=material("upload"), width="stretch"):
        st.session_state["show_import_dialog"] = True
    if b2.button("Add manually", key="onboard_add", icon=material("add"), width="stretch"):
        st.session_state["show_add_dialog"] = True
    st.markdown(
        '<div style="font-size:0.8rem;color:#94A3B8;text-align:center;margin-top:10px">Fidelity and Vanguard CSVs are '
        'detected automatically; other brokers\' CSV or Excel exports are read by the generic extractor. Cash is recorded '
        'separately from securities.</div>', unsafe_allow_html=True)
    if st.session_state.get("show_import_dialog"):
        _update_holdings_dialog()
    if st.session_state.get("show_add_dialog"):
        _add_position_dialog(accounts)


# ── Page assembly ─────────────────────────────────────────────────────────────

def _render_holdings_tab() -> None:
    if flash := st.session_state.pop("holdings_flash", None):
        st.success(flash, icon=material("check_circle"))

    all_holdings = get_holdings()
    cash_rows = get_cash_balances()
    accounts = group_holdings_by_account(all_holdings)

    if not all_holdings:
        _render_onboarding(accounts)
        return

    scope, scope_label = _render_scope_selector(accounts)
    scoped = scope_holdings(all_holdings, scope)
    scoped_cash = [c for c in cash_rows if scope == "all" or account_id(c["broker"], c["account_number"]) == scope]
    summary = summarize_holdings(scoped, scoped_cash)

    tiles = [
        ("My Holdings", scope_label, "work", True, lambda: (_render_portfolio_header(scope_label, summary, accounts),
                                                           _render_value_summary(summary, scope_label))),
        ("Holdings", f"{scope_label} · {len(scoped)} positions", "table_rows", False,
         lambda: _render_holdings_table(scoped, scope_label)),
        ("Sector Allocation", f"{scope_label} · valued positions only", "pie_chart", False,
         lambda: _render_allocation(summary, scope_label, scoped)),
        ("Performance", f"{scope_label} · daily closes", "trending_up", False,
         lambda: _render_trend(get_price_history(), scoped, scope, scope_label)),
        ("Rebalance scenario", f"{scope_label} · explicit recalculation", "calculate", False,
         lambda: _render_rebalance(scoped, scope, scope_label)),
    ]
    for title, badge, icon, expanded, render in tiles:
        tile = section_tile(title, badge_text=badge, icon=icon, expanded=expanded,
                            key=f"portfolio_{title.lower().replace(' ', '_')}")
        if tile:
            with tile:
                render()


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

    _tile_1 = section_tile("Tickers", badge_text=f"{len(display_tickers)} shown", badge_color=PRIMARY, expanded=True, key="portfolio_wl_1")
    if _tile_1:
        with _tile_1:

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
                div[class*="st-key-grid_rm_"] div[data-testid="stButton"] button:active {
                    background: transparent !important;
                    background-color: transparent !important;
                    border: none !important;
                    box-shadow: none !important;
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
                /* Keyboard users must still see where focus is. */
                div[class*="st-key-grid_rm_"] div[data-testid="stButton"] button:focus-visible {
                    outline: 2px solid #2563EB !important;
                    outline-offset: 2px !important;
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

    if _tile_1:
        with _tile_1:
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
