"""
Phase 0: execution tracing and characterization tests.

Read-only investigation — no production code was changed for this phase.
Traced by direct inspection of source (not docstrings) at the current commit:
agent.py, synthesis.py, clearance.py, pipeline/batch/{morning,intraday,evening}.py,
pipeline/daily/{apex,risk_technical}.py, tools/{reasoning_tools,weight_engine,
prediction_db,risk_flags_db,portfolio_risk,apex_dual_run}.py,
services/{context,account_service}.py, scenarios/prepare.py,
web/chat/{orchestration,tools}.py and web/pages/4_🤖_Chat.py.

═══════════════════════════════════════════════════════════════════════════
EXECUTION MAP — one entry point per row
═══════════════════════════════════════════════════════════════════════════

1. Morning batch (pipeline/batch/morning.py::run_batch_morning)
   news → research → fundamentals → event detection → APEX (_run_daily_apex,
   the SAME shared writer path audited in Phase 1) → valuation (no LLM) →
   risk/technical (writes risk_flags, NOT predictions — see #9) → universe
   screen (no LLM) → scoring snapshot → price backfill.
   Writer: prediction_db.insert_prediction via forecast_writer.write_shared_forecast
   only (apex.py imports write_shared_forecast, never insert_prediction directly).

2. Intraday batch (pipeline/batch/intraday.py::run_batch_intraday)
   Calls ensure_morning_ran_today() first (self-healing dependency — Intraday
   assumes Morning's same-day data exists). Track A: portfolio tickers with a
   severity-3+ event → 5d APEX (shared writer, same path as #1). Track B:
   trending/screener-promoted tickers → news+research+fundamentals+5d APEX
   (also shared writer) — these are NEVER portfolio positions, so nothing
   private (holdings) is involved in Track B ticker selection itself, only
   which tickers get analyzed.

3. Evening batch (pipeline/batch/evening.py::run_batch_evening)
   Calls ensure_morning_ran_today() first too. NO new predictions written.
   Reads/evaluates only: evaluate_matured_predictions, recompute_rolling_metrics
   (both scoped via LEGACY_REVIEW_SCOPES per Phase 1 audit), calibration
   outcome-fill, portfolio price snapshot, screener outcome scoring.

4. root_agent (agent.py) — ADK CLI ONLY (`adk run portfolio_agent` / `adk web`).
   SequentialAgent: preparation_phase (ticker+query extractors, parallel) →
   research_phase (6 specialists incl. `risk_agent`, parallel) → synthesis_agent
   → clearance_agent. synthesis_agent's output_key "recommendation" is NEVER
   persisted anywhere — grep confirms zero writers of `state["recommendation"]`
   into any DB table. clearance_agent's before_agent_callback (see Phase 1/2
   clearance.py audit) short-circuits its own LLM unconditionally, so it never
   adds a model call for clearance itself. Each of the other 8 agents (2
   extractors + 6 specialists incl. `risk_agent`, whose tools include
   get_portfolio_holdings/get_portfolio_concentration/get_technical_indicators)
   is an ADK LlmAgent with tools, so a single "root_agent run" is NOT a fixed
   8 model calls — it's 8 independent ADK tool-calling loops, each potentially
   multiple calls; NOT counted here (would need per-specialist ADK-loop mocks,
   out of scope for this phase's fixed-input characterization). This whole
   pipeline is currently UNREACHABLE from the Streamlit app (see #6, #7) — it
   is exercised only via the ADK CLI/dev UI.

5. CLI / runner mode (main.py, pipeline/runner.py::run_analysis_with_failover)
   Generic per-agent-name failover runner used by risk_technical.py and (per
   main.py) the ad hoc `--agent` CLI flag. Chain = FAILOVER_CHAINS[AGENT_TASK_TYPE
   [agent_name]] (defaults to "fast"); tries chain[start_idx:] in order, one
   model call per attempt, stops at first success. `model_session.start_idx` is
   STICKY across a batch (ModelSession) — once model N works for one ticker,
   later tickers in the same run start at N, not 0. A single ticker's own worst
   case is len(chain) attempts; a whole batch's total attempts are NOT
   `n_tickers * len(chain)` because of this stickiness — characterized below.

6. Streamlit conversational chat (web/chat/orchestration.py::_run_chat_agent_thread,
   invoked from web/pages/4_🤖_Chat.py for non-prediction questions, routed by
   _classify_intent()). Does NOT invoke root_agent or any ADK Runner — it is a
   bare LiteLLM tool-calling loop against FAILOVER_CHAINS["flash"], tools from
   web/chat/tools.py::_CHAT_TOOLS. Up to 8 rounds (one acompletion call per
   round) against the CURRENT chain model before a transient failure advances
   to the next chain model; a non-transient failure aborts immediately.

7. Streamlit APEX chat (web/chat/orchestration.py::_run_apex_thread, same page,
   routed by _classify_intent() == "predict"). ALSO does not invoke root_agent.
   Iterates FAILOVER_CHAINS["reasoning"] once each (no per-model round budget,
   unlike #6): provider == "groq" → _apex_direct_run (ONE direct litellm.
   acompletion call, hand-built prompt embedding get_full_analysis_context()
   JSON, NO ADK, NO tool-calling); any other provider → _apex_adk_run (spins up
   its OWN throwaway ADK Runner + InMemorySessionService wrapping ONLY
   specialists/reasoning.py's reasoning_agent — a single-specialist ADK graph,
   not root_agent's 8-agent pipeline). On a transient failure, advances to the
   next chain entry; each entry gets exactly one attempt. The result is saved by
   the CALLING page (Chat.py) via forecast_writer.write_private_forecast —
   confirmed in Phase 1 audit: origin="chat.ticker_prediction", always SCOPE_PRIVATE.

═══════════════════════════════════════════════════════════════════════════
PREDICTION WRITERS — exhaustive (grep for insert_prediction across the repo)
═══════════════════════════════════════════════════════════════════════════
Exactly 2 call sites of prediction_db.insert_prediction exist anywhere in the
repo, and both are inside tools/forecast_writer.py:
  write_shared_forecast(context: MarketContext, provenance, **row)
    origin must be in SHARED_ORIGINS = {"pipeline.apex"} — apex.py is its only
    production caller (batch #1 and #2 above).
  write_private_forecast(owner, provenance, **row)
    always scope=SCOPE_PRIVATE regardless of origin; web/pages/4_🤖_Chat.py is
    its only production caller (chat ticker-prediction save, after #7 above).
No other module writes to `predictions` at all. root_agent's synthesis_agent
output is never persisted (see #4) — it cannot leak into shared or private
storage because it is never stored anywhere.

═══════════════════════════════════════════════════════════════════════════
HISTORY / PREDICTION READERS — scope-relevant ones
═══════════════════════════════════════════════════════════════════════════
  market_context.default_loaders()["history"]   → SHARED_SCOPES only (Phase 1)
  validation_engine.weekly_pattern_analysis      → SHARED_SCOPES only (Phase 1)
  validation_engine.{recompute_rolling_metrics, get_recent_evaluated_predictions,
    get_accuracy_heatmap_data, get_calibration_data, get_drift_data,
    get_volume_by_horizon, get_system_version_comparison}
                                                  → LEGACY_REVIEW_SCOPES (shared
    + legacy, never private) — the "restricted legacy path" for historical
    evaluation/dashboards (Phase 1).
  web/chat/tools.py::_chat_tool_get_predictions  → DISPLAY_SCOPES explicitly
    (own view: shared + legacy + private) — correct for a chat tool answering
    "what has APEX said about my ticker," which may legitimately surface the
    user's own private forecasts back to them.
  scenarios/prepare.py::prepare_inputs           → get_all_latest_predictions()
    with NO scopes arg → defaults to DISPLAY_SCOPES. Appropriate: this is a
    private, single-owner computation reading the owner's own full view, never
    exposed to shared generation or another user.
  Unscoped raw SQL against `predictions` (web/app.py, web/data/*.py,
    web/components/predictions_cards.py, web/pages/1_📊_Database.py,
    opportunity_engine.py, scoring_snapshot.py, watchlist_db.py, a few
    scripts/*.py): NOT scope-filtered. In THIS single-owner deployment these
    are equivalent to DISPLAY_SCOPES (the local owner viewing their own
    complete data) — not a boundary violation today, but every one of them
    would need explicit scoping before hosted multi-user mode, since none of
    them currently distinguish "my predictions" from "everyone's predictions."
    Flagged as a launch-blocking gap for multi-user mode specifically (see below).

═══════════════════════════════════════════════════════════════════════════
PRIVATE-DATA INPUTS — everything privacy.collect_private_terms() knows about
═══════════════════════════════════════════════════════════════════════════
  user_profile_db.get_user_profile(): display_name, employer
  holdings_db.list_accounts(include_archived=True): display_name, account_number
    (skipped when it equals the account's own broker name)
Additional PRIVATE surfaces that are NOT run through the privacy/scope
boundary at all, because they never touch the `predictions` table:
  risk_flags table (tools/risk_flags_db.py) — LLM-scored (specialists/risk.py
    via run_analysis_with_failover), portfolio-tied, single-owner-only, no
    scope/owner column at all. "Approach B" per its own module docstring:
    deliberately decoupled from predictions/weight-engine. Not a leak risk
    today (single owner) but has NO owner_scope column to enforce isolation
    under multi-user mode — flagged below.
  scenarios (scenarios/engine.py + prepare.py) — profile, holdings, restricted
    list, predictions all pulled into one ScenarioInputs; never written back
    to a shared table; purely a private, in-memory/DB-row computation per
    request. No leak path found.

═══════════════════════════════════════════════════════════════════════════
LAUNCH-BLOCKING GAPS (found this phase; none were fixed — read-only phase)
═══════════════════════════════════════════════════════════════════════════
G1. risk_flags has no owner_scope column — fine for single-owner, but under
    hosted multi-user mode every row would be visible to every user with no
    code change required to cause it (unlike `predictions`, which at least has
    scope/owner_scope to retrofit against).
G2. The raw unscoped `predictions` SQL sites listed above would all need an
    explicit scope filter before multi-user mode; today they silently rely on
    "there is only one owner."
G3. root_agent (agent.py) is dead code from the Streamlit app's perspective —
    reachable only via ADK CLI. If it is ever wired into the app later,
    synthesis_agent's output would need a writer decision (is it private? is
    it shared? it currently has none) before that happens — today it's simply
    never persisted, which is safe by omission but not by design.
G4. preferred_horizons (user_profile) has no live consumer anywhere in the
    codebase (weight_engine.filter_preferred_horizons is defined, tested, and
    called by nothing in production) — a user-facing profile setting that
    silently does nothing. Noted in the Phase 1 report as an open question,
    repeated here since it surfaced independently during this trace.
G5. Streamlit APEX chat's two runners (_apex_direct_run / _apex_adk_run) build
    the prediction JSON via two DIFFERENT mechanisms (hand-built prompt+regex
    JSON extraction vs. ADK tool-calling agent) for the same logical operation,
    selected purely by provider ("groq" vs. everything else). No test in the
    existing suite pins either path's output shape — characterized minimally
    below (chain iteration/fallback only, not the JSON contract itself, which
    would need real model calls or much larger prompt-shape fixtures).

EXISTING TEST STATUS: `pytest tests/ -q` → 228 passed, 0 failed, at the time
this phase's tests were added — no pre-existing failures to report separately.
"""
from __future__ import annotations

