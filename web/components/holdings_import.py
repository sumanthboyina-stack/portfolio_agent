"""
Shared holdings-import interface — used by the Portfolio page's "Update
holdings" dialog and by the Accounts & Imports page, so there is exactly one
import workflow:

    Choose file → Select/map accounts → Preview changes → Confirm

All parsing, reconciliation and commits live in
portfolio_agent.tools.holdings_import; this module only renders the steps and
keeps the in-progress state in st.session_state["import_flow_<key>"].
"""

from __future__ import annotations

import html
import sys
from datetime import date
from pathlib import Path
from typing import Callable

import pandas as pd
import streamlit as st

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from web.styles import material, fmt_money, SUCCESS, DANGER, WARNING
from web.components.portfolio_cards import _fmt_shares
from web.data.portfolio import _enrich_prices
from portfolio_agent.services.account_service import (
    DuplicateAccount, DuplicatePosition, add_position, create_account, delete_account_permanently,
)
from portfolio_agent.tools.holdings_import import (
    BROKER_LABELS, ImportPlan, ImportResult, ParsedUpload, build_plan, commit_plan, parse_upload,
)
from web.auth import current_context

BROKER_META = {
    "fidelity": {"label": "Fidelity", "color": "#22863A",
                 "instructions": "Fidelity → Accounts & Trade → Portfolio → Positions → Download (CSV)."},
    "vanguard": {"label": "Vanguard", "color": "#9B1C1C",
                 "instructions": "Vanguard → My Accounts → Holdings → Download → CSV."},
    "other":    {"label": "Other broker", "color": "#1D4ED8",
                 "instructions": ("Schwab, E*TRADE, Robinhood, Merrill, Webull, Chase, or any other broker: open the "
                                  "Holdings / Positions page, look for Download or Export (CSV or Excel), upload it here. "
                                  "It is read automatically; cash, money-market and total rows are handled.")},
}
ACCOUNT_TYPES = ["TAXABLE", "IRA", "ROTH_IRA", "401K", "HSA"]
_STEPS = ["Choose file", "Map accounts", "Preview changes", "Confirm"]


def esc(v) -> str:
    """Escape imported/untrusted text before it goes into HTML text or attributes."""
    return html.escape(str(v if v is not None else ""), quote=True)


def bump_holdings_version() -> None:
    """Call after any holdings mutation so derived results (rebalance scenarios) go stale at once."""
    st.session_state["holdings_mutation_seq"] = st.session_state.get("holdings_mutation_seq", 0) + 1


def broker_label(broker: str | None) -> str:
    return BROKER_META.get(broker or "", {}).get("label", BROKER_LABELS.get(broker or "", (broker or "manual").title()))


def reset_import_flow(key: str) -> None:
    st.session_state.pop(f"import_flow_{key}", None)


def _steps_html(active: int) -> str:
    parts = []
    for i, s in enumerate(_STEPS):
        col = "#2563EB" if i == active else ("#059669" if i < active else "#9CA3AF")
        parts.append(f'<span style="color:{col};font-weight:{700 if i == active else 500}">{i + 1} {s}</span>')
    return ('<div style="display:flex;gap:14px;flex-wrap:wrap;font-size:0.78rem;margin:0 0 10px">'
            + '<span style="color:#D1D5DB">→</span>'.join(parts) + "</div>")


