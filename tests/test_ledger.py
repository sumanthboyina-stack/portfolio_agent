"""Transaction ledger (Step 3.1): the pure replay engine, and the service verbs that write the append-only ledger and rebuild tax lots."""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.services import account_service as accts
from portfolio_agent.services import investor_service as inv
from portfolio_agent.services import ledger_service as led
from portfolio_agent.services.context import RequestContext, _mint_identity_for_tests, local_context
from portfolio_agent.tools import auth_users_db
from portfolio_agent.tools import holdings_db as repo
from portfolio_agent.tools import investor_settings_db as sdb
from portfolio_agent.tools import ledger_db as ldb
from portfolio_agent.tools.ledger_engine import LedgerError, Txn, replay

ME = local_context("test")
D = lambda n: (date(2025, 1, 1) + timedelta(days=n)).isoformat()      # D(0) = 2025-01-01


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")


# ── engine ────────────────────────────────────────────────────────────────────

_n = [0]


def T(kind, day, *, iid=1, qty=None, px=None, fees=0.0, amt=0.0, method=None, sel=None, num=None, den=None, voids=None, tid=None):
    _n[0] += 1
    return Txn(txn_id=tid or _n[0] + 1000, account_id=1, txn_type=kind, trade_date=day, instrument_id=iid, quantity=qty, price=px, fees=fees,
               amount=amt, split_numerator=num, split_denominator=den, lot_method=method, lot_selection=sel, voids_txn_id=voids)


def buy(day, qty, px, fees=0.0, **kw):
    return T("BUY", day, qty=qty, px=px, fees=fees, amt=-(qty * px + fees), **kw)


def sell(day, qty, px, fees=0.0, method="FIFO", **kw):
    return T("SELL", day, qty=qty, px=px, fees=fees, amt=qty * px - fees, method=method, **kw)


def three_lots():
    return [buy(D(0), 10, 10.0, tid=1), buy(D(10), 10, 30.0, tid=2), buy(D(20), 10, 20.0, tid=3)]


@pytest.mark.parametrize("method,expect_lots", [("FIFO", [1]), ("LIFO", [3]), ("HIFO", [2])])
def test_each_method_picks_its_lot_and_realizes_the_hand_computed_gain(method, expect_lots):
    st = replay(three_lots() + [sell(D(30), 10, 25.0, method=method, tid=9)])
    assert [c.open_transaction_id for c in st.closures] == expect_lots
    basis = {1: 100.0, 2: 300.0, 3: 200.0}[expect_lots[0]]
    assert st.closures[0].cost_basis == basis and st.closures[0].realized_gain == pytest.approx(250.0 - basis)
    assert st.positions()[1]["quantity"] == 20 and st.cash == pytest.approx(-(100 + 300 + 200) + 250)


def test_a_sale_spans_lots_in_order_and_partial_lots_keep_their_remainder():
    st = replay(three_lots() + [sell(D(30), 15, 25.0, method="FIFO", tid=9)])
    assert [(c.open_transaction_id, c.quantity) for c in st.closures] == [(1, 10), (2, 5)]
    assert st.lots[2].remaining_quantity == 5 and st.lots[2].basis_remaining == 150.0 and st.lots[1].remaining_quantity == 0
    assert sum(c.proceeds for c in st.closures) == pytest.approx(375.0)
    assert st.realized_gain() == pytest.approx(375.0 - 100.0 - 150.0)


def test_fees_raise_basis_on_a_buy_and_cut_proceeds_on_a_sale_and_roll_up_pro_rata():
    st = replay([buy(D(0), 10, 10.0, fees=5.0, tid=1), buy(D(1), 10, 10.0, tid=2), sell(D(5), 15, 12.0, fees=3.0, tid=9)])
    assert st.lots[1].unit_cost == pytest.approx(10.5)                              # (100 + 5) / 10
    c1, c2 = st.closures
    assert (c1.proceeds, c2.proceeds) == (pytest.approx(118.0), pytest.approx(59.0))    # (180 - 3) split 10 : 5
    assert c1.cost_basis == 105.0 and c2.cost_basis == 50.0
    assert st.cash == pytest.approx(-105 - 100 + 177)


