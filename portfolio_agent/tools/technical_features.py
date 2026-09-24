"""
Instrument-level technical calculations and storage — extracted out of
pipeline.daily.risk_technical, which folds Technical into an LLM specialist
call (specialists/technical.py) and writes only a flag/score/narrative to
risk_flags. That LLM narrative stays exactly as it is (see risk_technical.py)
— it's optional and this module doesn't touch it. What lives here is the
DETERMINISTIC part: the same SMA/RSI/MACD/Bollinger/ATR math yfinance_tools.
get_technical_indicators() already computes (that function is UNCHANGED — see
its own docstring — this is a parallel, versioned, storable path, not a
replacement), now keyed and persisted so two callers asking about the same
instrument on the same data don't each pay for their own fetch+compute.

Key: (ticker, data_snapshot, interval, lookback, adjustment_method,
calculation_version) — UNIQUE. data_snapshot is the last trading date
actually present in the price series used, never "today" (a stale/partial
fetch must not silently claim to be today's snapshot). calculation_version
changes only when the FORMULAS here change; fetched_at (when this row was
computed) is tracked separately and is not part of the key, so re-running
the exact same calculation for the exact same data is a no-op upsert, not a
new row.

Market-only: ticker + public price history in, indicator numbers out. No
portfolio, holdings or identity ever reaches this module.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

from portfolio_agent.tools.db import db_conn

CALCULATION_VERSION = "technical-features/1.0"
UNKNOWN = "UNKNOWN"

_HISTORY_TTL = 900.0   # seconds -- same window as tools.price_acquisition


@dataclass
class _HistoryEntry:
    hist: object   # pandas.DataFrame (OHLC)
    expires_at: float


class _HistoryCache:
    """
    Dedup for the single-ticker OHLC pull technical features needs (High/Low
    for ATR, not just Close — tools.price_acquisition's Close-only, multi-
    ticker cache doesn't fit this shape, so this is a small parallel cache
    for the same purpose: two overlapping callers asking about the same
    ticker/interval/lookback/adjustment within the window share one fetch.
    """
    def __init__(self, ttl_seconds: float = _HISTORY_TTL):
        self._ttl = ttl_seconds
        self._store: dict[tuple, _HistoryEntry] = {}

    def get(self, ticker: str, lookback: str, interval: str, adjustment_method: str):
        key = (ticker.upper(), lookback, interval, adjustment_method)
        entry = self._store.get(key)
        now = time.monotonic()
        if entry is not None and entry.expires_at > now:
            return entry.hist
        import yfinance as yf
        hist = yf.Ticker(ticker.upper()).history(
            period=lookback, interval=interval, auto_adjust=(adjustment_method == "auto_adjust"))
        self._store[key] = _HistoryEntry(hist=hist, expires_at=now + self._ttl)
        return hist

    def invalidate_all(self) -> None:
        self._store.clear()


_default_history_cache = _HistoryCache()

# indicator -> minimum bars its rolling window needs to produce a real value
_WINDOWS = {
    "sma_20": 20, "sma_50": 50, "sma_200": 200,
    "rsi_14": 15, "macd_line": 26, "macd_signal": 35, "macd_histogram": 35,
    "bb_upper": 20, "bb_lower": 20, "atr_14": 15, "price_vs_sma50_pct": 50,
}


@contextmanager
def _db():
    def _setup(conn: sqlite3.Connection) -> None:
        _create_schema(conn)
        conn.commit()
    with db_conn(setup=_setup) as conn:
        yield conn


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS technical_features (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker               TEXT NOT NULL,
            data_snapshot        TEXT NOT NULL,
            interval             TEXT NOT NULL,
            lookback             TEXT NOT NULL,
            adjustment_method    TEXT NOT NULL,
            calculation_version  TEXT NOT NULL,
            bars_available       INTEGER NOT NULL,
            coverage             TEXT NOT NULL,
            features             TEXT NOT NULL,
            fetched_at           TEXT NOT NULL,
            UNIQUE(ticker, data_snapshot, interval, lookback, adjustment_method, calculation_version)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_technical_features_ticker "
                "ON technical_features(ticker, data_snapshot DESC)")


def _safe_float(v) -> float | None:
    try:
        f = float(v)
        return None if f != f else round(f, 4)   # f != f is the NaN check (no pandas/math import needed)
    except Exception:
        return None


def compute_technical_features(close, high, low) -> dict:
    """
    Pure: no network call, no DB. close/high/low are pandas Series (same
    shape yfinance_tools.get_technical_indicators uses) already in hand.
    Returns {bars_available, coverage: {indicator: bool}, features: {indicator: float|"UNKNOWN"}}
    — identical formulas to get_technical_indicators, computed independently
    here (both are exercised in tests against the same fixed input to prove
    they agree, rather than one calling the other, since
    get_technical_indicators is intentionally left untouched).
    """
    bars_available = int(len(close))

    sma20 = close.rolling(20).mean().iloc[-1] if bars_available else float("nan")
    sma50 = close.rolling(50).mean().iloc[-1] if bars_available else float("nan")
    sma200 = close.rolling(200).mean().iloc[-1] if bars_available else float("nan")

    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss
    rsi = (100 - 100 / (1 + rs)).iloc[-1] if bars_available else float("nan")

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_line = (ema12 - ema26).iloc[-1] if bars_available else float("nan")
    signal_line = (ema12 - ema26).ewm(span=9, adjust=False).mean().iloc[-1] if bars_available else float("nan")

    sma20_series = close.rolling(20).mean()
    std20 = close.rolling(20).std()
    bb_upper = (sma20_series + 2 * std20).iloc[-1] if bars_available else float("nan")
    bb_lower = (sma20_series - 2 * std20).iloc[-1] if bars_available else float("nan")

    if bars_available:
        import pandas as pd
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ], axis=1).max(axis=1)
        atr14 = tr.rolling(14).mean().iloc[-1]
    else:
        atr14 = float("nan")

    current_price = _safe_float(close.iloc[-1]) if bars_available else None
    sma50_f = _safe_float(sma50)

    raw = {
        "current_price": current_price,
        "sma_20": _safe_float(sma20),
        "sma_50": sma50_f,
        "sma_200": _safe_float(sma200),
        "rsi_14": _safe_float(rsi),
        "macd_line": _safe_float(macd_line),
        "macd_signal": _safe_float(signal_line),
        "macd_histogram": _safe_float(macd_line - signal_line) if bars_available else None,
        "bb_upper": _safe_float(bb_upper),
        "bb_lower": _safe_float(bb_lower),
        "atr_14": _safe_float(atr14),
        "price_vs_sma50_pct": _safe_float((current_price / sma50_f - 1) * 100) if (current_price and sma50_f) else None,
    }

    coverage = {k: (raw.get(k) is not None) for k in _WINDOWS}
    features = {k: (v if v is not None else UNKNOWN) for k, v in raw.items()}
    return {"bars_available": bars_available, "coverage": coverage, "features": features}


def store_technical_features(ticker: str, data_snapshot: str, result: dict, *,
                             interval: str = "1d", lookback: str = "1y",
                             adjustment_method: str = "auto_adjust",
                             calculation_version: str = CALCULATION_VERSION) -> None:
    """Upsert — the (ticker, data_snapshot, interval, lookback, adjustment_method,
    calculation_version) key is unique; recomputing the same key updates in place."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _db() as conn:
        conn.execute(
            """INSERT INTO technical_features
                   (ticker, data_snapshot, interval, lookback, adjustment_method, calculation_version,
                    bars_available, coverage, features, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(ticker, data_snapshot, interval, lookback, adjustment_method, calculation_version)
               DO UPDATE SET bars_available = excluded.bars_available, coverage = excluded.coverage,
                             features = excluded.features, fetched_at = excluded.fetched_at""",
            (ticker.upper(), data_snapshot, interval, lookback, adjustment_method, calculation_version,
             result["bars_available"], json.dumps(result["coverage"]), json.dumps(result["features"]), now),
        )
        conn.commit()


def get_stored_technical_features(ticker: str, data_snapshot: str, *, interval: str = "1d",
                                  lookback: str = "1y", adjustment_method: str = "auto_adjust",
                                  calculation_version: str = CALCULATION_VERSION) -> dict | None:
    """Reuse path: a caller checks this BEFORE fetching/computing — a hit means
    another caller (this run or an earlier one, same or different user) already
    produced this exact key, so nothing is re-fetched or recomputed."""
    with _db() as conn:
        row = conn.execute(
            """SELECT * FROM technical_features
               WHERE ticker = ? AND data_snapshot = ? AND interval = ? AND lookback = ?
                     AND adjustment_method = ? AND calculation_version = ?""",
            (ticker.upper(), data_snapshot, interval, lookback, adjustment_method, calculation_version),
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["coverage"] = json.loads(d["coverage"])
    d["features"] = json.loads(d["features"])
    return d


def get_latest_technical_features(ticker: str, *, interval: str = "1d", lookback: str = "1y",
                                  adjustment_method: str = "auto_adjust",
                                  calculation_version: str = CALCULATION_VERSION) -> dict | None:
    """
    Read-only reuse for a caller that doesn't know the exact data_snapshot in
    advance (e.g. market_context building a prompt) — the most recent stored
    row for (ticker, interval, lookback, adjustment_method, calculation_version),
    regardless of which trading day produced it. Never fetches or computes;
    None means nothing has been stored yet for this key (risk_technical.py's
    phase hasn't run for this ticker, or it's not a portfolio holding).
    """
    with _db() as conn:
        row = conn.execute(
            """SELECT * FROM technical_features
               WHERE ticker = ? AND interval = ? AND lookback = ?
                     AND adjustment_method = ? AND calculation_version = ?
               ORDER BY data_snapshot DESC LIMIT 1""",
            (ticker.upper(), interval, lookback, adjustment_method, calculation_version),
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["coverage"] = json.loads(d["coverage"])
    d["features"] = json.loads(d["features"])
    return d


def fetch_and_store_technical_features(ticker: str, *, interval: str = "1d", lookback: str = "1y",
                                       adjustment_method: str = "auto_adjust",
                                       calculation_version: str = CALCULATION_VERSION,
                                       history_cache: "_HistoryCache | None" = None) -> dict | None:
    """
    Acquisition + compute + store, with reuse at two levels: the raw OHLC
    pull is shared via _HistoryCache (two overlapping callers for the same
    ticker/params within the cache window make one yfinance call, not two),
    and the computed row is shared via the DB's unique key (a stored row for
    today's actual data_snapshot is returned as-is, no recomputation either).

    Returns None if there isn't enough price history to say anything at all
    (no rows, ticker delisted, etc.) — never stores a snapshot with zero bars.
    """
    hist = (history_cache or _default_history_cache).get(ticker, lookback, interval, adjustment_method)
    if hist is None or hist.empty:
        return None
    data_snapshot = hist.index[-1].date().isoformat()

    cached = get_stored_technical_features(ticker, data_snapshot, interval=interval, lookback=lookback,
                                           adjustment_method=adjustment_method, calculation_version=calculation_version)
    if cached is not None:
        return cached

    result = compute_technical_features(hist["Close"], hist["High"], hist["Low"])
    store_technical_features(ticker, data_snapshot, result, interval=interval, lookback=lookback,
                             adjustment_method=adjustment_method, calculation_version=calculation_version)
    return get_stored_technical_features(ticker, data_snapshot, interval=interval, lookback=lookback,
                                         adjustment_method=adjustment_method, calculation_version=calculation_version)
