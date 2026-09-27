"""
portfolio_tools.get_portfolio_holdings/get_portfolio_concentration — repro
tests for the leak where the chat risk agent computed weight/concentration
over every portfolio's combined holdings. tool_context.state["owner"] is set
server-side (web/chat/orchestration.py's ADK create_session) and is never
something the model can supply itself; a tool_context with no owner (batch
jobs, the Valuation page) must keep the old single-portfolio behavior.
"""
from __future__ import annotations

import json

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.services import account_service
from portfolio_agent.services.context import RequestContext, _mint_identity_for_tests, local_context
from portfolio_agent.tools import portfolio_tools


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")


class _FakeToolContext:
    def __init__(self, owner: str | None):
        self.state = {"owner": owner}


def _seed_second_owner(owner: str) -> None:
    from portfolio_agent.tools import holdings_db as repo
    with repo.unit_of_work() as conn:
        conn.execute("INSERT INTO portfolios (owner) VALUES (?)", (owner,))


def test_get_portfolio_holdings_scopes_to_the_chat_owner_not_the_combined_book():
    from tests.helpers_holdings import seed_holding
    seed_holding({"ticker": "AAPL", "shares": 10, "avg_cost": 100.0, "cost_basis_total": 1000.0,
                 "account_name": "Individual", "account_number": "X1", "account_type": "TAXABLE"},
                "fidelity", "2026-09-01")

    _seed_second_owner("bob")
    bob = RequestContext(identity=_mint_identity_for_tests("bob"), source="test")
    bob_acct = account_service.create_account(bob, broker="manual", display_name="Bob's account")
    account_service.add_position(bob, bob_acct["account_id"],
                                 {"ticker": "TSLA", "shares": 5, "avg_cost": 200.0, "cost_basis_total": 1000.0})

    bob_view = json.loads(portfolio_tools.get_portfolio_holdings(_FakeToolContext("bob")))
    local_view = json.loads(portfolio_tools.get_portfolio_holdings(_FakeToolContext(local_context("test").actor)))

    assert {h["ticker"] for h in bob_view["holdings"]} == {"TSLA"}
    assert {h["ticker"] for h in local_view["holdings"]} == {"AAPL"}


def test_get_portfolio_holdings_with_no_tool_context_keeps_the_legacy_unscoped_read():
    """Batch/pipeline/Valuation-page callers invoke this with no tool_context
    at all — that path must keep working exactly as before."""
    from tests.helpers_holdings import seed_holding
    seed_holding({"ticker": "AAPL", "shares": 10, "avg_cost": 100.0, "cost_basis_total": 1000.0,
                 "account_name": "Individual", "account_number": "X1", "account_type": "TAXABLE"},
                "fidelity", "2026-09-01")

    view = json.loads(portfolio_tools.get_portfolio_holdings())
    assert {h["ticker"] for h in view["holdings"]} == {"AAPL"}


def test_a_logged_in_owner_with_no_portfolio_yet_sees_empty_not_everyone_elses():
    from tests.helpers_holdings import seed_holding
    seed_holding({"ticker": "AAPL", "shares": 10, "avg_cost": 100.0, "cost_basis_total": 1000.0,
                 "account_name": "Individual", "account_number": "X1", "account_type": "TAXABLE"},
                "fidelity", "2026-09-01")

    view = json.loads(portfolio_tools.get_portfolio_holdings(_FakeToolContext("brand-new-user")))
    assert view["holdings"] == []


def test_get_portfolio_concentration_is_scoped_the_same_way():
    from tests.helpers_holdings import seed_holding
    seed_holding({"ticker": "AAPL", "shares": 10, "avg_cost": 100.0, "cost_basis_total": 1000.0,
                 "account_name": "Individual", "account_number": "X1", "account_type": "TAXABLE"},
                "fidelity", "2026-09-01")

    _seed_second_owner("bob")
    bob = RequestContext(identity=_mint_identity_for_tests("bob"), source="test")
    bob_acct = account_service.create_account(bob, broker="manual", display_name="Bob's account")
    account_service.add_position(bob, bob_acct["account_id"],
                                 {"ticker": "TSLA", "shares": 5, "avg_cost": 200.0, "cost_basis_total": 1000.0})

    bob_conc = json.loads(portfolio_tools.get_portfolio_concentration("AAPL", _FakeToolContext("bob")))
    # bob holds only TSLA -- must not see the local owner's AAPL position at all
    assert bob_conc["holdings_count"] == 1
    assert bob_conc["ticker_current_value"] == 0.0
