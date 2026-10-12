"""Step 3.4: the Accounts page's ledger panel. Page-data functions (web/data/ledger.py) are tested directly, in the style of
test_validation_page_data.py, with owner scoping checked on every read; the panel itself is driven with Streamlit's AppTest."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.services import account_service as accts
from portfolio_agent.services import ledger_service as led
from portfolio_agent.services.context import RequestContext, _mint_identity_for_tests, local_context
from portfolio_agent.tools import holdings_db as repo
from web.data import ledger as ld

ME = local_context("test")
day = lambda back=0: (date.today() - timedelta(days=back)).isoformat()


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    for name in ("APEX_SMTP_USERNAME", "APEX_SMTP_PASSWORD", "APEX_NTFY_TOKEN", "APEX_NOTIFY_SIGNING_KEY"):
        monkeypatch.delenv(name, raising=False)


def other_ctx(name="someone-else"):
    with repo.unit_of_work() as c:
        c.execute("INSERT OR IGNORE INTO portfolios (owner) VALUES (?)", (name,))
    return RequestContext(identity=_mint_identity_for_tests(name), source="test")


def ledger_account(name="L"):
    a = accts.create_account(ME, broker="manual", display_name=name)["account_id"]
    repo.update_account(a, holdings_source="ledger")
    return a


def busy_account():
    a = ledger_account()
    led.record_cash_transfer(ME, a, "DEPOSIT", 5000, day(400), contribution_source="employee")
    led.record_trade(ME, a, "VTI", "BUY", 10, 100, day(380))
    led.record_trade(ME, a, "VTI", "BUY", 10, 120, day(30))
    led.record_trade(ME, a, "VTI", "SELL", 12, 150, day(5))
    bad = led.record_trade(ME, a, "AAPL", "BUY", 1, 10, day(4))["txn"]["txn_id"]
    led.void_transaction(ME, bad, reason="wrong ticker")
    led.record_income(ME, a, "DIVIDEND", 12.5, day(3), ticker="VTI")
    return a


# ── transactions list ─────────────────────────────────────────────────────────

def test_transactions_view_keeps_voided_rows_struck_and_sorts_newest_first():
    a = busy_account()
    rows = ld.transactions_view(ME, a)
    assert [r["date"] for r in rows] == sorted((r["date"] for r in rows), reverse=True)
    voided = [r for r in rows if r["voided"]]
    assert len(voided) == 1 and voided[0]["ticker"] == "AAPL" and voided[0]["struck"] and not voided[0]["can_void"]
    void_row = next(r for r in rows if r["is_void_row"])
    assert void_row["struck"] and void_row["voids"] == voided[0]["txn_id"] and voided[0]["voided_by"] == void_row["txn_id"]
    assert all(not r["struck"] for r in rows if r["type"] in ("DEPOSIT", "SELL"))
    assert not any(r["struck"] for r in ld.transactions_view(ME, a, show_voided=False)) and len(ld.transactions_view(ME, a, show_voided=False)) == 5


def test_transaction_filters():
    a = busy_account()
    assert {r["type"] for r in ld.transactions_view(ME, a, types=["BUY"])} == {"BUY"}
    assert {r["ticker"] for r in ld.transactions_view(ME, a, ticker=" vt ")} == {"VTI"}
    assert [r["type"] for r in ld.transactions_view(ME, a, date_from=day(10), date_to=day(2), show_voided=False)] == ["DIVIDEND", "SELL"]
    assert {r["clearance"]["status"] for r in ld.transactions_view(ME, a, clearance=["missing"])} == {"missing"}
    assert ld.transactions_view(ME, a, clearance=["approved"]) == []
    sp = ld.transactions_view(ME, a, types=["DEPOSIT"])[0]
    assert sp["contribution_source"] == "EMPLOYEE" and sp["clearance"]["label"] == ""


def test_clearance_badges_say_what_they_mean():
    assert ld.clearance_badge("approved")["tone"] == "good" and ld.clearance_badge("missing") == {"status": "missing", "label": "No approval", "tone": "bad"}
    assert ld.clearance_badge("unknown")["tone"] == "warn" and ld.clearance_badge("not_required")["label"] == "Not required"
    assert ld.clearance_badge(None)["label"] == ""
    a = busy_account()
    s = ld.clearance_summary(ME, a)
    assert s["missing"] == 3 and s["needs_attention"] is True                            # two buys and the sale; the voided buy is not counted


def test_voidable_choices_exclude_voided_and_void_rows():
    a = busy_account()
    ids = [c["txn_id"] for c in ld.voidable_choices(ME, a)]
    rows = {r["txn_id"]: r for r in ld.transactions_view(ME, a)}
    assert ids and all(rows[i]["can_void"] for i in ids) and not any(rows[i]["voided"] or rows[i]["is_void_row"] for i in ids)
    assert ld.voidable_choices(ME, a)[0]["label"].startswith("#")
    ld.void(ME, ids[0], "test")
    assert ids[0] not in [c["txn_id"] for c in ld.voidable_choices(ME, a)]
    with pytest.raises(led.InvalidTransaction, match="reason"):
        ld.void(ME, ids[1], "")


# ── lots, gains, overview ─────────────────────────────────────────────────────

def test_lots_and_realized_gains_views():
    a = busy_account()
    lots = ld.lots_view(ME, a)
    assert [(l["ticker"], l["quantity"], l["unit_cost"]) for l in lots] == [("VTI", 8, 120.0)]
    g = ld.realized_gains_view(ME, a)
    assert [(r["quantity"], r["period"]) for r in g["rows"]] == [(10, "LONG"), (2, "SHORT")]
    assert g["totals"] == {"short_term": 60.0, "long_term": 500.0, "total": 560.0} and g["years"] == [date.today().year]
    assert ld.realized_gains_view(ME, a, year=1999) == {"years": g["years"], "year": 1999, "rows": [],
                                                       "totals": {"short_term": 0.0, "long_term": 0.0, "total": 0.0}}


def test_overview_for_ledger_and_snapshot_accounts():
    a = busy_account()
    o = ld.ledger_overview(ME, a)
    assert o["is_ledger"] and o["holdings_source"] == "ledger" and o["effective_lot_method"] == "FIFO" and o["lot_method"] is None
    assert o["summary"]["positions"]["VTI"]["quantity"] == 8 and o["summary"]["realized_gain"] == 560.0
    assert o["transactions"] == 6 and o["voided"] == 1 and o["clearance"]["missing"] == 3
    led.set_account_lot_method(ME, a, "HIFO")
    assert ld.ledger_overview(ME, a)["effective_lot_method"] == "HIFO"
    snap = accts.create_account(ME, broker="manual", display_name="S")["account_id"]
    so = ld.ledger_overview(ME, snap)
    assert so["is_ledger"] is False and so["holdings_source"] == "snapshot" and ld.lots_view(ME, snap) == []


# ── switching ─────────────────────────────────────────────────────────────────

def test_switch_preview_shows_the_opening_balances_writes_nothing_and_flags_missing_basis():
    s = accts.create_account(ME, broker="manual", display_name="Snap")["account_id"]
    accts.add_position(ME, s, {"ticker": "VTI", "shares": 10, "cost_basis_total": 2000.0, "current_price": 250.0})
    accts.add_position(ME, s, {"ticker": "XYZ", "shares": 4, "current_price": 5.0})
    accts.record_account_cash(ME, s, 300.0, as_of_date=day(1))
    pv = ld.switch_preview(ME, s, day(1))
    assert pv["can_switch"] is False and pv["needs_basis"] == ["XYZ"] and [p["ticker"] for p in pv["positions"]] == ["VTI"] and pv["cash"] == 300.0
    ok = ld.switch_preview(ME, s, day(1), basis_overrides={"xyz": 5.5})
    assert ok["can_switch"] and {p["ticker"]: (p["unit_cost"], p["cost_basis"]) for p in ok["positions"]} == {"VTI": (200.0, 2000.0), "XYZ": (5.5, 22.0)}
    flagged = ld.switch_preview(ME, s, day(1), allow_unknown_basis=True)
    assert flagged["unknown_basis"] == ["XYZ"] and flagged["can_switch"]
    assert led.list_transactions(ME, s) == [] and repo.get_account(s)["holdings_source"] == "snapshot"           # nothing was written
    with pytest.raises(led.InvalidTransaction, match="already"):
        ld.switch_preview(ME, busy_account(), day(1))


# ── reconciliation ────────────────────────────────────────────────────────────

def test_statement_text_parsing():
    p = ld.parse_statement_text("# my statement\nvti, 25.5\nBND 10\nQQQ\t3\t$1,200.00\nCASH, 1200.50\n\nbad\nXYZ, abc")
    assert p["positions"] == [{"ticker": "VTI", "shares": 25.5}, {"ticker": "BND", "shares": 10.0}, {"ticker": "QQQ", "shares": 3.0, "cost_basis_total": 1200.0}]
    assert p["cash"] == 1200.5 and len(p["errors"]) == 2 and "line 7" in p["errors"][0] and "line 8" in p["errors"][1]


def test_reconciliation_view_adds_tones_reports_parse_errors_and_changes_nothing():
    a = busy_account()
    before = len(led.list_transactions(ME, a))
    out = ld.reconciliation_view(ME, a, "VTI, 9\nNVDA, 2")
    by = {p["ticker"]: p for p in out["result"]["positions"]}
    assert by["VTI"]["tone"] == "bad" and by["VTI"]["difference"] == 1 and by["NVDA"]["status"] == "ONLY_IN_STATEMENT" and by["NVDA"]["tone"] == "warn"
    assert out["result"]["matches"] is False
    assert ld.reconciliation_view(ME, a, "VTI, 8")["result"]["matches"] is True
    bad = ld.reconciliation_view(ME, a, "VTI")
    assert bad["result"] is None and bad["errors"]
    assert len(led.list_transactions(ME, a)) == before


# ── the add-transaction form ──────────────────────────────────────────────────

def test_form_values_become_the_right_ledger_calls():
    a = ledger_account()
    f = lambda **k: {"date": day(2), **k}
    ld.submit_transaction(ME, a, "DEPOSIT", f(amount=1000.0, contribution_source="rollover", memo="  from old plan "))
    ld.submit_transaction(ME, a, "BUY", f(ticker="vti", quantity=5.0, price=100.0, fees=1.0))
    ld.submit_transaction(ME, a, "EMPLOYER_MATCH", f(amount=50.0))
    ld.submit_transaction(ME, a, "EMPLOYER_MATCH", f(ticker="TDF", quantity=2.0, price=40.0))
    ld.submit_transaction(ME, a, "DIVIDEND", f(ticker="VTI", amount=3.0))
    ld.submit_transaction(ME, a, "INTEREST", f(amount=1.0))
    ld.submit_transaction(ME, a, "FEE", f(amount=2.0))
    r = ld.submit_transaction(ME, a, "SELL", f(ticker="VTI", quantity=1.0, price=110.0, lot_method="LIFO"))
    assert r["txn"]["lot_method"] == "LIFO" and r["realized"][0]["realized_gain"] == pytest.approx(110 - 100.2)
    ld.submit_transaction(ME, a, "SPLIT", {"date": day(1), "ticker": "VTI", "numerator": 2.0, "denominator": 1.0})        # a split is effective at the start of its day
    rows = {t["txn_type"] + (t["ticker"] or ""): t for t in led.list_transactions(ME, a)}
    assert rows["DEPOSIT"]["contribution_source"] == "ROLLOVER" and rows["DEPOSIT"]["memo"] == "from old plan"
    assert rows["EMPLOYER_MATCH"]["contribution_source"] == "EMPLOYER" and rows["SPLITVTI"]["split_numerator"] == 2
    assert led.get_ledger_summary(ME, a)["positions"]["VTI"]["quantity"] == 8                       # (5 - 1) x 2
    for kind, vals, msg in (("BUY", {"quantity": 1.0, "price": 1.0}, "ticker is required"), ("BUY", {"ticker": "X", "price": 1.0}, "quantity is required"),
                            ("DEPOSIT", {}, "amount is required"), ("DIVIDEND", {"amount": 1.0}, "ticker is required"),
                            ("SPLIT", {"ticker": "VTI", "numerator": 2.0}, "denominator is required"), ("TRANSFER", {}, "unknown")):
        with pytest.raises(ld.FormError, match=msg):
            ld.submit_transaction(ME, a, kind, f(**vals))
    with pytest.raises(led.InvalidTransaction, match="only 4 held"):                               # the ledger's own refusals pass through
        ld.submit_transaction(ME, a, "SELL", f(ticker="VTI", quantity=99.0, price=1.0))


# ── import / export through the page layer ────────────────────────────────────

def test_import_and_export_functions():
    a = ledger_account()
    raw = b"date,type,ticker,quantity,price,amount\n2026-01-05,buy,VTI,5,100,\n2026-01-06,deposit,,,,50\n"
    sug = ld.import_suggestion(raw)
    assert sug["mapping"]["date"] == "date" and sug["rows"] == 2
    assert ld.import_preview(ME, a, raw, sug["mapping"])["can_import"] is True
    assert ld.import_commit(ME, a, raw, sug["mapping"], "jan.csv")["imported"] == 2
    files = ld.export_files(ME, a)
    assert files["transactions"][0].startswith("l-transactions-") and files["transactions"][1].startswith("txn_id,trade_date")
    assert "VTI" in files["transactions"][1] and files["realized_gains"][0].startswith("l-realized-gains-")
    assert ld.export_files(ME, a, year=2026)["realized_gains"][0].startswith("l-realized-gains-2026-")


# ── owner scoping on EVERY read and write ─────────────────────────────────────

def test_every_page_function_refuses_another_owners_account():
    a = busy_account()
    other = other_ctx()
    tid = ld.voidable_choices(ME, a)[0]["txn_id"]
    raw = b"date,type,ticker,quantity,price,amount\n2026-01-06,deposit,,,,50\n"
    mapping = ld.import_suggestion(raw)["mapping"]
    calls = [lambda: ld.ledger_overview(other, a), lambda: ld.transactions_view(other, a), lambda: ld.voidable_choices(other, a),
             lambda: ld.lots_view(other, a), lambda: ld.realized_gains_view(other, a), lambda: ld.clearance_summary(other, a),
             lambda: ld.switch_preview(other, a, day(1)), lambda: ld.reconciliation_view(other, a, "VTI, 1"),
             lambda: ld.submit_transaction(other, a, "DEPOSIT", {"amount": 1.0}), lambda: ld.void(other, tid, "x"),
             lambda: ld.import_preview(other, a, raw, mapping), lambda: ld.import_commit(other, a, raw, mapping, "f.csv"),
             lambda: ld.export_files(other, a), lambda: led.get_account_ledger_info(other, a)]
    for call in calls:
        with pytest.raises((accts.NotAuthorized, accts.NotFound)):
            call()
    mine = ld.transactions_view(ME, a)
    assert mine                                                                                    # and they were not just empty for everyone
    their = accts.create_account(other, broker="manual", display_name="Theirs")["account_id"]
    assert ld.transactions_view(other, their) == [] and their != a


# ── the panel (AppTest) ───────────────────────────────────────────────────────

def _panel_app():
    import sys
    from pathlib import Path as P
    sys.path.insert(0, str(P.cwd()))
    import streamlit as st
    from portfolio_agent.services.context import local_context as lc
    from portfolio_agent.services.account_service import list_accounts_for
    from web.components.ledger_panel import render_ledger_panel
    ctx = lc("web")
    render_ledger_panel(ctx, list_accounts_for(ctx), key="t")


def _run():
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_function(_panel_app, default_timeout=60).run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def test_the_panel_renders_a_ledger_account_with_struck_voided_rows_and_badges():
    a = busy_account()
    at = _run()
    html = " ".join(m.value for m in at.markdown)
    assert "line-through" in html and "voids #" in html and "No approval" in html and "without approval" in html
    assert [t.label for t in at.tabs] == ["Transactions", "Add", "Lots & gains", "Reconcile", "Import / export", "Ledger settings"]
    assert any("derived from the ledger" in c.value for c in at.caption) and a


def test_the_panel_for_a_snapshot_account_offers_the_switch_and_nothing_else_writes():
    s = accts.create_account(ME, broker="manual", display_name="Snap")["account_id"]
    accts.add_position(ME, s, {"ticker": "VTI", "shares": 10, "cost_basis_total": 2000.0})
    at = _run()
    assert any("snapshot" in c.value.lower() for c in at.caption)
    sw = [b for b in at.button if b.label == "Switch to ledger"]
    assert sw and sw[0].disabled is True                                          # needs the confirmation first
    at.checkbox(key="t_confirm_" + str(s)).check().run()
    at.button(key=f"t_switch_go_{s}").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert repo.get_account(s)["holdings_source"] == "ledger"
    assert [t["txn_type"] for t in led.list_transactions(ME, s)] == ["OPENING_BALANCE"]


def test_the_panel_add_form_records_a_transaction_and_void_works():
    a = ledger_account()
    at = _run()
    at.selectbox(key=f"t_addtype_{a}").select("DEPOSIT").run()
    assert not at.exception, [e.value for e in at.exception]
    form_btn = [b for b in at.button if b.label == "Record"]
    assert form_btn
    led.record_cash_transfer(ME, a, "DEPOSIT", 10, day(1))                        # (the form submit itself is covered through ld.submit_transaction)
    at = _run()
    pick = at.selectbox(key=f"t_void_pick_{a}")
    at.text_input(key=f"t_void_why_{a}").input("typo").run()
    at.button(key=f"t_void_go_{a}").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert [t["voided_by"] is not None for t in led.list_transactions(ME, a) if t["txn_type"] == "DEPOSIT"] == [True] and pick


def test_the_accounts_page_has_the_ledger_tile_and_the_panel_never_touches_the_database_directly():
    from pathlib import Path as P
    page = (P(__file__).resolve().parents[1] / "web" / "pages" / "13_🗂️_Accounts.py").read_text()
    assert "Transactions & ledger" in page and "render_ledger_panel" in page
    panel = (P(__file__).resolve().parents[1] / "web" / "components" / "ledger_panel.py").read_text()
    data = (P(__file__).resolve().parents[1] / "web" / "data" / "ledger.py").read_text()
    for src in (panel, data):
        assert "sqlite3" not in src and "unit_of_work" not in src and "holdings_db" not in src and "ledger_db" not in src