import asyncio

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.tools.prediction_db import _evaluation_date, get_scheduled_horizons, is_trading_day


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")


# ── Horizons / trading-day calendar (fixed dates — pure functions of `date`) ──

from datetime import date  # noqa: E402


def test_scheduled_horizons_fixed_dates():
    assert get_scheduled_horizons(date(2026, 9, 21)) == [5, 21]     # Monday
    assert get_scheduled_horizons(date(2026, 9, 22)) == [5]         # Tuesday, not month-start
    assert get_scheduled_horizons(date(2026, 10, 1)) == [5, 63]     # Thursday, 1st trading day of Oct
    assert get_scheduled_horizons(date(2026, 9, 26)) == []          # Saturday


def test_is_trading_day_fixed_dates_including_observed_holiday():
    assert is_trading_day(date(2026, 9, 21)) is True                # Monday
    assert is_trading_day(date(2026, 9, 26)) is False                # Saturday
    assert is_trading_day(date(2026, 7, 4)) is False                 # July 4th (Saturday in 2026)
    assert is_trading_day(date(2026, 7, 3)) is False                 # observed: Fri before July 4
    assert is_trading_day(date(2026, 11, 26)) is False               # Thanksgiving
    assert is_trading_day(date(2026, 12, 25)) is False               # Christmas


