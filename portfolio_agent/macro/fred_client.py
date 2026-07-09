"""
FRED API client with 1-hour in-process cache.

Builds on top of the existing portfolio_agent/tools/fred.py HTTP layer
but adds structured return types, YoY/momentum helpers, and caching.

Requires FRED_API_KEY in the environment. Missing key → graceful degradation
(returns empty dicts with an 'error' key) so APEX runs are never blocked.
"""
from __future__ import annotations

import os
import time
from datetime import date, timedelta
from typing import Optional

import requests

_API_KEY  = os.getenv("FRED_API_KEY", "")
_BASE_URL = "https://api.stlouisfed.org/fred"
_CACHE_TTL = 3600  # 1 hour

# Simple dict cache: key → (timestamp, value)
_cache: dict[str, tuple[float, object]] = {}


def _cache_get(key: str):
    entry = _cache.get(key)
    if entry and (time.time() - entry[0]) < _CACHE_TTL:
        return entry[1]
    return None


def _cache_set(key: str, value) -> None:
    _cache[key] = (time.time(), value)


def _check_key() -> bool:
    if not _API_KEY:
        return False
    return True


def _fetch_observations(series_id: str, limit: int = 24,
                        observation_start: str = "") -> list[dict]:
    """
    Fetch FRED observations, returning [{date, value}] sorted oldest→newest.
    Fetches descending (most-recent first) so limit always captures the latest N
    records, then reverses to preserve chronological order.
    """
    cache_key = f"obs:{series_id}:{limit}:{observation_start}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached  # type: ignore[return-value]

    params: dict = {
        "series_id":   series_id,
        "api_key":     _API_KEY,
        "file_type":   "json",
        "sort_order":  "desc",   # newest first so limit grabs the latest N
        "limit":       limit,
    }
    if observation_start:
        params["observation_start"] = observation_start

    try:
        resp = requests.get(
            f"{_BASE_URL}/series/observations", params=params, timeout=15
        )
        resp.raise_for_status()
        obs = resp.json().get("observations", [])
        result = [
            {"date": o["date"], "value": float(o["value"])}
            for o in obs
            if o.get("value") not in (".", None, "")
        ]
        result.reverse()  # restore chronological order (oldest→newest)
    except Exception as exc:
        result = [{"error": str(exc)}]

    _cache_set(cache_key, result)
    return result


def _fetch_series_info(series_id: str) -> dict:
    """Fetch FRED series metadata (units, title, etc.)."""
    cache_key = f"info:{series_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached  # type: ignore[return-value]

    try:
        resp = requests.get(
            f"{_BASE_URL}/series",
            params={"series_id": series_id, "api_key": _API_KEY, "file_type": "json"},
            timeout=10,
        )
        resp.raise_for_status()
        sers = resp.json().get("seriess", [{}])
        info = sers[0] if sers else {}
        result = {
            "units":       info.get("units_short", ""),
            "title":       info.get("title", series_id),
            "frequency":   info.get("frequency_short", ""),
        }
    except Exception:
        result = {"units": "", "title": series_id, "frequency": ""}

    _cache_set(cache_key, result)
    return result


# ── Public API ────────────────────────────────────────────────────────────────

def get_latest_value(series_id: str) -> dict:
    """
    Fetch the latest value and date for a FRED series.

    Returns:
        {"series_id": "CPIAUCSL", "value": 312.3, "date": "2026-05-15", "units": "..."}
        or {"series_id": ..., "error": "..."} on failure / missing key.
    """
    if not _check_key():
        return {"series_id": series_id, "error": "FRED_API_KEY not configured"}

    obs = _fetch_observations(series_id, limit=2)
    if not obs or "error" in obs[-1]:
        return {"series_id": series_id, "error": obs[-1].get("error", "no data") if obs else "no data"}

    latest = obs[-1]
    info   = _fetch_series_info(series_id)
    return {
        "series_id": series_id,
        "value":     latest["value"],
        "date":      latest["date"],
        "units":     info.get("units", ""),
    }


def get_historical_series(series_id: str,
                          start_date: Optional[str] = None,
                          observations: int = 12) -> list[dict]:
    """
    Fetch historical observations, oldest first.

    Returns list of {"date": "...", "value": float} or [{"error": "..."}].
    """
    if not _check_key():
        return [{"error": "FRED_API_KEY not configured"}]

    if start_date is None:
        start_date = (date.today() - timedelta(days=observations * 35)).isoformat()

    obs = _fetch_observations(series_id, limit=observations, observation_start=start_date)
    return obs


def get_yoy_change(series_id: str) -> dict:
    """
    Compute year-over-year change.

    Returns:
        {"current": 312.3, "year_ago": 305.1, "yoy_pct": 2.4, "date": "..."}
        or {"error": "..."} on failure.
    """
    if not _check_key():
        return {"error": "FRED_API_KEY not configured"}

    start = (date.today() - timedelta(days=400)).isoformat()
    obs   = _fetch_observations(series_id, limit=15, observation_start=start)

    if not obs or len(obs) < 2 or "error" in obs[-1]:
        return {"error": "insufficient data"}

    current = obs[-1]["value"]
    current_dt = obs[-1]["date"]

    # Find observation ~12 months ago
    target_year_ago = (date.fromisoformat(current_dt) - timedelta(days=365)).isoformat()
    year_ago_obs = min(obs[:-1], key=lambda o: abs(
        (date.fromisoformat(o["date"]) - date.fromisoformat(target_year_ago)).days
    ))
    year_ago = year_ago_obs["value"]

    yoy_pct = round((current - year_ago) / abs(year_ago) * 100, 2) if year_ago else None
    return {
        "current":    current,
        "year_ago":   year_ago,
        "yoy_pct":    yoy_pct,
        "date":       current_dt,
        "year_ago_date": year_ago_obs["date"],
    }


def get_recent_change(series_id: str, periods: int = 3) -> dict:
    """
    Compute change over last N observations vs prior N observations (momentum).

    Returns:
        {"recent_avg": ..., "prior_avg": ..., "change_pct": ..., "trend": "..."}
    """
    if not _check_key():
        return {"error": "FRED_API_KEY not configured"}

    obs = _fetch_observations(series_id, limit=periods * 2 + 2)
    clean = [o for o in obs if "error" not in o]

    if len(clean) < periods * 2:
        return {"error": "insufficient data"}

    recent_vals = [o["value"] for o in clean[-periods:]]
    prior_vals  = [o["value"] for o in clean[-periods * 2:-periods]]

    recent_avg = sum(recent_vals) / len(recent_vals)
    prior_avg  = sum(prior_vals)  / len(prior_vals)

    change_pct = round((recent_avg - prior_avg) / abs(prior_avg) * 100, 2) if prior_avg else None

    if change_pct is None:
        trend = "unknown"
    elif change_pct > 1:
        trend = "rising"
    elif change_pct < -1:
        trend = "falling"
    else:
        trend = "stable"

    return {
        "recent_avg":  round(recent_avg, 4),
        "prior_avg":   round(prior_avg, 4),
        "change_pct":  change_pct,
        "trend":       trend,
    }
