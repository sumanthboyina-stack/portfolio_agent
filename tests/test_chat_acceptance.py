"""
Acceptance checks for the portfolio-aware chat (chat-portfolio-assistant), end to end with mocked data:
routing -> the scripted fast model picks tools -> the REAL tool dispatcher and wrappers run on a seeded database -> the analyst chain writes
the answer -> digests are stored and replayed. The model, SEC, yfinance and the market-data engines are mocked; nothing touches the network.

  1  "Analyze my portfolio"                          chat mode, portfolio tools, no ticker prompt
  2  "NVDA news"                                     chat mode, no APEX run, no prediction row
  3  "I have $5,000, where should it go?"            recommend_cash_allocation, restricted list respected, analyst synthesis
  4  "What if I sell half my AAPL and buy VOO?"      simulate_hypothetical_trades, labelled hypothetical
  5  "How much NVDA do I really own incl. funds?"    get_fund_lookthrough
  6  follow-up "which of those do I already own?"    answered from the stored tool digest, no re-fetch
  7  user A never sees user B's data
"""
from __future__ import annotations

import json
import sqlite3
from decimal import Decimal as D
from queue import Queue
from types import SimpleNamespace

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.services.context import RequestContext, _mint_identity_for_tests, local_context
from portfolio_agent.tools import prediction_db as pdb
from tests.test_chat_engine_tools import BOB, ALICE, hold
from web.chat import orchestration as orch
from web.chat.routing import route_message

AL, BO = ALICE.actor, BOB.actor
FLASH, ANALYST = [("f1", "groq", "Groq GPT-OSS-120B")], [("a1", "openai", "GPT-5")]


class _TC:
    def __init__(self, cid, name, args):
        self.id, self.function = cid, SimpleNamespace(name=name, arguments=json.dumps(args))


def _resp(content=None, tool_calls=None):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=tool_calls, content=content))])


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    import socket
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(pdb, "_today_cst", lambda: "2026-10-26")
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: (_ for _ in ()).throw(OSError("network blocked")))
    import portfolio_agent.tools.apex_dual_run as adr
    adr._OUT_OF_CREDIT.clear(); adr._DEAD_UNTIL.clear()


@pytest.fixture
def chat(monkeypatch):
    """Scripted LiteLLM: the fast model asks for `plan[query]` tools then answers; the analyst writes from whatever it was handed."""
    import litellm
    import portfolio_agent._models as models_mod
    monkeypatch.setattr(models_mod, "FAILOVER_CHAINS", {"flash": FLASH, "analyst": ANALYST, "reasoning": [("r1", "anthropic", "Claude-Sonnet")]})
    st = SimpleNamespace(plan={}, calls=[], apex_runs=[])

    async def fake(model, messages, **kw):
        user = next(m["content"] for m in reversed(messages) if m["role"] == "user")
        st.calls.append({"model": model, "tools": "tools" in kw, "text": " ".join(str(m.get("content")) for m in messages),
                         "roles": [m["role"] for m in messages]})
        if model == "a1":
            return _resp("ANALYST: " + ("held" if "already_held" in st.calls[-1]["text"] else "answer"))
        steps = next((v for k, v in st.plan.items() if k in user), [])
        done = sum(1 for m in messages if m["role"] == "tool")
        if "tools" in kw and done < len(steps):
            name, args = steps[done]
            return _resp(tool_calls=[_TC(f"c{done}", name, args)])
        return _resp("FLASH draft")
    monkeypatch.setattr(litellm, "acompletion", fake)
    monkeypatch.setattr(orch, "_run_apex_thread", lambda *a, **k: st.apex_runs.append(a))

    def turn(query, ctx=ALICE, history=None, mentioned=()):
        r = route_message(query, list(mentioned), [])
        q = Queue()
        if r.mode == "chat":
            orch._run_chat_agent_thread(r.tickers, query, history or [], q, ctx.actor, ctx)
        ev = []
        while not q.empty():
            ev.append(q.get())
        return r, ev
    st.turn = turn
    return st


def results_handed_to_analyst(chat, index: int = 0) -> str:
    """Only the 'tool results gathered' part of what the analyst was given (the system prompt itself mentions example tickers)."""
    texts = [c["text"] for c in chat.calls if c["model"] == "a1"]
    return texts[index].split("[Tool results gathered for this question", 1)[1]


