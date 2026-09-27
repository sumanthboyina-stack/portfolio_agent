"""
scenarios/prepare.py::prepare_inputs — repro test for the "Profile page uses
your row" review's finding #2: rebalance/scenario limits (Limits.from_profile)
always used LOCAL_OWNER's mandate limits, regardless of which owner's
scenario was actually being prepared. Fixed by using
get_user_profile_by_owner(owner) when an owner is known, falling back to
get_user_profile() only for the no-owner CLI case.
"""
from __future__ import annotations

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.services import account_service
from portfolio_agent.services.context import local_context
from portfolio_agent.tools.user_profile_db import update_user_profile, update_user_profile_for


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")


@pytest.fixture(autouse=True)
def _no_external_research(monkeypatch):
    """prepare_inputs pulls in predictions/opportunities/sectors — none of it
    is relevant here, so short-circuit to keep this test to the profile/limits
    behavior it's actually checking."""
    import portfolio_agent.tools.opportunity_engine as oe
    import portfolio_agent.tools.prediction_db as pdb
    import portfolio_agent.tools.universe_db as udb
    pdb.get_all_latest_predictions()   # real call first: creates the `predictions` table prepare_inputs queries directly
    monkeypatch.setattr(oe, "get_daily_opportunities", lambda **kw: [])
    monkeypatch.setattr(pdb, "get_all_latest_predictions", lambda: {})
    monkeypatch.setattr(udb, "get_sectors", lambda tickers: {})


def _scope_for_local_owner() -> str:
    ctx = local_context("test")
    acct = account_service.create_account(ctx, broker="manual", display_name="Local")
    return f"account:{acct['account_id']}"


def test_prepare_inputs_uses_the_scenarios_own_owner_not_local_owner():
    update_user_profile(max_sector_pct=25.0, max_issuer_pct=15.0)   # LOCAL_OWNER's real mandate limits

    from portfolio_agent.services.context import RequestContext, _mint_identity_for_tests
    bob_ctx = RequestContext(identity=_mint_identity_for_tests("bob"), source="test")
    update_user_profile_for(bob_ctx, max_sector_pct=40.0, max_issuer_pct=30.0)   # bob's own, wider limits

    from portfolio_agent.scenarios.prepare import prepare_inputs

    scope = _scope_for_local_owner()

    inputs, _ctx = prepare_inputs(scope=scope, holdings=[], owner="bob")
    assert (float(inputs.limits.max_sector), float(inputs.limits.max_issuer)) == (0.40, 0.30)

    inputs, _ctx = prepare_inputs(scope=scope, holdings=[], owner=None)
    assert (float(inputs.limits.max_sector), float(inputs.limits.max_issuer)) == (0.25, 0.15)
