from __future__ import annotations

import pandas as pd
import pytest

from portfolio_agent.tools.screener import _THRESHOLDS, _compute_signals


def _flat_history(n_days: int = 260, flat_price: float = 100.0, flat_volume: float = 1000.0,
                   last_price: float | None = None, last_volume: float | None = None) -> pd.DataFrame:
    close = [flat_price] * n_days
    volume = [flat_volume] * n_days
    if last_price is not None:
        close[-1] = last_price
    if last_volume is not None:
        volume[-1] = last_volume
    return pd.DataFrame({"Close": close, "Volume": volume})


def test_flat_history_fires_no_signals():
    hist = _flat_history()
    results = _compute_signals(["AAA"], hist, _THRESHOLDS["extended"])
    assert results == []


def test_short_history_is_skipped():
    hist = _flat_history(n_days=10)
    results = _compute_signals(["AAA"], hist, _THRESHOLDS["extended"])
    assert results == []


def test_all_signals_fire_above_both_tier_thresholds():
    # +20% breakout with a matching volume spike trips every signal comfortably
    # past both the extended and broad thresholds.
    hist = _flat_history(last_price=120.0, last_volume=3000.0)

    extended = _compute_signals(["AAA"], hist, _THRESHOLDS["extended"])
    broad = _compute_signals(["AAA"], hist, _THRESHOLDS["broad"])

    assert len(extended) == 1
    assert len(broad) == 1
    assert extended[0]["score"] == 6
    assert broad[0]["score"] == 6


def test_extended_tier_is_more_sensitive_than_broad_tier():
    # +10% breakout with a 2.7x volume spike clears the extended thresholds
    # (momentum 1.08, vol 2.5x) but not the broad ones (1.15, 3.0x) — only
    # the gap and near-52w-high signals should survive under the broad tier.
    hist = _flat_history(last_price=110.0, last_volume=2700.0)

    extended = _compute_signals(["AAA"], hist, _THRESHOLDS["extended"])
    broad = _compute_signals(["AAA"], hist, _THRESHOLDS["broad"])

    assert len(extended) == 1
    assert extended[0]["score"] == 6
    assert "vol_spike" in extended[0]["signals_json"]
    assert "momentum" in extended[0]["signals_json"]

    assert len(broad) == 1
    assert broad[0]["score"] == 3
    assert "vol_spike" not in broad[0]["signals_json"]
    assert "momentum" not in broad[0]["signals_json"]


def test_score_below_min_score_is_excluded():
    # A lone near-52w-high tick (score 1) clears neither tier's min_score.
    hist = _flat_history(last_price=100.5, last_volume=1000.0)
    results = _compute_signals(["AAA"], hist, _THRESHOLDS["extended"])
    assert results == []


def _multi_ticker_history(per_ticker: dict[str, dict], n_days: int = 260) -> pd.DataFrame:
    """Build a MultiIndex (metric, ticker) frame, matching a multi-ticker
    yfinance.download() result, so _compute_signals takes its per-ticker path."""
    data = {}
    for ticker, overrides in per_ticker.items():
        close = [100.0] * n_days
        volume = [1000.0] * n_days
        if "last_price" in overrides:
            close[-1] = overrides["last_price"]
        if "last_volume" in overrides:
            volume[-1] = overrides["last_volume"]
        data[("Close", ticker)] = close
        data[("Volume", ticker)] = volume
    hist = pd.DataFrame(data)
    hist.columns = pd.MultiIndex.from_tuples(hist.columns)
    return hist


def test_results_sorted_by_score_descending():
    # STRONG scores 6 (all four signals); MEDIUM scores 3 (gap + near-high only,
    # momentum and volume both held under the extended thresholds).
    hist = _multi_ticker_history({
        "STRONG": {"last_price": 120.0, "last_volume": 3000.0},
        "MEDIUM": {"last_price": 104.0, "last_volume": 1000.0},
    })

    results = _compute_signals(["STRONG", "MEDIUM"], hist, _THRESHOLDS["extended"])

    assert [r["ticker"] for r in results] == ["STRONG", "MEDIUM"]
    assert results[0]["score"] == 6
    assert results[1]["score"] == 3
