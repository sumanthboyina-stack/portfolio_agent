"""
Portfolio trend series — pure data-shaping, no Streamlit, no Plotly.

Turns account-level price-history rows (one per date × account × ticker) into
the prepared series the trend chart renders. The chart component draws what it
is given; every financial definition lives here:

  value                 how much the recorded holdings were worth each date
  cost                  recorded cost basis each date
  gain_abs / gain_pct   unrealized gain/loss = value − cost (and % of cost)
  recorded_change_pct   % change of recorded VALUE since the first date. This
                        moves when you buy or sell, so it is NOT a return.
  investment_return_pct time-weighted return that treats share-count changes
                        between consecutive observed snapshots as cash flows
                        (bought/sold at that day's close). None wherever either
                        endpoint of an interval is not an observed snapshot —
                        estimated (backfilled/reconstructed) rows carry today's
                        share counts, so they cannot reveal flows.
  observed              per-date flag: every row that day is an observed snapshot
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from portfolio_agent.tools.holdings_db import OBSERVED_SOURCES

RETURN_METHOD = (
    "Time-weighted: daily changes in each position's share count are treated as "
    "contributions or withdrawals at that day's close; the daily returns are chain-linked. "
    "Only computed across consecutive observed snapshots."
)


def build_trend_series(rows: list[dict]) -> dict:
    """
    rows: portfolio_price_history dicts (already filtered by broker/type if
    desired). Returns a dict of parallel lists keyed by `dates`, plus per-ticker
    recorded value and the investment-return series.
    """
    if not rows:
        return {"dates": [], "value": [], "cost": [], "gain_abs": [], "gain_pct": [],
                "recorded_change_pct": [], "investment_return_pct": [], "observed": [],
                "estimated_dates": [], "ticker_value": {}, "return_intervals": 0,
                "return_method": RETURN_METHOD}

    by_date: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_date[r["date"]].append(r)
    dates = sorted(by_date)

    value, cost, observed = [], [], []
    ticker_value: dict[str, dict[str, float]] = defaultdict(dict)
    for d in dates:
        mv = sum(r["market_value"] for r in by_date[d] if r.get("market_value") is not None)
        cb = sum(r["cost_basis"] for r in by_date[d] if r.get("cost_basis") is not None)
        value.append(mv)
        cost.append(cb)
        observed.append(all((r.get("source") or "snapshot") in OBSERVED_SOURCES for r in by_date[d]))
        for r in by_date[d]:
            if r.get("market_value") is not None:
                t = r["ticker"]
                ticker_value[t][d] = ticker_value[t].get(d, 0.0) + r["market_value"]

    gain_abs = [v - c for v, c in zip(value, cost)]
    gain_pct = [g / c * 100 if c else 0.0 for g, c in zip(gain_abs, cost)]
    base = value[0] or None
    recorded_change_pct = [(v - base) / base * 100 if base else 0.0 for v in value]

    # ── Flow-adjusted (time-weighted) return ──────────────────────────────────
    def _positions(d: str) -> dict[tuple, dict]:
        return {
            (r.get("broker") or "", r.get("account_number") or "", r["ticker"]): r
            for r in by_date[d]
        }

    ret: list[float | None] = [None] * len(dates)
    cumulative = None
    intervals = 0
    for i in range(1, len(dates)):
        d0, d1 = dates[i - 1], dates[i]
        if not (observed[i - 1] and observed[i]) or not value[i - 1]:
            cumulative = None          # break the chain; restart at next observed pair
            continue
        p0, p1 = _positions(d0), _positions(d1)
        flow = 0.0
        for key in set(p0) | set(p1):
            s0 = (p0.get(key) or {}).get("shares") or 0.0
            s1 = (p1.get(key) or {}).get("shares") or 0.0
            if s1 == s0:
                continue
            # value the share change at d1's close, falling back to d0's close
            # for a position fully sold before d1
            px = (p1.get(key) or {}).get("close_price") or (p0.get(key) or {}).get("close_price") or 0.0
            flow += (s1 - s0) * px
        r_day = (value[i] - value[i - 1] - flow) / value[i - 1]
        cumulative = ((1 + (cumulative or 0.0) / 100) * (1 + r_day) - 1) * 100
        if ret[i - 1] is None:
            ret[i - 1] = 0.0           # anchor the (re)started chain at 0%
        ret[i] = cumulative
        intervals += 1

    return {
        "dates": dates,
        "value": value,
        "cost": cost,
        "gain_abs": gain_abs,
        "gain_pct": gain_pct,
        "recorded_change_pct": recorded_change_pct,
        "investment_return_pct": ret,
        "observed": observed,
        "estimated_dates": [d for d, o in zip(dates, observed) if not o],
        "ticker_value": {t: dict(v) for t, v in ticker_value.items()},
        "return_intervals": intervals,
        "return_method": RETURN_METHOD,
    }


def normalize_index_series(index_series: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    """{symbol: {date: close}} → {symbol: {date: % change from first close}}."""
    out: dict[str, dict[str, float]] = {}
    for symbol, closes in (index_series or {}).items():
        if not closes:
            continue
        ds = sorted(closes)
        base = closes[ds[0]] or None
        out[symbol] = {d: ((closes[d] - base) / base * 100 if base else 0.0) for d in ds}
    return out
