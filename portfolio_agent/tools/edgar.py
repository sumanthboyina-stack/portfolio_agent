"""SEC EDGAR tools — fetches financial statements and filings."""

import json
import os
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


def _latest_values(facts: dict, concept: str, unit: str = "USD", n: int = 4) -> list[dict]:
    """Extract the n most recent annual values for a given XBRL concept."""
    try:
        entries = facts["facts"]["us-gaap"][concept]["units"][unit]
    except KeyError:
        return []
    annual = [e for e in entries if e.get("form") in ("10-K", "10-K/A") and "end" in e]
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
        "revenue": _latest_values(facts, "Revenues") or _latest_values(facts, "RevenueFromContractWithCustomerExcludingAssessedTax"),
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
        "revenue":     (_latest_values(facts, "Revenues")
                        or _latest_values(facts, "RevenueFromContractWithCustomerExcludingAssessedTax")
                        or _latest_values(facts, "SalesRevenueNet")
                        or _latest_values(facts, "SalesRevenueGoodsNet")
                        or _latest_values(facts, "RevenuesNetOfInterestExpense")),
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

    return {
        "ticker":     ticker,
        "latest_10k": latest_10k,
        "income":     income,
        "balance":    balance,
        "cashflow":   cashflow,
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
