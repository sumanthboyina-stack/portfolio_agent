"""
web/data/ledger.py — page-data functions for the Accounts page's ledger panel. Everything the panel shows is built here (plain dicts and lists, no
Streamlit), so it can be tested directly, in the style of test_validation_page_data.py.

Owner scoping: every read goes through services.ledger_service / account_service with the caller's RequestContext, which checks that the account
belongs to the caller's own portfolio (NotAuthorized / NotFound otherwise) — there is no function here that takes an owner or reads a table directly.

Out of scope (TODO hooks only, nothing built): time-weighted / money-weighted performance (TWR / IRR), a paper-trading UI, wash-sale adjustment,
choosing specific lots in the form (the service supports SPECIFIC; the form does not offer it), and broker sync.
"""
from __future__ import annotations

import re
from datetime import date

from portfolio_agent.services import ledger_service as led
from portfolio_agent.services.context import RequestContext

TYPE_LABELS = {"BUY": "Buy", "SELL": "Sell", "DEPOSIT": "Deposit", "WITHDRAWAL": "Withdrawal", "DIVIDEND": "Dividend", "INTEREST": "Interest",
               "EMPLOYER_MATCH": "Employer match", "FEE": "Fee", "SPLIT": "Split", "VOID": "Void", "OPENING_BALANCE": "Opening balance"}
FORM_TYPES = ["BUY", "SELL", "DEPOSIT", "WITHDRAWAL", "DIVIDEND", "INTEREST", "EMPLOYER_MATCH", "FEE", "SPLIT"]
CLEARANCE_LABELS = {"approved": ("Cleared", "good"), "missing": ("No approval", "bad"), "not_required": ("Not required", "neutral"),
                    "unknown": ("Unchecked", "warn"), None: ("", "none")}


def clearance_badge(status: str | None) -> dict:
    label, tone = CLEARANCE_LABELS.get(status, (str(status), "neutral"))
    return {"status": status, "label": label, "tone": tone}


# ── reads (owner-checked) ─────────────────────────────────────────────────────

def ledger_overview(ctx: RequestContext, account_id: int) -> dict:
    """The account's header facts plus, for a ledger account, its replayed cash / positions / realized gain."""
    info = led.get_account_ledger_info(ctx, account_id)
    summary = led.get_ledger_summary(ctx, account_id)
    info["summary"] = None if "error" in summary else {k: summary[k] for k in ("cash", "realized_gain", "positions", "totals")}
    info["summary_error"] = summary.get("error")
    return info


def transactions_view(ctx: RequestContext, account_id: int, *, types: list[str] | None = None, ticker: str | None = None,
                      date_from: str | None = None, date_to: str | None = None, clearance: list[str] | None = None,
                      show_voided: bool = True) -> list[dict]:
    """Display rows, newest first. A voided transaction (and the VOID row that cancels it) is kept in the list, flagged `struck` so the page can
    draw it struck through, unless show_voided is False. Filters: transaction types, ticker (substring), date range, clearance statuses."""
    rows = led.list_transactions(ctx, account_id, include_voided=True)
    out = []
    t = (ticker or "").strip().upper()
    for r in rows:
        voided = r["voided_by"] is not None
        is_void_row = r["txn_type"] == "VOID"
        if not show_voided and (voided or is_void_row):
            continue
        if types and r["txn_type"] not in types:
            continue
        if t and t not in (r["ticker"] or ""):
            continue
        if date_from and r["trade_date"] < date_from:
            continue
        if date_to and r["trade_date"] > date_to:
            continue
        if clearance and (r["clearance_status"] or "") not in clearance:
            continue
        out.append({"txn_id": r["txn_id"], "date": r["trade_date"], "type": r["txn_type"], "type_label": TYPE_LABELS.get(r["txn_type"], r["txn_type"]),
                    "ticker": r["ticker"], "quantity": r["quantity"], "price": r["price"], "fees": r["fees"] or None, "amount": r["amount"],
                    "lot_method": r["lot_method"], "contribution_source": r["contribution_source"], "memo": r["memo"],
                    "clearance": clearance_badge(r["clearance_status"]), "voided": voided, "is_void_row": is_void_row,
                    "voided_by": r["voided_by"], "voids": r["voids_txn_id"], "struck": voided or is_void_row,
                    "split": (f'{r["split_numerator"]:g}-for-{r["split_denominator"]:g}' if r["txn_type"] == "SPLIT" else None),
                    "can_void": not voided and not is_void_row})
    out.sort(key=lambda x: (x["date"], x["txn_id"]), reverse=True)
    return out