def test_specific_lots_are_honoured_and_checked():
    sel = [{"lot": 2, "quantity": 4}, {"lot": 3, "quantity": 1}]
    st = replay(three_lots() + [sell(D(30), 5, 25.0, method="SPECIFIC", sel=sel, tid=9)])
    assert [(c.open_transaction_id, c.quantity) for c in st.closures] == [(2, 4), (3, 1)]
    for bad in ([{"lot": 2, "quantity": 4}], [{"lot": 99, "quantity": 5}], [{"lot": 2, "quantity": 11}],
                [{"lot": 2, "quantity": 2}, {"lot": 2, "quantity": 3}]):
        with pytest.raises(LedgerError):
            replay(three_lots() + [sell(D(30), 5, 25.0, method="SPECIFIC", sel=bad, tid=9)])


def test_holding_period_is_long_only_after_more_than_365_days():
    st = replay([buy(D(0), 2, 10.0, tid=1), sell(D(365), 1, 11.0, tid=8), sell(D(366), 1, 11.0, tid=9)])
    assert [c.holding_period for c in st.closures] == ["SHORT", "LONG"]


def test_overselling_and_selling_before_buying_are_refused():
    with pytest.raises(LedgerError, match="only 10 held"):
        replay([buy(D(0), 10, 10.0, tid=1), sell(D(5), 11, 10.0, tid=2)])
    with pytest.raises(LedgerError):
        replay([sell(D(0), 1, 10.0, tid=1), buy(D(5), 1, 10.0, tid=2)])                 # backdated order matters, not insertion order


def test_split_and_reverse_split_scale_shares_and_unit_cost_but_not_total_basis():
    st = replay([buy(D(0), 10, 100.0, tid=1), T("SPLIT", D(5), num=2, den=1, tid=2), buy(D(6), 5, 60.0, tid=3)])
    assert st.lots[1].remaining_quantity == 20 and st.lots[1].unit_cost == 50.0 and st.lots[1].basis_remaining == 1000.0
    assert st.lots[3].remaining_quantity == 5 and st.lots[3].unit_cost == 60.0         # bought after: untouched
    rv = replay([buy(D(0), 100, 1.0, tid=1), T("SPLIT", D(5), num=1, den=10, tid=2)])
    assert rv.lots[1].remaining_quantity == 10 and rv.lots[1].unit_cost == 10.0 and rv.positions()[1]["cost_basis"] == 100.0
    sold_after = replay([buy(D(0), 10, 100.0, tid=1), T("SPLIT", D(5), num=2, den=1, tid=2), sell(D(6), 20, 60.0, tid=3)])
    assert sold_after.closures[0].cost_basis == 1000.0 and sold_after.closures[0].realized_gain == 200.0


def test_a_split_goes_first_on_its_day_so_a_same_day_buy_is_not_split():
    st = replay([T("SPLIT", D(5), num=2, den=1, tid=1), buy(D(5), 10, 10.0, tid=2)])
    assert st.lots[2].remaining_quantity == 10
    st2 = replay([buy(D(5), 10, 10.0, tid=1), T("SPLIT", D(5), num=2, den=1, tid=2)])
    assert st2.lots[1].remaining_quantity == 10                                         # documented: a split is effective at the start of its day
    st3 = replay([buy(D(4), 10, 10.0, tid=1), T("SPLIT", D(5), num=2, den=1, tid=2)])
    assert st3.lots[1].remaining_quantity == 20


def test_cash_flows_totals_and_employer_match_both_forms():
    st = replay([T("DEPOSIT", D(0), iid=None, amt=1000.0), T("WITHDRAWAL", D(1), iid=None, amt=-100.0), T("DIVIDEND", D(2), amt=12.5),
                 T("INTEREST", D(3), iid=None, amt=1.5), T("FEE", D(4), iid=None, amt=-4.0),
                 T("EMPLOYER_MATCH", D(5), iid=None, amt=50.0), T("EMPLOYER_MATCH", D(6), iid=7, qty=2.5, px=40.0, amt=0.0, tid=77)])
    assert st.cash == pytest.approx(1000 - 100 + 12.5 + 1.5 - 4 + 50)
    assert st.totals == {"deposits": 1000.0, "withdrawals": 100.0, "dividends": 12.5, "interest": 1.5, "employer_match_cash": 50.0, "fees": 4.0, "opening_cash": 0.0}
    assert st.lots[77].unit_cost == 40.0 and st.lots[77].source_type == "EMPLOYER_MATCH" and st.positions()[7]["quantity"] == 2.5