# ── Evaluation dates (fixed start + horizon → fixed business-day offset) ─────

def test_evaluation_date_fixed_horizons_from_a_fixed_start():
    start = date(2026, 9, 21)   # Monday
    assert _evaluation_date(start, 5) == "2026-09-28"
    assert _evaluation_date(start, 21) == "2026-10-20"
    assert _evaluation_date(start, 63) == "2026-12-17"


# ── Chat intent routing (pure function, fixed inputs) ─────────────────────────

def test_classify_intent_fixed_inputs():
    from web.chat.orchestration import _classify_intent

    assert _classify_intent("Analyze AAPL", ["AAPL"]) == "predict"
    assert _classify_intent("Should I buy MSFT?", ["MSFT"]) == "predict"
    assert _classify_intent("AAPL", ["AAPL"]) == "predict"                       # <=3 words, has ticker
    assert _classify_intent("what are analyst predictions on AAPL saying", ["AAPL"]) == "chat"  # "predictions" != "predict"
    assert _classify_intent("what looks undervalued right now", []) == "chat"     # no ticker at all
    assert _classify_intent("how correlated is my portfolio to AAPL", ["AAPL"]) == "chat"


# ── apex_dual_run.run_apex: model-attempt counting, including fallback ───────

def _fake_resp(text: str):
    class _Msg:
        content = text
    class _Choice:
        message = _Msg()
    class _Resp:
        choices = [_Choice()]
    return _Resp()


