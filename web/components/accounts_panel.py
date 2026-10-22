"""
Accounts & Imports panel — the data-management side of the portfolio: brokerage
accounts (rename / archive / remove), instrument classification, the transaction
ledger, broker file imports through the shared workflow, manual position entry,
and import history. Rendered as the "Accounts & Imports" section of the
Portfolio page (web/pages/5_💼_Portfolio.py); it used to be its own page.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from web.styles import section_title, material, fmt_money, icon_html, SUCCESS, section_tile
from web.data.portfolio import group_holdings_by_account, account_id, masked_account_number
from web.components.holdings_import import (
    render_import_flow, render_add_position_form, render_add_cit_form, render_manual_breakdown_form, confirm_remove, broker_label, esc,
    bump_holdings_version,
)
from portfolio_agent.domain import ACCOUNT_TYPES, ASSET_CLASSES, INSTRUMENT_TYPES, is_synthetic_ticker, tax_treatment
from portfolio_agent.tools.holdings_db import get_import_runs
from portfolio_agent.services.account_service import (
    rename_account, archive_account, restore_account, VersionConflict, NotAuthorized,
    set_account_type, set_instrument_classification, set_position_price,
    list_holdings_for, get_cash_balances_for, list_accounts_for,
)

from web.auth import current_context

_RETIREMENT_TYPES = ("401K", "ROTH_401K", "403B", "457B")


def _plan_line(acct: dict, acct_rows: dict) -> str:
    """Plan facts for a retirement account: plan, provider, statement date, vested / unvested, loan (a loan is not a holding)."""
    row = acct_rows.get(acct["account_pk"]) or {}
    bits = [row.get("plan_name"), row.get("provider"), (f"statement {row['statement_period_end']}" if row.get("statement_period_end") else None)]
    unv = sum(max(0.0, float(h["current_value"]) - float(h["vested_value"])) for h in acct["holdings"]
              if h.get("current_value") is not None and h.get("vested_value") is not None)
    if row.get("vested_balance") is not None:
        bits.append(f"vested {fmt_money(row['vested_balance'])}")
    if unv > 0.005:
        bits.append(f"unvested {fmt_money(unv)} (left out of Health)")
    if row.get("loan_balance"):
        bits.append(f"loan {fmt_money(row['loan_balance'])} (not a holding)")
    return " · ".join(str(b) for b in bits if b)


def _statement_price_note(holdings: list[dict]) -> dict | None:
    """One line about funds priced from a statement (no market price): the statement date, and when it needs the next one (a setting)."""
    from datetime import datetime, timezone
    from portfolio_agent.tools import investor_settings_db as _sdb
    from portfolio_agent.tools.price_freshness import is_statement_priced, is_statement_price_stale, statement_price_due_date
    mine = [h for h in holdings if is_statement_priced(h.get("instrument_type"), h.get("ticker"))]
    if not mine:
        return None
    days = float(_sdb.get_portfolio_settings().get("statement_price_max_age_days") or 100)
    now = datetime.now(timezone.utc)
    dates = sorted({(h.get("price_as_of") or "")[:10] for h in mine if h.get("price_as_of")})
    if not dates:
        return {"text": f"{len(mine)} fund{'s' if len(mine) != 1 else ''} priced by hand — price date unknown", "color": "#D97706"}
    stale = [h for h in mine if is_statement_price_stale(h.get("price_as_of"), now, days)]
    newest, oldest = dates[-1], dates[0]
    if stale:
        return {"text": f"Needs next statement — {len(stale)} fund price{'s' if len(stale) != 1 else ''} from a statement dated {oldest} (older than {days:.0f} days)",
                "color": "#D97706"}
    due = statement_price_due_date(oldest, days)
    return {"text": f"Plan fund prices from the statement dated {newest if newest == oldest else f'{oldest} → {newest}'}; next one due by {due.isoformat()}", "color": "#64748B"}


def _type_label(account_type: str | None) -> str:
    if not account_type:
        return "Type not set"
    tax = tax_treatment(account_type)
    return f"{account_type.replace('_', ' ').title()}" + (f" · {tax.replace('_', ' ').lower()}" if tax else "")


def render_accounts_panel() -> None:
    if flash := st.session_state.pop("holdings_flash", None):
        st.success(flash, icon=material("check_circle"))

    _ctx = current_context("web:accounts")
    all_holdings = list_holdings_for(_ctx)
    cash_rows = get_cash_balances_for(_ctx)
    cash_by_acct = {account_id(c["broker"], c["account_number"]): c for c in cash_rows}
    accounts = group_holdings_by_account(all_holdings)
    # Accounts are records now: one that exists but holds no positions yet still shows up.
    _known = {a["account_pk"] for a in accounts}
    for _a in list_accounts_for(_ctx):
        if _a["account_id"] not in _known:
            accounts.append({"account_id": account_id(_a["broker"], _a["account_number"]), "account_pk": _a["account_id"],
                             "broker": _a["broker"], "account_number": _a["account_number"],
                             "masked": masked_account_number(_a["account_number"]), "account_name": _a["display_name"] or "",
                             "holdings": [], "value": 0.0, "count": 0, "unvalued": 0})

    _acct_types = {a["account_id"]: a.get("account_type") for a in list_accounts_for(_ctx)}
    _acct_rows = {a["account_id"]: a for a in list_accounts_for(_ctx)}

    # ── Accounts ──────────────────────────────────────────────────────────────────
    _tile_1 = section_tile("Accounts", badge_text=f"{len(accounts)} account{'s' if len(accounts) != 1 else ''}", expanded=True, key="accounts_1")
    if _tile_1:
        with _tile_1:
            st.caption("Holdings come from file imports or manual entry — there is no live broker connection. "
                       "An account is identified by its broker and account number; its name is just a label you can change.")
            if not accounts:
                st.info("No accounts yet. Import a holdings file or add a position manually below.", icon=material("account_balance"))

            for i, acct in enumerate(sorted(accounts, key=lambda a: (broker_label(a["broker"]), a["account_name"] or a["account_number"]))):
                cash = cash_by_acct.get(acct["account_id"])
                last_import = max((h.get("synced_at") or "" for h in acct["holdings"]), default="")[:16].replace("T", " ")
                prices = sorted({(h.get("price_as_of") or "")[:10] for h in acct["holdings"] if h.get("price_as_of")})
                with st.container(border=True):
                    c1, c2, c3 = st.columns([3.4, 3, 2.6], vertical_alignment="center")
                    with c1:
                        name = acct["account_name"] or acct["account_number"] or "Unnamed account"
                        st.markdown(
                            f'<div style="font-size:1rem;font-weight:800;color:#0F172A">{esc(name)} '
                            f'<span style="font-weight:500;color:#94A3B8;font-size:0.85rem">{esc(acct["masked"])}</span></div>'
                            f'<div style="font-size:0.78rem;color:#64748B">{esc(broker_label(acct["broker"]))}'
                            f' · {esc(_type_label(_acct_types.get(acct["account_pk"])))}</div>'
                            + (f'<div style="font-size:0.74rem;color:#64748B">{esc(_plan_line(acct, _acct_rows))}</div>' if _plan_line(acct, _acct_rows) else ""),
                            unsafe_allow_html=True)
                    _stmt = _statement_price_note(acct["holdings"])
                    with c2:
                        valued = acct["count"] - acct["unvalued"]
                        st.markdown(
                            f'<div style="font-size:0.85rem;color:#374151"><b>{fmt_money(acct["value"]) if valued else "—"}</b> securities '
                            f'<span style="color:#94A3B8">({valued} of {acct["count"]} valued)</span>'
                            + (f' · cash <b>{fmt_money(cash["amount"])}</b>' if cash else ' · cash unknown') + '</div>'
                            f'<div style="font-size:0.75rem;color:#94A3B8">Last imported {esc(last_import) or "—"}'
                            + (f' · prices as of {esc(prices[0])}' + (f' → {esc(prices[-1])}' if len(prices) > 1 else "") if prices else "")
                            + '</div>'
                            + (f'<div style="font-size:0.75rem;color:{_stmt["color"]}">{esc(_stmt["text"])}</div>' if _stmt else ""), unsafe_allow_html=True)
                    with c3:
                        r1, r2, r3 = st.columns([2.8, 1, 1])
                        with r1.popover("Edit", icon=material("edit"), width="stretch"):
                            new_name = st.text_input("Account name", value=acct["account_name"] or "", key=f"rename_{i}")
                            _cur_type = _acct_types.get(acct["account_pk"])
                            new_type = st.selectbox("Account type", ACCOUNT_TYPES, key=f"type_{i}",
                                                    index=ACCOUNT_TYPES.index(_cur_type) if _cur_type in ACCOUNT_TYPES else ACCOUNT_TYPES.index("OTHER"),
                                                    format_func=lambda t: t.replace("_", " ").title(),
                                                    help="Drives the tax treatment shown on the card (taxable, tax-deferred, tax-free).")
                            _plan_vals = None
                            if _cur_type in _RETIREMENT_TYPES:
                                _row = _acct_rows.get(acct["account_pk"]) or {}
                                st.caption("Retirement plan details (optional)")
                                _pn = st.text_input("Plan name", value=_row.get("plan_name") or "", key=f"plan_name_{i}")
                                _pv = st.text_input("Provider", value=_row.get("provider") or "", key=f"plan_prov_{i}")
                                _vb = st.number_input("Vested balance ($, 0 = not stated)", 0.0, value=float(_row.get("vested_balance") or 0.0), step=100.0, key=f"plan_vest_{i}")
                                _lb = st.number_input("Loan balance ($, not a holding)", 0.0, value=float(_row.get("loan_balance") or 0.0), step=100.0, key=f"plan_loan_{i}")
                                _plan_vals = {"plan_name": _pn, "provider": _pv, "vested_balance": _vb or None, "loan_balance": _lb or None}
                            if st.button("Save", key=f"rename_save_{i}", type="primary"):
                                try:
                                    _c = current_context("web:accounts")
                                    if _plan_vals is not None:
                                        from portfolio_agent.services.account_service import set_retirement_details
                                        set_retirement_details(_c, acct["account_pk"], reason="user edited plan details on Accounts page", **_plan_vals)
                                    if new_name.strip() and new_name.strip() != (acct["account_name"] or ""):
                                        rename_account(_c, acct["account_pk"], new_name)
                                    if new_type != _cur_type:
                                        set_account_type(_c, acct["account_pk"], new_type, reason="user changed on Accounts page")
                                    st.session_state["holdings_flash"] = f"Updated {new_name or name}."
                                except (ValueError, VersionConflict, NotAuthorized) as exc:
                                    st.session_state["holdings_flash"] = f"Update failed: {exc}"
                                bump_holdings_version()
                                st.rerun()
                        if r2.button("", key=f"archive_{i}", icon=material("archive"), width="stretch",
                                     help=f"Archive {name}: hide it from Portfolio and totals; positions, cash and history are kept and it can be restored below."):
                            archive_account(current_context("web:accounts"), acct["account_pk"], reason="user archived from Accounts page")
                            bump_holdings_version()
                            st.session_state["holdings_flash"] = f"Archived {name}."
                            st.rerun()
                        if r3.button("", key=f"remove_{i}", icon=material("delete"), width="stretch",
                                     help=f"Permanently delete all holdings and cash recorded for {name}"):
                            confirm_remove(acct["account_pk"], name, acct["count"])

            _archived = [a for a in list_accounts_for(_ctx, include_archived=True) if a["status"] != "active"]
            if _archived:
                with st.expander(f"Archived accounts ({len(_archived)})", icon=material("archive")):
                    for a in _archived:
                        ac1, ac2 = st.columns([5, 2], vertical_alignment="center")
                        ac1.markdown(f"**{esc(a['display_name'] or a['account_number'])}** · {esc(broker_label(a['broker']))} "
                                     f"<span style='color:#94A3B8'>{esc(masked_account_number(a['account_number']))}</span>",
                                     unsafe_allow_html=True)
                        if ac2.button("Restore", key=f"restore_{a['account_id']}", icon=material("unarchive"), width="stretch"):
                            restore_account(current_context("web:accounts"), a["account_id"], reason="user restored from Accounts page")
                            bump_holdings_version()
                            st.session_state["holdings_flash"] = f"Restored {a['display_name'] or a['account_number']}."
                            st.rerun()


    # ── What each holding is (instrument classification + manual override) ───────
    _tile_c = section_tile("What each holding is", badge_text="stock · ETF · fund · money market · …", expanded=False, key="accounts_class")
    if _tile_c:
        with _tile_c:
            st.caption("Classified automatically from market data. If it is wrong, override it — a manual choice is never "
                       "changed by the automatic check. The same security is shared across every account that holds it.")
            _by_inst: dict[int, dict] = {}
            for _h in all_holdings:
                if _h.get("instrument_id") is not None:
                    _by_inst.setdefault(_h["instrument_id"], _h)
            if not _by_inst:
                st.caption("No holdings yet.")
            else:
                st.dataframe(pd.DataFrame([{
                    "Ticker": ("(no ticker)" if is_synthetic_ticker(h["ticker"]) else h["ticker"]),
                    "Name": h.get("description") or "", "Type": h.get("instrument_type") or "UNKNOWN",
                    "Asset class": h.get("asset_class") or "UNKNOWN", "Sub-class": h.get("sub_asset_class") or "",
                    "Leverage": h.get("leverage_factor") if (h.get("leverage_factor") or 1.0) != 1.0 else None,
                    "Proxy": h.get("proxy_ticker") or "", "Set by": "you" if h.get("classification_source") == "manual" else "auto",
                } for h in _by_inst.values()]), hide_index=True, width="stretch")

                _pick = st.selectbox("Correct a holding", list(_by_inst), key="cls_pick",
                                     format_func=lambda k: f'{"(no ticker) " if is_synthetic_ticker(_by_inst[k]["ticker"]) else ""}'
                                                           f'{_by_inst[k]["ticker"] if not is_synthetic_ticker(_by_inst[k]["ticker"]) else _by_inst[k].get("description") or _by_inst[k]["ticker"]}')
                _cur = _by_inst[_pick]
                _types, _classes = sorted(INSTRUMENT_TYPES), sorted(ASSET_CLASSES)
                o1, o2, o3, o4 = st.columns(4)
                _t = o1.selectbox("Type", _types, key=f"cls_t_{_pick}",
                                  index=_types.index(_cur.get("instrument_type")) if _cur.get("instrument_type") in _types else _types.index("UNKNOWN"))
                _a = o2.selectbox("Asset class", _classes, key=f"cls_a_{_pick}",
                                  index=_classes.index(_cur.get("asset_class")) if _cur.get("asset_class") in _classes else _classes.index("UNKNOWN"))
                _lev = o3.number_input("Leverage", value=float(_cur.get("leverage_factor") or 1.0), step=1.0, key=f"cls_l_{_pick}",
                                       help="1 = normal, 2 / 3 = leveraged, negative = inverse (e.g. -1, -2).")
                _px = o4.text_input("Proxy ticker", value=_cur.get("proxy_ticker") or "", key=f"cls_p_{_pick}",
                                    help="Analytics only — never shown as your holding.").upper().strip()
                if st.button("Save override", type="primary", key=f"cls_save_{_pick}", icon=material("check")):
                    try:
                        set_instrument_classification(current_context("web:accounts"), _pick, instrument_type=_t, asset_class=_a,
                                                      leverage_factor=_lev, proxy_ticker=_px or None,
                                                      reason="user override on Accounts page")
                        st.session_state["holdings_flash"] = f"Saved your classification for {_cur['ticker']}."
                    except (ValueError, NotAuthorized) as exc:
                        st.session_state["holdings_flash"] = f"Could not save: {exc}"
                    bump_holdings_version()
                    st.rerun()

                _cits = [h for h in all_holdings if is_synthetic_ticker(h["ticker"])]
                if _cits:
                    st.divider()
                    st.markdown("**Prices for funds with no ticker** — entered by hand; they are never fetched from the market.")
                    for h in _cits:
                        p1, p2 = st.columns([3, 2], vertical_alignment="bottom")
                        _newp = p1.number_input(f'{h.get("description") or h["ticker"]} — unit price ($)', min_value=0.0, step=0.01,
                                                value=float(h.get("current_price") or 0.0), key=f"cit_price_{h['id']}")
                        if p2.button("Save price", key=f"cit_price_save_{h['id']}"):
                            try:
                                set_position_price(current_context("web:accounts"), h["id"], _newp, expected_version=h.get("version"),
                                                   reason="manual price on Accounts page")
                                st.session_state["holdings_flash"] = "Price saved."
                            except (ValueError, NotAuthorized, VersionConflict) as exc:
                                st.session_state["holdings_flash"] = f"Could not save price: {exc}"
                            bump_holdings_version()
                            st.rerun()

            # ── Transactions & ledger ─────────────────────────────────────────────────────
    _tile_ledger = section_tile("Transactions & ledger", badge_text="trades · lots · reconcile · CSV", expanded=False, key="accounts_ledger")
    if _tile_ledger:
        with _tile_ledger:
            from web.components.ledger_panel import render_ledger_panel
            render_ledger_panel(_ctx, list_accounts_for(_ctx), key="accounts_ledger")


            # ── Import ────────────────────────────────────────────────────────────────────
    _tile_2 = section_tile("Import holdings", badge_text="choose file → map accounts → preview → confirm", expanded=False, key="accounts_2")
    if _tile_2:
        with _tile_2:
            render_import_flow(key="accounts")


            # ── Manual entry ──────────────────────────────────────────────────────────────
    _tile_3 = section_tile("Add a position manually", expanded=False, key="accounts_3")
    if _tile_3:
        with _tile_3:
            render_add_position_form(accounts, key="accounts_add")


            # ── Fund with no ticker ───────────────────────────────────────────────────────
    _tile_cit = section_tile("Add a fund with no ticker (401k)", badge_text="collective investment trusts", expanded=False, key="accounts_cit")
    if _tile_cit:
        with _tile_cit:
            render_add_cit_form(accounts, key="accounts_cit_add")
            st.divider()
            st.markdown("**What a fund with no ticker is made of** — from your plan's fact sheet. Replaces a proxy ticker for that fund in look-through and Portfolio Health.")
            render_manual_breakdown_form(key="accounts_mb")


            # ── Import history ────────────────────────────────────────────────────────────
    _tile_4 = section_tile("Import history", badge_text="committed changes, not parsed rows", expanded=False, key="accounts_4")
    if _tile_4:
        with _tile_4:
            runs = get_import_runs(limit=50)
            if not runs:
                st.caption("No imports recorded yet.")
            else:
                df = pd.DataFrame([{
                    "imported_at": (r["imported_at"] or "")[:16].replace("T", " "),
                    "broker": broker_label(r["broker"]),
                    "accounts": ", ".join(str(a) for a in r["accounts"]),
                    "statement_date": r["statement_date"],
                    "positions": r["positions"], "added": r["added"], "updated": r["updated"], "removed": r["removed"],
                    "cash_accounts": r["cash_accounts"], "file": r["filename"],
                } for r in runs])
                st.dataframe(df, hide_index=True, width="stretch", column_config={
                    "imported_at": "Imported at", "broker": "Broker", "accounts": "Accounts", "statement_date": "Statement date",
                    "positions": "Positions", "added": "Added", "updated": "Updated", "removed": "Removed",
                    "cash_accounts": "Cash accts", "file": "File"})
