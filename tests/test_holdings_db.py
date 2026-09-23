from __future__ import annotations

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.services.account_service import (
    DuplicatePosition, NotFound, remove_position, replace_account_snapshot, set_position,
)
from portfolio_agent.tools import holdings_db as repo
from portfolio_agent.tools.holdings_db import get_holdings
from tests.helpers_holdings import CTX, account_id_of, seed_holding


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")


def _pos(ticker, shares, account="Brokerage", **extra):
    return {
        "ticker": ticker, "shares": shares, "avg_cost": 100.0,
        "cost_basis_total": shares * 100.0,
        "account_name": account, "account_number": account, "account_type": "TAXABLE",
        **extra,
    }


def _tickers(broker=None):
    return sorted(h["ticker"] for h in get_holdings(broker))


def test_add_holding_keeps_existing_positions_in_same_account():
    # The reported bug: adding MSFT after AAPL used to leave only MSFT.
    seed_holding(_pos("AAPL", 10), "manual", "2026-09-22")
    seed_holding(_pos("MSFT", 5), "manual", "2026-09-22")
    assert _tickers("manual") == ["AAPL", "MSFT"]


def test_add_holding_across_manual_accounts_are_distinct_buckets():
    seed_holding(_pos("AAPL", 10, account="Brokerage"), "manual", "2026-09-22")
    seed_holding(_pos("AAPL", 3, account="Roth"), "manual", "2026-09-22")
    rows = get_holdings("manual")
    assert {(h["account_number"], h["shares"]) for h in rows} == {("Brokerage", 10.0), ("Roth", 3.0)}


def test_add_holding_rejects_duplicate_ticker_in_same_account():
    seed_holding(_pos("AAPL", 10), "manual", "2026-09-22")
    with pytest.raises(DuplicatePosition):
        seed_holding(_pos("AAPL", 2), "manual", "2026-09-22")
    assert len(get_holdings("manual")) == 1


def test_update_holding_changes_only_that_row():
    a = seed_holding(_pos("AAPL", 10), "manual", "2026-09-22")
    seed_holding(_pos("MSFT", 5), "manual", "2026-09-22")
    after = set_position(CTX, a["id"], expected_version=a["version"], shares=12.0, avg_cost=110.0)
    assert after["version"] == a["version"] + 1 and after["cost_basis_total"] == 1320.0
    by_ticker = {h["ticker"]: h for h in get_holdings("manual")}
    assert by_ticker["AAPL"]["shares"] == 12.0
    assert by_ticker["AAPL"]["cost_basis_total"] == 1320.0
    assert by_ticker["MSFT"]["shares"] == 5.0


def test_update_position_refuses_identity_columns_and_unknown_ids():
    a = seed_holding(_pos("AAPL", 10), "manual", "2026-09-22")
    with pytest.raises(ValueError):
        repo.update_position(a["id"], account_id=5)          # identity column
    with pytest.raises(NotFound):
        set_position(CTX, 999_999, expected_version=None, shares=1.0, avg_cost=1.0)


def test_replace_account_snapshot_scoped_to_that_account_and_keeps_ids():
    seed_holding(_pos("AAPL", 10, account="Brokerage"), "manual", "2026-09-22")
    vti = seed_holding(_pos("VTI", 50, account="X1"), "fidelity", "2026-09-22")
    seed_holding(_pos("SPY", 1, account="X2"), "fidelity", "2026-09-22")
    x1 = account_id_of("fidelity", "X1")
    # Re-upload for X1 replaces X1's complete snapshot, leaves X2 and manual alone.
    with repo.transaction() as conn:
        res = replace_account_snapshot(CTX, x1, [_pos("QQQ", 7, account="X1"), _pos("VTI", 55, account="X1")],
                                       as_of_date="2026-09-23", conn=conn)
    assert res == {"added": 1, "updated": 1, "removed": 0, "unchanged": 0, "positions": 2}
    assert _tickers("fidelity") == ["QQQ", "SPY", "VTI"]
    assert _tickers("manual") == ["AAPL"]
    # The VTI row was updated in place: same position id, higher version.
    vti_after = repo.get_position(vti["id"])
    assert vti_after["shares"] == 55 and vti_after["version"] == vti["version"] + 1


