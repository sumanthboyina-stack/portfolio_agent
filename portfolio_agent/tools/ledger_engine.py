"""
Ledger replay — turns an account's transactions into cash, tax lots and realized gains. Pure and deterministic: transactions in, state out.
No database, no network, no clock; the same transactions always give the same answer, which is what lets tax lots be REBUILT at any time
instead of being edited.

Transactions (see domain.TransactionType) are applied in date order (a SPLIT first on its day, then by transaction id):
  BUY             opens a lot: unit cost = (quantity x price + fees) / quantity; cash += amount (negative)
  SELL            closes lots by the method stored on the sale (FIFO / LIFO / HIFO / SPECIFIC); each closure carries its proceeds (fees
                  taken out), cost basis, realized gain and SHORT / LONG holding period (LONG = held more than 365 days); cash += amount
  EMPLOYER_MATCH  with shares: opens a lot at the price given, no cash; without: cash += amount
  OPENING_BALANCE shares: opens a lot at the unit cost given (what the account held the day it became ledger-managed; not a trade); cash
                  only: starts the cash balance
  SPLIT           multiplies every open lot of the instrument by numerator / denominator and divides its unit cost; total basis is unchanged
  DEPOSIT / WITHDRAWAL / DIVIDEND / INTEREST / FEE   cash += amount (stored signed)
  VOID            cancels the transaction it points to: that transaction and the VOID row are both left out of the replay

A lot is identified by the id of the transaction that opened it (open_transaction_id) — stable however often the lots are rebuilt.
Selling more than the lots hold, voiding something that does not exist, or a malformed row raises LedgerError; callers replay BEFORE writing
so an invalid change is never stored. Not modelled: wash sales, average-cost method, cash in lieu of fractional shares.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

EPS = 1e-9
LONG_TERM_DAYS = 365


class LedgerError(ValueError):
    """The transactions cannot be applied (oversold, unknown lot, bad void ...)."""


@dataclass(frozen=True)
class Txn:
    txn_id: int
    account_id: int
    txn_type: str
    trade_date: str
    instrument_id: int | None = None
    quantity: float | None = None
    price: float | None = None
    fees: float = 0.0
    amount: float = 0.0                          # cash effect, signed
    split_numerator: float | None = None
    split_denominator: float | None = None
    lot_method: str | None = None
    lot_selection: list | None = None            # SPECIFIC: [{"lot": open_transaction_id, "quantity": q}]
    voids_txn_id: int | None = None


@dataclass
class Lot:
    open_transaction_id: int
    instrument_id: int
    open_date: str
    original_quantity: float
    remaining_quantity: float
    unit_cost: float
    source_type: str                             # BUY | EMPLOYER_MATCH | OPENING_BALANCE

    @property
    def basis_remaining(self) -> float:
        return round(self.remaining_quantity * self.unit_cost, 2)


@dataclass(frozen=True)
class Closure:
    open_transaction_id: int
    close_transaction_id: int
    instrument_id: int
    quantity: float
    proceeds: float
    cost_basis: float
    realized_gain: float
    open_date: str
    close_date: str
    holding_period: str                          # SHORT | LONG
    method: str


@dataclass
class LedgerState:
    cash: float = 0.0
    lots: dict[int, Lot] = field(default_factory=dict)
    closures: list[Closure] = field(default_factory=list)
    totals: dict[str, float] = field(default_factory=lambda: {
        "deposits": 0.0, "withdrawals": 0.0, "dividends": 0.0, "interest": 0.0, "employer_match_cash": 0.0, "fees": 0.0,
        "opening_cash": 0.0})
    applied: int = 0

    def positions(self) -> dict[int, dict]:
        """{instrument_id: {"quantity", "cost_basis", "avg_cost"}} over open lots."""
        out: dict[int, dict] = {}
        for lot in self.lots.values():
            if lot.remaining_quantity <= EPS:
                continue
            p = out.setdefault(lot.instrument_id, {"quantity": 0.0, "cost_basis": 0.0})
            p["quantity"] += lot.remaining_quantity
            p["cost_basis"] += lot.remaining_quantity * lot.unit_cost
        for p in out.values():
            p["quantity"] = round(p["quantity"], 8)
            p["cost_basis"] = round(p["cost_basis"], 2)
            p["avg_cost"] = round(p["cost_basis"] / p["quantity"], 6) if p["quantity"] else None
        return out

    def realized_gain(self) -> float:
        return round(sum(c.realized_gain for c in self.closures), 2)


def _days(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def _order_key(t: Txn):
    return (t.trade_date, 0 if t.txn_type == "SPLIT" else 1, t.txn_id)


def effective(txns: list[Txn]) -> list[Txn]:
    """The transactions that count: VOID rows and the rows they cancel are removed. Raises LedgerError for a void of a missing, foreign, or
    already-voided transaction, or of another VOID."""
    by_id = {t.txn_id: t for t in txns}
    voided: set[int] = set()
    for t in txns:
        if t.txn_type != "VOID":
            continue
        target = by_id.get(t.voids_txn_id)
        if target is None:
            raise LedgerError(f"transaction {t.txn_id} voids {t.voids_txn_id}, which is not in this account")
        if target.txn_type == "VOID":
            raise LedgerError(f"transaction {t.txn_id}: a void cannot be voided")
        if target.txn_id in voided:
            raise LedgerError(f"transaction {target.txn_id} is already voided")
        voided.add(target.txn_id)
    return [t for t in txns if t.txn_type != "VOID" and t.txn_id not in voided]


def _pick(lots: list[Lot], method: str) -> list[Lot]:
    if method == "LIFO":
        return sorted(lots, key=lambda l: (l.open_date, l.open_transaction_id), reverse=True)
    if method == "HIFO":
        return sorted(lots, key=lambda l: (-l.unit_cost, l.open_date, l.open_transaction_id))
    return sorted(lots, key=lambda l: (l.open_date, l.open_transaction_id))        # FIFO (and the base order)


def replay(txns: list[Txn]) -> LedgerState:
    st = LedgerState()
    for t in sorted(effective(list(txns)), key=_order_key):
        st.applied += 1
        k = t.txn_type
        if k == "BUY":
            if not t.instrument_id or not t.quantity or t.quantity <= 0 or t.price is None:
                raise LedgerError(f"BUY {t.txn_id}: instrument, quantity and price are required")
            unit = (t.quantity * t.price + (t.fees or 0.0)) / t.quantity
            st.lots[t.txn_id] = Lot(t.txn_id, t.instrument_id, t.trade_date, t.quantity, t.quantity, unit, "BUY")
            st.cash += t.amount
        elif k == "EMPLOYER_MATCH":
            if t.instrument_id and t.quantity:
                if t.price is None:
                    raise LedgerError(f"EMPLOYER_MATCH {t.txn_id}: a price is required for shares")
                st.lots[t.txn_id] = Lot(t.txn_id, t.instrument_id, t.trade_date, t.quantity, t.quantity, t.price, "EMPLOYER_MATCH")
            else:
                st.cash += t.amount
                st.totals["employer_match_cash"] += t.amount
        elif k == "OPENING_BALANCE":
            if t.instrument_id and t.quantity:
                if t.quantity <= 0 or t.price is None or t.price < 0:
                    raise LedgerError(f"OPENING_BALANCE {t.txn_id}: a positive quantity and a unit cost are required")
                st.lots[t.txn_id] = Lot(t.txn_id, t.instrument_id, t.trade_date, t.quantity, t.quantity, t.price, "OPENING_BALANCE")
            else:
                st.cash += t.amount
                st.totals["opening_cash"] += t.amount
        elif k == "SELL":
            _sell(st, t)
        elif k == "SPLIT":
            if not t.instrument_id or not t.split_numerator or not t.split_denominator or t.split_numerator <= 0 or t.split_denominator <= 0:
                raise LedgerError(f"SPLIT {t.txn_id}: instrument and a positive numerator / denominator are required")
            ratio = t.split_numerator / t.split_denominator
            for lot in st.lots.values():
                if lot.instrument_id == t.instrument_id and lot.remaining_quantity > EPS:
                    lot.remaining_quantity = round(lot.remaining_quantity * ratio, 8)
                    lot.original_quantity = round(lot.original_quantity * ratio, 8)
                    lot.unit_cost = lot.unit_cost / ratio
        elif k in ("DEPOSIT", "WITHDRAWAL", "DIVIDEND", "INTEREST", "FEE"):
            st.cash += t.amount
            key = {"DEPOSIT": "deposits", "WITHDRAWAL": "withdrawals", "DIVIDEND": "dividends", "INTEREST": "interest", "FEE": "fees"}[k]
            st.totals[key] += abs(t.amount) if k in ("WITHDRAWAL", "FEE") else t.amount
        else:
            raise LedgerError(f"transaction {t.txn_id}: unknown type {k!r}")
    st.cash = round(st.cash, 2)
    st.totals = {k: round(v, 2) for k, v in st.totals.items()}
    return st


def _sell(st: LedgerState, t: Txn) -> None:
    if not t.instrument_id or not t.quantity or t.quantity <= 0 or t.price is None:
        raise LedgerError(f"SELL {t.txn_id}: instrument, quantity and price are required")
    method = t.lot_method or "FIFO"
    open_lots = [l for l in st.lots.values() if l.instrument_id == t.instrument_id and l.remaining_quantity > EPS]
    have = sum(l.remaining_quantity for l in open_lots)
    if t.quantity > have + EPS:
        raise LedgerError(f"SELL {t.txn_id} on {t.trade_date}: selling {t.quantity:g} but only {have:g} held at that date")
    take: list[tuple[Lot, float]] = []
    if method == "SPECIFIC":
        by_id = {l.open_transaction_id: l for l in open_lots}
        total = 0.0
        for sel in (t.lot_selection or []):
            lot = by_id.get(int(sel["lot"]))
            q = float(sel["quantity"])
            if lot is None:
                raise LedgerError(f"SELL {t.txn_id}: lot {sel['lot']} is not an open lot of this instrument on {t.trade_date}")
            if q <= 0 or q > lot.remaining_quantity + EPS:
                raise LedgerError(f"SELL {t.txn_id}: lot {sel['lot']} has {lot.remaining_quantity:g} left, not {q:g}")
            take.append((lot, q))
            total += q
        if abs(total - t.quantity) > 1e-6:
            raise LedgerError(f"SELL {t.txn_id}: the chosen lots add up to {total:g}, not {t.quantity:g}")
        if len({l.open_transaction_id for l, _ in take}) != len(take):
            raise LedgerError(f"SELL {t.txn_id}: a lot is named twice")
    else:
        need = t.quantity
        for lot in _pick(open_lots, method):
            if need <= EPS:
                break
            q = min(lot.remaining_quantity, need)
            take.append((lot, q))
            need -= q
    net_proceeds = t.quantity * t.price - (t.fees or 0.0)
    allocated = 0.0
    for i, (lot, q) in enumerate(take):
        part = round(net_proceeds * q / t.quantity, 2) if i < len(take) - 1 else round(net_proceeds - allocated, 2)
        allocated += part
        basis = round(lot.unit_cost * q, 2)
        st.closures.append(Closure(
            open_transaction_id=lot.open_transaction_id, close_transaction_id=t.txn_id, instrument_id=t.instrument_id, quantity=round(q, 8),
            proceeds=part, cost_basis=basis, realized_gain=round(part - basis, 2), open_date=lot.open_date, close_date=t.trade_date,
            holding_period="LONG" if _days(lot.open_date, t.trade_date) > LONG_TERM_DAYS else "SHORT", method=method))
        lot.remaining_quantity = round(lot.remaining_quantity - q, 8)
    st.cash += t.amount
