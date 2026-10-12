"""Follow-up fixes: broker files for ledger-managed accounts are compared instead of failing the import, issuer class-share tickers are normalised,
one-time owner migrations no longer take the write lock on every connection, and the time-dependent tests no longer depend on the real date."""
from __future__ import annotations

import sqlite3
import time

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.services import account_service as accts
from portfolio_agent.services import ledger_service as led
from portfolio_agent.tools import holdings_db as repo
from portfolio_agent.tools import issuer_holdings as ih
from portfolio_agent.tools.holdings_import import build_plan, commit_plan, parse_upload
from tests.helpers_holdings import CTX, account_id_of
from tests.test_holdings_import import FIDELITY_CSV, _pos


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")


def _ledger_account_for_fidelity_file():
    """The account the FIDELITY_CSV file maps to (broker fidelity, number X12345678), switched to the ledger with AAPL 12 / MSFT 4 / cash 2500.50."""
    a = accts.create_account(CTX, broker="fidelity", account_number="X12345678", display_name="Individual - TOD")["account_id"]
    accts.add_position(CTX, a, {"ticker": "AAPL", "shares": 12, "cost_basis_total": 800.0})
    accts.add_position(CTX, a, {"ticker": "MSFT", "shares": 4, "cost_basis_total": 800.0})
    accts.record_account_cash(CTX, a, 2500.50, as_of_date="2026-09-01")
    led.switch_to_ledger(CTX, a, "2026-09-01")
    return a


def test_a_broker_file_for_a_ledger_account_is_compared_not_applied_and_the_rest_of_the_import_proceeds():
    a = _ledger_account_for_fidelity_file()
    other = accts.create_account(CTX, broker="fidelity", account_number="OTHER9999", display_name="Other")["account_id"]
    accts.add_position(CTX, other, {"ticker": "KO", "shares": 1, "cost_basis_total": 50.0})
    csv = FIDELITY_CSV + b""
    plan = build_plan(parse_upload(csv, "positions.csv"))
    acct = plan.accounts[0]
    assert acct.ledger_managed is True and acct.added == acct.updated == acct.removed == [] and plan.compared == 1
    assert plan.totals["added"] == plan.totals["updated"] == plan.totals["positions"] == 0 and plan.totals["cash_accounts"] == 0
    rec = acct.reconciliation
    by = {p["ticker"]: p["status"] for p in rec["positions"]}
    assert by == {"AAPL": "MATCH", "MSFT": "DIFFERENT"} and rec["cash"]["status"] == "MATCH" and rec["matches"] is False      # file says MSFT 5, ledger 4
    before = {t: (p["shares"], p["cost_basis_total"]) for t, p in ((p["ticker"], p) for p in repo.list_positions(a))}
    n_audit = len(repo.get_audit_events(limit=200, account_id=a))
    result = commit_plan(plan, fetch_prices=False)                                    # previously raised LedgerManaged and rolled everything back
    after = {p["ticker"]: (p["shares"], p["cost_basis_total"]) for p in repo.list_positions(a)}
    assert after == before and led.get_ledger_summary(CTX, a)["positions"]["MSFT"]["quantity"] == 4          # the ledger account was NOT changed
    assert len(led.list_transactions(CTX, a)) == 3                                    # two opening positions + opening cash: no trade invented
    assert (result.added, result.updated, result.removed, result.positions, result.cash_accounts) == (0, 0, 0, 0, 0)
    assert result.reconciled == [{"label": "Individual - TOD", "matches": False, "differences": 1, "tickers": ["MSFT"]}]
    assert "managed by its ledger" in result.message and "1 difference" in result.message and "MSFT" in result.message and "nothing changed" in result.message
    ops = [e["operation"] for e in repo.get_audit_events(limit=200, account_id=a)]
    assert ops.count("reconcile_ledger") == 1 and len(ops) == n_audit + 1             # one audited comparison, no import_snapshot for it
    runs = repo.get_import_runs(limit=5)
    assert runs[0]["positions"] == 0 and runs[0]["added"] == 0


