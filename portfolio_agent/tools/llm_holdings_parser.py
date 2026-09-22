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
    account_type      str          TAXABLE | IRA | ROTH_IRA | 401K
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

_VALID_ACCOUNT_TYPES = ("TAXABLE", "IRA", "ROTH_IRA", "401K")


# ── Input detection ────────────────────────────────────────────────────────────

def is_excel(content: bytes, filename: str = "") -> bool:
    """True if the payload is a spreadsheet, judged by extension first and
    file signature as a fallback (the filename may not be passed through)."""
    name = (filename or "").lower()
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
  "account_type"     string  - one of "TAXABLE", "IRA", "ROTH_IRA", "401K"

Rules:
- Skip cash, money-market, sweep, and "pending activity" line items (e.g. SPAXX, FDRXX,
  VMFXX, FCASH, "Cash & Cash Investments").
- Skip subtotal, total, and grand-total rows, and any header or footer rows.
- If multiple sheets are present, only pull real positions. Ignore summary, cover,
  performance, activity, and disclosure sheets entirely.
- Infer account_type from context (account name, sheet name, section headers):
  "Roth" -> ROTH_IRA; "Rollover"/"Traditional"/"SEP"/"IRA" -> IRA; "401(k)"/"401k" -> 401K;
  brokerage/individual/joint/trust or genuinely ambiguous -> TAXABLE.
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
    raw = _to_str(value).upper().replace("-", "_").replace(" ", "_")
    if raw in _VALID_ACCOUNT_TYPES:
        return raw
    combined = f"{raw} {account_name.upper()}"
    if "ROTH" in combined:
        return "ROTH_IRA"
    if "401" in combined:
        return "401K"
    if "IRA" in combined or "ROLLOVER" in combined or "TRADITIONAL" in combined or "SEP" in raw:
        return "IRA"
    return "TAXABLE"


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