def kinds(ev, k):
    return [v for kind, v in ev if kind == k]


def tool_names(ev):
    return [v.split("(", 1)[0] for v in kinds(ev, "tool")]


def predictions_count():
    with sqlite3.connect(str(db_module.DB_PATH)) as c:
        try:
            return c.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
        except sqlite3.OperationalError:
            return 0


# ── 1 ─────────────────────────────────────────────────────────────────────────

def test_1_analyze_my_portfolio_is_chat_with_portfolio_tools_and_no_ticker_prompt(chat, monkeypatch):
    import web.chat.tools as tools
    monkeypatch.setattr(tools, "_search_ticker_by_name", lambda *a, **k: pytest.fail("a personal question must never resolve a company name"))
    hold(ALICE, "AAPL"); hold(ALICE, "MSFT", shares=5)
    chat.plan = {"Analyze my portfolio": [("get_portfolio_summary", {}), ("get_portfolio_sector_allocation", {})]}
    route, ev = chat.turn("Analyze my portfolio")
    assert route.mode == "chat" and route.reason == "personal"
    assert tool_names(ev) == ["get_portfolio_summary", "get_portfolio_sector_allocation"]
    assert chat.apex_runs == [] and not any(k == "error" for k, _ in ev)
    assert kinds(ev, "text") == ["ANALYST: answer"]                                    # own-data turn: the analyst wrote it
    gathered = results_handed_to_analyst(chat)
    assert "get_portfolio_summary" in gathered and "AAPL" in gathered and "MSFT" in gathered


# ── 2 ─────────────────────────────────────────────────────────────────────────

def test_2_nvda_news_is_chat_with_no_apex_run_and_no_prediction_row(chat, monkeypatch):
    import web.chat.tools as tools
    monkeypatch.setattr(tools, "_chat_tool_get_news", lambda t: {"ticker": t, "news": [{"title": "NVDA beats"}]})
    before = predictions_count()
    chat.plan = {"NVDA news": [("get_ticker_news", {"ticker": "NVDA"})]}
    route, ev = chat.turn("NVDA news", mentioned=["NVDA"])
    assert route.mode == "chat" and "Analyze NVDA" in (route.suggest or "")
    assert tool_names(ev) == ["get_ticker_news"] and chat.apex_runs == []
    assert predictions_count() == before                                                # nothing was written
    assert [c["model"] for c in chat.calls] == ["f1", "f1"]                             # a news lookup never reaches the analyst


# ── 3 ─────────────────────────────────────────────────────────────────────────

def test_3_cash_allocation_respects_the_restricted_list_and_uses_the_analyst_chain(chat, monkeypatch):
    from portfolio_agent.tools.restricted_list_db import add_restricted
    import portfolio_agent.tools.portfolio_optimizer as po
    hold(ALICE, "AAPL")
    add_restricted("NVDA", "employer blackout", AL)
    monkeypatch.setattr(po, "recommend_allocation", lambda cash, **kw: {
        "allocation": [{"ticker": "NVDA", "kind": "new", "amount": 2500.0, "shares": 3, "why": "strong fit"},
                       {"ticker": "MSFT", "kind": "new", "amount": 2400.0, "shares": 5, "why": "diversifier"}],
        "reduce": [], "cash_reserved": 100.0, "impact": {"before": None, "after": None},
        "scenario": {"status": "feasible", "constraints": [{"name": "issuer", "status": "pass"}], "warnings": []},
        "inputs": {"price_as_of": "2026-10-26"}})
    chat.plan = {"where should it go": [("recommend_cash_allocation", {"cash_amount": 5000})]}
    route, ev = chat.turn("I have $5,000, where should it go?")
    assert route.mode == "chat" and tool_names(ev) == ["recommend_cash_allocation"]
    tool_result = next(c for c in chat.calls if c["model"] == "a1")["text"]
    assert '"restricted_or_blocked"' in tool_result and '"ticker": "NVDA"' in tool_result
    allocation = json.loads(tool_result.split("### recommend_cash_allocation(", 1)[1].split(")\n", 1)[1].split("\n\n", 1)[0])
    assert [a["ticker"] for a in allocation["allocation"]] == ["MSFT"]                   # the restricted ticker is not suggested
    assert allocation["restricted_or_blocked"][0]["ticker"] == "NVDA" and allocation["cash_not_allocated"] == 2600.0
    analyst = next(c for c in chat.calls if c["model"] == "a1")
    assert analyst["tools"] is False and "tool" not in analyst["roles"]
    assert ("model_used", ("GPT-5", "openai")) in ev and kinds(ev, "text") == ["ANALYST: answer"]
    assert not any(c["model"] == "r1" for c in chat.calls)                              # never the premium chain


