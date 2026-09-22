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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS portfolio_price_history (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            date         TEXT NOT NULL,
            ticker       TEXT NOT NULL,
            close_price  REAL,
            shares       REAL,
            market_value REAL,
            cost_basis   REAL,
            broker       TEXT,
            account_name TEXT,
            UNIQUE(date, ticker, broker)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pph_date ON portfolio_price_history (date)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pph_ticker ON portfolio_price_history (ticker)"
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
    """
    Replace holdings for just the (broker, account_number) pairs present in
    *holdings* — accounts at this broker not present in this upload are left
    untouched. A blank/missing account_number is its own bucket (rows with no
    account number get replaced together), same as before this was scoped.
    """
    account_numbers = {(h.get("account_number") or "").strip() for h in holdings}
    with _db() as conn:
        for acct_num in account_numbers:
            conn.execute(
                "DELETE FROM holdings WHERE broker = ? AND COALESCE(account_number, '') = ?",
                [broker, acct_num],
            )
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
                (h.get("account_number") or "").strip(),
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
    """Remove every holding from this broker, across all accounts."""
    with _db() as conn:
        cursor = conn.execute(
            "DELETE FROM holdings WHERE broker = ?", [broker]
        )
        conn.commit()
    return cursor.rowcount


def delete_account_holdings(broker: str, account_number: str) -> int:
    """Remove holdings from a single (broker, account_number) pair."""
    with _db() as conn:
        cursor = conn.execute(
            "DELETE FROM holdings WHERE broker = ? AND COALESCE(account_number, '') = ?",
            [broker, (account_number or "").strip()],
        )
        conn.commit()
    return cursor.rowcount


def delete_holding(ticker: str, broker: Optional[str] = None) -> int:
    """Remove a single position (e.g. after selling out of it), optionally scoped to one broker/account."""
    with _db() as conn:
        if broker:
            cursor = conn.execute(
                "DELETE FROM holdings WHERE ticker = ? AND broker = ?", [ticker, broker]
            )
        else:
            cursor = conn.execute("DELETE FROM holdings WHERE ticker = ?", [ticker])
        conn.commit()
    return cursor.rowcount


def snapshot_portfolio_prices(as_of_date: str | None = None) -> dict:
    """
    Fetch the last closing price for every current holding via yfinance and
    write one row per (date, ticker, broker) into portfolio_price_history.
    Called by the evening batch pipeline.
    """
    from datetime import date as _date
    import yfinance as yf

    today = as_of_date or _date.today().isoformat()
    holdings = get_holdings()
    if not holdings:
        return {"snapped": 0, "date": today}

    tickers = list({h["ticker"] for h in holdings if h.get("ticker")})

    # Fetch last close in one yfinance call
    prices: dict[str, float] = {}
    try:
        for chunk in [tickers[i:i+20] for i in range(0, len(tickers), 20)]:
            data = yf.download(
                " ".join(chunk), period="5d", interval="1d",
                auto_adjust=True, progress=False,
                group_by="ticker",
            )
            for t in chunk:
                try:
                    col = data["Close"] if len(chunk) == 1 else data[t]["Close"]
                    prices[t] = float(col.dropna().iloc[-1])
                except Exception:
                    pass
    except Exception:
        pass

    rows = []
    for h in holdings:
        t = h.get("ticker")
        if not t:
            continue
        cp = prices.get(t)
        shares = h.get("shares")
        mv = round(cp * shares, 2) if cp and shares else None
        cb = h.get("cost_basis_total")
        rows.append((
            today, t,
            round(cp, 4) if cp else None,
            shares, mv, cb,
            h.get("broker"), h.get("account_name"),
        ))

    with _db() as conn:
        conn.executemany(
            """INSERT OR REPLACE INTO portfolio_price_history
               (date, ticker, close_price, shares, market_value, cost_basis,
                broker, account_name)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()

    return {"snapped": len(rows), "date": today}


def get_price_history(
    tickers: list[str] | None = None,
    brokers: list[str] | None = None,
) -> list[dict]:
    """Return all price history rows, optionally filtered by ticker/broker."""
    with _db() as conn:
        clauses, params = [], []
        if tickers:
            clauses.append(f"ticker IN ({','.join('?'*len(tickers))})")
            params.extend(tickers)
        if brokers:
            clauses.append(f"broker IN ({','.join('?'*len(brokers))})")
            params.extend(brokers)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM portfolio_price_history {where} ORDER BY date, ticker",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def backfill_missing_price_snapshots() -> dict:
    """
    Find trading days between the earliest holding load date and yesterday that
    have no price snapshot, then fetch historical closing prices from yfinance
    and insert the missing rows.  Safe to call from any batch — it is a no-op
    when history is already complete and only fetches for confirmed prior days.
    """
    from datetime import date as _date, timedelta
    import yfinance as yf

    holdings = get_holdings()
    if not holdings:
        return {"backfilled": 0, "missing_days": 0}

    today = _date.today()
    yesterday = today - timedelta(days=1)

    # Earliest date we had any holding data — prefer as_of_date, fallback synced_at
    date_strs = []
    for h in holdings:
        d = (h.get("as_of_date") or h.get("synced_at") or "")[:10]
        if d:
            date_strs.append(d)
    if not date_strs:
        return {"backfilled": 0, "missing_days": 0}

    earliest = _date.fromisoformat(min(date_strs))
    if earliest > yesterday:
        return {"backfilled": 0, "missing_days": 0}

    # Dates already covered in the DB
    with _db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT date FROM portfolio_price_history"
        ).fetchall()
    covered = {r["date"] for r in rows}

    # Business days (Mon–Fri) between earliest and yesterday as a proxy for
    # trading days; yfinance simply returns no row for market holidays, so we
    # never write phantom data.
    try:
        import pandas as pd
        bdays = [
            d.date().isoformat()
            for d in pd.bdate_range(start=earliest, end=yesterday)
        ]
    except Exception:
        # Fallback: walk calendar days, skip weekends
        bdays = []
        cur = earliest
        while cur <= yesterday:
            if cur.weekday() < 5:
                bdays.append(cur.isoformat())
            cur += timedelta(days=1)

    missing = [d for d in bdays if d not in covered]
    if not missing:
        return {"backfilled": 0, "missing_days": 0}

    # Build a lookup: ticker → most-recent holding snapshot
    holding_map: dict[str, dict] = {}
    for h in holdings:
        t = h.get("ticker")
        if t:
            holding_map[t] = h
    tickers = list(holding_map)

    # Fetch the full date range in one yfinance call per chunk
    start_str = min(missing)
    end_str   = (_date.fromisoformat(max(missing)) + timedelta(days=1)).isoformat()

    prices_by_ticker_date: dict[str, dict[str, float]] = {t: {} for t in tickers}
    try:
        for chunk in [tickers[i:i+20] for i in range(0, len(tickers), 20)]:
            data = yf.download(
                " ".join(chunk),
                start=start_str,
                end=end_str,
                interval="1d",
                auto_adjust=True,
                progress=False,
                group_by="ticker",
            )
            for t in chunk:
                try:
                    close_col = data["Close"] if len(chunk) == 1 else data[t]["Close"]
                    for idx, val in close_col.dropna().items():
                        d_str = idx.date().isoformat() if hasattr(idx, "date") else str(idx)[:10]
                        prices_by_ticker_date[t][d_str] = float(val)
                except Exception:
                    pass
    except Exception as exc:
        return {"backfilled": 0, "missing_days": len(missing), "error": str(exc)}

    rows_to_insert = []
    for d_str in missing:
        for t, h in holding_map.items():
            cp = prices_by_ticker_date.get(t, {}).get(d_str)
            if cp is None:
                continue  # market holiday or simply no data — skip
            shares = h.get("shares")
            mv = round(cp * shares, 2) if shares else None
            cb = h.get("cost_basis_total")
            rows_to_insert.append((
                d_str, t,
                round(cp, 4),
                shares, mv, cb,
                h.get("broker"), h.get("account_name"),
            ))

    if rows_to_insert:
        with _db() as conn:
            conn.executemany(
                """INSERT OR REPLACE INTO portfolio_price_history
                   (date, ticker, close_price, shares, market_value, cost_basis,
                    broker, account_name)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                rows_to_insert,
            )
            conn.commit()

    return {"backfilled": len(rows_to_insert), "missing_days": len(missing)}


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
