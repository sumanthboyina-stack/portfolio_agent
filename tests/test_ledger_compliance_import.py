"""Step 3.3: pre-clearance checks on recorded trades, the COMPLIANCE notification, the Portfolio Health ACTION count, generic CSV transaction
import (mapping, preview, dedupe, ledger accounts only) and the two CSV exports. No network: the notification senders are never reached."""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.services import account_service as accts
from portfolio_agent.services import ledger_service as led
from portfolio_agent.services import notification_service as ns
from portfolio_agent.services.context import RequestContext, _mint_identity_for_tests, local_context
from portfolio_agent.tools import holdings_db as repo
from portfolio_agent.tools import ledger_csv_import as csvi
from portfolio_agent.tools import ledger_db as ldb
from portfolio_agent.tools import portfolio_health as ph
from portfolio_agent.tools import trade_approvals_db as tdb
from portfolio_agent.tools import user_profile_db
from tests.test_notification_delivery import _rows, _world

ME = local_context("test")
OWNER_SCOPE = f"user:{ME.actor}"
day = lambda back=0: (date.today() - timedelta(days=back)).isoformat()


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    for name in ("APEX_SMTP_USERNAME", "APEX_SMTP_PASSWORD", "APEX_NTFY_TOKEN", "APEX_NOTIFY_SIGNING_KEY"):
        monkeypatch.delenv(name, raising=False)
    ns._last_test.clear()


def ledger_account(name="L", ctx=ME):
    a = accts.create_account(ctx, broker="manual", display_name=name)["account_id"]
    repo.update_account(a, holdings_source="ledger")
    return a


