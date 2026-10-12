"""Step 3.2: snapshot and ledger accounts side by side. Switching with opening balances, derived positions, the guards that stop hand edits
of ledger-managed positions, reconciliation that reports but never invents trades, and the table upgrade for a ledger created before
OPENING_BALANCE existed."""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.services import account_service as accts
from portfolio_agent.services import ledger_service as led
from portfolio_agent.services.context import local_context
from portfolio_agent.tools import holdings_db as repo
from portfolio_agent.tools import ledger_db as ldb

ME = local_context("test")
day = lambda back=0: (date.today() - timedelta(days=back)).isoformat()


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")


def snapshot_account(positions=(("VTI", 10, 200.0, 2000.0, 250.0),), cash=500.0, name="Snap"):
    a = accts.create_account(ME, broker="manual", display_name=name)["account_id"]
    for t, sh, avg, basis, px in positions:
        accts.add_position(ME, a, {"ticker": t, "shares": sh, "avg_cost": avg, "cost_basis_total": basis, "current_price": px, "sector": "Tech"})
    if cash is not None:
        accts.record_account_cash(ME, a, cash, as_of_date=day(1))
    return a


def positions(a):
    return {p["ticker"]: p for p in repo.list_positions(a)}


# ── switching ─────────────────────────────────────────────────────────────────

def test_switch_records_opening_balances_not_trades_and_positions_are_unchanged():
    a = snapshot_account((("VTI", 10, 200.0, 2000.0, 250.0), ("BND", 5.5, 70.0, 385.0, 72.0)), cash=500.0)
    before = {t: (p["shares"], p["cost_basis_total"], p["current_price"], p["position_id"]) for t, p in positions(a).items()}
    out = led.switch_to_ledger(ME, a, day(1), reason="start tracking")
    assert out["opening_transactions"] == 3 and out["unknown_basis"] == []
    acct = repo.get_account(a)
    assert acct["holdings_source"] == "ledger" and acct["ledger_start_date"] == day(1)
    txns = led.list_transactions(ME, a)
    assert {t["txn_type"] for t in txns} == {"OPENING_BALANCE"} and all(t["source"] == "opening_balance" for t in txns)    # no BUY / SELL invented
    assert {t["ticker"]: (t["quantity"], t["price"]) for t in txns if t["ticker"]} == {"VTI": (10, 200.0), "BND": (5.5, 70.0)}
    after = {t: (p["shares"], p["cost_basis_total"], p["current_price"], p["position_id"]) for t, p in positions(a).items()}
    assert after == before                                                     # same shares, basis, price AND the same position rows
    assert repo.get_account_cash(a)["amount"] == 500.0
    lots = led.get_lots(ME, a)
    assert {l["ticker"]: (l["remaining_quantity"], l["unit_cost"], l["source_type"]) for l in lots} == \
        {"VTI": (10, 200.0, "OPENING_BALANCE"), "BND": (5.5, 70.0, "OPENING_BALANCE")}
    ev = [e for e in repo.get_audit_events(limit=50, account_id=a) if e["operation"] == "switch_to_ledger"][0]
    assert ev["after"]["opening_transactions"] == 3 and ev["reason"] == "start tracking"
    with pytest.raises(led.InvalidTransaction, match="already"):
        led.switch_to_ledger(ME, a, day(1))


def test_opening_lots_are_real_lots_for_later_sales():
    a = snapshot_account()
    led.switch_to_ledger(ME, a, day(30))
    s = led.record_trade(ME, a, "VTI", "SELL", 4, 260.0, day(2))
    assert s["realized"][0]["cost_basis"] == 800.0 and s["realized"][0]["realized_gain"] == pytest.approx(240.0)
    assert s["realized"][0]["holding_period"] == "SHORT"                       # held from the opening date, not from when it was really bought
    p = positions(a)["VTI"]
    assert p["shares"] == 6 and p["cost_basis_total"] == 1200.0 and p["avg_cost"] == 200.0 and p["current_price"] == 250.0
    assert p["current_value"] == pytest.approx(1500.0)
    assert repo.get_account_cash(a)["amount"] == pytest.approx(500 + 1040)