def test_void_removes_both_rows_and_is_validated():
    base = [buy(D(0), 10, 10.0, tid=1), sell(D(5), 4, 12.0, tid=2)]
    st = replay(base + [T("VOID", D(6), iid=None, voids=2, tid=3)])
    assert st.positions()[1]["quantity"] == 10 and st.closures == [] and st.cash == pytest.approx(-100)
    for bad in ([T("VOID", D(6), voids=999, tid=3)], [T("VOID", D(6), voids=2, tid=3), T("VOID", D(7), voids=2, tid=4)],
                [T("VOID", D(6), voids=2, tid=3), T("VOID", D(7), voids=3, tid=4)]):
        with pytest.raises(LedgerError):
            replay(base + bad)
    with pytest.raises(LedgerError):                                                    # voiding the buy a sale depends on
        replay(base + [T("VOID", D(6), voids=1, tid=3)])


def test_replay_is_deterministic_and_independent_of_input_order():
    txns = three_lots() + [sell(D(30), 15, 25.0, method="HIFO", tid=9), T("DEPOSIT", D(2), iid=None, amt=5.0)]
    a, b = replay(txns), replay(list(reversed(txns)))
    assert (a.cash, a.positions(), a.closures) == (b.cash, b.positions(), b.closures) and a.totals == b.totals


# ── service ───────────────────────────────────────────────────────────────────

def _acct(name="Brokerage", *, ledger=True, ctx=ME):
    a = accts.create_account(ctx, broker="manual", display_name=name)
    if ledger:
        repo.update_account(a["account_id"], holdings_source="ledger")
    return a["account_id"]


def _today(back=0):
    return (date.today() - timedelta(days=back)).isoformat()


def test_a_ledger_account_gets_lots_closures_and_cash_from_its_trades():
    a = _acct()
    led.record_cash_transfer(ME, a, "DEPOSIT", 5000, _today(400))
    b1 = led.record_trade(ME, a, "aapl", "BUY", 10, 150, _today(400), fees=1)
    b2 = led.record_trade(ME, a, "AAPL", "BUY", 10, 200, _today(100))
    s = led.record_trade(ME, a, "AAPL", "SELL", 12, 220, _today(5))
    assert s["txn"]["lot_method"] == "FIFO" and s["txn"]["amount"] == pytest.approx(2640.0)
    assert [(r["open_transaction_id"], r["quantity"], r["holding_period"]) for r in s["realized"]] == \
        [(b1["txn"]["txn_id"], 10, "LONG"), (b2["txn"]["txn_id"], 2, "SHORT")]
    lots = led.get_lots(ME, a)
    assert [(l["open_transaction_id"], l["remaining_quantity"]) for l in lots] == [(b2["txn"]["txn_id"], 8)]
    assert len(led.get_lots(ME, a, open_only=False)) == 2 and len(led.get_closures(ME, a)) == 2
    summ = led.get_ledger_summary(ME, a)
    assert summ["cash"] == pytest.approx(5000 - 1501 - 2000 + 2640) and summ["positions"]["AAPL"]["quantity"] == 8
    assert summ["realized_gain"] == pytest.approx(sum(r["realized_gain"] for r in s["realized"]))


def test_derived_tables_equal_a_fresh_replay_and_lot_ids_are_stable():
    a = _acct()
    t1 = led.record_trade(ME, a, "VTI", "BUY", 5, 100, _today(50))["txn"]["txn_id"]
    led.record_trade(ME, a, "VTI", "BUY", 5, 110, _today(40))
    led.record_trade(ME, a, "VTI", "SELL", 3, 120, _today(30))
    led.record_income(ME, a, "DIVIDEND", 4.2, _today(20), ticker="VTI")
    with repo.unit_of_work() as c:
        fresh = replay(ldb.txns_for_replay(c, a))
        stored = {l["open_transaction_id"]: l["remaining_quantity"] for l in ldb.list_lots(c, a, open_only=False)}
    assert stored == {k: v.remaining_quantity for k, v in fresh.lots.items()} and t1 in stored          # same lot key after every rebuild


def test_snapshot_accounts_take_transactions_as_history_only():
    a = _acct("Snap", ledger=False)
    r = led.record_trade(ME, a, "MSFT", "SELL", 100, 400, _today(3))                 # never held in the ledger: allowed, not validated
    assert r["txn"]["txn_id"] and r["realized"] == [] and led.get_lots(ME, a, open_only=False) == []
    summ = led.get_ledger_summary(ME, a)                                              # replaying a history that starts mid-stream cannot balance
    assert summ["holdings_source"] == "snapshot" and "error" in summ and "only 0 held" in summ["error"]


