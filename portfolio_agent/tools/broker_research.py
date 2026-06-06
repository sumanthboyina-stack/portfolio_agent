"""
Broker / analyst research fetcher — zero LLM.

TRACK A — No LLM, parallel:
  Primary   (yfinance):  consensus, price targets, upgrades/downgrades, quarterly ratings
  Secondary (Finnhub):   monthly recommendation trend, independent price target

Finnhub adds two unique signals:
  rec_trend   — monthly buy/hold/sell counts (is consensus improving or deteriorating?)
  finnhub_pt  — independent PT for cross-validation against yfinance

`needs_research_refresh()` compares the latest upgrade/downgrade date
against the stored as_of_date so no LLM call is wasted unless something
actually changed (or data is older than max_age_days).
"""

from __future__ import annotations

import json
from datetime import date, timedelta, timezone, datetime
from typing import Optional

import pandas as pd
import yfinance as yf

from portfolio_agent.tools.finnhub_broker import fetch_broker_bundle


def _sf(v) -> Optional[float]:
    try:
        f = float(v)
        return None if pd.isna(f) else round(f, 4)
    except Exception:
        return None


def get_broker_research(ticker: str, upgrades_limit: int = 10) -> str:
    """
    Fetch broker / analyst data for *ticker* from Yahoo Finance (primary)
    and Finnhub (secondary).

    No LLM involved — pure API calls.

    Args:
        ticker:          Stock ticker symbol.
        upgrades_limit:  Max number of recent upgrade/downgrade rows to return.

    Returns:
        JSON: {
          ticker, as_of_date,
          consensus_key,            # "strong_buy" | "buy" | "hold" | "sell" | "strong_sell"
          consensus_mean,           # 1.0 (strong buy) … 5.0 (strong sell)
          num_analysts,
          target_mean, target_high, target_low, target_median, current_price,
          upside_to_mean_pct,
          latest_upgrade_date,      # ISO date of most recent firm action
          quarterly_ratings: [{period, strongBuy, buy, hold, sell, strongSell}],
          recent_upgrades:   [{date, firm, action, to_grade, from_grade,
                               new_target, prior_target}],
          rec_trend:         [{period, strongBuy, buy, hold, sell, strongSell,
                               total, pct_bullish}],  # Finnhub monthly snapshots
          finnhub_pt:        {target_high, target_low, target_mean, last_updated}
                              or null,                # Finnhub independent PT
        }
    """
    t = yf.Ticker(ticker)
    info = t.info or {}

    # Finnhub broker data (non-blocking — failures return empty gracefully)
    fh = fetch_broker_bundle(ticker)

    current_price  = _sf(info.get("currentPrice") or info.get("regularMarketPrice"))
    target_mean    = _sf(info.get("targetMeanPrice"))
    target_high    = _sf(info.get("targetHighPrice"))
    target_low     = _sf(info.get("targetLowPrice"))
    target_median  = _sf(info.get("targetMedianPrice"))
    consensus_key  = info.get("recommendationKey", "")
    consensus_mean = _sf(info.get("recommendationMean"))
    num_analysts   = info.get("numberOfAnalystOpinions")

    upside_pct: Optional[float] = None
    if current_price and target_mean:
        upside_pct = round((target_mean - current_price) / current_price * 100, 2)

    # ── quarterly rating distribution ─────────────────────────────────────────
    quarterly_ratings: list[dict] = []
    try:
        rec = t.recommendations
        if rec is not None and not rec.empty:
            for _, row in rec.sort_index().tail(4).iterrows():
                quarterly_ratings.append({
                    "period":     str(row.get("period", "")),
                    "strongBuy":  int(row.get("strongBuy", 0)),
                    "buy":        int(row.get("buy", 0)),
                    "hold":       int(row.get("hold", 0)),
                    "sell":       int(row.get("sell", 0)),
                    "strongSell": int(row.get("strongSell", 0)),
                })
    except Exception:
        pass

    # ── recent upgrades / downgrades ──────────────────────────────────────────
    recent_upgrades: list[dict] = []
    latest_upgrade_date: Optional[str] = None

    try:
        ud = t.upgrades_downgrades
        if ud is not None and not ud.empty:
            ud = ud.sort_index(ascending=False).head(upgrades_limit)
            for grade_dt, row in ud.iterrows():
                dt_str = str(grade_dt)[:10]  # ISO date portion
                recent_upgrades.append({
                    "date":          dt_str,
                    "firm":          str(row.get("Firm", "")),
                    "action":        str(row.get("Action", "")),
                    "to_grade":      str(row.get("ToGrade", "")),
                    "from_grade":    str(row.get("FromGrade", "")),
                    "new_target":    _sf(row.get("priceTarget")),
                    "prior_target":  _sf(row.get("priorPriceTarget")),
                })
            latest_upgrade_date = recent_upgrades[0]["date"] if recent_upgrades else None
    except Exception:
        pass

    return json.dumps({
        "ticker":              ticker.upper(),
        "as_of_date":          date.today().isoformat(),
        "consensus_key":       consensus_key,
        "consensus_mean":      consensus_mean,
        "num_analysts":        num_analysts,
        "target_mean":         target_mean,
        "target_high":         target_high,
        "target_low":          target_low,
        "target_median":       target_median,
        "current_price":       current_price,
        "upside_to_mean_pct":  upside_pct,
        "latest_upgrade_date": latest_upgrade_date,
        "quarterly_ratings":   quarterly_ratings,
        "recent_upgrades":     recent_upgrades,
        # Finnhub secondary signals
        "rec_trend":           fh.get("rec_trend", []),
        "finnhub_pt":          fh.get("finnhub_pt"),
    })


