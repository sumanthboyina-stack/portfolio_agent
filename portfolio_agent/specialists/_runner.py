"""Shared standalone runner — lets any specialist be run directly for testing."""

from __future__ import annotations

import asyncio
import json
import sys

from portfolio_agent.log import get_logger as _get_logger

_log = _get_logger("specialist")


def run(agent, output_key: str) -> None:
    """
    Run *agent* for a single ticker and print tool calls + result.

    Usage:
        python portfolio_agent/specialists/news.py AAPL
        python portfolio_agent/specialists/news.py AAPL "What is the sentiment trend?"
    """
    from dotenv import load_dotenv
    load_dotenv()

    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    ticker = sys.argv[1].upper() if len(sys.argv) > 1 else "AAPL"
    query  = sys.argv[2] if len(sys.argv) > 2 else "Full analysis"
    _log.info(f"[{agent.name}] {ticker} — {query}", event_type="phase_start",
              agent=agent.name, ticker=ticker, query=query)

    async def _run() -> dict:
        svc = InMemorySessionService()
        runner = Runner(agent=agent, app_name="portfolio_agent", session_service=svc)

        session = await svc.create_session(
            app_name="portfolio_agent",
            user_id="cli",
            state={"current_ticker": ticker, "user_query": query},
        )

        async for event in runner.run_async(
            user_id="cli",
            session_id=session.id,
            new_message=types.Content(
                role="user",
                parts=[types.Part(text=query)],
            ),
        ):
            if not (hasattr(event, "content") and event.content):
                continue
            author = getattr(event, "author", "agent")
            for part in event.content.parts or []:
                if hasattr(part, "text") and part.text:
                    print(part.text, end="", flush=True)
                elif hasattr(part, "function_call") and part.function_call:
                    fc = part.function_call
                    args_str = json.dumps(fc.args) if fc.args else ""
                    _log.info(f"\n  ▶ [{author}] {fc.name}({args_str})",
                              event_type="llm_tool_call", author=author, tool=fc.name)
                elif hasattr(part, "function_response") and part.function_response:
                    fr = part.function_response
                    resp = str(fr.response)
                    preview = resp[:300] + ("…" if len(resp) > 300 else "")
                    _log.info(f"  ◀ [{author}] {fr.name} → {preview}",
                              event_type="llm_tool_response", author=author, tool=fr.name)
        print()

        final = await svc.get_session(
            app_name="portfolio_agent",
            user_id="cli",
            session_id=session.id,
        )
        return dict(final.state) if final else {}

    state = asyncio.run(_run())
    raw = state.get(output_key)
    if raw:
        try:
            _log.info(json.dumps(json.loads(raw), indent=2), event_type="summary")
        except Exception:
            _log.info(str(raw), event_type="summary")
