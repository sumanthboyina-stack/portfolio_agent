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

from portfolio_agent.domain import is_synthetic_ticker
from portfolio_agent.services import account_service
from portfolio_agent.services.context import RequestContext, local_context
from portfolio_agent.tools import holdings_db as repo
from portfolio_agent.tools.market_data import fetch_last_closes
from portfolio_agent.tools.holdings_parser import parse_csv, split_cash_rows
from portfolio_agent.tools.llm_holdings_parser import is_excel, is_pdf, parse_holdings_with_llm

BROKER_LABELS = {"fidelity": "Fidelity", "vanguard": "Vanguard", "other": "Other broker", "manual": "Manual"}


# ── Step 1: parse ─────────────────────────────────────────────────────────────

@dataclass
class ParsedUpload:
    broker: str
    securities: list[dict]
    cash_rows: list[dict]
    filename: str = ""
    # Set only for a retirement-plan statement (PDF): the full StatementParse (metadata, per-source lines, ending-balance cross-check), the funds
    # that have no ticker (kept apart from `securities` until they can be matched to a fund with no ticker), and warnings for the preview.
    statement: object | None = None
    ticker_less: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def unnamed_groups(self) -> list[str]:
        return missing_account_labels(self.securities + self.cash_rows + self.ticker_less)

    @property
    def statement_date(self) -> str | None:
        return next((r.get("as_of_date") for r in self.securities + self.cash_rows + self.ticker_less if r.get("as_of_date")), None)


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
    A PDF is a retirement-plan statement (401(k) / 403(b) / 457(b), any recordkeeper): its text layer is read locally (a scanned PDF is
    refused with a clear message), redacted, and read by the statement extractor, which KEEPS funds that have no ticker. Those come back in
    `ticker_less`, apart from `securities`.
    Raises ValueError when nothing usable was found.
    """
    if is_pdf(content, filename):
        from portfolio_agent.tools.llm_holdings_parser import parse_statement_with_llm
        sp = parse_statement_with_llm(content)                      # PdfStatementError / NoTextLayerError are ValueErrors with a plain message
        return ParsedUpload(broker=fallback_broker, securities=sp.with_ticker, cash_rows=[], filename=filename, statement=sp,
                            ticker_less=sp.ticker_less, warnings=list(sp.warnings))
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
class ImportOptions:
    """What the person chose in the preview, beyond the file itself. Every field has a safe default, so `ImportOptions()` is "do the sensible thing"."""
    remove_missing: bool | None = None      # drop positions the file does not list? None = the default for the kind of file: True for a broker file
                                            # (it lists everything), False for a quarterly plan statement (it need not)
    funds: dict[str, dict] = field(default_factory=dict)       # fund_key -> {"proxy": "VOO", "ticker": "VTSAX" (a CONFIRMED suggestion), "map_to": "CIT:..." | "__new__"}
    account_map: dict[str, int] = field(default_factory=dict)  # the plan's account_number -> an existing account_id to import into


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
    # An account managed by its transaction ledger is never overwritten by a file: the file is COMPARED with the ledger instead
    # (services.ledger_service.reconcile_account) and nothing is changed. `reconciliation` is that comparison (None if it could not be made).
    ledger_managed: bool = False
    reconciliation: dict | None = None
    # Statement imports
    remove_missing: bool = True                           # positions the file does not list are removed (broker files); a statement keeps them
    kept_missing: list[str] = field(default_factory=list) # positions in the account that the file does not list and that are being KEPT
    is_statement: bool = False
    account_type: str | None = None
    matched_existing: bool = False                        # the plan's account was matched to an account you already have
    plan_name: str = ""

    @property
    def label(self) -> str:
        return self.account_name or self.account_number or "Unnamed account"

    @property
    def funds_no_ticker(self) -> list[dict]:
        """The statement's funds that have no ticker, with how each will be handled (matched / new fund with no ticker)."""
        return [r for r in self.rows if r.get("fund_key")]


