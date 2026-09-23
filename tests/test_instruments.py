"""Instruments: stable identity behind a position; a ticker is a changeable attribute."""
from __future__ import annotations

import sqlite3

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.services import account_service as svc
from portfolio_agent.tools import holdings_db as repo
from tests.helpers_holdings import CTX, seed_holding


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")


def _pos(ticker, shares, account="X1", **extra):
    return {"ticker": ticker, "shares": shares, "avg_cost": 10.0, "cost_basis_total": shares * 10.0,
            "account_name": "Individual", "account_number": account, "account_type": "TAXABLE", **extra}


def test_same_ticker_in_two_accounts_is_one_instrument():
    a = seed_holding(_pos("fb", 1, description="META PLATFORMS"), "fidelity", "2026-09-01")
    b = seed_holding(_pos("FB ", 2, account="X2"), "vanguard", "2026-09-01")
    assert a["instrument_id"] == b["instrument_id"] and a["ticker"] == b["ticker"] == "FB"
    inst = repo.get_instrument(a["instrument_id"])
    assert inst["name"] == "META PLATFORMS" and len(repo.list_instruments()) == 1


def test_change_ticker_keeps_ids_and_relabels_history_in_one_transaction():
    a = seed_holding(_pos("FB", 1), "fidelity", "2026-09-01")
    b = seed_holding(_pos("FB", 2, account="X2"), "fidelity", "2026-09-01")
    repo.insert_price_history([repo.history_row("2026-09-01", h, 300.0, repo.HISTORY_SOURCE_SNAPSHOT)
                               for h in repo.get_holdings()], replace=True)
    inst = svc.change_instrument_ticker(CTX, a["instrument_id"], "meta", expected_version=1, reason="rebrand")
    assert inst["ticker"] == "META" and inst["version"] == 2
    view = {h["id"]: h for h in repo.get_holdings()}
    assert view[a["id"]]["ticker"] == view[b["id"]]["ticker"] == "META"          # every account, same ids
    assert view[a["id"]]["version"] == a["version"]                              # positions themselves untouched
    assert {r["ticker"] for r in repo.get_price_history()} == {"META"}
    assert repo.get_portfolio_tickers() == ["META"]
    assert repo.find_position(a["account_id"], "META")["position_id"] == a["id"]
    assert repo.find_position(a["account_id"], "FB") is None
    ev = repo.get_audit_events()[0]
    assert ev["operation"] == "change_ticker" and ev["before"]["ticker"] == "FB" and ev["after"]["history_rows_moved"] == 2
    assert [(c["old_ticker"], c["new_ticker"]) for c in repo.get_ticker_changes(a["instrument_id"])] == [("FB", "META")]


def test_change_ticker_rejects_collisions_and_stale_versions():
    a = seed_holding(_pos("AAA", 1), "fidelity", "2026-09-01")
    seed_holding(_pos("BBB", 1), "fidelity", "2026-09-01")
    with pytest.raises(svc.DuplicateInstrument):
        svc.change_instrument_ticker(CTX, a["instrument_id"], "BBB")
    with pytest.raises(svc.VersionConflict):
        svc.change_instrument_ticker(CTX, a["instrument_id"], "CCC", expected_version=7)
    with pytest.raises(svc.NotFound):
        svc.change_instrument_ticker(CTX, 999, "CCC")
    assert repo.get_instrument(a["instrument_id"])["ticker"] == "AAA"


def test_reimport_under_new_ticker_matches_the_renamed_instrument():
    a = seed_holding(_pos("FB", 1), "fidelity", "2026-09-01")
    svc.change_instrument_ticker(CTX, a["instrument_id"], "META")
    with repo.transaction() as conn:
        res = svc.replace_account_snapshot(CTX, a["account_id"], [_pos("META", 5)], as_of_date="2026-09-02", conn=conn)
    assert res["updated"] == 1 and res["added"] == 0
    assert repo.get_position(a["id"])["shares"] == 5                          # same position row


_PHASE2_POSITIONS_DDL = """CREATE TABLE positions (
    position_id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL, ticker TEXT NOT NULL,
    description TEXT, shares REAL, avg_cost REAL, cost_basis_total REAL, current_price REAL, current_value REAL,
    sector TEXT, as_of_date TEXT, synced_at TEXT NOT NULL DEFAULT (datetime('now')), price_as_of TEXT,
    source_import_id INTEGER, version INTEGER NOT NULL DEFAULT 1, UNIQUE(account_id, ticker))"""


def test_phase2_positions_table_is_rebuilt_around_instruments():
    """A database from the accounts+positions step, before instruments existed."""
    c = sqlite3.connect(db_module.DB_PATH)
    for ddl in repo._SCHEMA:
        if "CREATE TABLE IF NOT EXISTS positions" in ddl or "instrument" in ddl:
            continue
        c.execute(ddl)
    c.execute(_PHASE2_POSITIONS_DDL)
    c.execute("INSERT INTO portfolios (portfolio_id, owner) VALUES (1, 'local')")
    c.execute("INSERT INTO accounts (account_id, portfolio_id, broker, account_number, display_name) VALUES (1, 1, 'fidelity', 'X1', 'Ind')")
    c.execute("INSERT INTO accounts (account_id, portfolio_id, broker, account_number, display_name) VALUES (2, 1, 'fidelity', 'X2', 'IRA')")
    c.executemany("INSERT INTO positions (position_id, account_id, ticker, description, shares, version, source_import_id) VALUES (?,?,?,?,?,?,?)",
                  [(40001, 1, "AAPL", "APPLE INC", 3, 4, 9), (40002, 2, "AAPL", "Apple", 7, 1, 9), (40003, 1, "VTI", None, 1, 2, None)])
    c.execute("CREATE VIEW holdings AS SELECT position_id AS id, ticker FROM positions")   # the old view definition
    c.commit(); c.close()

    hs = {h["id"]: h for h in repo.get_holdings()}                                   # first open migrates
    assert set(hs) == {40001, 40002, 40003}
    assert hs[40001]["instrument_id"] == hs[40002]["instrument_id"] != hs[40003]["instrument_id"]
    assert (hs[40001]["version"], hs[40001]["source_import_id"], hs[40001]["shares"]) == (4, 9, 3)
    assert repo.get_instrument(hs[40001]["instrument_id"])["name"] in ("APPLE INC", "Apple")
    with repo.unit_of_work() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(positions)")}
        assert "instrument_id" in cols and "ticker" not in cols
        assert conn.execute("SELECT type FROM sqlite_master WHERE name='holdings'").fetchone()[0] == "view"
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='positions_v2_tmp'").fetchone()[0] == 0
    # Reopening is a no-op and the view exposes the instrument's ticker.
    assert sorted(h["ticker"] for h in repo.get_holdings()) == ["AAPL", "AAPL", "VTI"]
