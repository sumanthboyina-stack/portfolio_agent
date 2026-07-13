"""
FINRA short interest module.

Architecture:
  _latest_settlement_date()
      Deterministic computation (no network) — mirrors macro/calendar.py.
      FINRA publishes twice per month: ~15th and last business day.
      Data lands ~8 business days after the settlement date.

  fetch_short_interest_batch(tickers, settlement_date)
      One paginated API call to the FINRA Datasets API, filtered to the
      requested settlement date. Returns only rows whose ticker is in the
      given universe; the rest are discarded in Python, not the API.

  get_short_interest(ticker)
      Pre-computed trend summary read from the local DB.
      Falls back to yfinance Ticker.info (sharesShort / shortRatio)
      when the DB has no history for the ticker.

Auth: FINRA_API_KEY env var (Bearer token). Key: stored in env, not hardcoded.
"""

from __future__ import annotations

import json
import os
from calendar import monthrange
from datetime import date, timedelta
from typing import Optional

import requests

_FINRA_KEY   = os.getenv("FINRA_API_KEY", "")
_BASE        = "https://api.finra.org/data/group"
# "equityShortInterest" (OTC-only) was retired by FINRA in Sept 2022 — no longer
# published. "consolidatedShortInterest" is the current dataset and covers
# exchange-listed (NYSE/NASDAQ) names as well as OTC.
_DATASET     = "consolidatedShortInterest"
_GROUP       = "OTCMarket"
_HEADERS     = {"Authorization": f"Bearer {_FINRA_KEY}", "Accept": "application/json"}
_PAGE_LIMIT  = 5000        # FINRA max records per call
_PUB_LAG_BD  = 8           # business days between settlement date and publication


# ── Settlement date calendar (no network) ─────────────────────────────────────

def _prev_business_day(d: date) -> date:
    """Return *d* itself if a weekday, else the nearest prior weekday."""
    while d.weekday() >= 5:   # Sat=5, Sun=6
        d -= timedelta(days=1)
    return d


def _add_business_days(d: date, n: int) -> date:
    """Advance *d* by exactly *n* business days."""
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def _settlement_dates_for_month(year: int, month: int) -> list[date]:
    """
    Return the two FINRA settlement dates for a given (year, month):
      - Mid-month: 15th, rolled to the nearest prior weekday if needed.
      - End-of-month: last business day.
    """
    last_day = monthrange(year, month)[1]
    mid = _prev_business_day(date(year, month, 15))
    eom = _prev_business_day(date(year, month, last_day))
    return [mid, eom]


def _latest_settlement_date(today: Optional[date] = None) -> str:
    """
    Return the ISO date string of the most recent FINRA settlement date
    whose data has been published (publication = settlement + 8 business days).

    Scans the last two months of settlement dates so the function is always
    correct during the first days of a new month.

    Analogous to _compute_nfp_date() in macro/calendar.py — deterministic,
    no network, used only to decide whether a new pull is due.
    """
    today = today or date.today()

    candidates: list[date] = []
    for delta in range(-2, 1):       # two months back → current month
        m = today.month + delta
        y = today.year
        while m < 1:
            m += 12
            y -= 1
        while m > 12:
            m -= 12
            y += 1
        candidates.extend(_settlement_dates_for_month(y, m))

    published = [s for s in candidates if _add_business_days(s, _PUB_LAG_BD) <= today]

    # Fall back to the oldest candidate if nothing is published yet (rare edge case
    # at the very start of the month before the lag clears).
    return max(published).isoformat() if published else min(candidates).isoformat()


# ── FINRA Datasets API ─────────────────────────────────────────────────────────

def _fetch_page(settlement_date: str, offset: int) -> list[dict]:
    """Fetch one page of short interest records for *settlement_date*."""
    # NOTE: the FINRA Datasets API only honors compareFilters on POST (JSON
    # body); a GET with compareFilters in the query string is accepted (200)
    # but silently ignores the filter and returns unfiltered rows instead.
    body = {
        "limit":  _PAGE_LIMIT,
        "offset": offset,
        "compareFilters": [
            {"compareType": "equal", "fieldName": "settlementDate",
             "fieldValue": settlement_date},
        ],
        "fields": [
            "symbolCode", "settlementDate",
            "currentShortPositionQuantity", "previousShortPositionQuantity",
            "changePercent", "averageDailyVolumeQuantity", "daysToCoverQuantity",
        ],
    }
    resp = requests.post(
        f"{_BASE}/{_GROUP}/name/{_DATASET}",
        headers={**_HEADERS, "Content-Type": "application/json"},
        data=json.dumps(body),
        timeout=30,
    )
    if resp.status_code == 204:
        return []
    resp.raise_for_status()
    data = resp.json()
    # FINRA returns either a list directly or a dict with a "data" key
    return data if isinstance(data, list) else data.get("data", [])


