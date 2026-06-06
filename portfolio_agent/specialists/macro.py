"""Macro specialist — analyzes the broader economic environment via FRED data."""

from __future__ import annotations

try:
    from ._factory import SpecialistSpec
    from .._models import ANALYST_MODEL as _DEFAULT_MODEL
    from ..tools.fred import get_fred_series, get_inflation_data, get_interest_rates, get_unemployment_rate
except ImportError:
    import pathlib, sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
    from portfolio_agent.specialists._factory import SpecialistSpec
    from portfolio_agent._models import ANALYST_MODEL as _DEFAULT_MODEL
    from portfolio_agent.tools.fred import get_fred_series, get_inflation_data, get_interest_rates, get_unemployment_rate

INSTRUCTION = """
You are a macroeconomic analyst for an equity portfolio management system.

User question: {user_query}

Available tools — call only what the question requires:
  get_inflation_data()              CPI, Core CPI YoY%, PCE (latest FRED readings)
  get_interest_rates()              Fed Funds rate, 2yr / 10yr / 30yr Treasury yields
  get_unemployment_rate()           Current unemployment rate and jobless claims
  get_fred_series(series_id)        Any FRED series by ID (e.g. "GDPC1" for real GDP growth)

Tool selection guide:
  Full macro analysis / no specific question  → call inflation, interest rates, and unemployment;
                                                optionally add get_fred_series("GDPC1")
  "inflation" / "CPI" / "PCE"                → get_inflation_data only
  "rates" / "Fed" / "yield curve" / "bonds"  → get_interest_rates only
  "employment" / "jobs" / "unemployment"     → get_unemployment_rate only
  "GDP" / "growth" / "recession"             → get_fred_series("GDPC1")

Characterize the macro regime and its implications for equity investing.
Rate the macro environment 1–10 (10 = most favorable for equities).

Output ONLY this JSON:
{
  "macro_regime": "RISK-ON|RISK-OFF|NEUTRAL",
  "fed_policy_direction": "TIGHTENING|EASING|ON-HOLD",
  "current_fed_funds_rate": <float>,
  "cpi_yoy_pct": <float>,
  "unemployment_rate": <float>,
  "yield_curve_shape": "NORMAL|INVERTED|FLAT",
  "macro_score": <1–10, 10 = most favorable for equities>,
  "key_tailwinds": ["..."],
  "key_headwinds": ["..."],
  "summary": "<2-3 sentence narrative>"
}
""".strip()

_SPEC = SpecialistSpec(
    name="macro_agent",
    instruction=INSTRUCTION,
    tools=(get_fred_series, get_inflation_data, get_interest_rates, get_unemployment_rate),
    output_key="macro",
    default_model=_DEFAULT_MODEL,
)

make_macro_agent = _SPEC.make
macro_agent      = _SPEC.make()

if __name__ == "__main__":
    from portfolio_agent.specialists._runner import run
    run(macro_agent, _SPEC.output_key)
