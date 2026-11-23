"""
Every new connection runs the holdings schema setup. Under several web sessions or a batch plus the web, that setup used to
drop and rebuild the `holdings` view on each connection, so a second connection could fail with "no such view: holdings".
"""
from __future__ import annotations

import sqlite3
import threading

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.tools import holdings_db as repo


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")


def test_concurrent_connections_never_fail_on_the_holdings_view():
    repo.get_portfolio_by_owner("someone")                       # first setup creates the schema
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker():
        try:
            barrier.wait()
            for _ in range(15):
                repo.get_portfolio_by_owner("someone")
                repo.get_holdings()
        except BaseException as exc:                              # noqa: BLE001 - the test reports whatever failed
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_an_up_to_date_view_is_left_alone_and_a_stale_one_is_rebuilt():
    repo.get_portfolio_by_owner("someone")
    conn = sqlite3.connect(str(db_module.DB_PATH))
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    repo._create_schema(conn)
    assert not [s for s in statements if "VIEW holdings" in s.upper()]      # nothing dropped, nothing re-created
    conn.execute("DROP VIEW holdings")
    conn.execute("CREATE VIEW holdings AS SELECT 1 AS id")                   # an out-of-date definition
    conn.commit()
    repo._create_schema(conn)
    assert repo._view_is_current(conn) and conn.execute("SELECT COUNT(*) FROM holdings").fetchone()[0] == 0
    conn.close()
