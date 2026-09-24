"""
Account / position service — the single authority for account identity and
position mutations. Streamlit pages, chat tools, batch jobs and the import
workflow all go through here, so they cannot apply different rules to the
same account.

Every operation
  * takes a RequestContext and checks the actor owns the portfolio the
    account/position actually belongs to (never a role the UI supplies);
  * runs in ONE transaction with an audit event (actor, operation, ids,
    before/after, reason, request id) — pass ``conn`` to compose several
    operations into a larger transaction (the import does this);
  * is a distinct verb. "Set quantity to 20" is a holdings CORRECTION, not a
    trade; trades and cash transfers will be separate commands
    (record_trade / record_cash_transfer) when transaction accounting lands
    and are never inferred from differences between two snapshots.

Operations
  create_account, rename_account, archive_account, restore_account,
  delete_account_permanently                         — account lifecycle
  add_position, set_position, remove_position         — one position
  record_account_cash                                 — an account's cash line
  replace_account_snapshot                            — internal: after a
      validated import, make the account's positions equal the file's
      (updates rows in place so position ids survive re-imports)
  change_instrument_ticker                            — a security's ticker
      changed; its instrument_id, positions and history stay the same
"""

from __future__ import annotations

import sqlite3
from datetime import date

from portfolio_agent.domain import LOCAL_OWNER
from portfolio_agent.services.context import RequestContext, require_context
from portfolio_agent.tools import holdings_db as repo


# ── Errors ────────────────────────────────────────────────────────────────────

class NotAuthorized(PermissionError):
    """The actor does not own the portfolio this account/position belongs to."""


class NotFound(LookupError):
    """No such (active) account or position."""


class VersionConflict(RuntimeError):
    """The record changed since the caller loaded it; reload and retry."""


class DuplicateAccount(ValueError):
    """An account with this broker + account number already exists in the portfolio."""


class DuplicatePosition(ValueError):
    """The account already holds this ticker (use on_duplicate='update' or set_position)."""


class DuplicateInstrument(ValueError):
    """Another instrument already carries that ticker (merging instruments is a separate, manual step)."""


# ── Authorization ─────────────────────────────────────────────────────────────

def resolve_portfolio(ctx: RequestContext, *, conn: sqlite3.Connection | None = None) -> dict:
    """The portfolio the actor may act on. The local owner's is created on first use."""
    require_context(ctx)
    p = repo.get_portfolio_by_owner(ctx.actor, conn=conn)
    if p is None:
        raise NotAuthorized(f"{ctx.actor!r} has no portfolio here")
    return p


def _owned_account(ctx: RequestContext, account_id: int, conn: sqlite3.Connection, *,
                   allow_archived: bool = False) -> dict:
    acct = repo.get_account(account_id, conn=conn)
    if acct is None:
        raise NotFound(f"account {account_id} does not exist")
    if acct["portfolio_id"] != resolve_portfolio(ctx, conn=conn)["portfolio_id"]:
        raise NotAuthorized(f"{ctx.actor!r} may not act on account {account_id}")
    if acct["status"] != repo.ACCOUNT_ACTIVE and not allow_archived:
        raise NotFound(f"account {account_id} is archived")
    return acct


def _owned_position(ctx: RequestContext, position_id: int, conn: sqlite3.Connection) -> tuple[dict, dict]:
    pos = repo.get_position(position_id, conn=conn)
    if pos is None:
        raise NotFound(f"position {position_id} does not exist")
    return pos, _owned_account(ctx, pos["account_id"], conn)


def _audit(ctx: RequestContext, conn: sqlite3.Connection, operation: str, *, portfolio_id: int | None = None,
           account_id: int | None = None, position_id: int | None = None, before: dict | None = None,
           after: dict | None = None, reason: str | None = None) -> None:
    repo.record_audit_event(actor=ctx.actor, source=ctx.source, request_id=ctx.request_id, operation=operation,
                            portfolio_id=portfolio_id, account_id=account_id, position_id=position_id,
                            before=before, after=after, reason=reason, conn=conn)


def _account_snapshot(a: dict) -> dict:
    return {k: a.get(k) for k in ("display_name", "account_type", "status", "version")}