def test_a_ledger_account_that_matches_the_file_says_so_and_a_snapshot_account_is_still_applied_as_before():
    a = _ledger_account_for_fidelity_file()
    led.record_trade(CTX, a, "MSFT", "BUY", 1, 300.0, "2026-09-10")                    # now MSFT 5, cash 2200.50 ... cash will differ
    led.record_cash_transfer(CTX, a, "DEPOSIT", 300.0, "2026-09-10")                    # ... until the deposit that funded it is recorded
    res = commit_plan(build_plan(parse_upload(FIDELITY_CSV, "positions.csv")), fetch_prices=False)
    assert res.reconciled[0]["matches"] is True and "the file matches it" in res.message
    snap = accts.create_account(CTX, broker="fidelity", account_number="S1", display_name="Snap")["account_id"]
    csv = FIDELITY_CSV.replace(b"X12345678", b"S1")
    res2 = commit_plan(build_plan(parse_upload(csv, "p.csv")), fetch_prices=False)
    assert res2.reconciled == [] and (res2.added, res2.positions) == (2, 2) and {p["ticker"] for p in repo.list_positions(snap)} == {"AAPL", "MSFT"}


def test_the_preview_does_not_audit_every_refresh():
    a = _ledger_account_for_fidelity_file()
    plan_args = (parse_upload(FIDELITY_CSV, "positions.csv"),)
    for _ in range(3):
        build_plan(*plan_args)
    assert [e for e in repo.get_audit_events(limit=200, account_id=a) if e["operation"] == "reconcile_ledger"] == []


def test_issuer_class_share_tickers_are_written_the_way_market_data_writes_them():
    assert [ih.normalize_issuer_ticker(t) for t in ("BRKB", "brka", "BRK.B", "BFB", "LENB", "AAPL", "-", "", "N/A", "bad ticker!")] == \
        ["BRK-B", "BRK-A", "BRK-B", "BF-B", "LEN-B", "AAPL", None, None, None, None]
    from pathlib import Path
    d = ih.issuer_file_to_fund_data("IVV", ih.parse_issuer_file((Path(__file__).parent / "fixtures" / "issuer" / "ishares_ivv_sample.csv").read_bytes()))
    assert "BRK-B" in {h.symbol for h in d.holdings} and "BRKB" not in {h.symbol for h in d.holdings}


def test_one_time_owner_migrations_do_not_wait_on_another_connections_write_lock():
    """Opening an approvals / profile / watchlist connection while a write transaction is open must not block (it used to wait out the busy
    timeout running a bare UPDATE that changed nothing)."""
    from portfolio_agent.tools import trade_approvals_db as tdb, user_profile_db as upd, watchlist_db as wdb
    for mod_open in (lambda: tdb.get_active_approval("user:x", "AAPL", "trade"), lambda: upd.get_user_profile_by_owner("someone"),
                     lambda: wdb.get_watchlist() if hasattr(wdb, "get_watchlist") else None):
        mod_open()                                                                  # schemas exist now
    blocker = sqlite3.connect(db_module.DB_PATH, timeout=30)
    blocker.execute("BEGIN IMMEDIATE")                                              # another connection holds the write lock
    try:
        t0 = time.perf_counter()
        assert tdb.get_active_approval("user:x", "AAPL", "trade") is None
        if hasattr(wdb, "get_watchlist"):
            wdb.get_watchlist()
        assert time.perf_counter() - t0 < 2.0
    finally:
        blocker.rollback()
        blocker.close()


def test_update_if_any_only_writes_when_a_row_matches():
    from portfolio_agent.tools.db import update_if_any
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE t (owner TEXT)")
    c.execute("INSERT INTO t VALUES ('a')")
    assert update_if_any(c, "t", "owner = 'local'", "UPDATE t SET owner = 'x' WHERE owner = 'local'") == 0
    c.execute("INSERT INTO t VALUES ('local')")
    assert update_if_any(c, "t", "owner = 'local'", "UPDATE t SET owner = ? WHERE owner = 'local'", ("me",)) == 1
    assert sorted(r[0] for r in c.execute("SELECT owner FROM t")) == ["a", "me"]
