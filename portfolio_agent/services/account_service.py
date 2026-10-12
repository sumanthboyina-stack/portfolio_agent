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
  set_account_type                                    — an account's normalized type
  set_instrument_classification                       — a person's manual override of what a security is
      (instrument type, asset class, leverage, proxy); automatic classification never overwrites it
  apply_auto_classification                           — store the classifier's result (skips manual ones)
  create_synthetic_instrument                         — a ticker-less fund (401k CIT), keyed "CIT:<slug>"
  set_position_price                                  — a hand-entered price (the only price source for a CIT)
"""

from __future__ import annotations

import sqlite3
from datetime import date

from datetime import datetime, timezone

from portfolio_agent.domain import (
    ACCOUNT_TYPES, ASSET_CLASSES, INSTRUMENT_TYPES, LOCAL_OWNER, AssetClass, ClassificationSource, InstrumentType,
    is_synthetic_ticker, normalize_account_type, synthetic_cit_ticker,
)
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


class LedgerManaged(ValueError):
    """The account's positions and cash are derived from its transaction ledger, so they cannot be typed in, imported over or removed by hand.
    Record the trade (services.ledger_service) or compare a statement with reconcile_account instead."""


def _require_snapshot(acct: dict, what: str) -> None:
    if acct.get("holdings_source") == "ledger":
        raise LedgerManaged(f"{what}: this account is managed by its transaction ledger, so its positions and cash are derived from it. "
                            "Record a transaction, or compare a broker statement with reconcile_account; to type positions by hand, "
                            "switch the account back to a snapshot first.")


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
    account_type = normalize_account_type(account_type, display_name or "", default=None) if account_type else None
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

    A new security is classified (stock / ETF / fund / ...) right after the transaction commits, best effort:
    the lookup is a network call, so it never runs inside the transaction. When the caller passes ``conn`` the
    transaction is theirs and so is that follow-up — call instrument_classifier.classify_new_holdings afterwards.
    """
    result = _add_position(ctx, account_id, position, on_duplicate=on_duplicate, as_of_date=as_of_date,
                           reason=reason, conn=conn)
    if conn is None and not is_synthetic_ticker(result.get("ticker")):
        from portfolio_agent.services.instrument_classifier import classify_new_holdings
        classify_new_holdings(ctx, [result["ticker"]])
    return result


def _add_position(ctx: RequestContext, account_id: int, position: dict, *, on_duplicate: str,
                  as_of_date: str | None, reason: str | None, conn: sqlite3.Connection | None) -> dict:
    if on_duplicate not in ("reject", "update"):
        raise ValueError("add_position: on_duplicate must be 'reject' or 'update'")
    fields = repo.position_fields(position, as_of_date or date.today().isoformat())
    if not fields["ticker"]:
        raise ValueError("add_position: ticker is required")
    with repo.unit_of_work(conn) as c:
        acct = _owned_account(ctx, account_id, c)
        _require_snapshot(acct, "add_position")
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
        _require_snapshot(acct, "set_position")
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
        _require_snapshot(acct, "remove_position")
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
        _require_snapshot(acct, "record_account_cash")
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
    _require_snapshot(acct, "import")
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


# ── Taxonomy: account type, instrument classification, ticker-less funds, manual prices ──

_UNSET = object()          # "leave this field alone" (None is a real value for several of them)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_CLASSIFICATION_KEYS = ("instrument_type", "asset_class", "sub_asset_class", "leverage_factor", "proxy_ticker",
                        "expense_ratio", "fund_family", "category", "classification_source", "version")


def _classification_snapshot(i: dict) -> dict:
    return {k: i.get(k) for k in _CLASSIFICATION_KEYS}