def _position_snapshot(p: dict) -> dict:
    return {k: p.get(k) for k in ("ticker", "shares", "avg_cost", "cost_basis_total", "current_price",
                                  "current_value", "as_of_date", "price_as_of", "version")}


# ── Accounts ──────────────────────────────────────────────────────────────────

def find_account(ctx: RequestContext, broker: str, account_number: str | None, *,
                 conn: sqlite3.Connection | None = None) -> dict | None:
    """The actor's account with this external reference (any status), or None."""
    with repo.unit_of_work(conn) as c:
        pid = resolve_portfolio(ctx, conn=c)["portfolio_id"]
        return repo.find_account(pid, _norm_broker(broker), account_number or "", conn=c)


def _norm_broker(broker: str | None) -> str:
    return (broker or "").strip().lower() or "unknown"


def create_account(ctx: RequestContext, *, broker: str, display_name: str | None = None,
                   account_number: str | None = None, account_type: str | None = None,
                   reason: str | None = None, conn: sqlite3.Connection | None = None) -> dict:
    """
    Create an account under the actor's portfolio. An account with no broker
    number (manual entry, unnamed groups in a file) is keyed by its nickname,
    so two differently named manual accounts never collapse into one bucket.
    """
    broker = _norm_broker(broker)
    display_name = (display_name or "").strip() or None
    account_number = (account_number or "").strip() or (display_name or "")
    if not account_number:
        raise ValueError("create_account: an account needs an account number or a name")
    with repo.unit_of_work(conn) as c:
        pid = resolve_portfolio(ctx, conn=c)["portfolio_id"]
        existing = repo.find_account(pid, broker, account_number, conn=c)
        if existing is not None:
            state = "is archived — restore it" if existing["status"] != repo.ACCOUNT_ACTIVE else "already exists"
            raise DuplicateAccount(f"{broker} account {account_number!r} {state}")
        acct = repo.insert_account(pid, broker, account_number, display_name, account_type, conn=c)
        _audit(ctx, c, "create_account", portfolio_id=pid, account_id=acct["account_id"],
               after=_account_snapshot(acct) | {"broker": broker, "account_number": account_number}, reason=reason)
        return acct


def rename_account(ctx: RequestContext, account_id: int, name: str, *, expected_version: int | None = None,
                   reason: str | None = None, conn: sqlite3.Connection | None = None) -> dict:
    """Change the display name. Identity (broker + account number, ids, history) is unchanged."""
    name = (name or "").strip()
    if not name:
        raise ValueError("rename_account: name cannot be blank")
    with repo.unit_of_work(conn) as c:
        acct = _owned_account(ctx, account_id, c)
        _check_version(acct, expected_version, "account")
        repo.update_account(account_id, display_name=name, conn=c)
        after = repo.get_account(account_id, conn=c)
        _audit(ctx, c, "rename_account", portfolio_id=acct["portfolio_id"], account_id=account_id,
               before=_account_snapshot(acct), after=_account_snapshot(after), reason=reason)
        return after


def archive_account(ctx: RequestContext, account_id: int, *, reason: str | None = None,
                    conn: sqlite3.Connection | None = None) -> dict:
    """Take an account out of the active view. Positions, cash and history are all kept."""
    return _set_status(ctx, account_id, repo.ACCOUNT_ARCHIVED, "archive_account", reason, conn)


def restore_account(ctx: RequestContext, account_id: int, *, reason: str | None = None,
                    conn: sqlite3.Connection | None = None) -> dict:
    return _set_status(ctx, account_id, repo.ACCOUNT_ACTIVE, "restore_account", reason, conn)


def _set_status(ctx, account_id, status, operation, reason, conn) -> dict:
    with repo.unit_of_work(conn) as c:
        acct = _owned_account(ctx, account_id, c, allow_archived=True)
        if acct["status"] != status:
            repo.update_account(account_id, status=status, conn=c)
        after = repo.get_account(account_id, conn=c)
        _audit(ctx, c, operation, portfolio_id=acct["portfolio_id"], account_id=account_id,
               before=_account_snapshot(acct), after=_account_snapshot(after), reason=reason)
        return after


