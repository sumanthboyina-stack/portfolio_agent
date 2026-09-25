"""
Accounts & Imports — the data-management side of the portfolio: brokerage
accounts (rename / remove), broker file imports through the shared workflow,
manual position entry, and import history. The Portfolio page is about
understanding the investments; this page is about the data behind them.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from web.styles import inject_global_css, page_header, section_title, top_nav, material, fmt_money, icon_html, SUCCESS, section_tile
from web.data.portfolio import group_holdings_by_account, account_id, masked_account_number
from web.components.holdings_import import (
    render_import_flow, render_add_position_form, confirm_remove, broker_label, esc,
    bump_holdings_version,
)
from portfolio_agent.tools.holdings_db import get_holdings, get_cash_balances, get_import_runs, list_accounts
from portfolio_agent.services.account_service import rename_account, archive_account, restore_account, VersionConflict

from web.auth import current_context, require_login

st.set_page_config(page_title="APEX — Accounts & Imports", page_icon=str(_ROOT / "web" / "static" / "apex_mark.png"),
                   layout="wide", initial_sidebar_state="expanded")
inject_global_css()
require_login()
top_nav("accounts")
page_header("Accounts & Imports", subtitle="Brokerage accounts, holdings file imports, and import history",
            icon="account_balance")

if flash := st.session_state.pop("holdings_flash", None):
    st.success(flash, icon=material("check_circle"))

all_holdings = get_holdings()
cash_rows = get_cash_balances()
cash_by_acct = {account_id(c["broker"], c["account_number"]): c for c in cash_rows}
accounts = group_holdings_by_account(all_holdings)
# Accounts are records now: one that exists but holds no positions yet still shows up.
_known = {a["account_pk"] for a in accounts}
for _a in list_accounts():
    if _a["account_id"] not in _known:
        accounts.append({"account_id": account_id(_a["broker"], _a["account_number"]), "account_pk": _a["account_id"],
                         "broker": _a["broker"], "account_number": _a["account_number"],
                         "masked": masked_account_number(_a["account_number"]), "account_name": _a["display_name"] or "",
                         "holdings": [], "value": 0.0, "count": 0, "unvalued": 0})

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
                        f'{" · " + esc(acct["holdings"][0].get("account_type") or "") if acct["holdings"] else ""}</div>',
                        unsafe_allow_html=True)
                with c2:
                    valued = acct["count"] - acct["unvalued"]
                    st.markdown(
                        f'<div style="font-size:0.85rem;color:#374151"><b>{fmt_money(acct["value"]) if valued else "—"}</b> securities '
                        f'<span style="color:#94A3B8">({valued} of {acct["count"]} valued)</span>'
                        + (f' · cash <b>{fmt_money(cash["amount"])}</b>' if cash else ' · cash unknown') + '</div>'
                        f'<div style="font-size:0.75rem;color:#94A3B8">Last imported {esc(last_import) or "—"}'
                        + (f' · prices as of {esc(prices[0])}' + (f' → {esc(prices[-1])}' if len(prices) > 1 else "") if prices else "")
                        + '</div>', unsafe_allow_html=True)
                with c3:
                    r1, r2, r3 = st.columns([2.8, 1, 1])
                    with r1.popover("Rename", icon=material("edit"), width="stretch"):
                        new_name = st.text_input("Account name", value=acct["account_name"] or "", key=f"rename_{i}")
                        if st.button("Save name", key=f"rename_save_{i}", type="primary"):
                            try:
                                rename_account(current_context("web:accounts"), acct["account_pk"], new_name)
                                st.session_state["holdings_flash"] = f"Renamed account to {new_name} ({acct['count']} positions)."
                            except (ValueError, VersionConflict) as exc:
                                st.session_state["holdings_flash"] = f"Rename failed: {exc}"
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

        _archived = [a for a in list_accounts(include_archived=True) if a["status"] != "active"]
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