def ensure_instruments(ctx: RequestContext, tickers: list[str], *, conn: sqlite3.Connection | None = None) -> list[str]:
    """
    Make sure each ticker has an instrument row (created UNKNOWN, to be classified). Used by the pipeline for
    watchlist / trending tickers nobody holds, so every ticker it analyses has a type. Synthetic and blank keys
    are ignored. One audit event lists what was created; returns the created tickers (usually empty).
    """
    wanted = sorted({repo._norm_ticker(t) for t in tickers if t and not is_synthetic_ticker(t)})
    with repo.unit_of_work(conn) as c:
        portfolio = resolve_portfolio(ctx, conn=c)
        created = [t for t in wanted if repo.find_instrument(t, conn=c) is None]
        for t in created:
            repo.get_or_create_instrument(t, conn=c)
        if created:
            _audit(ctx, c, "create_instruments", portfolio_id=portfolio["portfolio_id"],
                   after={"tickers": created}, reason="seen by the pipeline")
        return created


def set_account_type(ctx: RequestContext, account_id: int, account_type: str, *, expected_version: int | None = None,
                     reason: str | None = None, conn: sqlite3.Connection | None = None) -> dict:
    """Set an account's type to one of domain.ACCOUNT_TYPES (TAXABLE, TRADITIONAL_IRA, ROTH_IRA, 401K, ...)."""
    if account_type not in ACCOUNT_TYPES:
        raise ValueError(f"set_account_type: {account_type!r} is not one of {list(ACCOUNT_TYPES)}")
    with repo.unit_of_work(conn) as c:
        acct = _owned_account(ctx, account_id, c)
        _check_version(acct, expected_version, "account")
        repo.update_account(account_id, account_type=str(account_type), conn=c)
        after = repo.get_account(account_id, conn=c)
        _audit(ctx, c, "set_account_type", portfolio_id=acct["portfolio_id"], account_id=account_id,
               before=_account_snapshot(acct), after=_account_snapshot(after), reason=reason)
        return after


def _owned_instrument(ctx: RequestContext, instrument_id: int, c: sqlite3.Connection) -> tuple[dict, dict]:
    """(instrument, portfolio). Instruments are shared reference rows, so the actor must hold the security."""
    portfolio = resolve_portfolio(ctx, conn=c)
    inst = repo.get_instrument(instrument_id, conn=c)
    if inst is None:
        raise NotFound(f"instrument {instrument_id} does not exist")
    if not repo.portfolio_holds_instrument(portfolio["portfolio_id"], instrument_id, conn=c):
        raise NotAuthorized(f"{ctx.actor!r} holds no position in instrument {instrument_id}")
    return inst, portfolio


def set_instrument_classification(ctx: RequestContext, instrument_id: int, *, instrument_type=_UNSET,
                                  asset_class=_UNSET, sub_asset_class=_UNSET, leverage_factor=_UNSET,
                                  proxy_ticker=_UNSET, expected_version: int | None = None,
                                  reason: str | None = None, conn: sqlite3.Connection | None = None) -> dict:
    """
    A person's manual override of what a security is. The row becomes classification_source='manual', so
    automatic re-classification never overwrites it. The actor must hold the instrument in one of their
    active accounts; note the instrument row itself is shared, so the override applies wherever it is held.
    Only the fields passed are changed. proxy_ticker (None/"" clears) names a market symbol to use for
    analytics on a ticker-less fund — it is never shown as the user's holding.
    """
    fields: dict = {}
    if instrument_type is not _UNSET:
        if instrument_type not in INSTRUMENT_TYPES:
            raise ValueError(f"instrument_type must be one of {sorted(INSTRUMENT_TYPES)}, not {instrument_type!r}")
        fields["instrument_type"] = str(instrument_type)
    if asset_class is not _UNSET:
        if asset_class not in ASSET_CLASSES:
            raise ValueError(f"asset_class must be one of {sorted(ASSET_CLASSES)}, not {asset_class!r}")
        fields["asset_class"] = str(asset_class)
    if sub_asset_class is not _UNSET:
        fields["sub_asset_class"] = (str(sub_asset_class).strip().upper().replace(" ", "_") or None) if sub_asset_class else None
    if leverage_factor is not _UNSET:
        lev = float(leverage_factor)
        if lev == 0 or abs(lev) > 5:
            raise ValueError("leverage_factor must be non-zero and within ±5 (1.0 normal, 2.0 / 3.0 leveraged, negative inverse)")
        fields["leverage_factor"] = lev
    if proxy_ticker is not _UNSET:
        px = repo._norm_ticker(proxy_ticker) if proxy_ticker else ""
        if is_synthetic_ticker(px):
            raise ValueError("a proxy ticker must be a real market symbol, not another ticker-less fund")
        fields["proxy_ticker"] = px or None
    if not fields:
        raise ValueError("set_instrument_classification: nothing to change")
    with repo.unit_of_work(conn) as c:
        inst, portfolio = _owned_instrument(ctx, instrument_id, c)
        _check_version(inst, expected_version, "instrument")
        repo.update_instrument(instrument_id, conn=c, classification_source=ClassificationSource.MANUAL.value,
                               classified_at=_now_iso(), **fields)
        after = repo.get_instrument(instrument_id, conn=c)
        _audit(ctx, c, "set_instrument_classification", portfolio_id=portfolio["portfolio_id"],
               before=_classification_snapshot(inst) | {"ticker": inst["ticker"], "instrument_id": instrument_id},
               after=_classification_snapshot(after) | {"ticker": after["ticker"], "instrument_id": instrument_id},
               reason=reason)
        return after