def test_an_invalid_change_writes_nothing():
    a = _acct()
    led.record_trade(ME, a, "VTI", "BUY", 5, 100, _today(10))
    n = len(led.list_transactions(ME, a))
    with pytest.raises(led.InvalidTransaction, match="only 5 held"):
        led.record_trade(ME, a, "VTI", "SELL", 6, 100, _today(2))
    assert len(led.list_transactions(ME, a)) == n and len(led.get_lots(ME, a)) == 1
    with repo.unit_of_work() as c:
        assert [e for e in repo.get_audit_events(limit=50, conn=c) if e["operation"] == "record_sell"] == []


def test_field_validation():
    a = _acct()
    bad = [dict(side="HOLD", quantity=1, price=1, trade_date=_today()), dict(side="BUY", quantity=0, price=1, trade_date=_today()),
           dict(side="BUY", quantity=1, price=-1, trade_date=_today()), dict(side="BUY", quantity=1, price=1, trade_date="nope"),
           dict(side="BUY", quantity=1, price=1, trade_date=(date.today() + timedelta(days=1)).isoformat()),
           dict(side="BUY", quantity=1, price=1, trade_date=_today(), fees=-1), dict(side="BUY", quantity=float("nan"), price=1, trade_date=_today()),
           dict(side="BUY", quantity=1, price=1, trade_date=_today(), lot_method="FIFO")]
    for kw in bad:
        with pytest.raises(led.InvalidTransaction):
            led.record_trade(ME, a, "VTI", **kw)
    for fn, args in ((led.record_cash_transfer, ("SEND", 5, _today())), (led.record_cash_transfer, ("DEPOSIT", 0, _today())),
                     (led.record_income, ("BONUS", 5, _today())), (led.record_fee, (-3, _today()))):
        with pytest.raises(led.InvalidTransaction):
            fn(ME, a, *args)
    assert led.list_transactions(ME, a) == []


def test_lot_method_precedence_is_sale_then_account_then_default_and_is_stored_on_the_sale():
    a = _acct()
    for px, day in ((10, 90), (30, 80), (20, 70)):
        led.record_trade(ME, a, "VTI", "BUY", 10, px, _today(day))
    s1 = led.record_trade(ME, a, "VTI", "SELL", 1, 25, _today(5))                           # default FIFO
    assert s1["txn"]["lot_method"] == "FIFO"
    led.set_account_lot_method(ME, a, "HIFO")
    s2 = led.record_trade(ME, a, "VTI", "SELL", 1, 25, _today(4))
    assert s2["txn"]["lot_method"] == "HIFO" and s2["realized"][0]["cost_basis"] == 30.0
    s3 = led.record_trade(ME, a, "VTI", "SELL", 1, 25, _today(3), lot_method="LIFO")          # the sale's own choice wins
    assert s3["txn"]["lot_method"] == "LIFO" and s3["realized"][0]["cost_basis"] == 20.0
    # changing the setting later does not rewrite what was already recorded
    led.set_account_lot_method(ME, a, "LIFO")
    assert {t["txn_id"]: t["lot_method"] for t in led.list_transactions(ME, a) if t["txn_type"] == "SELL"} == \
        {s1["txn"]["txn_id"]: "FIFO", s2["txn"]["txn_id"]: "HIFO", s3["txn"]["txn_id"]: "LIFO"}
    led.set_account_lot_method(ME, a, None)                                                  # back to the portfolio default
    auth_users_db.invite_user(f"{ME.actor}@example.com", ME.actor, "admin")
    inv.update_portfolio_settings(ME, default_lot_method="LIFO")
    assert led.record_trade(ME, a, "VTI", "SELL", 1, 25, _today(2))["txn"]["lot_method"] == "LIFO"
    for bad in ("SPECIFIC", "AVERAGE", "x"):
        with pytest.raises(led.InvalidTransaction):
            led.set_account_lot_method(ME, a, bad)
        with pytest.raises(ValueError):
            inv.update_portfolio_settings(ME, default_lot_method=bad)
    assert inv.reset_portfolio_settings(ME)["default_lot_method"] == "FIFO"
    assert sdb.get_portfolio_settings()["default_lot_method"] == "FIFO"


