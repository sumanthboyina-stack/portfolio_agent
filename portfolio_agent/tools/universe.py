"""
Ticker universe management — S&P 500/400/600 (Wikipedia) and Nasdaq full listed.

Tier assignment:
  extended — S&P 1500 (S&P 500 + S&P 400 + S&P 600); ~1,500 names
  broad    — Nasdaq/NYSE listed tickers not in S&P 1500; ~3,000–5,000 names

Refresh cadence: quarterly (90 days). Deterministic check via universe_db.needs_refresh().
Index data stored in universe_tickers SQLite table — not in config/watchlist.yaml.
"""

from __future__ import annotations

import logging
import re

import requests

_log = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; portfolio-agent/1.0)"}

_SP_URLS = {
    "sp500": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    "sp400": "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
    "sp600": "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies",
}

_NASDAQ_URL = (
    "https://api.nasdaq.com/api/screener/stocks"
    "?tableonly=true&limit=10000&offset=0&download=true"
)

_TIER_EXTENDED = "extended"
_TIER_BROAD = "broad"

_TICKER_RE = re.compile(r"^[A-Z]{1,5}$")


def _clean_ticker(raw: str) -> str:
    """Normalise a raw ticker string (replace dots with hyphens, strip)."""
    return re.sub(r"\.", "-", raw.strip()).upper()


# ── Wikipedia S&P constituent scraper ─────────────────────────────────────────

def _parse_html_table(html: str) -> list[list[str]]:
    """
    Parse the first <table id="constituents"> in *html*.
    Returns list of rows (each row = list of cell text strings).
    Handles <th> and <td>; skips empty rows.
    """
    table_m = re.search(
        r'<table[^>]+id=["\']constituents["\'][^>]*>(.*?)</table>',
        html,
        re.DOTALL | re.IGNORECASE,
    )
    if not table_m:
        return []
    table_html = table_m.group(1)
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, re.DOTALL | re.IGNORECASE)
    result = []
    for row in rows:
        cells = re.findall(r"<(?:td|th)[^>]*>(.*?)</(?:td|th)>", row, re.DOTALL | re.IGNORECASE)
        # Strip inner tags, decode basic entities
        clean = []
        for c in cells:
            text = re.sub(r"<[^>]+>", "", c)
            text = text.replace("&amp;", "&").replace("&#38;", "&").replace("&#160;", " ").strip()
            clean.append(text)
        if clean:
            result.append(clean)
    return result


def _fetch_sp_wikipedia(index_name: str) -> list[dict]:
    """
    Scrape S&P constituent table from Wikipedia.
    Returns list of {ticker, sector, index_name, tier}.
    """
    url = _SP_URLS[index_name]
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        _log.warning("Wikipedia GET failed for %s: %s", index_name, exc)
        return []

    try:
        rows = _parse_html_table(resp.text)
    except Exception as exc:
        _log.warning("Wikipedia parse failed for %s: %s", index_name, exc)
        return []

    if not rows:
        _log.warning("Wikipedia: no table found for %s", index_name)
        return []

    # Detect column indices from header row
    header = [h.lower() for h in rows[0]]
    sym_idx = next(
        (i for i, h in enumerate(header) if h in ("symbol", "ticker", "ticker symbol")),
        0,
    )
    sec_idx = next(
        (i for i, h in enumerate(header) if "sector" in h),
        None,
    )

    result = []
    for row in rows[1:]:
        if len(row) <= sym_idx:
            continue
        raw_ticker = _clean_ticker(row[sym_idx])
        if not raw_ticker or len(raw_ticker) > 10:
            continue
        sector = row[sec_idx].strip() if sec_idx is not None and len(row) > sec_idx else None
        result.append({
            "ticker":     raw_ticker,
            "index_name": index_name,
            "sector":     sector or None,
            "tier":       _TIER_EXTENDED,
        })

    return result


