"""Vanguard "OfxDownload" CSV: one file holding a positions table and then a transactions table. The transactions are found by their header and
rewritten into the generic columns; the money-market settlement fund is the account's cash, so its sweeps and reinvestments are skipped. The
rows are a trimmed copy of a real export."""
from __future__ import annotations

import csv
import io
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

VANGUARD = """Account Number,Investment Name,Symbol,Shares,Share Price,Total Value,
50235676,APPLE INC,AAPL,50.184,345.5108,17339.11,
50235676,VANGUARD FEDERAL MONEY MARKET INVESTOR CL,VMFXX,425.63,1,425.63,



Account Number,Trade Date,Settlement Date,Transaction Type,Transaction Description,Investment Name,Symbol,Shares,Share Price,Principal Amount,Commissions and Fees,Net Amount,Accrued Interest,Account Type,
50235676,2026-02-12,2026-02-12,Dividend,Dividend Received,APPLE INC,AAPL,0.00000,1.0,13.01,0.0,13.01,0.0,CASH,
50235676,2026-02-12,2026-02-12,Reinvestment,Dividend Reinvestment,APPLE INC,AAPL,0.04700,275.3219,-13.01,0.0,-13.01,0.0,CASH,
50235676,2026-01-29,2026-01-30,Buy,Buy,MICROSOFT CORP,MSFT,14.00000,424.875,-5948.25,0.0,-5948.25,0.0,CASH,
50235676,2026-01-29,2026-01-30,Sell,Sell,VANGUARD S&P 500 INDEX ETF,VOO,-10.00000,630.835,6308.35,0.0,6308.35,0.0,CASH,
50235676,2026-08-27,2026-08-27,Sell,Liquidation,GRAPHEX GROUP LTD SPON ADR,GRFXY,-25.00000,0.0,2.65,0.0,2.65,0.0,CASH,
50235676,2026-06-02,2026-06-03,Sell,Sell,RIVIAN AUTOMOTIVE INC CL A,RIVN,-200.00000,17.175,3434.93,0.07,3434.93,0.0,CASH,
50235676,2026-07-02,2026-07-02,Corp Action (CUSIP change),CUSIP Change,EXXONMOBIL HOLDINGS CORP,XOM,25.00300,0.0,0.00,0.0,0.00,0.0,CASH,
50235676,2026-07-02,2026-07-02,Corp Action (CUSIP change),CUSIP Change,EXXONMOBIL HOLDINGS CORP,XOM,-25.00300,0.0,0.00,0.0,0.00,0.0,CASH,
50235676,2026-01-05,2026-01-05,Sweep in,Sweep Into Settlement Fund,VANGUARD FEDERAL MONEY MARKET INVESTOR CL,VMFXX,0.00000,1.0,-28.20,0.0,-28.20,0.0,CASH,
50235676,2026-06-30,2026-06-30,Dividend,Dividend Received,VANGUARD FEDERAL MONEY MARKET INVESTOR CL,VMFXX,0.00000,0.0,2.35,0.0,2.35,0.0,CASH,
50235676,2026-06-30,2026-06-30,Reinvestment,Dividend Reinvestment,VANGUARD FEDERAL MONEY MARKET INVESTOR CL,VMFXX,0.00000,0.0,-2.35,0.0,-2.35,0.0,CASH,
50235676,2026-10-21,2026-10-22,Buy,Buy,COREWEAVE INC CL A,CRWV,8.00000,61.82,-494.56,0.0,-494.56,0.0,CASH,

""".encode()


def _plan(raw=VANGUARD, **kw):
    headers, rows = csvi.read_csv(raw)
    return csvi.plan_rows(rows, csvi.suggest_mapping(headers), existing_external_ids=set(), existing_keys={}, today=TODAY, **kw)


def test_the_transactions_table_is_found_and_the_positions_table_above_it_is_ignored():
    headers, rows = csvi.read_csv(VANGUARD)
    assert headers == list(csvi.PRESET_COLUMNS) and len(rows) == 12
    assert csvi.suggest_mapping(headers)["date"] == "Date" and csvi.suggest_mapping(headers)["type"] == "Type"
    assert {r["Ticker"] for r in rows} >= {"AAPL", "MSFT", "VOO", "RIVN"} and all(r["Date"].startswith("2026-") for r in rows)


def test_no_row_is_invalid_and_each_kind_is_read_as_the_right_ledger_type():
    plan = _plan()
    assert csvi.summarize(plan) == {"new": 8, "duplicate": 0, "skipped": 4, "invalid": 0, "rows": 12, "warnings": 0}
    kinds = [(p.status, p.txn_type) for p in plan]
    assert kinds == [("new", "DIVIDEND"), ("new", "BUY"), ("new", "BUY"), ("new", "SELL"), ("new", "SELL"), ("new", "SELL"),
                     ("skipped", None), ("skipped", None), ("skipped", None), ("new", "INTEREST"), ("skipped", None), ("new", "BUY")]


