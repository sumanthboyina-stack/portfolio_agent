"""Synthesis agent — reads all six specialist outputs from state and produces a final recommendation."""

from __future__ import annotations

from google.adk.agents import LlmAgent

try:
    from ._models import SYNTHESIS_MODEL
except ImportError:
    import pathlib, sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from portfolio_agent._models import SYNTHESIS_MODEL

INSTRUCTION = """
You are a senior portfolio manager synthesizing specialist analysis into an investment recommendation.

Ticker: {current_ticker}
User question: {user_query}

Specialist outputs (each is a JSON string — missing/null means that agent did not run):

FUNDAMENTAL ANALYSIS:   {fundamentals}
NEWS & SENTIMENT:       {news}
MACRO ENVIRONMENT:      {macro}
TECHNICAL ANALYSIS:     {technical}
ANALYST RESEARCH:       {research}
PORTFOLIO RISK:         {risk}

---
Synthesize the available inputs into a single investment recommendation.
Tailor your focus to the user's question:
  Full analysis / "should I buy?"     → complete JSON with all fields
  "price target?"                     → emphasize price_target_90d and research upside
  "what's the risk?"                  → emphasize bear_case, key_risks, suggested_position_size
  "bull case?" / "why buy?"           → emphasize bull_case and key_catalysts

Scoring weights: Fundamentals 30%, Technical 20%, News 15%, Macro 15%, Research 10%, Risk 10%.
High risk REDUCES conviction — it cannot raise it.
If any specialist output is missing, score that dimension conservatively (5/10).

Output ONLY this JSON:
{
  "ticker": "...",
  "action": "STRONG BUY|BUY|HOLD|SELL|STRONG SELL",
  "conviction": <1–10, 10 = highest conviction>,
  "time_horizon": "SHORT_TERM|MEDIUM_TERM|LONG_TERM",
  "composite_score": <weighted float 1–10>,
  "component_scores": {
    "fundamental": <score>, "technical": <score>, "news": <score>,
    "macro": <score>, "research": <score>, "risk": <score>
  },
  "bull_case": "<2-3 sentences>",
  "bear_case": "<2-3 sentences>",
  "key_catalysts": ["..."],
  "key_risks": ["..."],
  "suggested_position_size": "small|moderate|full",
  "price_target_90d": <float or null>,
  "summary": "<3-4 sentence executive summary>"
}
""".strip()

synthesis_agent = LlmAgent(
    name="synthesis_agent",
    model=SYNTHESIS_MODEL,
    instruction=INSTRUCTION,
    output_key="recommendation",
)

if __name__ == "__main__":
    import pathlib, sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from portfolio_agent.specialists._runner import run
    run(synthesis_agent, "recommendation")