@dataclass
class ImportPlan:
    broker: str
    statement_date: str | None
    filename: str
    accounts: list[AccountPlan]
    options: ImportOptions = field(default_factory=ImportOptions)
    is_statement: bool = False
    statement_meta: dict = field(default_factory=dict)    # plan name, provider, period (statements)
    warnings: list[str] = field(default_factory=list)     # extraction warnings incl. the ending-balance cross-check (shown, never blocking)
    check: dict = field(default_factory=dict)             # the ending-balance cross-check
    loan_balance: float | None = None                     # plan loan stated on the statement: recorded on the account, never a holding
    vested_balance: float | None = None                   # plan-level vested total stated on the statement

    @property
    def totals(self) -> dict:
        return {
            "added": sum(len(a.added) for a in self.accounts),
            "updated": sum(len(a.updated) for a in self.accounts),
            "removed": sum(len(a.removed) for a in self.accounts),
            "unchanged": sum(len(a.unchanged) for a in self.accounts),
            "positions": sum(len(a.rows) for a in self.accounts if not a.ledger_managed),
            "cash_accounts": sum(1 for a in self.accounts if a.cash_amount is not None and not a.ledger_managed),
        }

    @property
    def compared(self) -> int:
        """Accounts managed by their ledger: the file is compared with them, not applied."""
        return sum(1 for a in self.accounts if a.ledger_managed)


def _diff(existing: list, new_rows: list[dict], *, remove_missing: bool = True,
          compare_values: bool = False) -> tuple[list[str], list[dict], list[str], list[str]]:
    """
    (added, updated, removed, unchanged) by ticker. A fund with no ticker is keyed by its synthetic "CIT:..." key, so a statement row for one
    updates the position you already hold instead of adding it twice and removing the old one. remove_missing=False: nothing is reported as removed
    (the caller lists what is being kept). compare_values=True (statements): a changed price or balance counts as an update, not only shares / cost.
    """
    old = {h["ticker"].upper(): h for h in existing}
    new = {(r.get("ticker") or "").upper(): r for r in new_rows}
    added = sorted(t for t in new if t not in old)
    removed = sorted(t for t in old if t not in new) if remove_missing else []
    updated, unchanged = [], []
    for t in sorted(t for t in new if t in old):
        o, n = old[t], new[t]
        o_sh, n_sh = round(o.get("shares") or 0, 6), round(n.get("shares") or 0, 6)
        o_cb, n_cb = round(o.get("cost_basis_total") or 0, 2), round(n.get("cost_basis_total") or 0, 2)
        changed = o_sh != n_sh or o_cb != n_cb
        if compare_values and not changed:
            changed = ((n.get("current_value") is not None and round(o.get("current_value") or 0, 2) != round(n["current_value"], 2))
                       or (n.get("current_price") is not None and round(o.get("current_price") or 0, 4) != round(n["current_price"], 4)))
        if changed:
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
    if acct and acct.get("holdings_source") == "ledger":
        plan_acct.ledger_managed = True                   # no diff to apply: positions come from the ledger; compare instead
        plan_acct.reconciliation = _compare_with_ledger(ctx, acct["account_id"], plan_acct, None, audit=False)
        return
    plan_acct.added, plan_acct.updated, plan_acct.removed, plan_acct.unchanged = _diff(
        existing, plan_acct.rows, remove_missing=plan_acct.remove_missing, compare_values=plan_acct.is_statement)
    if not plan_acct.remove_missing:
        listed = {(r.get("ticker") or "").upper() for r in plan_acct.rows}
        plan_acct.kept_missing = sorted(h["ticker"].upper() for h in existing if h["ticker"].upper() not in listed)


def _compare_with_ledger(ctx: RequestContext, account_id: int, plan_acct: AccountPlan, statement_date: str | None, *, audit: bool) -> dict | None:
    """The file's positions and cash against the account's ledger (read-only). None if the ledger cannot be replayed."""
    from portfolio_agent.services import ledger_service
    stmt = [{"ticker": r["ticker"], "shares": r.get("shares") or 0.0, "cost_basis_total": r.get("cost_basis_total")}
            for r in plan_acct.rows if r.get("ticker")]
    for as_of in (statement_date, None):                   # a statement date the ledger rejects (future / malformed) falls back to "now"
        try:
            return ledger_service.reconcile_account(ctx, account_id, stmt, plan_acct.cash_amount, as_of=as_of, audit=audit)
        except ledger_service.InvalidTransaction:
            continue
    return None


def build_plan(parsed: ParsedUpload, labels: dict[str, str] | None = None, *,
               context: RequestContext | None = None, options: ImportOptions | None = None) -> ImportPlan:
    """Group the upload per account and diff each against what is stored now for this actor."""
    ctx = context or local_context("import")
    labels = labels or {}
    options = options or ImportOptions()
    if parsed.statement is not None:
        return _build_statement_plan(parsed, labels, ctx, options)
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
        p.remove_missing = True if options.remove_missing is None else bool(options.remove_missing)   # a broker file lists everything it holds
        _reconcile(p, ctx)
    return ImportPlan(parsed.broker, parsed.statement_date, parsed.filename,
                      sorted(by_acct.values(), key=lambda a: a.label), options=options)


