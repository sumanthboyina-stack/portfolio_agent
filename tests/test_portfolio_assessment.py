"""
Phase 5: private numerical risk and policy assessment.

PortfolioAssessment (tools.portfolio_assessment) computes, from the actor's
OWN authorized holdings + cash + one effective policy (tools.portfolio_policy,
sourced from their profile's numeric limits only): concentration/issuer/cash-
reserve findings for one ticker, plus best-effort correlation/beta. Concentration
never depends on price-history success (a real bug fixed this phase — see
test_missing_return_history_does_not_suppress_concentration_checks). No LLM,
no invented risk-tolerance score — see fit_score's own docstring in
portfolio_risk.py for why that pre-existing metric is not a replacement for
the old LLM risk_score, and this module adds no scoring of its own kind.
"""
from __future__ import annotations

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.services import account_service
from portfolio_agent.services.context import RequestContext, _mint_identity_for_tests, local_context
from portfolio_agent.tools import portfolio_assessment as pa
from portfolio_agent.tools import portfolio_risk as risk
from portfolio_agent.tools import holdings_db as repo
from portfolio_agent.tools import user_profile_db


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")


def _seed_holding(ctx, ticker, shares, current_price, sector="Technology", broker="manual"):
    acct = account_service.create_account(ctx, broker=broker, display_name=f"{broker}-{ticker}")
    account_service.add_position(ctx, acct["account_id"], {
        "ticker": ticker, "shares": shares, "current_price": current_price, "sector": sector,
    })


def _seed_second_owner(owner: str) -> None:
    """No production path creates a second owner (single-user deployment) —
    direct insert, mirroring the Phase 2 test pattern, purely to exercise
    cross-portfolio isolation."""
    with repo.unit_of_work() as conn:
        conn.execute("INSERT INTO portfolios (owner) VALUES (?)", (owner,))


# ── Authorization ──────────────────────────────────────────────────────────

def test_unauthorized_actor_is_refused_before_any_holdings_read():
    ctx = RequestContext(identity=_mint_identity_for_tests("stranger"), source="test")
    with pytest.raises(account_service.NotAuthorized):
        pa.build_portfolio_assessment(ctx, "AAPL")


# ── Empty / single-position portfolios ────────────────────────────────────

def test_empty_portfolio_is_insufficient_data_with_no_holdings_reason_code():
    ctx = local_context("test")
    out = pa.build_portfolio_assessment(ctx, "AAPL")
    assert out.exposures["current_weight_pct"] == pa.UNKNOWN
    assert pa.RC_NO_HOLDINGS in out.evidence_refs["reason_codes"]
    assert out.sizing["status"] == pa.INSUFFICIENT_DATA


def test_single_position_portfolio_gets_an_explicit_reason_code(monkeypatch):
    ctx = local_context("test")
    _seed_holding(ctx, "AAPL", shares=10, current_price=100.0)
    monkeypatch.setattr(risk, "compute_portfolio_risk_context", lambda *a, **k: {})

    out = pa.build_portfolio_assessment(ctx, "AAPL")
    assert pa.RC_SINGLE_POSITION in out.evidence_refs["reason_codes"]
    # Concentration is still computable for a single-position book (100% of value).
    assert out.exposures["current_weight_pct"] == 100.0


# ── The core fix: missing return history must not suppress concentration ──

def test_missing_return_history_does_not_suppress_concentration_checks(monkeypatch):
    ctx = local_context("test")
    _seed_holding(ctx, "AAPL", shares=10, current_price=100.0, sector="Technology")
    _seed_holding(ctx, "MSFT", shares=5, current_price=200.0, sector="Technology")
    # Simulate total yfinance failure -- portfolio_risk's price-history-gated function returns {}.
    monkeypatch.setattr(risk, "compute_portfolio_risk_context", lambda *a, **k: {})

    out = pa.build_portfolio_assessment(ctx, "AAPL")
    # Sector concentration comes from STATIC holdings data (shares x stored
    # current_price) -- unaffected by the price-history failure.
    assert out.exposures["sector_concentration_pct"] == 100.0    # both holdings are Technology
    assert out.exposures["current_weight_pct"] == pytest.approx(1000 / 2000 * 100, abs=0.01)
    # Correlation/beta genuinely need price history and correctly stay UNKNOWN.
    assert out.exposures["correlation_with_portfolio"] == pa.UNKNOWN
    assert out.exposures["beta_vs_spy"] == pa.UNKNOWN
    # The sector limit (enforceable, computable) is evaluated; correlation isn't a limit at all.
    sector_limit = next(c for c in out.limits if c.name == "sector_concentration")
    assert sector_limit.breached is True and sector_limit.severity == pa.SEVERITY_ENFORCEABLE