def test_delete_holding_by_id_removes_single_row():
    a = seed_holding(_pos("AAPL", 10), "manual", "2026-09-22")
    seed_holding(_pos("MSFT", 5), "manual", "2026-09-22")
    assert remove_position(CTX, a["id"])["ticker"] == "AAPL"
    assert _tickers("manual") == ["MSFT"]


_LEGACY_HOLDINGS_DDL = """CREATE TABLE holdings (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, description TEXT, shares REAL, avg_cost REAL,
    cost_basis_total REAL, current_price REAL, current_value REAL, account_name TEXT, account_number TEXT,
    account_type TEXT, broker TEXT, sector TEXT, as_of_date TEXT, synced_at TEXT DEFAULT (datetime('now')),
    price_as_of TEXT)"""
_LEGACY_CASH_DDL = """CREATE TABLE cash_balances (
    id INTEGER PRIMARY KEY AUTOINCREMENT, broker TEXT NOT NULL, account_number TEXT NOT NULL DEFAULT '',
    account_name TEXT, amount REAL, as_of_date TEXT, detail TEXT, imported_at TEXT DEFAULT (datetime('now')),
    UNIQUE(broker, account_number))"""


def test_legacy_flat_holdings_table_migrates_to_accounts_and_positions():
    """A pre-accounts database: flat holdings + cash_balances rows, manual rows with blank account numbers."""
    import sqlite3 as _sql
    c = _sql.connect(db_module.DB_PATH)
    c.execute(_LEGACY_HOLDINGS_DDL); c.execute(_LEGACY_CASH_DDL)
    c.executemany(
        "INSERT INTO holdings (id, ticker, shares, account_name, account_number, broker, current_value) VALUES (?,?,?,?,?,?,?)",
        [(501, "AAPL", 1, "Brokerage", "", "manual", 100.0), (502, "MSFT", 1, "Roth", "", "manual", None),
         (777, "VTI", 10, "Individual", "X1", "fidelity", 2000.0), (778, "VTI", 20, "IRA", "X2", "fidelity", 4000.0)])
    c.execute("INSERT INTO cash_balances (broker, account_number, account_name, amount) VALUES ('fidelity','X1','Individual',55.5)")
    c.commit(); c.close()

    rows = {(h["broker"], h["account_number"], h["ticker"]): h for h in get_holdings()}   # first open migrates
    # Blank manual account numbers are keyed by name; every row kept its id.
    assert set(rows) == {("manual", "Brokerage", "AAPL"), ("manual", "Roth", "MSFT"),
                         ("fidelity", "X1", "VTI"), ("fidelity", "X2", "VTI")}
    assert rows[("manual", "Roth", "MSFT")]["id"] == 502 and rows[("fidelity", "X2", "VTI")]["id"] == 778
    # Accounts became records under the local portfolio, cash moved with its account.
    accts = {(a["broker"], a["account_number"]): a for a in repo.list_accounts()}
    assert len(accts) == 4 and accts[("fidelity", "X1")]["display_name"] == "Individual"
    assert repo.get_cash_balances() == [{**repo.get_cash_balances()[0], "account_id": accts[("fidelity", "X1")]["account_id"], "amount": 55.5}]
    # Old tables were kept as backups, and `holdings` is now the view.
    with repo.unit_of_work() as conn:
        kinds = dict(conn.execute("SELECT name, type FROM sqlite_master WHERE name IN "
                                  "('holdings','holdings_legacy_v1','cash_balances','cash_balances_legacy_v1')").fetchall())
    assert kinds == {"holdings": "view", "holdings_legacy_v1": "table", "cash_balances_legacy_v1": "table"}


# ── Daily price history: account-level grain ─────────────────────────────────

import sqlite3
from datetime import date, timedelta

import pandas as pd
import yfinance

from portfolio_agent.tools.holdings_db import (
    HISTORY_SOURCE_ATTRIBUTED, HISTORY_SOURCE_BACKFILL, HISTORY_SOURCE_LEGACY, HISTORY_SOURCE_RECONSTRUCTED,
    HISTORY_SOURCE_SNAPSHOT, get_history_coverage, get_price_history, repair_legacy_history,
)
from portfolio_agent.tools.holdings_prices import backfill_missing_price_snapshots, snapshot_portfolio_prices


