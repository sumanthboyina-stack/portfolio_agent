"""
Portfolio trend series — pure data-shaping, no Streamlit, no Plotly.

Turns account-level price-history rows (one per date × account × ticker) into
the prepared series the trend chart renders. The chart component draws what it
is given; every financial definition lives here:

  value                 securities plus ledger-managed account cash each date
  cost                  recorded cost basis each date
  gain_abs / gain_pct   unrealized gain/loss = value − cost (and % of cost)
  recorded_change_pct   % change of recorded VALUE since the first date. This
                        moves when you buy or sell, so it is NOT a return.
  investment_return_pct Modified Dietz return per observed interval, chain-
                        linked into a time-weighted return. Ledger accounts use
                        recorded external cash flows; snapshot accounts infer
                        flows from share-count changes at the close.
  observed              per-date flag: every row that day is an observed snapshot
"""

from __future__ import annotations

import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from portfolio_agent.tools.holdings_db import OBSERVED_SOURCES

RETURN_METHOD = (
    "Modified Dietz return for each observed-snapshot interval, chain-linked into a time-weighted return. "
    "Ledger-managed accounts include ledger cash, contributions, withdrawals, distributions, "
    "and transaction fees. Statement-managed accounts use share changes as estimated flows; "
    "cash and transaction fees are unavailable for those dates."
)


def _account_key(row: dict) -> tuple[str, str]:
    return (
        (row.get("broker") or "").strip().lower(),
        (row.get("account_number") or "").strip(),
    )


