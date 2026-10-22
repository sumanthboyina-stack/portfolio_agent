"""
Retirement-plan PDF statements (401(k) / 403(b) / 457(b)) — PDF to text, and the privacy step before any of it reaches a language model.

Provider-agnostic: nothing here is specific to one recordkeeper (the provider list below is only used to LABEL a statement; an unknown provider
is simply None).

  extract_pages(content)         every page's text layer, in order. Text PDFs only: a scanned / image-only PDF raises NoTextLayerError ("this PDF
                                 has no text layer ...") — there is no OCR. A password-protected, damaged or oversized PDF raises PdfStatementError.
  select_holdings_pages(pages)   which pages probably list positions (balance by investment, shares / units, price, ending balance ...), always with
                                 page 1 (statement period, plan name), a continuation page of a holdings table, and never the disclosure pages.
  find_holdings_pages(pages)     the text of those pages. Replaces blind truncation: whole pages are kept or dropped, never cut mid-table (fit_pages).
  redact(text)                   strips what must not leave the machine: SSNs (full and partial), full account numbers (last 4 kept), street
                                 addresses, phone numbers, dates of birth (labelled), email addresses and the participant's name after a label
                                 such as "Prepared for". Run on EVERY page before anything is sent to an LLM.
  extract_statement_meta(pages)  statement period start / end, plan name, provider — read locally from the first pages; a value that is not there is None.

Nothing is invented: every extractor returns None / [] for what it cannot find.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date

MAX_PDF_BYTES = 25 * 1024 * 1024
MAX_PAGES = 80                          # a quarterly plan statement is a few dozen pages at most
MIN_TEXT_CHARS = 40                     # fewer characters than this across the whole file = no usable text layer

_PDF_SIGNATURE = b"%PDF-"


class PdfStatementError(ValueError):
    """The PDF cannot be read (password-protected, damaged, too large, ...). The message is safe to show the person."""


class NoTextLayerError(PdfStatementError):
    """A scanned / image-only PDF: there is no text to read and this system does not run OCR."""


# ── Detection ─────────────────────────────────────────────────────────────────

def is_pdf(content: bytes, filename: str = "") -> bool:
    """True if the payload is a PDF, judged by extension and by the %PDF- signature (it may sit a few bytes in, after a BOM or whitespace)."""
    if (filename or "").lower().endswith(".pdf"):
        return True
    return isinstance(content, (bytes, bytearray)) and _PDF_SIGNATURE in bytes(content[:1024])


# ── PDF -> text ───────────────────────────────────────────────────────────────

def extract_pages(content: bytes) -> list[str]:
    """Text of every page ("" for a page with no text), in order. Raises NoTextLayerError / PdfStatementError (never anything else)."""
    if not isinstance(content, (bytes, bytearray)) or not content:
        raise PdfStatementError("the file is empty")
    if len(content) > MAX_PDF_BYTES:
        raise PdfStatementError(f"this PDF is larger than {MAX_PDF_BYTES // (1024 * 1024)} MB")
    if _PDF_SIGNATURE not in bytes(content[:1024]):
        raise PdfStatementError("this file is not a PDF")
    try:
        import pdfplumber
    except ImportError:                                           # pragma: no cover - requirements.txt installs it
        raise PdfStatementError("PDF support is not installed (pdfplumber)") from None
    pages: list[str] = []
    try:
        with pdfplumber.open(io.BytesIO(bytes(content))) as pdf:
            if len(pdf.pages) > MAX_PAGES:
                raise PdfStatementError(f"this PDF has {len(pdf.pages)} pages; the limit is {MAX_PAGES}")
            for page in pdf.pages:
                pages.append((page.extract_text() or "").strip())
    except PdfStatementError:
        raise
    except Exception as exc:                                      # pdfminer raises many types: password, syntax, recursion ...
        name = type(exc).__name__
        if "Password" in name or "Encrypted" in name or "encrypt" in str(exc).lower():
            raise PdfStatementError("this PDF is password-protected; remove the password and upload it again") from None
        raise PdfStatementError("this PDF could not be read (it may be damaged)") from None
    if sum(len(p) for p in pages) < MIN_TEXT_CHARS:
        raise NoTextLayerError("this PDF has no text layer (it looks like a scan or an image). Download the statement as a text PDF "
                               "from your plan's website instead; scanned statements are not supported.")
    return pages


# ── Which pages hold positions ────────────────────────────────────────────────

_HOLDINGS_KEYWORDS = [
    r"balance by investment", r"investment options?", r"investment (?:name|fund|detail|summary)", r"your investments?", r"\bholdings?\b",
    r"\bfunds?\b", r"shares\s*/\s*units|units\s*/\s*shares", r"\bshares\b", r"\bunits?\b", r"unit value|unit price|share price|\bprice\b",
    r"ending balance", r"balance by (?:money )?source", r"% of (?:total|account|portfolio)", r"asset allocation", r"market value",
]
_DISCLOSURE_MARKERS = [
    r"important (?:information|disclosures?|notice)", r"\bdisclosures?\b", r"\bglossary\b", r"this statement (?:is|has been)", r"past performance",
    r"not (?:a )?(?:guarantee|insured)", r"for (?:more )?information (?:about|regarding)", r"terms and conditions", r"privacy (?:policy|notice)",
]
_ACTIVITY_ONLY = re.compile(r"^\s*(?:transaction|account) (?:activity|history|summary)\b", re.I | re.M)
_AMOUNT = re.compile(r"(?<![\w.])\$?\(?\d{1,3}(?:,\d{3})*(?:\.\d{2,6})\)?(?![\w.])|(?<![\w.,])\$?\d+\.\d{2,6}(?![\w.])")
_MIN_SCORE = 3                                                    # distinct holdings keywords on the page
_MIN_AMOUNTS = 4                                                  # money-looking numbers: a table, not prose


def _score(text: str) -> tuple[int, int, int]:
    """(distinct holdings keywords, money-like numbers, disclosure markers) for one page."""
    low = text.lower()
    kw = sum(1 for k in _HOLDINGS_KEYWORDS if re.search(k, low))
    return kw, len(_AMOUNT.findall(text)), sum(1 for d in _DISCLOSURE_MARKERS if re.search(d, low))


def select_holdings_pages(pages: list[str]) -> list[int]:
    """
    Indexes (ascending) of the pages to read: page 1 always (statement period, plan name), then every page that looks like a holdings table
    (>= 3 distinct holdings keywords and >= 4 amounts, and not a disclosure page), then the page right after a selected one when it is a
    continuation (a table with many amounts and no disclosure text). A pure activity / transaction page is not selected unless it also has
    the holdings keywords.
    """
    chosen: set[int] = set()
    if pages:
        chosen.add(0)
    scores = [_score(p) for p in pages]
    for i, (kw, amt, disc) in enumerate(scores):
        if i == 0:
            continue
        looks_like_disclosure = disc >= 2 and amt < _MIN_AMOUNTS * 2
        if kw >= _MIN_SCORE and amt >= _MIN_AMOUNTS and not looks_like_disclosure:
            if _ACTIVITY_ONLY.search(pages[i]) and kw < _MIN_SCORE + 1:
                continue
            chosen.add(i)
    for i in sorted(chosen):                                      # continuation pages of a table that ran over
        j = i + 1
        while j < len(pages) and j not in chosen:
            kw, amt, disc = scores[j]
            if amt >= _MIN_AMOUNTS * 2 and disc == 0 and not _ACTIVITY_ONLY.search(pages[j]):
                chosen.add(j)
                j += 1
            else:
                break
    return sorted(chosen)


def fit_pages(pages: list[str], indexes: list[int], max_chars: int) -> list[int]:
    """
    Keep as many of the selected pages as fit in max_chars, whole pages only (a table is never cut in half). Page 1 is always kept; when the
    budget is short the pages with the fewest holdings keywords go first.
    """
    keep = list(indexes)
    def size(idx): return sum(len(pages[i]) for i in idx)
    while len(keep) > 1 and size(keep) > max_chars:
        droppable = [i for i in keep if i != 0]
        if not droppable:
            break
        worst = min(droppable, key=lambda i: (_score(pages[i])[0], _score(pages[i])[1]))
        keep.remove(worst)
    return sorted(keep)


def find_holdings_pages(pages: list[str], max_chars: int | None = None) -> list[str]:
    """The text of the pages select_holdings_pages picks (optionally fitted to max_chars with fit_pages)."""
    idx = select_holdings_pages(pages)
    if max_chars is not None:
        idx = fit_pages(pages, idx, max_chars)
    return [pages[i] for i in idx]


# ── Redaction ─────────────────────────────────────────────────────────────────

_SSN_FULL = re.compile(r"(?<!\d)\d{3}[- ]\d{2}[- ]\d{4}(?!\d)")
_SSN_PARTIAL = re.compile(r"(?<![\w*])(?:[*xX•]{3}|[*xX•]{5})[- ]?[*xX•]{2}[- ]?\d{4}(?!\d)")
_SSN_LABELLED = re.compile(r"(?i)\b(?:ssn|social security(?: number| no\.?| #)?)\s*[:#]?\s*[*xX•\d][*xX•\d -]{3,}\d")
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<![\w$.,-])(?:\+?1[ .-]?)?(?:\(\d{3}\)\s?|\d{3}[ .-])\d{3}[ .-]\d{4}(?![\d,]|\.\d)")
_DOB = re.compile(r"(?i)\b(date of birth|birth ?date|d\.?o\.?b\.?|born)\b\s*[:#-]?\s*"
                  r"(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|[A-Za-z]{3,9}\.? \d{1,2},? \d{4}|\d{4}-\d{2}-\d{2})")
_STREET_SUFFIX = (r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Court|Ct|Way|Place|Pl|Parkway|Pkwy|Circle|Cir|"
                  r"Terrace|Ter|Highway|Hwy|Trail|Trl|Square|Sq)")
_ADDRESS = re.compile(r"(?<![\w$.,])\d{1,6}[A-Za-z]?\s+(?:(?:N|S|E|W|NE|NW|SE|SW|North|South|East|West)\.?\s+)?(?:[A-Za-z0-9.'’-]+\s+){0,4}?"
                      + _STREET_SUFFIX + r"\b\.?(?:,?\s+(?:Apt|Apartment|Unit|Suite|Ste|Fl|Floor|#)\.?\s*[\w-]+)?")
_PO_BOX = re.compile(r"(?i)\bP\.?\s?O\.?\s+Box\s+\d+")
_CITY_STATE_ZIP = re.compile(r"\b[A-Z][A-Za-z.'’ -]{1,28},?\s+(?:[A-Z]{2})\s+\d{5}(?:-\d{4})?\b")
_ACCOUNT_LABELLED = re.compile(r"(?i)\b(account|acct|participant|plan|contract|policy)\s*(?:number|no\.?|num\.?|id|#)\s*[:#]?\s*"
                               r"((?=[A-Z0-9*•-]{5,26}(?![A-Z0-9*•-]))(?=[A-Z0-9*•-]*\d{3})[A-Z0-9*•-]+)")
_LONG_DIGITS = re.compile(r"(?<![\d$.,\w])(?:\d[ -]?){7,16}\d(?![\d]|[.,]\d)")
_NAME_WORD = r"[A-Z][A-Za-z.'’-]*"
_NAME_LABELLED = re.compile(rf"(?im)^(\s*(?:prepared for|participant(?: name)?|account holder|statement for|owner|name)\s*[:\-]?[ \t]*(?:\n[ \t]*)?)(?!plan\b)"
                            rf"({_NAME_WORD}(?: {_NAME_WORD}){{0,3}})[ \t]*$")
_DATE_COMPACT = re.compile(r"^(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])$")


def _mask_digits(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    return "****" + digits[-4:]


def _mask_labelled_account(m: re.Match) -> str:
    value = m.group(2)
    if re.fullmatch(r"[*xX•-]+\d{0,4}", value) or value.startswith(("****", "xxxx", "XXXX")):
        return m.group(0)                                                # already masked
    core = re.sub(r"[^A-Za-z0-9]", "", value)
    if len(core) <= 4:
        return m.group(0)
    return m.group(0).replace(value, "****" + core[-4:])


def redact(text: str) -> str:
    """
    Remove personal identifiers from statement text before it is sent anywhere:
      SSNs (123-45-6789, ***-**-6789, xxx-xx-6789, "SSN: ..."); account / participant / contract numbers (kept as ****1234, last four only);
      other long digit runs (8+ digits) the same way (not amounts: a number with a comma or a decimal point is left alone, as is a compact
      date such as 20260331); street addresses, PO boxes and "City, ST 12345" lines; phone numbers; email addresses; a labelled date of
      birth (the label is kept, the date is not); the participant's name after "Prepared for / Participant / Account holder / Statement for".
    Fund names, plan names, dates of the statement period, share counts, prices and balances are kept: they are what the extraction needs.
    """
    if not text:
        return ""
    t = text
    t = _SSN_LABELLED.sub("SSN: [SSN]", t)
    t = _SSN_FULL.sub("[SSN]", t)
    t = _SSN_PARTIAL.sub("[SSN]", t)
    t = _EMAIL.sub("[EMAIL]", t)
    t = _DOB.sub(lambda m: f"{m.group(1)}: [DOB]", t)
    t = _ACCOUNT_LABELLED.sub(_mask_labelled_account, t)
    t = _PO_BOX.sub("[ADDRESS]", t)
    t = _ADDRESS.sub("[ADDRESS]", t)
    t = _CITY_STATE_ZIP.sub("[ADDRESS]", t)
    t = _PHONE.sub("[PHONE]", t)
    t = _LONG_DIGITS.sub(lambda m: m.group(0) if _DATE_COMPACT.match(re.sub(r"\D", "", m.group(0))) else _mask_digits(m.group(0)), t)
    t = _NAME_LABELLED.sub(lambda m: f"{m.group(1)}[NAME]", t)
    return t


# ── Statement metadata ────────────────────────────────────────────────────────

_PROVIDERS = [
    ("Empower", r"\bEmpower\b"), ("Fidelity", r"\bFidelity\b"), ("Vanguard", r"\bVanguard\b"), ("Principal", r"\bPrincipal(?: Financial)?\b"),
    ("Voya", r"\bVoya\b"), ("Transamerica", r"\bTransamerica\b"), ("Prudential", r"\bPrudential\b"), ("T. Rowe Price", r"\bT\.? ?Rowe Price\b"),
    ("Schwab", r"\bSchwab\b"), ("MassMutual", r"\bMass ?Mutual\b"), ("John Hancock", r"\bJohn Hancock\b"), ("Nationwide", r"\bNationwide\b"),
    ("Lincoln Financial", r"\bLincoln Financial\b"), ("TIAA", r"\bTIAA\b"), ("Alight", r"\bAlight\b"), ("ADP", r"\bADP\b"),
    ("Guideline", r"\bGuideline\b"), ("Human Interest", r"\bHuman Interest\b"), ("Ascensus", r"\bAscensus\b"), ("Securian", r"\bSecurian\b"),
    ("Ameriprise", r"\bAmeriprise\b"), ("Mercer", r"\bMercer\b"), ("Newport", r"\bNewport\b"), ("Merrill", r"\bMerrill\b"),
    ("Principal", r"\bPrincipal\b"),
]
_MONTHS = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
_DATE_PAT = rf"(?:{_MONTHS}\.?\s+\d{{1,2}},?\s+\d{{4}}|\d{{1,2}}/\d{{1,2}}/\d{{2,4}}|\d{{4}}-\d{{2}}-\d{{2}})"
_PERIOD = re.compile(rf"(?i)(?:statement\s+period|period(?:\s+covered)?|for\s+the\s+period|reporting\s+period|quarter|from)?\s*[:\-]?\s*"
                     rf"({_DATE_PAT})\s*(?:-|–|—|to|through|thru)\s*({_DATE_PAT})")
_QUARTER_END = re.compile(rf"(?i)(?:quarter\s+end(?:ing|ed)|as\s+of|ending|period\s+end(?:ing|ed)?|through)\s*[:\-]?\s*({_DATE_PAT})")
_PLAN_LABELLED = re.compile(r"(?im)^\s*plan(?:\s+name)?\s*[:\-]\s*(.{4,90}?)\s*$")
_PLAN_LINE = re.compile(r"(?im)^\s*([A-Za-z0-9&.,'’() /-]{3,80}?(?:401\s?\(?k\)?|403\s?\(?b\)?|457\s?\(?b\)?|retirement (?:savings )?plan|savings plan|profit sharing plan)[A-Za-z0-9&.,'’() /-]{0,40}?)\s*$")


def _parse_date(raw: str) -> str | None:
    """ISO date from a statement date string; None if it is not one. Two-digit years are read as 20xx."""
    from dateutil import parser as dp
    try:
        d = dp.parse(raw.replace("Sept", "Sep"), dayfirst=False, fuzzy=False)
    except (ValueError, OverflowError):
        return None
    if d.year < 100:
        d = d.replace(year=2000 + d.year)
    return date(d.year, d.month, d.day).isoformat() if 1990 <= d.year <= 2100 else None


@dataclass
class StatementMeta:
    period_start: str | None = None
    period_end: str | None = None
    plan_name: str | None = None
    provider: str | None = None
    notes: list[str] = field(default_factory=list)          # what could not be found, for the preview

    def as_dict(self) -> dict:
        return {"period_start": self.period_start, "period_end": self.period_end, "plan_name": self.plan_name, "provider": self.provider}


def extract_statement_meta(pages: list[str]) -> StatementMeta:
    """Read the statement period, plan name and provider from the first two pages (locally; nothing is sent anywhere). Missing = None."""
    meta = StatementMeta()
    head = "\n".join(pages[:2])
    if not head.strip():
        meta.notes.append("no text on the first pages")
        return meta
    for name, pat in _PROVIDERS:
        if re.search(pat, head):
            meta.provider = name
            break
    for m in _PERIOD.finditer(head):
        a, b = _parse_date(m.group(1)), _parse_date(m.group(2))
        if a and b and a <= b:
            meta.period_start, meta.period_end = a, b
            break
    if meta.period_end is None:
        m = _QUARTER_END.search(head)
        if m:
            meta.period_end = _parse_date(m.group(1))                      # an end date alone: the start is NOT guessed
    m = _PLAN_LABELLED.search(head)
    if m:
        meta.plan_name = m.group(1).strip(" .:-")
    else:
        for m in _PLAN_LINE.finditer(head):
            cand = m.group(1).strip(" .:-")
            if not re.search(r"(?i)statement|summary|balance|page \d|account", cand):
                meta.plan_name = cand
                break
    for label, val in (("statement period", meta.period_end), ("plan name", meta.plan_name), ("provider", meta.provider)):
        if not val:
            meta.notes.append(f"{label} not found")
    return meta


# ── Ending balance (for the cross-check) ──────────────────────────────────────

_END_LABEL = re.compile(r"(?i)\b(?:total\s+)?(?:ending|closing)\s+(?:account\s+|plan\s+)?(?:balance|value)\b|\btotal\s+(?:account|plan)\s+(?:balance|value)\b|"
                        r"\bending\s+(?:account|plan)\s+value\b|\baccount\s+balance\s+as\s+of\b|\btotal\s+(?:value|balance)\s+of\s+(?:your\s+)?(?:account|investments)\b")
_MONEY_TOKEN = re.compile(r"\(?-?\$?\(?\d{1,3}(?:,\d{3})*(?:\.\d{2})\)?|\(?-?\$?\d+\.\d{2}\)?")


def _money_value(tok: str) -> float | None:
    neg = tok.strip().startswith("(") or "-" in tok
    digits = re.sub(r"[^\d.]", "", tok)
    try:
        v = float(digits)
    except ValueError:
        return None
    return -v if neg else v


def extract_ending_balance(pages: list[str]) -> float | None:
    """
    The statement's own stated ending balance, read from the first three pages with label patterns that recordkeepers share ("Ending balance",
    "Total ending balance", "Closing balance", "Total account value", ...). Taken from the line's LAST dollar amount. When several different
    values qualify, the value that appears most often wins (ties: the earliest on the earliest page). None if the statement states none —
    the cross-check then simply does not run; nothing is estimated.
    """
    found: list[float] = []
    for page in pages[:3]:
        for line in page.splitlines():
            if not _END_LABEL.search(line):
                continue
            toks = _MONEY_TOKEN.findall(line[_END_LABEL.search(line).end():])
            if not toks:
                continue
            v = _money_value(toks[-1])
            if v is not None and v > 0:
                found.append(round(v, 2))
    if not found:
        return None
    best = max(set(found), key=lambda x: (found.count(x), -found.index(x)))
    return best


# ── Loan and vested balances (plan-level facts, read locally) ─────────────────

_LOAN_LABEL = re.compile(r"(?i)\b(?:outstanding\s+)?(?:plan\s+)?loan(?:s)?(?:\s+(?:balance|principal|amount outstanding))?\b")
_VESTED_LABEL = re.compile(r"(?i)\b(?:total\s+)?vested\s+(?:account\s+)?(?:balance|value|amount)\b|\bvested\s+balance\b")


def _labelled_amount(pages: list[str], label: re.Pattern, *, require_balance_word: bool = False) -> float | None:
    found: list[float] = []
    for page in pages[:3]:
        for line in page.splitlines():
            m = label.search(line)
            if not m:
                continue
            if require_balance_word and not re.search(r"(?i)balance|outstanding|principal", line):
                continue
            toks = _MONEY_TOKEN.findall(line[m.end():])
            if toks:
                v = _money_value(toks[-1])
                if v is not None and v >= 0:
                    found.append(round(v, 2))
    if not found:
        return None
    return max(set(found), key=lambda x: (found.count(x), -found.index(x)))


def extract_vested_balance(pages: list[str]) -> float | None:
    """The statement's stated VESTED balance ("Vested balance $124,000.00"), from the first three pages; None if it states none (nothing inferred)."""
    return _labelled_amount(pages, _VESTED_LABEL)


def extract_loan_balance(pages: list[str]) -> float | None:
    """The statement's stated outstanding loan balance ("Outstanding loan balance $5,000.00"); None if there is none. A line that only mentions loans
    without "balance / outstanding / principal" and an amount is ignored. A loan is money borrowed from the plan: never a holding."""
    return _labelled_amount(pages, _LOAN_LABEL, require_balance_word=True)
