"""
Generic, LLM-based holdings extraction (CSV + Excel).

Fallback path for broker exports that the hand-written parsers in
``holdings_parser.py`` don't recognise. Produces rows in the exact same
canonical shape as ``parse_fidelity_csv`` / ``parse_vanguard_csv``:

    ticker            str          uppercase ticker symbol
    description       str          company/fund name, "" if unknown
    shares            float|None
    avg_cost          float|None
    cost_basis_total  float|None
    current_price     float|None
    current_value     float|None
    account_name      str          "" if unknown
    account_number    str          "" if unknown (masked to last 4 digits)
    account_type      str          a domain.AccountType value: TAXABLE | TRADITIONAL_IRA | ROTH_IRA | ROLLOVER_IRA |
                                   SEP_IRA | 401K | ROTH_401K | 403B | 457B | HSA | 529 | CASH | OTHER
    sector            str          always "" at parse time

Never raises on bad input — returns [] and lets the caller decide how to
surface that.
"""

from __future__ import annotations

import io
import json
import re
from typing import Any, Optional

from portfolio_agent.log import get_logger as _get_logger
from portfolio_agent.tools.holdings_parser import (
    _clean_float,
    _decode,
    _is_invalid_ticker,
)

_log = _get_logger("llm_holdings_parser")

# Statements / workbooks can be long; extraction doesn't need every disclosure
# page or empty sheet.
MAX_PROMPT_CHARS = 15_000

_EXCEL_EXTENSIONS = (".xlsx", ".xlsm", ".xls")
_ZIP_SIGNATURE = b"PK\x03\x04"          # every .xlsx is a ZIP container
_XLS_SIGNATURE = b"\xd0\xcf\x11\xe0"    # legacy .xls (OLE2 compound file)

from portfolio_agent.domain import normalize_account_type
from portfolio_agent.tools.pdf_statement import is_pdf   # noqa: F401  (re-exported: detection of a PDF lives next to is_excel)


# ── Input detection ────────────────────────────────────────────────────────────

def is_excel(content: bytes, filename: str = "") -> bool:
    """True if the payload is a spreadsheet, judged by extension first and
    file signature as a fallback (the filename may not be passed through).
    A PDF is never a spreadsheet (see is_pdf)."""
    name = (filename or "").lower()
    if name.endswith(".pdf") or (isinstance(content, (bytes, bytearray)) and b"%PDF-" in bytes(content[:1024])):
        return False
    if name.endswith(_EXCEL_EXTENSIONS):
        return True
    if isinstance(content, (bytes, bytearray)):
        head = bytes(content[:4])
        if head == _ZIP_SIGNATURE or head == _XLS_SIGNATURE:
            return True
    return False


# ── Source → text ──────────────────────────────────────────────────────────────

def _excel_to_text(content: bytes) -> str:
    """Flatten every sheet of a workbook into labelled CSV blocks.

    All sheets are included on purpose — brokers name and organise sheets
    inconsistently, so which sheet(s) hold positions is left to the LLM.
    """
    import pandas as pd

    sheets = pd.read_excel(io.BytesIO(content), sheet_name=None, header=None)
    blocks: list[str] = []
    for sheet_name, df in sheets.items():
        df = df.dropna(how="all").dropna(axis=1, how="all")
        if df.empty:
            csv_text = "(empty sheet)"
        else:
            csv_text = df.to_csv(index=False, header=False, lineterminator="\n").strip()
        blocks.append(f"--- Sheet: {sheet_name} ---\n{csv_text}\n")
    return "\n".join(blocks)


def _source_to_text(content: bytes, filename: str = "") -> str:
    if is_excel(content, filename):
        return _excel_to_text(content)
    return _decode(content)


# ── Prompt ─────────────────────────────────────────────────────────────────────

