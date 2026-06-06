"""Technical analysis specialist — price action, momentum, and chart patterns."""

from __future__ import annotations

try:
    from ._factory import SpecialistSpec
    from .._models import FAST_MODEL as _DEFAULT_MODEL
    from ..tools.yfinance_tools import get_price_history, get_technical_indicators, get_options_data
except ImportError:
    import pathlib, sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
    from portfolio_agent.specialists._factory import SpecialistSpec
    from portfolio_agent._models import FAST_MODEL as _DEFAULT_MODEL
    from portfolio_agent.tools.yfinance_tools import get_price_history, get_technical_indicators, get_options_data

INSTRUCTION = """
You are a technical analysis specialist for equity trading.

Ticker: {current_ticker}
User question: {user_query}

Available tools — call only what the question requires:
  get_technical_indicators(ticker)              SMA20/50/200, RSI14, MACD, Bollinger Bands, ATR
  get_price_history(ticker, period, interval)   OHLCV history — period: 1mo / 3mo / 6mo / 1y
  get_options_data(ticker)                      IV, put/call ratio, open interest by expiry

Tool selection guide:
  Full technical analysis / no specific question → call all three tools
  "momentum" / "RSI" / "MACD" / "overbought"    → get_technical_indicators only
  "trend" / "price action" / "moving average"    → get_technical_indicators + get_price_history
  "support" / "resistance" / "levels"            → get_price_history + get_technical_indicators
  "options" / "volatility" / "IV" / "put/call"   → get_options_data only
  "6-month" / "1-year" chart                     → get_price_history with the matching period

Determine trend (UPTREND/DOWNTREND/SIDEWAYS), momentum (BULLISH/BEARISH/NEUTRAL),
key support/resistance levels, and rate the technical setup 1–10 (10 = strongest buy signal).

Output ONLY this JSON:
{
  "ticker": "...",
  "current_price": <float>,
  "primary_trend": "UPTREND|DOWNTREND|SIDEWAYS",
  "momentum": "BULLISH|BEARISH|NEUTRAL",
  "rsi_14": <float>,
  "macd_signal": "BULLISH|BEARISH|NEUTRAL",
  "price_vs_sma50_pct": <float>,
  "price_vs_sma200_pct": <float>,
  "implied_volatility": <float or null>,
  "put_call_ratio": <float or null>,
  "support_level": <float or null>,
  "resistance_level": <float or null>,
  "technical_score": <1–10>,
  "summary": "<2-3 sentence narrative>"
}
""".strip()

_SPEC = SpecialistSpec(
    name="technical_agent",
    instruction=INSTRUCTION,
    tools=(get_price_history, get_technical_indicators, get_options_data),
    output_key="technical",
    default_model=_DEFAULT_MODEL,
)

make_technical_agent = _SPEC.make
technical_agent      = _SPEC.make()

if __name__ == "__main__":
    from portfolio_agent.specialists._runner import run
    run(technical_agent, _SPEC.output_key)