def test_specific_lot_sale_through_the_service():
    a = _acct()
    b1 = led.record_trade(ME, a, "VTI", "BUY", 10, 10, _today(60))["txn"]["txn_id"]
    b2 = led.record_trade(ME, a, "VTI", "BUY", 10, 30, _today(50))["txn"]["txn_id"]
    s = led.record_trade(ME, a, "VTI", "SELL", 4, 40, _today(1), lot_selection=[{"lot": b2, "quantity": 4}])
    assert s["txn"]["lot_method"] == "SPECIFIC" and s["txn"]["lot_selection"] == [{"lot": b2, "quantity": 4}]
    assert s["realized"][0]["open_transaction_id"] == b2 and s["realized"][0]["realized_gain"] == pytest.approx(40.0)
    with pytest.raises(led.InvalidTransaction):
        led.record_trade(ME, a, "VTI", "SELL", 4, 40, _today(1), lot_method="SPECIFIC")        # names no lots
    with pytest.raises(led.InvalidTransaction):
        led.record_trade(ME, a, "VTI", "SELL", 4, 40, _today(1), lot_selection=[{"lot": b1, "quantity": 99}])


def test_the_ledger_is_append_only_in_the_database_itself():
    a = _acct()
    t = led.record_cash_transfer(ME, a, "DEPOSIT", 100, _today())["txn"]["txn_id"]
    with sqlite3.connect(db_module.DB_PATH) as c:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            c.execute("UPDATE transactions SET amount = 1 WHERE txn_id = ?", [t])
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            c.execute("DELETE FROM transactions WHERE txn_id = ?", [t])
    assert led.list_transactions(ME, a)[0]["amount"] == 100


def test_void_cancels_by_appending_and_keeps_the_original():
    a = _acct()
    b = led.record_trade(ME, a, "VTI", "BUY", 10, 10, _today(9))["txn"]["txn_id"]
    s = led.record_trade(ME, a, "VTI", "SELL", 4, 12, _today(3))["txn"]["txn_id"]
    with pytest.raises(led.InvalidTransaction, match="reason"):
        led.void_transaction(ME, s, reason="  ")
    v = led.void_transaction(ME, s, reason="entered twice")
    assert v["txn"]["txn_type"] == "VOID" and v["txn"]["voids_txn_id"] == s and v["voided"]["txn_id"] == s
    rows = {r["txn_id"]: r for r in led.list_transactions(ME, a)}
    assert rows[s]["voided_by"] == v["txn"]["txn_id"] and rows[s]["txn_type"] == "SELL"            # still there
    assert [r["txn_id"] for r in led.list_transactions(ME, a, include_voided=False)] == [b]
    assert led.get_lots(ME, a)[0]["remaining_quantity"] == 10 and led.get_closures(ME, a) == []
    with pytest.raises(led.InvalidTransaction, match="already voided"):
        led.void_transaction(ME, s, reason="again")
    with pytest.raises(led.InvalidTransaction, match="cannot be voided"):
        led.void_transaction(ME, v["txn"]["txn_id"], reason="undo the void")
    with pytest.raises(led.NotFound):
        led.void_transaction(ME, 99999, reason="x")
    # voiding the buy a (re-recorded) sale depends on is refused and changes nothing
    led.record_trade(ME, a, "VTI", "SELL", 4, 12, _today(2))
    n = len(led.list_transactions(ME, a))
    with pytest.raises(led.InvalidTransaction):
        led.void_transaction(ME, b, reason="wrong")
    assert len(led.list_transactions(ME, a)) == n


def test_external_id_makes_a_write_idempotent():
    a = _acct()
    r1 = led.record_trade(ME, a, "VTI", "BUY", 5, 100, _today(2), external_id="brk-001")
    r2 = led.record_trade(ME, a, "VTI", "BUY", 5, 100, _today(2), external_id="brk-001")
    assert r2["duplicate"] is True and r2["txn"]["txn_id"] == r1["txn"]["txn_id"]
    assert len(led.list_transactions(ME, a)) == 1 and led.get_ledger_summary(ME, a)["positions"]["VTI"]["quantity"] == 5


