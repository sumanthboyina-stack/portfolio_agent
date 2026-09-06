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


def get_trailing_return(ticker: str, as_of: str, lookback_days: int = 10) -> Optional[float]:
    """Return ticker's return over the ~lookback_days trading days before as_of."""
    end_price = get_close(ticker, as_of)
    start_date = (date.fromisoformat(as_of) - timedelta(days=lookback_days + 4)).isoformat()
    start_price = get_close(ticker, start_date)
    if not end_price or not start_price or start_price == 0:
        return None
    return (end_price / start_price) - 1


def get_closes_in_range(ticker: str, start_date: str, end_date: str) -> dict[str, float]:
    """
    Return {date_iso: close} for every trading day in [start_date, end_date].

    Uses auto_adjust=True so splits/dividends don't distort a return comparison.
    yfinance's `end` is exclusive, so it's padded by one day to include end_date itself.
    """
    try:
        end_incl = (date.fromisoformat(end_date) + timedelta(days=1)).isoformat()
        hist = yf.Ticker(ticker).history(start=start_date, end=end_incl, auto_adjust=True)
        if hist.empty:
            return {}
        return {str(idx.date()): float(row["Close"]) for idx, row in hist.iterrows()}
    except Exception as exc:
        _log.warning("yfinance get_closes_in_range failed for %s [%s, %s]: %s",
                     ticker, start_date, end_date, exc)
        return {}


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


def get_short_interest(ticker: str) -> str:
    """
    Return short interest data for a ticker.

    Reads from the local FINRA DB first (rich trend history). Falls back to
    yfinance Ticker.info when no FINRA data has been loaded yet (e.g. first
    run before the twice-monthly refresh fires).

    Args:
        ticker: Stock ticker symbol.

    Returns:
        JSON string with current_shares, days_to_cover, trend, squeeze_pressure,
        and settlement_date (or source="yfinance" when using the fallback).
    """
    try:
        from portfolio_agent.tools.finra import get_short_interest as _get_si
        return json.dumps(_get_si(ticker))
    except Exception as exc:
        return json.dumps({"ticker": ticker.upper(), "available": False, "error": str(exc)})


def get_valuation_multiples(ticker: str) -> dict:
    """
    Balance sheet, market cap, and valuation-multiple inputs for the DCF/relative
    valuation engine (portfolio_agent/tools/valuation_engine.py).

    Unlike the other functions in this file, returns a plain dict rather than a
    JSON string — this isn't an LLM-callable tool, it's a data primitive consumed
    directly by deterministic Python math (same convention as portfolio_risk.py).

    yf.Ticker(t).info is notoriously incomplete (varies by ticker type/exchange) —
    every field is independently None-safe; this function never raises.
    """
    try:
        info = yf.Ticker(ticker).info or {}
    except Exception as exc:
        _log.warning("yfinance get_valuation_multiples failed for %s: %s", ticker, exc)
        info = {}

    market_cap = _safe_float(info.get("marketCap"))
    enterprise_value = _safe_float(info.get("enterpriseValue"))
    ebitda = _safe_float(info.get("ebitda"))
    revenue = _safe_float(info.get("totalRevenue"))

    return {
        "ticker": ticker.upper(),
        "current_price": _safe_float(info.get("currentPrice") or info.get("regularMarketPrice")),
        "market_cap": market_cap,
        "shares_outstanding": _safe_float(info.get("sharesOutstanding")),
        "total_debt": _safe_float(info.get("totalDebt")),
        "total_cash": _safe_float(info.get("totalCash")),
        "beta": _safe_float(info.get("beta")),
        "trailing_pe": _safe_float(info.get("trailingPE")),
        "forward_pe": _safe_float(info.get("forwardPE")),
        "enterprise_value": enterprise_value,
        "ebitda": ebitda,
        "ev_to_ebitda": round(enterprise_value / ebitda, 2) if enterprise_value and ebitda else None,
        "ev_to_revenue": round(enterprise_value / revenue, 2) if enterprise_value and revenue else None,
        "price_to_sales": _safe_float(info.get("priceToSalesTrailing12Months")),
        "peg_ratio": _safe_float(info.get("pegRatio") or info.get("trailingPegRatio")),
        "operating_margin": _safe_float(info.get("operatingMargins")),
        "free_cashflow": _safe_float(info.get("freeCashflow")),
        "trailing_eps": _safe_float(info.get("trailingEps")),
        "fifty_two_week_low": _safe_float(info.get("fiftyTwoWeekLow")),
        "fifty_two_week_high": _safe_float(info.get("fiftyTwoWeekHigh")),
        "sector": info.get("sector"),
    }


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