def test_unknown_cost_basis_is_refused_unless_given_or_explicitly_allowed():
    a = accts.create_account(ME, broker="manual", display_name="NoBasis")["account_id"]
    accts.add_position(ME, a, {"ticker": "AAA", "shares": 3, "current_price": 10.0})
    accts.add_position(ME, a, {"ticker": "BBB", "shares": 2, "avg_cost": 5.0, "current_price": 6.0})
    with pytest.raises(led.InvalidTransaction, match="AAA: no cost basis"):
        led.switch_to_ledger(ME, a, day(1))
    assert repo.get_account(a)["holdings_source"] == "snapshot" and led.list_transactions(ME, a) == []          # nothing was written
    out = led.switch_to_ledger(ME, a, day(1), basis_overrides={"aaa": 8.0})
    assert out["unknown_basis"] == [] and {l["ticker"]: l["unit_cost"] for l in led.get_lots(ME, a)} == {"AAA": 8.0, "BBB": 5.0}
    b = accts.create_account(ME, broker="manual", display_name="Allow")["account_id"]
    accts.add_position(ME, b, {"ticker": "CCC", "shares": 1, "current_price": 10.0})
    out = led.switch_to_ledger(ME, b, day(1), allow_unknown_basis=True)
    assert out["unknown_basis"] == ["CCC"] and led.get_lots(ME, b)[0]["unit_cost"] == 0.0
    assert "cost basis unknown" in led.list_transactions(ME, b)[0]["memo"]


def test_short_positions_and_future_dates_are_refused():
    a = accts.create_account(ME, broker="manual", display_name="Short")["account_id"]
    accts.add_position(ME, a, {"ticker": "SHRT", "shares": -5, "avg_cost": 10.0})
    with pytest.raises(led.InvalidTransaction, match="short"):
        led.switch_to_ledger(ME, a, day(1))
    with pytest.raises(led.InvalidTransaction, match="future"):
        led.switch_to_ledger(ME, snapshot_account(name="F"), (date.today() + timedelta(days=1)).isoformat())


def test_an_empty_snapshot_account_can_switch_and_start_from_nothing():
    a = accts.create_account(ME, broker="manual", display_name="Empty")["account_id"]
    assert led.switch_to_ledger(ME, a, day(5))["opening_transactions"] == 0
    led.record_cash_transfer(ME, a, "DEPOSIT", 1000, day(4))
    led.record_trade(ME, a, "VTI", "BUY", 3, 100, day(3))
    assert positions(a)["VTI"]["shares"] == 3 and repo.get_account_cash(a)["amount"] == 700.0


def test_history_before_the_opening_date_is_kept_but_not_replayed_and_cannot_be_added_later():
    a = snapshot_account()
    led.record_trade(ME, a, "OLD", "SELL", 99, 1.0, day(200))                   # history-only on a snapshot account: stored, unvalidated
    led.switch_to_ledger(ME, a, day(10))
    assert any(t["ticker"] == "OLD" for t in led.list_transactions(ME, a))      # still in the record
    assert "OLD" not in positions(a) and led.get_ledger_summary(ME, a)["positions"] == {"VTI": {"quantity": 10, "cost_basis": 2000.0, "avg_cost": 200.0}}
    with pytest.raises(led.InvalidTransaction, match="before this account's ledger started"):
        led.record_trade(ME, a, "VTI", "BUY", 1, 1.0, day(20))
    led.record_trade(ME, a, "VTI", "BUY", 1, 250.0, day(10))                    # on the opening date itself is fine
    assert positions(a)["VTI"]["shares"] == 11


# ── positions are derived ─────────────────────────────────────────────────────