# ── 4 ─────────────────────────────────────────────────────────────────────────

def test_4_a_what_if_goes_through_the_scenario_engine_and_is_labelled_hypothetical(chat, monkeypatch):
    import portfolio_agent.scenarios.prepare as prep
    from portfolio_agent.scenarios.engine import Assumptions, Limits, Position, ScenarioInputs
    acct = hold(ALICE, "AAPL", shares=10)
    aid = acct["account_id"]
    monkeypatch.setattr(prep, "prepare_inputs", lambda **kw: (ScenarioInputs(
        scope="all", account_ids=(aid,), account_labels={aid: "Main"}, positions=(Position(aid, "AAPL", D(10), "Tech"),),
        prices={"AAPL": D(100), "VOO": D(400)}, price_as_of="2026-10-26", starting_cash={aid: D(0)}, external_contribution={aid: D(0)},
        limits=Limits(D("0.5"), D("0.5"), D("1"), D("1")), assumptions=Assumptions()), {"funding_account_id": aid}))
    chat.plan = {"What if I sell half": [("simulate_hypothetical_trades", {"trades": [
        {"ticker": "AAPL", "side": "SELL", "fraction": 0.5}, {"ticker": "VOO", "side": "BUY", "amount": 500}]})]}
    route, ev = chat.turn("What if I sell half my AAPL and buy VOO?")
    assert route.mode == "chat" and tool_names(ev) == ["simulate_hypothetical_trades"]
    handed = next(c for c in chat.calls if c["model"] == "a1")["text"]
    assert "HYPOTHETICAL" in handed and "constraint_violations" in handed and "not a forecast" in handed
    assert '"quantity": "5.0000"' in handed                                                # half of 10 shares, from the engine, not the model
    assert kinds(ev, "text") == ["ANALYST: answer"]


# ── 5 ─────────────────────────────────────────────────────────────────────────

def test_5_how_much_nvda_including_funds_uses_the_look_through(chat, monkeypatch):
    import portfolio_agent.tools.portfolio_assessment as pa
    hold(ALICE, "NVDA", shares=2); hold(ALICE, "VTI", shares=20)
    hh = SimpleNamespace(named_issuers=lambda: {"NVDA": {"value": 700.0, "via": {"VTI": 500.0, "NVDA": 200.0}}}, overlap=lambda: [])
    monkeypatch.setattr(pa, "compute_exposures_for_owner", lambda owner, portfolio, **kw: SimpleNamespace(
        total_value=10000.0, holdings_count=2, built_at="2026-10-26", lookthrough_applicable=True, lookthrough_used=True,
        lookthrough_coverage_pct=92.0, lookthrough_min_coverage_pct=70.0, lookthrough_reason_codes=(),
        lookthrough_issuer_pct={"NVDA": 7.0}, lookthrough_issuer_via_pct={"NVDA": 5.0}, lookthrough_sector_pct={"Technology": 38.0},
        leveraged_inverse_pct=0.0, per_holding_weight_pct={"NVDA": 2.0}, issuer_of={}, lookthrough=SimpleNamespace(household=hh), raw_holdings=()))
    monkeypatch.setattr(pa, "issuer_key_for", lambda t, e: t)
    chat.plan = {"really own": [("get_fund_lookthrough", {"ticker": "NVDA"})]}
    route, ev = chat.turn("How much NVDA do I really own including funds?", mentioned=["NVDA"])
    assert route.mode == "chat" and tool_names(ev) == ["get_fund_lookthrough"]
    handed = next(c for c in chat.calls if c["model"] == "a1")["text"]
    assert '"direct_pct": 2.0' in handed and '"via_funds_pct": 5.0' in handed and '"total_pct": 7.0' in handed


