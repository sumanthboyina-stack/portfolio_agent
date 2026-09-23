from __future__ import annotations

import csv
import io
import re
from typing import Optional


def _clean_float(s: str) -> Optional[float]:
    if not s:
        return None
    cleaned = re.sub(r"[\$,% +\s]", "", str(s))
    if not cleaned or cleaned in ("-", "--", "N/A", "n/a"):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _decode(content: str | bytes) -> str:
    if isinstance(content, bytes):
        try:
            return content.decode("utf-8-sig")
        except UnicodeDecodeError:
            return content.decode("latin-1")
    return content


_TICKER_RE = re.compile(r"^[A-Z]{1,5}$")

_MONEY_MARKET_RE = re.compile(
    r"\*|^FDRXX|^SPAXX|^FCASH|^FMPXX|^FZFXX|^FZAXX|^FNSXX|^FDIC|^VMFXX|^VMMXX",
    re.IGNORECASE,
)


_CASH_LABELS = ("CASH", "PENDING ACTIVITY", "CASH & CASH INVESTMENTS")


def is_cash_like(symbol: str) -> bool:
    """Money-market / sweep / cash / pending-activity line: a cash balance, not a security."""
    sym = (symbol or "").strip()
    if not sym:
        return False
    return sym.upper() in _CASH_LABELS or bool(_MONEY_MARKET_RE.search(sym))