def test_a_stock_dividend_is_reinvested_as_a_buy_and_a_money_market_dividend_is_interest_on_cash():
    plan = _plan()
    div, reinvest, mm_div = plan[0], plan[1], plan[9]
    assert (div.txn_type, div.fields["ticker"], div.cash_amount) == ("DIVIDEND", "AAPL", 13.01)
    assert (reinvest.txn_type, reinvest.fields["ticker"], reinvest.fields["quantity"], reinvest.cash_amount) == ("BUY", "AAPL", 0.047, -13.01)
    assert (mm_div.txn_type, mm_div.fields["ticker"], mm_div.cash_amount) == ("INTEREST", None, 2.35)


def test_sales_take_quantity_from_the_sign_and_price_from_the_cash_including_fees_and_a_zero_listed_price():
    plan = _plan()
    rivn, grfxy = plan[5], plan[4]
    assert (rivn.fields["quantity"], rivn.fields["fees"], rivn.fields["price"], rivn.cash_amount) == (200.0, 0.07, 17.175, 3434.93)
    assert (grfxy.fields["quantity"], grfxy.fields["price"], grfxy.cash_amount) == (25.0, 0.106, 2.65)      # listed price 0.0: derived from the 2.65 paid


def test_sweeps_money_market_reinvestments_and_cusip_changes_are_skipped_with_a_reason():
    skipped = [p for p in _plan() if p.status == "skipped"]
    reasons = " | ".join(p.skip_reason for p in skipped)
    assert "CUSIP change" in reasons and "sweep" in reasons and "money-market reinvestment" in reasons
    assert all(p.errors == [] and p.txn_type is None for p in skipped)


def test_every_cent_in_the_file_is_accounted_for_by_the_imported_rows_plus_the_cash_that_only_moved_between_the_fund_and_the_account():
    """The settlement fund holds ~0 between events, so the Net Amounts of ALL rows add to the one purchase that has not settled yet."""
    sec = VANGUARD.decode()[VANGUARD.decode().index("Account Number,Trade Date"):]
    rows = [r for r in csv.DictReader(io.StringIO(sec)) if r["Trade Date"]]
    new_cash = round(sum(p.cash_amount or 0.0 for p in _plan() if p.status == "new"), 2)
    moved = round(sum(float(r["Net Amount"]) for r in rows if r["Transaction Type"].lower().startswith("sweep")
                      or (r["Transaction Type"] == "Reinvestment" and "MONEY MARKET" in r["Investment Name"])), 2)
    assert round(new_cash + moved, 2) == round(sum(float(r["Net Amount"]) for r in rows), 2)


def test_an_unknown_transaction_type_is_invalid_and_names_it():
    raw = VANGUARD.replace(b",Sweep in,", b",Transfer (outgoing),")
    bad = [p for p in _plan(raw) if p.status == "invalid"]
    assert len(bad) == 1 and "Transfer (outgoing)" in bad[0].errors[0]


def test_rows_on_or_before_the_ledger_start_are_skipped():
    plan = _plan(ledger_start_date="2026-10-21")
    assert csvi.summarize(plan)["new"] == 0 and csvi.summarize(plan)["invalid"] == 0
    assert [p.fields["trade_date"] for p in plan if p.status == "skipped" and p.fields][-1] <= "2026-10-21"


@pytest.fixture
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(led, "_today", lambda: TODAY)


def test_through_the_service_a_ledger_account_previews_imports_and_ignores_a_second_upload(_env):
    a = accts.create_account(ME, broker="manual", display_name="Vanguard")["account_id"]
    repo.update_account(a, holdings_source="ledger")
    for ticker, qty in (("VOO", 10), ("GRFXY", 25), ("RIVN", 200)):          # what the sales in the file sold
        led.record_trade(ME, a, ticker, "BUY", qty, 10.0, "2025-12-01")
    sug = led.suggest_csv_mapping(VANGUARD)
    assert sug["rows"] == 12 and sug["mapping"]["type"] == "Type"
    pv = led.preview_transactions_csv(ME, a, VANGUARD, sug["mapping"])
    assert pv["summary"]["new"] == 8 and pv["summary"]["skipped"] == 4 and pv["summary"]["invalid"] == 0 and pv["can_import"] is True
    out = led.import_transactions_csv(ME, a, VANGUARD, sug["mapping"], filename="OfxDownload.csv")
    assert (out["imported"], out["skipped"]) == (8, 4)
    s = led.get_ledger_summary(ME, a)
    assert s["positions"]["MSFT"]["quantity"] == 14 and s["positions"]["CRWV"]["quantity"] == 8 and "RIVN" not in s["positions"]
    again = led.import_transactions_csv(ME, a, VANGUARD, sug["mapping"])
    assert (again["imported"], again["skipped"]) == (0, 12)