def test_every_ledger_write_keeps_positions_and_cash_in_step_including_removal():
    a = snapshot_account(cash=None)
    led.switch_to_ledger(ME, a, day(30))
    led.record_trade(ME, a, "MSFT", "BUY", 2, 400.0, day(5), fees=1.0)
    assert positions(a)["MSFT"]["shares"] == 2 and positions(a)["MSFT"]["cost_basis_total"] == 801.0
    assert repo.get_account_cash(a)["amount"] == -801.0                         # no deposit recorded: cash shows it, nothing is invented
    led.record_trade(ME, a, "MSFT", "SELL", 2, 410.0, day(3))
    assert "MSFT" not in positions(a)                                           # fully sold: position removed
    t = led.record_trade(ME, a, "VTI", "SELL", 1, 250.0, day(2))["txn"]["txn_id"]
    assert positions(a)["VTI"]["shares"] == 9
    led.void_transaction(ME, t, reason="duplicate")
    assert positions(a)["VTI"]["shares"] == 10                                  # a void restores it


def test_rebuild_repairs_positions_that_drifted_and_is_refused_for_snapshot_accounts():
    a = snapshot_account()
    led.switch_to_ledger(ME, a, day(5))
    with sqlite3.connect(db_module.DB_PATH) as c:                                # simulate corruption behind the service's back
        c.execute("UPDATE positions SET shares = 999 WHERE account_id = ?", [a])
        c.execute("INSERT INTO positions (account_id, instrument_id, shares) SELECT ?, instrument_id, 1 FROM instruments WHERE ticker = 'VTI' AND 0", [a])
    out = led.rebuild_positions_from_ledger(ME, a, reason="repair")
    assert out["updated"] == ["VTI"] and positions(a)["VTI"]["shares"] == 10
    assert led.rebuild_positions_from_ledger(ME, a) == {"added": [], "updated": ["VTI"], "removed": [], "cash": 500.0}      # idempotent
    snap = snapshot_account(name="S2")
    with pytest.raises(led.InvalidTransaction, match="snapshot account"):
        led.rebuild_positions_from_ledger(ME, snap)


def test_hand_edits_and_imports_are_refused_for_ledger_accounts_but_not_snapshot_ones():
    a = snapshot_account()
    snap = snapshot_account(name="Other")
    led.switch_to_ledger(ME, a, day(5))
    pid = positions(a)["VTI"]["position_id"]
    calls = [lambda: accts.add_position(ME, a, {"ticker": "NEW", "shares": 1}), lambda: accts.set_position(ME, pid, expected_version=None, shares=1, avg_cost=1),
             lambda: accts.remove_position(ME, pid), lambda: accts.record_account_cash(ME, a, 1.0, as_of_date=day())]
    for call in calls:
        with pytest.raises(accts.LedgerManaged, match="transaction ledger"):
            call()
    with repo.unit_of_work() as c:
        with pytest.raises(accts.LedgerManaged):
            accts.replace_account_snapshot(ME, a, [], as_of_date=day(), conn=c)
        accts.replace_account_snapshot(ME, snap, [{"ticker": "VTI", "shares": 1, "avg_cost": 1.0}], as_of_date=day(), conn=c)      # snapshot: as before
    assert positions(a)["VTI"]["shares"] == 10
    accts.set_position_price(ME, pid, 260.0)                                    # a price is market data, not a holding
    assert positions(a)["VTI"]["current_price"] == 260.0 and positions(a)["VTI"]["shares"] == 10