# ── Nasdaq screener API ────────────────────────────────────────────────────────

def _fetch_nasdaq_listed() -> list[dict]:
    """
    Pull all currently listed tickers from the Nasdaq screener API.
    No API key required. Returns list of {ticker, sector, market_cap_category, tier}.
    """
    try:
        resp = requests.get(_NASDAQ_URL, headers=_HEADERS, timeout=45)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        _log.warning("Nasdaq screener fetch failed: %s", exc)
        return []

    rows_raw = (data.get("data") or {}).get("table", {}).get("rows") or []
    result = []
    for r in rows_raw:
        raw = (r.get("symbol") or "").strip()
        ticker = _clean_ticker(raw)
        # Skip warrants, rights, units, ETNs with slashes, preferred shares w/ ^
        if not ticker or len(ticker) > 6 or not _TICKER_RE.match(ticker.replace("-", "")):
            continue
        result.append({
            "ticker":           ticker,
            "index_name":       "nasdaq_listed",
            "sector":           r.get("sector") or None,
            "market_cap_category": r.get("marketCap") or None,
            "tier":             _TIER_BROAD,
        })

    return result


# ── Orchestrator ──────────────────────────────────────────────────────────────

def refresh_universe(log=None) -> dict[str, int]:
    """
    Fetch S&P 500/400/600 (extended) and full Nasdaq listed universe (broad).
    Writes results to universe_tickers table.

    S&P members already in the Nasdaq list are upgraded to extended tier.
    Skips indexes that were refreshed within the last 90 days.

    Returns {index_name: ticker_count} for each index that was fetched.
    """
    from portfolio_agent.tools.universe_db import (
        upsert_batch, mark_refreshed, needs_refresh,
    )

    _l = log or _log
    summary: dict[str, int] = {}
    sp_tickers: set[str] = set()

    # ── S&P 1500 ──────────────────────────────────────────────────────────────
    all_sp_rows: list[dict] = []
    for index_name in ("sp500", "sp400", "sp600"):
        if not needs_refresh(index_name):
            _l.info(
                f"  [universe] {index_name} still fresh — skipping.",
                event_type="skip",
            )
            continue
        _l.info(
            f"  [universe] Fetching {index_name} from Wikipedia…",
            event_type="fetch_start",
        )
        rows = _fetch_sp_wikipedia(index_name)
        if not rows:
            _l.warning(
                f"  [universe] {index_name}: 0 rows returned",
                event_type="warning",
            )
            continue
        all_sp_rows.extend(rows)
        sp_tickers.update(r["ticker"] for r in rows)
        mark_refreshed(index_name, len(rows))
        summary[index_name] = len(rows)
        _l.info(
            f"  [universe] {index_name}: {len(rows)} tickers cached",
            event_type="db_write",
        )

    if all_sp_rows:
        upsert_batch(all_sp_rows)

    # ── Full Nasdaq listed ─────────────────────────────────────────────────────
    if needs_refresh("nasdaq_listed"):
        _l.info(
            "  [universe] Fetching full Nasdaq listed universe…",
            event_type="fetch_start",
        )
        nasdaq_rows = _fetch_nasdaq_listed()
        if nasdaq_rows:
            # Upgrade any S&P 1500 members found in Nasdaq list to extended tier
            for r in nasdaq_rows:
                if r["ticker"] in sp_tickers:
                    r["tier"] = _TIER_EXTENDED
            upsert_batch(nasdaq_rows)
            mark_refreshed("nasdaq_listed", len(nasdaq_rows))
            summary["nasdaq_listed"] = len(nasdaq_rows)
            n_broad = sum(1 for r in nasdaq_rows if r["tier"] == _TIER_BROAD)
            _l.info(
                f"  [universe] nasdaq_listed: {len(nasdaq_rows)} tickers "
                f"({n_broad} broad, {len(nasdaq_rows) - n_broad} extended)",
                event_type="db_write",
            )

    return summary
