"""Fidelity "History for Account" CSV: recognised by its headers and rewritten into the generic columns, so the usual mapping / preview / dedupe /
replay / apply path takes it. The rows below are a trimmed copy of a real export (header, trades, dividends, a money-market sweep, a payroll credit,
a transfer, and the disclaimer lines Fidelity appends)."""
from __future__ import annotations

from datetime import date

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.services import account_service as accts
from portfolio_agent.services import ledger_service as led
from portfolio_agent.services.context import local_context
from portfolio_agent.tools import holdings_db as repo
from portfolio_agent.tools import ledger_csv_import as csvi

ME = local_context("test")
TODAY = date(2026, 10, 22)

FIDELITY = (
    "\n"
    "Run Date,Action,Symbol,Description,Type,Price ($),Quantity,Commission ($),Fees ($),Accrued Interest ($),Amount ($),Cash Balance ($),Settlement Date\n"
    '10/16/2026,YOU SOLD ARTHUR J. GALLAGHER & CO. ISIN #US36... (AJG) (Cash),AJG,ARTHUR J. GALLAGHER & CO.,Cash,252.84,"-20","",0.11,"",5056.69,5626.43,10/19/2026\n'
    '10/02/2026,YOU BOUGHT ESPP### AS OF 09-30-26 ARTHUR J. GALLAGHER & CO. ISIN #US36... (AJG) (Cash),AJG,ARTHUR J. GALLAGHER & CO.,Cash,216.71,22.3509,"","","","-4843.55",569.74,10/02/2026\n'
    '10/02/2026,JOURNALED SPP PURCHASE CREDIT (Cash),"",No Description,Cash,"",0,"","","",4843.55,5413.29,""\n'
    '10/01/2026,DIVIDEND RECEIVED NVIDIA CORPORATION COM (NVDA) (Cash),NVDA,NVIDIA CORPORATION COM,Cash,"",0,"","","",18.5,569.74,""\n'
    '09/30/2026,REINVESTMENT FIDELITY GOVERNMENT MONEY MARKET (SPAXX) (Cash),SPAXX,FIDELITY GOVERNMENT MONEY MARKET,Cash,1,0.42,"","","","-0.42",551.24,""\n'
    '09/30/2026,DIVIDEND RECEIVED FIDELITY GOVERNMENT MONEY MARKET (SPAXX) (Cash),SPAXX,FIDELITY GOVERNMENT MONEY MARKET,Cash,"",0,"","","",0.42,551.24,""\n'
    '06/23/2026,YOU BOUGHT MICRON TECHNOLOGY INC COM (MU) (Cash),MU,MICRON TECHNOLOGY INC COM,Cash,1079.19,4,"","","","-4316.74",502.89,06/24/2026\n'
    '06/23/2026,Electronic Funds Transfer Received (Cash),"",No Description,Cash,"",0,"","","",5000,5359.22,""\n'
    "\n"
    "The data and information in this spreadsheet is provided to you solely for your use and is not for distribution. The spreadsheet is provided for\n"
    '"informational purposes only, and is not intended to provide advice."\n'
    "\n"
    "Date downloaded 10/22/2026 10:23 am\n"
).encode()


def _plan(raw=FIDELITY, **kw):
    headers, rows = csvi.read_csv(raw)
    return csvi.plan_rows(rows, csvi.suggest_mapping(headers), existing_external_ids=set(), existing_keys={}, today=TODAY, **kw)


def test_the_export_is_recognised_the_disclaimer_lines_are_dropped_and_the_mapping_is_right():
    headers, rows = csvi.read_csv(FIDELITY)
    assert headers == list(csvi.FIDELITY_COLUMNS)
    assert len(rows) == 8                                                     # the 8 data rows; the blank / disclaimer / "Date downloaded" lines are gone
    assert csvi.suggest_mapping(headers) == {"date": "Date", "type": "Type", "ticker": "Ticker", "quantity": "Quantity", "price": "Price",
                                             "amount": "Amount", "fees": "Fees", "external_ref": "Reference", "contribution_source": "Money source"}


def test_every_row_is_read_with_no_invalid_rows_or_warnings():
    plan = _plan()
    assert csvi.summarize(plan) == {"new": 7, "duplicate": 0, "skipped": 1, "invalid": 0, "rows": 8, "warnings": 0}
    by_line = {p.fields.get("ticker") or p.txn_type: p for p in plan if p.status == "new"}
    assert [p.txn_type for p in plan] == ["SELL", "BUY", "DEPOSIT", "DIVIDEND", None, "INTEREST", "BUY", "DEPOSIT"]
    assert by_line["NVDA"].txn_type == "DIVIDEND" and by_line["NVDA"].cash_amount == 18.5


def test_a_sale_takes_its_quantity_from_the_sign_and_its_price_from_the_cash_that_moved():
    sell = _plan()[0]
    assert (sell.txn_type, sell.fields["ticker"], sell.fields["quantity"], sell.fields["fees"]) == ("SELL", "AJG", 20.0, 0.11)
    assert sell.cash_amount == 5056.69                                       # = 20 x 252.84 - 0.11, to the cent
    buy = _plan()[1]
    assert buy.txn_type == "BUY" and buy.fields["quantity"] == 22.3509 and buy.cash_amount == -4843.55   # the listed price is rounded; the cash is exact


