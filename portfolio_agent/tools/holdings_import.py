"""
Holdings import service — parsing, account mapping, reconciliation and
account-scoped commit of broker file uploads, independent of any screen.

Flow (each step is a plain function so any UI can drive it):

    parsed = parse_upload(content, filename, fallback_broker)
    parsed.unnamed_groups        -> accounts that still need a nickname
    plan   = build_plan(parsed, labels)      # diff vs. current holdings
    result = commit_plan(plan)               # replace per account, record run

Counts in ImportResult come from the diff taken against the database at
commit time, not from the number of parsed rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from portfolio_agent.services import account_service
from portfolio_agent.services.context import RequestContext, local_context
from portfolio_agent.tools import holdings_db as repo
from portfolio_agent.tools.market_data import fetch_last_closes
from portfolio_agent.tools.holdings_parser import parse_csv, split_cash_rows
from portfolio_agent.tools.llm_holdings_parser import is_excel, parse_holdings_with_llm

BROKER_LABELS = {"fidelity": "Fidelity", "vanguard": "Vanguard", "other": "Other broker", "manual": "Manual"}


# ── Step 1: parse ─────────────────────────────────────────────────────────────

@dataclass
class ParsedUpload:
    broker: str
    securities: list[dict]
    cash_rows: list[dict]
    filename: str = ""

    @property
    def unnamed_groups(self) -> list[str]:
        return missing_account_labels(self.securities + self.cash_rows)

    @property
    def statement_date(self) -> str | None:
        return next((r.get("as_of_date") for r in self.securities + self.cash_rows if r.get("as_of_date")), None)


def missing_account_labels(rows: list[dict]) -> list[str]:
    """
    Distinct account_name values among rows with no account_number — each needs
    a user-supplied nickname before saving so it becomes its own account bucket.
    "" itself is a valid distinct group (no name either).
    """
    seen: list[str] = []
    for h in rows:
        if not (h.get("account_number") or "").strip():
            name = (h.get("account_name") or "").strip()
            if name not in seen:
                seen.append(name)
    return seen


def parse_upload(content: bytes, filename: str, fallback_broker: str = "other") -> ParsedUpload:
    """
    Detect the format and parse. Spreadsheets and unrecognised CSV layouts go
    through the LLM extractor and are filed under *fallback_broker*.
    Raises ValueError when nothing usable was found.
    """
    if is_excel(content, filename):
        broker, rows = fallback_broker, parse_holdings_with_llm(content, filename)
    else:
        try:
            broker, rows = parse_csv(content)
        except ValueError:
            broker, rows = fallback_broker, parse_holdings_with_llm(content, filename)
    securities, cash_rows = split_cash_rows(rows or [])
    if not securities and not cash_rows:
        raise ValueError("No valid holdings found in this file. Check that it is a holdings/positions export (CSV or Excel).")
    return ParsedUpload(broker=broker, securities=securities, cash_rows=cash_rows, filename=filename)


# ── Step 2: map accounts + reconcile ─────────────────────────────────────────

def apply_account_labels(rows: list[dict], labels: dict[str, str]) -> list[dict]:
    """Rows without an account number get the nickname chosen for their group as account_number."""
    out = []
    for h in rows:
        h = dict(h)
        if not (h.get("account_number") or "").strip():
            group = (h.get("account_name") or "").strip()
            label = (labels.get(group) or group).strip()
            h["account_number"] = label
            if not h.get("account_name"):
                h["account_name"] = label
        out.append(h)
    return out


@dataclass
class AccountPlan:
    broker: str
    account_number: str
    account_name: str
    rows: list[dict]
    cash_amount: float | None
    cash_lines: list[str]
    added: list[str] = field(default_factory=list)
    updated: list[dict] = field(default_factory=list)     # {ticker, old_shares, new_shares, old_cost, new_cost}
    removed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    existing_count: int = 0
    account_id: int | None = None                         # None until the account exists (created on commit)

    @property
    def label(self) -> str:
        return self.account_name or self.account_number or "Unnamed account"


@dataclass
class ImportPlan:
    broker: str
    statement_date: str | None
    filename: str
    accounts: list[AccountPlan]

    @property
    def totals(self) -> dict:
        return {
            "added": sum(len(a.added) for a in self.accounts),
            "updated": sum(len(a.updated) for a in self.accounts),
            "removed": sum(len(a.removed) for a in self.accounts),
            "unchanged": sum(len(a.unchanged) for a in self.accounts),
            "positions": sum(len(a.rows) for a in self.accounts),
            "cash_accounts": sum(1 for a in self.accounts if a.cash_amount is not None),
        }


def _diff(existing: list, new_rows: list[dict]) -> tuple[list[str], list[dict], list[str], list[str]]:
    old = {h["ticker"].upper(): h for h in existing}
    new = {(r.get("ticker") or "").upper(): r for r in new_rows}
    added = sorted(t for t in new if t not in old)
    removed = sorted(t for t in old if t not in new)
    updated, unchanged = [], []
    for t in sorted(t for t in new if t in old):
        o, n = old[t], new[t]
        o_sh, n_sh = round(o.get("shares") or 0, 6), round(n.get("shares") or 0, 6)
        o_cb, n_cb = round(o.get("cost_basis_total") or 0, 2), round(n.get("cost_basis_total") or 0, 2)
        if o_sh != n_sh or o_cb != n_cb:
            updated.append({"ticker": t, "old_shares": o.get("shares"), "new_shares": n.get("shares"),
                            "old_cost": o.get("cost_basis_total"), "new_cost": n.get("cost_basis_total")})
        else:
            unchanged.append(t)
    return added, updated, removed, unchanged


def _reconcile(plan_acct: AccountPlan, ctx: RequestContext, conn=None) -> None:
    acct = account_service.find_account(ctx, plan_acct.broker, plan_acct.account_number, conn=conn)
    plan_acct.account_id = acct["account_id"] if acct else None
    existing = repo.list_positions(acct["account_id"], conn=conn) if acct else []
    plan_acct.existing_count = len(existing)
    plan_acct.added, plan_acct.updated, plan_acct.removed, plan_acct.unchanged = _diff(existing, plan_acct.rows)


def build_plan(parsed: ParsedUpload, labels: dict[str, str] | None = None, *,
               context: RequestContext | None = None) -> ImportPlan:
    """Group the upload per account and diff each against what is stored now for this actor."""
    ctx = context or local_context("import")
    labels = labels or {}
    securities = apply_account_labels(parsed.securities, labels)
    cash_rows = apply_account_labels(parsed.cash_rows, labels)
    remaining = missing_account_labels(securities + cash_rows)
    if remaining:
        raise ValueError(f"Account(s) still need a nickname: {remaining}")

    by_acct: dict[str, AccountPlan] = {}
    for r in securities:
        acct = (r.get("account_number") or "").strip()
        p = by_acct.setdefault(acct, AccountPlan(parsed.broker, acct, r.get("account_name") or "", [], None, []))
        p.rows.append(r)
        if not p.account_name and r.get("account_name"):
            p.account_name = r["account_name"]
    for c in cash_rows:
        acct = (c.get("account_number") or "").strip()
        p = by_acct.setdefault(acct, AccountPlan(parsed.broker, acct, c.get("account_name") or "", [], None, []))
        p.cash_amount = (p.cash_amount or 0.0) + (c.get("current_value") or 0.0)
        p.cash_lines.append(c.get("ticker") or "CASH")
    for p in by_acct.values():
        _reconcile(p, ctx)
    return ImportPlan(parsed.broker, parsed.statement_date, parsed.filename,
                      sorted(by_acct.values(), key=lambda a: a.label))


# ── Step 3: commit ────────────────────────────────────────────────────────────

@dataclass
class ImportResult:
    run_id: int | None
    broker: str
    accounts: list[str]
    added: int
    updated: int
    removed: int
    positions: int
    cash_accounts: int
    statement_date: str | None
    price_as_of: str | None
    unpriced: list[str]

    @property
    def message(self) -> str:
        acc = ", ".join(self.accounts)
        msg = (f"Import completed: {self.added} position{'s' if self.added != 1 else ''} added, "
               f"{self.updated} updated, {self.removed} removed ({BROKER_LABELS.get(self.broker, self.broker.title())} · {acc}).")
        if self.unpriced:
            msg += f" No price available for {', '.join(self.unpriced)}."
        return msg


def enrich_prices(rows: list[dict]) -> tuple[list[dict], str | None]:
    """Fill current_price/current_value/price_as_of where the file gave no price. Unknown stays None."""
    need = sorted({r["ticker"] for r in rows if r.get("current_price") is None})
    if not need:
        return rows, None
    prices, as_of = fetch_last_closes(need)
    for r in rows:
        if r.get("current_price") is None and r["ticker"] in prices:
            r["current_price"] = round(prices[r["ticker"]], 4)
            r["current_value"] = round(prices[r["ticker"]] * r["shares"], 2) if r.get("shares") else None
            r["price_as_of"] = as_of
    return rows, as_of


def commit_plan(plan: ImportPlan, fetch_prices: bool = True, *,
                context: RequestContext | None = None) -> ImportResult:
    """
    Apply a validated plan: for each account the user selected, make its
    positions equal the file's (in place — position ids survive), record its
    cash (or forget it when the file has no cash line), and log the run. All
    of it is ONE transaction with one audit event per account, so a failure
    part-way leaves every account and the import history exactly as they were.

    Prices are fetched before the transaction opens so the write lock is
    never held while waiting on the market-data provider. Counts come from
    what actually changed at commit time, not from the preview.
    """
    ctx = context or local_context("import")
    today = date.today().isoformat()
    statement = plan.statement_date or today
    price_as_of = None
    unpriced: set[str] = set()

    # Provider calls first, outside the transaction.
    priced_rows: dict[str, list[dict]] = {}
    for acct in plan.accounts:
        rows = [dict(r) for r in acct.rows]
        if fetch_prices and rows:
            rows, as_of = enrich_prices(rows)
            price_as_of = price_as_of or as_of
        unpriced.update(r["ticker"] for r in rows if r.get("current_price") is None)
        priced_rows[acct.account_number] = rows

    totals = {"added": 0, "updated": 0, "removed": 0, "positions": 0}
    cash_accounts = 0
    with repo.transaction() as conn:
        run_id = repo.record_import_run(broker=plan.broker, accounts=[a.label for a in plan.accounts],
                                        filename=plan.filename, statement_date=statement, conn=conn)
        for acct in plan.accounts:
            account = account_service.find_or_create_import_account(
                ctx, broker=plan.broker, account_number=acct.account_number, display_name=acct.account_name,
                account_type=next((r.get("account_type") for r in acct.rows if r.get("account_type")), None),
                conn=conn)
            acct.account_id = account["account_id"]
            res = account_service.replace_account_snapshot(
                ctx, account["account_id"], priced_rows[acct.account_number],
                as_of_date=today, source_import_id=run_id, conn=conn)
            for k in totals:
                totals[k] += res[k]
            account_service.record_account_cash(
                ctx, account["account_id"], acct.cash_amount, as_of_date=statement,
                detail=", ".join(acct.cash_lines) or None, reason="import", conn=conn)
            cash_accounts += acct.cash_amount is not None
        repo.update_import_run(run_id, conn=conn, cash_accounts=cash_accounts, **totals)
    return ImportResult(run_id, plan.broker, [a.label for a in plan.accounts], totals["added"], totals["updated"],
                        totals["removed"], totals["positions"], cash_accounts, statement, price_as_of, sorted(unpriced))
