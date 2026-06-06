"""
Agent runner — single-ticker analysis with model failover.

Public API:
  run_analysis(ticker, agent_name, …)              — run one agent, no failover
  run_analysis_with_failover(ticker, agent_name, …) — loop through failover chain
"""

from __future__ import annotations

import json
import sys

from portfolio_agent.log import get_logger as _get_logger
from portfolio_agent._models import GEMINI_COUNTER, ModelSession  # noqa: F401 (re-exported)
from portfolio_agent.pipeline.failover import (
    CreditExhaustedError,
    _is_failover_error,
    _is_ticker_only_fallback,
)
from portfolio_agent.pipeline.rendering import pretty_print as _pretty_print


# Legacy alias — keeps existing call-sites compiling
_is_credit_error = _is_failover_error


def _get_agent_factory(name: str):
    """Return (make_fn, default_agent) for the given specialist name."""
    if name == "fundamentals":
        from portfolio_agent.specialists.fundamentals import make_fundamentals_agent, fundamentals_agent
        return make_fundamentals_agent, fundamentals_agent
    if name == "news":
        from portfolio_agent.specialists.news import make_news_agent, news_agent
        return make_news_agent, news_agent
    if name == "macro":
        from portfolio_agent.specialists.macro import make_macro_agent, macro_agent
        return make_macro_agent, macro_agent
    if name == "research":
        from portfolio_agent.specialists.research import make_research_agent, research_agent
        return make_research_agent, research_agent
    if name == "risk":
        from portfolio_agent.specialists.risk import make_risk_agent, risk_agent
        return make_risk_agent, risk_agent
    if name == "reasoning":
        from portfolio_agent.specialists.reasoning import make_reasoning_agent, reasoning_agent
        return make_reasoning_agent, reasoning_agent
    # Non-factory agents (no failover; fast/utility)
    if name == "technical":
        from portfolio_agent.specialists.technical import technical_agent
        return None, technical_agent
    if name == "synthesis":
        from portfolio_agent.synthesis import synthesis_agent
        return None, synthesis_agent
    if name == "clearance":
        from portfolio_agent.clearance import clearance_agent
        return None, clearance_agent
    if name == "all":
        from portfolio_agent.agent import root_agent
        return None, root_agent
    return None, None


def _get_agent(name: str):
    """Return the default agent for name (backward-compat wrapper)."""
    _, agent = _get_agent_factory(name)
    return agent


async def run_analysis(
    ticker: str,
    agent_name: str = "all",
    output_json: bool = False,
    query: str = "",
    _agent_override=None,
    _model_label: str = "",
) -> dict:
    """Run *agent_name* for *ticker* and return the final session state."""
    log = _get_logger("main")
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    _valid = {"fundamentals", "news", "macro", "technical", "research",
              "risk", "synthesis", "clearance", "reasoning", "all"}
    if agent_name not in _valid:
        log.error(
            f"[error] Unknown agent '{agent_name}'. Choose from: {', '.join(sorted(_valid))}",
            event_type="error",
        )
        sys.exit(1)

    agent = _agent_override if _agent_override is not None else _get_agent(agent_name)
    session_service = InMemorySessionService()
    runner = Runner(
        agent=agent,
        app_name="portfolio_agent",
        session_service=session_service,
    )

    user_query   = query.strip() if query.strip() else "Full analysis"
    message_text = query.strip() if query.strip() else f"Analyze {ticker.upper()}"

    session = await session_service.create_session(
        app_name="portfolio_agent",
        user_id="cli_user",
        state={
            "current_ticker": ticker.upper(),
            "user_query":     user_query,
        },
    )

    message = types.Content(
        role="user",
        parts=[types.Part(text=message_text)],
    )

    label = f"[{agent_name}]" if agent_name != "all" else "[full pipeline]"
    log.info(f"\n{'━' * 64}", event_type="separator")
    log.info(f"  {ticker.upper()}  {label}  — {user_query}", event_type="phase_start")
    log.info(f"{'━' * 64}", event_type="separator")

    try:
        async for event in runner.run_async(
            user_id="cli_user",
            session_id=session.id,
            new_message=message,
        ):
            if not (hasattr(event, "content") and event.content):
                continue
            author = getattr(event, "author", "agent")
            for part in (event.content.parts or []):
                if hasattr(part, "text") and part.text:
                    print(part.text, end="", flush=True)
                elif hasattr(part, "function_call") and part.function_call:
                    fc = part.function_call
                    args_str = json.dumps(fc.args) if fc.args else ""
                    log.info(f"\n  ▶ [{author}] {fc.name}({args_str})",
                             event_type="llm_tool_call", author=author, tool=fc.name)
                elif hasattr(part, "function_response") and part.function_response:
                    fr = part.function_response
                    resp    = str(fr.response)
                    preview = resp[:300] + ("…" if len(resp) > 300 else "")
                    log.info(f"  ◀ [{author}] {fr.name} → {preview}",
                             event_type="llm_tool_response", author=author, tool=fr.name)
    except Exception as exc:
        if _is_credit_error(exc):
            raise CreditExhaustedError(str(exc)) from exc
        raise

    print()

    final_session = await session_service.get_session(
        app_name="portfolio_agent",
        user_id="cli_user",
        session_id=session.id,
    )
    state: dict = dict(final_session.state) if final_session else {}

    if output_json:
        printable = {k: v for k, v in state.items() if k != "current_ticker"}
        log.info(json.dumps(printable, indent=2), event_type="summary")
    else:
        _pretty_print(ticker, agent_name, state)

    return state