def grant(ticker, action="trade", recorded_at=None, expires_at=None, event="granted"):
    """An approval event (recorded_at can be set in the past; the table is append-only so this only inserts)."""
    rec = recorded_at or (date.today().isoformat() + "T08:00:00+00:00")
    with tdb._db():
        pass                                                                                                    # makes sure the table exists, grants nothing
    with sqlite3.connect(db_module.DB_PATH) as c:
        c.execute("INSERT INTO trade_approvals (owner_scope, ticker, action, event, granted_by, recorded_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (OWNER_SCOPE, ticker.upper(), action, event, "compliance desk", rec, expires_at))


def status(res):
    return res["txn"]["clearance_status"], res["txn"]["clearance_approval_id"]


# ── clearance ─────────────────────────────────────────────────────────────────

def test_a_trade_without_an_approval_is_recorded_flagged_missing_and_audited_never_blocked():
    a = ledger_account()
    r = led.record_trade(ME, a, "AAPL", "BUY", 5, 100, day(1))
    assert r["txn"]["txn_id"] and status(r) == ("missing", None)                    # the trade stands
    ev = next(e for e in repo.get_audit_events(limit=20, account_id=a) if e["operation"] == "record_buy")
    assert ev["after"]["clearance_status"] == "missing"
    assert led.get_ledger_summary(ME, a)["positions"]["AAPL"]["quantity"] == 5


def test_an_approval_covering_ticker_side_and_date_marks_the_trade_approved_with_its_id():
    a = ledger_account()
    grant("AAPL", "trade", recorded_at=day(10) + "T09:00:00+00:00")
    r = led.record_trade(ME, a, "aapl", "BUY", 1, 100, day(1))
    st, aid = status(r)
    assert st == "approved" and isinstance(aid, int)
    with sqlite3.connect(db_module.DB_PATH) as c:
        assert c.execute("SELECT ticker, action FROM trade_approvals WHERE id = ?", [aid]).fetchone() == ("AAPL", "trade")
    grant("MSFT", "buy", recorded_at=day(10) + "T09:00:00+00:00")                    # an approval for the buy side only
    assert status(led.record_trade(ME, a, "MSFT", "BUY", 1, 100, day(1)))[0] == "approved"
    led.record_trade(ME, a, "MSFT", "BUY", 1, 100, day(1))
    assert status(led.record_trade(ME, a, "MSFT", "SELL", 1, 100, day(0)))[0] == "missing"      # ... does not cover a sale
    assert status(led.record_trade(ME, a, "NVDA", "BUY", 1, 100, day(1)))[0] == "missing"       # ... nor another ticker


def test_approvals_are_read_as_of_the_trade_date():
    a = ledger_account()
    grant("AAPL", recorded_at=day(5) + "T09:00:00+00:00")
    assert status(led.record_trade(ME, a, "AAPL", "BUY", 1, 1, day(20)))[0] == "missing"      # approved only 5 days ago: a trade 20 days ago was not covered
    assert status(led.record_trade(ME, a, "AAPL", "BUY", 1, 1, day(5)))[0] == "approved"      # the same day counts
    grant("TSLA", recorded_at=day(30) + "T09:00:00+00:00", expires_at=day(10) + "T00:00:00+00:00")
    assert status(led.record_trade(ME, a, "TSLA", "BUY", 1, 1, day(12)))[0] == "approved"     # inside its window
    assert status(led.record_trade(ME, a, "TSLA", "BUY", 1, 1, day(3)))[0] == "missing"       # after it expired
    grant("KO", recorded_at=day(30) + "T09:00:00+00:00")
    grant("KO", recorded_at=day(10) + "T09:00:00+00:00", event="revoked")
    assert status(led.record_trade(ME, a, "KO", "BUY", 1, 1, day(15)))[0] == "approved"       # revoked LATER does not un-cover an earlier trade
    assert status(led.record_trade(ME, a, "KO", "BUY", 1, 1, day(2)))[0] == "missing"         # after the revoke


def test_not_required_when_the_owner_turned_pre_clearance_off():
    a = ledger_account()
    user_profile_db.update_user_profile_for(ME, pre_clearance_required=False)
    assert status(led.record_trade(ME, a, "AAPL", "BUY", 1, 1, day(1))) == ("not_required", None)


def test_a_paper_account_is_not_checked_at_all():
    a = ledger_account("Paper")
    with sqlite3.connect(db_module.DB_PATH) as c:
        c.execute("UPDATE accounts SET is_paper = 1 WHERE account_id = ?", [a])
    r = led.record_trade(ME, a, "AAPL", "BUY", 1, 1, day(1))
    assert status(r) == (None, None) and r["notify_pending"] is False


def test_unreadable_approvals_are_unknown_not_missing_and_send_nothing(monkeypatch):
    a = ledger_account()
    monkeypatch.setattr(tdb, "get_approval_as_of", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    spy = []
    monkeypatch.setattr(ns, "notify", lambda *a, **k: spy.append((a, k)) or {})
    r = led.record_trade(ME, a, "AAPL", "BUY", 1, 1, day(1))
    assert status(r) == ("unknown", None) and spy == []


def test_a_missing_clearance_enqueues_one_brief_compliance_notification_without_amounts():
    _world()
    a = ledger_account()
    r = led.record_trade(ME, a, "AAPL", "BUY", 123, 456.78, day(1), fees=9.99)
    txn_id = r["txn"]["txn_id"]
    rows = _rows(dedup_key=f"ledger_clearance:{txn_id}")
    assert sorted(x["channel"] for x in rows) == ["EMAIL", "PUSH"] and {x["category"] for x in rows} == {"COMPLIANCE"}
    for x in rows:
        text = f"{x['subject']} {x['body_brief']} {x.get('body_detailed') or ''}"
        for secret in ("123", "456", "9.99", "$"):
            assert secret not in text
    assert "AAPL" in " ".join(str(v) for x in rows for v in x.values() if isinstance(v, str))
    again = led.notify_clearance_missing(ME.actor, r["txn"])                         # however often it is called: once per transaction
    assert again["enqueued"] == [] and sorted(again["duplicates"]) == ["EMAIL", "PUSH"]
    assert len(_rows(dedup_key=f"ledger_clearance:{txn_id}")) == 2
    grant("MSFT", recorded_at=day(5) + "T09:00:00+00:00")
    led.record_trade(ME, a, "MSFT", "BUY", 1, 1, day(1))                              # approved: nothing sent
    assert len(_rows()) == 2
    user_profile_db.update_user_profile_for(ME, pre_clearance_required=False)
    led.record_trade(ME, a, "KO", "BUY", 1, 1, day(1))                                # not required: nothing sent
    assert len(_rows()) == 2


def test_with_a_caller_owned_transaction_the_notification_waits_for_the_caller(monkeypatch):
    _world()
    a = ledger_account()
    with repo.unit_of_work() as c:
        r = led.record_trade(ME, a, "AAPL", "BUY", 1, 1, day(1), conn=c)
        assert r["notify_pending"] is True and _rows() == []                             # nothing sent inside the transaction
    led.notify_clearance_missing(ME.actor, r["txn"])
    assert len(_rows()) == 2


# ── Portfolio Health ──────────────────────────────────────────────────────────

def test_health_compliance_check_is_action_when_trades_lack_clearance():
    a = ledger_account()
    t1 = led.record_trade(ME, a, "AAPL", "BUY", 1, 1, day(3))["txn"]["txn_id"]
    led.record_trade(ME, a, "MSFT", "BUY", 1, 1, day(2))
    grant("KO", recorded_at=day(9) + "T09:00:00+00:00")
    led.record_trade(ME, a, "KO", "BUY", 1, 1, day(1))
    paper = ledger_account("P")
    with sqlite3.connect(db_module.DB_PATH) as c:
        c.execute("UPDATE accounts SET is_paper = 1 WHERE account_id = ?", [paper])
    led.record_trade(ME, paper, "NVDA", "BUY", 1, 1, day(1))
    pid = repo.get_portfolio_by_owner(ME.actor)["portfolio_id"]
    with repo.unit_of_work() as c:
        counts = ldb.clearance_counts(c, pid)
    assert (counts["missing"], counts["approved"], counts["missing_tickers"]) == (2, 1, ["AAPL", "MSFT"])      # paper excluded, approved not counted
    inp = SimpleNamespace(restricted=[], holdings=[], clearance=counts)
    chk = ph._compliance(inp)
    assert chk.status == ph.A and chk.key == "compliance" and "2 recorded trades" in chk.summary and "AAPL" in chk.summary
    assert ph.RC_TRADES_WITHOUT_CLEARANCE in chk.reason_codes and chk.metrics["trades_missing_clearance"] == 2
    led.void_transaction(ME, t1, reason="entered twice")                                                       # a voided trade no longer counts
    with repo.unit_of_work() as c:
        assert ldb.clearance_counts(c, pid)["missing"] == 1


def test_health_compliance_is_unchanged_when_nothing_needs_attention_and_unknown_is_a_watch():
    clean = ph._compliance(SimpleNamespace(restricted=[], holdings=[], clearance={"missing": 0, "unknown": 0}))
    assert (clean.status, clean.title) == (ph.G, "Restricted list")                                          # exactly the old check
    assert ph._compliance(SimpleNamespace(restricted=[], holdings=[], clearance=None)).status == ph.G
    unk = ph._compliance(SimpleNamespace(restricted=[], holdings=[], clearance={"missing": 0, "unknown": 2, "missing_tickers": []}))
    assert unk.status == ph.W and ph.RC_CLEARANCE_UNKNOWN in unk.reason_codes
    both = ph._compliance(SimpleNamespace(restricted=[{"ticker": "X", "reason": "r"}], holdings=[{"ticker": "X", "shares": 1, "current_value": 10}],
                                          clearance={"missing": 1, "unknown": 0, "missing_tickers": ["X"]}))
    assert both.status == ph.A and "restricted list" in both.summary and "1 recorded trade " in both.summary


# ── CSV import ────────────────────────────────────────────────────────────────

CSV = (b"Trade Date,Action,Symbol,Shares,Price,Amount,Commission,Transaction ID,Source\n"
       b"2026-01-05,Buy,VTI,10,200.00,,1.00,T1,\n"
       b"01/06/2026,Contribution,,,,\"$1,500.00\",,T2,Employee\n"
       b"2026-01-07,Dividend,VTI,,,12.34,,T3,\n"
       b"2026-01-08,Sold,VTI,4,210,,0,T4,\n"
       b"2026-01-09,Employer Match,,,,(100.00),,T5,Employer\n")


def mapping_for(raw=CSV):
    return led.suggest_csv_mapping(raw)["mapping"]


def test_the_mapping_step_suggests_columns_and_the_preview_writes_nothing():
    sug = led.suggest_csv_mapping(CSV)
    assert sug["mapping"] == {"date": "Trade Date", "type": "Action", "ticker": "Symbol", "quantity": "Shares", "price": "Price",
                              "amount": "Amount", "fees": "Commission", "external_ref": "Transaction ID", "contribution_source": "Source"}
    assert sug["rows"] == 5 and sug["headers"][0] == "Trade Date" and len(sug["sample"]) == 5
    a = ledger_account()
    before = len(led.list_transactions(ME, a))
    pv = led.preview_transactions_csv(ME, a, CSV, sug["mapping"])
    assert pv["summary"] == {"new": 5, "duplicate": 0, "invalid": 0, "rows": 5, "warnings": 0} and pv["can_import"] is True
    assert [r["type"] for r in pv["rows"]] == ["BUY", "DEPOSIT", "DIVIDEND", "SELL", "EMPLOYER_MATCH"]
    assert pv["rows"][0]["cash_amount"] == -2001.0 and pv["rows"][3]["cash_amount"] == 840.0
    assert len(led.list_transactions(ME, a)) == before == 0
    with repo.unit_of_work() as c:
        assert c.execute("SELECT COUNT(*) FROM import_runs").fetchone()[0] == 0


def test_import_applies_everything_through_the_ledger_and_records_the_run():
    a = ledger_account()
    out = led.import_transactions_csv(ME, a, CSV, mapping_for(), filename="jan.csv")
    assert (out["imported"], out["skipped"]) == (5, 0) and out["missing_clearance"] == 2          # the BUY and the SELL: neither has an approval
    rows = {r["external_id"]: r for r in led.list_transactions(ME, a)}
    assert set(rows) == {"T1", "T2", "T3", "T4", "T5"} and all(r["import_run_id"] == out["run_id"] for r in rows.values())
    assert rows["T2"]["contribution_source"] == "EMPLOYEE" and rows["T5"]["contribution_source"] == "EMPLOYER" and rows["T5"]["amount"] == 100.0
    assert rows["T4"]["txn_type"] == "SELL" and rows["T4"]["lot_method"] == "FIFO" and rows["T1"]["fees"] == 1.0
    s = led.get_ledger_summary(ME, a)
    assert s["positions"]["VTI"]["quantity"] == 6 and s["cash"] == pytest.approx(-2001 + 1500 + 12.34 + 840 + 100)
    with repo.unit_of_work() as c:
        run = dict(c.execute("SELECT * FROM import_runs WHERE id = ?", [out["run_id"]]).fetchone())
    assert (run["broker"], run["filename"], run["added"], run["skipped"]) == ("csv_transactions", "jan.csv", 5, 0) and run["statement_date"] == "2026-01-09"
    ops = [e["operation"] for e in repo.get_audit_events(limit=50, account_id=a)]
    assert "import_transactions_csv" in ops and ops.count("record_buy") == 1


def test_reimporting_the_same_file_adds_nothing():
    a = ledger_account()
    led.import_transactions_csv(ME, a, CSV, mapping_for())
    n = len(led.list_transactions(ME, a))
    out = led.import_transactions_csv(ME, a, CSV, mapping_for())
    assert (out["imported"], out["skipped"]) == (0, 5) and len(led.list_transactions(ME, a)) == n
    pv = led.preview_transactions_csv(ME, a, CSV, mapping_for())
    assert pv["summary"]["duplicate"] == 5 and {r["duplicate_of"] for r in pv["rows"]} == {"external_ref"} and pv["can_import"] is False


def test_without_an_external_ref_rows_dedupe_on_date_type_ticker_quantity_and_amount_by_count():
    raw = (b"date,type,ticker,quantity,price,amount\n2026-02-02,buy,VTI,5,100,\n2026-02-02,buy,VTI,5,100,\n2026-02-03,deposit,,,,500\n")
    m = mapping_for(raw)
    assert m["external_ref"] is None
    a = ledger_account()
    first = led.import_transactions_csv(ME, a, raw, m)
    assert first["imported"] == 3                                                       # two genuinely identical trades are both kept the first time
    second = led.import_transactions_csv(ME, a, raw, m)
    assert second["imported"] == 0 and second["skipped"] == 3
    more = raw + b"2026-02-02,buy,VTI,5,100,\n"                                          # a THIRD identical row is new
    third = led.import_transactions_csv(ME, a, more, m)
    assert (third["imported"], third["skipped"]) == (1, 3)
    # a voided transaction no longer blocks its own re-import
    txns = [t for t in led.list_transactions(ME, a) if t["txn_type"] == "DEPOSIT"]
    led.void_transaction(ME, txns[0]["txn_id"], reason="wrong")
    assert led.import_transactions_csv(ME, a, raw, m)["imported"] == 1


def test_import_into_a_snapshot_account_is_rejected_and_writes_nothing():
    snap = accts.create_account(ME, broker="manual", display_name="Snap")["account_id"]
    with pytest.raises(led.ImportRejected, match="ledger account"):
        led.import_transactions_csv(ME, snap, CSV, mapping_for())
    with pytest.raises(led.ImportRejected):
        led.preview_transactions_csv(ME, snap, CSV, mapping_for())
    assert led.list_transactions(ME, snap) == []
    with repo.unit_of_work() as c:
        assert c.execute("SELECT COUNT(*) FROM import_runs").fetchone()[0] == 0


def test_invalid_rows_block_the_whole_import_with_the_line_numbers():
    raw = (b"date,type,ticker,quantity,price,amount\n2026-03-01,buy,VTI,5,100,\n2026-03-02,gift,,,,10\n"
           b"not a date,buy,VTI,1,1,\n2026-03-03,buy,,1,1,\n2026-03-04,sell,VTI,x,1,\n2026-03-05,split,VTI,2,,\n2999-01-01,buy,VTI,1,1,\n")
    a = ledger_account()
    pv = led.preview_transactions_csv(ME, a, raw, mapping_for(raw))
    assert pv["summary"]["invalid"] == 6 and pv["can_import"] is False
    errs = {r["line"]: " ".join(r["errors"]) for r in pv["rows"] if r["errors"]}
    assert "gift" in errs[3] and "date" in errs[4] and "ticker" in errs[5] and "not a number" in errs[6] and "splits" in errs[7] and "future" in errs[8]
    with pytest.raises(led.ImportRejected, match="6 row"):
        led.import_transactions_csv(ME, a, raw, mapping_for(raw))
    assert led.list_transactions(ME, a) == []                                            # even the one good row was not imported


def test_a_sale_the_ledger_cannot_support_is_caught_in_the_preview_and_nothing_is_written():
    raw = b"date,type,ticker,quantity,price,amount\n2026-03-01,buy,VTI,5,100,\n2026-03-02,sell,VTI,9,100,\n"
    a = ledger_account()
    pv = led.preview_transactions_csv(ME, a, raw, mapping_for(raw))
    assert pv["can_import"] is False and "only 5 held" in pv["dry_run_error"]
    with pytest.raises(led.ImportRejected, match="only 5 held"):
        led.import_transactions_csv(ME, a, raw, mapping_for(raw))
    assert led.list_transactions(ME, a) == []


def test_file_order_does_not_matter_a_sale_listed_before_its_purchase_still_imports():
    raw = b"date,type,ticker,quantity,price,amount\n2026-03-05,sell,VTI,5,110,\n2026-03-01,buy,VTI,5,100,\n"
    a = ledger_account()
    assert led.import_transactions_csv(ME, a, raw, mapping_for(raw))["imported"] == 2
    assert led.get_closures(ME, a)[0]["realized_gain"] == pytest.approx(50.0)


def test_the_import_is_all_or_nothing_if_a_row_fails_midway(monkeypatch):
    a = ledger_account()
    real = led.record_income
    monkeypatch.setattr(led, "record_income", lambda *a_, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        led.import_transactions_csv(ME, a, CSV, mapping_for())
    monkeypatch.setattr(led, "record_income", real)
    assert led.list_transactions(ME, a) == [] and led.get_lots(ME, a) == []
    with repo.unit_of_work() as c:
        assert c.execute("SELECT COUNT(*) FROM import_runs").fetchone()[0] == 0              # not even the run row
    assert led.import_transactions_csv(ME, a, CSV, mapping_for())["imported"] == 5            # and the same file then goes in cleanly


def test_cell_parsing_and_price_derivation_warning():
    assert csvi.parse_date("3/7/26") == "2026-03-07" and csvi.parse_date("07-Mar-2026") == "2026-03-07" and csvi.parse_date("Mar 7, 2026") == "2026-03-07"
    assert csvi.parse_date("garbage") is None
    assert csvi.parse_number("$1,234.50") == 1234.5 and csvi.parse_number("(5.00)") == -5.0 and csvi.parse_number("") is None
    with pytest.raises(ValueError):
        csvi.parse_number("abc")
    assert csvi.normalize_type("  Bought ") == "BUY" and csvi.normalize_type("DEPOSIT") == "DEPOSIT" and csvi.normalize_type("wire") is None
    raw = b"date,type,ticker,quantity,amount\n2026-03-01,buy,VTI,10,1001\n"
    a = ledger_account()
    pv = led.preview_transactions_csv(ME, a, raw, mapping_for(raw))
    assert pv["rows"][0]["warnings"] and pv["can_import"] is True
    led.import_transactions_csv(ME, a, raw, mapping_for(raw))
    assert led.list_transactions(ME, a)[0]["price"] == pytest.approx(100.1)


def test_mapping_and_file_errors_are_clear():
    for raw in (b"", b"   \n", b"a,a\n1,2\n"):
        with pytest.raises(led.ImportRejected):
            led.suggest_csv_mapping(raw)
    a = ledger_account()
    for bad in ({"date": "Trade Date"}, {**mapping_for(), "price": "Nope"}, {**mapping_for(), "ticker": "Symbol", "price": "Symbol"}):
        with pytest.raises(led.ImportRejected):
            led.preview_transactions_csv(ME, a, CSV, bad)


def test_only_the_owner_can_import_or_preview():
    other = RequestContext(identity=_mint_identity_for_tests("someone-else"), source="test")
    a = ledger_account()
    for call in (lambda: led.import_transactions_csv(other, a, CSV, mapping_for()), lambda: led.preview_transactions_csv(other, a, CSV, mapping_for()),
                 lambda: led.export_transactions_csv(other, a), lambda: led.export_realized_gains_csv(other, a)):
        with pytest.raises((led.NotAuthorized, led.NotFound)):
            call()


# ── exports ───────────────────────────────────────────────────────────────────

def read(text):
    import csv, io
    return list(csv.DictReader(io.StringIO(text)))


def test_transactions_export_lists_everything_including_voided_rows_and_defuses_formulas():
    a = ledger_account()
    led.record_cash_transfer(ME, a, "DEPOSIT", 1000, day(30), contribution_source="employee", memo="=HYPERLINK(\"http://x\")")
    b = led.record_trade(ME, a, "VTI", "BUY", 3, 100, day(20))["txn"]["txn_id"]
    led.void_transaction(ME, b, reason="oops")
    rows = read(led.export_transactions_csv(ME, a))
    assert [r["type"] for r in rows] == ["DEPOSIT", "BUY", "VOID"] and rows[0]["contribution_source"] == "EMPLOYEE"
    assert rows[0]["memo"].startswith("'=HYPERLINK") and rows[1]["voided_by"] == rows[2]["txn_id"] and rows[2]["voids_txn_id"] == str(b)
    assert rows[1]["clearance_status"] == "missing" and rows[1]["ticker"] == "VTI" and float(rows[1]["amount"]) == -300.0
    assert list(rows[0])[:5] == ["txn_id", "trade_date", "settle_date", "type", "ticker"]


def test_realized_gains_export_has_the_lots_and_filters_by_year():
    a = ledger_account()
    led.record_trade(ME, a, "VTI", "BUY", 10, 100, "2024-03-01")
    led.record_trade(ME, a, "VTI", "BUY", 10, 150, "2025-06-01")
    led.record_trade(ME, a, "VTI", "SELL", 15, 200, "2025-09-01")
    led.record_trade(ME, a, "VTI", "SELL", 2, 210, day(1))
    allrows = read(led.export_realized_gains_csv(ME, a))
    assert [(r["quantity"], r["holding_period"], float(r["realized_gain"])) for r in allrows[:2]] == [("10.0", "LONG", 1000.0), ("5.0", "SHORT", 250.0)]
    assert len(allrows) == 3 and allrows[0]["lot_method"] == "FIFO" and allrows[0]["open_date"] == "2024-03-01"
    assert len(read(led.export_realized_gains_csv(ME, a, year=2025))) == 2
    assert read(led.export_realized_gains_csv(ME, a, year=2019)) == []
    snap = accts.create_account(ME, broker="manual", display_name="S")["account_id"]
    assert led.export_realized_gains_csv(ME, snap).strip().split("\n")[0].startswith("ticker,quantity")        # header only