def render_import_flow(key: str, on_close: Callable[[], None] | None = None) -> None:
    """
    Render the shared import workflow. On completion the committed
    ImportResult's message is placed in st.session_state["holdings_flash"],
    the holdings version is bumped, `on_close` is called and the page reruns.
    """
    state_key = f"import_flow_{key}"
    state = st.session_state.setdefault(state_key, {"step": "choose", "labels": {}})

    def _close() -> None:
        reset_import_flow(key)
        if on_close:
            on_close()
        st.rerun()

    if state["step"] == "choose":
        st.markdown(_steps_html(0), unsafe_allow_html=True)
        c1, c2 = st.columns([1.2, 3])
        broker = c1.selectbox("Broker", list(BROKER_META), key=f"{key}_broker",
                              format_func=lambda k: BROKER_META[k]["label"],
                              help="Fidelity and Vanguard CSVs are detected automatically; anything else is read by the generic extractor.")
        uploaded = c2.file_uploader("Holdings export (CSV or Excel)", type=["csv", "xlsx", "xls"], key=f"{key}_file")
        with st.container():
            st.caption(BROKER_META[broker]["instructions"])
            st.caption("An import **replaces the complete holdings** of each account in the file (including positions "
                       "added manually to it) and records its cash line separately. Other accounts are untouched.")
        if uploaded is not None:
            sig = (uploaded.name, uploaded.size)
            if state.get("sig") != sig:
                with st.spinner(f"Parsing {uploaded.name}…"):
                    try:
                        parsed = parse_upload(uploaded.read(), uploaded.name, fallback_broker=broker)
                    except Exception as exc:
                        st.error(f"Import failed: {esc(exc)}", icon=material("error"))
                        return
                state.update({"sig": sig, "parsed": parsed, "labels": {},
                              "step": "map" if parsed.unnamed_groups else "preview"})
                st.rerun()
        if on_close and st.button("Cancel", key=f"{key}_cancel0"):
            _close()
        return

    parsed: ParsedUpload = state["parsed"]

    if state["step"] == "map":
        st.markdown(_steps_html(1), unsafe_allow_html=True)
        st.warning(f"{len(parsed.unnamed_groups)} account(s) in this file have no account number. Give each one a "
                   "nickname so it is tracked as its own account (re-use the same nickname next time to update it).",
                   icon=material("warning"))
        for group in parsed.unnamed_groups:
            target = f"'{group}'" if group else "the unnamed account"
            state["labels"][group] = st.text_input(
                f"Nickname for {target}", value=state["labels"].get(group) or group or f"{broker_label(parsed.broker)} account",
                key=f"{key}_label_{group}").strip()
        b1, b2 = st.columns([1, 1])
        ready = all(state["labels"].get(g) for g in parsed.unnamed_groups)
        if b1.button("Continue to preview", type="primary", key=f"{key}_to_preview", disabled=not ready):
            state["step"] = "preview"
            st.rerun()
        if b2.button("Start over", key=f"{key}_restart1"):
            reset_import_flow(key)
            st.rerun()
        return

    if state["step"] == "preview":
        st.markdown(_steps_html(2), unsafe_allow_html=True)
        try:
            plan: ImportPlan = build_plan(parsed, state["labels"])
        except Exception as exc:
            st.error(f"Could not prepare the import: {esc(exc)}", icon=material("error"))
            if st.button("Start over", key=f"{key}_restart_err"):
                reset_import_flow(key); st.rerun()
            return
        t = plan.totals
        st.markdown(
            f'<div style="font-size:0.9rem;color:#374151;margin-bottom:6px"><b>{esc(broker_label(plan.broker))}</b> · '
            f'{len(plan.accounts)} account{"s" if len(plan.accounts) != 1 else ""} · statement date '
            f'<b>{esc(plan.statement_date or "not in file (import date will be used)")}</b></div>'
            f'<div style="display:flex;gap:18px;font-size:0.85rem;margin-bottom:10px">'
            f'<span style="color:{SUCCESS}"><b>{t["added"]}</b> to add</span>'
            f'<span style="color:{WARNING}"><b>{t["updated"]}</b> to update</span>'
            f'<span style="color:{DANGER}"><b>{t["removed"]}</b> to remove</span>'
            f'<span style="color:#6B7280"><b>{t["unchanged"]}</b> unchanged</span>'
            f'<span style="color:#6B7280">cash for <b>{t["cash_accounts"]}</b> account{"s" if t["cash_accounts"] != 1 else ""}</span></div>',
            unsafe_allow_html=True,
        )
        for a in plan.accounts:
            with st.container(border=True):
                cash_txt = f" · cash {fmt_money(a.cash_amount)}" if a.cash_amount is not None else " · no cash line (cash will be unknown)"
                st.markdown(f"**{esc(a.label)}** · {len(a.rows)} positions in file, {a.existing_count} stored now{cash_txt}")
                rows = ([{"change": "add", "ticker": x, "shares": next(r.get("shares") for r in a.rows if r["ticker"] == x), "was": None} for x in a.added]
                        + [{"change": "update", "ticker": u["ticker"], "shares": u["new_shares"], "was": u["old_shares"]} for u in a.updated]
                        + [{"change": "remove", "ticker": x, "shares": None, "was": None} for x in a.removed])
                if rows:
                    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                                 column_config={"change": st.column_config.TextColumn("Change", width="small"),
                                                "ticker": st.column_config.TextColumn("Ticker", width="small"),
                                                "shares": st.column_config.NumberColumn("Shares (new)", format="%.3f"),
                                                "was": st.column_config.NumberColumn("Shares (before)", format="%.3f")})
                if a.unchanged:
                    st.caption(f"Unchanged: {esc(', '.join(a.unchanged))}")
        b1, b2 = st.columns([1, 1])
        if b1.button("Confirm & import", type="primary", key=f"{key}_confirm", icon=material("check")):
            with st.spinner("Importing and fetching prices…"):
                result: ImportResult = commit_plan(plan, context=current_context("web:import"))
            bump_holdings_version()
            st.session_state["holdings_flash"] = result.message
            _close()
        if b2.button("Start over", key=f"{key}_restart2"):
            reset_import_flow(key)
            st.rerun()


# ── Manual position entry (shared) ───────────────────────────────────────────

_NEW_ACCOUNT = "__new__"


def account_option_label(acct: dict) -> str:
    name = acct["account_name"] or acct["account_number"] or "Unnamed account"
    suffix = f" ({acct['masked']})" if acct.get("masked") and acct["masked"] != name else ""
    return f"{broker_label(acct['broker'])} · {name}{suffix}"