def test_switch_back_keeps_positions_clears_lots_and_a_second_switch_starts_a_fresh_opening():
    a = snapshot_account()
    led.switch_to_ledger(ME, a, day(30))
    led.record_trade(ME, a, "VTI", "BUY", 5, 240.0, day(10))
    acct = led.switch_to_snapshot(ME, a, reason="going manual")
    assert acct["holdings_source"] == "snapshot" and acct["ledger_start_date"] is None
    assert positions(a)["VTI"]["shares"] == 15 and led.get_lots(ME, a, open_only=False) == []      # positions stay as derived; lots only exist for ledger accounts
    assert len(led.list_transactions(ME, a)) == 3                                                  # two opening rows + the buy: kept as history
    accts.set_position(ME, positions(a)["VTI"]["position_id"], expected_version=None, shares=20, avg_cost=210.0)            # editable again
    with pytest.raises(led.InvalidTransaction, match="already a snapshot"):
        led.switch_to_snapshot(ME, a)
    led.switch_to_ledger(ME, a, day(2))
    assert led.get_ledger_summary(ME, a)["positions"]["VTI"]["quantity"] == 20                     # the old opening + buy are before the new start: not replayed
    assert positions(a)["VTI"]["shares"] == 20 and len(led.get_lots(ME, a)) == 1


# ── reconciliation never invents trades ───────────────────────────────────────

def test_reconcile_reports_every_kind_of_difference_and_changes_nothing():
    a = snapshot_account((("VTI", 10, 200.0, 2000.0, 250.0), ("BND", 5, 70.0, 350.0, 72.0)), cash=500.0)
    led.switch_to_ledger(ME, a, day(30))
    led.record_trade(ME, a, "AAPL", "BUY", 2, 180.0, day(5))
    n_txns, before_pos = len(led.list_transactions(ME, a)), {t: p["shares"] for t, p in positions(a).items()}
    r = led.reconcile_account(ME, a, [{"ticker": "vti", "shares": 10}, {"ticker": "BND", "shares": 6, "cost_basis_total": 400.0},
                                      {"ticker": "NVDA", "shares": 3}], statement_cash=140.0)
    by = {p["ticker"]: p for p in r["positions"]}
    assert by["VTI"]["status"] == "MATCH" and by["BND"]["status"] == "DIFFERENT" and by["BND"]["difference"] == 1
    assert by["AAPL"]["status"] == "ONLY_IN_LEDGER" and by["NVDA"]["status"] == "ONLY_IN_STATEMENT"
    assert (by["BND"]["ledger_cost_basis"], by["BND"]["statement_cost_basis"]) == (350.0, 400.0)
    assert r["cash"] == {"ledger": 140.0, "statement": 140.0, "difference": 0.0, "status": "MATCH"} and r["matches"] is False
    assert r["summary"] == {"MATCH": 1, "DIFFERENT": 1, "ONLY_IN_LEDGER": 1, "ONLY_IN_STATEMENT": 1}
    assert len(led.list_transactions(ME, a)) == n_txns and {t: p["shares"] for t, p in positions(a).items()} == before_pos    # no trade invented, no position edited
    ok = led.reconcile_account(ME, a, [{"ticker": "VTI", "shares": 10}, {"ticker": "BND", "shares": 5}, {"ticker": "AAPL", "shares": 2}], statement_cash=140.0)
    assert ok["matches"] is True
    assert led.reconcile_account(ME, a, [{"ticker": "VTI", "shares": 10}, {"ticker": "BND", "shares": 5}, {"ticker": "AAPL", "shares": 2}],
                                 statement_cash=100.0)["cash"]["status"] == "DIFFERENT"
    assert "reconcile_ledger" in [e["operation"] for e in repo.get_audit_events(limit=50, account_id=a)]


def test_reconcile_as_of_a_date_ignores_later_transactions_and_tolerates_rounding():
    a = snapshot_account(cash=None)
    led.switch_to_ledger(ME, a, day(30))
    led.record_trade(ME, a, "VTI", "BUY", 5, 240.0, day(5))
    early = led.reconcile_account(ME, a, [{"ticker": "VTI", "shares": 10.0004}], as_of=day(10))
    assert early["positions"][0]["status"] == "MATCH"                           # within the 0.001-share tolerance, before the buy
    late = led.reconcile_account(ME, a, [{"ticker": "VTI", "shares": 10}])
    assert late["positions"][0]["status"] == "DIFFERENT" and late["positions"][0]["ledger_shares"] == 15
    with pytest.raises(led.InvalidTransaction):
        led.reconcile_account(ME, a, [{"shares": 1}])