def apply_auto_classification(ctx: RequestContext, instrument_id: int, classification, *,
                              conn: sqlite3.Connection | None = None) -> dict:
    """
    Store the classifier's result (an instrument_classifier.Classification). A manual classification is
    returned untouched. A FAILED lookup writes nothing — it never wipes a good classification, and leaving
    classified_at empty makes the next run retry. An audit event is written only when something changed.
    """
    with repo.unit_of_work(conn) as c:
        portfolio = resolve_portfolio(ctx, conn=c)
        inst = repo.get_instrument(instrument_id, conn=c)
        if inst is None:
            raise NotFound(f"instrument {instrument_id} does not exist")
        if inst.get("classification_source") == ClassificationSource.MANUAL.value or is_synthetic_ticker(inst["ticker"]):
            return inst
        if not classification.lookup_ok:
            return inst
        fields = {
            "instrument_type": classification.instrument_type, "asset_class": classification.asset_class,
            "sub_asset_class": classification.sub_asset_class, "leverage_factor": classification.leverage_factor,
            "expense_ratio": classification.expense_ratio, "fund_family": classification.fund_family,
            "category": classification.category,
        }
        changed = any(inst.get(k) != v for k, v in fields.items())
        repo.update_instrument(instrument_id, conn=c, classification_source=ClassificationSource.AUTO.value,
                               classified_at=_now_iso(), **fields)
        after = repo.get_instrument(instrument_id, conn=c)
        if changed:
            _audit(ctx, c, "classify_instrument", portfolio_id=portfolio["portfolio_id"],
                   before=_classification_snapshot(inst) | {"ticker": inst["ticker"], "instrument_id": instrument_id},
                   after=_classification_snapshot(after) | {"ticker": after["ticker"], "instrument_id": instrument_id},
                   reason=classification.basis or None)
        return after


def create_synthetic_instrument(ctx: RequestContext, *, plan_name: str, fund_name: str,
                                asset_class: str = AssetClass.UNKNOWN.value, sub_asset_class: str | None = None,
                                proxy_ticker: str | None = None, reason: str | None = None,
                                conn: sqlite3.Connection | None = None) -> dict:
    """
    Create (or return, if it already exists) the instrument for a fund with no ticker — typically a 401(k)
    collective investment trust. Its key is the synthetic "CIT:<PLAN>-<FUND>": never priced from the market and
    never predicted. Its price is entered by hand (set_position_price); proxy_ticker (e.g. VOO for an S&P 500
    CIT) is for analytics only. The new row is a manual classification of type CIT.
    """
    ticker = synthetic_cit_ticker(plan_name, fund_name)
    if asset_class not in ASSET_CLASSES:
        raise ValueError(f"asset_class must be one of {sorted(ASSET_CLASSES)}, not {asset_class!r}")
    proxy = repo._norm_ticker(proxy_ticker) if proxy_ticker else None
    if proxy and is_synthetic_ticker(proxy):
        raise ValueError("a proxy ticker must be a real market symbol, not another ticker-less fund")
    with repo.unit_of_work(conn) as c:
        portfolio = resolve_portfolio(ctx, conn=c)
        existing = repo.find_instrument(ticker, conn=c)
        if existing is not None:
            return existing
        inst = repo.get_or_create_instrument(ticker, name=f"{fund_name.strip()} ({plan_name.strip()})" if plan_name.strip()
                                             else fund_name.strip(), conn=c)
        repo.update_instrument(inst["instrument_id"], conn=c, instrument_type=InstrumentType.CIT.value,
                               asset_class=str(asset_class),
                               sub_asset_class=(sub_asset_class or "").strip().upper().replace(" ", "_") or None,
                               proxy_ticker=proxy, classification_source=ClassificationSource.MANUAL.value,
                               classified_at=_now_iso())
        after = repo.get_instrument(inst["instrument_id"], conn=c)
        _audit(ctx, c, "create_synthetic_instrument", portfolio_id=portfolio["portfolio_id"],
               after=_classification_snapshot(after) | {"ticker": ticker, "instrument_id": after["instrument_id"]},
               reason=reason)
        return after