# ── Retirement-plan statements: funds with no ticker, accounts, and what is kept ───────────────────────────────

_STATEMENT_ASSET_CLASS = {"STABLE_VALUE": "FIXED_INCOME", "MONEY_MARKET": "CASH"}
_STATEMENT_SUB_CLASS = {"TARGET_DATE_FUND": "TARGET_DATE", "STABLE_VALUE": "STABLE_VALUE"}


def statement_broker(meta, fallback: str = "other") -> str:
    """The account's broker key for a statement: the recordkeeper (lower-case slug) when the statement names one, else the fallback."""
    import re as _re
    prov = (getattr(meta, "provider", None) or "").strip()
    slug = _re.sub(r"[^a-z0-9]+", "-", prov.lower()).strip("-")
    return slug or fallback


def synthetic_type_for_hint(hint: str | None) -> str:
    """The instrument type a NEW fund with no ticker gets: stable value and plan money-market funds keep their own type; every other ticker-less
    fund (collective trust, target-date CIT, anything unlabelled) is a CIT."""
    return {"STABLE_VALUE": "STABLE_VALUE", "MONEY_MARKET": "MONEY_MARKET"}.get(hint or "", "CIT")


def match_existing_cit(plan_name: str, fund_name: str, owner_cits: list[dict]) -> tuple[dict | None, bool]:
    """
    The fund-with-no-ticker THIS owner already holds that a statement row refers to: (instrument, ambiguous). First the exact synthetic key for
    (plan name, fund name); else, among funds of the SAME plan (the key starts with the plan's slug; an unnamed plan matches any), the one whose
    fund name is the same after normalisation (share-class and "fund" / "trust" words ignored). More than one candidate is never guessed between:
    (None, True), and the row is proposed as new. A fund's stored name is "Fund (Plan)", so the plan suffix is removed before comparing.
    """
    from portfolio_agent.domain import synthetic_cit_ticker
    from portfolio_agent.tools.fund_name_lookup import normalize_fund_name
    key = synthetic_cit_ticker(plan_name, fund_name)
    for c in owner_cits:
        if c["ticker"] == key:
            return c, False
    want = normalize_fund_name(fund_name)
    if not want:
        return None, False
    prefix = synthetic_cit_ticker(plan_name, "x")[:-1] if plan_name else ""          # "CIT:<PLAN>-"
    suffix = f" ({plan_name})" if plan_name else ""
    cands = []
    for c in owner_cits:
        if prefix and not c["ticker"].startswith(prefix):
            continue
        name = c.get("name") or ""
        fname = name[:-len(suffix)] if suffix and name.endswith(suffix) else name
        if normalize_fund_name(fname) == want:
            cands.append(c)
    return (cands[0], False) if len(cands) == 1 else (None, len(cands) > 1)