def split_cash_rows(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Separate parser output into (securities, cash rows flagged is_cash)."""
    securities = [r for r in rows if not r.get("is_cash")]
    cash = [r for r in rows if r.get("is_cash")]
    return securities, cash


def _cash_row(symbol: str, description: str, value, account_name: str,
              account_number: str, account_type: str) -> dict:
    return {
        "is_cash": True,
        "ticker": symbol.strip().upper(),
        "description": description,
        "current_value": value,
        "account_name": account_name,
        "account_number": account_number,
        "account_type": account_type,
    }


def _is_invalid_ticker(ticker: str) -> bool:
    if not ticker:
        return True
    if ticker in ("--", "Pending Activity", "Cash", "N/A"):
        return True
    if _MONEY_MARKET_RE.search(ticker):
        return True
    return not _TICKER_RE.match(ticker.upper())


def _fidelity_account_type(account_name: str) -> str:
    name = account_name.upper()
    if "ROTH" in name:
        return "ROTH_IRA"
    if "ROLLOVER" in name:
        return "ROLLOVER_IRA"
    if "IRA" in name or "TRADITIONAL" in name:
        return "IRA"
    if "401" in name:
        return "401K"
    if "HSA" in name:
        return "HSA"
    return "TAXABLE"


def _flex_header(headers: list[str], *candidates: str) -> Optional[int]:
    """Return index of first header matching any candidate (case-insensitive, stripped)."""
    norm = [h.strip().lower() for h in headers]
    for candidate in candidates:
        target = candidate.strip().lower()
        if target in norm:
            return norm.index(target)
    return None


_FIDELITY_DATA_COLS = {
    "quantity", "last price", "current value", "cost basis total",
    "average cost basis", "description",
}


def _is_fidelity_header_row(normalized: list[str]) -> bool:
    """True if this row looks like a Fidelity data-header (has Symbol + data columns)."""
    if "symbol" not in normalized:
        return False
    return any(c in normalized for c in _FIDELITY_DATA_COLS) or "account number" in normalized


def _find_fidelity_sections(all_rows: list) -> list[tuple[int, list[str], str, str]]:
    """
    Locate all data-header rows in a Fidelity CSV.

    Handles two export layouts:
    • Inline (modern): single header row with Account Number / Account Name as columns
      e.g.  Account Number,Account Name,Symbol,Description,...
    • Sectioned (multi-account): separate metadata rows before each Symbol header
      e.g.  Account Number,X123,Account Name,Roth IRA
            Symbol,Description,...

    Returns list of (header_row_idx, header_cols, account_name, account_number).
    """
    sections: list[tuple[int, list[str], str, str]] = []
    pending_name = ""
    pending_number = ""

    for i, row in enumerate(all_rows):
        normalized = [c.strip().lower() for c in row]

        # ── Metadata key-value row (sectioned format) ─────────────────────
        # Signature: exactly 2–4 non-empty cells, no "symbol", first cell is
        # "account number" or "account name", second cell is an actual value
        # (not another column header name).
        is_metadata = (
            "account number" in normalized
            and "symbol" not in normalized
            and len([c for c in row if c.strip()]) <= 6
        )
        if is_metadata:
            norm = normalized
            if "account number" in norm:
                idx = norm.index("account number")
                val = row[idx + 1].strip() if idx + 1 < len(row) else ""
                # Only treat as value if it doesn't look like a column header
                if val and val.lower() not in _FIDELITY_DATA_COLS | {"account name", "symbol", "description"}:
                    pending_number = val
            if "account name" in norm:
                idx = norm.index("account name")
                val = row[idx + 1].strip() if idx + 1 < len(row) else ""
                if val and val.lower() not in _FIDELITY_DATA_COLS | {"account number", "symbol", "description"}:
                    pending_name = val
            continue

        # ── Data header row ────────────────────────────────────────────────
        if _is_fidelity_header_row(normalized):
            headers = [c.strip() for c in row]
            # Inline format: Account Number and Account Name ARE columns
            # → don't use pending values (they'll be read per-row from the data)
            if "account number" in normalized and "account name" in normalized:
                sections.append((i, headers, "", ""))
            else:
                sections.append((i, headers, pending_name, pending_number))
            pending_name = ""
            pending_number = ""

    return sections


def parse_fidelity_csv(content: str | bytes) -> list[dict]:
    text = _decode(content)
    reader = csv.reader(io.StringIO(text))
    all_rows = list(reader)

    sections = _find_fidelity_sections(all_rows)
    if not sections:
        return []

    def _get(row: list[str], idx: Optional[int]) -> str:
        if idx is None or idx >= len(row):
            return ""
        return row[idx].strip()

    # Fidelity footers start with the download date ("MM/DD/YYYY ...") — that
    # is the statement date the prices/values are as of.
    statement_date = None
    for row in all_rows:
        first = row[0].strip() if row else ""
        m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})", first)
        if m:
            statement_date = f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
            break

    results = []
    for sec_idx, (header_idx, headers, sec_account_name, sec_account_number) in enumerate(sections):
        next_header = sections[sec_idx + 1][0] if sec_idx + 1 < len(sections) else len(all_rows)
        data_rows = all_rows[header_idx + 1:next_header]

        i_account_name   = _flex_header(headers, "Account Name", "Account Name/Number")
        i_account_number = _flex_header(headers, "Account Number")
        i_symbol         = _flex_header(headers, "Symbol")
        i_description    = _flex_header(headers, "Description")
        i_quantity       = _flex_header(headers, "Quantity")
        i_last_price     = _flex_header(headers, "Last Price", "Current Price")
        i_current_value  = _flex_header(headers, "Current Value", "Value")
        i_cost_basis     = _flex_header(headers, "Cost Basis Total", "Total Cost Basis")
        i_avg_cost       = _flex_header(headers, "Average Cost Basis", "Avg Cost Basis", "Average Cost")

        for row in data_rows:
            if not any(c.strip() for c in row):
                continue

            first_cell = row[0].strip() if row else ""
            # Skip footer/disclaimer rows
            if re.match(r"^\d{1,2}/\d{1,2}/\d{4}", first_cell):
                continue
            if len(first_cell) > 60:
                continue

            symbol = _get(row, i_symbol)
            account_name   = _get(row, i_account_name) or sec_account_name
            account_number = _get(row, i_account_number) or sec_account_number

            if is_cash_like(symbol):
                value = _clean_float(_get(row, i_current_value))
                if value is not None:
                    results.append(_cash_row(symbol, _get(row, i_description), value, account_name,
                                             account_number, _fidelity_account_type(account_name)))
                continue
            if _is_invalid_ticker(symbol):
                continue

            ticker = symbol.upper()

            results.append({
                "as_of_date": statement_date,
                "ticker": ticker,
                "description": _get(row, i_description),
                "shares": _clean_float(_get(row, i_quantity)),
                "avg_cost": _clean_float(_get(row, i_avg_cost)),
                "cost_basis_total": _clean_float(_get(row, i_cost_basis)),
                "current_price": _clean_float(_get(row, i_last_price)),
                "current_value": _clean_float(_get(row, i_current_value)),
                "account_name": account_name,
                "account_number": account_number,
                "account_type": _fidelity_account_type(account_name),
                "sector": "",
            })

    return results


def _vanguard_account_type(account_number: str, description: str) -> str:
    combined = (account_number + " " + description).upper()
    if "ROTH" in combined:
        return "ROTH_IRA"
    if "TRAD" in combined or "IRA" in combined:
        return "IRA"
    if "BROKERAGE" in combined or "INDIVIDUAL" in combined:
        return "TAXABLE"
    if "401" in combined:
        return "401K"
    return "TAXABLE"


def parse_vanguard_csv(content: str | bytes) -> list[dict]:
    text = _decode(content)
    reader = csv.reader(io.StringIO(text))
    all_rows = list(reader)

    header_idx = None
    for i, row in enumerate(all_rows):
        normalized = [c.strip().lower() for c in row]
        # Look for the data header row
        has_symbol = any(c in normalized for c in ("symbol", "ticker symbol"))
        has_name = any(c in normalized for c in ("investment name", "fund name", "security name"))
        if has_symbol or has_name:
            header_idx = i
            break

    if header_idx is None:
        return []

    headers = [c.strip() for c in all_rows[header_idx]]
    data_rows = all_rows[header_idx + 1:]

    i_account_number = _flex_header(headers, "Account Number", "Account #")
    i_name           = _flex_header(headers, "Investment Name", "Fund Name", "Security Name", "Name")
    i_symbol         = _flex_header(headers, "Symbol", "Ticker Symbol", "Ticker")
    i_shares         = _flex_header(headers, "Shares", "Share Quantity", "Quantity")
    i_price          = _flex_header(headers, "Share Price", "Price", "NAV")
    i_total_value    = _flex_header(headers, "Total Value", "Value", "Market Value")

    def _get(row: list[str], idx: Optional[int]) -> str:
        if idx is None or idx >= len(row):
            return ""
        return row[idx].strip()

    results = []
    for row in data_rows:
        if not any(c.strip() for c in row):
            continue

        symbol = _get(row, i_symbol)
        if not symbol or symbol.upper() in ("N/A", "--", ""):
            continue
        if is_cash_like(symbol):
            value = _clean_float(_get(row, i_total_value))
            if value is not None:
                acct_no = _get(row, i_account_number)
                results.append(_cash_row(symbol, _get(row, i_name), value, "Vanguard", acct_no,
                                         _vanguard_account_type(acct_no, _get(row, i_name))))
            continue
        # Vanguard sometimes has rows with more than 5 chars for bond/fund codes
        ticker = symbol.strip().upper()
        if not _TICKER_RE.match(ticker):
            continue

        account_number = _get(row, i_account_number)
        description = _get(row, i_name)

        results.append({
            "ticker": ticker,
            "description": description,
            "shares": _clean_float(_get(row, i_shares)),
            "avg_cost": None,
            "cost_basis_total": None,
            "current_price": _clean_float(_get(row, i_price)),
            "current_value": _clean_float(_get(row, i_total_value)),
            "account_name": "Vanguard",
            "account_number": account_number,
            "account_type": _vanguard_account_type(account_number, description),
            "sector": "",
        })

    return results


def detect_broker(content: str | bytes) -> Optional[str]:
    text = _decode(content)
    sample = text[:5000].lower()

    if "fidelity" in sample:
        return "fidelity"
    if "vanguard" in sample:
        return "vanguard"

    # Fidelity-specific column names
    fidelity_signals = {"average cost basis", "cost basis total", "last price change"}
    if sum(1 for s in fidelity_signals if s in sample) >= 2:
        return "fidelity"
    if any(s in sample for s in fidelity_signals):
        return "fidelity"

    # Vanguard-specific column pattern
    vanguard_cols = {"investment name", "share price", "% of portfolio"}
    if sum(1 for col in vanguard_cols if col in sample) >= 2:
        return "vanguard"

    return None


def parse_csv(content: str | bytes) -> tuple[str, list[dict]]:
    broker = detect_broker(content)
    if broker == "fidelity":
        return "fidelity", parse_fidelity_csv(content)
    if broker == "vanguard":
        return "vanguard", parse_vanguard_csv(content)
    raise ValueError(
        "Unrecognised CSV format — expected Fidelity or Vanguard export"
    )
