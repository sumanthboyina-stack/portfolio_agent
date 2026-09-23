"""Account/position service: authorization, distinct operations, versions and audit."""
from __future__ import annotations

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.services import account_service as svc
from portfolio_agent.services.context import RequestContext, local_context
from portfolio_agent.tools import holdings_db as repo
from tests.helpers_holdings import CTX, seed_holding


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")


def _pos(ticker, shares, account="X1", **extra):
    return {"ticker": ticker, "shares": shares, "avg_cost": 100.0, "cost_basis_total": shares * 100.0,
            "account_name": "Individual", "account_number": account, "account_type": "TAXABLE", **extra}


def _ops(**kw):
    return [e["operation"] for e in repo.get_audit_events(**kw)]


def test_foreign_actor_cannot_touch_the_local_portfolio():
    a = seed_holding(_pos("AAPL", 1), "fidelity", "2026-09-01")
    stranger = RequestContext(actor="someone-else", source="test")
    with pytest.raises(svc.NotAuthorized):
        svc.set_position(stranger, a["id"], expected_version=1, shares=2.0, avg_cost=1.0)
    with pytest.raises(svc.NotAuthorized):
        svc.create_account(stranger, broker="manual", display_name="Theirs")
    assert repo.get_position(a["id"])["shares"] == 1 and repo.list_accounts() and len(repo.list_accounts()) == 1


def test_create_account_keys_unnumbered_accounts_by_name_and_rejects_duplicates():
    a = svc.create_account(CTX, broker="Manual", display_name="Roth", account_type="ROTH_IRA")
    assert (a["broker"], a["account_number"], a["display_name"]) == ("manual", "Roth", "Roth")
    with pytest.raises(svc.DuplicateAccount):
        svc.create_account(CTX, broker="manual", display_name="Roth")
    b = svc.create_account(CTX, broker="manual", display_name="Brokerage")
    assert a["account_id"] != b["account_id"]
    assert _ops() == ["create_account", "create_account"]


def test_add_position_rejects_duplicate_unless_update_is_explicit():
    a = seed_holding(_pos("AAPL", 10), "fidelity", "2026-09-01")
    with pytest.raises(svc.DuplicatePosition):
        svc.add_position(CTX, a["account_id"], _pos("AAPL", 20))
    assert repo.get_position(a["id"])["shares"] == 10                              # untouched, not doubled
    updated = svc.add_position(CTX, a["account_id"], _pos("AAPL", 20), on_duplicate="update")
    assert updated["position_id"] == a["id"] and updated["shares"] == 20 and updated["version"] == 2
    assert _ops(position_id=a["id"]) == ["set_position", "add_position"]


def test_set_position_is_a_correction_with_optimistic_locking():
    a = seed_holding(_pos("AAPL", 10, current_price=50.0, current_value=500.0), "fidelity", "2026-09-01")
    after = svc.set_position(CTX, a["id"], expected_version=a["version"], shares=20.0, avg_cost=90.0, reason="typo")
    assert (after["shares"], after["cost_basis_total"], after["current_value"], after["version"]) == (20.0, 1800.0, 1000.0, 2)
    with pytest.raises(svc.VersionConflict):                                        # stale editor
        svc.set_position(CTX, a["id"], expected_version=a["version"], shares=1.0, avg_cost=1.0)
    assert repo.get_position(a["id"])["shares"] == 20.0
    ev = repo.get_audit_events(position_id=a["id"])[0]
    assert ev["operation"] == "set_position" and ev["reason"] == "typo" and ev["actor"] == CTX.actor
    assert ev["before"]["shares"] == 10 and ev["after"]["shares"] == 20 and ev["request_id"] == CTX.request_id


def test_audit_event_lands_in_the_same_transaction_as_the_mutation(monkeypatch):
    a = seed_holding(_pos("AAPL", 10), "fidelity", "2026-09-01")
    real = repo.record_audit_event

    def boom(**kw):
        if kw.get("operation") == "remove_position":
            raise RuntimeError("audit store down")
        return real(**kw)

    monkeypatch.setattr(repo, "record_audit_event", boom)
    with pytest.raises(RuntimeError):
        svc.remove_position(CTX, a["id"])
    assert repo.get_position(a["id"]) is not None                                   # delete rolled back with the audit


def test_archive_hides_account_and_restore_brings_it_back_with_ids_intact():
    a = seed_holding(_pos("AAPL", 10), "fidelity", "2026-09-01")
    b = seed_holding(_pos("VTI", 1, account="X2"), "fidelity", "2026-09-01")
    with repo.transaction() as conn:
        repo.set_account_cash(a["account_id"], 9.0, "2026-09-01", conn=conn)
    svc.archive_account(CTX, a["account_id"], reason="closed at broker")
    assert [h["id"] for h in repo.get_holdings()] == [b["id"]]
    assert repo.get_cash_balances() == []
    assert [x["account_id"] for x in repo.list_accounts()] == [b["account_id"]]
    with pytest.raises(svc.NotFound):                                               # archived = not editable
        svc.set_position(CTX, a["id"], expected_version=None, shares=1.0, avg_cost=1.0)
    svc.restore_account(CTX, a["account_id"])
    assert {h["id"] for h in repo.get_holdings()} == {a["id"], b["id"]}
    assert repo.get_cash_balances()[0]["amount"] == 9.0
    assert _ops(account_id=a["account_id"])[:2] == ["restore_account", "archive_account"]


def test_permanent_delete_is_distinct_and_keeps_price_history():
    a = seed_holding(_pos("AAPL", 10), "fidelity", "2026-09-01")
    repo.insert_price_history([repo.history_row("2026-09-01", _pos("AAPL", 10) | {"broker": "fidelity"}, 100.0,
                                                repo.HISTORY_SOURCE_SNAPSHOT)], replace=True)
    res = svc.delete_account_permanently(CTX, a["account_id"], reason="entered twice")
    assert res["positions"] == 1 and repo.get_account(a["account_id"]) is None and repo.get_holdings() == []
    assert len(repo.get_price_history(tickers=["AAPL"])) == 1
    ev = repo.get_audit_events(account_id=a["account_id"])[0]
    assert ev["operation"] == "delete_account" and ev["before"]["positions"][0]["ticker"] == "AAPL"


def test_operations_compose_into_one_transaction_via_conn():
    acct = svc.create_account(CTX, broker="manual", display_name="Brokerage")
    with pytest.raises(RuntimeError):
        with repo.transaction() as conn:
            svc.add_position(CTX, acct["account_id"], _pos("AAPL", 1), conn=conn)
            svc.record_account_cash(CTX, acct["account_id"], 10.0, as_of_date="2026-09-01", conn=conn)
            raise RuntimeError("abort")
    assert repo.get_holdings() == [] and repo.get_cash_balances() == []
    assert _ops() == ["create_account"]


def test_local_context_resolves_to_the_seeded_local_portfolio():
    p = svc.resolve_portfolio(local_context("web:test"))
    assert p["owner"] == "local" and p["base_currency"] == "USD"