def test_only_the_owner_can_switch_or_reconcile():
    from portfolio_agent.services.context import RequestContext, _mint_identity_for_tests
    other = RequestContext(identity=_mint_identity_for_tests("someone-else"), source="test")
    a = snapshot_account()
    for call in (lambda: led.switch_to_ledger(other, a, day(1)), lambda: led.reconcile_account(other, a, []),
                 lambda: led.rebuild_positions_from_ledger(other, a), lambda: led.switch_to_snapshot(other, a)):
        with pytest.raises((led.NotAuthorized, led.NotFound)):
            call()
    assert repo.get_account(a)["holdings_source"] == "snapshot"


# ── schema upgrade ────────────────────────────────────────────────────────────

OLD_DDL = """CREATE TABLE transactions (
    txn_id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL REFERENCES accounts(account_id),
    instrument_id INTEGER REFERENCES instruments(instrument_id),
    txn_type TEXT NOT NULL CHECK (txn_type IN ('BUY','SELL','DEPOSIT','WITHDRAWAL','DIVIDEND','INTEREST','EMPLOYER_MATCH','FEE','SPLIT','VOID')),
    trade_date TEXT NOT NULL, settle_date TEXT, quantity REAL, price REAL, fees REAL NOT NULL DEFAULT 0, amount REAL NOT NULL DEFAULT 0,
    split_numerator REAL, split_denominator REAL, lot_method TEXT, lot_selection_json TEXT, voids_txn_id INTEGER REFERENCES transactions(txn_id),
    external_id TEXT, memo TEXT, source TEXT, created_by TEXT NOT NULL, request_id TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')))"""


def test_a_ledger_created_before_opening_balances_existed_is_upgraded_in_place():
    a = snapshot_account(name="Pre")
    led.record_cash_transfer(ME, a, "DEPOSIT", 100, day(3), external_id="x1")
    with sqlite3.connect(db_module.DB_PATH) as c:                                # put the OLD table shape back, rows included
        newer = {"contribution_source", "clearance_status", "clearance_approval_id", "import_run_id"}      # columns added after the old shape
        cols = [r[1] for r in c.execute("PRAGMA table_info(transactions)") if r[1] not in newer]
        rows = c.execute(f"SELECT {', '.join(cols)} FROM transactions").fetchall()
        c.execute("DROP TABLE transactions")
        c.execute(OLD_DDL)
        c.executemany(f"INSERT INTO transactions ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", rows)
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO transactions (account_id, txn_type, trade_date, created_by) VALUES (1, 'OPENING_BALANCE', '2026-01-01', 'x')")
        c.commit()
    for _ in range(2):                                                           # any connection upgrades it; a second pass changes nothing
        repo.get_account(a)
    with sqlite3.connect(db_module.DB_PATH) as c:
        sql = c.execute("SELECT sql FROM sqlite_master WHERE name = 'transactions'").fetchone()[0]
        assert "OPENING_BALANCE" in sql and "clearance_status" in sql
        assert c.execute("SELECT txn_id, txn_type, amount, external_id FROM transactions").fetchall() == [(1, "DEPOSIT", 100.0, "x1")]
        names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type IN ('trigger', 'index')")}
        assert {"trg_transactions_no_update", "trg_transactions_no_delete", "idx_transactions_external", "idx_transactions_one_void"} <= names
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            c.execute("DELETE FROM transactions")
    led.switch_to_ledger(ME, a, day(2))                                          # and the new type works end to end
    assert [t["txn_type"] for t in led.list_transactions(ME, a)][-1] == "OPENING_BALANCE"
    assert led.record_cash_transfer(ME, a, "DEPOSIT", 5, day(1))["txn"]["txn_id"] > 2        # ids continue where they were
