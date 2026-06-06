"""
Root agent for the portfolio analysis pipeline.

ADK CLI entry point: exposes `root_agent`.
  adk run portfolio_agent          # chat UI via adk web
  adk web                          # browser dev UI

Pipeline (sequential):
  1. preparation_phase  — two agents run in parallel:
       ticker_extractor  sets state["current_ticker"]
       query_extractor   sets state["user_query"] (captures the user's specific focus)
  2. research_phase     — six specialists run in parallel, each reading user_query
                          to decide which tools to call
  3. synthesis_agent    — reads all six state keys, produces state["recommendation"]
  4. clearance_agent    — compliance check + logging, produces state["clearance"]
"""

from google.adk.agents import LlmAgent, ParallelAgent, SequentialAgent

from ._models import FAST_MODEL
from .clearance import clearance_agent
from .specialists import (
    fundamentals_agent,
    macro_agent,
    news_agent,
    research_agent,
    risk_agent,
    technical_agent,
)
from .synthesis import synthesis_agent

# Extracts the ticker symbol from the user's message.
# When current_ticker is already set in state (CLI path), echoes it unchanged.
ticker_extractor = LlmAgent(
    name="ticker_extractor",
    model=FAST_MODEL,
    instruction="""
Extract a stock ticker symbol from the user's message.

If the session state already has a non-empty value for current_ticker
(shown as '{current_ticker}'), output ONLY that exact value unchanged.

Otherwise, identify the company or ticker in the user's message and output
ONLY the uppercase ticker symbol — nothing else.
Examples: AAPL, MSFT, TSLA, NVDA
""".strip(),
    output_key="current_ticker",
)

# Captures the user's specific question or analytical focus so every downstream
# specialist can tailor its tool selection accordingly.
# If the message is generic ("Analyze AAPL"), this outputs "Full analysis".
# If the message is targeted ("Is AAPL's debt load manageable?"), that intent
# is preserved verbatim and injected into every specialist as {user_query}.
query_extractor = LlmAgent(
    name="query_extractor",
    model=FAST_MODEL,
    instruction="""
Extract the user's specific question or analysis focus from their message.

Rules:
- If the message is generic (e.g. "Analyze AAPL", "full analysis", "Analyze MSFT"),
  output exactly: Full analysis
- Otherwise extract the core question or focus concisely (one sentence max).

Examples:
  "Is AAPL's debt load manageable given their expansion?"
    → Is AAPL's debt load manageable given their expansion?
  "What is the technical setup for NVDA right now?"
    → What is the technical setup for NVDA?
  "Should I hold or sell TSLA?"
    → Should I hold or sell TSLA?
  "Analyze MSFT"
    → Full analysis

Output ONLY the question or focus text. No explanation, no punctuation beyond the sentence.
""".strip(),
    output_key="user_query",
)

# Both extractors run simultaneously — neither depends on the other.
preparation_phase = ParallelAgent(
    name="preparation_phase",
    sub_agents=[ticker_extractor, query_extractor],
)

# All six specialist agents run concurrently; each reads {user_query} from state.
research_phase = ParallelAgent(
    name="research_phase",
    sub_agents=[
        fundamentals_agent,
        news_agent,
        macro_agent,
        technical_agent,
        research_agent,
        risk_agent,
    ],
)

# Full pipeline (what ADK CLI / adk web uses).
root_agent = SequentialAgent(
    name="portfolio_analysis_pipeline",
    description=(
        "Full portfolio analysis pipeline. "
        "Accepts natural language: 'Analyze AAPL' or 'Is TSLA's debt concerning?'. "
        "Extracts ticker and intent, runs six specialist agents in parallel "
        "(fundamentals, news, macro, technical, research, risk), "
        "synthesizes into a conviction-scored recommendation, "
        "and performs compliance clearance."
    ),
    sub_agents=[preparation_phase, research_phase, synthesis_agent, clearance_agent],
)
