"""
Clearance agent — compliance restricted-list check.

Two deterministic gates run in a before_agent_callback, *before* the LLM is
invoked (and therefore before check_restricted_list is ever called):

  1. pre_clearance_required is false in the user profile → the step is skipped
     and state["clearance"] records clearance_status="SKIPPED".
  2. Today falls inside the profile's blackout_start..blackout_end window →
     state["clearance"] records clearance_status="BLOCKED" with the window in
     the reason. No LLM call is needed for either outcome.

Only when both gates pass does the LLM run and call check_restricted_list.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.genai import types

try:
    from ._models import FAST_MODEL as SPECIALIST_MODEL
    from .tools.portfolio_tools import check_restricted_list
except ImportError:
    import pathlib, sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from portfolio_agent._models import FAST_MODEL as SPECIALIST_MODEL
    from portfolio_agent.tools.portfolio_tools import check_restricted_list


# ── Deterministic gates (no LLM) ──────────────────────────────────────────────

def _parse_iso_date(raw: Optional[str]) -> Optional[date]:
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def is_in_blackout(today: date, blackout_start: Optional[str], blackout_end: Optional[str]) -> bool:
    """
    True when *today* falls inside the inclusive [blackout_start, blackout_end]
    window. An open-ended window (only one bound set) is honoured; no bounds
    means no blackout. Unparseable dates are treated as unset.
    """
    start = _parse_iso_date(blackout_start)
    end = _parse_iso_date(blackout_end)
    if start is None and end is None:
        return False
    if start is not None and today < start:
        return False
    if end is not None and today > end:
        return False
    return True


def _today_in(tz_name: Optional[str]) -> date:
    try:
        return datetime.now(ZoneInfo(tz_name or "America/Chicago")).date()
    except Exception:
        return date.today()


def profile_gate_result(ticker: str, profile, today: Optional[date] = None) -> Optional[dict]:
    """
    Evaluate the profile-driven gates for *ticker*. Returns the clearance dict
    to write to state when a gate fires, or None when the LLM should run.
    """
    if not profile.pre_clearance_required:
        return {
            "ticker": ticker,
            "clearance_status": "SKIPPED",
            "restriction_reason": None,
            "memo": "Pre-trade clearance is disabled in the user profile "
                    "(pre_clearance_required = false); restricted-list check not performed.",
        }
    today = today or _today_in(profile.timezone)
    if is_in_blackout(today, profile.blackout_start, profile.blackout_end):
        start = profile.blackout_start or "open"
        end = profile.blackout_end or "open"
        reason = f"Trading blackout window {start} to {end} is in effect (today: {today.isoformat()})."
        return {
            "ticker": ticker,
            "clearance_status": "BLOCKED",
            "restriction_reason": reason,
            "memo": f"{ticker}: trade BLOCKED — {reason} No orders may be placed until the window ends.",
        }
    return None


def _profile_gate(callback_context: CallbackContext) -> Optional[types.Content]:
    """before_agent_callback: short-circuit the LLM when a profile gate fires."""
    from portfolio_agent.tools.user_profile_db import get_user_profile

    ticker = str(callback_context.state.get("current_ticker") or "").upper()
    try:
        profile = get_user_profile()
    except Exception:
        return None  # profile unavailable — fall through to the normal LLM check
    result = profile_gate_result(ticker, profile)
    if result is None:
        return None
    payload = json.dumps(result)
    callback_context.state["clearance"] = payload
    return types.Content(role="model", parts=[types.Part(text=payload)])


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
    before_agent_callback=_profile_gate,
)

if __name__ == "__main__":
    import pathlib, sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from portfolio_agent.specialists._runner import run
    run(clearance_agent, "clearance")