def _norm_label(s: str | None) -> str:
    import re as _re
    return _re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _build_statement_plan(parsed: ParsedUpload, labels: dict[str, str], ctx: RequestContext, options: ImportOptions) -> ImportPlan:
    from portfolio_agent.domain import synthetic_cit_ticker
    sp = parsed.statement
    meta = sp.meta
    rows = apply_account_labels([dict(r) for r in sp.rows], labels)
    remaining = missing_account_labels(rows)
    if remaining:
        raise ValueError(f"Account(s) still need a nickname: {remaining}")
    owner_cits = account_service.list_cit_instruments_for(ctx)
    cit_by_key = {c["ticker"]: c for c in owner_cits}
    broker = statement_broker(meta, parsed.broker)
    by_acct: dict[str, AccountPlan] = {}
    for r in rows:
        acct = (r.get("account_number") or "").strip()
        plan_name = r.get("plan_name") or meta.plan_name or ""
        r["plan_name"] = plan_name
        if r.get("ticker_less"):
            key = synthetic_cit_ticker(plan_name, r["fund_name"])
            r["fund_key"] = key
            choice = options.funds.get(key) or {}
            confirmed = (choice.get("ticker") or "").strip().upper()
            if confirmed:                                          # a suggestion the person CONFIRMED: from here on it is an ordinary fund
                r.update({"ticker": confirmed, "ticker_less": False, "resolved_by": "confirmed lookup", "cit_status": None})
            else:
                target = choice.get("map_to") or ""
                matched, ambiguous = None, False
                if target and target != "__new__":
                    matched = cit_by_key.get(target)
                elif not target:
                    matched, ambiguous = match_existing_cit(plan_name, r["fund_name"], owner_cits)
                r.update({"ticker": matched["ticker"] if matched else key, "cit_status": "matched" if matched else "new",
                          "cit_name": (matched or {}).get("name"), "ambiguous": ambiguous, "proxy": (choice.get("proxy") or "").strip().upper() or None})
        p = by_acct.setdefault(acct, AccountPlan(broker, acct, r.get("account_name") or "", [], None, [], is_statement=True,
                                                  account_type=r.get("account_type"), plan_name=plan_name))
        p.rows.append(r)
    accounts = [a for a in account_service.list_accounts_for(ctx) if not a.get("is_paper")]
    for number, p in by_acct.items():
        existing = account_service.find_account(ctx, p.broker, p.account_number)
        chosen = options.account_map.get(number)
        target = next((a for a in accounts if a["account_id"] == chosen), None) if chosen else None
        if target is None and existing is None:                    # the same plan you already track by hand: match by type and name, never guess between two
            same = [a for a in accounts if a.get("account_type") == p.account_type and _norm_label(a.get("display_name")) in (_norm_label(p.account_name), _norm_label(p.plan_name))
                    and _norm_label(a.get("display_name"))]
            target = same[0] if len(same) == 1 else None
        if target is not None:
            p.broker, p.account_number, p.account_name = target["broker"], target["account_number"], target.get("display_name") or p.account_name
            p.matched_existing = True
        p.remove_missing = False if options.remove_missing is None else bool(options.remove_missing)   # a statement need not list everything held
        _reconcile(p, ctx)
    period = meta.period_end
    return ImportPlan(broker, period or parsed.statement_date, parsed.filename, sorted(by_acct.values(), key=lambda a: a.label), options=options,
                      is_statement=True, statement_meta={**meta.as_dict(), "broker": broker}, warnings=list(parsed.warnings), check=dict(sp.check),
                      loan_balance=sp.loan_balance, vested_balance=sp.vested_balance)


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
    reconciled: list[dict] = field(default_factory=list)   # [{label, matches, differences, tickers}] for ledger-managed accounts (compared, not changed)
    funds_created: list[str] = field(default_factory=list)  # statements: new funds with no ticker created
    funds_matched: list[str] = field(default_factory=list)  # statements: funds with no ticker matched to ones you already hold
    kept: list[str] = field(default_factory=list)           # statements: positions the statement did not list, kept
    is_statement: bool = False

    @property
    def message(self) -> str:
        acc = ", ".join(self.accounts)
        msg = (f"Import completed: {self.added} position{'s' if self.added != 1 else ''} added, "
               f"{self.updated} updated, {self.removed} removed ({BROKER_LABELS.get(self.broker, self.broker.title())} · {acc}).")
        if self.is_statement:
            msg = (f"Statement imported: {self.added} fund{'s' if self.added != 1 else ''} added, {self.updated} updated"
                   f"{f', {self.removed} removed' if self.removed else ''} ({BROKER_LABELS.get(self.broker, self.broker.title())} · {acc}).")
            if self.funds_created or self.funds_matched:
                msg += f" Funds with no ticker: {len(self.funds_created)} new, {len(self.funds_matched)} matched to ones you already hold."
            if self.kept:
                msg += f" Kept (not on this statement): {', '.join(self.kept[:8])}."
        if self.unpriced:
            msg += f" No price available for {', '.join(self.unpriced)}."
        for r in self.reconciled:
            if r["matches"] is None:
                msg += f" {r['label']} is managed by its ledger; the file could not be compared with it."
            elif r["matches"]:
                msg += f" {r['label']} is managed by its ledger: the file matches it (nothing changed)."
            else:
                msg += (f" {r['label']} is managed by its ledger, so the file was compared, not applied: {r['differences']} difference"
                        f"{'s' if r['differences'] != 1 else ''} ({', '.join(r['tickers'][:8])}) — nothing changed; record or void transactions to resolve.")
        return msg


