from __future__ import annotations

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.services import account_service
from portfolio_agent.tools.holdings_db import get_audit_events, get_cash_balances, get_holdings, get_import_runs
from tests.helpers_holdings import CTX, account_id_of, seed_holding
from portfolio_agent.tools.holdings_import import ParsedUpload, build_plan, commit_plan, parse_upload

FIDELITY_CSV = b"""Account Number,Account Name,Symbol,Description,Quantity,Last Price,Current Value,Cost Basis Total,Average Cost Basis
X12345678,Individual - TOD,AAPL,APPLE INC,12,$100.00,"$1,200.00",$800.00,$66.67
X12345678,Individual - TOD,MSFT,MICROSOFT,5,$300.00,"$1,500.00","$1,000.00",$200.00
X12345678,Individual - TOD,SPAXX**,FIDELITY GOVERNMENT MONEY MARKET,,,"$2,500.50",,

"09/19/2026 4:01 PM ET"
"""


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")


def _pos(ticker, shares, account="X12345678", **extra):
    return {"ticker": ticker, "shares": shares, "avg_cost": 100.0, "cost_basis_total": shares * 100.0,
            "account_name": "Individual - TOD", "account_number": account, "account_type": "TAXABLE", **extra}


def test_plan_counts_reflect_diff_against_stored_holdings():
    seed_holding(_pos("AAPL", 10), "fidelity", "2026-09-01")      # will be updated (10 → 12 shares)
    seed_holding(_pos("GOOG", 3), "fidelity", "2026-09-01")       # not in file → removed
    seed_holding(_pos("VTI", 1, account="OTHER"), "fidelity", "2026-09-01")  # other account → untouched
    parsed = parse_upload(FIDELITY_CSV, "Portfolio_Positions.csv")
    assert parsed.broker == "fidelity" and parsed.unnamed_groups == [] and parsed.statement_date == "2026-09-19"
    plan = build_plan(parsed)
    assert len(plan.accounts) == 1
    a = plan.accounts[0]
    assert a.added == ["MSFT"] and a.removed == ["GOOG"] and [u["ticker"] for u in a.updated] == ["AAPL"]
    assert a.cash_amount == 2500.5 and plan.totals == {"added": 1, "updated": 1, "removed": 1, "unchanged": 0,
                                                       "positions": 2, "cash_accounts": 1}


def test_commit_replaces_only_that_account_records_cash_and_history():
    seed_holding(_pos("AAPL", 10), "fidelity", "2026-09-01")
    seed_holding(_pos("GOOG", 3), "fidelity", "2026-09-01")
    seed_holding(_pos("VTI", 1, account="OTHER"), "fidelity", "2026-09-01")
    plan = build_plan(parse_upload(FIDELITY_CSV, "positions.csv"))
    result = commit_plan(plan, fetch_prices=False)
    assert (result.added, result.updated, result.removed, result.positions) == (1, 1, 1, 2)
    assert "1 position added, 1 updated, 1 removed" in result.message
    stored = {(h["account_number"], h["ticker"]): h for h in get_holdings("fidelity")}
    assert set(stored) == {("X12345678", "AAPL"), ("X12345678", "MSFT"), ("OTHER", "VTI")}
    assert stored[("X12345678", "AAPL")]["shares"] == 12
    assert stored[("X12345678", "AAPL")]["as_of_date"] == "2026-09-19"          # statement date from file
    assert stored[("X12345678", "AAPL")]["price_as_of"] == "2026-09-19"         # file price is as of statement
    cash = get_cash_balances("fidelity")
    assert len(cash) == 1 and cash[0]["amount"] == 2500.5 and cash[0]["account_number"] == "X12345678"
    runs = get_import_runs()
    assert len(runs) == 1 and runs[0]["added"] == 1 and runs[0]["accounts"] == ["Individual - TOD"]
    # Re-importing the same file changes nothing and says so — and position ids are stable across imports.
    ids_before = {h["ticker"]: h["id"] for h in get_holdings("fidelity")}
    again = commit_plan(build_plan(parse_upload(FIDELITY_CSV, "positions.csv")), fetch_prices=False)
    assert (again.added, again.updated, again.removed) == (0, 0, 0)
    assert {h["ticker"]: h["id"] for h in get_holdings("fidelity")} == ids_before
    assert all(h["source_import_id"] == again.run_id for h in get_holdings("fidelity") if h["account_number"] == "X12345678")