def set_position_price(ctx: RequestContext, position_id: int, price: float, *, as_of_date: str | None = None,
                       expected_version: int | None = None, reason: str | None = None,
                       conn: sqlite3.Connection | None = None) -> dict:
    """
    Record a hand-entered price (and the resulting value) for one position. For a ticker-less fund this is the
    only price source — price refreshes skip it. For a market-priced position the next refresh overwrites it.
    """
    price = float(price)
    if not price > 0:
        raise ValueError("set_position_price: price must be positive")
    as_of = as_of_date or date.today().isoformat()
    with repo.unit_of_work(conn) as c:
        pos, acct = _owned_position(ctx, position_id, c)
        _check_version(pos, expected_version, "position")
        shares = pos.get("shares")
        repo.update_position(position_id, conn=c, touch=False, current_price=round(price, 4),
                             current_value=round(price * shares, 2) if shares else None, price_as_of=as_of)
        after = repo.get_position(position_id, conn=c)
        _audit(ctx, c, "set_position_price", portfolio_id=acct["portfolio_id"], account_id=pos["account_id"],
               position_id=position_id, before=_position_snapshot(pos), after=_position_snapshot(after), reason=reason)
        return after


# ── Authorized reads ──────────────────────────────────────────────────────────
#
# Every read below checks resolve_portfolio(ctx) FIRST — before touching a
# single holdings/account/cash/import/price row — and passes the resolved
# portfolio_id into the repo call, so an unauthorized/unverified caller never
# reaches private data AND a verified caller only ever sees their own
# portfolio's rows, never every portfolio's. holdings_db's own read functions
# (get_holdings, list_accounts, get_cash_balances, get_import_runs,
# get_price_history) still default to unscoped when called with no filter —
# that stays correct for system-wide batch jobs (nightly repricing across
# every portfolio) — but every one of these *_for functions must pass
# portfolio_id through, since this is the authorized boundary the rest of the
# app is expected to call.

def list_holdings_for(ctx: RequestContext) -> list:
    portfolio = resolve_portfolio(ctx)
    return repo.get_holdings(portfolio_id=portfolio["portfolio_id"])


def list_accounts_for(ctx: RequestContext, *, include_archived: bool = False) -> list[dict]:
    portfolio = resolve_portfolio(ctx)
    return repo.list_accounts(portfolio["portfolio_id"], include_archived=include_archived)


def get_cash_balances_for(ctx: RequestContext) -> list[dict]:
    portfolio = resolve_portfolio(ctx)
    return repo.get_cash_balances(portfolio_id=portfolio["portfolio_id"])


def list_import_runs_for(ctx: RequestContext, *, limit: int = 25) -> list[dict]:
    resolve_portfolio(ctx)
    return repo.get_import_runs(limit=limit)


def get_price_history_for(ctx: RequestContext, *, tickers: list[str] | None = None,
                          brokers: list[str] | None = None) -> list[dict]:
    resolve_portfolio(ctx)
    return repo.get_price_history(tickers=tickers, brokers=brokers)