def test_the_payroll_credit_and_the_transfer_are_deposits_and_a_money_market_dividend_is_interest():
    plan = _plan()
    credit, interest, eft = plan[2], plan[5], plan[7]
    assert (credit.txn_type, credit.fields["contribution_source"], credit.cash_amount) == ("DEPOSIT", "ESPP", 4843.55)
    assert (interest.txn_type, interest.fields["ticker"], interest.cash_amount) == ("INTEREST", None, 0.42)
    assert (eft.txn_type, eft.cash_amount) == ("DEPOSIT", 5000.0)


def test_a_money_market_reinvestment_is_skipped_with_its_reason_not_counted_as_a_trade():
    sweep = _plan()[4]
    assert sweep.status == "skipped" and "money-market" in sweep.skip_reason and sweep.txn_type is None and sweep.errors == []


def _file_cash_without_sweeps() -> float:
    """Independent of the preset: the Amount column of the raw file, minus the money-market reinvestment (which is cash moving into the sweep fund
    that is this account's cash, so it nets out)."""
    import csv
    import io
    rows = [r for r in csv.DictReader(io.StringIO(FIDELITY.decode().lstrip())) if (r.get("Amount ($)") or "").strip() and "REINVESTMENT" not in r["Action"]]
    return round(sum(float(r["Amount ($)"]) for r in rows), 2)


def test_the_cash_of_the_imported_rows_equals_the_amount_column_of_the_file():
    plan = _plan()
    assert round(sum(p.cash_amount or 0.0 for p in plan if p.status == "new"), 2) == _file_cash_without_sweeps() == 5758.87


def test_rows_on_or_before_the_ledger_start_are_skipped_not_invalid_and_later_ones_stay_new():
    plan = _plan(ledger_start_date="2026-10-01")
    status = {p.fields.get("trade_date") or p.skip_reason[:12]: p.status for p in plan}
    assert [p.status for p in plan] == ["new", "new", "new", "skipped", "skipped", "skipped", "skipped", "skipped"]
    assert csvi.summarize(plan)["invalid"] == 0
    skipped = [p for p in plan if p.status == "skipped" and p.fields]
    assert skipped and all("opening balance" in p.skip_reason and "2026-10-01" in p.skip_reason for p in skipped)
    assert status["2026-10-16"] == "new"


def test_an_action_the_preset_does_not_know_is_invalid_and_names_it():
    raw = FIDELITY.replace(b"Electronic Funds Transfer Received (Cash)", b"TRANSFERRED FROM TO BROKERAGE (Cash)")
    bad = [p for p in _plan(raw) if p.status == "invalid"]
    assert len(bad) == 1 and "TRANSFERRED FROM TO BROKERAGE (Cash)" in bad[0].errors[0]


# ── through the service: preview and import on a ledger account ──────────────────────────────────────────────────

@pytest.fixture
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(led, "_today", lambda: TODAY)


def _ledger_account(start=None):
    a = accts.create_account(ME, broker="manual", display_name="Fidelity")["account_id"]
    repo.update_account(a, holdings_source="ledger", ledger_start_date=start)
    return a


def test_the_service_previews_and_imports_the_whole_export_and_the_ledger_cash_matches(_env):
    a = _ledger_account()
    sug = led.suggest_csv_mapping(FIDELITY)
    assert sug["rows"] == 8 and sug["mapping"]["type"] == "Type"
    pv = led.preview_transactions_csv(ME, a, FIDELITY, sug["mapping"])
    assert pv["summary"]["new"] == 7 and pv["summary"]["skipped"] == 1 and pv["summary"]["invalid"] == 0 and pv["can_import"] is True
    out = led.import_transactions_csv(ME, a, FIDELITY, sug["mapping"], filename="History_for_Account_X77985900.csv")
    assert (out["imported"], out["skipped"]) == (7, 1)
    s = led.get_ledger_summary(ME, a)
    assert s["cash"] == pytest.approx(_file_cash_without_sweeps())
    assert s["positions"]["AJG"]["quantity"] == pytest.approx(2.3509) and s["positions"]["MU"]["quantity"] == 4
    again = led.import_transactions_csv(ME, a, FIDELITY, sug["mapping"])
    assert (again["imported"], again["skipped"]) == (0, 8)                  # a re-import adds nothing


def test_with_a_ledger_start_date_the_covered_rows_are_skipped_and_nothing_is_refused(_env):
    a = _ledger_account(start="2026-10-16")                                   # the opening balance already holds everything up to 10/16
    sug = led.suggest_csv_mapping(FIDELITY)
    pv = led.preview_transactions_csv(ME, a, FIDELITY, sug["mapping"])
    assert pv["summary"] == {"new": 0, "duplicate": 0, "skipped": 8, "invalid": 0, "rows": 8, "warnings": 0}
    assert all(r["skip_reason"] for r in pv["rows"]) and pv["can_import"] is False     # nothing new to import, and nothing is an error