async def run_analysis_with_failover(
    ticker: str,
    agent_name: str,
    query: str = "",
    output_json: bool = False,
    model_session: "ModelSession | None" = None,
) -> dict:
    """
    Run *agent_name* with automatic model failover.

    When *model_session* is provided (recommended for batch runs), the session
    tracks which model is currently working across the whole batch.

    Returns the session state dict with two extra keys injected:
      _model_used      — display name of the model that succeeded
      _model_provider  — provider label ("anthropic", "google", "groq")
    """
    log = _get_logger("main")
    from portfolio_agent._models import FAILOVER_CHAINS, AGENT_TASK_TYPE, _lite

    task_type = AGENT_TASK_TYPE.get(agent_name, "fast")
    chain     = FAILOVER_CHAINS.get(task_type, FAILOVER_CHAINS["fast"])

    if model_session is not None:
        if model_session.exhausted:
            raise CreditExhaustedError(
                f"All models exhausted for {agent_name} (batch session)."
            )
        model_session.print_remaining()
        start = model_session.start_idx
    else:
        from portfolio_agent._models import print_chain
        print_chain(agent_name)
        start = 0

    make_fn, default_agent = _get_agent_factory(agent_name)

    for i in range(start, len(chain)):
        model_id, provider, label = chain[i]
        agent = make_fn(model=_lite(model_id)) if make_fn is not None else default_agent

        try:
            log.info(
                f"  ⟳ [{agent_name}] trying {label} ({provider})  [{i+1}/{len(chain)}]",
                event_type="llm_call_start", model=label, provider=provider,
                model_idx=i, chain_len=len(chain),
            )
            state = await run_analysis(
                ticker=ticker,
                agent_name=agent_name,
                output_json=output_json,
                query=query,
                _agent_override=agent,
                _model_label=label,
            )
            state["_model_used"]     = label
            state["_model_provider"] = provider
            if provider == "google":
                GEMINI_COUNTER.record(model_id)
            return state

        except Exception as exc:
            persistent   = _is_failover_error(exc)
            ticker_only  = not persistent and _is_ticker_only_fallback(exc)
            if persistent or ticker_only:
                if persistent and model_session is not None:
                    model_session.on_failure(i)
                if i < len(chain) - 1:
                    next_label = chain[i + 1][2]
                    tag = "" if persistent else " [ticker-only]"
                    log.warning(
                        f"  ⚠ {label} failed{tag} ({type(exc).__name__}: "
                        f"{str(exc)[:120]}). Falling back to {next_label}…",
                        event_type="llm_call_error", model=label, provider=provider,
                        error_type=type(exc).__name__, error=str(exc)[:200],
                    )
                    continue
                else:
                    raise CreditExhaustedError(
                        f"All models exhausted for {agent_name}. Last error: {exc}"
                    ) from exc
            raise

    raise CreditExhaustedError(f"Failover chain empty for {agent_name}.")