_VALID_JSON = '{"recommendation": "BUY", "horizons": [{"horizon_days": 5}]}'


def test_run_apex_primary_success_is_exactly_one_model_call(monkeypatch):
    from portfolio_agent.tools import apex_dual_run as adr

    calls = []

    async def _fake_acompletion(model, **kw):
        calls.append(model)
        return _fake_resp(_VALID_JSON)

    monkeypatch.setattr(adr, "random", type("R", (), {"shuffle": staticmethod(lambda lst: None)}))
    monkeypatch.setattr(adr.litellm, "acompletion", _fake_acompletion)

    result = asyncio.run(adr.run_apex("AAPL", "prompt"))
    assert result is not None and result.used_fallback is False
    assert len(calls) == 1                                    # exactly one model attempted


def test_run_apex_primary_failure_falls_back_to_second_model_once(monkeypatch):
    from portfolio_agent.tools import apex_dual_run as adr

    calls = []

    async def _fake_acompletion(model, **kw):
        calls.append(model)
        if len(calls) == 1:
            raise RuntimeError("429 rate limit")              # matches _is_failover_error
        return _fake_resp(_VALID_JSON)

    monkeypatch.setattr(adr, "random", type("R", (), {"shuffle": staticmethod(lambda lst: None)}))
    monkeypatch.setattr(adr.litellm, "acompletion", _fake_acompletion)

    result = asyncio.run(adr.run_apex("AAPL", "prompt"))
    assert result is not None and result.used_fallback is True
    assert len(calls) == 2                                    # primary + exactly one fallback attempt


def test_run_apex_both_models_failing_returns_none_after_exactly_two_calls(monkeypatch):
    from portfolio_agent.tools import apex_dual_run as adr

    calls = []

    async def _fake_acompletion(model, **kw):
        calls.append(model)
        raise RuntimeError("429 rate limit")

    monkeypatch.setattr(adr, "random", type("R", (), {"shuffle": staticmethod(lambda lst: None)}))
    monkeypatch.setattr(adr.litellm, "acompletion", _fake_acompletion)

    result = asyncio.run(adr.run_apex("AAPL", "prompt"))
    assert result is None
    assert len(calls) == 2                                    # never more than one retry — no placeholder written


# ── web/chat orchestration: chain fallback, one attempt per chain entry ──────

def test_run_apex_thread_tries_each_chain_model_once_until_success(monkeypatch):
    from web.chat import orchestration as orch
    import portfolio_agent._models as models_mod

    attempts = []

    async def _fake_direct(ticker, query, model_id, label, provider, chain_pos, q):
        attempts.append((provider, label))
        raise RuntimeError("529 overloaded")                  # transient -> advances to next chain entry

    async def _fake_adk(ticker, query, model_id, label, provider, chain_pos, q):
        attempts.append((provider, label))
        q.put(("done", "ok"))
        return True

    fake_chain = [("m1", "groq", "L1"), ("m2", "anthropic", "L2"), ("m3", "google", "L3")]
    monkeypatch.setattr(models_mod, "FAILOVER_CHAINS", {"reasoning": fake_chain})
    monkeypatch.setattr(orch, "_apex_direct_run", _fake_direct)
    monkeypatch.setattr(orch, "_apex_adk_run", _fake_adk)

    from queue import Queue
    q = Queue()
    orch._run_apex_thread("AAPL", "full analysis", q)

    assert attempts == [("groq", "L1"), ("anthropic", "L2")]   # stopped at first success, groq used direct path


def test_run_apex_thread_all_chain_models_failing_emits_error_once(monkeypatch):
    from web.chat import orchestration as orch
    import portfolio_agent._models as models_mod

    attempts = []

    async def _fake_direct(*a, **kw):
        attempts.append("direct")
        raise RuntimeError("429 rate limit")

    fake_chain = [("m1", "groq", "L1"), ("m2", "groq", "L2")]
    monkeypatch.setattr(models_mod, "FAILOVER_CHAINS", {"reasoning": fake_chain})
    monkeypatch.setattr(orch, "_apex_direct_run", _fake_direct)

    from queue import Queue
    q = Queue()
    orch._run_apex_thread("AAPL", "full analysis", q)

    assert len(attempts) == 2                                  # exactly one attempt per chain entry, no repeats
    events = []
    while not q.empty():
        events.append(q.get())
    assert any(kind == "error" for kind, _ in events)