def _fake_download(closes: dict[str, float]):
    """yfinance.download stand-in: constant close per ticker over the requested range."""
    def fake(tickers, **kw):
        syms = tickers.split()
        if kw.get("start"):
            idx = pd.bdate_range(start=kw["start"], end=pd.Timestamp(kw["end"]) - pd.Timedelta(days=1))
        else:
            idx = pd.DatetimeIndex([pd.Timestamp(date.today())])
        if len(syms) == 1:
            return pd.DataFrame({"Close": [closes[syms[0]]] * len(idx)}, index=idx)
        cols = pd.MultiIndex.from_tuples([(s, "Close") for s in syms])
        return pd.DataFrame([[closes[s] for s in syms]] * len(idx), index=idx, columns=cols)
    return fake


def _seed_two_fidelity_accounts():
    seed_holding(_pos("AAPL", 10, account="X1"), "fidelity", "2026-09-22")   # $1,000 @ 100
    seed_holding(_pos("AAPL", 20, account="X2"), "fidelity", "2026-09-22")   # $2,000 @ 100


def test_snapshot_keeps_one_row_per_account(monkeypatch):
    # The reported bug: two Fidelity accounts in AAPL collapsed to the last one.
    _seed_two_fidelity_accounts()
    monkeypatch.setattr(yfinance, "download", _fake_download({"AAPL": 100.0}))
    res = snapshot_portfolio_prices()
    rows = get_price_history(tickers=["AAPL"])
    assert res["snapped"] == 2 and len(rows) == 2
    assert sum(r["market_value"] for r in rows) == 3000.0
    assert {r["account_number"] for r in rows} == {"X1", "X2"}
    assert {r["source"] for r in rows} == {HISTORY_SOURCE_SNAPSHOT}
    # Re-running the same day refreshes rather than duplicates.
    snapshot_portfolio_prices()
    assert len(get_price_history(tickers=["AAPL"])) == 2