# ── Cash inclusion and the reserve-cash advisory limit ────────────────────

def test_cash_is_included_in_total_value_and_denominator():
    ctx = local_context("test")
    acct = account_service.create_account(ctx, broker="manual", display_name="cash-acct")
    account_service.add_position(ctx, acct["account_id"], {"ticker": "AAPL", "shares": 10, "current_price": 100.0})
    account_service.record_account_cash(ctx, acct["account_id"], 1000.0, as_of_date="2026-09-21")

    exposures = pa.compute_portfolio_exposures(ctx)
    assert exposures.cash_total == 1000.0
    assert exposures.total_value == 2000.0   # $1000 AAPL + $1000 cash
    assert exposures.per_holding_weight_pct["AAPL"] == 50.0


def test_cash_reserve_limit_is_advisory_not_enforceable():
    ctx = local_context("test")
    _seed_holding(ctx, "AAPL", shares=10, current_price=100.0)
    acct_id = repo.list_accounts()[0]["account_id"]
    account_service.record_account_cash(ctx, acct_id, 100.0, as_of_date="2026-09-21")

    out = pa.build_portfolio_assessment(ctx, "MSFT")   # not held -- candidate sizing vs cash
    cash_limit = next(c for c in out.limits if c.name == "cash_reserve_for_candidate")
    assert cash_limit.severity == pa.SEVERITY_ADVISORY


# ── Unsupported currency ───────────────────────────────────────────────────

def test_unsupported_currency_marks_findings_unknown_with_reason_code(monkeypatch):
    ctx = local_context("test")
    _seed_holding(ctx, "AAPL", shares=10, current_price=100.0)
    monkeypatch.setattr(pa, "resolve_portfolio",
                        lambda c, **k: {**repo.get_portfolio_by_owner(c.actor), "base_currency": "EUR"})

    out = pa.build_portfolio_assessment(ctx, "AAPL")
    assert out.exposures["current_weight_pct"] == pa.UNKNOWN
    assert pa.RC_UNSUPPORTED_CURRENCY in out.evidence_refs["reason_codes"]


# ── Missing data is never "safe" ────────────────────────────────────────────

def test_missing_data_never_produces_a_permissive_sizing_default(monkeypatch):
    ctx = local_context("test")
    monkeypatch.setattr(pa, "resolve_portfolio",
                        lambda c, **k: {**repo.get_portfolio_by_owner(c.actor), "base_currency": "EUR"})
    _seed_holding(ctx, "AAPL", shares=10, current_price=100.0)

    out = pa.build_portfolio_assessment(ctx, "AAPL")
    assert out.sizing["status"] == pa.INSUFFICIENT_DATA
    assert out.sizing["recommended_position_pct"] is None   # never a fallback number


# ── Identical forecasts, different portfolios -> different findings ────────

def test_identical_ticker_produces_different_findings_for_different_portfolios(monkeypatch):
    monkeypatch.setattr(risk, "compute_portfolio_risk_context", lambda *a, **k: {})
    ctx_a = local_context("test")
    _seed_holding(ctx_a, "NVDA", shares=1, current_price=100.0, sector="Technology")
    _seed_holding(ctx_a, "JNJ", shares=9, current_price=100.0, sector="Healthcare")

    _seed_second_owner("bob")
    ctx_b = RequestContext(identity=_mint_identity_for_tests("bob"), source="test")
    account_service.create_account(ctx_b, broker="manual", display_name="bob-acct")
    holdings_b = [{"ticker": "AAPL", "shares": 10, "current_price": 100.0, "sector": "Technology"}]

    # bob has no real profile row -- user_profile is a genuine single-row table
    # by design (Phase 2), so a synthetic identity for this cross-portfolio
    # test supplies its own policy rather than sharing LOCAL_OWNER's.
    from portfolio_agent.domain import UserProfile
    from portfolio_agent.tools.portfolio_policy import effective_policy_from_profile
    policy_b = effective_policy_from_profile(UserProfile())

    out_a = pa.build_portfolio_assessment(ctx_a, "NVDA")
    out_b = pa.build_portfolio_assessment(ctx_b, "NVDA", holdings=holdings_b, policy=policy_b)

    assert out_a.exposures["current_weight_pct"] != out_b.exposures["current_weight_pct"]
    assert out_a.exposures["sector_concentration_pct"] != out_b.exposures["sector_concentration_pct"]


# ── Holdings/policy changes invalidate only dependent results ─────────────