def enrich_prices(rows: list[dict]) -> tuple[list[dict], str | None]:
    """Fill current_price/current_value/price_as_of where the file gave no price. Unknown stays None."""
    need = sorted({r["ticker"] for r in rows if r.get("current_price") is None and not is_synthetic_ticker(r["ticker"])})   # a fund with no ticker has no market price
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

    An account managed by its transaction ledger (holdings_source = "ledger") is NEVER overwritten: its positions and cash come from its
    ledger. The file is compared with the ledger after the commit (read-only; the differences are reported in ImportResult.reconciled and
    audited) and nothing about that account is changed.

    A quarterly plan STATEMENT (plan.is_statement) differs in four ways: positions the statement does not list are KEPT unless the person asked to
    remove them (options.remove_missing); the account's cash line is left alone (a statement has none); each fund with no ticker is matched to the
    one you already hold or created as a new fund with no ticker (a "CIT:" key; stable value and plan money-market funds keep their own type), inside
    the transaction; and a no-ticker fund's price comes from the statement (set_position_price as of the statement's end date, reason "quarterly
    statement <period>"), a balance-only fund keeps its balance with the statement's date as its price date.

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
    ledger_accounts: list[tuple[AccountPlan, int]] = []
    funds_created: list[str] = []
    funds_matched: list[str] = []
    kept: list[str] = []
    meta = plan.statement_meta or {}
    period_txt = (f"{meta['period_start']} – {meta['period_end']}" if meta.get("period_start") and meta.get("period_end")
                  else (meta.get("period_end") or statement))
    with repo.transaction() as conn:
        run_id = repo.record_import_run(broker=plan.broker, accounts=[a.label for a in plan.accounts],
                                        filename=plan.filename, statement_date=statement, conn=conn)
        for acct in plan.accounts:
            account = account_service.find_or_create_import_account(
                ctx, broker=acct.broker, account_number=acct.account_number, display_name=acct.account_name,
                account_type=next((r.get("account_type") for r in acct.rows if r.get("account_type")), None),
                conn=conn)
            acct.account_id = account["account_id"]
            if account.get("holdings_source") == "ledger":
                acct.ledger_managed = True
                ledger_accounts.append((acct, account["account_id"]))      # compared after the commit; nothing written for it here
                continue
            if plan.is_statement:
                _create_statement_funds(ctx, conn, acct, priced_rows[acct.account_number], period_txt, funds_created, funds_matched)
            res = account_service.replace_account_snapshot(
                ctx, account["account_id"], priced_rows[acct.account_number],
                as_of_date=today, source_import_id=run_id, remove_missing=acct.remove_missing, conn=conn)
            for k in totals:
                totals[k] += res[k]
            if plan.is_statement:
                _apply_statement_prices(ctx, conn, account["account_id"], priced_rows[acct.account_number], statement, period_txt)
                _apply_plan_details(ctx, conn, account["account_id"], acct, plan, period_txt)
                listed = {(r.get("ticker") or "").upper() for r in priced_rows[acct.account_number]}
                if not acct.remove_missing:
                    kept += [h["ticker"] for h in repo.list_positions(account["account_id"], conn=conn) if h["ticker"].upper() not in listed]
                continue                                           # a statement has no cash line: the account's cash is left as it is
            account_service.record_account_cash(
                ctx, account["account_id"], acct.cash_amount, as_of_date=statement,
                detail=", ".join(acct.cash_lines) or None, reason="import", conn=conn)
            cash_accounts += acct.cash_amount is not None
        repo.update_import_run(run_id, conn=conn, cash_accounts=cash_accounts, **totals)
    reconciled: list[dict] = []
    for acct, aid in ledger_accounts:                      # read-only comparisons, after the import's transaction has closed
        rec = _compare_with_ledger(ctx, aid, acct, plan.statement_date, audit=True)
        diffs = [p["ticker"] for p in rec["positions"] if p["status"] != "MATCH"] if rec else []
        if rec and rec["cash"] and rec["cash"]["status"] != "MATCH":
            diffs.append("cash")
        acct.reconciliation = rec
        reconciled.append({"label": acct.label, "matches": None if rec is None else rec["matches"], "differences": len(diffs), "tickers": diffs})
    # Classify any security seen for the first time (stock / ETF / fund / ...). A network lookup, so it runs
    # after the commit; best effort — a failed lookup leaves the instrument UNKNOWN and never fails the import.
    from portfolio_agent.services.instrument_classifier import classify_new_holdings
    classify_new_holdings(ctx, sorted({r["ticker"] for a in plan.accounts if not a.ledger_managed for r in priced_rows[a.account_number]
                                       if r.get("ticker") and not is_synthetic_ticker(r["ticker"])}))
    return ImportResult(run_id, plan.broker, [a.label for a in plan.accounts], totals["added"], totals["updated"],
                        totals["removed"], totals["positions"], cash_accounts, statement, price_as_of, sorted(unpriced), reconciled,
                        funds_created=funds_created, funds_matched=funds_matched, kept=sorted(set(kept)), is_statement=plan.is_statement)