def _create_legacy_history(path, rows):
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE portfolio_price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, ticker TEXT NOT NULL,
            close_price REAL, shares REAL, market_value REAL, cost_basis REAL,
            broker TEXT, account_name TEXT, UNIQUE(date, ticker, broker))""")
    conn.executemany(
        "INSERT INTO portfolio_price_history (date, ticker, close_price, shares, market_value, cost_basis, broker, account_name) "
        "VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.commit(); conn.close()


def test_legacy_table_migrates_to_account_grain_and_flags_rows():
    _create_legacy_history(db_module.DB_PATH, [
        ("2026-09-01", "AAPL", 100.0, 20, 2000.0, 2000.0, "fidelity", "IRA"),
    ])
    rows = get_price_history()                       # opening the DB runs the migration
    assert len(rows) == 1
    assert rows[0]["account_number"] == "" and rows[0]["source"] == HISTORY_SOURCE_LEGACY
    cov = get_history_coverage()
    assert cov["by_source"] == {HISTORY_SOURCE_LEGACY: 1}
    assert cov["legacy_dates"] == ["2026-09-01"]


def test_repair_rebuilds_collapsed_rows_and_rekeys_single_account_rows():
    _create_legacy_history(db_module.DB_PATH, [
        ("2026-09-01", "AAPL", 100.0, 20, 2000.0, 2000.0, "fidelity", "IRA"),   # collapsed: 2 accts
        ("2026-09-01", "VTI", 250.0, 4, 1000.0, 900.0, "vanguard", "Vanguard"), # 1 acct → rekey
        ("2026-09-01", "GONE", 5.0, 1, 5.0, 5.0, "fidelity", "IRA"),            # no owner → flagged
    ])
    _seed_two_fidelity_accounts()
    seed_holding(_pos("VTI", 4, account="V1"), "vanguard", "2026-09-22")

    res = repair_legacy_history()
    assert res == {"rekeyed": 1, "rebuilt": 1, "unresolved": 1, "estimates_replaced": 0, "phantoms_removed": 0,
                   "legacy_rows_seen": 3}

    aapl = get_price_history(tickers=["AAPL"])
    assert {(r["account_number"], r["market_value"]) for r in aapl} == {("X1", 1000.0), ("X2", 2000.0)}
    assert sum(r["market_value"] for r in aapl) == 3000.0           # combined history restored
    vti = get_price_history(tickers=["VTI"])[0]
    # Single owner: the observed row is attributed, its recorded values untouched.
    assert vti["account_number"] == "V1" and vti["source"] == HISTORY_SOURCE_ATTRIBUTED
    assert vti["shares"] == 4 and vti["market_value"] == 1000.0 and vti["cost_basis"] == 900.0
    gone = get_price_history(tickers=["GONE"])[0]
    assert gone["source"] == HISTORY_SOURCE_LEGACY
    assert repair_legacy_history()["rebuilt"] == 0                   # idempotent


def test_backfill_fills_partial_days_per_position_without_overwriting(monkeypatch):
    # Holdings loaded 3 business days ago so backfill has days to consider.
    start = (pd.Timestamp(date.today()) - pd.tseries.offsets.BDay(3)).date().isoformat()
    seed_holding(_pos("AAPL", 10, account="X1"), "fidelity", start)
    seed_holding(_pos("AAPL", 20, account="X2"), "fidelity", start)
    # Day `start` already has an (estimated) row for X1 only → partial day.
    from portfolio_agent.tools.holdings_db import _db, _PRICE_HISTORY_INSERT
    with _db() as conn:
        conn.execute(_PRICE_HISTORY_INSERT.format(conflict="REPLACE"),
                     (start, "fidelity", "X1", "AAPL", "X1", 111.0, 10, 1110.0, 1000.0, HISTORY_SOURCE_BACKFILL))
    assert get_history_coverage()["partial_days"] == 1

    monkeypatch.setattr(yfinance, "download", _fake_download({"AAPL": 100.0}))
    res = backfill_missing_price_snapshots()
    assert res["backfilled"] >= 1 and res["missing_days"] >= 1

    day_rows = [r for r in get_price_history() if r["date"] == start]
    by_acct = {r["account_number"]: r for r in day_rows}
    assert by_acct["X1"]["close_price"] == 111.0                     # existing row untouched
    assert by_acct["X1"]["source"] == HISTORY_SOURCE_BACKFILL
    assert by_acct["X2"]["market_value"] == 2000.0 and by_acct["X2"]["source"] == HISTORY_SOURCE_BACKFILL
    cov = get_history_coverage()
    assert cov["partial_days"] == 0 and cov["complete_days"] == cov["days"]
    assert backfill_missing_price_snapshots()["backfilled"] == 0    # now a no-op


def test_transaction_rolls_back_every_write_on_error():
    a = seed_holding(_pos("AAPL", 10), "fidelity", "2026-09-01")
    acct = account_id_of("fidelity", "Brokerage")
    with pytest.raises(RuntimeError):
        with repo.transaction() as conn:
            repo.update_position(a["id"], shares=99.0, conn=conn)
            repo.set_account_cash(acct, 5.0, "2026-09-02", conn=conn)
            # Visible on the owning connection before the failure...
            assert conn.execute("SELECT shares FROM positions WHERE position_id = ?", [a["id"]]).fetchone()[0] == 99
            raise RuntimeError("boom")
    # ...and gone afterwards, for both tables.
    assert get_holdings("fidelity")[0]["shares"] == 10
    assert repo.get_cash_balances("fidelity") == []


def test_transaction_commits_all_writes_together():
    a = seed_holding(_pos("AAPL", 1), "fidelity", "2026-09-01")
    acct = account_id_of("fidelity", "Brokerage")
    with repo.transaction() as conn:
        repo.update_position(a["id"], shares=2.0, conn=conn)
        repo.set_account_cash(acct, 5.0, "2026-09-01", conn=conn)
    assert get_holdings("fidelity")[0]["shares"] == 2.0
    assert repo.get_cash_balances("fidelity")[0]["amount"] == 5.0


def test_refresh_prices_reprices_every_position_in_one_pass(monkeypatch):
    from portfolio_agent.tools.holdings_prices import refresh_holding_prices
    seed_holding(_pos("AAPL", 10, account="X1", current_price=50.0, current_value=500.0), "fidelity", "2026-09-01")
    seed_holding(_pos("AAPL", 2, account="X2"), "fidelity", "2026-09-01")     # no price yet
    monkeypatch.setattr(yfinance, "download", _fake_download({"AAPL": 120.0}))
    res = refresh_holding_prices()
    assert res["updated"] == 2 and res["failed"] == [] and res["price_as_of"] == date.today().isoformat()
    by_acct = {h["account_number"]: h for h in get_holdings("fidelity")}
    assert (by_acct["X1"]["current_value"], by_acct["X2"]["current_value"]) == (1200.0, 240.0)
    assert by_acct["X2"]["price_as_of"] == date.today().isoformat()


def _legacy_row(date_, ticker, broker, shares, close):
    return (date_, ticker, close, shares, shares * close, shares * close, broker, "Individual")


def test_repair_prefers_the_observation_over_a_standing_estimate():
    """The bug: a legacy (observed) row was deleted because a backfill estimate already sat in its place."""
    _create_legacy_history(db_module.DB_PATH, [_legacy_row("2026-09-01", "AAPL", "fidelity", 10, 100.0)])
    a = seed_holding(_pos("AAPL", 60, account="X1"), "fidelity", "2026-09-22")     # 60 shares TODAY, 10 back then
    repo.insert_price_history([repo.history_row("2026-09-01", a | {"broker": "fidelity", "account_number": "X1"},
                                                100.0, HISTORY_SOURCE_BACKFILL)], replace=True)
    res = repair_legacy_history()
    assert res["rekeyed"] == 1 and res["estimates_replaced"] == 1 and res["unresolved"] == 0
    rows = get_price_history(tickers=["AAPL"])
    assert len(rows) == 1 and rows[0]["source"] == HISTORY_SOURCE_ATTRIBUTED
    assert rows[0]["shares"] == 10 and rows[0]["market_value"] == 1000.0 and rows[0]["account_number"] == "X1"
    # A real per-account OBSERVATION does win over a legacy copy.
    repo.insert_price_history([("2026-09-02", "fidelity", "X1", "AAPL", "X1", 101.0, 10, 1010.0, 1000.0,
                                          HISTORY_SOURCE_SNAPSHOT)], replace=True)
    with repo.unit_of_work() as conn:
        conn.execute(repo._PRICE_HISTORY_INSERT.format(conflict="IGNORE"),
                     ("2026-09-02", "fidelity", "", "AAPL", "Individual", 101.0, 10, 1010.0, 1000.0, HISTORY_SOURCE_LEGACY))
    res = repair_legacy_history()
    day2 = [r for r in get_price_history(tickers=["AAPL"]) if r["date"] == "2026-09-02"]
    assert len(day2) == 1 and day2[0]["source"] == HISTORY_SOURCE_SNAPSHOT and res["estimates_replaced"] == 0


def test_repair_attributes_sold_tickers_to_the_brokers_only_account():
    _create_legacy_history(db_module.DB_PATH, [_legacy_row("2026-09-01", "RIVN", "vanguard", 66, 12.0)])
    seed_holding(_pos("VTI", 1, account="V1"), "vanguard", "2026-09-22")          # RIVN no longer held
    res = repair_legacy_history()
    assert res["rekeyed"] == 1 and res["unresolved"] == 0
    row = get_price_history(tickers=["RIVN"])[0]
    assert row["account_number"] == "V1" and row["source"] == HISTORY_SOURCE_ATTRIBUTED and row["shares"] == 66


def test_snapshot_days_are_complete_so_estimates_on_them_are_phantoms(monkeypatch):
    start = (pd.Timestamp(date.today()) - pd.tseries.offsets.BDay(3)).date().isoformat()
    _create_legacy_history(db_module.DB_PATH, [_legacy_row(start, "AAPL", "fidelity", 10, 100.0)])
    seed_holding(_pos("AAPL", 10, account="X1"), "fidelity", start)
    seed_holding(_pos("MSFT", 5, account="X1"), "fidelity", start)                # bought AFTER `start`
    repo.insert_price_history([
        repo.history_row(start, {"broker": "fidelity", "account_number": "X1", "ticker": "AAPL", "shares": 10}, 100.0, HISTORY_SOURCE_BACKFILL),
        repo.history_row(start, {"broker": "fidelity", "account_number": "X1", "ticker": "MSFT", "shares": 5}, 300.0, HISTORY_SOURCE_BACKFILL),
    ], replace=True)
    res = repair_legacy_history()
    assert res["estimates_replaced"] == 1 and res["phantoms_removed"] == 1
    day = {r["ticker"]: r for r in get_price_history() if r["date"] == start}
    assert set(day) == {"AAPL"} and day["AAPL"]["source"] == HISTORY_SOURCE_ATTRIBUTED
    # Neither coverage nor backfill treats the snapshot day as incomplete afterwards.
    cov = get_history_coverage()
    assert start not in cov["partial_dates"]
    monkeypatch.setattr(yfinance, "download", _fake_download({"AAPL": 100.0, "MSFT": 300.0}))
    backfill_missing_price_snapshots()
    assert {r["ticker"] for r in get_price_history() if r["date"] == start} == {"AAPL"}