def test_holdings_change_invalidates_cached_exposures(monkeypatch):
    calls = []
    real_compute = pa.compute_portfolio_exposures

    def _counting_compute(ctx, **kw):
        calls.append(1)
        return real_compute(ctx, **kw)

    monkeypatch.setattr(pa, "compute_portfolio_exposures", _counting_compute)
    monkeypatch.setattr(risk, "compute_portfolio_risk_context", lambda *a, **k: {})

    ctx = local_context("test")
    _seed_holding(ctx, "AAPL", shares=10, current_price=100.0)
    cache = pa._get_default_cache()

    e1 = pa.get_portfolio_exposures(ctx, cache=cache)
    e2 = pa.get_portfolio_exposures(ctx, cache=cache)
    assert len(calls) == 1 and e1.version == e2.version   # unchanged holdings -> cached, not recomputed

    _seed_holding(ctx, "MSFT", shares=5, current_price=50.0)   # holdings_version changes
    e3 = pa.get_portfolio_exposures(ctx, cache=cache)
    assert len(calls) == 2 and e3.version != e1.version


def test_policy_limit_change_invalidates_cached_exposures(monkeypatch):
    monkeypatch.setattr(risk, "compute_portfolio_risk_context", lambda *a, **k: {})
    ctx = local_context("test")
    _seed_holding(ctx, "AAPL", shares=10, current_price=100.0)
    cache = pa._get_default_cache()

    e1 = pa.get_portfolio_exposures(ctx, cache=cache)
    user_profile_db.update_user_profile_for(ctx, max_sector_pct=40.0)
    e2 = pa.get_portfolio_exposures(ctx, cache=cache)
    assert e1.policy_fingerprint != e2.policy_fingerprint
    assert e1.version != e2.version


def test_name_and_notification_changes_do_not_recompute_risk(monkeypatch):
    calls = []
    real_compute = pa.compute_portfolio_exposures

    def _counting_compute(ctx, **kw):
        calls.append(1)
        return real_compute(ctx, **kw)

    monkeypatch.setattr(pa, "compute_portfolio_exposures", _counting_compute)
    monkeypatch.setattr(risk, "compute_portfolio_risk_context", lambda *a, **k: {})

    ctx = local_context("test")
    _seed_holding(ctx, "AAPL", shares=10, current_price=100.0)
    cache = pa._get_default_cache()

    e1 = pa.get_portfolio_exposures(ctx, cache=cache)
    user_profile_db.update_user_profile_for(ctx, display_name="New Name", employer="New Employer")
    e2 = pa.get_portfolio_exposures(ctx, cache=cache)

    assert len(calls) == 1   # NOT recomputed -- display_name/employer aren't in the policy fingerprint
    assert e1.policy_fingerprint == e2.policy_fingerprint
    assert e1.version == e2.version


def test_policy_fingerprint_depends_only_on_the_four_numeric_limits():
    from portfolio_agent.tools.portfolio_policy import effective_policy_from_profile
    from portfolio_agent.domain import UserProfile

    a = effective_policy_from_profile(UserProfile(display_name="Alice", employer="Acme"))
    b = effective_policy_from_profile(UserProfile(display_name="Bob", employer="Globex"))
    assert a.fingerprint == b.fingerprint   # only display_name/employer differ

    c = effective_policy_from_profile(UserProfile(display_name="Alice", employer="Acme", max_sector_pct=50.0))
    assert a.fingerprint != c.fingerprint   # a real limit differs


# ── Advisory vs enforceable + routine assessment makes zero LLM calls ──────

def test_limits_distinguish_advisory_from_enforceable(monkeypatch):
    monkeypatch.setattr(risk, "compute_portfolio_risk_context", lambda *a, **k: {})
    ctx = local_context("test")
    _seed_holding(ctx, "AAPL", shares=10, current_price=100.0)

    out = pa.build_portfolio_assessment(ctx, "AAPL")
    severities = {c.name: c.severity for c in out.limits}
    assert severities["position_concentration"] == pa.SEVERITY_ENFORCEABLE
    assert severities["sector_concentration"] == pa.SEVERITY_ENFORCEABLE
    assert severities["issuer_concentration"] == pa.SEVERITY_ENFORCEABLE
    assert severities["cash_reserve_for_candidate"] == pa.SEVERITY_ADVISORY


def test_routine_assessment_makes_zero_llm_calls(monkeypatch):
    def _no_llm(*a, **kw):
        raise AssertionError("a routine numerical assessment must never call an LLM")

    import litellm
    monkeypatch.setattr(litellm, "acompletion", _no_llm)
    monkeypatch.setattr(risk, "compute_portfolio_risk_context", lambda *a, **k: {})

    ctx = local_context("test")
    _seed_holding(ctx, "AAPL", shares=10, current_price=100.0)
    pa.build_portfolio_assessment(ctx, "AAPL")   # would raise via _no_llm if it ever touched litellm