def _build_prompt(text: str, filename: str = "") -> str:
    source_note = f' (file name: "{filename}")' if filename else ""
    return f"""You are extracting brokerage holdings from an exported statement{source_note}.
The source may be a CSV export or a multi-sheet spreadsheet flattened to text, with each
sheet introduced by a line like "--- Sheet: <name> ---".

Extract EVERY actual security position (stocks, ETFs, mutual funds, bonds with a ticker)
into a JSON array. Each element must have exactly these keys:

  "ticker"           string  - ticker symbol, uppercase
  "description"      string  - company / fund name, "" if not present
  "shares"           number or null
  "avg_cost"         number or null  - average cost per share
  "cost_basis_total" number or null  - total cost basis for the position
  "current_price"    number or null  - last / current price per share
  "current_value"    number or null  - current market value of the position
  "account_name"     string  - account nickname / title, "" if not present
  "account_number"   string  - masked to the LAST 4 DIGITS only (e.g. "****1234"), "" if not present
  "account_type"     string  - one of "TAXABLE", "TRADITIONAL_IRA", "ROTH_IRA", "ROLLOVER_IRA", "SEP_IRA",
                               "401K", "ROTH_401K", "403B", "457B", "HSA", "529", "CASH", "OTHER"

Rules:
- Skip cash, money-market, sweep, and "pending activity" line items (e.g. SPAXX, FDRXX,
  VMFXX, FCASH, "Cash & Cash Investments").
- Skip subtotal, total, and grand-total rows, and any header or footer rows.
- If multiple sheets are present, only pull real positions. Ignore summary, cover,
  performance, activity, and disclosure sheets entirely.
- Infer account_type from context (account name, sheet name, section headers):
  "Roth IRA" -> ROTH_IRA; "Roth 401(k)" -> ROTH_401K; "Rollover" -> ROLLOVER_IRA; "SEP" -> SEP_IRA;
  "Traditional"/plain "IRA" -> TRADITIONAL_IRA; "401(k)"/"401k" -> 401K; "403(b)" -> 403B; "457" -> 457B;
  "HSA" -> HSA; "529" -> 529; brokerage/individual/joint/trust or genuinely ambiguous -> TAXABLE.
- Mask account numbers to their last 4 digits if present.
- Use null for numbers and "" for strings that are not present in the source.
  NEVER fabricate, estimate, or compute a value that is not in the source.
- Strip currency symbols, commas, and percent signs from numbers.
- Return ONLY the JSON array. No markdown fences, no explanation, no prose.
  If there are no positions, return [].

Source:
{text}
"""


# ── LLM call ───────────────────────────────────────────────────────────────────

def _call_llm(prompt: str) -> str:
    """Try each model in the cheap 'flash' chain until one returns a
    non-empty response. Same litellm + failover pattern as
    ``validation_engine.weekly_pattern_analysis``."""
    import litellm
    from portfolio_agent._models import FAILOVER_CHAINS

    chain = FAILOVER_CHAINS.get("flash", [])
    for model_id, _provider, label in chain:
        try:
            resp = litellm.completion(
                model=model_id,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=8192,
            )
            text = (resp.choices[0].message.content or "").strip()
            if text:
                _log.info(f"  [llm_holdings] extracted with {label}",
                          event_type="info", model=label)
                return text
            _log.info(f"  [llm_holdings] {label} returned empty response — trying next",
                      event_type="info", model=label)
        except Exception as exc:
            _log.info(f"  [llm_holdings] {label} failed: {str(exc)[:200]} — trying next",
                      event_type="info", model=label)
            continue
    return ""


# ── Response parsing ───────────────────────────────────────────────────────────