# ── web/chat conversational agent: per-round call counting + round cap ───────

def test_chat_agent_thread_counts_one_model_call_per_round_until_final_text(monkeypatch):
    from web.chat import orchestration as orch
    import portfolio_agent._models as models_mod

    monkeypatch.setattr(models_mod, "FAILOVER_CHAINS", {"flash": [("m1", "google", "Flash")]})
    monkeypatch.setattr(orch, "_execute_chat_tool", lambda name, args: '{"ok": true}')

    class _ToolCall:
        def __init__(self, cid, name, args):
            self.id = cid
            self.function = type("F", (), {"name": name, "arguments": args})

    call_log = []

    async def _fake_acompletion(model, messages, **kw):
        call_log.append(model)
        if len(call_log) == 1:
            msg = type("M", (), {
                "tool_calls": [_ToolCall("tc1", "get_portfolio_summary", "{}")],
                "content": "",
            })
        else:
            msg = type("M", (), {"tool_calls": None, "content": "Here is your answer."})
        return type("R", (), {"choices": [type("C", (), {"message": msg})]})

    import litellm
    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)

    from queue import Queue
    q = Queue()
    orch._run_chat_agent_thread(["AAPL"], "what's my portfolio", [], q)

    assert len(call_log) == 2                                  # 1 tool round + 1 final-text round
    events = []
    while not q.empty():
        events.append(q.get())
    assert ("text", "Here is your answer.") in events
    assert ("done", "chat") in events


def test_chat_agent_thread_stops_at_eight_rounds_if_never_producing_final_text(monkeypatch):
    from web.chat import orchestration as orch
    import portfolio_agent._models as models_mod

    monkeypatch.setattr(models_mod, "FAILOVER_CHAINS", {"flash": [("m1", "google", "Flash")]})
    monkeypatch.setattr(orch, "_execute_chat_tool", lambda name, args: '{"ok": true}')

    class _ToolCall:
        def __init__(self, cid, name, args):
            self.id = cid
            self.function = type("F", (), {"name": name, "arguments": args})

    call_log = []

    async def _fake_acompletion(model, messages, **kw):
        call_log.append(model)
        # Always returns a tool call -- the agent never produces final text.
        msg = type("M", (), {"tool_calls": [_ToolCall("tc", "get_portfolio_summary", "{}")], "content": ""})
        return type("R", (), {"choices": [type("C", (), {"message": msg})]})

    import litellm
    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)

    from queue import Queue
    q = Queue()
    orch._run_chat_agent_thread(["AAPL"], "what's my portfolio", [], q)

    assert len(call_log) == 8                                  # hard round cap, characterized as-is
    events = []
    while not q.empty():
        events.append(q.get())
    assert any(kind == "text" and "did not produce a final answer" in payload for kind, payload in events)


# ── risk_flags persistence: upsert is keyed (ticker, as_of_date, source) ──────

def test_risk_flag_upsert_is_idempotent_and_updates_in_place():
    from portfolio_agent.tools.risk_flags_db import get_flags_for_ticker, needs_refresh, upsert_risk_flag

    assert needs_refresh("AAPL", "2026-09-21") is True
    upsert_risk_flag(ticker="aapl", as_of_date="2026-09-21", source="risk", flag=True,
                     score=8.0, summary="concentrated", raw_json="{}", model_used="claude")
    assert needs_refresh("AAPL", "2026-09-21") is True         # only 1 of 2 sources present

    upsert_risk_flag(ticker="AAPL", as_of_date="2026-09-21", source="technical", flag=False,
                     score=6.0, summary="neutral", raw_json="{}", model_used="claude")
    assert needs_refresh("AAPL", "2026-09-21") is False        # both sources present now

    # Re-upsert 'risk' with different values -- same (ticker, date, source) key updates in place.
    upsert_risk_flag(ticker="AAPL", as_of_date="2026-09-21", source="risk", flag=False,
                     score=3.0, summary="resolved", raw_json="{}", model_used="gpt-4o")
    flags = get_flags_for_ticker("AAPL", "2026-09-21")
    assert set(flags) == {"risk", "technical"}                 # still exactly one row per source
    assert flags["risk"]["score"] == 3.0 and flags["risk"]["flag"] == 0
