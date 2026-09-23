"""
Holdings price-maintenance service.

Orchestrates the market-data adapter (portfolio_agent.tools.market_data) and
the holdings repository (portfolio_agent.tools.holdings_db) for the three
jobs that keep stored prices current. Every provider call happens BEFORE the
database transaction opens, so the write lock is never held on the network.

    refresh_holding_prices()             — UI "Refresh prices" button
    snapshot_portfolio_prices(date)      — evening batch, one row per position
    backfill_missing_price_snapshots()   — morning/intraday batch, fill gaps
"""

from __future__ import annotations

from datetime import date as _date, timedelta

from portfolio_agent.services.context import system_context
from portfolio_agent.tools.holdings_db import (
    HISTORY_SOURCE_BACKFILL, HISTORY_SOURCE_SNAPSHOT,
    get_holdings, get_observed_dates, get_price_history_keys, history_row, insert_price_history,
    position_key, record_audit_event, transaction, update_position,
)
from portfolio_agent.tools.market_data import fetch_daily_closes, fetch_last_closes


def refresh_holding_prices() -> dict:
    """
    Re-fetch the latest close for EVERY holding (not just those missing a
    price) and stamp price_as_of with the trading date observed. Imported
    statement prices go stale silently otherwise. All rows update in one
    transaction, so a portfolio is never half-repriced.
    """
    holdings = get_holdings()
    tickers = sorted({h["ticker"] for h in holdings if h.get("ticker")})
    if not tickers:
        return {"updated": 0, "failed": [], "price_as_of": None}
    prices, as_of = fetch_last_closes(tickers)
    ctx = system_context("price_refresh")
    updated = 0
    with transaction() as conn:
        for h in holdings:
            cp = prices.get(h["ticker"])
            if cp is None or h.get("id") is None:
                continue
            shares = h.get("shares")
            updated += update_position(
                h["id"], conn=conn, touch=False, current_price=round(cp, 4),
                current_value=round(cp * shares, 2) if shares else None,
                price_as_of=as_of,
            )
        record_audit_event(actor=ctx.actor, source=ctx.source, request_id=ctx.request_id,
                           operation="refresh_prices", after={"updated": updated, "price_as_of": as_of,
                                                              "failed": sorted(set(tickers) - set(prices))},
                           conn=conn)
    return {"updated": updated, "failed": sorted(set(tickers) - set(prices)), "price_as_of": as_of}


def snapshot_portfolio_prices(as_of_date: str | None = None) -> dict:
    """
    Fetch the last close for every current holding and write one history row
    per position (date × account × ticker). Re-running on the same day
    refreshes each position's own row and nothing else.
    """
    today = as_of_date or _date.today().isoformat()
    holdings = get_holdings()
    if not holdings:
        return {"snapped": 0, "date": today}

    tickers = sorted({h["ticker"] for h in holdings if h.get("ticker")})
    prices, _ = fetch_last_closes(tickers)
    rows = [history_row(today, h, prices.get(h["ticker"]), HISTORY_SOURCE_SNAPSHOT)
            for h in holdings if h.get("ticker")]
    insert_price_history(rows, replace=True)
    return {"snapped": len(rows), "date": today}


def _business_days(start: _date, end: _date) -> list[str]:
    try:
        import pandas as pd
        return [d.date().isoformat() for d in pd.bdate_range(start=start, end=end)]
    except Exception:
        out, cur = [], start
        while cur <= end:
            if cur.weekday() < 5:
                out.append(cur.isoformat())
            cur += timedelta(days=1)
        return out


def backfill_missing_price_snapshots() -> dict:
    """
    For every trading day between the earliest holding load date and yesterday,
    find POSITIONS (broker, account, ticker) that have no history row, fetch the
    historical closes and insert the missing rows.

    Completeness is checked per position, not per date: a day with rows for
    some accounts but not others is partially covered and gets its gaps filled.
    Days that have an observed snapshot are never touched — the snapshot is the
    record of what was held at that close. Existing rows are never overwritten.
    Safe to call from any batch — it is a no-op when history is complete.
    """
    empty = {"backfilled": 0, "missing_days": 0, "missing_rows": 0}
    holdings = get_holdings()
    if not holdings:
        return empty

    yesterday = _date.today() - timedelta(days=1)
    date_strs = [d for d in ((h.get("as_of_date") or h.get("synced_at") or "")[:10] for h in holdings) if d]
    if not date_strs:
        return empty
    earliest = _date.fromisoformat(min(date_strs))
    if earliest > yesterday:
        return empty

    # Every position at its full grain — two accounts holding the same ticker
    # at one broker are two entries, never collapsed.
    position_map = {position_key(h): h for h in holdings if h.get("ticker")}
    covered = get_price_history_keys()
    observed = get_observed_dates()      # a snapshot day is complete: a position absent from it was not held

    missing: dict[str, list[tuple[str, str, str]]] = {}
    for d in _business_days(earliest, yesterday):
        if d in observed:
            continue
        gaps = [k for k in position_map if (d, *k) not in covered]
        if gaps:
            missing[d] = gaps
    if not missing:
        return empty
    missing_rows_total = sum(len(v) for v in missing.values())

    tickers = sorted({k[2] for gaps in missing.values() for k in gaps})
    start_str = min(missing)
    end_str = (_date.fromisoformat(max(missing)) + timedelta(days=1)).isoformat()
    try:
        closes = fetch_daily_closes(tickers, start_str, end_str)
    except Exception as exc:
        return {"backfilled": 0, "missing_days": len(missing),
                "missing_rows": missing_rows_total, "error": str(exc)}

    rows = []
    for d_str, gaps in missing.items():
        for key in gaps:
            cp = closes.get(key[2], {}).get(d_str)
            if cp is None:
                continue  # market holiday or no data — never write phantom rows
            rows.append(history_row(d_str, position_map[key], cp, HISTORY_SOURCE_BACKFILL))
    if rows:
        insert_price_history(rows, replace=False)
    return {"backfilled": len(rows), "missing_days": len(missing), "missing_rows": missing_rows_total}