def delete_account_permanently(ctx: RequestContext, account_id: int, *, reason: str | None = None,
                               conn: sqlite3.Connection | None = None) -> dict:
    """
    Hard-delete an account with its positions and cash — distinct from
    archiving. Daily price history stays (it is keyed by the external account
    reference) so past portfolio values are not rewritten.
    """
    with repo.unit_of_work(conn) as c:
        acct = _owned_account(ctx, account_id, c, allow_archived=True)
        positions = repo.list_positions(account_id, conn=c)
        cash = repo.get_account_cash(account_id, conn=c)
        n = repo.delete_account(account_id, conn=c)
        _audit(ctx, c, "delete_account", portfolio_id=acct["portfolio_id"], account_id=account_id,
               before=_account_snapshot(acct) | {"broker": acct["broker"], "account_number": acct["account_number"],
                                                 "positions": [_position_snapshot(p) for p in positions],
                                                 "cash": cash and cash.get("amount")},
               reason=reason)
        return {"positions": n, "account": acct}


def _check_version(record: dict, expected: int | None, what: str) -> None:
    if expected is not None and record.get("version") != expected:
        raise VersionConflict(f"this {what} changed since you loaded it "
                              f"(you had v{expected}, it is now v{record.get('version')}); reload and retry")


# ── Positions ─────────────────────────────────────────────────────────────────

def add_position(ctx: RequestContext, account_id: int, position: dict, *, on_duplicate: str = "reject",
                 as_of_date: str | None = None, reason: str | None = None,
                 conn: sqlite3.Connection | None = None) -> dict:
    """
    Add a NEW position to an account. If the account already holds the
    ticker, reject (default) or — only when the caller explicitly asks with
    on_duplicate="update" — correct the existing position's quantity and cost
    instead. Never silently doubles up.
    """
    if on_duplicate not in ("reject", "update"):
        raise ValueError("add_position: on_duplicate must be 'reject' or 'update'")
    fields = repo.position_fields(position, as_of_date or date.today().isoformat())
    if not fields["ticker"]:
        raise ValueError("add_position: ticker is required")
    with repo.unit_of_work(conn) as c:
        acct = _owned_account(ctx, account_id, c)
        existing = repo.find_position(account_id, fields["ticker"], conn=c)
        if existing is not None:
            if on_duplicate == "reject":
                raise DuplicatePosition(f"{fields['ticker']} is already in this account (position {existing['position_id']})")
            return set_position(ctx, existing["position_id"], expected_version=existing["version"],
                                shares=fields["shares"], avg_cost=fields["avg_cost"],
                                cost_basis_total=fields["cost_basis_total"], reason=reason, conn=c)
        pos = repo.insert_position(account_id, fields, conn=c)
        _audit(ctx, c, "add_position", portfolio_id=acct["portfolio_id"], account_id=account_id,
               position_id=pos["position_id"], after=_position_snapshot(pos), reason=reason)
        return pos


def set_position(ctx: RequestContext, position_id: int, *, expected_version: int | None, shares: float | None,
                 avg_cost: float | None, cost_basis_total: float | None = None, reason: str | None = None,
                 conn: sqlite3.Connection | None = None) -> dict:
    """
    Correct the recorded quantity / cost basis of ONE position. This is a
    holdings correction, not a trade. expected_version must match the row the
    caller loaded (pass None only for callers that just read it in this
    transaction).
    """
    if cost_basis_total is None and shares and avg_cost:
        cost_basis_total = round(shares * avg_cost, 2)
    with repo.unit_of_work(conn) as c:
        pos, acct = _owned_position(ctx, position_id, c)
        _check_version(pos, expected_version, "position")
        price = pos.get("current_price")
        repo.update_position(position_id, conn=c, shares=shares, avg_cost=avg_cost, cost_basis_total=cost_basis_total,
                             current_value=round(price * shares, 2) if (price and shares) else None)
        after = repo.get_position(position_id, conn=c)
        _audit(ctx, c, "set_position", portfolio_id=acct["portfolio_id"], account_id=pos["account_id"],
               position_id=position_id, before=_position_snapshot(pos), after=_position_snapshot(after), reason=reason)
        return after


