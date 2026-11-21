"""
Phase 4 of the chat assistant: model tiering. Tool selection stays on the flash chain; when a turn ran an engine or own-data tool, ONE
tool-less call on the analyst chain writes the final answer (setting chat_synthesis_tier, default 'analyst'); the flash answer is the
fallback; the reasoning chain is predict-only; a model out of credit is skipped (shared with APEX) and never retried in a loop.
"""
from __future__ import annotations

import inspect
import json
from queue import Queue
from types import SimpleNamespace

import pytest

import portfolio_agent.tools.apex_dual_run as adr
import portfolio_agent.tools.db as db_module
from portfolio_agent.services import investor_service as svc
from portfolio_agent.services.context import local_context
from portfolio_agent.tools import engine_settings_db as esdb
from web.chat import orchestration as orch

FLASH = [("f1", "groq", "Groq GPT-OSS-120B"), ("f2", "mistral", "Mistral-Small")]
ANALYST = [("a1", "openai", "GPT-5"), ("a2", "openai", "o3")]
REASONING = [("r1", "anthropic", "Claude-Sonnet")]
CREDIT = 'litellm.BadRequestError: DeepseekException - {"error":{"message":"Insufficient Balance"}}'


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    adr._OUT_OF_CREDIT.clear(); adr._DEAD_UNTIL.clear()
    yield
    adr._OUT_OF_CREDIT.clear(); adr._DEAD_UNTIL.clear()


class _TC:
    def __init__(self, cid, name, args="{}"):
        self.id, self.function = cid, SimpleNamespace(name=name, arguments=args)


def _msg(tool_calls=None, content=""):
    return SimpleNamespace(tool_calls=tool_calls, content=content)


def _resp(msg):
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


class _Stream:
    def __init__(self, text):
        self.text = text

    def __aiter__(self):
        async def gen():
            for part in self.text.split(" "):
                yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=part + " ", tool_calls=None))])
        return gen()


@pytest.fixture
def chat(monkeypatch):
    """A scripted chat: flash models call tools then answer; analyst models answer; `behaviour[model]` can raise. Records every call."""
    import litellm
    import portfolio_agent._models as models_mod
    st = SimpleNamespace(calls=[], executed=[], flash_tools=[("get_portfolio_summary", "{}")], flash_answer="FLASH draft answer",
                         analyst_answer="ANALYST final answer", fail={}, chains={"flash": FLASH, "analyst": ANALYST, "reasoning": REASONING},
                         tool_out=lambda n, a: json.dumps({"as_of": "2026-11-20", "total_value": 1234}))
    monkeypatch.setattr(models_mod, "FAILOVER_CHAINS", st.chains)

    async def fake(model, messages, **kw):
        st.calls.append({"model": model, "tools": "tools" in kw, "stream": kw.get("stream"),
                         "roles": [m.get("role") for m in messages], "text": " ".join(str(m.get("content")) for m in messages)})
        if model in st.fail:
            raise Exception(st.fail[model])
        if model.startswith("f"):
            tool_turns = sum(1 for m in messages if m.get("role") == "tool")
            if "tools" in kw and tool_turns < len(st.flash_tools):
                name, args = st.flash_tools[tool_turns]
                resp = _resp(_msg([_TC(f"t{tool_turns}", name, args)]))
            else:
                resp = _resp(_msg(None, st.flash_answer))
        else:
            resp = _resp(_msg(None, st.analyst_answer))
        return resp

    monkeypatch.setattr(litellm, "acompletion", fake)
    monkeypatch.setattr(orch, "_execute_chat_tool", lambda name, args, owner=None, **kw: st.executed.append(name) or st.tool_out(name, args))

    def run(query="how is my portfolio?", history=None):
        q = Queue()
        orch._run_chat_agent_thread(["AAPL"], query, history or [], q, "alice", None)
        ev = []
        while not q.empty():
            ev.append(q.get())
        return ev
    st.run = run
    return st


def models(chat):
    return [c["model"] for c in chat.calls]


# ═══ the setting ══════════════════════════════════════════════════════════════