def _create_statement_funds(ctx: RequestContext, conn, acct: AccountPlan, rows: list[dict], period_txt: str,
                            created: list[str], matched: list[str]) -> None:
    """Inside the import's transaction: create each NEW fund with no ticker (its type from the statement's hint); note the matched ones."""
    for r in rows:
        if r.get("cit_status") == "new":
            inst = account_service.create_synthetic_instrument(
                ctx, plan_name=r.get("plan_name") or "", fund_name=r["fund_name"], asset_class=_STATEMENT_ASSET_CLASS.get(r.get("instrument_hint"), "UNKNOWN"),
                sub_asset_class=_STATEMENT_SUB_CLASS.get(r.get("instrument_hint")), proxy_ticker=r.get("proxy"),
                instrument_type=synthetic_type_for_hint(r.get("instrument_hint")), reason=f"created from quarterly statement {period_txt}", conn=conn)
            if inst["ticker"] != r["ticker"]:
                raise ValueError(f"internal: fund key {inst['ticker']} does not match the plan's {r['ticker']}")
            if inst["ticker"] not in created:                      # the same fund can appear in two accounts (pre-tax and Roth)
                created.append(inst["ticker"])
        elif r.get("cit_status") == "matched" and r["ticker"] not in matched:
            matched.append(r["ticker"])


def _apply_plan_details(ctx: RequestContext, conn, account_id: int, acct: AccountPlan, plan: ImportPlan, period_txt: str) -> None:
    """Record the retirement-plan facts the statement states on the account: plan name, provider, statement period end, and (on the plan's main account,
    not its Roth sibling) the loan balance and the vested total. A fact the statement does not state is left as it was; nothing is cleared or guessed."""
    meta = plan.statement_meta or {}
    facts: dict = {"plan_name": acct.plan_name or meta.get("plan_name"), "statement_period_end": meta.get("period_end")}
    if meta.get("provider"):
        facts["provider"] = meta["provider"]
    if acct.account_type != "ROTH_401K":
        if plan.loan_balance is not None:
            facts["loan_balance"] = plan.loan_balance
        if plan.vested_balance is not None:
            facts["vested_balance"] = plan.vested_balance
    facts = {k: v for k, v in facts.items() if v is not None}
    if facts:
        account_service.set_retirement_details(ctx, account_id, reason=f"quarterly statement {period_txt}", conn=conn, **facts)


def _apply_statement_prices(ctx: RequestContext, conn, account_id: int, rows: list[dict], statement_end: str, period_txt: str) -> None:
    """A no-ticker fund's price is the statement's: set_position_price as of the statement end date. A balance-only fund (no units or no unit price)
    keeps the balance the import stored and gets the statement's date as its price date (nothing is computed). A matched fund gets the person's
    proxy ticker only if it has none (a proxy already chosen is never overwritten)."""
    for r in rows:
        if not r.get("cit_status"):
            continue
        pos = repo.find_position(account_id, r["ticker"], conn=conn)
        if pos is None:
            continue
        as_of = r.get("as_of_date") or statement_end
        if r.get("current_price") and pos.get("shares"):
            account_service.set_position_price(ctx, pos["position_id"], r["current_price"], as_of_date=as_of,
                                               reason=f"quarterly statement {period_txt}", conn=conn)
            if r.get("current_value") is not None:                  # the balance AS PRINTED wins over units x price (statement prices are rounded)
                repo.update_position(pos["position_id"], conn=conn, touch=False, current_value=r["current_value"])
        elif r.get("current_value") is not None:
            repo.update_position(pos["position_id"], conn=conn, touch=False, price_as_of=as_of)
        if r.get("cit_status") == "matched" and r.get("proxy"):
            inst = repo.get_instrument(pos["instrument_id"], conn=conn) or {}
            if not inst.get("proxy_ticker"):
                account_service.set_instrument_classification(ctx, pos["instrument_id"], proxy_ticker=r["proxy"],
                                                              reason=f"proxy chosen on quarterly statement {period_txt}", conn=conn)
