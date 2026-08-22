"""
Pure-math screening for extended and broad universe tickers.

No LLM involved. Signals are computed from yfinance batch price/volume history:
  momentum     — price / 200-day SMA (breakout above a threshold)
  volume_spike — today's volume / 20-day avg volume (anomalous interest)
  gap          — overnight price change from prior close (material move)
  near_52w_high — price within threshold of 52-week high (breakout proximity)

Thresholds are higher for the broad tier to reduce false positives from noisier names.
Promoted candidates are written to screening_signals and picked up by intraday Track B.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

_log = logging.getLogger(__name__)

_THRESHOLDS: dict[str, dict] = {
    "extended": {
        "volume_spike_ratio":  2.5,
        "momentum_threshold":  1.08,   # 8% above 200d SMA
        "gap_pct":             3.0,    # 3% overnight gap
        "near_52w_high_pct":   0.98,   # within 2% of 52W high
        "min_score":           2,
    },
    "broad": {
        "volume_spike_ratio":  3.0,    # higher bar for noisier universe
        "momentum_threshold":  1.15,   # 15% above 200d SMA
        "gap_pct":             5.0,
        "near_52w_high_pct":   0.99,   # within 1% of 52W high
        "min_score":           3,
    },
}

_DOWNLOAD_BATCH = 500   # tickers per yfinance.download() call


def _download_history(tickers: list[str]):
    """Batch download 1-year daily OHLCV for a list of tickers (returns DataFrame)."""
    import yfinance as yf
    import pandas as pd
    if not tickers:
        return pd.DataFrame()
    return yf.download(
        tickers,
        period="1y",
        interval="1d",
        auto_adjust=True,
        progress=False,
        threads=True,
    )


def _compute_signals(
    tickers: list[str],
    hist,          # pd.DataFrame from yf.download
    thresholds: dict,
) -> list[dict]:
    """
    Apply screening math to a batch. Returns list of {ticker, score, signals_json, tier}
    for tickers that trip at least min_score signal points.
    """
    import pandas as pd

    if hist is None or hist.empty:
        return []

    # yfinance multi-ticker downloads use a MultiIndex on columns (metric, ticker)
    # Single-ticker downloads use flat columns.
    multi = isinstance(getattr(hist.columns, "levels", None), object) and len(hist.columns.names) > 1

    results = []
    for ticker in tickers:
        try:
            if multi:
                close_s  = hist["Close"][ticker].dropna()
                volume_s = hist["Volume"][ticker].dropna()
            else:
                close_s  = hist["Close"].dropna()
                volume_s = hist["Volume"].dropna()

            if len(close_s) < 20:
                continue

            current    = float(close_s.iloc[-1])
            prev_close = float(close_s.iloc[-2]) if len(close_s) >= 2 else current
            sma200     = float(close_s.rolling(252).mean().iloc[-1]) if len(close_s) >= 200 else None
            sma20      = float(close_s.rolling(20).mean().iloc[-1])
            high_52w   = float(close_s.rolling(252).max().iloc[-1]) if len(close_s) >= 252 else float(close_s.max())

            vol_today   = float(volume_s.iloc[-1])   if len(volume_s) >= 1 else 0.0
            vol_20d_avg = float(volume_s.iloc[-20:-1].mean()) if len(volume_s) >= 21 else 0.0

            momentum   = (current / sma200) if sma200 and sma200 > 0 else (current / sma20 if sma20 > 0 else 1.0)
            vol_ratio  = vol_today / vol_20d_avg if vol_20d_avg > 0 else 0.0
            gap_pct    = abs((current - prev_close) / prev_close * 100) if prev_close > 0 else 0.0
            near_high  = (current / high_52w >= thresholds["near_52w_high_pct"]) if high_52w > 0 else False

            score = 0
            fired: list[str] = []

            if vol_ratio >= thresholds["volume_spike_ratio"]:
                score += 2
                fired.append(f"vol_spike:{vol_ratio:.1f}x")
            if momentum >= thresholds["momentum_threshold"]:
                score += 1
                fired.append(f"momentum:{momentum:.2f}")
            if gap_pct >= thresholds["gap_pct"]:
                score += 2
                fired.append(f"gap:{gap_pct:.1f}%")
            if near_high:
                score += 1
                fired.append("near_52w_high")

            if score >= thresholds["min_score"]:
                results.append({
                    "ticker":       ticker,
                    "score":        score,
                    "signals_json": json.dumps(fired),
                })
        except Exception:
            continue

    return sorted(results, key=lambda x: x["score"], reverse=True)


def screen_universe(
    tier: str,
    exclude: Optional[set[str]] = None,
    promote_limit: int = 15,
    log=None,
) -> list[str]:
    """
    Screen all tickers in *tier* from universe_tickers.

    Downloads 1-year price/volume history in batches of _DOWNLOAD_BATCH tickers,
    applies math signals, writes results to screening_signals, and returns the
    top *promote_limit* tickers for downstream news/research triage.

    Args:
        tier:          "extended" or "broad"
        exclude:       uppercase tickers already in core pipeline (skip these)
        promote_limit: max candidates to return
        log:           optional pipeline logger (uses module logger if None)

    Returns:
        List of promoted ticker symbols, highest-scored first.
    """
    from portfolio_agent.tools.universe_db import get_tickers, insert_signals, has_any_universe_data

    _l = log or _log
    thresholds = _THRESHOLDS.get(tier, _THRESHOLDS["extended"])

    if not has_any_universe_data():
        _l.info(
            "  [screener] Universe cache is empty — run scripts/refresh_universe.py first.",
            event_type="skip",
        )
        return []

    candidates = get_tickers(tier=tier, exclude=exclude or set())
    if not candidates:
        _l.info(f"  [screener] No {tier} tickers in cache (all excluded or empty).", event_type="skip")
        return []

    _l.info(
        f"  [screener] Screening {len(candidates)} {tier} tickers for signals…",
        event_type="fetch_start",
    )

    all_signals: list[dict] = []

    for i in range(0, len(candidates), _DOWNLOAD_BATCH):
        batch = candidates[i: i + _DOWNLOAD_BATCH]
        try:
            hist = _download_history(batch)
            signals = _compute_signals(batch, hist, thresholds)
            for s in signals:
                s["tier"] = tier
            all_signals.extend(signals)
        except Exception as exc:
            _l.warning(
                f"  [screener] Batch {i//  _DOWNLOAD_BATCH + 1} failed: {exc}",
                event_type="warning",
            )

    if all_signals:
        insert_signals(all_signals)

    promoted = [s["ticker"] for s in all_signals[:promote_limit]]
    _l.info(
        f"  [screener] {tier}: {len(candidates)} screened, "
        f"{len(all_signals)} signals, {len(promoted)} promoted",
        event_type="summary",
    )
    return promoted


def evaluate_screener_outcomes(log=None) -> dict:
    """
    Score forward returns for screening_signals rows that are now old enough
    to mature (~1 trading week), so the Opportunities screen's rolling 30-day
    conviction (persistence + did-it-actually-work) has fresh data each morning.

    Entry price = close on signal_date, exit price = close as of today —
    each row is only picked up once it's past the maturity window, so "today"
    is consistently ~1 week out from signal_date. Rows whose price data is
    still missing are left for a later run rather than retried in this pass.
    """
    from portfolio_agent.tools.universe_db import get_signals_needing_outcome, record_signal_outcome
    from portfolio_agent.tools.yfinance_tools import get_close
    from datetime import date

    _l = log or _log
    today = date.today().isoformat()

    due = get_signals_needing_outcome()
    evaluated = 0
    data_missing = 0
    errors = 0

    for row in due:
        try:
            entry = get_close(row["ticker"], row["signal_date"])
            exit_ = get_close(row["ticker"], today)
            if not entry or not exit_:
                data_missing += 1
                continue
            fwd_return = (exit_ / entry) - 1.0
            record_signal_outcome(row["id"], fwd_return)
            evaluated += 1
        except Exception:
            errors += 1

    _l.info(
        f"  [screener] outcomes: {evaluated} evaluated, "
        f"{data_missing} data_missing, {errors} errors",
        event_type="summary",
    )
    return {"evaluated": evaluated, "data_missing": data_missing, "errors": errors}
