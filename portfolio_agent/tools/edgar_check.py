"""
Cheap EDGAR filing check — no LLM, no API key required.

Uses SEC's public submissions JSON API to detect whether a new 10-K or 10-Q
has been filed since the last stored fundamentals run.

The CIK map (ticker→CIK) is cached locally for 24 h to avoid hitting SEC
servers on every call.  SEC asks for ≤10 requests/second; we use max_workers=4
so typical batch of 100 tickers completes in ~30s without throttling.
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import requests

_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
_CIK_CACHE_PATH = _DATA_DIR / "sec_cik_cache.json"
_CIK_CACHE_TTL = 86_400  # 24 h

_HEADERS = {
    "User-Agent": os.getenv("EDGAR_USER_AGENT", "PortfolioAgent contact@example.com"),
    "Accept-Encoding": "gzip, deflate",
}
_BASE = "https://data.sec.gov"
_FILING_TYPES = ("10-K", "10-Q")


# ── CIK map (cached) ───────────────────────────────────────────────────────────

def _load_cik_map() -> dict[str, str]:
    """Return ticker→zero-padded-CIK map, refreshing the local cache if stale."""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    if _CIK_CACHE_PATH.exists():
        age = time.time() - _CIK_CACHE_PATH.stat().st_mtime
        if age < _CIK_CACHE_TTL:
            return json.loads(_CIK_CACHE_PATH.read_text())

    resp = requests.get(
        "https://www.sec.gov/files/company_tickers.json",
        headers=_HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    cik_map = {
        entry["ticker"].upper(): str(entry["cik_str"]).zfill(10)
        for entry in resp.json().values()
    }
    _CIK_CACHE_PATH.write_text(json.dumps(cik_map))
    return cik_map


# ── single-ticker filing check ─────────────────────────────────────────────────

def get_latest_filing_info(
    ticker: str,
    filing_types: tuple[str, ...] = _FILING_TYPES,
) -> Optional[dict]:
    """
    Return metadata for the most recent 10-K or 10-Q filed by *ticker*.

    Returns:
        {filing_type, filing_date, period_of_report, accession, raw_filing_ref}
        or None if CIK not found or no matching filing exists.
    """
    cik_map = _load_cik_map()
    cik = cik_map.get(ticker.upper())
    if not cik:
        return None

    try:
        resp = requests.get(
            f"{_BASE}/submissions/CIK{cik}.json",
            headers=_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        sub = resp.json()
    except Exception:
        return None

    recent = sub.get("filings", {}).get("recent", {})
    forms    = recent.get("form", [])
    dates    = recent.get("filingDate", [])
    accnos   = recent.get("accessionNumber", [])
    periods  = recent.get("reportDate", [])

    for form, filing_date, acc, period in zip(forms, dates, accnos, periods):
        if form in filing_types:
            acc_clean = acc.replace("-", "")
            return {
                "filing_type":      form,
                "filing_date":      filing_date,
                "period_of_report": period,
                "accession":        acc,
                "raw_filing_ref":   (
                    f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_clean}/"
                ),
            }
    return None


# ── batch check ────────────────────────────────────────────────────────────────

def batch_check_filings(
    tickers: list[str],
    filing_types: tuple[str, ...] = _FILING_TYPES,
    max_workers: int = 4,
) -> dict[str, Optional[dict]]:
    """
    Fetch the latest filing info for every ticker in parallel.

    Args:
        tickers:      List of ticker symbols.
        filing_types: Form types to look for (default: 10-K and 10-Q).
        max_workers:  Thread-pool size; keep ≤4 to stay under SEC's 10 req/s limit.

    Returns:
        Dict mapping ticker → filing_info dict (or None if not found/error).
    """
    results: dict[str, Optional[dict]] = {}

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(get_latest_filing_info, t, filing_types): t
            for t in tickers
        }
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                results[ticker] = future.result()
            except Exception:
                results[ticker] = None

    return results
