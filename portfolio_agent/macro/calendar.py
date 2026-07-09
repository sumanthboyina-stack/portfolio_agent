"""
Macro event calendar — upcoming FOMC, NFP, CPI, and other high-impact events.

All dates are deterministically computed or hard-coded; no external API required
for FOMC/NFP/CPI. Finnhub economic calendar is layered on top when available.
"""
from __future__ import annotations

import json
import os
from calendar import monthrange
from datetime import date, timedelta
from typing import Optional


# ── FOMC hard-coded schedule (federalreserve.gov) ─────────────────────────────
# Dates are the *announcement day* (second day of the two-day meeting).
_FOMC_2026 = [
    date(2026, 1, 28),
    date(2026, 3, 18),
    date(2026, 4, 29),
    date(2026, 6, 17),
    date(2026, 7, 29),
    date(2026, 9, 16),
    date(2026, 10, 28),
    date(2026, 12, 16),
]

# Meetings with press conferences and updated Summary of Economic Projections (dot plot)
_FOMC_DOT_PLOT_2026 = {date(2026, 3, 18), date(2026, 6, 17),
                       date(2026, 9, 16), date(2026, 12, 16)}


def _get_fomc_calendar(days_ahead: int = 60) -> list[dict]:
    """Return upcoming FOMC meetings within the next N days."""
    today  = date.today()
    cutoff = today + timedelta(days=days_ahead)
    events = []
    for d in _FOMC_2026:
        if today <= d <= cutoff:
            events.append({
                "event":      "FOMC",
                "date":       d.isoformat(),
                "days_away":  (d - today).days,
                "importance": "high",
                "detail":     "Fed decision" + (" + dot plot" if d in _FOMC_DOT_PLOT_2026 else ""),
            })
    return events


def _compute_nfp_date(month: int, year: int) -> date:
    """NFP is released on the first Friday of the month (for the prior month)."""
    d = date(year, month, 1)
    # weekday(): Monday=0, Friday=4
    days_until_friday = (4 - d.weekday()) % 7
    return d + timedelta(days=days_until_friday)


def _compute_cpi_estimated_date(month: int, year: int) -> date:
    """
    CPI is typically released around the 10th–15th of the month, covering the
    prior month. Find the Tuesday closest to the 10th.
    """
    for day in range(8, 16):
        try:
            d = date(year, month, day)
            if d.weekday() == 1:  # Tuesday
                return d
        except ValueError:
            continue
    # Fallback: 12th
    return date(year, month, 12)


def _compute_pce_estimated_date(month: int, year: int) -> date:
    """PCE is released near the last business day of the month."""
    last_day = monthrange(year, month)[1]
    d = date(year, month, last_day)
    while d.weekday() >= 5:  # skip weekend
        d -= timedelta(days=1)
    return d - timedelta(days=2)  # typically 2-3 days before month end


def _nfp_cpi_calendar(days_ahead: int = 60) -> list[dict]:
    """Generate NFP and CPI release dates for current and next two months."""
    today  = date.today()
    cutoff = today + timedelta(days=days_ahead)
    events = []

    for delta_month in range(-1, 3):
        # compute target month
        m = today.month + delta_month
        y = today.year
        while m > 12:
            m -= 12
            y += 1
        while m < 1:
            m += 12
            y -= 1

        nfp_d = _compute_nfp_date(m, y)
        cpi_d = _compute_cpi_estimated_date(m, y)
        pce_d = _compute_pce_estimated_date(m, y)

        for ev_date, name, detail in [
            (nfp_d, "NFP",  "Nonfarm Payrolls"),
            (cpi_d, "CPI",  "Consumer Price Index"),
            (pce_d, "PCE",  "PCE Price Index (estimated)"),
        ]:
            if today <= ev_date <= cutoff:
                events.append({
                    "event":      name,
                    "date":       ev_date.isoformat(),
                    "days_away":  (ev_date - today).days,
                    "importance": "high",
                    "detail":     detail,
                })

    return events


def _finnhub_calendar(days_ahead: int = 14) -> list[dict]:
    """Attempt to pull upcoming events from Finnhub economic calendar."""
    api_key = os.getenv("FINNHUB_API_KEY", "")
    if not api_key:
        return []
    try:
        import requests
        today  = date.today()
        to_dt  = today + timedelta(days=days_ahead)
        resp   = requests.get(
            "https://finnhub.io/api/v1/calendar/economic",
            params={"from": today.isoformat(), "to": to_dt.isoformat(), "token": api_key},
            timeout=10,
        )
        resp.raise_for_status()
        raw = resp.json().get("economicCalendar", [])
        events = []
        for item in raw:
            ev_date_str = (item.get("time") or "")[:10]
            if not ev_date_str:
                continue
            try:
                ev_date = date.fromisoformat(ev_date_str)
            except ValueError:
                continue
            imp = item.get("impact", "low").lower()
            if imp not in ("high", "medium"):
                continue
            events.append({
                "event":      item.get("event", "Unknown"),
                "date":       ev_date.isoformat(),
                "days_away":  (ev_date - today).days,
                "importance": imp,
                "detail":     item.get("unit", ""),
                "source":     "finnhub",
            })
        return events
    except Exception:
        return []


def get_upcoming_events(days_ahead: int = 14) -> list[dict]:
    """
    Return upcoming high-impact macro events in the next N days.
    Combines FOMC (hard-coded), NFP/CPI/PCE (computed), and Finnhub (optional).
    Deduplicated and sorted by date.
    """
    events: list[dict] = []
    events.extend(_get_fomc_calendar(days_ahead))
    events.extend(_nfp_cpi_calendar(days_ahead))

    # Finnhub supplements with additional events
    fh = _finnhub_calendar(days_ahead)
    seen = {(e["event"], e["date"]) for e in events}
    for e in fh:
        if (e["event"], e["date"]) not in seen:
            events.append(e)
            seen.add((e["event"], e["date"]))

    events.sort(key=lambda e: e["date"])
    return events[:10]  # cap at 10 to keep prompt concise