def test_default_is_analyst_and_a_bad_value_is_rejected():
    assert esdb.DEFAULT_CHAT_SYNTHESIS_TIER == "analyst" and esdb.CHAT_SYNTHESIS_TIERS == ("flash", "analyst")
    assert esdb.get_engine_settings()["chat_synthesis_tier"] == "analyst" and esdb.get_chat_synthesis_tier() == "analyst"
    with pytest.raises(ValueError, match="chat_synthesis_tier"):
        esdb.update_engine_settings(updated_by="t", chat_synthesis_tier="reasoning")
    assert esdb.get_chat_synthesis_tier() == "analyst"                                   # a rejected value changes nothing
    assert esdb.get_chat_synthesis_tier({"chat_synthesis_tier": "nonsense"}) == "analyst"   # unreadable -> default, never an error
    assert esdb.get_chat_synthesis_tier({}) == "analyst"


def test_the_setting_is_versioned_audited_admin_only_and_resettable(monkeypatch):
    from portfolio_agent.tools import holdings_db as repo
    ctx = local_context("test")
    with pytest.raises(svc.NotAdmin):
        svc.update_chat_synthesis_tier(ctx, "flash")
    monkeypatch.setattr(svc, "require_admin", lambda c: None)
    v0 = esdb.get_engine_settings()["version"]
    assert svc.update_chat_synthesis_tier(ctx, "flash", expected_version=v0, reason="save credits") == "flash"
    assert esdb.get_chat_synthesis_tier() == "flash" and esdb.get_engine_settings()["version"] == v0 + 1
    with pytest.raises(RuntimeError):
        svc.update_chat_synthesis_tier(ctx, "analyst", expected_version=v0)                  # stale version refused
    with pytest.raises(ValueError):
        svc.update_chat_synthesis_tier(ctx, "bogus")
    assert svc.reset_chat_synthesis_tier(ctx) == "analyst"
    events = [e for e in repo.get_audit_events() if e["operation"] == "update_chat_synthesis_tier"]
    assert len(events) == 2
    assert events[1]["before"] == {"chat_synthesis_tier": "analyst"} and events[1]["after"] == {"chat_synthesis_tier": "flash"}
    assert events[1]["reason"] == "save credits" and events[0]["after"] == {"chat_synthesis_tier": "analyst"}


# ═══ tiering in the loop ══════════════════════════════════════════════════════

def test_an_own_data_turn_gets_exactly_one_tool_free_analyst_call_and_its_answer(chat):
    ev = chat.run()
    assert models(chat) == ["f1", "f1", "a1"]                                             # tool round, flash draft, ONE analyst call
    analyst = chat.calls[2]
    assert analyst["tools"] is False and "tool" not in analyst["roles"]                  # no tools, no tool-role messages (provider-safe)
    assert "total_value" in analyst["text"] and "get_portfolio_summary" in analyst["text"]    # it was given what the tool returned
    assert ("text", "ANALYST final answer") in ev and ("model_used", ("GPT-5", "openai")) in ev
    assert not any(k == "text" and v == "FLASH draft answer" for k, v in ev)
    assert chat.executed == ["get_portfolio_summary"]


def test_the_flash_tool_selection_never_uses_the_analyst_or_reasoning_chains(chat):
    chat.run()
    assert all(not c["tools"] for c in chat.calls if c["model"] in ("a1", "a2"))
    assert "r1" not in models(chat) and set(models(chat)) <= {"f1", "a1"}


def test_a_turn_without_an_own_data_or_engine_tool_makes_no_analyst_call(chat):
    chat.flash_tools = [("get_ticker_news", '{"ticker": "AAPL"}')]
    ev = chat.run("any news on AAPL?")
    assert models(chat) == ["f1", "f1"] and ("text", "FLASH draft answer") in ev and ("model_used", ("Groq GPT-OSS-120B", "groq")) in ev
    chat.calls.clear(); chat.flash_tools = []
    chat.run("hello")
    assert models(chat) == ["f1"]                                                         # no tools at all: nothing to synthesise


@pytest.mark.parametrize("tool", ["get_personal_opportunities", "recommend_cash_allocation", "simulate_hypothetical_trades",
                                  "get_fund_lookthrough", "get_valuation", "get_trade_plan_odds", "get_tax_lots_and_gains",
                                  "get_investor_profile", "get_macro_calendar", "get_sec_filings", "get_latest_opportunities",
                                  "get_portfolio_summary", "get_portfolio_sector_allocation", "get_portfolio_risk_flags", "get_portfolio_health"])
def test_every_engine_or_portfolio_tool_triggers_the_analyst(chat, tool):
    chat.flash_tools = [(tool, "{}")]
    chat.run()
    assert models(chat).count("a1") == 1