def _row_to_dict(raw: dict, settlement_date: str) -> dict:
    """Normalise one FINRA API row into our internal shape."""
    def _int(v) -> Optional[int]:
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def _float(v) -> Optional[float]:
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    return {
        "ticker":           (raw.get("symbolCode") or "").upper().strip(),
        "settlement_date":  raw.get("settlementDate", settlement_date),
        "current_shares":   _int(raw.get("currentShortPositionQuantity")),
        "prev_shares":      _int(raw.get("previousShortPositionQuantity")),
        "pct_change":       _float(raw.get("changePercent")),
        "avg_daily_volume": _int(raw.get("averageDailyVolumeQuantity")),
        "days_to_cover":    _float(raw.get("daysToCoverQuantity")),
    }


def fetch_short_interest_batch(
    tickers: list[str],
    settlement_date: str,
) -> dict[str, dict]:
    """
    Pull ALL records for *settlement_date* from the FINRA Datasets API
    (paginated) and return only the rows whose ticker is in *tickers*.

    Downloading the full file and filtering locally is more robust than
    constructing a large IN filter — the total file is ~8 000 rows × ~200 B.

    Returns:
        {TICKER: {ticker, settlement_date, current_shares, prev_shares,
                  pct_change, avg_daily_volume, days_to_cover}}
        Missing tickers → not present in dict (use fallback).

    Raises:
        requests.HTTPError on API failure (caller decides whether to skip or abort).
    """
    universe = {t.upper() for t in tickers}
    result:  dict[str, dict] = {}
    offset = 0

    while True:
        page = _fetch_page(settlement_date, offset)
        if not page:
            break
        for raw in page:
            row = _row_to_dict(raw, settlement_date)
            if row["ticker"] and row["ticker"] in universe:
                result[row["ticker"]] = row
        if len(page) < _PAGE_LIMIT:
            break    # last page
        offset += _PAGE_LIMIT

    return result


# ── Public summary accessor ────────────────────────────────────────────────────

def get_short_interest(ticker: str) -> dict:
    """
    Return a pre-computed short interest trend summary for *ticker*.

    Priority:
      1. Local DB (FINRA data, 2-3 periods of history) — compute_trend()
      2. yfinance Ticker.info fallback (sharesShort, shortRatio, shortPercentOfFloat)

    This is the function the Research agent calls — it never hits the FINRA
    API directly; that happens once per settlement cycle in maybe_refresh().
    """
    from portfolio_agent.tools.short_interest_db import compute_trend

    summary = compute_trend(ticker)
    if summary.get("available"):
        return summary

    # ── yfinance fallback ──────────────────────────────────────────────────────
    try:
        import yfinance as yf
        info = yf.Ticker(ticker.upper()).info
        shares_short   = info.get("sharesShort")
        short_ratio    = info.get("shortRatio")     # same as days-to-cover
        short_pct_flt  = info.get("shortPercentOfFloat")

        if shares_short or short_ratio:
            dtc = float(short_ratio) if short_ratio else None
            squeeze = (
                "high"     if dtc and dtc >= 10 else
                "elevated" if dtc and dtc >= 5  else
                "low"      if dtc               else
                "unknown"
            )
            return {
                "ticker":           ticker.upper(),
                "available":        True,
                "source":           "yfinance",
                "current_shares":   int(shares_short) if shares_short else None,
                "days_to_cover":    dtc,
                "short_pct_float":  float(short_pct_flt) if short_pct_flt else None,
                "trend":            "unknown",     # no history for direction
                "squeeze_pressure": squeeze,
                "periods_available": 1,
            }
    except Exception:
        pass

    return {"ticker": ticker.upper(), "available": False}


# ── Pipeline refresh hook ──────────────────────────────────────────────────────

def maybe_refresh_short_interest(tickers: list[str], log=None) -> bool:
    """
    Check whether a new FINRA settlement period has published data since our
    last fetch; if so, pull it in one batch and store to DB.

    Called once per daily pipeline run. Cheap when no new data (one DB query);
    ~two paginated FINRA API calls when a new settlement date is available
    (expected ~twice per month).

    Returns True if data was refreshed, False if already current.
    """
    from portfolio_agent.tools.short_interest_db import needs_refresh, batch_upsert, mark_fetched
    from portfolio_agent.log import get_logger as _get_logger

    _log = log or _get_logger("short_interest")
    settlement_date = _latest_settlement_date()

    if not needs_refresh(settlement_date):
        _log.info(
            f"  [short_interest] Settlement {settlement_date} already fetched — skipping.",
            event_type="skip",
        )
        return False

    _log.info(
        f"  [short_interest] New settlement date {settlement_date} — fetching from FINRA…",
        event_type="fetch_start",
    )

    try:
        batch = fetch_short_interest_batch(tickers, settlement_date)
    except Exception as exc:
        _log.warning(
            f"  [short_interest] FINRA API failed ({exc!r}) — skipping this cycle.",
            event_type="warning",
        )
        return False

    if not batch:
        _log.warning(
            f"  [short_interest] FINRA returned 0 rows for {settlement_date} — "
            f"possibly pre-publication. Will retry next run.",
            event_type="warning",
        )
        return False

    written = batch_upsert(list(batch.values()))
    mark_fetched(settlement_date, ticker_count=written)

    _log.info(
        f"  [short_interest] Stored {written} rows for settlement {settlement_date}.",
        event_type="db_write",
    )
    return True