def remove_position(ctx: RequestContext, position_id: int, *, expected_version: int | None = None,
                    reason: str | None = None, conn: sqlite3.Connection | None = None) -> dict:
    """Remove exactly one position (e.g. it was entered by mistake). Not a sale."""
    with repo.unit_of_work(conn) as c:
        pos, acct = _owned_position(ctx, position_id, c)
        _check_version(pos, expected_version, "position")
        repo.delete_position(position_id, conn=c)
        _audit(ctx, c, "remove_position", portfolio_id=acct["portfolio_id"], account_id=pos["account_id"],
               position_id=position_id, before=_position_snapshot(pos), reason=reason)
        return pos


# ── Cash ──────────────────────────────────────────────────────────────────────

def record_account_cash(ctx: RequestContext, account_id: int, amount: float | None, *, as_of_date: str,
                        detail: str | None = None, reason: str | None = None,
                        conn: sqlite3.Connection | None = None) -> None:
    """Record (amount) or forget (None) the cash line of one account."""
    with repo.unit_of_work(conn) as c:
        acct = _owned_account(ctx, account_id, c)
        before = repo.get_account_cash(account_id, conn=c)
        if amount is None:
            repo.clear_account_cash(account_id, conn=c)
        else:
            repo.set_account_cash(account_id, round(amount, 2), as_of_date, detail, conn=c)
        if (before or {}).get("amount") != (None if amount is None else round(amount, 2)):
            _audit(ctx, c, "record_account_cash", portfolio_id=acct["portfolio_id"], account_id=account_id,
                   before={"amount": before and before.get("amount")},
                   after={"amount": None if amount is None else round(amount, 2), "as_of_date": as_of_date},
                   reason=reason)


# ── Import support ────────────────────────────────────────────────────────────

def find_or_create_import_account(ctx: RequestContext, *, broker: str, account_number: str, display_name: str,
                                  account_type: str | None, conn: sqlite3.Connection) -> dict:
    """The account a broker file's rows belong to; created if new, restored if it had been archived."""
    acct = find_account(ctx, broker, account_number, conn=conn)
    if acct is None:
        return create_account(ctx, broker=broker, account_number=account_number, display_name=display_name,
                              account_type=account_type, reason="import", conn=conn)
    if acct["status"] != repo.ACCOUNT_ACTIVE:
        acct = restore_account(ctx, acct["account_id"], reason="import", conn=conn)
    return acct


def _changed(old: dict, new: dict) -> bool:
    return (round(old.get("shares") or 0, 6) != round(new.get("shares") or 0, 6)
            or round(old.get("cost_basis_total") or 0, 2) != round(new.get("cost_basis_total") or 0, 2))


def replace_account_snapshot(ctx: RequestContext, account_id: int, rows: list[dict], *, as_of_date: str,
                             source_import_id: int | None = None, conn: sqlite3.Connection) -> dict:
    """
    Make the account's positions equal *rows* — the complete, authoritative
    snapshot a broker file gives for that account. Existing tickers are
    updated IN PLACE (position ids and audit trails survive), new tickers are
    inserted, tickers absent from the file are removed. Only ever called for
    an account the user explicitly selected in a validated import plan; never
    a general-purpose upsert.
    """
    acct = _owned_account(ctx, account_id, conn)
    existing = {p["ticker"]: p for p in repo.list_positions(account_id, conn=conn)}
    seen: set[str] = set()
    added: list[str] = []; updated: list[str] = []; unchanged: list[str] = []
    for r in rows:
        f = repo.position_fields(r, as_of_date)
        t = f["ticker"]
        if not t or t in seen:
            continue
        seen.add(t)
        if t in existing:
            (updated if _changed(existing[t], f) else unchanged).append(t)
            repo.update_position(existing[t]["position_id"], conn=conn, source_import_id=source_import_id, **f)
        else:
            repo.insert_position(account_id, f, source_import_id=source_import_id, conn=conn)
            added.append(t)
    removed = sorted(set(existing) - seen)
    for t in removed:
        repo.delete_position(existing[t]["position_id"], conn=conn)
    _audit(ctx, conn, "import_snapshot", portfolio_id=acct["portfolio_id"], account_id=account_id,
           before={"positions": len(existing)},
           after={"positions": len(seen), "added": sorted(added), "updated": sorted(updated), "removed": removed,
                  "source_import_id": source_import_id, "as_of_date": as_of_date})
    return {"added": len(added), "updated": len(updated), "removed": len(removed),
            "unchanged": len(unchanged), "positions": len(seen)}


