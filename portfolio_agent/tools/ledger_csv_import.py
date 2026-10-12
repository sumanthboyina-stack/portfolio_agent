"""
Generic CSV transaction import — the pure part: reading a file, suggesting a column mapping, normalising each row into a planned ledger
transaction, and deduplicating. No database, no network (services.ledger_service reads the account's existing transactions and writes).

Broker-specific parsers are OUT OF SCOPE (TODO: one mapping preset per broker once real files are available); this takes any CSV and lets the
person map its columns to the ledger's fields:

    date (required) · type (required) · ticker · quantity · price · amount · fees · external_ref · contribution_source

Types are matched case-insensitively through an alias table (Buy / Bought / Purchase -> BUY, Contribution / Deposit -> DEPOSIT, Div -> DIVIDEND ...).
A type it does not know makes that row INVALID (nothing is guessed). SPLIT rows are not importable (use record_split).

Per type: BUY / SELL need ticker, quantity and a price; if the price is blank but an amount is given the price is DERIVED from it (and the row
carries a warning). DEPOSIT / WITHDRAWAL / INTEREST / FEE need an amount (its sign is ignored; the type decides the direction). DIVIDEND needs an
amount and a ticker. EMPLOYER_MATCH is cash (amount) or shares (ticker, quantity, price).

Dates: YYYY-MM-DD, MM/DD/YYYY, M/D/YY, DD-Mon-YYYY, Mon D, YYYY. Numbers: thousands separators, $, and (parentheses) for negatives are accepted.

DEDUPLICATION. A row is a duplicate of an existing transaction when its external_ref is already used in the account, else when an existing
non-voided transaction has the same (trade_date, type, ticker, quantity, cash amount). Matching is by COUNT: if the account already holds 1 of 2
identical rows in a file, one is a duplicate and one is new, so two genuine identical trades on a day are both kept the first time and neither is
re-added the second time the same file is imported. Duplicates inside a file are decided by the same rule, in file order.
"""
from __future__ import annotations

import csv
import io
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime

MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 20000

FIELDS = ("date", "type", "ticker", "quantity", "price", "amount", "fees", "external_ref", "contribution_source")
REQUIRED = ("date", "type")

_HEADER_HINTS = {
    "date": ("date", "trade date", "trade_date", "transaction date", "settlement date", "run date"),
    "type": ("type", "transaction type", "action", "activity", "description", "transaction"),
    "ticker": ("ticker", "symbol", "security", "instrument"),
    "quantity": ("quantity", "qty", "shares", "units"),
    "price": ("price", "unit price", "share price", "price per share"),
    "amount": ("amount", "net amount", "total", "value", "net"),
    "fees": ("fees", "fee", "commission", "commissions"),
    "external_ref": ("external_ref", "external ref", "id", "transaction id", "reference", "ref", "confirmation", "order id"),
    "contribution_source": ("contribution_source", "contribution source", "source", "money source", "contribution type"),
}

_TYPE_ALIASES = {
    "BUY": ("buy", "bought", "purchase", "purchased", "buy to open", "reinvest", "reinvestment"),
    "SELL": ("sell", "sold", "sale", "sell to close", "redemption"),
    "DEPOSIT": ("deposit", "contribution", "transfer in", "cash in", "journal in", "rollover", "ach deposit", "electronic funds transfer received"),
    "WITHDRAWAL": ("withdrawal", "withdraw", "distribution", "transfer out", "cash out", "journal out", "ach withdrawal"),
    "DIVIDEND": ("dividend", "div", "cash dividend", "qualified dividend", "ordinary dividend"),
    "INTEREST": ("interest", "int", "bank interest", "credit interest"),
    "EMPLOYER_MATCH": ("employer match", "match", "employer contribution", "company match", "employer"),
    "FEE": ("fee", "fees", "advisory fee", "management fee", "custody fee", "service fee", "commission"),
}
_ALIAS = {a: t for t, al in _TYPE_ALIASES.items() for a in al}
IMPORTABLE = tuple(_TYPE_ALIASES)


class CsvImportError(ValueError):
    """The file as a whole cannot be read."""


@dataclass
class PlannedRow:
    line: int                                   # 1-based line number in the file (the header is line 1)
    raw: dict
    status: str                                 # new | duplicate | invalid
    txn_type: str | None = None
    fields: dict = field(default_factory=dict)  # arguments for the ledger verb: trade_date, ticker, quantity, price, amount, fees, external_ref, contribution_source
    cash_amount: float | None = None            # signed cash effect (the dedupe key uses it)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    duplicate_of: str | None = None             # "external_ref" | "same transaction"


