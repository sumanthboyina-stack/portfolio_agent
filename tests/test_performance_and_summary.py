from __future__ import annotations

from web.data.performance import build_trend_series, normalize_index_series
from web.data.portfolio import summarize_holdings, holdings_table_rows, account_id, group_holdings_by_account
from portfolio_agent.tools.holdings_parser import parse_fidelity_csv, split_cash_rows


def _row(date, acct, ticker, shares, close, source="snapshot", cost=None):
    return {"date": date, "broker": "fidelity", "account_number": acct, "ticker": ticker,
            "shares": shares, "close_price": close, "market_value": shares * close,
            "cost_basis": cost if cost is not None else shares * close, "source": source}


def test_buying_more_shares_is_not_investment_return():
    # Price unchanged, shares doubled: recorded value +100%, investment return 0%.
    rows = [_row("2026-09-01", "X1", "AAPL", 10, 100.0), _row("2026-09-02", "X1", "AAPL", 20, 100.0)]
    s = build_trend_series(rows)
    assert s["recorded_change_pct"] == [0.0, 100.0]
    assert s["investment_return_pct"] == [0.0, 0.0]
    assert s["observed"] == [True, True] and s["return_intervals"] == 1


def test_price_move_with_flow_is_separated():
    # Day 2: bought 10 more at 110 and price rose 100 → 110: return +10%, value +120%.
    rows = [_row("2026-09-01", "X1", "AAPL", 10, 100.0), _row("2026-09-02", "X1", "AAPL", 20, 110.0)]
    s = build_trend_series(rows)
    assert round(s["recorded_change_pct"][1], 2) == 120.0
    assert round(s["investment_return_pct"][1], 2) == 10.0


def test_estimated_days_break_the_return_chain_but_count_in_value():
    rows = [_row("2026-09-01", "X1", "AAPL", 10, 100.0),
            _row("2026-09-02", "X1", "AAPL", 10, 105.0, source="backfill"),
            _row("2026-09-03", "X1", "AAPL", 10, 110.0),
            _row("2026-09-04", "X1", "AAPL", 10, 121.0)]
    s = build_trend_series(rows)
    assert s["estimated_dates"] == ["2026-09-02"]
    assert s["value"] == [1000.0, 1050.0, 1100.0, 1210.0]
    assert s["investment_return_pct"][1] is None            # not computed across an estimated day
    assert round(s["investment_return_pct"][3], 2) == 10.0  # chain restarts at the next observed pair


def test_multi_account_rows_sum_per_date():
    rows = [_row("2026-09-01", "X1", "AAPL", 10, 100.0), _row("2026-09-01", "X2", "AAPL", 20, 100.0)]
    s = build_trend_series(rows)
    assert s["value"] == [3000.0] and s["ticker_value"]["AAPL"]["2026-09-01"] == 3000.0


def test_normalize_index_series():
    out = normalize_index_series({"^GSPC": {"2026-09-01": 100.0, "2026-09-02": 103.0}})
    assert out["^GSPC"]["2026-09-02"] == 3.0


def test_summary_keeps_unknown_values_unknown():
    holdings = [
        {"ticker": "AAPL", "current_value": 1000.0, "cost_basis_total": 800.0, "current_price": 100.0,
         "broker": "fidelity", "account_number": "X1", "sector": "Tech", "as_of_date": "2026-09-20",
         "synced_at": "2026-09-21T10:00:00", "price_as_of": "2026-09-19"},
        {"ticker": "ZZZZ", "current_value": None, "cost_basis_total": None, "current_price": None,
         "broker": "fidelity", "account_number": "X1", "sector": "", "as_of_date": "2026-09-20",
         "synced_at": "2026-09-21T10:00:00", "price_as_of": None},
    ]
    s = summarize_holdings(holdings, [{"amount": 250.0}])
    assert s["securities_value"] == 1000.0 and s["valued_count"] == 1 and s["unvalued_count"] == 1
    assert s["unvalued_tickers"] == ["ZZZZ"] and s["cash_balance"] == 250.0
    assert s["freshness"]["price_missing"] == 1 and s["freshness"]["price_as_of"]["oldest"] == "2026-09-19"
    rows = holdings_table_rows(holdings)
    assert rows[1]["current_value"] is None and rows[1]["gain"] is None   # never coerced to 0
    assert rows[0]["account_id"] == account_id("fidelity", "X1") == "fidelity:X1"


def test_accounts_with_same_nickname_keep_distinct_ids():
    hs = [{"ticker": "A", "broker": "fidelity", "account_number": "111111", "account_name": "Brokerage"},
          {"ticker": "B", "broker": "fidelity", "account_number": "222222", "account_name": "Brokerage"}]
    accts = group_holdings_by_account(hs)
    assert {a["account_id"] for a in accts} == {"fidelity:111111", "fidelity:222222"}
    assert {a["masked"] for a in accts} == {"···1111", "···2222"}


FIDELITY_CSV = """Account Number,Account Name,Symbol,Description,Quantity,Last Price,Current Value,Cost Basis Total,Average Cost Basis
X12345678,Individual - TOD,AAPL,APPLE INC,10,$100.00,"$1,000.00",$800.00,$80.00
X12345678,Individual - TOD,SPAXX**,FIDELITY GOVERNMENT MONEY MARKET,,,"$2,500.50",,
X12345678,Individual - TOD,Pending Activity,,,,"$12.00",,

"The data and information in this spreadsheet is provided to you solely for your use"
"09/19/2026 4:01 PM ET"
"""


def test_fidelity_parser_captures_cash_and_statement_date():
    rows = parse_fidelity_csv(FIDELITY_CSV)
    securities, cash = split_cash_rows(rows)
    assert [r["ticker"] for r in securities] == ["AAPL"]
    assert securities[0]["as_of_date"] == "2026-09-19"
    assert sorted(c["ticker"] for c in cash) == ["PENDING ACTIVITY", "SPAXX**"]
    assert round(sum(c["current_value"] for c in cash), 2) == 2512.5
    assert all(c["account_number"] == "X12345678" for c in cash)
