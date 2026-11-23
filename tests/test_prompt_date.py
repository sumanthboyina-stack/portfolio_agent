"""
Every prompt that asks a model about markets, rates, news or timing carries today's date, so it cannot answer as if it were still
its training date (the chat once said the next Fed meeting was in December 2023). No network, no LLM: the model calls are stubs.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from portfolio_agent.pipeline import prompt_date as pd_mod

# fixtures and stand-ins the chat-loop and routing tests already use
from tests.test_chat_loop import _msg, _resp, harness  # noqa: F401
from tests.test_etf_routing import _full_loaders, _isolated_db, _model_result, _typed  # noqa: F401

# 03:30 UTC on 23 November is still the evening of Sunday 22 November in US Central
_FIXED = datetime(2026, 11, 23, 3, 30, tzinfo=timezone.utc)
_EXPECTED = "Sunday, November 22, 2026 (2026-11-22, US Central)"


@pytest.fixture
def fixed_now(monkeypatch):
    monkeypatch.setattr(pd_mod, "_now", lambda: _FIXED.astimezone(pd_mod._TZ))


def test_the_line_names_the_central_date_and_tells_the_model_not_to_assume():
    line = pd_mod.today_line(_FIXED)
    assert f"Today's date: {_EXPECTED}" in line
    assert "training knowledge ends before this date" in line and "do not describe an earlier date as now" in line
    assert pd_mod.with_today("PROMPT", _FIXED) == line + "\n\nPROMPT"


def test_without_an_argument_the_line_uses_the_current_day():
    today = datetime.now(pd_mod._TZ)
    assert f"{today.year} ({today.date().isoformat()}, US Central)" in pd_mod.today_line()


def test_the_chat_system_prompt_carries_the_date(harness, fixed_now):
    harness.script = [_resp(_msg(content="ok"))]
    harness.run(query="should I wait for the next Fed meeting?")
    system = harness.calls[0]["messages"][0]
    assert system["role"] == "system" and f"Today's date: {_EXPECTED}" in system["content"]


def test_the_daily_apex_prompt_carries_the_date(monkeypatch, fixed_now):
    from portfolio_agent.tools import apex_dual_run as adr
    from portfolio_agent.tools import market_context as market_context_mod
    from portfolio_agent.tools import prediction_db as pdb
    from portfolio_agent.tools import reasoning_tools as rt_mod
    from portfolio_agent.tools import yfinance_tools as yft
    import portfolio_agent.pipeline.daily.apex as apex_mod
    _typed("AAPL", "STOCK")
    monkeypatch.setattr(market_context_mod, "default_loaders", _full_loaders)
    monkeypatch.setattr(rt_mod, "get_macro_snapshot", lambda: '{"vix": 20.0}')
    monkeypatch.setattr(pdb, "is_trading_day", lambda d: True)
    monkeypatch.setattr(yft, "get_close", lambda t, d: 100.0)
    monkeypatch.setattr(yft, "get_trailing_return", lambda t, d: 0.01)
    prompts: list[str] = []

    async def _fake_run_apex(ticker, prompt, **kw):
        prompts.append(prompt)
        return _model_result(ticker)
    monkeypatch.setattr(adr, "run_apex", _fake_run_apex)
    asyncio.run(apex_mod._run_daily_apex(["AAPL"], scheduled_horizons_override=[5]))
    assert prompts and prompts[0].startswith(f"Today's date: {_EXPECTED}")


def test_the_news_scorer_prompt_carries_the_date(monkeypatch, fixed_now):
    import litellm
    import portfolio_agent._models as models_mod
    from portfolio_agent.tools import news_filter
    sent: list = []

    def _fake_completion(model, messages, **kw):
        sent.append(messages)
        return type("R", (), {"choices": [type("C", (), {"message": type("M", (), {"content": "[]"})()})()]})()
    monkeypatch.setattr(litellm, "completion", _fake_completion)
    monkeypatch.setitem(models_mod.FAILOVER_CHAINS, "triage", [("m1", "groq", "Groq")])
    news_filter._call_scorer("AAPL: a headline")
    assert sent and sent[0][0]["content"].startswith(f"Today's date: {_EXPECTED}")


def test_no_pipeline_prompt_is_built_without_the_date():
    """A guard for future call sites: every place a batch prompt template is formatted goes through with_today."""
    root = Path(__file__).resolve().parents[1] / "portfolio_agent" / "pipeline" / "daily"
    missing = []
    for path in sorted(root.glob("*.py")):
        text = path.read_text()
        for m in re.finditer(r"(_BATCH_[A-Z]+_PROMPT|_APEX_PROMPT_TEMPLATE)\.format\(", text):
            if not text[max(0, m.start() - 12):m.start()].endswith("with_today("):
                missing.append(f"{path.name}: {m.group(1)}")
    assert missing == []
