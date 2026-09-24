"""
Shared price-history acquisition — the one place that pulls per-ticker
historical closes from yfinance, so overlapping instrument requests (two
portfolios sharing tickers, or one process's Risk/Technical/Optimizer calls
all wanting the same names within the same run) fetch each ticker at most
once within the cache window instead of once per caller.

Deliberately keyed per TICKER, not per caller's whole ticker list: the cache
key is (ticker, period, interval, adjustment), and callers get back one dict
of independent per-ticker Series. Cross-ticker ALIGNMENT (dropna over the
intersection of dates across a specific set of tickers + benchmark) stays the
CALLER's job — which rows survive that alignment depends on which other
tickers are in THAT caller's own set, so an aligned multi-ticker frame is not
something this layer can safely share between two different callers with
different ticker sets. See portfolio_risk.py, which still does its own
alignment locally after fetching shared raw series from here.

Public market data only (prices, no portfolio/personal content) — no
authorization or privacy boundary applies to this module.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

_DEFAULT_TTL = 900.0   # 15 minutes -- comfortably inside one batch run, well under intraday cadence


@dataclass
class _Entry:
    series: "pd.Series"
    expires_at: float


class PriceCache:
    """Process-local cache of raw per-ticker close series (public market data)."""

    def __init__(self, ttl_seconds: float = _DEFAULT_TTL):
        self._ttl = ttl_seconds
        self._store: dict[tuple, _Entry] = {}

    def get_many(self, tickers: list[str], *, period: str = "6mo", interval: str = "1d",
                 auto_adjust: bool = True, min_rows: int = 20) -> dict[str, "pd.Series"]:
        """
        Return {ticker: close_series} for every ticker with >= min_rows of
        history. Only tickers not already warm in the cache are fetched; one
        yf.download call covers every still-missing ticker, never one call
        per ticker. A ticker yfinance can't supply enough history for is
        simply absent from the result (same "best-effort" contract every
        caller of this already expected from its own inline fetch).
        """
        tickers = sorted({str(t).upper() for t in tickers if t})
        now = time.monotonic()

        def _key(t: str) -> tuple:
            return (t, period, interval, auto_adjust)

        result: dict[str, "pd.Series"] = {}
        missing: list[str] = []
        for t in tickers:
            entry = self._store.get(_key(t))
            if entry is not None and entry.expires_at > now:
                result[t] = entry.series
            else:
                missing.append(t)

        if missing:
            import pandas as pd
            import yfinance as yf
            try:
                raw = yf.download(missing, period=period, interval=interval,
                                  auto_adjust=auto_adjust, progress=False, group_by="ticker")
            except Exception:
                raw = None
            for t in missing:
                series = None
                if raw is not None:
                    try:
                        s = (raw[t]["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw["Close"]).dropna()
                        if len(s) >= min_rows:
                            series = s
                    except Exception:
                        series = None
                if series is not None:
                    self._store[_key(t)] = _Entry(series=series, expires_at=now + self._ttl)
                    result[t] = series
        return result

    def invalidate_all(self) -> None:
        self._store.clear()


_default_cache = PriceCache()


def fetch_price_series(tickers: list[str], *, period: str = "6mo", interval: str = "1d",
                       auto_adjust: bool = True, min_rows: int = 20,
                       cache: PriceCache | None = None) -> dict[str, "pd.Series"]:
    """Module-level convenience over a shared default cache (pass `cache=` to inject one — tests)."""
    return (cache or _default_cache).get_many(
        tickers, period=period, interval=interval, auto_adjust=auto_adjust, min_rows=min_rows)