def voidable_choices(ctx: RequestContext, account_id: int) -> list[dict]:
    """Transactions that can still be voided (not voided, not a VOID row), newest first, with a one-line label for a picker."""
    rows = [r for r in transactions_view(ctx, account_id, show_voided=False)]
    return [{"txn_id": r["txn_id"], "label": f'#{r["txn_id"]} · {r["date"]} · {r["type_label"]}' + (f' {r["ticker"]}' if r["ticker"] else "") +
                                             (f' · {r["amount"]:,.2f}' if r["amount"] else "")} for r in rows]


def lots_view(ctx: RequestContext, account_id: int) -> list[dict]:
    return [{"ticker": l["ticker"], "open_date": l["open_date"], "quantity": l["remaining_quantity"], "unit_cost": l["unit_cost"],
             "cost_basis": l["basis_remaining"], "origin": l["source_type"], "lot_id": l["open_transaction_id"]}
            for l in led.get_lots(ctx, account_id, open_only=True)]


def realized_gains_view(ctx: RequestContext, account_id: int, *, year: int | None = None) -> dict:
    """Realized gains by lot closure with SHORT / LONG totals; `years` lists the closing years present so the page can offer a filter."""
    closures = led.get_closures(ctx, account_id)
    years = sorted({int(c["close_date"][:4]) for c in closures})
    rows = [c for c in closures if year is None or c["close_date"].startswith(str(year))]
    tot = {"SHORT": 0.0, "LONG": 0.0}
    for c in rows:
        tot[c["holding_period"]] += c["realized_gain"]
    return {"years": years, "year": year,
            "rows": [{"ticker": c["ticker"], "quantity": c["quantity"], "open_date": c["open_date"], "close_date": c["close_date"],
                      "proceeds": c["proceeds"], "cost_basis": c["cost_basis"], "gain": c["realized_gain"], "period": c["holding_period"],
                      "method": c["method"]} for c in rows],
            "totals": {"short_term": round(tot["SHORT"], 2), "long_term": round(tot["LONG"], 2), "total": round(tot["SHORT"] + tot["LONG"], 2)}}


def clearance_summary(ctx: RequestContext, account_id: int) -> dict:
    c = led.get_account_ledger_info(ctx, account_id)["clearance"]
    return {**c, "needs_attention": c["missing"] > 0 or c["unknown"] > 0}


# ── switching ─────────────────────────────────────────────────────────────────

def switch_preview(ctx: RequestContext, account_id: int, opening_date: str, *, basis_overrides: dict | None = None,
                   allow_unknown_basis: bool = False) -> dict:
    """The opening balances a switch would record (shares, unit cost, basis per position; cash), nothing written."""
    return led.preview_switch_to_ledger(ctx, account_id, opening_date, basis_overrides=basis_overrides, allow_unknown_basis=allow_unknown_basis)


# ── reconciliation ────────────────────────────────────────────────────────────

def parse_statement_text(text: str) -> dict:
    """
    Statement positions typed or pasted as lines: `TICKER, shares` or `TICKER, shares, cost basis`; a line `CASH, amount` gives the statement's
    cash. The separator is the first of tab, semicolon, comma that the line contains, else whitespace; $ is accepted in numbers, and thousands
    commas only when the separator is a tab, semicolon or whitespace (with commas as separators, write 1200.50, not 1,200.50).
    Returns {"positions": [{ticker, shares[, cost_basis_total]}], "cash": float | None, "errors": [str]}.
    """
    positions, cash, errors = [], None, []
    for n, line in enumerate((text or "").splitlines(), start=1):
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        sep = "\t" if "\t" in raw else ";" if ";" in raw else "," if "," in raw else None
        parts = [p.strip() for p in raw.split(sep) if p.strip()] if sep else raw.split()
        if len(parts) < 2:
            errors.append(f"line {n}: expected 'TICKER, shares'")
            continue
        try:
            nums = [float(p.replace("$", "").replace(",", "")) for p in parts[1:]]
        except ValueError:
            errors.append(f"line {n}: {parts[1:]} is not a number")
            continue
        if parts[0].upper() == "CASH":
            cash = nums[0]
        else:
            row = {"ticker": parts[0].upper(), "shares": nums[0]}
            if len(nums) > 1:
                row["cost_basis_total"] = nums[1]
            positions.append(row)
    return {"positions": positions, "cash": cash, "errors": errors}


def reconciliation_view(ctx: RequestContext, account_id: int, text: str, *, as_of: str | None = None) -> dict:
    """Compare the ledger with a pasted statement (read-only; nothing is ever written to make them agree)."""
    parsed = parse_statement_text(text)
    if parsed["errors"]:
        return {"errors": parsed["errors"], "result": None}
    result = led.reconcile_account(ctx, account_id, parsed["positions"], parsed["cash"], as_of=as_of or None)
    tone = {"MATCH": "good", "DIFFERENT": "bad", "ONLY_IN_LEDGER": "warn", "ONLY_IN_STATEMENT": "warn"}
    result["positions"] = [{**p, "tone": tone[p["status"]]} for p in result["positions"]]
    return {"errors": [], "result": result}


