"""Fundamentals specialist — analyzes SEC filings and financial statements."""

from __future__ import annotations

try:
    from ._factory import SpecialistSpec
    from .._models import FLASH_MODEL as _DEFAULT_MODEL
    from ..tools.edgar import get_income_statement, get_balance_sheet, get_cash_flow, get_sec_filings
except ImportError:
    import pathlib, sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
    from portfolio_agent.specialists._factory import SpecialistSpec
    from portfolio_agent._models import FLASH_MODEL as _DEFAULT_MODEL
    from portfolio_agent.tools.edgar import get_income_statement, get_balance_sheet, get_cash_flow, get_sec_filings

INSTRUCTION = """
You are a fundamental analysis specialist for equity research.

Ticker: {current_ticker}
User question: {user_query}

Available tools — call only what the question requires:
  get_income_statement(ticker)              Revenue, gross profit, operating income, net income (3 yrs)
  get_balance_sheet(ticker)                 Assets, liabilities, equity, cash, total debt (3 yrs)
  get_cash_flow(ticker)                     Operating CF, capex, FCF, investing/financing flows (3 yrs)
  get_sec_filings(ticker, filing_type)      Most recent SEC 10-K or 10-Q text and metadata

Tool selection guide:
  Full analysis / no specific question  → call all four tools
  "debt" / "leverage" / "balance sheet" → get_balance_sheet + get_cash_flow
  "revenue" / "growth" / "margins"      → get_income_statement
  "cash flow" / "FCF" / "capex"         → get_cash_flow
  "10-K" / "filing" / "annual report"   → get_sec_filings

From the retrieved data compute: revenue_growth_yoy_pct, net_margin, fcf,
debt_to_equity. Flag any red flags (deteriorating margins, rising debt, negative FCF).
Rate fundamental strength 1–10 (10 = strongest).

Output ONLY this JSON — field names must match exactly:
{
  "ticker": "...",
  "revenue_growth_yoy_pct": <float or null>,
  "net_margin": <float or null>,
  "fcf": <float or null>,
  "debt_to_equity": <float or null>,
  "fundamental_score": <1–10>,
  "key_strengths": ["..."],
  "key_risks": ["..."],
  "summary": "<2-3 sentence narrative>"
}
""".strip()

_SPEC = SpecialistSpec(
    name="fundamentals_agent",
    instruction=INSTRUCTION,
    tools=(get_income_statement, get_balance_sheet, get_cash_flow, get_sec_filings),
    output_key="fundamentals",
    default_model=_DEFAULT_MODEL,
)

make_fundamentals_agent = _SPEC.make
fundamentals_agent      = _SPEC.make()

if __name__ == "__main__":
    from portfolio_agent.specialists._runner import run
    run(fundamentals_agent, _SPEC.output_key)
