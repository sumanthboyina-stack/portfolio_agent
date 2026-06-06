"""Clearance agent — compliance restricted-list check."""

from __future__ import annotations

from google.adk.agents import LlmAgent

try:
    from ._models import FAST_MODEL as SPECIALIST_MODEL
    from .tools.portfolio_tools import check_restricted_list
except ImportError:
    import pathlib, sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from portfolio_agent._models import FAST_MODEL as SPECIALIST_MODEL
    from portfolio_agent.tools.portfolio_tools import check_restricted_list

INSTRUCTION = """
You are a compliance officer performing pre-trade clearance.

Ticker: {current_ticker}
User question: {user_query}
Investment recommendation: {recommendation}

Available tools:
  check_restricted_list(ticker)  Returns is_restricted (bool) and restriction_reason

Always call check_restricted_list("{current_ticker}") first.

If is_restricted is TRUE:
  Set clearance_status="BLOCKED" in output.

If is_restricted is FALSE:
  Parse {recommendation} to extract: action, conviction, composite_score, summary.
  Set clearance_status="CLEARED".

Output ONLY this JSON:
{
  "ticker": "...",
  "clearance_status": "CLEARED|BLOCKED",
  "restriction_reason": "<string or null>",
  "memo": "<2-3 sentence pre-trade memo or block notice>"
}
""".strip()

clearance_agent = LlmAgent(
    name="clearance_agent",
    model=SPECIALIST_MODEL,
    instruction=INSTRUCTION,
    tools=[check_restricted_list],
    output_key="clearance",
)

if __name__ == "__main__":
    import pathlib, sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from portfolio_agent.specialists._runner import run
    run(clearance_agent, "clearance")
