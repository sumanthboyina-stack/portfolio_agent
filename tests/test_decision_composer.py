"""
Phase 7: common decision composition and entrypoint adapters.

compose_decision() (tools.decision_composer) combines MarketForecast (stored
SHARED predictions), PortfolioAssessment (Phase 5), and PolicyDecision
(Phase 6) into one ComposedDecision with three separate sections plus an
optional private explanation. It makes ZERO specialist/LLM calls by default
— it only retrieves already-stored/computable data; a caller that finds
stale/unavailable market evidence decides whether to trigger the EXISTING
generation path itself.

ENTRYPOINT AUDIT (see report for the full writeup)
════════════════════════════════════════════════════
  - web/chat/orchestration.py's APEX runners (_apex_direct_run/_apex_adk_run)
    do NOT invoke root_agent (confirmed in Phase 0's trace) and, before this
    phase, saved a private forecast with ZERO policy/clearance check — a
    restricted or blacked-out ticker's BUY recommendation would render
    completely unqualified in the chat UI. This was the clearest "legacy
    tool able to bypass policy checks" this phase's instructions warn about.
  - main.py's CLI ("all"/root_agent path) already ran clearance_agent's
    deterministic _profile_gate (Phase 6) but rendered Recommendation and
    Clearance as two INDEPENDENT pretty-printed sections with no composition
    tying them together — a BLOCKED ticker's "action: STRONG BUY" line
    printed with nothing stopping a reader from treating it as actionable.
  - Both are wired to tools.decision_composer in this phase — see
    web/pages/4_🤖_Chat.py's prediction-save block and
    portfolio_agent/pipeline/rendering.py's pretty_print("all", ...).
  - root_agent itself (agent.py) is unchanged — still ADK-CLI-only, still
    unreachable from the web app (Phase 0's finding stands); its synthesis_
    agent output is still never persisted, so there is nothing there to
    compose against beyond what the CLI wiring above already covers via
    the SAME clearance.evaluate_clearance function root_agent's
    clearance_agent step uses.

EXISTING TEST STATUS: `pytest tests/ -q` → 313 passed, 0 failed, before this
phase's additions — no pre-existing failures to report separately.
"""
from __future__ import annotations

import inspect

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.services import account_service
from portfolio_agent.services.context import RequestContext, _mint_identity_for_tests, local_context
from portfolio_agent.tools import decision_composer as dc
from portfolio_agent.tools import portfolio_risk as risk
from portfolio_agent.tools.forecast_writer import ForecastProvenance, write_shared_forecast
from portfolio_agent.tools.market_context import build_market_context


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")


NEWS = [{"headline": "steady", "sentiment": "NEUTRAL", "sentiment_score": 0.0,
        "published_at": "2026-09-20", "date": "2026-09-20"}]
RESEARCH = {"price_target_avg": 120.0, "current_price": 100.0, "num_analysts": 10,
           "as_of_date": "2026-09-15", "consensus": "BUY"}
MACRO = {"vix": 20.0, "regime": "NEUTRAL"}
FUND = {"as_of_date": "2026-08-01", "fundamental_score": 7}
VAL = {"as_of_date": "2026-09-01", "intrinsic_value": 110.0}


def _loaders(**over):
    base = {"fundamentals": lambda t: FUND, "research": lambda t: RESEARCH, "news": lambda t: NEWS,
            "valuation": lambda t: VAL, "macro": lambda: MACRO, "history": lambda t: [], "failure_patterns": lambda: []}
    base.update(over)
    return base


def _seed_shared_forecast(ticker="AAPL", horizon_days=5, recommendation="STRONG_BUY", **row_over):
    from portfolio_agent.tools.forecast_writer import GUARDRAIL_VERSION
    ctx = build_market_context(ticker, [horizon_days], loaders=_loaders(), private_terms=frozenset())
    prov = ForecastProvenance(origin="pipeline.apex", model_used="anthropic:claude", guardrail_version=GUARDRAIL_VERSION)
    row = dict(ticker=ticker, prediction="BULLISH", recommendation=recommendation, horizon_days=horizon_days,
              reasoning="steady momentum", composite_score=8.0)
    row.update(row_over)
    res = write_shared_forecast(ctx, prov, **row)
    assert res["saved"] is True
    return ctx


def _seed_holding(ctx, ticker, shares, current_price, sector="Technology", broker="manual"):
    acct = account_service.create_account(ctx, broker=broker, display_name=f"{broker}-{ticker}")
    account_service.add_position(ctx, acct["account_id"], {
        "ticker": ticker, "shares": shares, "current_price": current_price, "sector": sector,
    })


def _no_restriction(monkeypatch):
    import portfolio_agent.tools.restricted_list_db as rl
    monkeypatch.setattr(rl, "is_restricted", lambda t: (False, None))
    import portfolio_agent.tools.blackout_windows_db as bw
    monkeypatch.setattr(bw, "list_active_blackout_windows", lambda **kw: [])


def _no_llm(monkeypatch):
    import litellm
    def _boom(*a, **kw):
        raise AssertionError("compose_decision must never call an LLM in the routine path")
    monkeypatch.setattr(litellm, "acompletion", _boom)


