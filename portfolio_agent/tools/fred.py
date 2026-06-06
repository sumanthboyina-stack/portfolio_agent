"""FRED (Federal Reserve Economic Data) tools."""

import json
import os
from datetime import date, timedelta

import requests

_API_KEY = os.getenv("FRED_API_KEY", "")
_BASE = "https://api.stlouisfed.org/fred"


def _fred_series(series_id: str, limit: int = 12, observation_start: str = "") -> list[dict]:
    if not _API_KEY:
        return [{"error": "FRED_API_KEY not set"}]
    params = {
        "series_id": series_id,
        "api_key": _API_KEY,
        "file_type": "json",
        "sort_order": "desc",
        "limit": limit,
    }
    if observation_start:
        params["observation_start"] = observation_start
    resp = requests.get(f"{_BASE}/series/observations", params=params, timeout=15)
    resp.raise_for_status()
    obs = resp.json().get("observations", [])
    return [{"date": o["date"], "value": o["value"]} for o in obs if o["value"] != "."]


def get_fred_series(series_id: str, limit: int = 12) -> str:
    """
    Fetch raw observations for any FRED series.

    Args:
        series_id: FRED series identifier, e.g. "GDP", "UNRATE", "FEDFUNDS".
        limit: Number of most-recent observations to return.

    Returns:
        JSON string with series_id and list of {date, value} observations.
    """
    data = _fred_series(series_id, limit=limit)
    return json.dumps({"series_id": series_id, "observations": data})


def get_inflation_data(months: int = 24) -> str:
    """
    Return CPI (all items) and Core CPI YoY change for the last *months* months.

    Args:
        months: Number of months of history to return.

    Returns:
        JSON string with cpi and core_cpi observation lists.
    """
    start = (date.today() - timedelta(days=months * 31)).isoformat()
    return json.dumps({
        "cpi_all_items": _fred_series("CPIAUCSL", limit=months, observation_start=start),
        "core_cpi": _fred_series("CPILFESL", limit=months, observation_start=start),
    })


def get_interest_rates(months: int = 24) -> str:
    """
    Return Fed Funds rate, 2-year, 10-year, and 30-year Treasury yields.

    Args:
        months: Number of months of history to return.

    Returns:
        JSON string with fed_funds, t2y, t10y, t30y observation lists.
    """
    start = (date.today() - timedelta(days=months * 31)).isoformat()
    return json.dumps({
        "fed_funds": _fred_series("FEDFUNDS", limit=months, observation_start=start),
        "treasury_2yr": _fred_series("GS2", limit=months, observation_start=start),
        "treasury_10yr": _fred_series("GS10", limit=months, observation_start=start),
        "treasury_30yr": _fred_series("GS30", limit=months, observation_start=start),
    })


def get_unemployment_rate(months: int = 24) -> str:
    """
    Return US unemployment rate and labor force participation rate.

    Args:
        months: Number of months of history to return.

    Returns:
        JSON string with unemployment and labor_force_participation observation lists.
    """
    start = (date.today() - timedelta(days=months * 31)).isoformat()
    return json.dumps({
        "unemployment_rate": _fred_series("UNRATE", limit=months, observation_start=start),
        "labor_force_participation": _fred_series("CIVPART", limit=months, observation_start=start),
        "initial_jobless_claims": _fred_series("ICSA", limit=months, observation_start=start),
    })