# ── Instruments ───────────────────────────────────────────────────────────────

def change_instrument_ticker(ctx: RequestContext, instrument_id: int, new_ticker: str, *,
                             expected_version: int | None = None, reason: str | None = None,
                             conn: sqlite3.Connection | None = None) -> dict:
    """
    Record that a security now trades under a new symbol. The instrument, its
    positions (in every account) and its audit trail keep their ids; the
    `holdings` view shows the new ticker immediately, and the instrument's
    daily history rows are relabelled so the portfolio time series stays
    continuous. Nothing is bought or sold.
    """
    new = repo._norm_ticker(new_ticker)
    if not new:
        raise ValueError("change_instrument_ticker: ticker cannot be blank")
    with repo.unit_of_work(conn) as c:
        portfolio = resolve_portfolio(ctx, conn=c)            # instruments are shared; acting needs a portfolio
        inst = repo.get_instrument(instrument_id, conn=c)
        if inst is None:
            raise NotFound(f"instrument {instrument_id} does not exist")
        _check_version(inst, expected_version, "instrument")
        if inst["ticker"] == new:
            return inst
        clash = repo.find_instrument(new, conn=c)
        if clash is not None:
            raise DuplicateInstrument(f"{new} already belongs to instrument {clash['instrument_id']}")
        repo.update_instrument(instrument_id, ticker=new, conn=c)
        repo.record_ticker_change(instrument_id, inst["ticker"], new, conn=c)
        history = repo.relabel_price_history_ticker(inst["ticker"], new, conn=c)
        after = repo.get_instrument(instrument_id, conn=c)
        _audit(ctx, c, "change_ticker", portfolio_id=portfolio["portfolio_id"],
               before={"ticker": inst["ticker"], "version": inst["version"]},
               after={"ticker": new, "version": after["version"], "instrument_id": instrument_id,
                      "history_rows_moved": history["moved"], "history_rows_skipped": history["skipped"]},
               reason=reason)
        return after


# ── Authorized reads ──────────────────────────────────────────────────────────
#
# Every read below checks resolve_portfolio(ctx) FIRST — before touching a
# single holdings/account/cash/import/price row — so an unauthorized or
# unverified caller never reaches private data, not even to get an empty
# result back quietly. holdings_db's own read functions (get_holdings,
# list_accounts, get_cash_balances, get_import_runs, get_price_history) stay
# as they are: unscoped, single-portfolio, still what most of the web app
# calls directly today. These are the authorized alternative going forward —
# see the Phase 2 report's inventory of callers still on the unscoped path.

def list_holdings_for(ctx: RequestContext) -> list:
    resolve_portfolio(ctx)
    return repo.get_holdings()


def list_accounts_for(ctx: RequestContext, *, include_archived: bool = False) -> list[dict]:
    portfolio = resolve_portfolio(ctx)
    return repo.list_accounts(portfolio["portfolio_id"], include_archived=include_archived)


def get_cash_balances_for(ctx: RequestContext) -> list[dict]:
    resolve_portfolio(ctx)
    return repo.get_cash_balances()


def list_import_runs_for(ctx: RequestContext, *, limit: int = 25) -> list[dict]:
    resolve_portfolio(ctx)
    return repo.get_import_runs(limit=limit)


def get_price_history_for(ctx: RequestContext, *, tickers: list[str] | None = None,
                          brokers: list[str] | None = None) -> list[dict]:
    resolve_portfolio(ctx)
    return repo.get_price_history(tickers=tickers, brokers=brokers)
