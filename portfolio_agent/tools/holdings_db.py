from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import yaml

from portfolio_agent.domain import Holding
from portfolio_agent.tools.db import db_conn, migrate_columns

_COLUMNS = [
    "id", "ticker", "description", "shares", "avg_cost", "cost_basis_total",
    "current_price", "current_value", "account_name", "account_number",
    "account_type", "broker", "sector", "as_of_date", "synced_at",
]


@contextmanager
def _db():
    def _setup(conn: sqlite3.Connection) -> None:
        _create_schema(conn)
        _migrate(conn)
        conn.commit()
    with db_conn(setup=_setup) as conn:
        yield conn


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS holdings (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker           TEXT NOT NULL,
            description      TEXT,
            shares           REAL,
            avg_cost         REAL,
            cost_basis_total REAL,
            current_price    REAL,
            current_value    REAL,
            account_name     TEXT,
            account_number   TEXT,
            account_type     TEXT,
            broker           TEXT,
            sector           TEXT,
            as_of_date       TEXT,
            synced_at        TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_holdings_broker ON holdings (broker)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_holdings_ticker ON holdings (ticker)"
    )


def _migrate(conn: sqlite3.Connection) -> None:
    migrate_columns(conn, "holdings", [
        ("description",      "TEXT"),
        ("shares",           "REAL"),
        ("avg_cost",         "REAL"),
        ("cost_basis_total", "REAL"),
        ("current_price",    "REAL"),
        ("current_value",    "REAL"),
        ("account_name",     "TEXT"),
        ("account_number",   "TEXT"),
        ("account_type",     "TEXT"),
        ("broker",           "TEXT"),
        ("sector",           "TEXT"),
        ("as_of_date",       "TEXT"),
        ("synced_at",        "TEXT"),
    ])


def upsert_holdings(holdings: list[dict], broker: str, as_of_date: str) -> dict:
    with _db() as conn:
        conn.execute("DELETE FROM holdings WHERE broker = ?", [broker])
        rows_to_insert = []
        for h in holdings:
            rows_to_insert.append((
                h.get("ticker"),
                h.get("description"),
                h.get("shares"),
                h.get("avg_cost"),
                h.get("cost_basis_total"),
                h.get("current_price"),
                h.get("current_value"),
                h.get("account_name"),
                h.get("account_number"),
                h.get("account_type"),
                broker,
                h.get("sector", ""),
                as_of_date,
            ))
        conn.executemany(
            """INSERT INTO holdings
               (ticker, description, shares, avg_cost, cost_basis_total,
                current_price, current_value, account_name, account_number,
                account_type, broker, sector, as_of_date)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows_to_insert,
        )
        conn.commit()
    return {"saved": len(rows_to_insert), "broker": broker}


def get_holdings(broker: Optional[str] = None) -> list[Holding]:
    with _db() as conn:
        if broker:
            rows = conn.execute(
                "SELECT * FROM holdings WHERE broker = ? ORDER BY ticker",
                [broker],
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM holdings ORDER BY ticker"
            ).fetchall()
    return [Holding.from_db_row(dict(r)) for r in rows]


def get_holdings_summary() -> dict:
    with _db() as conn:
        rows = conn.execute("SELECT * FROM holdings").fetchall()

    if not rows:
        return {
            "total_value": 0.0,
            "holding_count": 0,
            "brokers": [],
            "by_broker": {},
            "by_sector": {},
            "last_synced_at": None,
        }

    total_value = 0.0
    brokers: set[str] = set()
    by_broker: dict[str, dict] = {}
    by_sector: dict[str, float] = {}
    last_synced_at = None

    for r in rows:
        row = dict(r)
        broker = row.get("broker") or "unknown"
        sector = row.get("sector") or "Unknown"
        value = row.get("current_value") or 0.0
        synced = row.get("synced_at")

        total_value += value
        brokers.add(broker)

        if broker not in by_broker:
            by_broker[broker] = {"count": 0, "value": 0.0}
        by_broker[broker]["count"] += 1
        by_broker[broker]["value"] += value

        by_sector[sector] = by_sector.get(sector, 0.0) + value

        if synced and (last_synced_at is None or synced > last_synced_at):
            last_synced_at = synced

    return {
        "total_value": round(total_value, 2),
        "holding_count": len(rows),
        "brokers": sorted(brokers),
        "by_broker": {
            b: {"count": v["count"], "value": round(v["value"], 2)}
            for b, v in by_broker.items()
        },
        "by_sector": {s: round(v, 2) for s, v in sorted(by_sector.items())},
        "last_synced_at": last_synced_at,
    }


def delete_broker_holdings(broker: str) -> int:
    with _db() as conn:
        cursor = conn.execute(
            "DELETE FROM holdings WHERE broker = ?", [broker]
        )
        conn.commit()
    return cursor.rowcount


def sync_to_yaml(yaml_path: str) -> None:
    holdings = get_holdings()
    records = []
    for h in holdings:
        record: dict = {"ticker": h["ticker"]}
        if h.get("shares") is not None:
            record["shares"] = h["shares"]
        if h.get("avg_cost") is not None:
            record["avg_cost"] = h["avg_cost"]
        if h.get("sector"):
            record["sector"] = h["sector"]
        if h.get("account_name"):
            record["account"] = h["account_name"]
        if h.get("broker"):
            record["broker"] = h["broker"]
        records.append(record)

    out = {"holdings": records}
    path = Path(yaml_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.dump(out, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