def test_unnamed_accounts_need_labels_then_become_their_own_bucket():
    parsed = ParsedUpload("other", [{"ticker": "AAPL", "shares": 1, "account_name": "Roth", "account_number": ""},
                                    {"ticker": "MSFT", "shares": 1, "account_name": "", "account_number": ""}], [])
    assert parsed.unnamed_groups == ["Roth", ""]
    with pytest.raises(ValueError):
        build_plan(parsed, {"Roth": "Roth IRA"})       # "" still unlabelled
    plan = build_plan(parsed, {"Roth": "Roth IRA", "": "Taxable"})
    assert sorted(a.account_number for a in plan.accounts) == ["Roth IRA", "Taxable"]
    commit_plan(plan, fetch_prices=False)
    assert {(h["account_number"], h["ticker"]) for h in get_holdings("other")} == {("Roth IRA", "AAPL"), ("Taxable", "MSFT")}


def test_rename_account_keeps_identity():
    a = seed_holding(_pos("AAPL", 10), "fidelity", "2026-09-01")
    acct_id = account_id_of("fidelity", "X12345678")
    after = account_service.rename_account(CTX, acct_id, "Taxable brokerage")
    assert after["display_name"] == "Taxable brokerage" and after["version"] == 2
    h = get_holdings("fidelity")[0]
    assert h["account_name"] == "Taxable brokerage" and h["account_number"] == "X12345678"
    assert h["id"] == a["id"] and h["account_id"] == acct_id            # identity unchanged


TWO_ACCOUNT_CSV = b"""Account Number,Account Name,Symbol,Description,Quantity,Last Price,Current Value,Cost Basis Total,Average Cost Basis
X12345678,Individual - TOD,AAPL,APPLE INC,12,$100.00,"$1,200.00",$800.00,$66.67
X12345678,Individual - TOD,SPAXX**,FIDELITY GOVERNMENT MONEY MARKET,,,"$2,500.50",,
Z99999999,Roth IRA,MSFT,MICROSOFT,5,$300.00,"$1,500.00","$1,000.00",$200.00
Z99999999,Roth IRA,SPAXX**,FIDELITY GOVERNMENT MONEY MARKET,,,"$10.00",,

"09/19/2026 4:01 PM ET"
"""


def test_commit_is_atomic_across_accounts(monkeypatch):
    """A failure while writing the SECOND account must roll back the first
    account's replacement, its cash, and leave no import receipt behind."""
    svc = account_service
    seed_holding(_pos("AAPL", 10), "fidelity", "2026-09-01")
    seed_holding(_pos("GOOG", 3), "fidelity", "2026-09-01")
    seed_holding(_pos("MSFT", 1, account="Z99999999"), "fidelity", "2026-09-01")
    before = sorted((h["account_number"], h["ticker"], h["shares"]) for h in get_holdings("fidelity"))

    real_set_cash = svc.record_account_cash
    calls = {"n": 0}

    def flaky_set_cash(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("disk full")
        return real_set_cash(*args, **kwargs)

    monkeypatch.setattr(svc, "record_account_cash", flaky_set_cash)
    plan = build_plan(parse_upload(TWO_ACCOUNT_CSV, "positions.csv"))
    assert len(plan.accounts) == 2
    with pytest.raises(RuntimeError, match="disk full"):
        commit_plan(plan, fetch_prices=False)

    after = sorted((h["account_number"], h["ticker"], h["shares"]) for h in get_holdings("fidelity"))
    assert after == before, "first account's replacement leaked out of the failed import"
    assert get_cash_balances("fidelity") == []
    assert get_import_runs() == []
    assert not any(e["operation"] == "import_snapshot" for e in get_audit_events())   # audit rolled back too

    # The same plan commits cleanly once the fault is gone.
    monkeypatch.setattr(svc, "record_account_cash", real_set_cash)
    result = commit_plan(plan, fetch_prices=False)
    assert result.positions == 2 and result.cash_accounts == 2 and len(get_import_runs()) == 1
