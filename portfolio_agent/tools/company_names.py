"""
Ticker -> company name lookup.

Backed by the same free SEC bulk file edgar_check.py already downloads for
CIK lookups (https://www.sec.gov/files/company_tickers.json) — no per-ticker
network calls. Cached to disk for 24h and held in-process for 5 minutes so
pages rendering hundreds of tickers don't re-read/parse the file per ticker.

Coverage: SEC-registered US-listed companies only. ETFs, foreign ADRs, and
delisted tickers won't have a name — callers should fall back to the bare
ticker in that case.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

import requests

_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
_NAME_CACHE_PATH = _DATA_DIR / "sec_name_cache.json"
_DISK_CACHE_TTL = 86_400   # 24h, mirrors edgar_check's CIK cache
_MEM_CACHE_TTL  = 300      # 5 min in-process, avoids re-reading the file per ticker

_HEADERS = {
    "User-Agent": os.getenv("EDGAR_USER_AGENT", "PortfolioAgent contact@example.com"),
    "Accept-Encoding": "gzip, deflate",
}

_mem_cache: Optional[dict[str, str]] = None
_mem_cache_loaded_at: float = 0.0


def _load_name_map() -> dict[str, str]:
    global _mem_cache, _mem_cache_loaded_at
    now = time.time()
    if _mem_cache is not None and (now - _mem_cache_loaded_at) < _MEM_CACHE_TTL:
        return _mem_cache

    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    if _NAME_CACHE_PATH.exists():
        age = now - _NAME_CACHE_PATH.stat().st_mtime
        if age < _DISK_CACHE_TTL:
            _mem_cache = json.loads(_NAME_CACHE_PATH.read_text())
            _mem_cache_loaded_at = now
            return _mem_cache

    try:
        resp = requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers=_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        name_map = {
            entry["ticker"].upper(): entry["title"]
            for entry in resp.json().values()
        }
        _NAME_CACHE_PATH.write_text(json.dumps(name_map))
    except Exception:
        # Network/parse failure — fall back to a stale on-disk cache if any,
        # otherwise an empty map (callers just show the bare ticker).
        name_map = json.loads(_NAME_CACHE_PATH.read_text()) if _NAME_CACHE_PATH.exists() else {}

    _mem_cache = name_map
    _mem_cache_loaded_at = now
    return name_map


def get_company_name(ticker: str) -> Optional[str]:
    """Return the SEC-registered company name for *ticker*, or None if unknown."""
    return _load_name_map().get(ticker.upper())


def get_company_names(tickers: list[str]) -> dict[str, str]:
    """Batch lookup — one cache load regardless of how many tickers are asked for."""
    name_map = _load_name_map()
    return {t.upper(): name_map[t.upper()] for t in tickers if t.upper() in name_map}