def test_income_fee_match_and_split_verbs():
    a, b = _acct("A"), _acct("B")
    led.record_cash_transfer(ME, a, "DEPOSIT", 1000, _today(30))
    led.record_income(ME, a, "DIVIDEND", 12.34, _today(20), ticker="VTI")
    led.record_income(ME, a, "INTEREST", 1.0, _today(19))
    led.record_fee(ME, a, 2.5, _today(18), memo="custody")
    led.record_employer_match(ME, a, _today(17), amount=100)
    led.record_employer_match(ME, a, _today(16), ticker="TDF", quantity=3.5, price=40)
    s = led.get_ledger_summary(ME, a)
    assert s["cash"] == pytest.approx(1000 + 12.34 + 1 - 2.5 + 100) and s["positions"]["TDF"]["quantity"] == 3.5
    for kw in (dict(amount=5, ticker="T", quantity=1, price=1), dict(), dict(ticker="T", quantity=1)):
        with pytest.raises(led.InvalidTransaction):
            led.record_employer_match(ME, a, _today(1), **kw)
    # a split lands in every account of the owner that held the ticker on the date, and nowhere else
    led.record_trade(ME, a, "NVDA", "BUY", 10, 100, _today(10))
    led.record_trade(ME, b, "NVDA", "BUY", 4, 100, _today(9))
    out = led.record_split(ME, "NVDA", 10, 1, _today(5), external_id="nvda-10for1")
    assert len(out) == 2
    assert led.get_ledger_summary(ME, a)["positions"]["NVDA"] == {"quantity": 100, "cost_basis": 1000.0, "avg_cost": 10.0}
    assert led.get_ledger_summary(ME, b)["positions"]["NVDA"]["quantity"] == 40
    assert len(led.record_split(ME, "NVDA", 10, 1, _today(5), external_id="nvda-10for1")) == 2 and \
        len(led.list_transactions(ME, a)) == len(led.list_transactions(ME, a))                        # idempotent per account
    assert sum(t["txn_type"] == "SPLIT" for t in led.list_transactions(ME, a)) == 1
    with pytest.raises(led.InvalidTransaction):
        led.record_split(ME, "ZZZZ", 2, 1, _today(1))                                              # nobody held it


def test_only_the_owner_can_touch_an_account_and_every_write_is_audited():
    a = _acct()
    other = RequestContext(identity=_mint_identity_for_tests("someone-else"), source="test")
    for call in (lambda: led.record_trade(other, a, "VTI", "BUY", 1, 1, _today()), lambda: led.list_transactions(other, a),
                 lambda: led.record_cash_transfer(other, a, "DEPOSIT", 1, _today()), lambda: led.get_lots(other, a),
                 lambda: led.set_account_lot_method(other, a, "LIFO")):
        with pytest.raises((led.NotAuthorized, led.NotFound)):
            call()
    t = led.record_trade(ME, a, "VTI", "BUY", 1, 1, _today())["txn"]["txn_id"]
    led.void_transaction(ME, t, reason="oops")
    ops = [e["operation"] for e in repo.get_audit_events(limit=50, account_id=a)]
    assert "record_buy" in ops and "void_transaction" in ops
    ev = next(e for e in repo.get_audit_events(limit=50, account_id=a) if e["operation"] == "record_buy")
    assert ev["actor"] == ME.actor and ev["after"]["ticker"] == "VTI"


def test_an_account_with_transactions_cannot_be_hard_deleted():
    a = _acct()
    led.record_cash_transfer(ME, a, "DEPOSIT", 10, _today())
    with pytest.raises(ValueError, match="archive it"):
        accts.delete_account_permanently(ME, a)
    assert accts.archive_account(ME, a)["status"] != "active"                                     # archiving is the way out
    assert len(led.list_transactions(ME, a)) == 1
    empty = _acct("Empty")
    accts.delete_account_permanently(ME, empty)                                                    # no ledger: deletes as before


def test_migration_adds_the_ledger_to_an_existing_database_idempotently():
    a = _acct()
    led.record_cash_transfer(ME, a, "DEPOSIT", 10, _today())
    with sqlite3.connect(db_module.DB_PATH) as c:
        c.execute("DROP TABLE lot_closures"); c.execute("DROP TABLE tax_lots")                    # derived tables are disposable
    repo.get_account(a)                                                                           # any connection re-creates them
    for _ in range(2):
        repo.get_account(a)
    with sqlite3.connect(db_module.DB_PATH) as c:
        names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'trigger')")}
        assert {"transactions", "tax_lots", "lot_closures", "trg_transactions_no_update", "trg_transactions_no_delete"} <= names
        assert "lot_method" in [r[1] for r in c.execute("PRAGMA table_info(accounts)")]
    assert len(led.list_transactions(ME, a)) == 1                                                  # the ledger itself was never touched
