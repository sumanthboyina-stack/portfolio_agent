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


def parse_fidelity_csv(content: str | bytes) -> list[dict]:
    text = _decode(content)
    reader = csv.reader(io.StringIO(text))
    all_rows = list(reader)

    header_idx = None
    for i, row in enumerate(all_rows):
        normalized = [c.strip().lower() for c in row]
        if "symbol" in normalized and "account number" in normalized:
            header_idx = i
            break

    if header_idx is None:
        return []

    headers = [c.strip() for c in all_rows[header_idx]]
    data_rows = all_rows[header_idx + 1:]

    i_account_name   = _flex_header(headers, "Account Name", "Account Name/Number")
    i_account_number = _flex_header(headers, "Account Number")
    i_symbol         = _flex_header(headers, "Symbol")
    i_description    = _flex_header(headers, "Description")
    i_quantity       = _flex_header(headers, "Quantity")
    i_last_price     = _flex_header(headers, "Last Price", "Current Price")
    i_current_value  = _flex_header(headers, "Current Value", "Value")
    i_cost_basis     = _flex_header(headers, "Cost Basis Total", "Total Cost Basis")
    i_avg_cost       = _flex_header(headers, "Average Cost Basis", "Avg Cost Basis", "Average Cost")

    def _get(row: list[str], idx: Optional[int]) -> str:
        if idx is None or idx >= len(row):
            return ""
        return row[idx].strip()

    results = []
    for row in data_rows:
        if not any(c.strip() for c in row):
            continue

        # Skip footer/disclaimer rows — long text in the first cell, no real ticker
        first_cell = row[0].strip() if row else ""
        if re.match(r"^\d{1,2}/\d{1,2}/\d{4}", first_cell):
            continue
        if len(first_cell) > 60:
            continue

        symbol = _get(row, i_symbol)
        if _is_invalid_ticker(symbol):
            continue

        ticker = symbol.upper()
        account_name = _get(row, i_account_name)
        account_number = _get(row, i_account_number)

        results.append({
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
        if _MONEY_MARKET_RE.search(symbol):
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
    sample = text[:500].lower()

    if "fidelity" in sample:
        return "fidelity"
    if "vanguard" in sample:
        return "vanguard"

    # Check column headers for Fidelity-specific columns
    first_lines = text[:2000]
    if "average cost basis" in first_lines.lower():
        return "fidelity"

    # Check for Vanguard-specific column pattern
    vanguard_cols = {"investment name", "share price", "% of portfolio"}
    found = sum(1 for col in vanguard_cols if col in first_lines.lower())
    if found >= 2:
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
