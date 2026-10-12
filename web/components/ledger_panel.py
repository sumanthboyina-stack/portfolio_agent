"""
Ledger panel for the Accounts page: per account, the transactions list, add-transaction form, void, snapshot -> ledger switch with an
opening-balance confirmation, reconciliation, open lots, realized gains, clearance badges, and CSV import / export.

All data comes from web/data/ledger.py (owner-checked through the service layer); all writes go through its submit / void / import functions,
so every change carries the caller's RequestContext, an ownership check and an audit event. Nothing here touches the database directly.

TODO hooks (deliberately not built): TWR / IRR performance, a paper-trading UI, wash-sale adjustment, choosing specific lots in the form,
broker sync.
"""
from __future__ import annotations

import html
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from portfolio_agent.services import ledger_service as led
from portfolio_agent.services.account_service import LedgerManaged, NotAuthorized, NotFound, VersionConflict
from web.data import ledger as ld
from web.styles import (DANGER, DANGER_LIGHT, PRIMARY, PRIMARY_LIGHT, SUCCESS, SUCCESS_LIGHT, WARNING, WARNING_LIGHT, badge_html, fmt_money,
                        material)

_TONE = {"good": (SUCCESS, SUCCESS_LIGHT), "bad": (DANGER, DANGER_LIGHT), "warn": (WARNING, WARNING_LIGHT), "neutral": ("#475569", "#F1F5F9"),
         "none": ("#475569", "#F1F5F9")}
_ERRORS = (ld.FormError, led.InvalidTransaction, led.ImportRejected, LedgerManaged, NotAuthorized, NotFound, VersionConflict, ValueError)


def _e(v) -> str:
    return html.escape(str(v if v is not None else ""), quote=True)


def _badge(label: str, tone: str) -> str:
    c, bg = _TONE.get(tone, _TONE["neutral"])
    return badge_html(_e(label), c, bg, size="0.72rem") if label else ""


def _fmt_qty(q) -> str:
    return "" if q is None else f"{q:,.6f}".rstrip("0").rstrip(".")


def _money(v) -> str:
    return "" if v is None else fmt_money(v)


