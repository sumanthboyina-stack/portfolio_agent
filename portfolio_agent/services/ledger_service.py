"""
Ledger service — the only writer of the transaction ledger. Every verb: checks the actor owns the account, validates the fields, REPLAYS
the account's whole ledger with the new row included (an invalid change — selling shares that were not held, voiding a buy that a later
sale depends on — is refused before anything is written), appends the row, rebuilds the account's tax lots and closures, and records an
audit event, all in ONE database transaction.

Verbs   record_trade · record_cash_transfer · record_income · record_employer_match · record_fee · record_split · void_transaction
Reads   list_transactions · get_lots · get_closures · get_ledger_summary
        set_account_lot_method

Rules
  * The ledger is append-only (triggers in the database enforce it). A correction is a VOID row, which needs a reason.
  * The tax-lot method of a SELL is resolved when it is RECORDED (the sale's own choice, else the account's, else the portfolio-wide
    default, FIFO) and stored on the sale, so changing a setting later never changes a past sale.
  * `external_id` makes a write idempotent: recording the same (account, external_id) again returns the stored row and writes nothing.
  * Accounts whose holdings_source is "snapshot" (positions typed in or imported from a broker file) take transactions as HISTORY ONLY:
    they are stored, but nothing is validated against positions and no lots are built, because the snapshot — not the ledger — is the truth
    there. Accounts whose holdings_source is "ledger" get the full replay, lots and closures.
  * Trades cannot be dated in the future. Nothing here infers a trade from a change in positions.
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import date, datetime

from portfolio_agent.domain import LOT_METHODS, HoldingsSource, TransactionType
from portfolio_agent.services.account_service import LedgerManaged, NotAuthorized, NotFound, _owned_account
from portfolio_agent.services.context import RequestContext, require_context
from portfolio_agent.tools import holdings_db as repo
from portfolio_agent.tools import investor_settings_db as sdb
from portfolio_agent.tools import ledger_db as ldb
from portfolio_agent.tools.ledger_engine import LedgerError, replay

QTY_PLACES = 8


class InvalidTransaction(ValueError):
    """The transaction is not acceptable (bad field, or it would make the ledger inconsistent)."""


class DuplicateTransaction(ValueError):
    pass


# ── validation helpers ────────────────────────────────────────────────────────

def _today() -> date:
    return date.today()


def _date(v, what: str, *, allow_future: bool = False) -> str:
    try:
        d = v if isinstance(v, date) and not isinstance(v, datetime) else date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        raise InvalidTransaction(f"{what}: not a date (YYYY-MM-DD)") from None
    if d > _today() and not allow_future:
        raise InvalidTransaction(f"{what}: {d.isoformat()} is in the future")
    return d.isoformat()


def _num(v, what: str, *, positive: bool = True, zero_ok: bool = False) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise InvalidTransaction(f"{what}: not a number") from None
    if not math.isfinite(f):
        raise InvalidTransaction(f"{what}: not a finite number")
    if positive and (f < 0 or (f == 0 and not zero_ok)):
        raise InvalidTransaction(f"{what}: must be {'zero or ' if zero_ok else ''}greater than 0")
    return f


def _money(v) -> float:
    return round(float(v), 2)


def _ledger_account(account: dict) -> bool:
    return account.get("holdings_source") == HoldingsSource.LEDGER.value


def _instrument(ticker: str, c: sqlite3.Connection) -> dict:
    t = (ticker or "").strip().upper()
    if not t:
        raise InvalidTransaction("ticker is required")
    inst = repo.find_instrument(t, conn=c)
    if inst is None:
        inst = repo.get_or_create_instrument(t, conn=c)
    return inst


def _resolve_method(requested: str | None, account: dict, c: sqlite3.Connection) -> str:
    """The sale's own choice, else the account's, else the portfolio-wide default; stored on the sale."""
    if requested:
        m = str(requested).strip().upper()
        if m not in LOT_METHODS:
            raise InvalidTransaction(f"lot method {requested!r}: one of {sorted(LOT_METHODS)}")
        return m
    if account.get("lot_method"):
        return account["lot_method"]
    return sdb.get_portfolio_settings(conn=c).get("default_lot_method") or "FIFO"


def _snap(t: dict | None) -> dict | None:
    if t is None:
        return None
    return {k: t.get(k) for k in ("txn_id", "txn_type", "ticker", "trade_date", "quantity", "price", "fees", "amount", "lot_method",
                                  "voids_txn_id", "external_id", "memo", "contribution_source", "clearance_status", "clearance_approval_id")}


# ── the one write path ────────────────────────────────────────────────────────

def _append(ctx: RequestContext, c: sqlite3.Connection, account: dict, fields: dict, operation: str, *, reason: str | None = None) -> dict:
    """Validate by replay, append, rebuild derived lots, audit. `fields` are transactions columns (account_id / created_by are set here)."""
    ext = fields.get("external_id")
    if ext:
        existing = ldb.find_by_external_id(c, account["account_id"], ext)
        if existing is not None:
            return {"txn": existing, "duplicate": True, "realized": []}
    row = {**fields, "account_id": account["account_id"], "created_by": ctx.actor, "request_id": ctx.request_id,
           "source": fields.get("source") or ctx.source}
    if "lot_selection" in row:
        sel = row.pop("lot_selection")
        row["lot_selection_json"] = json.dumps(sel) if sel else None
    ledger = _ledger_account(account)
    start = account.get("ledger_start_date")
    if ledger and start and row["trade_date"] < start and row["txn_type"] != "VOID":
        raise InvalidTransaction(f"trade_date {row['trade_date']} is before this account's ledger started ({start}); earlier activity is history only")
    txn_id = ldb.insert_transaction(c, row)
    new_row = ldb.get_transaction(c, txn_id)
    realized: list[dict] = []
    if ledger:
        try:
            state = replay(ldb.txns_for_replay(c, account["account_id"]))
        except LedgerError as exc:
            raise InvalidTransaction(str(exc)) from None                      # raised inside the caller's unit of work: rolled back
        ldb.replace_derived(c, account["account_id"], state)
        _sync_positions(c, account, state)
        realized = [{"open_transaction_id": cl.open_transaction_id, "quantity": cl.quantity, "proceeds": cl.proceeds,
                     "cost_basis": cl.cost_basis, "realized_gain": cl.realized_gain, "holding_period": cl.holding_period}
                    for cl in state.closures if cl.close_transaction_id == txn_id]
    repo.record_audit_event(actor=ctx.actor, source=ctx.source, request_id=ctx.request_id, operation=operation,
                            portfolio_id=account["portfolio_id"], account_id=account["account_id"], before=None,
                            after=_snap(new_row), reason=reason, conn=c)
    return {"txn": new_row, "duplicate": False, "realized": realized}


def _common(external_id, memo, *, import_run_id: int | None = None, contribution_source: str | None = None) -> dict:
    cs = (str(contribution_source).strip().upper()[:40] or None) if contribution_source else None
    return {"external_id": (str(external_id).strip() or None) if external_id is not None else None,
            "memo": (str(memo).strip() or None) if memo else None, "import_run_id": import_run_id, "contribution_source": cs}


# ── pre-clearance ─────────────────────────────────────────────────────────────

def _clearance_lookup(owner: str, ticker: str, side: str, trade_date: str) -> tuple[str, int | None]:
    """(status, approval_id) for a trade by `owner`. Reads the owner's profile and trade_approvals (each its own short connection, so call it
    BEFORE a write transaction opens). Anything unreadable is 'unknown', never 'missing' or 'approved'."""
    try:
        from portfolio_agent.tools.user_profile_db import get_user_profile_by_owner
        required = bool(get_user_profile_by_owner(owner).pre_clearance_required)
    except Exception:
        return "unknown", None
    from portfolio_agent.clearance import evaluate_ledger_clearance
    return evaluate_ledger_clearance(owner, (ticker or "").strip().upper(), side, trade_date, pre_clearance_required=required)


def notify_clearance_missing(owner: str, txn: dict) -> dict:
    """Enqueue the COMPLIANCE notification for a trade with no pre-clearance approval. BRIEF text with NO amounts (ticker, side and date only);
    deduplicated per transaction (ledger_clearance:<txn id>) however often it is called; never raises. Call after the trade has committed."""
    from portfolio_agent.services import notification_service
    side = str(txn.get("txn_type") or "").lower()
    return notification_service.notify(
        owner, "COMPLIANCE", dedup_key=f"ledger_clearance:{txn['txn_id']}", title="Trade without pre-clearance",
        brief=f"A {side} of {txn.get('ticker') or 'a security'} on {txn.get('trade_date')} has no pre-clearance approval on file. "
              f"Review it on the Accounts page.")


# ── verbs ─────────────────────────────────────────────────────────────────────

def record_trade(ctx: RequestContext, account_id: int, ticker: str, side: str, quantity: float, price: float, trade_date, *,
                 fees: float = 0.0, settle_date=None, lot_method: str | None = None, lot_selection: list[dict] | None = None,
                 external_id: str | None = None, memo: str | None = None, reason: str | None = None,
                 import_run_id: int | None = None, clearance: tuple[str, int | None] | None = None,
                 conn: sqlite3.Connection | None = None) -> dict:
    """A BUY or SELL. BUY opens a lot (basis = quantity x price + fees). SELL closes lots by `lot_method` (else the account's, else the
    default); for SPECIFIC give lot_selection = [{"lot": open_transaction_id, "quantity": q}, ...] adding up to the sale. The result lists
    the realized gain of each lot closed. Cash moves by -(quantity x price + fees) for a buy, +(quantity x price - fees) for a sale.

    PRE-CLEARANCE. If the owner's profile has pre_clearance_required on and the account is not a paper account, the trade is checked against
    trade_approvals (clearance.evaluate_ledger_clearance) for an approval covering ticker + side + trade_date. The trade is NEVER blocked (it
    already happened): the result is stored on the row (clearance_status approved | missing | not_required | unknown, clearance_approval_id)
    and audited. 'missing' enqueues a brief COMPLIANCE notification (dedup key ledger_clearance:<txn id>) once the transaction has committed;
    when the caller passes its own `conn`, the commit is theirs, so the result carries notify_pending=True instead and the caller notifies
    (notify_clearance_missing) after committing, and the caller should read the clearance BEFORE opening its transaction and pass it as
    `clearance=(status, approval_id)` (the approvals tables run schema maintenance on every connection, which would wait on the caller's lock). A paper account is not checked at all (clearance_status stays NULL)."""
    require_context(ctx)
    side = (side or "").strip().upper()
    if side not in ("BUY", "SELL"):
        raise InvalidTransaction("side must be BUY or SELL")
    q = round(_num(quantity, "quantity"), QTY_PLACES)
    px = _num(price, "price", zero_ok=(side == "SELL"))
    fee = _num(fees or 0.0, "fees", zero_ok=True)
    day = _date(trade_date, "trade_date")
    settle = _date(settle_date, "settle_date", allow_future=True) if settle_date else None
    if side == "SELL":
        sdb.get_portfolio_settings()          # makes sure the settings schema exists BEFORE the write transaction opens (the default method is read inside it)
    if clearance is None:                  # (a caller that opens its own write transaction reads it first and passes it in: see import_transactions_csv)
        clearance = _clearance_lookup(ctx.actor, ticker, side, day)       # reads other tables: done before the write transaction opens
    with repo.unit_of_work(conn) as c:
        account = _owned_account(ctx, account_id, c)
        inst = _instrument(ticker, c)
        cash = round(-(q * px + fee), 2) if side == "BUY" else round(q * px - fee, 2)
        method = None
        if side == "SELL":
            method = _resolve_method("SPECIFIC" if lot_selection else lot_method, account, c)
            if method == "SPECIFIC" and not lot_selection:
                raise InvalidTransaction("a SPECIFIC sale must name its lots (lot_selection)")
            if lot_selection and method != "SPECIFIC":
                raise InvalidTransaction("lot_selection is only for lot_method SPECIFIC")
        elif lot_method or lot_selection:
            raise InvalidTransaction("a lot method applies to sales only")
        status, approval_id = (None, None) if account.get("is_paper") else clearance
        res = _append(ctx, c, account, {
            "instrument_id": inst["instrument_id"], "txn_type": side, "trade_date": day, "settle_date": settle, "quantity": q, "price": px,
            "fees": round(fee, 2), "amount": cash, "lot_method": method, "lot_selection": lot_selection,
            "clearance_status": status, "clearance_approval_id": approval_id,
            **_common(external_id, memo, import_run_id=import_run_id)}, f"record_{side.lower()}", reason=reason)
        res["notify_pending"] = (not res["duplicate"]) and res["txn"].get("clearance_status") == "missing"
    if res["notify_pending"] and conn is None:
        notify_clearance_missing(ctx.actor, res["txn"])
        res["notify_pending"] = False
    return res


def record_cash_transfer(ctx: RequestContext, account_id: int, direction: str, amount: float, trade_date, *,
                         contribution_source: str | None = None, external_id: str | None = None, memo: str | None = None,
                         reason: str | None = None, import_run_id: int | None = None, conn: sqlite3.Connection | None = None) -> dict:
    """Cash in (DEPOSIT) or out (WITHDRAWAL) of the account from outside it. `amount` is positive. `contribution_source` (free text, upper-cased:
    EMPLOYEE, EMPLOYER, ROLLOVER ...) says where a deposit came from."""
    require_context(ctx)
    d = (direction or "").strip().upper()
    if d not in ("DEPOSIT", "WITHDRAWAL"):
        raise InvalidTransaction("direction must be DEPOSIT or WITHDRAWAL")
    amt = _money(_num(amount, "amount"))
    day = _date(trade_date, "trade_date")
    with repo.unit_of_work(conn) as c:
        account = _owned_account(ctx, account_id, c)
        return _append(ctx, c, account, {"txn_type": d, "trade_date": day, "amount": amt if d == "DEPOSIT" else -amt,
                                         **_common(external_id, memo, import_run_id=import_run_id, contribution_source=contribution_source)},
                       f"record_{d.lower()}", reason=reason)


def record_income(ctx: RequestContext, account_id: int, kind: str, amount: float, trade_date, *, ticker: str | None = None,
                  external_id: str | None = None, memo: str | None = None, reason: str | None = None,
                  import_run_id: int | None = None, conn: sqlite3.Connection | None = None) -> dict:
    """A DIVIDEND (usually with its ticker) or INTEREST payment received into the account's cash. Reinvestment is a separate BUY."""
    require_context(ctx)
    k = (kind or "").strip().upper()
    if k not in ("DIVIDEND", "INTEREST"):
        raise InvalidTransaction("kind must be DIVIDEND or INTEREST")
    amt = _money(_num(amount, "amount"))
    day = _date(trade_date, "trade_date")
    with repo.unit_of_work(conn) as c:
        account = _owned_account(ctx, account_id, c)
        iid = _instrument(ticker, c)["instrument_id"] if ticker else None
        return _append(ctx, c, account, {"instrument_id": iid, "txn_type": k, "trade_date": day, "amount": amt,
                                         **_common(external_id, memo, import_run_id=import_run_id)}, f"record_{k.lower()}", reason=reason)


def record_employer_match(ctx: RequestContext, account_id: int, trade_date, *, amount: float | None = None, ticker: str | None = None,
                          quantity: float | None = None, price: float | None = None, contribution_source: str | None = None,
                          external_id: str | None = None, memo: str | None = None, reason: str | None = None,
                          import_run_id: int | None = None, conn: sqlite3.Connection | None = None) -> dict:
    """An employer contribution. Either CASH (`amount`) credited to the account, or SHARES (`ticker`, `quantity`, `price`) that open a tax
    lot at that price with no cash movement — whichever form the plan delivered it in, not both."""
    require_context(ctx)
    day = _date(trade_date, "trade_date")
    shares = ticker is not None or quantity is not None or price is not None
    if shares and amount is not None:
        raise InvalidTransaction("an employer match is either cash (amount) or shares (ticker, quantity, price), not both")
    if not shares and amount is None:
        raise InvalidTransaction("give either amount (cash) or ticker, quantity and price (shares)")
    with repo.unit_of_work(conn) as c:
        account = _owned_account(ctx, account_id, c)
        if shares:
            if not (ticker and quantity is not None and price is not None):
                raise InvalidTransaction("shares need ticker, quantity and price")
            q, px = round(_num(quantity, "quantity"), QTY_PLACES), _num(price, "price")
            fields = {"instrument_id": _instrument(ticker, c)["instrument_id"], "quantity": q, "price": px, "amount": 0.0}
        else:
            fields = {"amount": _money(_num(amount, "amount"))}
        return _append(ctx, c, account, {"txn_type": "EMPLOYER_MATCH", "trade_date": day, **fields,
                                         **_common(external_id, memo, import_run_id=import_run_id, contribution_source=contribution_source)},
                       "record_employer_match", reason=reason)


def record_fee(ctx: RequestContext, account_id: int, amount: float, trade_date, *, ticker: str | None = None,
               external_id: str | None = None, memo: str | None = None, reason: str | None = None,
               import_run_id: int | None = None, conn: sqlite3.Connection | None = None) -> dict:
    """An account-level fee (advisory, custody, ...) taken from cash. `amount` is positive. A trade's own commission goes in record_trade."""
    require_context(ctx)
    amt = _money(_num(amount, "amount"))
    day = _date(trade_date, "trade_date")
    with repo.unit_of_work(conn) as c:
        account = _owned_account(ctx, account_id, c)
        iid = _instrument(ticker, c)["instrument_id"] if ticker else None
        return _append(ctx, c, account, {"instrument_id": iid, "txn_type": "FEE", "trade_date": day, "amount": -amt,
                                         **_common(external_id, memo, import_run_id=import_run_id)}, "record_fee", reason=reason)


def record_split(ctx: RequestContext, ticker: str, numerator: float, denominator: float, effective_date, *,
                 account_id: int | None = None, external_id: str | None = None, memo: str | None = None, reason: str | None = None,
                 conn: sqlite3.Connection | None = None) -> list[dict]:
    """A split of `ticker`, numerator-for-denominator (2 for 1 = 2, 1; a 1-for-10 reverse split = 1, 10). Recorded once per account that
    held the ticker on the date (or only `account_id`). Share counts scale and unit cost divides; total basis does not change. Returns the
    transactions written; `external_id`, if given, is made unique per account by suffixing the account id."""
    require_context(ctx)
    n, d = _num(numerator, "numerator"), _num(denominator, "denominator")
    day = _date(effective_date, "effective_date")
    out: list[dict] = []
    with repo.unit_of_work(conn) as c:
        inst = _instrument(ticker, c)
        portfolio = repo.get_portfolio_by_owner(ctx.actor, conn=c)
        if portfolio is None:
            raise NotAuthorized(f"{ctx.actor!r} has no portfolio here")
        if account_id is not None:
            targets = [_owned_account(ctx, account_id, c)]
        else:
            targets = [a for a in repo.list_accounts(portfolio["portfolio_id"], conn=c) if _held_on(c, a, inst["instrument_id"], day)]
        if not targets:
            raise InvalidTransaction(f"no account of yours held {inst['ticker']} on {day}")
        for a in targets:
            ext = f"{external_id}:{a['account_id']}" if external_id else None
            out.append(_append(ctx, c, a, {"instrument_id": inst["instrument_id"], "txn_type": "SPLIT", "trade_date": day,
                                           "split_numerator": n, "split_denominator": d, **_common(ext, memo)},
                               "record_split", reason=reason))
    return out


def _held_on(c: sqlite3.Connection, account: dict, instrument_id: int, day: str) -> bool:
    """Did the account hold the instrument on `day`? Ledger accounts: from the replay; snapshot accounts: the position as it is now."""
    if _ledger_account(account):
        try:
            txns = [t for t in ldb.txns_for_replay(c, account["account_id"]) if t.trade_date < day or t.txn_type == "VOID"]
            return any(p for iid, p in replay(txns).positions().items() if iid == instrument_id)
        except LedgerError:
            return False
    pos = [p for p in repo.list_positions(account["account_id"], conn=c) if p.get("instrument_id") == instrument_id]
    return bool(pos and (pos[0].get("shares") or 0) > 0)


def void_transaction(ctx: RequestContext, txn_id: int, *, reason: str, conn: sqlite3.Connection | None = None) -> dict:
    """Cancel a transaction by appending a VOID row that points at it (the original stays in the ledger, marked voided). A reason is
    required. Refused if the cancelled row is a VOID, is already voided, or if the ledger without it would be inconsistent (for example
    voiding a buy whose shares a later sale sold)."""
    require_context(ctx)
    why = (reason or "").strip()
    if not why:
        raise InvalidTransaction("a reason is required to void a transaction")
    with repo.unit_of_work(conn) as c:
        target = ldb.get_transaction(c, txn_id)
        if target is None:
            raise NotFound(f"transaction {txn_id} does not exist")
        account = _owned_account(ctx, target["account_id"], c, allow_archived=True)
        if target["txn_type"] == "VOID":
            raise InvalidTransaction("a void cannot be voided")
        if any(r["voids_txn_id"] == txn_id for r in ldb.list_transactions(c, account["account_id"])):
            raise InvalidTransaction(f"transaction {txn_id} is already voided")
        res = _append(ctx, c, account, {"txn_type": "VOID", "trade_date": _today().isoformat(), "voids_txn_id": txn_id, "memo": why},
                      "void_transaction", reason=why)
        res["voided"] = target
        return res


# ── account setting ───────────────────────────────────────────────────────────

def set_account_lot_method(ctx: RequestContext, account_id: int, method: str | None, *, reason: str | None = None,
                           conn: sqlite3.Connection | None = None) -> dict:
    """The standing tax-lot method for an account's FUTURE sales (None = use the portfolio-wide default). Past sales keep the method stored
    on them. FIFO / LIFO / HIFO only (SPECIFIC is a per-sale choice)."""
    require_context(ctx)
    m = None if method in (None, "") else str(method).strip().upper()
    if m is not None and m not in sdb.STANDING_LOT_METHODS:
        raise InvalidTransaction(f"lot method: one of {list(sdb.STANDING_LOT_METHODS)} or None, not {method!r}")
    with repo.unit_of_work(conn) as c:
        account = _owned_account(ctx, account_id, c)
        before = account.get("lot_method")
        repo.update_account(account_id, lot_method=m, conn=c)
        repo.record_audit_event(actor=ctx.actor, source=ctx.source, request_id=ctx.request_id, operation="set_account_lot_method",
                                portfolio_id=account["portfolio_id"], account_id=account_id, before={"lot_method": before},
                                after={"lot_method": m}, reason=reason, conn=c)
        return repo.get_account(account_id, conn=c)


# ── reads ─────────────────────────────────────────────────────────────────────

def list_transactions(ctx: RequestContext, account_id: int, *, include_voided: bool = True) -> list[dict]:
    require_context(ctx)
    with repo.unit_of_work() as c:
        _owned_account(ctx, account_id, c, allow_archived=True)
        return ldb.list_transactions(c, account_id, include_voided=include_voided)


def get_lots(ctx: RequestContext, account_id: int, *, open_only: bool = True) -> list[dict]:
    require_context(ctx)
    with repo.unit_of_work() as c:
        _owned_account(ctx, account_id, c, allow_archived=True)
        return ldb.list_lots(c, account_id, open_only=open_only)


def get_closures(ctx: RequestContext, account_id: int) -> list[dict]:
    require_context(ctx)
    with repo.unit_of_work() as c:
        _owned_account(ctx, account_id, c, allow_archived=True)
        return ldb.list_closures(c, account_id)


def get_ledger_summary(ctx: RequestContext, account_id: int) -> dict:
    """Cash, open positions (quantity, basis), realized gain and flow totals as replayed from the account's ledger. Computed fresh; for a
    snapshot account this is the history-only view and does not describe its positions."""
    require_context(ctx)
    with repo.unit_of_work() as c:
        account = _owned_account(ctx, account_id, c, allow_archived=True)
        try:
            state = replay(ldb.txns_for_replay(c, account_id))
        except LedgerError as exc:
            return {"account_id": account_id, "holdings_source": account.get("holdings_source"), "error": str(exc)}
        tick = {r["instrument_id"]: r["ticker"] for r in c.execute("SELECT instrument_id, ticker FROM instruments").fetchall()}
    return {"account_id": account_id, "holdings_source": account.get("holdings_source"), "cash": state.cash, "totals": state.totals,
            "realized_gain": state.realized_gain(),
            "positions": {tick[i]: p for i, p in state.positions().items()}, "transactions": state.applied}


# ── ledger-managed accounts: positions and cash are DERIVED ───────────────────

def _sync_positions(c: sqlite3.Connection, account: dict, state) -> dict:
    """
    Make the account's positions and cash equal what the replay says (shares, average cost, cost basis from the open lots; cash from the
    ledger). Existing position rows are updated in place (their price, sector and ids are kept); instruments no longer held are removed.
    Writes only positions / account_cash; never a transaction. Runs inside the caller's database transaction.
    """
    aid = account["account_id"]
    existing = {p["instrument_id"]: p for p in repo.list_positions(aid, conn=c)}
    derived = state.positions()
    names = {r["instrument_id"]: (r["ticker"], r["name"]) for r in c.execute("SELECT instrument_id, ticker, name FROM instruments").fetchall()}
    last = max((t.trade_date for t in ldb.txns_for_replay(c, aid)), default=_today().isoformat())
    added, updated = [], []
    for iid, d in derived.items():
        price = (existing.get(iid) or {}).get("current_price")
        fields = {"shares": d["quantity"], "avg_cost": d["avg_cost"], "cost_basis_total": d["cost_basis"], "as_of_date": last,
                  "current_value": round(price * d["quantity"], 2) if price else None}
        if iid in existing:
            repo.update_position(existing[iid]["position_id"], conn=c, **fields)
            updated.append(names[iid][0])
        else:
            ticker, name = names[iid]
            repo.insert_position(aid, repo.position_fields({"ticker": ticker, "description": name, **fields}, last), conn=c)
            added.append(ticker)
    removed = []
    for iid, p in existing.items():
        if iid not in derived:
            repo.delete_position(p["position_id"], conn=c)
            removed.append(p["ticker"])
    if state.applied:
        repo.set_account_cash(aid, state.cash, last, "derived from the transaction ledger", conn=c)
    return {"added": sorted(added), "updated": sorted(updated), "removed": sorted(removed), "cash": state.cash if state.applied else None}


def rebuild_positions_from_ledger(ctx: RequestContext, account_id: int, *, reason: str | None = None,
                                  conn: sqlite3.Connection | None = None) -> dict:
    """
    Re-derive a ledger account's tax lots, positions and cash from its transactions (repair / verification: every ledger write already
    does this). Refused for a snapshot account, whose positions are the truth and are never overwritten from a ledger. Returns what changed.
    """
    require_context(ctx)
    with repo.unit_of_work(conn) as c:
        account = _owned_account(ctx, account_id, c)
        if not _ledger_account(account):
            raise InvalidTransaction("this account is a snapshot account: its positions are the source of truth, not its ledger")
        try:
            state = replay(ldb.txns_for_replay(c, account_id))
        except LedgerError as exc:
            raise InvalidTransaction(f"the ledger does not replay: {exc}") from None
        ldb.replace_derived(c, account_id, state)
        out = _sync_positions(c, account, state)
        if out["added"] or out["updated"] or out["removed"]:
            repo.record_audit_event(actor=ctx.actor, source=ctx.source, request_id=ctx.request_id, operation="rebuild_positions_from_ledger",
                                    portfolio_id=account["portfolio_id"], account_id=account_id, after=out, reason=reason, conn=c)
        return out


def _opening_plan(c: sqlite3.Connection, account_id: int, overrides: dict[str, float], allow_unknown_basis: bool):
    """What switch_to_ledger would record: (problems, unknown_basis tickers, [(position, shares, unit_cost)]). Reads only."""
    positions = [p for p in repo.list_positions(account_id, conn=c) if p.get("shares")]
    problems, unknown, plan = [], [], []
    for p in positions:
        sh = float(p["shares"])
        if sh < 0:
            problems.append(f"{p['ticker']}: short positions are not supported")
            continue
        unit = None
        if p["ticker"] in overrides:
            unit = overrides[p["ticker"]]
        elif p.get("cost_basis_total") is not None:
            unit = float(p["cost_basis_total"]) / sh
        elif p.get("avg_cost") is not None:
            unit = float(p["avg_cost"])
        if unit is None or unit < 0:
            if not allow_unknown_basis:
                problems.append(f"{p['ticker']}: no cost basis (give basis_overrides or allow_unknown_basis)")
                continue
            unit = 0.0
            unknown.append(p["ticker"])
        plan.append((p, sh, unit))
    return problems, unknown, plan


def switch_to_ledger(ctx: RequestContext, account_id: int, opening_date, *, basis_overrides: dict[str, float] | None = None,
                     allow_unknown_basis: bool = False, reason: str | None = None, conn: sqlite3.Connection | None = None) -> dict:
    """
    Make a snapshot account ledger-managed. What it holds today becomes its OPENING BALANCE as of `opening_date`: one OPENING_BALANCE row
    per position (shares at a unit cost = cost basis / shares) and one for cash. These are not trades; nothing is inferred about how the
    account got here. From then on its positions and cash are derived from the ledger and can no longer be typed in or imported over.

    A position whose cost basis is unknown is REFUSED, because a tax lot needs a unit cost: give `basis_overrides={"TICKER": unit_cost}`, or
    pass allow_unknown_basis=True to open those lots at a unit cost of 0 (recorded and returned as `unknown_basis`, so every later gain on them
    is overstated and says so). Short positions are refused. Transactions dated before `opening_date` that were stored as history stay in the
    ledger but are not replayed. Returns {"opening_transactions", "unknown_basis", "positions"}.
    """
    require_context(ctx)
    day = _date(opening_date, "opening_date")
    overrides = {k.strip().upper(): float(v) for k, v in (basis_overrides or {}).items()}
    sdb.get_portfolio_settings()                                           # schema touched before the write transaction opens
    with repo.unit_of_work(conn) as c:
        account = _owned_account(ctx, account_id, c)
        if _ledger_account(account):
            raise InvalidTransaction("this account is already managed by its ledger")
        problems, unknown, plan = _opening_plan(c, account_id, overrides, allow_unknown_basis)
        if problems:
            raise InvalidTransaction("cannot open the ledger: " + "; ".join(problems))
        cash = repo.get_account_cash(account_id, conn=c)
        opened = []
        for p, sh, unit in plan:
            opened.append(ldb.insert_transaction(c, {
                "account_id": account_id, "instrument_id": p["instrument_id"], "txn_type": "OPENING_BALANCE", "trade_date": day,
                "quantity": round(sh, QTY_PLACES), "price": unit, "amount": 0.0, "source": "opening_balance",
                "memo": "opening balance" + (" (cost basis unknown)" if p["ticker"] in unknown else ""), "created_by": ctx.actor,
                "request_id": ctx.request_id}))
        if cash and cash.get("amount") is not None:
            opened.append(ldb.insert_transaction(c, {
                "account_id": account_id, "txn_type": "OPENING_BALANCE", "trade_date": day, "amount": round(float(cash["amount"]), 2),
                "source": "opening_balance", "memo": "opening cash balance", "created_by": ctx.actor, "request_id": ctx.request_id}))
        repo.update_account(account_id, holdings_source=HoldingsSource.LEDGER.value, ledger_start_date=day, conn=c)
        account = repo.get_account(account_id, conn=c)
        state = replay(ldb.txns_for_replay(c, account_id))
        ldb.replace_derived(c, account_id, state)
        synced = _sync_positions(c, account, state)
        repo.record_audit_event(actor=ctx.actor, source=ctx.source, request_id=ctx.request_id, operation="switch_to_ledger",
                                portfolio_id=account["portfolio_id"], account_id=account_id, before={"holdings_source": "snapshot"},
                                after={"holdings_source": "ledger", "opening_date": day, "opening_transactions": len(opened),
                                       "unknown_basis": unknown, "positions": [p["ticker"] for p, _, _ in plan]}, reason=reason, conn=c)
        return {"opening_transactions": len(opened), "unknown_basis": unknown, "positions": synced}


def switch_to_snapshot(ctx: RequestContext, account_id: int, *, reason: str | None = None,
                       conn: sqlite3.Connection | None = None) -> dict:
    """
    Give an account back to manual / imported snapshots. Its positions and cash stay exactly as the ledger last derived them (that is the
    snapshot from here on, to be edited or imported over); its transactions stay in the ledger as history only and its tax lots are cleared
    (they only exist for ledger accounts). Switching to the ledger again later needs a new opening balance.
    """
    require_context(ctx)
    with repo.unit_of_work(conn) as c:
        account = _owned_account(ctx, account_id, c)
        if not _ledger_account(account):
            raise InvalidTransaction("this account is already a snapshot account")
        n = c.execute("SELECT COUNT(*) FROM tax_lots WHERE account_id = ?", [account_id]).fetchone()[0]
        c.execute("DELETE FROM lot_closures WHERE account_id = ?", [account_id])
        c.execute("DELETE FROM tax_lots WHERE account_id = ?", [account_id])
        repo.update_account(account_id, holdings_source=HoldingsSource.SNAPSHOT.value, ledger_start_date=None, conn=c)
        repo.record_audit_event(actor=ctx.actor, source=ctx.source, request_id=ctx.request_id, operation="switch_to_snapshot",
                                portfolio_id=account["portfolio_id"], account_id=account_id,
                                before={"holdings_source": "ledger", "ledger_start_date": account.get("ledger_start_date")},
                                after={"holdings_source": "snapshot", "lots_cleared": n}, reason=reason, conn=c)
        return repo.get_account(account_id, conn=c)


def reconcile_account(ctx: RequestContext, account_id: int, statement_positions: list[dict], statement_cash: float | None = None, *,
                      as_of=None, share_tolerance: float = 0.001, cash_tolerance: float = 0.01) -> dict:
    """
    Compare what the account's LEDGER says it holds (as of `as_of`, default now) with a broker statement: [{"ticker", "shares",
    optional "cost_basis_total"}] and optionally the statement's cash. READ-ONLY with respect to the ledger and positions: a difference is
    reported, never "fixed" by inventing a trade; the person decides what really happened and records it (or voids a wrong row).
    Each ticker is MATCH, DIFFERENT, ONLY_IN_LEDGER or ONLY_IN_STATEMENT; cash is MATCH or DIFFERENT. The result is also audited.
    """
    require_context(ctx)
    through = _date(as_of, "as_of") if as_of else None
    stmt: dict[str, dict] = {}
    for r in statement_positions or []:
        t = (r.get("ticker") or "").strip().upper()
        if not t:
            raise InvalidTransaction("every statement position needs a ticker")
        stmt[t] = {"shares": _num(r.get("shares", 0), f"{t} shares", positive=False), "basis": r.get("cost_basis_total")}
    with repo.unit_of_work() as c:
        account = _owned_account(ctx, account_id, c, allow_archived=True)
        try:
            state = replay(ldb.txns_for_replay(c, account_id, through=through))
        except LedgerError as exc:
            raise InvalidTransaction(f"the ledger does not replay: {exc}") from None
        tick = {r["instrument_id"]: r["ticker"] for r in c.execute("SELECT instrument_id, ticker FROM instruments").fetchall()}
        ledger = {tick[i]: p for i, p in state.positions().items()}
        rows, counts = [], {"MATCH": 0, "DIFFERENT": 0, "ONLY_IN_LEDGER": 0, "ONLY_IN_STATEMENT": 0}
        for t in sorted(set(ledger) | set(stmt)):
            ls = ledger.get(t, {}).get("quantity")
            ss = stmt.get(t, {}).get("shares")
            if ls is None:
                status = "ONLY_IN_STATEMENT" if (ss or 0) > share_tolerance else "MATCH"
            elif ss is None or (ss == 0 and t not in stmt):
                status = "ONLY_IN_LEDGER"
            else:
                status = "MATCH" if abs(ls - ss) <= share_tolerance else "DIFFERENT"
            counts[status] += 1
            row = {"ticker": t, "ledger_shares": ls, "statement_shares": ss, "difference": None if (ls is None or ss is None) else round(ss - ls, 8),
                   "status": status}
            if stmt.get(t, {}).get("basis") is not None and ledger.get(t):
                row["ledger_cost_basis"], row["statement_cost_basis"] = ledger[t]["cost_basis"], float(stmt[t]["basis"])
            rows.append(row)
        cash = None
        if statement_cash is not None:
            diff = round(float(statement_cash) - state.cash, 2)
            cash = {"ledger": state.cash, "statement": round(float(statement_cash), 2), "difference": diff,
                    "status": "MATCH" if abs(diff) <= cash_tolerance else "DIFFERENT"}
        out = {"account_id": account_id, "as_of": through, "holdings_source": account.get("holdings_source"),
               "matches": counts["MATCH"] == len(rows) and (cash is None or cash["status"] == "MATCH"),
               "positions": rows, "cash": cash, "summary": counts}
        repo.record_audit_event(actor=ctx.actor, source=ctx.source, request_id=ctx.request_id, operation="reconcile_ledger",
                                portfolio_id=account["portfolio_id"], account_id=account_id,
                                after={"matches": out["matches"], "summary": counts, "cash_status": cash and cash["status"], "as_of": through}, conn=c)
        return out


# ── CSV import ────────────────────────────────────────────────────────────────

class ImportRejected(ValueError):
    """The CSV import cannot go ahead (snapshot account, unreadable file, invalid rows ...). Nothing was written."""


def _plan_for(ctx: RequestContext, account_id: int, raw: bytes, mapping: dict, c: sqlite3.Connection):
    from portfolio_agent.tools import ledger_csv_import as csvi
    account = _owned_account(ctx, account_id, c)
    if not _ledger_account(account):
        raise ImportRejected("transactions can only be imported into a ledger account; this account's positions are a snapshot "
                             "(switch it to the ledger first, which records an opening balance)")
    try:
        headers, rows = csvi.read_csv(raw)
        csvi.validate_mapping(mapping, headers)
    except csvi.CsvImportError as exc:
        raise ImportRejected(str(exc)) from None
    ext, keys = ldb.existing_for_dedupe(c, account_id)
    return account, csvi.plan_rows(rows, mapping, existing_external_ids=ext, existing_keys=keys, today=_today())


def suggest_csv_mapping(raw: bytes) -> dict:
    """Step 1 of the import: the file's headers, a first-guess mapping of them to the ledger's fields, and the first rows. Touches no account."""
    from portfolio_agent.tools import ledger_csv_import as csvi
    try:
        headers, rows = csvi.read_csv(raw)
    except csvi.CsvImportError as exc:
        raise ImportRejected(str(exc)) from None
    return {"headers": headers, "mapping": csvi.suggest_mapping(headers), "sample": rows[:5], "rows": len(rows)}


def preview_transactions_csv(ctx: RequestContext, account_id: int, raw: bytes, mapping: dict) -> dict:
    """
    Step 2: what an import WOULD do, writing nothing. Every row is new / duplicate / invalid with its errors and warnings, and the new rows are
    dry-run through the ledger replay so a sale that does not fit the holdings is reported here, not after the fact. `can_import` is False
    while any row is invalid or the dry run fails.
    """
    require_context(ctx)
    from portfolio_agent.tools import ledger_csv_import as csvi
    with repo.unit_of_work() as c:
        account, plan = _plan_for(ctx, account_id, raw, mapping, c)
        problem = None
        if not any(p.status == "invalid" for p in plan):
            problem = _dry_run(c, account_id, csvi.import_order(plan))
    summ = csvi.summarize(plan)
    return {"account_id": account_id, "summary": summ, "can_import": summ["invalid"] == 0 and problem is None and summ["new"] > 0,
            "dry_run_error": problem,
            "rows": [{"line": p.line, "status": p.status, "type": p.txn_type, "trade_date": p.fields.get("trade_date"),
                      "ticker": p.fields.get("ticker"), "quantity": p.fields.get("quantity"), "cash_amount": p.cash_amount,
                      "duplicate_of": p.duplicate_of, "errors": p.errors, "warnings": p.warnings} for p in plan]}


def _dry_run(c: sqlite3.Connection, account_id: int, ordered) -> str | None:
    """Replay the account's ledger plus the planned rows, in memory. Returns the first problem or None."""
    from portfolio_agent.tools.ledger_engine import Txn
    base = ldb.txns_for_replay(c, account_id)
    nxt = max((t.txn_id for t in base), default=0) + 1
    extra = []
    inst = {r["ticker"]: r["instrument_id"] for r in c.execute("SELECT ticker, instrument_id FROM instruments").fetchall()}
    for i, p in enumerate(ordered):
        f = p.fields
        iid = inst.get(f.get("ticker")) or (-1 if f.get("ticker") else None)           # an unseen ticker: a placeholder id is enough for a replay
        amount = p.cash_amount or 0.0
        extra.append(Txn(txn_id=nxt + i, account_id=account_id, txn_type=p.txn_type, trade_date=f["trade_date"], instrument_id=iid,
                         quantity=f.get("quantity"), price=f.get("price"), fees=f.get("fees") or 0.0, amount=amount,
                         lot_method="FIFO" if p.txn_type == "SELL" else None))
    try:
        replay(base + extra)
    except LedgerError as exc:
        return str(exc)
    return None


def import_transactions_csv(ctx: RequestContext, account_id: int, raw: bytes, mapping: dict, *, filename: str | None = None,
                            reason: str | None = None) -> dict:
    """
    Step 3: apply the import, ALL OR NOTHING in one database transaction. Refused (nothing written) for a snapshot account, an unreadable
    file, any invalid row, or a set of rows that does not replay (a sale of shares not held). Duplicates are skipped and counted. Each
    imported row goes through the same verb a hand-entered one does (validation, lot rebuild, pre-clearance check, audit), is tagged with the
    import run (import_runs, source 'csv_transactions') and its optional contribution_source / external_ref. COMPLIANCE notifications for
    trades without clearance are enqueued after the commit. Returns {"run_id", "imported", "skipped", "missing_clearance", "warnings"}.
    """
    require_context(ctx)
    from portfolio_agent.tools import ledger_csv_import as csvi
    sdb.get_portfolio_settings()
    # Read-only first pass (its own short transaction): validate and plan, then look up every trade's pre-clearance BEFORE the write transaction
    # opens (the approvals / profile tables run schema maintenance on each connection and would wait on a write lock held by this import).
    with repo.unit_of_work() as c0:
        _, plan0 = _plan_for(ctx, account_id, raw, mapping, c0)
    clearance = {(p.fields["ticker"], p.txn_type, p.fields["trade_date"]):
                 _clearance_lookup(ctx.actor, p.fields["ticker"], p.txn_type, p.fields["trade_date"])
                 for p in plan0 if p.status == "new" and p.txn_type in ("BUY", "SELL")}
    _clearance_lookup(ctx.actor, "WARMUP", "BUY", _today().isoformat())      # make sure the profile / approvals schemas exist
    run_id, imported, notify_txns = None, 0, []
    with repo.unit_of_work() as c:
        account, plan = _plan_for(ctx, account_id, raw, mapping, c)
        bad = [p for p in plan if p.status == "invalid"]
        if bad:
            raise ImportRejected(f"{len(bad)} row(s) are invalid, so nothing was imported: " +
                                 "; ".join(f"line {p.line}: {'; '.join(p.errors)}" for p in bad[:5]) + (" ..." if len(bad) > 5 else ""))
        ordered = csvi.import_order(plan)
        problem = _dry_run(c, account_id, ordered)
        if problem:
            raise ImportRejected(f"the rows do not fit this account's ledger, so nothing was imported: {problem}")
        skipped = sum(p.status == "duplicate" for p in plan)
        run_id = repo.record_import_run(broker="csv_transactions", accounts=[account.get("display_name") or str(account_id)], filename=filename,
                                        statement_date=max((p.fields["trade_date"] for p in ordered), default=None), conn=c)
        for p in ordered:
            res = _apply_row(ctx, c, account_id, p, run_id, clearance)
            imported += 0 if res["duplicate"] else 1
            if res.get("notify_pending"):
                notify_txns.append(res["txn"])
        repo.update_import_run(run_id, conn=c, added=imported, updated=0, removed=0, skipped=skipped)
        repo.record_audit_event(actor=ctx.actor, source=ctx.source, request_id=ctx.request_id, operation="import_transactions_csv",
                                portfolio_id=account["portfolio_id"], account_id=account_id,
                                after={"run_id": run_id, "filename": filename, "imported": imported, "skipped": skipped,
                                       "missing_clearance": len(notify_txns)}, reason=reason, conn=c)
    for t in notify_txns:                                  # after the commit: notifications never run inside the import's transaction
        notify_clearance_missing(ctx.actor, t)
    return {"run_id": run_id, "imported": imported, "skipped": skipped, "missing_clearance": len(notify_txns),
            "warnings": [{"line": p.line, "warnings": p.warnings} for p in plan if p.warnings]}


def _apply_row(ctx, c, account_id: int, p, run_id: int, clearance: dict) -> dict:
    f, t = p.fields, p.txn_type
    common = dict(external_id=f.get("external_ref"), import_run_id=run_id, conn=c)
    day = f["trade_date"]
    if t in ("BUY", "SELL"):
        return record_trade(ctx, account_id, f["ticker"], t, f["quantity"], f["price"], day, fees=f.get("fees") or 0.0,
                            clearance=clearance.get((f["ticker"], t, day)) or ("unknown", None), **common)
    if t in ("DEPOSIT", "WITHDRAWAL"):
        return record_cash_transfer(ctx, account_id, t, f["amount"], day, contribution_source=f.get("contribution_source"), **common)
    if t in ("DIVIDEND", "INTEREST"):
        return record_income(ctx, account_id, t, f["amount"], day, ticker=f.get("ticker"), **common)
    if t == "FEE":
        return record_fee(ctx, account_id, f["amount"], day, ticker=f.get("ticker"), **common)
    if t == "EMPLOYER_MATCH":
        if f.get("quantity"):
            return record_employer_match(ctx, account_id, day, ticker=f["ticker"], quantity=f["quantity"], price=f["price"],
                                         contribution_source=f.get("contribution_source"), **common)
        return record_employer_match(ctx, account_id, day, amount=f["amount"], contribution_source=f.get("contribution_source"), **common)
    raise ImportRejected(f"line {p.line}: type {t} cannot be imported")


# ── CSV export ────────────────────────────────────────────────────────────────

def _cell(v):
    """A CSV cell: None -> empty; text that a spreadsheet would run as a formula (= + - @) is prefixed with an apostrophe."""
    if v is None:
        return ""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def _csv(header: list[str], rows: list[list]) -> str:
    import csv as _csv_mod
    import io
    buf = io.StringIO()
    w = _csv_mod.writer(buf, lineterminator="\n")
    w.writerow(header)
    for r in rows:
        w.writerow([_cell(v) for v in r])
    return buf.getvalue()


def export_transactions_csv(ctx: RequestContext, account_id: int) -> str:
    """Every transaction of one account (voided ones and the VOID rows included, marked), for compliance. Oldest first."""
    require_context(ctx)
    rows = list_transactions(ctx, account_id, include_voided=True)
    head = ["txn_id", "trade_date", "settle_date", "type", "ticker", "quantity", "price", "fees", "amount", "lot_method", "contribution_source",
            "clearance_status", "clearance_approval_id", "external_ref", "voided_by", "voids_txn_id", "memo", "source", "created_by", "created_at"]
    return _csv(head, [[r["txn_id"], r["trade_date"], r["settle_date"], r["txn_type"], r["ticker"], r["quantity"], r["price"], r["fees"],
                        r["amount"], r["lot_method"], r["contribution_source"], r["clearance_status"], r["clearance_approval_id"],
                        r["external_id"], r["voided_by"], r["voids_txn_id"], r["memo"], r["source"], r["created_by"], r["created_at"]]
                       for r in rows])


def export_realized_gains_csv(ctx: RequestContext, account_id: int, *, year: int | None = None) -> str:
    """Realized gains by lot closure (for taxes): ticker, quantity, open / close dates, proceeds, cost basis, gain and SHORT / LONG, optionally for
    one calendar year of the closing date. Empty (header only) for an account with no sales or a snapshot account (it has no lots)."""
    require_context(ctx)
    closures = get_closures(ctx, account_id)
    if year is not None:
        closures = [c for c in closures if str(c["close_date"]).startswith(str(int(year)))]
    head = ["ticker", "quantity", "open_date", "close_date", "proceeds", "cost_basis", "realized_gain", "holding_period", "lot_method",
            "open_transaction_id", "close_transaction_id"]
    return _csv(head, [[c["ticker"], c["quantity"], c["open_date"], c["close_date"], c["proceeds"], c["cost_basis"], c["realized_gain"],
                        c["holding_period"], c["method"], c["open_transaction_id"], c["close_transaction_id"]] for c in closures])


# ── read helpers for the Accounts page ────────────────────────────────────────

def get_account_ledger_info(ctx: RequestContext, account_id: int) -> dict:
    """One account's ledger-relevant facts for the page (owner-checked): source, lot method (and the default it falls back on), start date,
    paper flag, transaction counts, and the clearance counts of its effective trades."""
    require_context(ctx)
    default = sdb.get_portfolio_settings().get("default_lot_method") or "FIFO"
    with repo.unit_of_work() as c:
        a = _owned_account(ctx, account_id, c, allow_archived=True)
        rows = ldb.list_transactions(c, account_id)
        counts = {"missing": 0, "unknown": 0, "approved": 0, "not_required": 0}
        for r in rows:
            if r["txn_type"] in ("BUY", "SELL") and r["voided_by"] is None and r["clearance_status"] in counts:
                counts[r["clearance_status"]] += 1
    return {"account_id": account_id, "name": a.get("display_name") or a.get("account_number") or "Unnamed account", "status": a.get("status"),
            "holdings_source": a.get("holdings_source") or "snapshot", "is_ledger": a.get("holdings_source") == "ledger",
            "is_paper": bool(a.get("is_paper")), "lot_method": a.get("lot_method"), "default_lot_method": default,
            "effective_lot_method": a.get("lot_method") or default, "ledger_start_date": a.get("ledger_start_date"),
            "transactions": sum(1 for r in rows if r["txn_type"] != "VOID"), "voided": sum(1 for r in rows if r["voided_by"] is not None),
            "clearance": counts}


def preview_switch_to_ledger(ctx: RequestContext, account_id: int, opening_date, *, basis_overrides: dict[str, float] | None = None,
                             allow_unknown_basis: bool = False) -> dict:
    """What switching a snapshot account to the ledger would record as its opening balance, WITHOUT writing anything: each position's shares,
    unit cost and total basis (or why it cannot be opened), the opening cash, and whether the switch can go ahead."""
    require_context(ctx)
    day = _date(opening_date, "opening_date")
    overrides = {k.strip().upper(): float(v) for k, v in (basis_overrides or {}).items()}
    with repo.unit_of_work() as c:
        account = _owned_account(ctx, account_id, c)
        if _ledger_account(account):
            raise InvalidTransaction("this account is already managed by its ledger")
        problems, unknown, plan = _opening_plan(c, account_id, overrides, allow_unknown_basis)
        cash = repo.get_account_cash(account_id, conn=c)
        needs_basis = [p.split(":")[0] for p in problems if "no cost basis" in p]
        positions = [{"ticker": p["ticker"], "shares": sh, "unit_cost": unit, "cost_basis": round(sh * unit, 2),
                      "unknown_basis": p["ticker"] in unknown} for p, sh, unit in plan]
    return {"opening_date": day, "positions": positions, "cash": None if not cash or cash.get("amount") is None else round(float(cash["amount"]), 2),
            "unknown_basis": unknown, "needs_basis": needs_basis, "problems": problems, "can_switch": not problems}