def render_add_position_form(accounts: list[dict], key: str = "add", on_close: Callable[[], None] | None = None) -> None:
    """Add exactly one position to an existing account (chosen by account_id) or a new manual account."""
    by_id = {a["account_id"]: a for a in accounts}
    options = [a["account_id"] for a in sorted(accounts, key=account_option_label)] + [_NEW_ACCOUNT]
    chosen = st.selectbox("Account", options, index=0 if accounts else len(options) - 1, key=f"{key}_acct_choice",
                          format_func=lambda k: ("＋ Create new account…" if k == _NEW_ACCOUNT
                                                 else account_option_label(by_id[k]) if k in by_id else str(k)),
                          help="Every account is its own bucket — adding here never affects other accounts.")
    if chosen == _NEW_ACCOUNT:
        nc1, nc2 = st.columns([3, 2])
        new_name = nc1.text_input("New account name", placeholder="e.g. Robinhood brokerage", key=f"{key}_new_acct_name").strip()
        new_type = nc2.selectbox("Account type", ACCOUNT_TYPES, key=f"{key}_new_acct_type")
        target = {"broker": "manual", "account_number": new_name, "account_name": new_name, "account_type": new_type}
    else:
        acct = by_id[chosen]
        first = acct["holdings"][0] if acct["holdings"] else {}
        target = {"broker": acct["broker"], "account_number": acct["account_number"],
                  "account_name": acct["account_name"], "account_type": first.get("account_type")}
        if acct["broker"] != "manual":
            st.caption("This account is fed by a broker file. A position added here stays until the next import for this account replaces its complete holdings.")
    pc1, pc2, pc3 = st.columns(3)
    p_ticker = pc1.text_input("Ticker", placeholder="AAPL", key=f"{key}_ticker").upper().strip()
    p_shares = pc2.number_input("Shares", min_value=0.0, step=0.001, format="%.3f", key=f"{key}_shares")
    p_cost = pc3.number_input("Avg cost / share ($)", min_value=0.0, step=0.01, key=f"{key}_cost")
    b1, b2 = st.columns([1, 1])
    if b1.button("Add position", type="primary", key=f"{key}_submit", icon=material("add")):
        if not p_ticker:
            st.warning("Enter a ticker symbol."); return
        if chosen == _NEW_ACCOUNT and not target["account_name"]:
            st.warning("Give the new account a name."); return
        holding = {"ticker": p_ticker, "description": "", "shares": float(p_shares) if p_shares else None,
                   "avg_cost": float(p_cost) if p_cost else None,
                   "cost_basis_total": round(p_shares * p_cost, 2) if p_shares and p_cost else None,
                   "current_price": None, "current_value": None, "sector": "",
                   **{k: target[k] for k in ("account_name", "account_number", "account_type")}}
        enriched = _enrich_prices([holding])[0]
        ctx = current_context("web:add_position")
        try:
            if chosen == _NEW_ACCOUNT:
                account_pk = create_account(ctx, broker="manual", display_name=new_name, account_type=new_type)["account_id"]
            else:
                account_pk = acct["account_pk"]
            add_position(ctx, account_pk, enriched, as_of_date=date.today().isoformat(), reason="added manually")
        except DuplicateAccount:
            st.error(f"An account named {target['account_name']} already exists — choose it from the list.",
                     icon=material("error")); return
        except DuplicatePosition:
            st.error(f"{p_ticker} is already in {target['account_name'] or 'this account'}. Select it in the holdings table to edit it.",
                     icon=material("error")); return
        bump_holdings_version()
        st.session_state["holdings_flash"] = f"Added {p_ticker} to {target['account_name'] or 'account'}."
        if on_close:
            on_close()
        st.rerun()
    if on_close and b2.button("Cancel", key=f"{key}_cancel"):
        on_close(); st.rerun()


@st.dialog("Remove account?")
def confirm_remove(account_pk: int, label: str, n_positions: int) -> None:
    """Confirmation before PERMANENTLY deleting an account with its positions and recorded cash (archive is the reversible option)."""
    st.warning(f"You're about to permanently remove **{n_positions} position{'s' if n_positions != 1 else ''}** in **{esc(label)}** "
               "from your portfolio, together with its recorded cash balance.\n\n"
               "This can't be undone — to restore them you'll need to import the holdings file again. "
               "If you only want it out of the way, use Archive instead.", icon=material("warning"))
    c1, c2 = st.columns(2)
    if c1.button("Cancel", key="rm_cancel", width="stretch"):
        st.rerun()
    if c2.button("Yes, remove permanently", key="rm_confirm", type="primary", icon=material("delete"), width="stretch"):
        res = delete_account_permanently(current_context("web:accounts"), account_pk, reason="user removed from Accounts page")
        bump_holdings_version()
        n = res["positions"]
        st.session_state["holdings_flash"] = f"Removed {n} position{'s' if n != 1 else ''} from {label}."
        st.rerun()