def needs_research_refresh(
    ticker: str,
    stored_as_of_date: Optional[str],
    stored_latest_upgrade: Optional[str],
    max_age_days: int = 30,
) -> tuple[bool, str]:
    """
    Pure-Python check — no LLM — whether research data needs to be refreshed.

    Compares the live latest_upgrade_date from yfinance against the stored value.

    Returns:
        (True, reason) or (False, "current")
    """
    if not stored_as_of_date:
        return True, "no_data"

    # Stale fallback: refresh if data is older than max_age_days
    try:
        age = (date.today() - date.fromisoformat(stored_as_of_date)).days
        if age > max_age_days:
            return True, f"stale_{age}d"
    except ValueError:
        return True, "bad_date"

    # Check for a newer firm action without calling the full fetcher
    try:
        ud = yf.Ticker(ticker).upgrades_downgrades
        if ud is not None and not ud.empty:
            latest = str(ud.index.max())[:10]
            if stored_latest_upgrade and latest > stored_latest_upgrade:
                return True, f"new_action_{latest}"
    except Exception:
        pass

    return False, "current"


def batch_check_research(
    tickers: list[str],
    stored: dict[str, dict],
    max_age_days: int = 30,
    max_workers: int = 8,
) -> dict[str, tuple[bool, str]]:
    """
    Check all tickers for research refresh in parallel (no LLM).

    Args:
        tickers:      List of ticker symbols.
        stored:       Map of ticker → stored_record dict (from research_db).
        max_age_days: Force refresh after this many days regardless.
        max_workers:  Thread-pool size.

    Returns:
        Dict mapping ticker → (needs_refresh: bool, reason: str).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _check(ticker: str) -> tuple[str, bool, str]:
        rec = stored.get(ticker, {})
        refresh, reason = needs_research_refresh(
            ticker,
            rec.get("as_of_date"),
            rec.get("latest_upgrade_date"),
            max_age_days,
        )
        return ticker, refresh, reason

    results: dict[str, tuple[bool, str]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_check, t): t for t in tickers}
        for f in as_completed(futures):
            try:
                ticker, refresh, reason = f.result()
                results[ticker] = (refresh, reason)
            except Exception:
                results[futures[f]] = (True, "error")
    return results