# ── 6 ─────────────────────────────────────────────────────────────────────────

def test_6_a_follow_up_is_answered_from_the_stored_digest_without_refetching(chat, monkeypatch):
    import portfolio_agent.tools.opportunity_engine as oe
    hold(ALICE, "AAPL")
    monkeypatch.setattr(oe, "get_daily_opportunities", lambda top_n=10, holdings=None: [
        {"ticker": "AAPL", "rank": 1, "call": "BUY", "opportunity_score": 80, "composite_score": 8.0, "why": "x"},
        {"ticker": "MSFT", "rank": 2, "call": "BUY", "opportunity_score": 70, "composite_score": 7.0, "why": "y"}])
    monkeypatch.setattr(oe, "latest_trending_opportunity_date", lambda: "2026-10-26")
    chat.plan = {"what looks good": [("get_personal_opportunities", {})]}
    _, ev1 = chat.turn("what looks good for me?")
    digests = kinds(ev1, "digests")[0]
    assert digests[0]["tool"] == "get_personal_opportunities" and {r["ticker"] for r in digests[0]["rows"]} == {"AAPL", "MSFT"}
    # what the page stores and replays on the next turn
    messages = [{"role": "user", "content": "what looks good for me?", "type": "text", "metadata": {}},
                {"role": "assistant", "content": kinds(ev1, "text")[0], "type": "text", "metadata": {"tool_digests": digests}}]
    history = orch._build_chat_history(messages)
    calls_before = len(chat.calls)
    chat.plan = {}                                                                       # the model has no tool plan: it must use the digest
    route, ev2 = chat.turn("which of those do I already own?", history=history)
    assert route.mode == "chat" and tool_names(ev2) == [] and kinds(ev2, "digests") == []   # no tool ran
    seen_by_model = chat.calls[calls_before]["text"]
    assert "[Tool digests" in seen_by_model and '"already_held":true' in seen_by_model and '"ticker":"MSFT"' in seen_by_model
    assert len(chat.calls) == calls_before + 1                                          # a single model call, nothing re-fetched


# ── 7 ─────────────────────────────────────────────────────────────────────────

def test_7_user_a_never_sees_user_b_through_any_tool(chat, monkeypatch):
    import portfolio_agent.tools.opportunity_engine as oe
    hold(ALICE, "AAPL", account="Alice IRA"); hold(BOB, "TSLA", account="Bob Roth")
    seen_books = []
    monkeypatch.setattr(oe, "get_daily_opportunities", lambda top_n=10, holdings=None: seen_books.append(sorted({h["ticker"] for h in holdings})) or [])
    steps = [("get_portfolio_summary", {}), ("get_portfolio_sector_allocation", {}), ("get_personal_opportunities", {}),
             ("get_tax_lots_and_gains", {}), ("get_investor_profile", {})]
    chat.plan = {"everything": steps}
    _, ev_a = chat.turn("show me everything about my portfolio", ctx=ALICE)
    _, ev_b = chat.turn("show me everything about my portfolio", ctx=BOB)
    a_text = " ".join(kinds(ev_a, "result")) + json.dumps(kinds(ev_a, "digests"))
    b_text = " ".join(kinds(ev_b, "result")) + json.dumps(kinds(ev_b, "digests"))
    assert "TSLA" not in a_text and "Bob" not in a_text and "AAPL" not in b_text and "Alice" not in b_text
    assert seen_books == [["AAPL"], ["TSLA"]]
    handed_a, handed_b = results_handed_to_analyst(chat, 0), results_handed_to_analyst(chat, 1)
    assert "AAPL" in handed_a and "TSLA" not in handed_a and "Bob" not in handed_a
    assert "TSLA" in handed_b and "AAPL" not in handed_b and "Alice" not in handed_b
    # and with no signed-in account the personal tools say so instead of falling back
    _, ev_none = chat.turn("show me everything about my portfolio", ctx=RequestContext(identity=_mint_identity_for_tests("nobody"), source="t"))
    assert "AAPL" not in " ".join(kinds(ev_none, "result")) and "TSLA" not in " ".join(kinds(ev_none, "result"))