def read_csv(raw: bytes) -> tuple[list[str], list[dict]]:
    """(headers, rows as dicts keyed by header). Raises CsvImportError for an empty / oversize / headerless / blank-header file."""
    if not raw or not raw.strip():
        raise CsvImportError("the file is empty")
    if len(raw) > MAX_BYTES:
        raise CsvImportError(f"the file is larger than {MAX_BYTES // (1024 * 1024)} MB")
    text = None
    for enc in ("utf-8-sig", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    rows = [r for r in reader]
    while rows and not any(c.strip() for c in rows[0]):
        rows.pop(0)
    if not rows:
        raise CsvImportError("the file has no header row")
    headers = [h.strip() for h in rows[0]]
    if not any(headers) or len(set(h for h in headers if h)) != len([h for h in headers if h]):
        raise CsvImportError("the header row is blank or repeats a column name")
    body = [r for r in rows[1:] if any(c.strip() for c in r)]
    if len(body) > MAX_ROWS:
        raise CsvImportError(f"more than {MAX_ROWS} rows")
    return headers, [{h: (r[i].strip() if i < len(r) else "") for i, h in enumerate(headers) if h} for r in body]


def suggest_mapping(headers: list[str]) -> dict[str, str | None]:
    """{field: header or None}, the first header that matches each field's usual names (exact, case-insensitive); a header is used once."""
    used: set[str] = set()
    out: dict[str, str | None] = {}
    low = {h.lower().strip(): h for h in headers if h}
    for f in FIELDS:
        pick = None
        for hint in _HEADER_HINTS[f]:
            h = low.get(hint)
            if h and h not in used:
                pick = h
                break
        out[f] = pick
        if pick:
            used.add(pick)
    return out


def validate_mapping(mapping: dict, headers: list[str]) -> None:
    for f in REQUIRED:
        if not mapping.get(f):
            raise CsvImportError(f"map a column for '{f}'")
    for f, h in mapping.items():
        if f not in FIELDS:
            raise CsvImportError(f"unknown field {f!r}")
        if h and h not in headers:
            raise CsvImportError(f"column {h!r} (for '{f}') is not in the file")
    chosen = [h for h in mapping.values() if h]
    if len(chosen) != len(set(chosen)):
        raise CsvImportError("a column cannot be mapped to two fields")


# ── cell parsing ──────────────────────────────────────────────────────────────

_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d-%b-%Y", "%b %d, %Y", "%B %d, %Y", "%Y/%m/%d", "%m-%d-%Y")


def parse_date(v: str) -> str | None:
    v = (v or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(v, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_number(v: str) -> float | None:
    """None for blank; ValueError for text that is not a number."""
    t = (v or "").strip()
    if t in ("", "-", "--", "N/A", "n/a"):
        return None
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()").replace("$", "").replace(",", "").replace("%", "").strip()
    f = float(t)
    if not math.isfinite(f):
        raise ValueError("not finite")
    return -f if neg else f


def normalize_type(v: str) -> str | None:
    t = re.sub(r"\s+", " ", (v or "").strip().lower())
    if t.upper() in IMPORTABLE:
        return t.upper()
    return _ALIAS.get(t)


# ── planning ──────────────────────────────────────────────────────────────────

def plan_rows(rows: list[dict], mapping: dict[str, str | None], *, existing_external_ids: set[str],
              existing_keys: dict, today: date | None = None) -> list[PlannedRow]:
    """Normalise every row and mark it new / duplicate / invalid. `existing_*` come from ledger_db.existing_for_dedupe for the target account."""
    from portfolio_agent.tools.ledger_db import dedupe_key
    today = today or date.today()
    keys = dict(existing_keys)                       # consumed as rows match, so identical rows are matched by count
    seen_ext = set(existing_external_ids)
    out: list[PlannedRow] = []
    get = lambda r, f: (r.get(mapping[f], "") if mapping.get(f) else "")
    for i, r in enumerate(rows, start=2):
        pr = PlannedRow(line=i, raw=r, status="new")
        try:
            _normalise(pr, r, get, today)
        except ValueError as exc:
            pr.errors.append(str(exc))
        if pr.errors:
            pr.status = "invalid"
            out.append(pr)
            continue
        ref = pr.fields.get("external_ref")
        if ref and ref in seen_ext:
            pr.status, pr.duplicate_of = "duplicate", "external_ref"
        else:
            k = dedupe_key(pr.fields["trade_date"], pr.txn_type, pr.fields.get("ticker"), pr.fields.get("quantity"), pr.cash_amount)
            if not ref and keys.get(k, 0) > 0:
                keys[k] -= 1
                pr.status, pr.duplicate_of = "duplicate", "same transaction"
        if pr.status == "new" and ref:
            seen_ext.add(ref)
        out.append(pr)
    return out


def _normalise(pr: PlannedRow, r: dict, get, today: date) -> None:
    d = parse_date(get(r, "date"))
    if d is None:
        raise ValueError(f"date {get(r, 'date')!r} is not a date")
    if d > today.isoformat():
        raise ValueError(f"date {d} is in the future")
    t = normalize_type(get(r, "type"))
    if t is None:
        raw_t = get(r, "type")
        raise ValueError(f"type {raw_t!r} is not one the import knows" + (" (splits are recorded one at a time, not imported)" if "split" in raw_t.lower() else ""))
    pr.txn_type = t

    def num(name):
        try:
            return parse_number(get(r, name))
        except ValueError:
            raise ValueError(f"{name} {get(r, name)!r} is not a number") from None
    qty, price, amount, fees = num("quantity"), num("price"), num("amount"), num("fees")
    ticker = (get(r, "ticker") or "").strip().upper() or None
    if fees is not None and fees < 0:
        fees = abs(fees)
    f = {"trade_date": d, "ticker": ticker, "external_ref": (get(r, "external_ref") or "").strip() or None,
         "contribution_source": (get(r, "contribution_source") or "").strip() or None, "memo": None}
    if t in ("BUY", "SELL"):
        if not ticker:
            raise ValueError(f"{t} needs a ticker")
        if qty is None or qty <= 0:
            raise ValueError(f"{t} needs a positive quantity")
        qty = abs(qty)
        fees = fees or 0.0
        if price is None:
            if amount is None:
                raise ValueError(f"{t} needs a price (or an amount to derive it from)")
            base = abs(amount) - fees if t == "BUY" else abs(amount) + fees
            if base <= 0:
                raise ValueError(f"{t}: the amount is too small to derive a price after fees")
            price = base / qty
            pr.warnings.append("price derived from the amount (amount ∓ fees) ÷ quantity")
        if price < 0 or (price == 0 and t == "BUY"):
            raise ValueError(f"{t} price must be positive")
        f.update(quantity=round(qty, 8), price=price, fees=round(fees, 2))
        pr.cash_amount = round(-(qty * price + fees) if t == "BUY" else qty * price - fees, 2)
        if amount is not None and price is not None and abs(abs(amount) - abs(pr.cash_amount)) > 0.011 and "derived" not in " ".join(pr.warnings):
            pr.warnings.append(f"the amount in the file ({abs(amount):.2f}) differs from quantity × price ± fees ({abs(pr.cash_amount):.2f}); the latter is used")
    elif t in ("DEPOSIT", "WITHDRAWAL", "INTEREST", "FEE", "DIVIDEND"):
        if amount is None or amount == 0:
            raise ValueError(f"{t} needs a non-zero amount")
        if t == "DIVIDEND" and not ticker:
            raise ValueError("DIVIDEND needs a ticker")
        amt = round(abs(amount), 2)
        f["amount"] = amt
        pr.cash_amount = -amt if t in ("WITHDRAWAL", "FEE") else amt
        if qty is not None:
            f["quantity"] = None                    # a quantity on a cash row is ignored, not stored
            pr.warnings.append("quantity ignored on a cash transaction")
        if t not in ("DEPOSIT",) and f["contribution_source"]:
            pr.warnings.append("contribution_source applies to deposits and employer matches only; ignored")
            f["contribution_source"] = None
    elif t == "EMPLOYER_MATCH":
        if ticker or qty is not None or price is not None:
            if not (ticker and qty and qty > 0 and price is not None and price > 0):
                raise ValueError("an employer match in shares needs ticker, quantity and price")
            f.update(quantity=round(abs(qty), 8), price=price)
            pr.cash_amount = 0.0
        else:
            if amount is None or amount == 0:
                raise ValueError("an employer match needs an amount (cash) or ticker, quantity and price (shares)")
            f["amount"] = round(abs(amount), 2)
            pr.cash_amount = f["amount"]
    pr.fields = f


def summarize(plan: list[PlannedRow]) -> dict:
    c = {"new": 0, "duplicate": 0, "invalid": 0}
    for p in plan:
        c[p.status] += 1
    return {**c, "rows": len(plan), "warnings": sum(len(p.warnings) for p in plan)}


def import_order(plan: list[PlannedRow]) -> list[PlannedRow]:
    """The NEW rows in the order they should be applied: by date, same-day money in / purchases before sales, then file order — so a sale never
    appears to come before the purchase it needs just because of how the file was sorted."""
    pri = {"SELL": 2, "WITHDRAWAL": 2, "FEE": 2}
    return sorted((p for p in plan if p.status == "new"), key=lambda p: (p.fields["trade_date"], pri.get(p.txn_type, 1), p.line))