def build_trend_series(
    rows: list[dict],
    *,
    ledger_accounts: dict[tuple[str, str], dict] | None = None,
    transactions: list[dict] | None = None,
) -> dict:
    """
    rows: portfolio_price_history dicts (already filtered by owner, account,
    broker/type, and period if desired). `ledger_accounts` maps broker/account
    keys to ledger start-date details and can disable cash inclusion for
    security-only views; `transactions` contains effective owner-scoped ledger
    rows. Returns parallel daily series and return metadata.
    """
    ledger_accounts = {
        ((key[0] or "").strip().lower(), (key[1] or "").strip()): account
        for key, account in (ledger_accounts or {}).items()
    }
    transactions = transactions or []
    if not rows:
        return {"dates": [], "value": [], "cost": [], "gain_abs": [], "gain_pct": [],
                "recorded_change_pct": [], "investment_return_pct": [], "observed": [],
                "estimated_dates": [], "ticker_value": {}, "return_intervals": 0,
                "return_method": RETURN_METHOD, "ledger_cash": [], "ledger_fees": 0.0,
                "ledger_cash_coverage": 0, "return_start_date": None, "return_end_date": None}

    rows = [
        row for row in rows
        if _account_key(row) not in ledger_accounts
        or not ledger_accounts[_account_key(row)].get("ledger_start_date")
        or row["date"] >= ledger_accounts[_account_key(row)]["ledger_start_date"]
    ]
    if not rows:
        return {"dates": [], "value": [], "cost": [], "gain_abs": [], "gain_pct": [],
                "recorded_change_pct": [], "investment_return_pct": [], "observed": [],
                "estimated_dates": [], "ticker_value": {}, "return_intervals": 0,
                "return_method": RETURN_METHOD, "ledger_cash": [], "ledger_fees": 0.0,
                "ledger_cash_coverage": 0, "return_start_date": None, "return_end_date": None}

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

    txns_by_account: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for txn in transactions:
        key = _account_key(txn)
        if key in ledger_accounts and ledger_accounts[key].get("include_cash", True):
            start = ledger_accounts[key].get("ledger_start_date")
            if not start or txn["date"] >= start:
                txns_by_account[key].append(txn)
    for account_txns in txns_by_account.values():
        account_txns.sort(key=lambda txn: (txn["date"], txn.get("txn_id") or 0))

    ledger_cash: list[float] = []
    cash_by_date: dict[str, float] = {}
    for key in ledger_accounts:
        if not ledger_accounts[key].get("include_cash", True):
            continue
        account_txns = txns_by_account.get(key, [])
        cash = 0.0
        txn_index = 0
        for d in dates:
            while txn_index < len(account_txns) and account_txns[txn_index]["date"] <= d:
                cash += float(account_txns[txn_index].get("amount") or 0.0)
                txn_index += 1
            cash_by_date[d] = cash_by_date.get(d, 0.0) + cash
    for i, d in enumerate(dates):
        ledger_cash.append(cash_by_date.get(d, 0.0))
        value[i] += ledger_cash[-1]

    securities_value = list(value)
    for i, cash in enumerate(ledger_cash):
        securities_value[i] -= cash

    external_flows: dict[str, float] = defaultdict(float)
    ledger_fees = 0.0
    for account_txns in txns_by_account.values():
        for txn in account_txns:
            kind = txn["type"]
            if kind in ("DEPOSIT", "WITHDRAWAL"):
                external_flows[txn["date"]] += float(txn.get("amount") or 0.0)
            elif kind in ("EMPLOYER_MATCH", "OPENING_BALANCE"):
                amount = txn.get("amount") or 0.0
                if txn.get("quantity") is not None and txn.get("price") is not None:
                    amount = float(txn["quantity"]) * float(txn["price"])
                external_flows[txn["date"]] += float(amount)
            ledger_fees += float(txn.get("fees") or 0.0)
            if kind == "FEE":
                ledger_fees += abs(float(txn.get("amount") or 0.0))

    gain_abs = [v - c for v, c in zip(securities_value, cost)]
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
    return_start_index = None
    for i in range(1, len(dates)):
        d0, d1 = dates[i - 1], dates[i]
        if not (observed[i - 1] and observed[i]) or not value[i - 1]:
            cumulative = None          # break the chain; restart at next observed pair
            intervals = 0
            continue
        p0, p1 = _positions(d0), _positions(d1)
        interval_flows: list[tuple[float, str]] = [
            (amount, day) for day, amount in external_flows.items() if d0 < day <= d1
        ]
        for key in p0.keys() | p1.keys():
            acct_key = ((key[0] or "").strip().lower(), (key[1] or "").strip())
            if acct_key in ledger_accounts and ledger_accounts[acct_key].get("include_cash", True):
                continue
            s0 = (p0.get(key) or {}).get("shares") or 0.0
            s1 = (p1.get(key) or {}).get("shares") or 0.0
            if s1 == s0:
                continue
            # value the share change at d1's close, falling back to d0's close
            # for a position fully sold before d1
            px = (p1.get(key) or {}).get("close_price") or (p0.get(key) or {}).get("close_price") or 0.0
            interval_flows.append(((s1 - s0) * px, d1))
        duration = max(1, (date.fromisoformat(d1) - date.fromisoformat(d0)).days)
        flow = sum(amount for amount, _ in interval_flows)
        weighted_flow = sum(
            amount * max(0.0, (date.fromisoformat(d1) - date.fromisoformat(day)).days / duration)
            for amount, day in interval_flows
        )
        denominator = value[i - 1] + weighted_flow
        if denominator <= 0:
            cumulative = None
            intervals = 0
            continue
        r_day = (value[i] - value[i - 1] - flow) / denominator
        cumulative = ((1 + (cumulative or 0.0) / 100) * (1 + r_day) - 1) * 100
        if ret[i - 1] is None:
            ret[i - 1] = 0.0           # anchor the (re)started chain at 0%
            return_start_index = i - 1
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
        "ledger_cash": ledger_cash,
        "ledger_fees": round(ledger_fees, 2),
        "ledger_cash_coverage": sum(
            account.get("include_cash", True) for account in ledger_accounts.values()
        ),
        "return_start_date": dates[return_start_index] if return_start_index is not None else None,
        "return_end_date": dates[max((i for i, value in enumerate(ret) if value is not None), default=0)]
        if return_start_index is not None else None,
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


def benchmark_returns(
    index_series: dict[str, dict[str, float]],
    start_date: str,
    end_date: str,
) -> dict[str, dict]:
    """Return benchmark changes over the requested window using available closes within it."""
    out = {}
    for symbol, closes in (index_series or {}).items():
        window = {
            day: float(close)
            for day, close in closes.items()
            if start_date <= day <= end_date and close is not None
        }
        days = sorted(window)
        if not days or not window[days[0]]:
            continue
        first, last = days[0], days[-1]
        out[symbol] = {
            "return_pct": (window[last] / window[first] - 1) * 100,
            "start_date": first,
            "end_date": last,
            "series_pct": {
                day: (window[day] / window[first] - 1) * 100
                for day in days
            },
        }
    return out