def _transactions_html(rows: list[dict]) -> str:
    """A compact table; voided transactions and the VOID rows that cancel them are struck through (greyed)."""
    head = ["Date", "Type", "Ticker", "Qty", "Price", "Amount", "Method", "Clearance", "Note"]
    out = ['<table style="width:100%;border-collapse:collapse;font-size:0.82rem"><thead><tr>']
    out += [f'<th style="text-align:left;padding:6px 8px;color:#64748B;border-bottom:1px solid #E2E8F0">{h}</th>' for h in head]
    out.append("</tr></thead><tbody>")
    for r in rows:
        style = "color:#94A3B8;text-decoration:line-through" if r["struck"] else "color:#111827"
        note = r["memo"] or ""
        if r["is_void_row"]:
            note = f'voids #{r["voids"]}' + (f" — {note}" if note else "")
        elif r["voided"]:
            note = f'voided by #{r["voided_by"]}' + (f" — {note}" if note else "")
        if r["split"]:
            note = r["split"] + (f" — {note}" if note else "")
        if r["contribution_source"]:
            note = f'{r["contribution_source"].title()} · ' + note
        cells = [r["date"], r["type_label"], r["ticker"] or "", _fmt_qty(r["quantity"]), _money(r["price"]), _money(r["amount"]) if r["amount"] else "",
                 r["lot_method"] or "", None, note]
        out.append(f'<tr style="border-bottom:1px solid #F1F5F9;{style}">')
        for c in cells:
            if c is None:
                out.append(f'<td style="padding:6px 8px;text-decoration:none">{_badge(r["clearance"]["label"], r["clearance"]["tone"])}</td>')
            else:
                out.append(f'<td style="padding:6px 8px">{_e(c)}</td>')
        out.append("</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def _flash(msg: str, kind: str = "ok") -> None:
    st.session_state["ledger_flash"] = (kind, msg)


def render_ledger_panel(ctx, accounts: list[dict], *, key: str = "ledger") -> None:
    """`accounts` = list_accounts_for(ctx) rows (account_id, display_name, broker ...)."""
    if flash := st.session_state.pop("ledger_flash", None):
        (st.success if flash[0] == "ok" else st.error)(flash[1], icon=material("check_circle" if flash[0] == "ok" else "error"))
    if not accounts:
        st.caption("No accounts yet.")
        return
    ids = [a["account_id"] for a in accounts]
    names = {a["account_id"]: (a.get("display_name") or a.get("account_number") or f"Account {a['account_id']}") for a in accounts}
    pick = st.selectbox("Account", ids, key=f"{key}_account", format_func=lambda i: names[i])
    try:
        info = ld.ledger_overview(ctx, pick)
    except _ERRORS as exc:
        st.error(str(exc), icon=material("error"))
        return

    badges = [_badge("Ledger" if info["is_ledger"] else "Snapshot", "good" if info["is_ledger"] else "neutral")]
    if info["is_paper"]:
        badges.append(_badge("Paper", "neutral"))
    if info["is_ledger"]:
        badges.append(_badge(f"Lots: {info['effective_lot_method']}" + ("" if info["lot_method"] else " (default)"), "neutral"))
        badges.append(_badge(f"Since {info['ledger_start_date']}", "neutral"))
    cl = info["clearance"]
    if cl["missing"]:
        badges.append(_badge(f"{cl['missing']} trade{'s' if cl['missing'] != 1 else ''} without approval", "bad"))
    if cl["unknown"]:
        badges.append(_badge(f"{cl['unknown']} unchecked", "warn"))
    st.markdown(" ".join(badges), unsafe_allow_html=True)
    if info["is_ledger"]:
        s = info["summary"]
        if s:
            st.caption(f"Cash {fmt_money(s['cash'])} · realized gain {fmt_money(s['realized_gain'], signed=True)} · "
                       f"{len(s['positions'])} open position{'s' if len(s['positions']) != 1 else ''} — all derived from the ledger.")
        else:
            st.error(f"The ledger does not replay: {info['summary_error']}", icon=material("error"))
    else:
        st.caption("This account's positions are a snapshot (typed in or imported). Transactions recorded here are history only until you switch it "
                   "to the ledger.")

    tabs = st.tabs(["Transactions", "Add", "Lots & gains", "Reconcile", "Import / export", "Ledger settings"])
    with tabs[0]:
        _tab_transactions(ctx, pick, key)
    with tabs[1]:
        _tab_add(ctx, pick, info, key)
    with tabs[2]:
        _tab_lots(ctx, pick, info, key)
    with tabs[3]:
        _tab_reconcile(ctx, pick, info, key)
    with tabs[4]:
        _tab_import_export(ctx, pick, info, key)
    with tabs[5]:
        _tab_settings(ctx, pick, info, key)


# ── tabs ──────────────────────────────────────────────────────────────────────

def _tab_transactions(ctx, aid, key) -> None:
    f1, f2, f3, f4 = st.columns([2.2, 1.4, 1.6, 1.6])
    types = f1.multiselect("Type", ld.FORM_TYPES + ["OPENING_BALANCE", "VOID"], key=f"{key}_ft_{aid}", format_func=lambda t: ld.TYPE_LABELS[t])
    ticker = f2.text_input("Ticker", key=f"{key}_fk_{aid}")
    cleared = f3.multiselect("Clearance", ["approved", "missing", "not_required", "unknown"], key=f"{key}_fc_{aid}",
                             format_func=lambda s: ld.CLEARANCE_LABELS[s][0])
    show_voided = f4.checkbox("Show voided", value=True, key=f"{key}_fv_{aid}")
    d1, d2 = st.columns(2)
    d_from = d1.date_input("From", value=None, key=f"{key}_df_{aid}")
    d_to = d2.date_input("To", value=None, key=f"{key}_dt_{aid}")
    try:
        rows = ld.transactions_view(ctx, aid, types=types or None, ticker=ticker, date_from=d_from.isoformat() if d_from else None,
                                    date_to=d_to.isoformat() if d_to else None, clearance=cleared or None, show_voided=show_voided)
    except _ERRORS as exc:
        st.error(str(exc))
        return
    if not rows:
        st.caption("No transactions match.")
    else:
        st.markdown(_transactions_html(rows[:500]), unsafe_allow_html=True)
        if len(rows) > 500:
            st.caption(f"Showing the newest 500 of {len(rows)}; narrow the filters or export the full list.")
    st.divider()
    choices = ld.voidable_choices(ctx, aid)
    if choices:
        st.markdown("**Void a transaction** — it stays in the record, struck through; nothing is deleted.")
        v1, v2, v3 = st.columns([3, 3, 1.2], vertical_alignment="bottom")
        which = v1.selectbox("Transaction", [c["txn_id"] for c in choices], key=f"{key}_void_pick_{aid}",
                             format_func=lambda i: next(c["label"] for c in choices if c["txn_id"] == i))
        why = v2.text_input("Reason (required)", key=f"{key}_void_why_{aid}")
        if v3.button("Void", key=f"{key}_void_go_{aid}", icon=material("block")):
            try:
                ld.void(ctx, which, why)
                _flash(f"Transaction #{which} voided.")
            except _ERRORS as exc:
                _flash(str(exc), "error")
            st.rerun()


_FIELDS = {  # which form fields each type shows
    "BUY": ("ticker", "quantity", "price", "fees"), "SELL": ("ticker", "quantity", "price", "fees", "lot_method"),
    "DEPOSIT": ("amount", "contribution_source"), "WITHDRAWAL": ("amount",), "DIVIDEND": ("ticker", "amount"), "INTEREST": ("amount",),
    "EMPLOYER_MATCH": ("amount", "ticker", "quantity", "price"), "FEE": ("amount", "ticker"), "SPLIT": ("ticker", "numerator", "denominator"),
}


def _tab_add(ctx, aid, info, key) -> None:
    if info["is_paper"]:
        st.caption("Paper account: trades are not checked for pre-clearance.")
    if not info["is_ledger"]:
        st.warning("This is a snapshot account, so a transaction recorded here is history only: it does not change the positions shown.",
                   icon=material("history"))
    kind = st.selectbox("Type", ld.FORM_TYPES, key=f"{key}_addtype_{aid}", format_func=lambda t: ld.TYPE_LABELS[t])
    show = _FIELDS[kind]
    with st.form(f"{key}_addform_{aid}_{kind}", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        v: dict = {"date": c1.date_input("Date", value=date.today(), max_value=date.today()).isoformat()}
        if "ticker" in show:
            optional = kind in ("FEE", "EMPLOYER_MATCH")
            v["ticker"] = c2.text_input("Ticker" + (" (optional)" if optional else "")).strip().upper()
        if "quantity" in show:
            v["quantity"] = c3.number_input("Shares" + (" (match in shares)" if kind == "EMPLOYER_MATCH" else ""), min_value=0.0, step=1.0, format="%.6f")
        if "price" in show:
            v["price"] = c1.number_input("Price per share", min_value=0.0, step=0.01, format="%.4f")
        if "amount" in show:
            v["amount"] = c2.number_input("Amount ($)" + (" (match in cash)" if kind == "EMPLOYER_MATCH" else ""), min_value=0.0, step=1.0, format="%.2f")
        if "fees" in show:
            v["fees"] = c3.number_input("Fees ($)", min_value=0.0, step=0.01, format="%.2f")
        if "numerator" in show:
            v["numerator"] = c1.number_input("New shares", min_value=0.0, value=2.0, step=1.0, help="2-for-1 = 2 for 1; a 1-for-10 reverse split = 1 for 10.")
            v["denominator"] = c2.number_input("For old shares", min_value=0.0, value=1.0, step=1.0)
        if "lot_method" in show:
            opts = ["(account default: " + info["effective_lot_method"] + ")", "FIFO", "LIFO", "HIFO"]
            pick = c2.selectbox("Lot method", opts)
            v["lot_method"] = None if pick.startswith("(") else pick
        if "contribution_source" in show:
            v["contribution_source"] = c3.selectbox("Money came from", ["", "EMPLOYEE", "EMPLOYER", "ROLLOVER", "TRANSFER", "OTHER"],
                                                    format_func=lambda s: s.title() if s else "—")
        v["memo"] = st.text_input("Note (optional)")
        go = st.form_submit_button("Record", type="primary", icon=material("add"))
    if go:
        try:
            res = ld.submit_transaction(ctx, aid, kind, v)
            extra = ""
            if res.get("txn", {}).get("clearance_status") == "missing":
                extra = " It has no pre-clearance approval on file; you have been notified."
            if res.get("realized"):
                gain = sum(r["realized_gain"] for r in res["realized"])
                extra += f" Realized gain {fmt_money(gain, signed=True)}."
            _flash(f"{ld.TYPE_LABELS[kind]} recorded.{extra}")
        except _ERRORS as exc:
            _flash(str(exc), "error")
        st.rerun()


def _tab_lots(ctx, aid, info, key) -> None:
    if not info["is_ledger"]:
        st.caption("Tax lots exist only for ledger accounts. Switch this account to the ledger (Ledger settings) to track them.")
        return
    lots = ld.lots_view(ctx, aid)
    st.markdown("**Open lots**")
    if lots:
        st.dataframe(pd.DataFrame([{"Ticker": l["ticker"], "Opened": l["open_date"], "Shares": l["quantity"], "Unit cost": l["unit_cost"],
                                    "Cost basis": l["cost_basis"], "From": l["origin"].replace("_", " ").title()} for l in lots]),
                     hide_index=True, width="stretch")
    else:
        st.caption("No open lots.")
    gains0 = ld.realized_gains_view(ctx, aid)
    year = st.selectbox("Realized gains — year", [None] + gains0["years"][::-1], key=f"{key}_gy_{aid}", format_func=lambda y: "All years" if y is None else str(y))
    g = ld.realized_gains_view(ctx, aid, year=year)
    st.markdown("**Realized gains**")
    if g["rows"]:
        st.dataframe(pd.DataFrame([{"Ticker": r["ticker"], "Shares": r["quantity"], "Opened": r["open_date"], "Closed": r["close_date"],
                                    "Proceeds": r["proceeds"], "Cost basis": r["cost_basis"], "Gain": r["gain"], "Term": "Long" if r["period"] == "LONG" else "Short",
                                    "Method": r["method"]} for r in g["rows"]]), hide_index=True, width="stretch")
        t = g["totals"]
        st.caption(f"Short-term {fmt_money(t['short_term'], signed=True)} · long-term {fmt_money(t['long_term'], signed=True)} · total {fmt_money(t['total'], signed=True)}. "
                   "Long-term = held more than a year. Wash sales are not adjusted.")
    else:
        st.caption("No sales recorded" + (f" in {year}." if year else " yet."))


def _tab_reconcile(ctx, aid, info, key) -> None:
    st.caption("Paste what your broker statement shows, one position per line (`VTI, 25.5`; add a cost basis as a third number; `CASH, 1200.50` for cash). "
               "The ledger is compared with it. Differences are only reported: nothing is changed and no trade is invented.")
    text = st.text_area("Statement", key=f"{key}_rec_text_{aid}", height=140, placeholder="VTI, 25.5\nBND, 10\nCASH, 1200.50")
    as_of = st.date_input("Statement date (optional)", value=None, max_value=date.today(), key=f"{key}_rec_asof_{aid}")
    if st.button("Compare", key=f"{key}_rec_go_{aid}", type="primary", icon=material("compare_arrows")):
        try:
            out = ld.reconciliation_view(ctx, aid, text, as_of=as_of.isoformat() if as_of else None)
        except _ERRORS as exc:
            st.error(str(exc), icon=material("error"))
            return
        if out["errors"]:
            st.error(" · ".join(out["errors"]), icon=material("error"))
            return
        r = out["result"]
        st.markdown(_badge("Everything matches" if r["matches"] else "Differences found", "good" if r["matches"] else "bad"), unsafe_allow_html=True)
        st.markdown('<table style="width:100%;font-size:0.82rem;border-collapse:collapse"><tr>' + "".join(
            f'<th style="text-align:left;padding:5px 8px;color:#64748B;border-bottom:1px solid #E2E8F0">{h}</th>' for h in ("Ticker", "Ledger", "Statement", "Difference", "")) + "</tr>" +
                    "".join(f'<tr><td style="padding:5px 8px">{_e(p["ticker"])}</td><td style="padding:5px 8px">{_fmt_qty(p["ledger_shares"])}</td>'
                            f'<td style="padding:5px 8px">{_fmt_qty(p["statement_shares"])}</td><td style="padding:5px 8px">{_fmt_qty(p["difference"])}</td>'
                            f'<td style="padding:5px 8px">{_badge(p["status"].replace("_", " ").title(), p["tone"])}</td></tr>' for p in r["positions"]) +
                    "</table>", unsafe_allow_html=True)
        if r["cash"]:
            c = r["cash"]
            st.caption(f"Cash: ledger {fmt_money(c['ledger'])} · statement {fmt_money(c['statement'])} · difference {fmt_money(c['difference'], signed=True)} "
                       f"({'match' if c['status'] == 'MATCH' else 'different'}).")


def _tab_import_export(ctx, aid, info, key) -> None:
    st.markdown("**Export**")
    try:
        files = ld.export_files(ctx, aid)
    except _ERRORS as exc:
        st.error(str(exc))
        return
    e1, e2 = st.columns(2)
    e1.download_button("Transactions (CSV)", files["transactions"][1], file_name=files["transactions"][0], mime="text/csv", icon=material("download"),
                       key=f"{key}_dl_t_{aid}", help="Every transaction, voided ones included and marked.")
    e2.download_button("Realized gains (CSV)", files["realized_gains"][1], file_name=files["realized_gains"][0], mime="text/csv",
                       icon=material("download"), key=f"{key}_dl_g_{aid}", help="Realized gains by lot (short / long), for taxes.")
    st.divider()
    st.markdown("**Import transactions from a CSV**")
    if not info["is_ledger"]:
        st.info("Transactions can only be imported into a ledger account. Switch this account to the ledger first (Ledger settings).", icon=material("info"))
        return
    st.caption("Any CSV: map its columns, review the preview, then import. Duplicates are skipped; nothing is written until you confirm. "
               "Broker-specific formats are not built yet.")
    up = st.file_uploader("CSV file", type=["csv", "txt"], key=f"{key}_up_{aid}")
    if up is None:
        return
    raw = up.getvalue()
    try:
        sug = ld.import_suggestion(raw)
    except _ERRORS as exc:
        st.error(str(exc), icon=material("error"))
        return
    st.caption(f"{sug['rows']} rows. Map the columns:")
    headers = [""] + sug["headers"]
    mapping: dict = {}
    cols = st.columns(3)
    labels = {"date": "Date *", "type": "Type *", "ticker": "Ticker", "quantity": "Quantity", "price": "Price", "amount": "Amount", "fees": "Fees",
              "external_ref": "Reference / ID", "contribution_source": "Money source"}
    for i, (f, lab) in enumerate(labels.items()):
        cur = sug["mapping"].get(f) or ""
        mapping[f] = cols[i % 3].selectbox(lab, headers, index=headers.index(cur) if cur in headers else 0, key=f"{key}_map_{aid}_{f}_{up.file_id}") or None
    try:
        pv = ld.import_preview(ctx, aid, raw, mapping)
    except _ERRORS as exc:
        st.error(str(exc), icon=material("error"))
        return
    s = pv["summary"]
    st.markdown(_badge(f"{s['new']} new", "good") + " " + _badge(f"{s['duplicate']} duplicate", "neutral") + " " +
                _badge(f"{s['invalid']} invalid", "bad" if s["invalid"] else "neutral"), unsafe_allow_html=True)
    if pv["dry_run_error"]:
        st.error(f"The rows do not fit this account's ledger: {pv['dry_run_error']}", icon=material("error"))
    st.dataframe(pd.DataFrame([{"Line": r["line"], "Status": r["status"], "Type": r["type"], "Date": r["trade_date"], "Ticker": r["ticker"],
                                "Qty": r["quantity"], "Cash": r["cash_amount"], "Notes": "; ".join(r["errors"] + r["warnings"]) or (r["duplicate_of"] or "")}
                               for r in pv["rows"][:300]]), hide_index=True, width="stretch")
    if st.button(f"Import {s['new']} new transaction{'s' if s['new'] != 1 else ''}", key=f"{key}_imp_go_{aid}", type="primary", disabled=not pv["can_import"],
                 icon=material("upload")):
        try:
            out = ld.import_commit(ctx, aid, raw, mapping, up.name)
            _flash(f"Imported {out['imported']} transactions ({out['skipped']} duplicates skipped)."
                   + (f" {out['missing_clearance']} trade(s) have no pre-clearance approval." if out["missing_clearance"] else ""))
        except _ERRORS as exc:
            _flash(str(exc), "error")
        st.rerun()


def _tab_settings(ctx, aid, info, key) -> None:
    if info["is_ledger"]:
        st.markdown("**Tax-lot method for future sales**")
        opts = ["(portfolio default: " + info["default_lot_method"] + ")", "FIFO", "LIFO", "HIFO"]
        cur = info["lot_method"] or opts[0]
        pick = st.selectbox("Method", opts, index=opts.index(cur) if cur in opts else 0, key=f"{key}_lm_{aid}",
                            help="Stored on each sale when it is recorded; changing it never changes past sales.")
        if st.button("Save method", key=f"{key}_lm_go_{aid}"):
            try:
                led.set_account_lot_method(ctx, aid, None if pick.startswith("(") else pick, reason="Accounts page")
                _flash("Lot method saved.")
            except _ERRORS as exc:
                _flash(str(exc), "error")
            st.rerun()
        st.divider()
        st.markdown("**Hand this account back to snapshots**")
        st.caption("Positions and cash stay as the ledger last derived them; tax lots are cleared; the transactions stay as history. "
                   "Switching to the ledger again later needs a fresh opening balance.")
        if st.checkbox("I understand", key=f"{key}_back_ok_{aid}") and st.button("Switch back to snapshot", key=f"{key}_back_go_{aid}"):
            try:
                led.switch_to_snapshot(ctx, aid, reason="Accounts page")
                _flash("This account is a snapshot account again.")
            except _ERRORS as exc:
                _flash(str(exc), "error")
            st.rerun()
        return
    st.markdown("**Switch this account to the ledger**")
    st.caption("What the account holds today becomes its **opening balance** on the date you pick: shares at their cost, and cash. These are not trades, "
               "and nothing is inferred about how the account got here. From then on its positions and cash are derived from transactions and "
               "can no longer be typed in or imported over (a broker file is compared instead).")
    opening = st.date_input("Opening balance date", value=date.today() - timedelta(days=1), max_value=date.today(), key=f"{key}_open_{aid}")
    try:
        pv = ld.switch_preview(ctx, aid, opening.isoformat())
    except _ERRORS as exc:
        st.error(str(exc))
        return
    overrides: dict = {}
    if pv["needs_basis"]:
        st.warning("These positions have no cost basis. Enter a unit cost for each, or allow them to open at 0 (every later gain on them would be overstated).",
                   icon=material("warning"))
        for t in pv["needs_basis"]:
            val = st.number_input(f"{t} — cost per share ($)", min_value=0.0, value=0.0, step=0.01, key=f"{key}_ov_{aid}_{t}")
            if val > 0:
                overrides[t] = val
        allow = st.checkbox("Open the remaining ones at a cost of 0 and flag them", key=f"{key}_allow_{aid}")
        pv = ld.switch_preview(ctx, aid, opening.isoformat(), basis_overrides=overrides, allow_unknown_basis=allow)
    if pv["positions"] or pv["cash"] is not None:
        st.dataframe(pd.DataFrame([{"Ticker": p["ticker"], "Shares": p["shares"], "Cost per share": p["unit_cost"], "Cost basis": p["cost_basis"],
                                    "Note": "cost unknown — opened at 0" if p["unknown_basis"] else ""} for p in pv["positions"]]
                                  + ([{"Ticker": "CASH", "Shares": None, "Cost per share": None, "Cost basis": pv["cash"], "Note": "opening cash"}]
                                     if pv["cash"] is not None else [])), hide_index=True, width="stretch")
    else:
        st.caption("This account holds nothing yet; the ledger would start empty.")
    for p in pv["problems"]:
        if "no cost basis" not in p:
            st.error(p, icon=material("error"))
    ok = st.checkbox("These opening balances are right; switch this account to the ledger", key=f"{key}_confirm_{aid}")
    if st.button("Switch to ledger", key=f"{key}_switch_go_{aid}", type="primary", disabled=not (ok and pv["can_switch"]), icon=material("swap_horiz")):
        try:
            out = led.switch_to_ledger(ctx, aid, opening.isoformat(), basis_overrides=overrides, allow_unknown_basis=bool(pv["unknown_basis"]),
                                       reason="Accounts page")
            _flash(f"Switched to the ledger with {out['opening_transactions']} opening balance entries.")
        except _ERRORS as exc:
            _flash(str(exc), "error")
        st.rerun()