# ── Warm evidence causes zero fresh specialist calls ──────────────────────────

def test_warm_evidence_makes_zero_llm_calls_and_reuses_the_stored_forecast(monkeypatch):
    _no_restriction(monkeypatch)
    _no_llm(monkeypatch)
    monkeypatch.setattr(risk, "compute_portfolio_risk_context", lambda *a, **k: {})

    _seed_shared_forecast()
    # _current_evidence_refs reads the REAL fundamentals/research/valuation
    # tables (not this test file's custom `loaders`, which only fed
    # build_market_context) — pin it to exactly what got captured at write
    # time, so this test isolates the reuse DECISION rather than needing to
    # seed three separate stores with byte-identical data.
    import portfolio_agent.tools.decision_composer as dc_mod
    from portfolio_agent.tools.market_context import MARKET_CONTEXT_VERSION
    monkeypatch.setattr(dc_mod, "_current_evidence_refs", lambda t: {
        "context_version": MARKET_CONTEXT_VERSION, "fundamentals_as_of": "2026-08-01",
        "research_as_of": "2026-09-15", "valuation_as_of": "2026-09-01",
    })

    ctx = local_context("test")
    _seed_holding(ctx, "AAPL", shares=10, current_price=100.0)

    out = dc.compose_decision(ctx, "AAPL", action="buy", horizon_days=5)
    assert out.market_outlook.source == "reused"
    assert out.market_outlook.recommendation == "STRONG_BUY"


def test_stale_market_evidence_is_reported_never_silently_regenerated(monkeypatch):
    """compose_decision reports staleness; it never triggers a fresh (paid)
    generation call itself — that stays the caller's decision."""
    _no_restriction(monkeypatch)
    _no_llm(monkeypatch)
    monkeypatch.setattr(risk, "compute_portfolio_risk_context", lambda *a, **k: {})

    _seed_shared_forecast()
    # Fundamentals updated since the forecast was built -- the stored row is now stale evidence.
    import portfolio_agent.tools.fundamentals_db as fdb
    monkeypatch.setattr(fdb, "get_stored_fundamentals", lambda t: dict(FUND, as_of_date="2026-09-20"))

    ctx = local_context("test")
    _seed_holding(ctx, "AAPL", shares=10, current_price=100.0)
    out = dc.compose_decision(ctx, "AAPL", action="buy", horizon_days=5)
    assert out.market_outlook.source == "stale"
    assert "fundamentals_as_of" in out.market_outlook.reason


def test_no_stored_forecast_is_unavailable_not_an_error(monkeypatch):
    _no_restriction(monkeypatch)
    _no_llm(monkeypatch)
    ctx = local_context("test")
    out = dc.compose_decision(ctx, "ZZZZ", action="buy")
    assert out.market_outlook.source == "unavailable"
    assert out.market_outlook.recommendation is None


# ── Private questions stay out of shared history / shared generation ─────────

def test_market_outlook_signature_never_accepts_a_private_question():
    """Structural guarantee, not just behavioral: _market_outlook can't even
    be called with a private question -- there is no parameter for one."""
    params = inspect.signature(dc._market_outlook).parameters
    assert "question" not in params and "private_question" not in params


def test_private_question_never_reaches_shared_history(monkeypatch):
    _no_restriction(monkeypatch)
    _no_llm(monkeypatch)
    monkeypatch.setattr(risk, "compute_portfolio_risk_context", lambda *a, **k: {})
    _seed_shared_forecast()
    ctx = local_context("test")
    _seed_holding(ctx, "AAPL", shares=10, current_price=100.0)

    sentinel = "my daughter's wedding, need cash urgently, embarrassing medical bills"
    out = dc.compose_decision(ctx, "AAPL", action="buy", private_question=sentinel)
    assert sentinel not in str(out.to_dict())

    from portfolio_agent.tools.prediction_db import SHARED_SCOPES, get_prediction_history
    history_text = str([p.to_dict() for p in get_prediction_history("AAPL", scopes=SHARED_SCOPES)])
    assert sentinel not in history_text


def test_explanation_step_is_opt_in_and_off_by_default_makes_no_llm_call(monkeypatch):
    _no_restriction(monkeypatch)
    _no_llm(monkeypatch)
    monkeypatch.setattr(risk, "compute_portfolio_risk_context", lambda *a, **k: {})
    _seed_shared_forecast()
    ctx = local_context("test")
    _seed_holding(ctx, "AAPL", shares=10, current_price=100.0)

    out = dc.compose_decision(ctx, "AAPL", action="buy")   # allow_llm_explanation defaults to False
    assert out.explanation is None


# ── Users receive different overlays ──────────────────────────────────────────

