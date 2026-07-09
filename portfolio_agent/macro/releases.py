"""
Macro release tracker — fetches recent FRED data, classifies surprises,
persists to macro_releases table.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from portfolio_agent.macro.fred_client import get_historical_series, get_latest_value
from portfolio_agent.macro.series_registry import FRED_SERIES, TIER1_KEYS

_DB = Path(__file__).resolve().parents[3] / "data" / "portfolio.db"


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(_DB), timeout=10, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def ensure_macro_tables() -> None:
    """Idempotent — safe to call on every startup."""
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS macro_releases (
            release_id       TEXT PRIMARY KEY,
            series_id        TEXT NOT NULL,
            event_name       TEXT NOT NULL,
            released_at      TEXT NOT NULL,
            period_label     TEXT,
            actual_value     REAL,
            prior_value      REAL,
            consensus_value  REAL,
            surprise         REAL,
            surprise_severity INTEGER DEFAULT 0,
            yoy_change       REAL,
            mom_change       REAL,
            source           TEXT DEFAULT 'fred',
            raw_data         TEXT,
            UNIQUE(series_id, released_at)
        );

        CREATE INDEX IF NOT EXISTS idx_macro_releases_event
            ON macro_releases(event_name, released_at);
        CREATE INDEX IF NOT EXISTS idx_macro_releases_severity
            ON macro_releases(surprise_severity, released_at);

        CREATE TABLE IF NOT EXISTS macro_calendar (
            calendar_id   TEXT PRIMARY KEY,
            event_name    TEXT NOT NULL,
            scheduled_at  TEXT NOT NULL,
            period_label  TEXT,
            consensus_value REAL,
            importance    TEXT DEFAULT 'high',
            released      INTEGER DEFAULT 0,
            release_id    TEXT,
            source        TEXT DEFAULT 'computed',
            UNIQUE(event_name, scheduled_at)
        );

        CREATE INDEX IF NOT EXISTS idx_macro_calendar_upcoming
            ON macro_calendar(scheduled_at, released);
        """)


def classify_surprise(series_key: str,
                      actual: float,
                      prior: float,
                      consensus: Optional[float] = None) -> int:
    """
    Score surprise severity 0-3.
      0 = met expectations / no surprise
      1 = small deviation  (< 0.5σ)
      2 = moderate surprise (0.5–1.5σ)
      3 = major surprise   (> 1.5σ)

    Uses consensus when available; falls back to period-over-period deviation
    vs the series' typical sigma from the registry.
    """
    meta  = FRED_SERIES.get(series_key, {})
    sigma = meta.get("typical_monthly_sigma")

    if consensus is not None and sigma:
        deviation = abs(actual - consensus)
    elif sigma:
        deviation = abs(actual - prior)
    else:
        return 0

    if sigma == 0:
        return 0

    ratio = deviation / sigma
    if ratio > 1.5:
        return 3
    if ratio > 0.5:
        return 2
    if ratio > 0.1:
        return 1
    return 0


def _period_label(series_key: str, release_date: str) -> str:
    """Human-readable period label, e.g. 'May 2026', 'Q1 2026'."""
    try:
        d = date.fromisoformat(release_date)
    except ValueError:
        return release_date

    freq = FRED_SERIES.get(series_key, {}).get("frequency", "monthly")
    if freq == "quarterly":
        q = (d.month - 1) // 3 + 1
        return f"Q{q} {d.year}"
    if freq == "weekly":
        return f"Week of {d.strftime('%b %d, %Y')}"
    return d.strftime("%b %Y")


def fetch_recent_releases(lookback_days: int = 30) -> list[dict]:
    """
    For each Tier-1 series, fetch the latest two observations from FRED.
    Compute actual vs prior, classify surprise severity, and persist to DB.
    Returns the list of new or updated releases.
    """
    ensure_macro_tables()
    start = (date.today() - timedelta(days=lookback_days + 60)).isoformat()
    new_releases: list[dict] = []

    for series_key in TIER1_KEYS:
        meta      = FRED_SERIES[series_key]
        series_id = meta["id"]
        event_name = series_key

        try:
            obs = get_historical_series(series_id, start_date=start, observations=14)
            if not obs or "error" in obs[-1] or len(obs) < 2:
                continue

            latest = obs[-1]
            prior  = obs[-2]

            actual_val = latest["value"]
            prior_val  = prior["value"]
            release_dt = latest["date"]

            # Only process if within lookback window
            try:
                if (date.today() - date.fromisoformat(release_dt)).days > lookback_days:
                    continue
            except ValueError:
                continue

            # YoY: find ~12 months ago
            yoy_change: Optional[float] = None
            if len(obs) >= 13:
                year_ago_val = obs[-13]["value"]
                if year_ago_val and year_ago_val != 0:
                    yoy_change = round((actual_val - year_ago_val) / abs(year_ago_val) * 100, 2)

            mom_change: Optional[float] = None
            if prior_val and prior_val != 0:
                mom_change = round((actual_val - prior_val) / abs(prior_val) * 100, 3)

            severity = classify_surprise(series_key, actual_val, prior_val)

            release_id = hashlib.md5(
                f"{series_id}:{release_dt}".encode()
            ).hexdigest()[:16]

            raw = json.dumps({
                "series_key": series_key,
                "series_id":  series_id,
                "latest":     latest,
                "prior":      prior,
            })

            period = _period_label(series_key, release_dt)

            try:
                with _conn() as c:
                    c.execute(
                        """INSERT OR IGNORE INTO macro_releases
                           (release_id, series_id, event_name, released_at,
                            period_label, actual_value, prior_value,
                            surprise_severity, yoy_change, mom_change,
                            source, raw_data)
                           VALUES (?,?,?,?,?,?,?,?,?,?,'fred',?)""",
                        (release_id, series_id, event_name, release_dt,
                         period, actual_val, prior_val,
                         severity, yoy_change, mom_change, raw),
                    )
            except Exception:
                pass

            release = {
                "series_key":      series_key,
                "event_name":      event_name,
                "released_at":     release_dt,
                "period_label":    period,
                "actual_value":    actual_val,
                "prior_value":     prior_val,
                "yoy_change":      yoy_change,
                "mom_change":      mom_change,
                "surprise_severity": severity,
            }
            new_releases.append(release)

        except Exception:
            continue

    return new_releases


def get_recent_surprises(lookback_days: int = 30) -> list[dict]:
    """
    Query macro_releases for meaningful surprises (severity >= 1) within lookback.
    Returns up to 5, sorted newest first.
    """
    ensure_macro_tables()
    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
    try:
        with _conn() as c:
            rows = c.execute(
                """SELECT event_name, released_at, period_label,
                          actual_value, prior_value, yoy_change, mom_change,
                          surprise_severity
                   FROM macro_releases
                   WHERE released_at >= ? AND surprise_severity >= 1
                   ORDER BY released_at DESC
                   LIMIT 5""",
                [cutoff],
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
