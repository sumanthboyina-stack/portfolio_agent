"""yfinance-backed tools for price data, technicals, and options."""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Optional

import logging

import pandas as pd
import yfinance as yf

_log = logging.getLogger(__name__)


def _safe_float(v) -> float | None:
    try:
        f = float(v)
        return None if pd.isna(f) else round(f, 4)
    except Exception:
        return None


def get_close(ticker: str, date_str: str) -> Optional[float]:
    """
    Return the adjusted closing price for ticker on or just after date_str.

    Uses auto_adjust=True so corporate actions (splits, dividends) are reflected
    consistently across both start and end prices in return calculations.
    Returns None if no data is available within 7 calendar days of date_str.
    """
    try:
        dt = date.fromisoformat(date_str)
        end = (dt + timedelta(days=7)).isoformat()
        hist = yf.Ticker(ticker).history(start=date_str, end=end, auto_adjust=True)
        if hist.empty:
            return None
        return float(hist["Close"].iloc[0])
    except Exception as exc:
        _log.warning("yfinance get_close failed for %s on %s: %s", ticker, date_str, exc)
        return None


def get_price_history(ticker: str, period: str = "1y", interval: str = "1d") -> str:
    """
    Return OHLCV price history for a ticker.

    Args:
        ticker: Stock ticker symbol.
        period: yfinance period string — "1mo", "3mo", "6mo", "1y", "2y", "5y".
        interval: Bar interval — "1d", "1wk", "1mo".

    Returns:
        JSON string with list of {date, open, high, low, close, volume} bars.
    """
    t = yf.Ticker(ticker)
    hist = t.history(period=period, interval=interval, auto_adjust=True)
    if hist.empty:
        return json.dumps({"error": f"No price data for {ticker}"})
    records = []
    for idx, row in hist.iterrows():
        records.append({
            "date": str(idx.date()),
            "open": _safe_float(row["Open"]),
            "high": _safe_float(row["High"]),
            "low": _safe_float(row["Low"]),
            "close": _safe_float(row["Close"]),
            "volume": int(row["Volume"]) if not pd.isna(row["Volume"]) else None,
        })
    return json.dumps({"ticker": ticker.upper(), "period": period, "bars": records})


def get_technical_indicators(ticker: str) -> str:
    """
    Compute common technical indicators: SMA20, SMA50, SMA200, RSI14,
    MACD, Bollinger Bands, and ATR14.

    Args:
        ticker: Stock ticker symbol.

    Returns:
        JSON string with computed indicator values and current price.
    """
    t = yf.Ticker(ticker)
    hist = t.history(period="1y", interval="1d", auto_adjust=True)
    if hist.empty:
        return json.dumps({"error": f"No data for {ticker}"})

    close = hist["Close"]
    high = hist["High"]
    low = hist["Low"]

    # SMAs
    sma20 = close.rolling(20).mean().iloc[-1]
    sma50 = close.rolling(50).mean().iloc[-1]
    sma200 = close.rolling(200).mean().iloc[-1]

    # RSI-14
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss
    rsi = (100 - 100 / (1 + rs)).iloc[-1]

    # MACD
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_line = (ema12 - ema26).iloc[-1]
    signal_line = (ema12 - ema26).ewm(span=9, adjust=False).mean().iloc[-1]

    # Bollinger Bands (20, 2)
    sma20_series = close.rolling(20).mean()
    std20 = close.rolling(20).std()
    bb_upper = (sma20_series + 2 * std20).iloc[-1]
    bb_lower = (sma20_series - 2 * std20).iloc[-1]

    # ATR-14
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr14 = tr.rolling(14).mean().iloc[-1]

    current_price = _safe_float(close.iloc[-1])
    return json.dumps({
        "ticker": ticker.upper(),
        "current_price": current_price,
        "sma_20": _safe_float(sma20),
        "sma_50": _safe_float(sma50),
        "sma_200": _safe_float(sma200),
        "rsi_14": _safe_float(rsi),
        "macd_line": _safe_float(macd_line),
        "macd_signal": _safe_float(signal_line),
        "macd_histogram": _safe_float(macd_line - signal_line),
        "bb_upper": _safe_float(bb_upper),
        "bb_lower": _safe_float(bb_lower),
        "atr_14": _safe_float(atr14),
        "price_vs_sma50_pct": _safe_float(
            (current_price / _safe_float(sma50) - 1) * 100 if _safe_float(sma50) else None
        ),
    })


def get_options_data(ticker: str) -> str:
    """
    Return implied volatility skew and put/call ratio from the nearest
    expiration options chain.

    Args:
        ticker: Stock ticker symbol.

    Returns:
        JSON string with implied_volatility_atm, put_call_ratio, and
        nearest expiration date.
    """
    t = yf.Ticker(ticker)
    exps = t.options
    if not exps:
        return json.dumps({"error": f"No options data for {ticker}"})
    nearest_exp = exps[0]
    chain = t.option_chain(nearest_exp)
    calls = chain.calls
    puts = chain.puts
    total_call_vol = int(calls["volume"].fillna(0).sum())
    total_put_vol = int(puts["volume"].fillna(0).sum())
    put_call_ratio = round(total_put_vol / total_call_vol, 4) if total_call_vol > 0 else None
    # ATM IV — find strike closest to last price
    info = t.fast_info
    spot = getattr(info, "last_price", None)
    atm_iv = None
    if spot and not calls.empty:
        idx = (calls["strike"] - spot).abs().idxmin()
        atm_iv = _safe_float(calls.loc[idx, "impliedVolatility"])
    return json.dumps({
        "ticker": ticker.upper(),
        "nearest_expiry": nearest_exp,
        "put_call_ratio": put_call_ratio,
        "atm_implied_volatility": atm_iv,
        "total_call_volume": total_call_vol,
        "total_put_volume": total_put_vol,
    })


def get_analyst_targets(ticker: str) -> str:
    """
    Return analyst price targets and recommendation consensus from yfinance.

    Args:
        ticker: Stock ticker symbol.

    Returns:
        JSON string with mean/high/low targets, recommendation, and number of analysts.
    """
    t = yf.Ticker(ticker)
    info = t.info
    return json.dumps({
        "ticker": ticker.upper(),
        "recommendation": info.get("recommendationKey"),
        "number_of_analysts": info.get("numberOfAnalystOpinions"),
        "target_mean_price": _safe_float(info.get("targetMeanPrice")),
        "target_high_price": _safe_float(info.get("targetHighPrice")),
        "target_low_price": _safe_float(info.get("targetLowPrice")),
        "current_price": _safe_float(info.get("currentPrice")),
        "upside_to_mean_pct": _safe_float(
            (info.get("targetMeanPrice", 0) / info.get("currentPrice", 1) - 1) * 100
            if info.get("currentPrice") else None
        ),
    })