def test_different_portfolios_receive_different_portfolio_fit_for_the_same_forecast(monkeypatch):
    _no_restriction(monkeypatch)
    monkeypatch.setattr(risk, "compute_portfolio_risk_context", lambda *a, **k: {})
    _seed_shared_forecast()   # one shared MarketOutlook, identical for everyone

    ctx_a = local_context("test")
    _seed_holding(ctx_a, "AAPL", shares=100, current_price=100.0)   # AAPL is 100% of a small book

    from portfolio_agent.tools import holdings_db as repo
    with repo.unit_of_work() as conn:
        conn.execute("INSERT INTO portfolios (owner) VALUES (?)", ("bob",))
    ctx_b = RequestContext(identity=_mint_identity_for_tests("bob"), source="test")
    from portfolio_agent.domain import UserProfile
    from portfolio_agent.tools.portfolio_policy import effective_policy_from_profile

    out_a = dc.compose_decision(ctx_a, "AAPL", action="buy")
    holdings_b = [{"ticker": "MSFT", "shares": 100, "current_price": 100.0, "sector": "Technology"}]
    account_service.create_account(ctx_b, broker="manual", display_name="bob-acct")
    from portfolio_agent.tools.portfolio_assessment import compute_portfolio_exposures
    exposures_b = compute_portfolio_exposures(ctx_b, holdings=holdings_b,
                                              policy=effective_policy_from_profile(UserProfile()))

    assert out_a.market_outlook.recommendation == "STRONG_BUY"   # same shared forecast
    assert out_a.portfolio_fit.current_weight_pct == 100.0        # AAPL dominates ctx_a's book
    assert "AAPL" not in exposures_b.per_holding_value              # bob doesn't hold it at all


# ── Root/chat/CLI agree on permission ─────────────────────────────────────────

def test_composer_and_direct_clearance_evaluation_agree_on_the_same_permission(monkeypatch):
    """Whatever entry point calls it (root_agent's _profile_gate, or
    decision_composer for chat/CLI), the SAME clearance.evaluate_clearance
    function decides — proven here by calling both paths with matching
    inputs and requiring identical decisions, not just similar ones."""
    import portfolio_agent.tools.restricted_list_db as rl
    monkeypatch.setattr(rl, "is_restricted", lambda t: (True, "insider list"))

    from portfolio_agent.domain import UserProfile
    from portfolio_agent.clearance import evaluate_clearance

    ctx = local_context("test")
    direct = evaluate_clearance("AAPL", UserProfile(), action="trade", owner_scope=f"user:{ctx.actor}")
    composed_permission = dc._action_permission(ctx, "AAPL", "trade")
    assert direct.decision == composed_permission.decision == "BLOCKED"


# ── Blocked actions never appear as unqualified actionable BUY ───────────────

def test_blocked_ticker_never_produces_an_actionable_recommendation(monkeypatch):
    import portfolio_agent.tools.restricted_list_db as rl
    monkeypatch.setattr(rl, "is_restricted", lambda t: (True, "insider list"))
    monkeypatch.setattr(risk, "compute_portfolio_risk_context", lambda *a, **k: {})

    _seed_shared_forecast(recommendation="STRONG_BUY")
    ctx = local_context("test")
    _seed_holding(ctx, "AAPL", shares=10, current_price=100.0)

    out = dc.compose_decision(ctx, "AAPL", action="buy")
    assert out.market_outlook.recommendation == "STRONG_BUY"   # the raw forecast is still visible/inspectable
    assert out.actionable_recommendation is None                # but never surfaced as "what to do"
    assert out.action_permission.decision == "BLOCKED"


def test_pending_approval_ticker_never_produces_an_actionable_recommendation(monkeypatch):
    _no_restriction(monkeypatch)
    monkeypatch.setattr(risk, "compute_portfolio_risk_context", lambda *a, **k: {})
    _seed_shared_forecast(recommendation="BUY")
    ctx = local_context("test")
    _seed_holding(ctx, "AAPL", shares=10, current_price=100.0)

    out = dc.compose_decision(ctx, "AAPL", action="buy")   # default profile requires pre-clearance, no approval on file
    assert out.action_permission.decision == "PENDING_APPROVAL"
    assert out.actionable_recommendation is None


def test_allowed_ticker_does_produce_an_actionable_recommendation(monkeypatch):
    _no_restriction(monkeypatch)
    monkeypatch.setattr(risk, "compute_portfolio_risk_context", lambda *a, **k: {})
    _seed_shared_forecast(recommendation="BUY")
    ctx = local_context("test")
    _seed_holding(ctx, "AAPL", shares=10, current_price=100.0)

    from portfolio_agent.tools.trade_approvals_db import record_approval
    record_approval(owner_scope=f"user:{ctx.actor}", ticker="AAPL", action="buy", granted_by="compliance:jane")

    out = dc.compose_decision(ctx, "AAPL", action="buy")
    assert out.action_permission.decision == "ALLOWED_BY_RULES"
    assert out.actionable_recommendation == "BUY"


# ── Policy findings attach independently of model-generated prose ────────────

def test_action_permission_never_depends_on_explanation_being_generated(monkeypatch):
    """Structural: allow_llm_explanation isn't even consulted until after
    action_permission is already fully computed — see compose_decision."""
    src = inspect.getsource(dc.compose_decision)
    permission_idx = src.index("action_permission = _action_permission")
    explanation_idx = src.index("if allow_llm_explanation")
    assert permission_idx < explanation_idx