def test_flash_tier_turns_the_extra_call_off(chat):
    esdb.update_engine_settings(updated_by="t", chat_synthesis_tier="flash")
    ev = chat.run()
    assert models(chat) == ["f1", "f1"] and ("text", "FLASH draft answer") in ev


def test_if_every_analyst_model_fails_the_flash_answer_is_used_and_each_is_tried_once(chat):
    chat.fail = {"a1": "429 rate limit", "a2": "503 overloaded"}
    ev = chat.run()
    assert models(chat).count("a1") == 1 and models(chat).count("a2") == 1               # no retry loops on the same model
    assert ("text", "FLASH draft answer") in ev
    assert any(k == "warn" and "analyst models were not available" in v for k, v in ev)
    assert ("model_used", ("Groq GPT-OSS-120B", "groq")) in ev


def test_the_flash_draft_is_hidden_and_the_analyst_answer_streams(chat, monkeypatch):
    import litellm
    inner = litellm.acompletion

    async def streaming(model, messages, **kw):
        out = await inner(model, messages, **kw)
        if kw.get("stream") and not out.choices[0].message.tool_calls:
            return _Stream(out.choices[0].message.content)
        return out
    monkeypatch.setattr(litellm, "acompletion", streaming)
    monkeypatch.setattr(litellm, "stream_chunk_builder", lambda chunks, messages=None: _resp(_msg(
        None, "".join(c.choices[0].delta.content for c in chunks).strip())), raising=False)
    ev = chat.run()
    deltas = "".join(v for k, v in ev if k == "delta")
    assert "ANALYST" in deltas and "FLASH" not in deltas                                  # the user only ever sees the analyst's text
    assert ("text", "ANALYST final answer") in ev


def test_a_credit_failure_marks_the_model_and_the_next_turn_skips_it_without_a_call(chat):
    chat.fail = {"a1": CREDIT}
    chat.run()
    assert "GPT-5" in adr._OUT_OF_CREDIT and models(chat).count("a1") == 1 and models(chat).count("a2") == 1
    chat.calls.clear(); chat.fail = {}
    ev = chat.run()
    assert "a1" not in models(chat) and models(chat).count("a2") == 1                    # the dead model is not paid for again
    assert ("model_used", ("o3", "openai")) in ev


def test_the_dead_model_memory_is_shared_with_apex_marks(chat):
    adr._OUT_OF_CREDIT.add("GPT-5")                                                      # APEX discovered it first
    chat.run()
    assert "a1" not in models(chat) and "a2" in models(chat)


def test_a_dead_flash_model_is_skipped_and_the_next_one_is_used(chat):
    chat.fail = {"f1": CREDIT}
    chat.run()
    assert "Groq GPT-OSS-120B" in adr._OUT_OF_CREDIT
    chat.calls.clear(); chat.fail = {}
    ev = chat.run()
    assert "f1" not in models(chat) and "f2" in models(chat)
    assert ("text", "ANALYST final answer") in ev


def test_when_every_flash_model_is_skipped_or_failing_the_turn_says_so(chat):
    adr._OUT_OF_CREDIT.update({"Groq GPT-OSS-120B"})
    chat.fail = {"f2": "429 rate limit"}
    ev = chat.run()
    assert any(k == "error" for k, _ in ev) and ("done", "") in ev


def test_chat_mode_code_never_references_the_reasoning_chain():
    for fn in (orch._run_chat_agent_thread, orch._synthesize_answer, orch._chat_round):
        src = inspect.getsource(fn)
        assert '"reasoning"' not in src and "'reasoning'" not in src, fn.__name__
    assert '"reasoning"' in inspect.getsource(orch._run_apex_thread)                      # ...it is predict mode's


def test_the_model_that_wrote_the_answer_is_reported_in_model_used(chat):
    ev = chat.run()
    assert [v for k, v in ev if k == "model_used"] == [("GPT-5", "openai")]
    chat.calls.clear(); chat.flash_tools = []
    assert [v for k, v in chat.run("hello") if k == "model_used"] == [("Groq GPT-OSS-120B", "groq")]


def test_the_synthesis_prompt_carries_the_safety_rules_and_the_question_and_history(chat):
    history = [{"role": "user", "content": "earlier question"}, {"role": "assistant", "content": "earlier answer"}]
    chat.run("how is my portfolio?", history=history)
    text = chat.calls[-1]["text"]
    for needle in ("earlier question", "earlier answer", "how is my portfolio?", "UNTRUSTED_DATA", "FINAL answer", "Do not call tools"):
        assert needle in text, needle