# ── adding a transaction from the form ────────────────────────────────────────

class FormError(ValueError):
    """A form value is missing or wrong (the message is shown to the person)."""


def _req(v, what):
    if v in (None, "", 0, 0.0):
        raise FormError(f"{what} is required")
    return v


def submit_transaction(ctx: RequestContext, account_id: int, kind: str, v: dict) -> dict:
    """
    Turn the add-transaction form's values into the right ledger verb and call it. `kind` is one of FORM_TYPES; `v` holds the form fields
    (date, ticker, quantity, price, amount, fees, lot_method, contribution_source, memo, numerator, denominator). Raises FormError for a missing
    value, or the service's InvalidTransaction (shown as is) for anything the ledger refuses. Returns the verb's result.
    """
    day = v.get("date") or date.today().isoformat()
    memo = (v.get("memo") or "").strip() or None
    if kind in ("BUY", "SELL"):
        return led.record_trade(ctx, account_id, _req(v.get("ticker"), "ticker"), kind, _req(v.get("quantity"), "quantity"),
                                v.get("price") if kind == "SELL" and v.get("price") == 0 else _req(v.get("price"), "price"), day,
                                fees=v.get("fees") or 0.0, lot_method=(v.get("lot_method") or None), memo=memo)
    if kind in ("DEPOSIT", "WITHDRAWAL"):
        return led.record_cash_transfer(ctx, account_id, kind, _req(v.get("amount"), "amount"), day,
                                        contribution_source=(v.get("contribution_source") or None) if kind == "DEPOSIT" else None, memo=memo)
    if kind in ("DIVIDEND", "INTEREST"):
        if kind == "DIVIDEND":
            _req(v.get("ticker"), "ticker")
        return led.record_income(ctx, account_id, kind, _req(v.get("amount"), "amount"), day, ticker=(v.get("ticker") or None), memo=memo)
    if kind == "EMPLOYER_MATCH":
        if v.get("ticker") or v.get("quantity"):
            return led.record_employer_match(ctx, account_id, day, ticker=_req(v.get("ticker"), "ticker"), quantity=_req(v.get("quantity"), "quantity"),
                                             price=_req(v.get("price"), "price"), contribution_source=(v.get("contribution_source") or "EMPLOYER"), memo=memo)
        return led.record_employer_match(ctx, account_id, day, amount=_req(v.get("amount"), "amount"),
                                         contribution_source=(v.get("contribution_source") or "EMPLOYER"), memo=memo)
    if kind == "FEE":
        return led.record_fee(ctx, account_id, _req(v.get("amount"), "amount"), day, ticker=(v.get("ticker") or None), memo=memo)
    if kind == "SPLIT":
        out = led.record_split(ctx, _req(v.get("ticker"), "ticker"), _req(v.get("numerator"), "numerator"), _req(v.get("denominator"), "denominator"),
                               day, account_id=account_id, memo=memo)
        return out[0]
    raise FormError(f"unknown transaction type {kind!r}")


def void(ctx: RequestContext, txn_id: int, reason: str) -> dict:
    return led.void_transaction(ctx, txn_id, reason=reason)


# ── CSV import (the three steps) and exports ──────────────────────────────────

def import_suggestion(raw: bytes) -> dict:
    return led.suggest_csv_mapping(raw)


def import_preview(ctx: RequestContext, account_id: int, raw: bytes, mapping: dict) -> dict:
    return led.preview_transactions_csv(ctx, account_id, raw, mapping)


def import_commit(ctx: RequestContext, account_id: int, raw: bytes, mapping: dict, filename: str | None) -> dict:
    return led.import_transactions_csv(ctx, account_id, raw, mapping, filename=filename)


def export_files(ctx: RequestContext, account_id: int, *, year: int | None = None) -> dict:
    """{"transactions": (filename, csv text), "realized_gains": (filename, csv text)} for download buttons."""
    info = led.get_account_ledger_info(ctx, account_id)
    slug = re.sub(r"[^a-z0-9]+", "-", info["name"].lower()).strip("-") or f"account-{account_id}"
    stamp = date.today().isoformat()
    return {"transactions": (f"{slug}-transactions-{stamp}.csv", led.export_transactions_csv(ctx, account_id)),
            "realized_gains": (f"{slug}-realized-gains{'-' + str(year) if year else ''}-{stamp}.csv",
                               led.export_realized_gains_csv(ctx, account_id, year=year))}
