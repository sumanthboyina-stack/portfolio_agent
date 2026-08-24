"""SEC EDGAR tools — fetches financial statements and filings."""

import html
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import requests

_HEADERS = {
    "User-Agent": os.getenv("EDGAR_USER_AGENT", "PortfolioAgent contact@example.com"),
    "Accept-Encoding": "gzip, deflate",
}
_BASE = "https://data.sec.gov"


def _cik_for_ticker(ticker: str) -> Optional[str]:
    url = "https://www.sec.gov/files/company_tickers.json"
    resp = requests.get(url, headers=_HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    ticker_upper = ticker.upper()
    for entry in data.values():
        if entry.get("ticker", "").upper() == ticker_upper:
            return str(entry["cik_str"]).zfill(10)
    return None


def _company_facts(cik: str) -> dict:
    url = f"{_BASE}/api/xbrl/companyfacts/CIK{cik}.json"
    resp = requests.get(url, headers=_HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.json()


def _is_annual_duration(entry: dict) -> bool:
    """
    True for instant facts (balance-sheet items like Assets/Cash — no "start",
    a point-in-time value, always valid) and for duration facts spanning a
    full fiscal year (~300-400 days).

    A 10-K's XBRL also tags each fiscal quarter's figures (for the "selected
    quarterly data" footnote) under the SAME concept and form="10-K" — with no
    duration filter, _latest_values previously mixed full-year revenue with
    same-year quarterly slices under different "end" dates (e.g. AAPL FY2018's
    Revenues concept: one true annual $265.6B entry plus four ~$50-90B
    quarterly entries, all form="10-K"), silently corrupting any multi-year
    trend built from "the n most recent values."
    """
    start, end = entry.get("start"), entry.get("end")
    if not start:
        return True  # instant fact — no duration to check
    try:
        from datetime import date as _date
        days = (_date.fromisoformat(end) - _date.fromisoformat(start)).days
    except (TypeError, ValueError):
        return True
    return 300 <= days <= 400


def _latest_values(facts: dict, concept: str, unit: str = "USD", n: int = 4) -> list[dict]:
    """Extract the n most recent annual values for a given XBRL concept."""
    try:
        entries = facts["facts"]["us-gaap"][concept]["units"][unit]
    except KeyError:
        return []
    annual = [
        e for e in entries
        if e.get("form") in ("10-K", "10-K/A") and "end" in e and _is_annual_duration(e)
    ]
    seen = set()
    deduped = []
    for e in sorted(annual, key=lambda x: x["end"], reverse=True):
        key = (e["end"], e.get("val"))
        if key not in seen:
            seen.add(key)
            deduped.append({"period": e["end"], "value": e["val"]})
        if len(deduped) >= n:
            break
    return deduped


_REVENUE_CONCEPTS = (
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
    "RevenuesNetOfInterestExpense",
)


def _best_revenue_series(facts: dict, n: int = 4) -> list[dict]:
    """
    Companies migrate which XBRL revenue concept they tag over time (most
    commonly the ASC 606 transition around fiscal 2018-2019, e.g. Apple moved
    from "Revenues" to "RevenueFromContractWithCustomerExcludingAssessedTax").
    An `or`-chain over _latest_values() picks the FIRST concept with any data
    at all, which can be years stale if the company's now-retired tag still
    has old non-empty results and the chain never reaches the current one
    (confirmed on AAPL: "Revenues" only has 2016-2018 data and short-circuited
    the chain, hiding "RevenueFromContractWithCustomerExcludingAssessedTax"'s
    2022-2025 data). Instead, try every candidate and keep whichever series's
    most recent period is actually the most recent.
    """
    best: list[dict] = []
    for concept in _REVENUE_CONCEPTS:
        series = _latest_values(facts, concept, n=n)
        if series and (not best or series[0]["period"] > best[0]["period"]):
            best = series
    return best


def get_income_statement(ticker: str) -> str:
    """
    Return key income-statement metrics (revenue, net income, EPS) for the
    last four annual periods for *ticker*.

    Args:
        ticker: Stock ticker symbol, e.g. "AAPL".

    Returns:
        JSON string with keys: ticker, revenue, net_income, eps_diluted.
    """
    cik = _cik_for_ticker(ticker)
    if not cik:
        return json.dumps({"error": f"CIK not found for ticker {ticker}"})
    facts = _company_facts(cik)
    result = {
        "ticker": ticker.upper(),
        "revenue": _best_revenue_series(facts),
        "net_income": _latest_values(facts, "NetIncomeLoss"),
        "eps_diluted": _latest_values(facts, "EarningsPerShareDiluted", unit="USD/shares"),
    }
    return json.dumps(result)


def get_balance_sheet(ticker: str) -> str:
    """
    Return key balance-sheet metrics for the last four annual periods.

    Args:
        ticker: Stock ticker symbol.

    Returns:
        JSON string with total_assets, total_liabilities, stockholders_equity, cash.
    """
    cik = _cik_for_ticker(ticker)
    if not cik:
        return json.dumps({"error": f"CIK not found for ticker {ticker}"})
    facts = _company_facts(cik)
    result = {
        "ticker": ticker.upper(),
        "total_assets": _latest_values(facts, "Assets"),
        "total_liabilities": _latest_values(facts, "Liabilities"),
        "stockholders_equity": _latest_values(facts, "StockholdersEquity"),
        "cash_and_equivalents": _latest_values(facts, "CashAndCashEquivalentsAtCarryingValue"),
    }
    return json.dumps(result)


def get_cash_flow(ticker: str) -> str:
    """
    Return operating, investing, and financing cash flow for the last four
    annual periods.

    Args:
        ticker: Stock ticker symbol.

    Returns:
        JSON string with operating_cf, investing_cf, financing_cf, capex.
    """
    cik = _cik_for_ticker(ticker)
    if not cik:
        return json.dumps({"error": f"CIK not found for ticker {ticker}"})
    facts = _company_facts(cik)
    result = {
        "ticker": ticker.upper(),
        "operating_cf": _latest_values(facts, "NetCashProvidedByUsedInOperatingActivities"),
        "investing_cf": _latest_values(facts, "NetCashProvidedByUsedInInvestingActivities"),
        "financing_cf": _latest_values(facts, "NetCashProvidedByUsedInFinancingActivities"),
        "capex": _latest_values(facts, "PaymentsToAcquirePropertyPlantAndEquipment"),
    }
    return json.dumps(result)


# Patterns tried in priority order — first match wins.
_GUIDANCE_PATTERNS = [
    # Specific guidance-block intros (highest signal — appear just before the numbers)
    re.compile(r"providing the following guidance", re.IGNORECASE),
    re.compile(r"(our|the company'?s?)\s+(fiscal|q[1-4]|second|third|fourth|first).{0,40}(guidance|outlook)", re.IGNORECASE),
    re.compile(r"(raises?|lowers?|reaffirms?|narrows?|updates?)\s+(its\s+)?(full.year|fiscal|annual|quarterly)\s+(guidance|outlook|forecast)", re.IGNORECASE),
    # Section headers (moderate signal)
    re.compile(r"\n\s*(Financial Outlook|Business Outlook|Outlook|Guidance)\s*\n", re.IGNORECASE),
    re.compile(r"(full.year \d{4}|fiscal \d{4} guid|q[1-4] \d{4} guid)", re.IGNORECASE),
]


def _extract_guidance_section(text: str, max_chars: int = 3000) -> str:
    """Extract the Outlook/Guidance section from a press release; fall back to head."""
    for pat in _GUIDANCE_PATTERNS:
        m = pat.search(text)
        if m:
            start = max(0, m.start() - 100)
            return text[start: start + max_chars].strip()
    return text[:max_chars].strip()


def get_earnings_guidance(ticker: str, cik_map: dict[str, str] | None = None) -> dict:
    """
    Fetch the EX-99.1 press-release text from the latest 8-K for *ticker* and
    return the guidance / outlook section (≤ 3 000 chars).

    Args:
        ticker:  Stock ticker symbol.
        cik_map: Optional pre-loaded ticker→CIK dict (avoids a redundant download).

    Returns:
        {ticker, guidance_text, guidance_date, filing_accession}
        or {ticker, guidance_text: None, error: str} on failure.
    """
    ticker  = ticker.upper()
    cik     = ((cik_map or {}).get(ticker)) or _cik_for_ticker(ticker)
    if not cik:
        return {"ticker": ticker, "guidance_text": None, "error": "no_cik"}
    cik_int = int(cik)

    # ── Find the latest 8-K ────────────────────────────────────────────────────
    try:
        sub = requests.get(f"{_BASE}/submissions/CIK{cik}.json",
                           headers=_HEADERS, timeout=15)
        sub.raise_for_status()
        recent     = sub.json().get("filings", {}).get("recent", {})
        forms      = recent.get("form", [])
        dates      = recent.get("filingDate", [])
        accessions = recent.get("accessionNumber", [])

        latest_acc  = None
        latest_date = None
        for form, dt, acc in zip(forms, dates, accessions):
            if form == "8-K":
                latest_acc  = acc
                latest_date = dt
                break
        if not latest_acc:
            return {"ticker": ticker, "guidance_text": None, "error": "no_8k"}
    except Exception as exc:
        return {"ticker": ticker, "guidance_text": None, "error": str(exc)}

    # ── Filing folder listing → EX-99.1 filename ──────────────────────────────
    # EDGAR doesn't expose a JSON document index; parse the HTML folder listing.
    acc_nodash = latest_acc.replace("-", "")
    folder_base = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}"
    _EX99_RE = re.compile(
        r'href="(/Archives/edgar/data/[^"]*(?:ex[\-_]?99[^"]*|EX[\-_]?99[^"]*)\.htm[^"]*)"',
        re.IGNORECASE,
    )
    try:
        folder = requests.get(f"{folder_base}/", headers=_HEADERS, timeout=15)
        folder.raise_for_status()
        matches = _EX99_RE.findall(folder.text)
        if not matches:
            return {"ticker": ticker, "guidance_text": None,
                    "guidance_date": latest_date, "error": "no_ex991"}
        exhibit_url = f"https://www.sec.gov{matches[0]}"
    except Exception as exc:
        return {"ticker": ticker, "guidance_text": None, "error": str(exc)}

    # ── Fetch, strip HTML, extract guidance section ────────────────────────────
    try:
        doc = requests.get(exhibit_url, headers=_HEADERS, timeout=20)
        doc.raise_for_status()
        text = doc.text
        if re.search(r"<html|<body", text, re.IGNORECASE):
            text = re.sub(r"<[^>]+>", " ", text)
        text = html.unescape(text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()

        return {
            "ticker":           ticker,
            "guidance_text":    _extract_guidance_section(text),
            "guidance_date":    latest_date,
            "filing_accession": latest_acc,
        }
    except Exception as exc:
        return {"ticker": ticker, "guidance_text": None,
                "guidance_date": latest_date, "error": str(exc)}


def get_sec_filings(ticker: str, form_type: str = "10-K", limit: int = 5) -> str:
    """
    Return the most recent SEC filings of a given form type.

    Args:
        ticker: Stock ticker symbol.
        form_type: SEC form type, e.g. "10-K", "10-Q", "8-K".
        limit: Maximum number of filings to return.

    Returns:
        JSON string with a list of filing metadata (date, accession number, URL).
    """
    cik = _cik_for_ticker(ticker)
    if not cik:
        return json.dumps({"error": f"CIK not found for ticker {ticker}"})
    url = f"{_BASE}/submissions/CIK{cik}.json"
    resp = requests.get(url, headers=_HEADERS, timeout=15)
    resp.raise_for_status()
    sub = resp.json()
    recent = sub.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    filings = []
    for form, date, acc in zip(forms, dates, accessions):
        if form == form_type:
            acc_clean = acc.replace("-", "")
            filings.append({
                "form": form,
                "date": date,
                "accession": acc,
                "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_clean}/",
            })
            if len(filings) >= limit:
                break
    return json.dumps({"ticker": ticker.upper(), "filings": filings})


# ── Batch pre-fetch (no LLM) ───────────────────────────────────────────────────

def get_fundamentals_bundle(ticker: str, cik_map: dict[str, str]) -> dict:
    """
    Pre-fetch ALL fundamentals data for one ticker using the shared CIK map.

    Fetches companyfacts ONCE (not 3× like the per-tool ADK approach) and
    extracts income, balance, and cash-flow data from the single response.
    Also fetches the latest 10-K filing metadata from the submissions endpoint.

    Args:
        ticker:  Stock ticker symbol.
        cik_map: Shared ticker→CIK dict (from edgar_check._load_cik_map).
                 Passing it in avoids repeated company_tickers.json downloads.

    Returns:
        {ticker, latest_10k, income, balance, cashflow}
        or {ticker, error: str} on failure.
    """
    ticker = ticker.upper()
    cik    = cik_map.get(ticker)
    if not cik:
        return {"ticker": ticker, "error": "no_cik"}

    # ── companyfacts: income + balance + cashflow from ONE download ───────────
    try:
        facts = _company_facts(cik)
    except Exception as exc:
        return {"ticker": ticker, "error": str(exc)}

    income = {
        "revenue":     _best_revenue_series(facts),
        "net_income":  _latest_values(facts, "NetIncomeLoss"),
        "eps_diluted": _latest_values(facts, "EarningsPerShareDiluted", unit="USD/shares"),
    }
    balance = {
        "total_assets":        _latest_values(facts, "Assets"),
        "total_liabilities":   _latest_values(facts, "Liabilities"),
        "stockholders_equity": _latest_values(facts, "StockholdersEquity"),
        "cash":                _latest_values(facts, "CashAndCashEquivalentsAtCarryingValue"),
    }
    cashflow = {
        "operating_cf": _latest_values(facts, "NetCashProvidedByUsedInOperatingActivities"),
        "investing_cf": _latest_values(facts, "NetCashProvidedByUsedInInvestingActivities"),
        "financing_cf": _latest_values(facts, "NetCashProvidedByUsedInFinancingActivities"),
        "capex":        _latest_values(facts, "PaymentsToAcquirePropertyPlantAndEquipment"),
    }

    # ── submissions: latest 10-K filing date ──────────────────────────────────
    latest_10k = None
    try:
        sub  = requests.get(f"{_BASE}/submissions/CIK{cik}.json", headers=_HEADERS, timeout=15)
        sub.raise_for_status()
        recent = sub.json().get("filings", {}).get("recent", {})
        for form, date in zip(recent.get("form", []), recent.get("filingDate", [])):
            if form == "10-K":
                latest_10k = {"type": form, "date": date}
                break
    except Exception:
        pass

    # ── 8-K / EX-99.1 guidance section ──────────────────────────────────────
    guidance = get_earnings_guidance(ticker, cik_map)

    return {
        "ticker":        ticker,
        "latest_10k":   latest_10k,
        "income":        income,
        "balance":       balance,
        "cashflow":      cashflow,
        "guidance_text": guidance.get("guidance_text"),
        "guidance_date": guidance.get("guidance_date"),
    }


def batch_prefetch_fundamentals(
    tickers: list[str],
    cik_map: dict[str, str],
    max_workers: int = 4,
) -> dict[str, dict]:
    """
    Pre-fetch fundamentals bundles for multiple tickers in parallel.

    Uses max_workers=4 to stay within SEC's ≤10 req/s guidance.
    Returns {ticker: bundle_dict}; failed tickers have {"error": ...}.
    """
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(get_fundamentals_bundle, t, cik_map): t
            for t in tickers
        }
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                results[ticker] = future.result()
            except Exception as exc:
                results[ticker] = {"ticker": ticker, "error": str(exc)}
    return results