def _parse_json_array(text: str) -> list[dict]:
    """Defensively pull a JSON array out of an LLM response."""
    if not text:
        return []
    cleaned = text.strip()

    # Strip ```json ... ``` / ``` ... ``` fences if the model ignored the instruction
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, re.DOTALL | re.IGNORECASE)
    if fence:
        cleaned = fence.group(1).strip()

    candidates = [cleaned]
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start != -1 and end > start:
        candidates.append(cleaned[start:end + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            # Some models wrap the array: {"holdings": [...]} / {"positions": [...]}
            for v in parsed.values():
                if isinstance(v, list):
                    parsed = v
                    break
        if isinstance(parsed, list):
            return [row for row in parsed if isinstance(row, dict)]
    return []


# ── Row normalisation ──────────────────────────────────────────────────────────

def _to_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return _clean_float(str(value))


def _to_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalise_account_type(value: Any, account_name: str = "") -> str:
    """Any account-type string the model (or a sheet) produced -> an AccountType value; unknown -> TAXABLE."""
    return normalize_account_type(_to_str(value), account_name)


def _mask_account_number(value: Any) -> str:
    """Enforce last-4 masking even if the model returned the full number."""
    s = _to_str(value)
    if not s:
        return ""
    core = s.lstrip("*xX•·#- ")
    if len(core) > 4:
        return "****" + core[-4:]
    return s


def _normalise_row(row: dict) -> Optional[dict]:
    ticker = _to_str(row.get("ticker") or row.get("symbol")).upper()
    if _is_invalid_ticker(ticker):
        return None

    account_name = _to_str(row.get("account_name"))
    return {
        "ticker":           ticker,
        "description":      _to_str(row.get("description")),
        "shares":           _to_float(row.get("shares")),
        "avg_cost":         _to_float(row.get("avg_cost")),
        "cost_basis_total": _to_float(row.get("cost_basis_total")),
        "current_price":    _to_float(row.get("current_price")),
        "current_value":    _to_float(row.get("current_value")),
        "account_name":     account_name,
        "account_number":   _mask_account_number(row.get("account_number")),
        "account_type":     _normalise_account_type(row.get("account_type"), account_name),
        "sector":           "",
    }


# ── Public entry point ─────────────────────────────────────────────────────────

def parse_holdings_with_llm(content: bytes, filename: str = "") -> list[dict]:
    """Extract holdings from an unrecognised CSV or Excel export via the LLM.

    Returns the canonical holdings rows, or [] if nothing could be extracted.
    Never raises.
    """
    try:
        text = _source_to_text(content, filename)
    except Exception as exc:
        _log.info(f"  [llm_holdings] could not read source ({filename or 'unnamed'}): {str(exc)[:200]}",
                  event_type="info")
        return []

    text = (text or "").strip()
    if not text:
        return []
    if len(text) > MAX_PROMPT_CHARS:
        text = text[:MAX_PROMPT_CHARS] + "\n... [truncated]"

    try:
        response = _call_llm(_build_prompt(text, filename))
    except Exception as exc:
        _log.info(f"  [llm_holdings] LLM call failed: {str(exc)[:200]}", event_type="info")
        return []
    if not response:
        return []

    results: list[dict] = []
    for raw in _parse_json_array(response):
        try:
            row = _normalise_row(raw)
        except Exception:
            continue
        if row is not None:
            results.append(row)

    _log.info(f"  [llm_holdings] {len(results)} position(s) extracted from {filename or 'upload'}",
              event_type="info", count=len(results))
    return results


# ══════════════════════════════════════════════════════════════════════════════
# Retirement-plan statements (401(k) / 403(b) / 457(b) PDFs)
#
# A separate prompt and normaliser: the brokerage CSV / Excel path above is unchanged. Provider-agnostic on purpose — recordkeepers label the same
# things differently ("Investment option" / "Fund" / "Investment"; "Units" / "Shares"; "Salary deferral" / "Employee pre-tax"; "Safe harbor match" /
# "Employer matching"), so the prompt describes WHAT to extract, never what one company's layout looks like, and the normaliser accepts the
# many names a model may give each field. Only REDACTED page text (pdf_statement.redact) is ever put in a prompt.
# ══════════════════════════════════════════════════════════════════════════════

from dataclasses import dataclass, field as _dc_field

from portfolio_agent.tools import pdf_statement as _pdf

STATEMENT_CHUNK_CHARS = 12_000          # a chunk of whole pages per LLM call (long per-source tables would overflow one answer)
STATEMENT_MAX_CHARS = 60_000            # beyond this the lowest-scoring pages are dropped (whole pages only; the cross-check then warns)

INSTRUMENT_HINTS = ("STOCK", "MUTUAL_FUND", "ETF", "TARGET_DATE_FUND", "CIT", "STABLE_VALUE", "MONEY_MARKET", "OTHER")
CONTRIBUTION_SOURCES = ("PRE_TAX", "ROTH", "EMPLOYER_MATCH", "AFTER_TAX", "ROLLOVER")
EMPLOYEE_SOURCES = ("PRE_TAX", "ROTH", "AFTER_TAX", "ROLLOVER")          # the participant's own money: always fully vested

_TICKER_OK = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_TOTAL_ROW = re.compile(r"(?i)^\s*(?:grand\s+|sub\s*)?total\b|^\s*(?:beginning|ending|opening|closing)\s+balance\b|^\s*total\s+(?:ending|account|plan)\b|"
                        r"^\s*(?:net\s+)?(?:contributions|transfers|loan|fees|withdrawals|distributions|gain|loss)\b.*(?:summary|total)\s*$")


def _statement_prompt(text: str, meta: "_pdf.StatementMeta", chunk_no: int, chunks: int) -> str:
    ctx = []
    if meta.plan_name:
        ctx.append(f"plan name: {meta.plan_name}")
    if meta.period_end:
        ctx.append(f"statement period: {meta.period_start or '?'} to {meta.period_end}")
    ctx_line = ("Known from the statement header: " + "; ".join(ctx) + ".") if ctx else ""
    part = f" (part {chunk_no} of {chunks}; a table may continue from the previous part)" if chunks > 1 else ""
    return f"""You are extracting the INVESTMENT BALANCES from a quarterly retirement-plan statement (401(k), 403(b) or 457(b)){part}.
Personal details have already been removed from the text. {ctx_line}

List EVERY investment the participant holds, as a JSON array. Each element has exactly these keys:

  "fund_name"           string  - the investment's name exactly as printed
  "ticker"              string  - the PUBLIC exchange ticker symbol, uppercase, ONLY if the statement prints one. Collective trusts, stable value /
                                  guaranteed funds, plan-specific funds and internal fund codes have NO ticker: use "". Never guess a ticker.
  "instrument_hint"     string  - one of "STOCK", "MUTUAL_FUND", "ETF", "TARGET_DATE_FUND", "CIT", "STABLE_VALUE", "MONEY_MARKET", "OTHER"
                                  (CIT = collective investment trust or commingled fund; company / employer stock = "STOCK")
  "shares"              number or null - shares or units held
  "unit_price"          number or null - price / unit value / NAV per share or unit
  "current_value"       number or null - the balance / market value of this line
  "contribution_source" string  - which money this line is, when the statement splits balances by source: "PRE_TAX" (salary deferral, employee
                                  pre-tax, traditional), "ROTH", "EMPLOYER_MATCH" (any employer money: match, safe harbor, profit sharing,
                                  non-elective), "AFTER_TAX", "ROLLOVER"; "" when the line is the fund's total or no source is given
  "vested_value"        number or null - the VESTED part of this line, only if the statement states it
  "account_name"        string  - the plan / account title, "" if not shown
  "account_number"      string  - the LAST 4 characters only (e.g. "****1234"), "" if not shown
  "account_type"        string  - "401K", "ROTH_401K", "403B", "457B" or "OTHER", from the plan type ("" if unclear)
  "as_of_date"          string  - the statement end date, YYYY-MM-DD, "" if not shown

Rules:
- Include stable value funds, guaranteed / fixed-account options, plan money-market and cash-reserve funds, collective trusts and company stock:
  they are real holdings. Do NOT skip a fund because it has no ticker.
- If balances are listed per fund AND per source (e.g. a fund shown separately for pre-tax, Roth and employer money), return one element per
  line as printed, with its contribution_source. If a fund appears once with a total, return one element with contribution_source "".
- Skip ONLY: totals and subtotals (including "total ending balance"), beginning/ending balance summary lines, loan balances, activity /
  transaction / contribution tables, performance and return tables, allocation percentages with no balance, fee tables and disclosures.
- Use null for a number and "" for a string that is not in the text. NEVER fabricate, estimate, convert or compute a value: if only a dollar
  balance is printed, shares stay null; if only units are printed, current_value stays null.
- Strip currency symbols, commas and percent signs from numbers. A number in parentheses is negative.
- Return ONLY the JSON array: no markdown fences, no explanation. If there are no investments in this text, return [].

Statement text:
{text}
"""


# ── field-name tolerance ──────────────────────────────────────────────────────

_ALIASES = {
    "fund_name": ("fund_name", "fund", "investment", "investment_name", "investment_option", "option", "name", "description", "security", "security_name"),
    "ticker": ("ticker", "symbol", "ticker_symbol"),
    "instrument_hint": ("instrument_hint", "hint", "instrument_type", "asset_type", "type", "fund_type"),
    "shares": ("shares", "units", "shares_units", "units_shares", "quantity", "number_of_units", "share_units"),
    "unit_price": ("unit_price", "price", "unit_value", "share_price", "nav", "price_per_unit", "price_per_share", "current_price"),
    "current_value": ("current_value", "value", "balance", "ending_balance", "market_value", "amount", "ending_value", "total_value"),
    "vested_value": ("vested_value", "vested_balance", "vested", "vested_amount"),
    "contribution_source": ("contribution_source", "source", "money_source", "money_type", "contribution_type", "source_type"),
    "as_of_date": ("as_of_date", "date", "statement_date", "as_of", "period_end"),
    "account_name": ("account_name", "plan_name", "plan", "account"),
    "account_number": ("account_number", "account_no", "account_num", "acct"),
    "account_type": ("account_type", "plan_type"),
}


def _pick(row: dict, field: str):
    low = {str(k).strip().lower().replace(" ", "_").replace("/", "_"): v for k, v in row.items()}
    for alias in _ALIASES[field]:
        if alias in low and low[alias] not in (None, ""):
            return low[alias]
    return None


def normalize_contribution_source(text: Any) -> str:
    """Any recordkeeper's label for a money source -> PRE_TAX | ROTH | EMPLOYER_MATCH | AFTER_TAX | ROLLOVER, or "" when it is a total / unknown.
    Employer money of every kind (match, safe harbor, profit sharing, non-elective) is EMPLOYER_MATCH. Order matters: "Roth rollover" is Roth."""
    t = re.sub(r"[_\-/]+", " ", _to_str(text)).lower()
    if not t or re.search(r"\b(total|all sources|combined)\b", t):
        return ""
    if "roth" in t:
        return "ROTH"
    if re.search(r"after ?tax|non ?roth after|voluntary after", t):
        return "AFTER_TAX"
    if re.search(r"roll ?over|rolled|transfer in|conversion", t):
        return "ROLLOVER"
    if re.search(r"employer|company|match|safe harbor|profit sharing|non ?elective|discretionary|qnec|qmac|er contrib", t):
        return "EMPLOYER_MATCH"
    if re.search(r"pre ?tax|before ?tax|deferral|salary|employee|elective|traditional|ee contrib|contribution", t):
        return "PRE_TAX"
    return ""


_HINT_BY_NAME = [
    ("STABLE_VALUE", r"stable value|guaranteed|capital preservation|fixed account|principal protection|income guarantee|fixed interest"),
    ("TARGET_DATE_FUND", r"target[- ]?(?:date|retirement|risk)|life ?cycle|life ?path|lifepath|retirement (?:income|20\d0)|\b20[2-6]0\b.*(?:fund|trust|portfolio)|freedom|"
                         r"\bin retirement\b"),
    ("MONEY_MARKET", r"money market|cash reserve|cash management|government (?:cash|reserves?)|treasury (?:cash|reserves?)|short[- ]term investment fund|\bstif\b"),
    ("STOCK", r"common stock|company stock|employer stock|\besop\b|\bstock fund\b.*\bcompany\b"),
    ("ETF", r"\betf\b|exchange[- ]traded"),
    ("CIT", r"\bcit\b|collective|commingled|common trust|\btrust\b|\bseparate account\b|\bcollective investment\b"),
]


def _hint_from_name(name: str, has_ticker: bool) -> str:
    low = name.lower()
    for hint, pat in _HINT_BY_NAME:
        if re.search(pat, low):
            return hint
    return "MUTUAL_FUND" if has_ticker else "OTHER"


def _normalise_statement_row(raw: dict, meta: "_pdf.StatementMeta") -> Optional[dict]:
    """
    One model answer element -> the statement row shape, or None. Kept: any row with a ticker OR a fund name that has shares or a value (a
    ticker-less fund is a real holding and is flagged ticker_less). Dropped: rows with neither, total / summary rows, rows with no shares and no value.
    Nothing is computed or defaulted into a number.
    """
    name = _to_str(_pick(raw, "fund_name"))
    ticker = _to_str(_pick(raw, "ticker")).upper().lstrip("$").strip()
    if ticker in ("N/A", "NA", "NONE", "--", "-", "NULL", "UNKNOWN") or not _TICKER_OK.match(ticker):
        ticker = ""
    if not ticker and not name:
        return None
    if name and _TOTAL_ROW.search(name) and not ticker:
        return None
    shares, price, value = _to_float(_pick(raw, "shares")), _to_float(_pick(raw, "unit_price")), _to_float(_pick(raw, "current_value"))
    if not shares and not value:                                       # nothing held on this line (also drops headers the model echoed)
        return None
    hint = _to_str(_pick(raw, "instrument_hint")).upper().replace(" ", "_").replace("-", "_")
    hint_source = "llm"
    if hint not in INSTRUMENT_HINTS:
        hint, hint_source = _hint_from_name(name, bool(ticker)), "name"
    dropped_ticker = None
    if ticker and hint in ("CIT", "STABLE_VALUE"):                     # a collective trust / stable value fund has no exchange ticker: never one to look up
        dropped_ticker, ticker = ticker, ""
    as_of = _to_str(_pick(raw, "as_of_date"))
    as_of = _pdf._parse_date(as_of) if as_of else None
    account_name = _to_str(_pick(raw, "account_name")) or (meta.plan_name or "")
    atype = _normalise_account_type(_pick(raw, "account_type"), account_name)
    if atype in ("TAXABLE", "OTHER") and re.search(r"(?i)403\s?\(?b\)?", account_name):
        atype = "403B"
    elif atype in ("TAXABLE", "OTHER") and re.search(r"(?i)457", account_name):
        atype = "457B"
    elif atype in ("TAXABLE", "OTHER"):
        atype = "401K"                                                  # a retirement-plan statement: the default plan type
    row = {
        "ticker": ticker, "ticker_less": not ticker, "fund_name": name or ticker, "description": name or ticker,
        "instrument_hint": hint, "hint_source": hint_source, "dropped_ticker": dropped_ticker,
        "shares": shares, "current_price": price, "current_value": value, "avg_cost": None, "cost_basis_total": None,
        "contribution_source": normalize_contribution_source(_pick(raw, "contribution_source")),
        "vested_value": _to_float(_pick(raw, "vested_value")),
        "as_of_date": as_of or meta.period_end,
        "account_name": account_name, "account_number": _mask_account_number(_pick(raw, "account_number")), "account_type": atype,
        "sector": "",
    }
    return row


def harmonise_accounts(lines: list[dict]) -> None:
    """
    Models often print the account number on some rows only. A statement is normally ONE account (plus its Roth split, which is added later), so
    a line with no account number takes the statement's number when exactly one distinct number appears; with several distinct numbers the lines
    are left as they are (several real accounts). Changes the lines in place.
    """
    numbers = {l["account_number"] for l in lines if l["account_number"]}
    if len(numbers) == 1:
        only = next(iter(numbers))
        for l in lines:
            if not l["account_number"]:
                l["account_number"] = only


# ── per-fund, per-account-type aggregation ────────────────────────────────────

def _fund_key(row: dict) -> str:
    name = re.sub(r"[^a-z0-9]+", " ", (row["fund_name"] or "").lower()).strip()
    return row["ticker"] or name


def aggregate_statement_rows(rows: list[dict]) -> list[dict]:
    """
    One row per (fund, account type). Money sources are split into accounts: ROTH -> the ROTH_401K account (when the plan is a 401(k); a 403(b) /
    457(b) keeps its Roth money in its own account type, there being no separate type), everything else -> the plan's account. Within a group:
      shares   the sum of the lines' shares, but ONLY when every line states shares (units are never derived from dollars);
      price    the lines' unit price when they agree (within 0.5%), else None;
      value    the sum of the lines' values, only when every line states one;
      vested   the sum of the lines' vested values, only when every line states one;
    and `sources` keeps the per-source lines (source, shares, value, vested_value) for display. A group that could not be summed says so in
    `partial` (e.g. "shares") rather than showing a number that is too low.
    """
    groups: dict[tuple, list[dict]] = {}
    order: list[tuple] = []
    for r in rows:
        atype = r["account_type"]
        if r["contribution_source"] == "ROTH" and atype == "401K":
            atype = "ROTH_401K"
        key = (atype, r["account_name"], r["account_number"], _fund_key(r))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append({**r, "_atype": atype})
    out: list[dict] = []
    for key in order:
        lines = groups[key]
        first = lines[0]
        partial: list[str] = []
        shares = None
        if all(l["shares"] is not None for l in lines):
            shares = round(sum(l["shares"] for l in lines), 6)
        elif any(l["shares"] is not None for l in lines):
            partial.append("shares")
        value = None
        if all(l["current_value"] is not None for l in lines):
            value = round(sum(l["current_value"] for l in lines), 2)
        elif any(l["current_value"] is not None for l in lines):
            partial.append("value")
        prices = [l["current_price"] for l in lines if l["current_price"] is not None]
        price = None
        if prices and (max(prices) - min(prices)) <= 0.005 * max(prices):
            price = prices[0]
        # Vested part of the position. EMPLOYEE money (pre-tax, Roth, after-tax, rollover) is 100% vested by law, so such a line counts in full; EMPLOYER
        # money counts only where the statement states its vested amount. If any line is employer money (or has no stated source) with no vested figure,
        # the position's vested value is unknown (None), never assumed.
        vested_parts, vested_known = [], True
        for l in lines:
            if l["vested_value"] is not None:
                vested_parts.append(l["vested_value"])
            elif l["contribution_source"] in EMPLOYEE_SOURCES and l["current_value"] is not None:
                vested_parts.append(l["current_value"])
            else:
                vested_known = False
        vested = round(sum(vested_parts), 2) if lines and vested_known else None
        atype = first["_atype"]
        plan_name = first["account_name"]
        name = first["account_name"]
        number = first["account_number"]
        if atype == "ROTH_401K":
            number = (number + "-ROTH") if number else ""
            name = f"{name} (Roth)" if name else "Roth"
        row = {k: v for k, v in first.items() if k not in ("_atype", "contribution_source")}
        row.update({"account_type": atype, "account_name": name, "account_number": number, "shares": shares, "current_value": value,
                    "current_price": price, "vested_value": vested, "partial": partial, "plan_name": plan_name,
                    "sources": [{"source": l["contribution_source"], "shares": l["shares"], "value": l["current_value"],
                                 "vested_value": l["vested_value"] if l["vested_value"] is not None else
                                 (l["current_value"] if l["contribution_source"] in EMPLOYEE_SOURCES else None),
                                 "vested_inferred": l["vested_value"] is None and l["contribution_source"] in EMPLOYEE_SOURCES} for l in lines]})
        out.append(row)
    return out


# ── cross-check ───────────────────────────────────────────────────────────────

def check_against_ending_balance(rows: list[dict], ending_balance: float | None, *, tolerance_pct: float = 0.5) -> dict:
    """
    Compare the rows' values with the statement's stated ending balance. Informational only (the preview shows it; nothing is blocked):
    {"ending_balance", "sum_of_rows", "difference", "ok", "missing_values"}. `ok` is None when there is no stated balance or no row value to
    sum; rows with no value are counted in `missing_values` (the sum is then known to be low). The difference may be real (a loan, an
    unvested amount shown elsewhere, pending activity), which is why it is a warning and not an error.
    """
    values = [r["current_value"] for r in rows if r.get("current_value") is not None]
    missing = sum(1 for r in rows if r.get("current_value") is None)
    total = round(sum(values), 2) if values else None
    out = {"ending_balance": ending_balance, "sum_of_rows": total, "difference": None, "ok": None, "missing_values": missing}
    if ending_balance is None or total is None:
        return out
    out["difference"] = round(ending_balance - total, 2)
    out["ok"] = abs(out["difference"]) <= max(1.0, ending_balance * tolerance_pct / 100.0)
    return out


# ── entry point ───────────────────────────────────────────────────────────────

@dataclass
class StatementParse:
    rows: list[dict]                                   # aggregated per fund per account type (ticker-less rows included)
    lines: list[dict]                                  # the normalised lines before aggregation (per source)
    meta: "_pdf.StatementMeta"
    check: dict
    pages: int = 0
    pages_read: int = 0
    warnings: list[str] = _dc_field(default_factory=list)
    loan_balance: float | None = None                  # plan-level, read from the statement text; a loan is NOT a holding
    vested_balance: float | None = None                # plan-level vested total, if the statement states one

    @property
    def ticker_less(self) -> list[dict]:
        return [r for r in self.rows if r["ticker_less"]]

    @property
    def with_ticker(self) -> list[dict]:
        return [r for r in self.rows if not r["ticker_less"]]


def _chunks(pages: list[str], max_chars: int) -> list[list[str]]:
    out, cur, size = [], [], 0
    for p in pages:
        if cur and size + len(p) > max_chars:
            out.append(cur)
            cur, size = [], 0
        cur.append(p)
        size += len(p)
    if cur:
        out.append(cur)
    return out


def parse_statement_with_llm(content: bytes) -> StatementParse:
    """
    A retirement-plan statement PDF -> StatementParse. Reads the text layer, picks the pages that hold positions, REDACTS them, asks the model
    (page-sized chunks), normalises and aggregates the answer, and cross-checks the total against the statement's stated ending balance.
    Raises pdf_statement.PdfStatementError / NoTextLayerError for an unreadable PDF and ValueError when the model returns no investments;
    otherwise never raises. Only redacted text reaches the model.
    """
    pages = _pdf.extract_pages(content)
    meta = _pdf.extract_statement_meta(pages)
    ending = _pdf.extract_ending_balance(pages)
    idx = _pdf.select_holdings_pages(pages)
    fitted = _pdf.fit_pages(pages, idx, STATEMENT_MAX_CHARS)
    warnings: list[str] = []
    if len(fitted) < len(idx):
        warnings.append(f"{len(idx) - len(fitted)} page(s) were left out because the statement is very long; the totals may not add up")
    texts = [f"--- Page {i + 1} ---\n{_pdf.redact(pages[i])}" for i in fitted]
    chunks = _chunks(texts, STATEMENT_CHUNK_CHARS)
    lines: list[dict] = []
    answered = 0
    for n, chunk in enumerate(chunks, start=1):
        try:
            response = _call_llm(_statement_prompt("\n\n".join(chunk), meta, n, len(chunks)))
        except Exception as exc:
            _log.info(f"  [llm_statement] LLM call failed: {str(exc)[:200]}", event_type="info")
            response = ""
        if not response:
            warnings.append(f"part {n} of {len(chunks)} of the statement could not be read" if len(chunks) > 1 else "the statement could not be read")
            continue
        answered += 1
        for raw in _parse_json_array(response):
            try:
                row = _normalise_statement_row(raw, meta)
            except Exception:
                continue
            if row is not None:
                lines.append(row)
    if not lines:
        raise ValueError("No investments could be read from this statement." + (" The model gave no answer." if not answered else ""))
    harmonise_accounts(lines)
    rows = aggregate_statement_rows(lines)
    for r in rows:
        if r["partial"]:
            warnings.append(f"{r['fund_name']}: " + " and ".join(r["partial"]) + " not stated on every line, so the total is left blank, not estimated")
        if r["shares"] and r["current_price"] and r["current_value"] and abs(r["shares"] * r["current_price"] - r["current_value"]) > max(1.0, 0.01 * r["current_value"]):
            warnings.append(f"{r['fund_name']}: units × price does not equal the balance as printed")
    loan = _pdf.extract_loan_balance(pages)
    vested_total = _pdf.extract_vested_balance(pages)
    check = check_against_ending_balance(rows, ending)
    if loan is not None and check.get("difference") and abs(abs(check["difference"]) - loan) <= max(1.0, loan * 0.005):
        check["loan_explains"] = True
    if check["ok"] is False and check.get("loan_explains"):
        warnings.append(f"The funds add up to {check['sum_of_rows']:,.2f}; the statement's ending balance is {check['ending_balance']:,.2f}. The difference "
                        f"({check['difference']:,.2f}) matches the plan loan balance of {loan:,.2f}, which is not a holding.")
    elif check["ok"] is False:
        warnings.append(f"The funds add up to {check['sum_of_rows']:,.2f} but the statement's ending balance is {check['ending_balance']:,.2f} "
                        f"(difference {check['difference']:,.2f}); a loan, pending activity or a missed fund could explain it")
    elif check["ok"] is None and ending is None:
        warnings.append("The statement does not state an ending balance that could be found, so the total was not cross-checked")
    for note in meta.notes:
        warnings.append(f"Statement header: {note}")
    _log.info(f"  [llm_statement] {len(rows)} fund row(s) ({sum(1 for r in rows if r['ticker_less'])} without a ticker) from {len(fitted)} page(s)",
              event_type="info", count=len(rows))
    return StatementParse(rows=rows, lines=lines, meta=meta, check=check, pages=len(pages), pages_read=len(fitted), warnings=warnings,
                          loan_balance=loan, vested_balance=vested_total)
