"""(1) A position opened on the ledger without a known cost basis stays UNKNOWN (None, shown as "unknown"), never a cost of 0 that makes the whole
value look like profit. (2) Past activity from a broker history file is saved on the side and summarised (dividends, interest, deposits, sales),
combined with the ledger's own transactions, without touching positions, lots or cash."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.services import account_service as accts
from portfolio_agent.services import ledger_service as led
from portfolio_agent.services.account_service import NotAuthorized, NotFound
from portfolio_agent.services.context import RequestContext, _mint_identity_for_tests, local_context
from portfolio_agent.tools import holdings_db as repo
from portfolio_agent.tools import ledger_db as ldb
from tests.test_ledger_fidelity_import import FIDELITY, TODAY
from web.data import ledger as ld
from web.data.portfolio import summarize_holdings

ME = local_context("test")
day = lambda back=0: (date.today() - timedelta(days=back)).isoformat()


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(led, "_today", lambda: TODAY)


def snapshot(positions, cash=100.0, name="Snap"):
    a = accts.create_account(ME, broker="manual", display_name=name)["account_id"]
    for t, sh, basis, px in positions:
        accts.add_position(ME, a, {"ticker": t, "shares": sh, "cost_basis_total": basis, "avg_cost": (basis / sh) if basis else None,
                                   "current_price": px, "sector": "Tech"})
    if cash is not None:
        accts.record_account_cash(ME, a, cash, as_of_date=day(1))
    return a


def pos(a):
    return {p["ticker"]: p for p in repo.list_positions(a)}


# ── (1) unknown basis stays unknown ───────────────────────────────────────────

def test_a_position_opened_without_a_basis_has_no_cost_basis_not_a_zero_one():
    a = snapshot([("AAA", 10, None, 50.0), ("BBB", 4, 400.0, 150.0)])
    led.switch_to_ledger(ME, a, day(1), allow_unknown_basis=True)
    p = pos(a)
    assert p["AAA"]["cost_basis_total"] is None and p["AAA"]["avg_cost"] is None            # unknown, not 0
    assert p["BBB"]["cost_basis_total"] == 400.0 and p["BBB"]["avg_cost"] == 100.0           # a known one is untouched
    s = led.get_ledger_summary(ME, a)["positions"]
    assert s["AAA"]["basis_unknown"] is True and s["AAA"]["cost_basis"] is None and s["AAA"]["quantity"] == 10
    assert "basis_unknown" not in s["BBB"] and s["BBB"]["cost_basis"] == 400.0


def test_unrealized_gain_leaves_out_the_unknown_position_instead_of_counting_all_of_it_as_profit():
    a = snapshot([("AAA", 10, None, 50.0), ("BBB", 4, 400.0, 150.0)])
    led.switch_to_ledger(ME, a, day(1), allow_unknown_basis=True)
    s = summarize_holdings(accts.list_holdings_for(ME), accts.get_cash_balances_for(ME))
    assert s["unrealized"] == pytest.approx(4 * 150.0 - 400.0)                                # BBB only: 200, not 200 + 500


def test_lots_show_unknown_and_a_sale_of_unknown_shares_has_no_gain_and_is_reported_separately():
    a = snapshot([("AAA", 10, None, 50.0)])
    led.switch_to_ledger(ME, a, day(1), allow_unknown_basis=True)
    lots = ld.lots_view(ME, a)
    assert lots[0]["basis_unknown"] is True and lots[0]["unit_cost"] is None and lots[0]["cost_basis"] is None
    led.record_trade(ME, a, "AAA", "SELL", 4, 60.0, day(0))
    g = ld.realized_gains_view(ME, a)
    assert g["rows"][0]["basis_unknown"] is True and g["rows"][0]["gain"] is None and g["rows"][0]["cost_basis"] is None
    assert g["totals"] == {"short_term": 0.0, "long_term": 0.0, "total": 0.0, "unknown_basis_sales": 1, "unknown_basis_proceeds": 240.0}
    s = led.get_ledger_summary(ME, a)
    assert s["realized_gain"] == 0.0 and s["realized_unknown_basis_proceeds"] == 240.0


def test_buying_more_of_an_unknown_position_adds_a_known_lot_but_the_position_as_a_whole_stays_unknown():
    a = snapshot([("AAA", 10, None, 50.0)])
    led.switch_to_ledger(ME, a, day(1), allow_unknown_basis=True)
    led.record_trade(ME, a, "AAA", "BUY", 5, 40.0, day(0))
    lots = {l["quantity"]: l for l in ld.lots_view(ME, a)}
    assert lots[10]["basis_unknown"] is True and lots[5]["basis_unknown"] is False and lots[5]["cost_basis"] == 200.0
    assert pos(a)["AAA"]["cost_basis_total"] is None and pos(a)["AAA"]["shares"] == 15


def test_a_basis_given_up_front_is_kept_exactly():
    a = snapshot([("AAA", 10, None, 50.0)])
    led.switch_to_ledger(ME, a, day(1), basis_overrides={"AAA": 12.5})
    assert pos(a)["AAA"]["cost_basis_total"] == 125.0 and pos(a)["AAA"]["avg_cost"] == 12.5
    assert ld.lots_view(ME, a)[0]["basis_unknown"] is False


# ── (2) saved history and the activity summary ────────────────────────────────

def ledger_account(start):
    a = accts.create_account(ME, broker="manual", display_name="Fidelity")["account_id"]
    repo.update_account(a, holdings_source="ledger", ledger_start_date=start)
    return a


def mapping():
    return led.suggest_csv_mapping(FIDELITY)["mapping"]


def test_history_rows_up_to_the_ledger_start_are_saved_and_a_second_upload_adds_nothing():
    a = ledger_account("2026-10-21")
    pv = led.preview_history_csv(ME, a, FIDELITY, mapping())
    assert pv["summary"] == {"new": 7, "duplicate": 0, "skipped": 1, "later": 0, "invalid": 0, "rows": 8} and pv["can_save"] is True
    out = led.import_history_csv(ME, a, FIDELITY, mapping(), filename="History.csv")
    assert (out["saved"], out["duplicates"], out["skipped"]) == (7, 0, 1)
    again = led.import_history_csv(ME, a, FIDELITY, mapping(), filename="History.csv")
    assert (again["saved"], again["duplicates"]) == (0, 7)
    with repo.unit_of_work() as c:
        assert len(ldb.list_history(c, a)) == 7
        assert c.execute("SELECT COUNT(*) FROM transactions WHERE account_id = ?", [a]).fetchone()[0] == 0     # nothing went into the ledger



def test_saving_history_never_touches_positions_lots_or_cash():
    a = snapshot([("AAA", 10, 100.0, 50.0)], cash=250.0)
    led.switch_to_ledger(ME, a, day(1))
    before = ({t: (p["shares"], p["cost_basis_total"]) for t, p in pos(a).items()}, led.get_ledger_summary(ME, a)["cash"], len(ld.lots_view(ME, a)))
    led.import_history_csv(ME, a, FIDELITY, mapping(), filename="History.csv")
    after = ({t: (p["shares"], p["cost_basis_total"]) for t, p in pos(a).items()}, led.get_ledger_summary(ME, a)["cash"], len(ld.lots_view(ME, a)))
    assert before == after


def test_rows_dated_after_the_ledger_start_are_left_for_the_transaction_import():
    a = ledger_account("2026-10-01")
    pv = led.preview_history_csv(ME, a, FIDELITY, mapping())
    assert pv["summary"]["later"] == 3 and pv["summary"]["new"] == 4                          # 10/02 x2 and 10/16 are after the start
    assert all("transactions instead" in r["reason"] for r in pv["rows"] if r["status"] == "later")
    out = led.import_history_csv(ME, a, FIDELITY, mapping())
    assert out["saved"] == 4 and out["later"] == 3


def test_a_snapshot_account_takes_every_row_as_history():
    a = accts.create_account(ME, broker="manual", display_name="Snap")["account_id"]
    assert led.import_history_csv(ME, a, FIDELITY, mapping())["saved"] == 7


def test_the_activity_summary_adds_up_the_file_by_kind():
    a = ledger_account("2026-10-21")
    led.import_history_csv(ME, a, FIDELITY, mapping(), filename="History.csv")
    s = led.get_activity_summary(ME, a, year=2026)
    assert s["years"] == [2026] and (s["from_history"], s["from_ledger"]) == (7, 0)
    t = s["totals"]
    assert (t["dividends"], t["interest"], t["deposits"], t["withdrawals"], t["fees"]) == (18.5, 0.42, 9843.55, 0.0, 0.0)
    assert (t["sale_proceeds"], t["sale_count"], t["shares_sold"]) == (5056.69, 1, 20.0)
    assert (t["purchases"], t["purchase_count"]) == (9160.29, 2)
    assert s["deposit_sources"] == {"Transfer": 5000.0, "Espp": 4843.55} and s["dividends_by_ticker"] == {"NVDA": 18.5}
    assert s["sales"] == [{"date": "2026-10-16", "ticker": "AJG", "quantity": 20.0, "proceeds": 5056.69}]
    assert led.get_activity_summary(ME, a, year=2025)["totals"]["dividends"] == 0.0


def test_the_summary_combines_saved_history_with_the_ledgers_own_transactions():
    a = ledger_account("2026-10-21")
    led.import_history_csv(ME, a, FIDELITY, mapping())
    led.record_cash_transfer(ME, a, "DEPOSIT", 1000.0, "2026-10-22", contribution_source="employee")
    led.record_income(ME, a, "INTEREST", 3.0, "2026-10-22")
    s = led.get_activity_summary(ME, a, year=2026)
    assert (s["from_history"], s["from_ledger"]) == (7, 2)
    assert s["totals"]["deposits"] == 10843.55 and s["totals"]["interest"] == 3.42
    assert ld.activity_view(ME, a, 2026)["totals"] == s["totals"]


def test_a_voided_ledger_transaction_is_not_counted_and_opening_balances_are_not_activity():
    a = snapshot([("AAA", 10, 100.0, 50.0)], cash=250.0)
    led.switch_to_ledger(ME, a, "2026-10-01")
    t = led.record_cash_transfer(ME, a, "DEPOSIT", 500.0, "2026-10-02")["txn"]
    led.void_transaction(ME, t["txn_id"], reason="typo")
    s = led.get_activity_summary(ME, a, year=2026)
    assert s["totals"]["deposits"] == 0.0 and s["from_ledger"] == 0


def test_the_all_accounts_summary_has_a_line_per_account_and_is_owner_scoped():
    a = ledger_account("2026-10-21")
    led.import_history_csv(ME, a, FIDELITY, mapping())
    b = accts.create_account(ME, broker="manual", display_name="Empty")["account_id"]
    s = led.get_activity_summary_all(ME, year=2026)
    assert {x["account_id"] for x in s["accounts"]} == {a, b} and s["totals"]["dividends"] == 18.5
    assert next(x for x in s["accounts"] if x["account_id"] == a)["events"] == 7 and next(x for x in s["accounts"] if x["account_id"] == b)["events"] == 0
    other = RequestContext(identity=_mint_identity_for_tests("someone-else"), source="t")
    with pytest.raises((NotAuthorized, NotFound)):
        led.get_activity_summary(other, a)
    with pytest.raises((NotAuthorized, NotFound)):
        led.import_history_csv(other, a, FIDELITY, mapping())
    with pytest.raises((NotAuthorized, NotFound)):
        led.clear_history(other, a)


def test_an_invalid_row_stops_the_whole_save_and_clearing_removes_only_history():
    a = ledger_account("2026-10-21")
    bad = FIDELITY.replace(b"Electronic Funds Transfer Received (Cash)", b"TRANSFERRED FROM TO BROKERAGE (Cash)")
    with pytest.raises(led.ImportRejected, match="TRANSFERRED FROM TO BROKERAGE"):
        led.import_history_csv(ME, a, bad, mapping())
    with repo.unit_of_work() as c:
        assert ldb.list_history(c, a) == []
    led.import_history_csv(ME, a, FIDELITY, mapping())
    assert led.clear_history(ME, a, reason="test") == 7 and led.get_activity_summary(ME, a)["years"] == []
    ops = [e["operation"] for e in repo.get_audit_events(limit=50, account_id=a)]
    assert "import_history_csv" in ops and "clear_history" in ops


def test_identical_rows_in_one_file_are_both_kept_and_a_longer_file_adds_only_the_new_ones():
    a = accts.create_account(ME, broker="manual", display_name="Snap")["account_id"]
    two = b"date,type,ticker,amount\n2026-03-01,dividend,AAA,10\n2026-03-01,dividend,AAA,10\n"
    m = led.suggest_csv_mapping(two)["mapping"]
    assert led.import_history_csv(ME, a, two, m)["saved"] == 2
    three = two + b"2026-03-01,dividend,AAA,10\n"
    longer = led.import_history_csv(ME, a, three, m)
    assert (longer["saved"], longer["duplicates"]) == (1, 2)                                  # only the third identical row is new
    assert led.import_history_csv(ME, a, three, m)["saved"] == 0
    assert led.get_activity_summary(ME, a, year=2026)["totals"]["dividends"] == 30.0
