"""
Fund data storage.

SHARED tables (market data about a fund — no owner, safe for the shared forecast path to read):
  fund_profiles             one row per fund instrument: cost, family, category, size, share class, how much of the fund the
                            stored holdings explain (holdings_coverage), when / from where it was fetched, and the reason
                            codes for any piece the provider could not supply.
  fund_holdings             the holdings the provider gave (yfinance: top 10), with whether each is itself a fund.
  fund_sector_weights       sector -> weight.
  fund_asset_class_weights  stock / bond / cash / other -> weight.

The owner-scoped manual breakdown lives in tools.fund_manual_db, a separate module on purpose: this one is
readable by the shared forecast path (tools.instrument_reference), so it holds no private data and no way to reach any.

Fund instruments are keyed by instruments.instrument_id (the security's permanent identity), so a ticker change does not
orphan its data. Weights are fractions.

Data layer only. Writes happen through services.fund_data_service (refresh).
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from portfolio_agent.tools.db import db_conn
from portfolio_agent.tools.fund_data_providers import FundData, FundHolding, FundProfile

_schema_ready: set[str] = set()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fund_profiles (
            instrument_id      INTEGER PRIMARY KEY,
            expense_ratio      REAL,
            fund_family        TEXT,
            category           TEXT,
            aum                REAL,
            share_class        TEXT,
            share_class_group  TEXT,
            holdings_coverage  REAL NOT NULL DEFAULT 0,
            as_of              TEXT,
            source             TEXT,
            fetched_at         TEXT NOT NULL,
            reason_codes_json  TEXT NOT NULL DEFAULT '[]'
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fund_profiles_group ON fund_profiles(share_class_group)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fund_holdings (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            fund_instrument_id  INTEGER NOT NULL,
            holding_symbol      TEXT,
            holding_name        TEXT,
            cusip               TEXT,
            weight              REAL NOT NULL,
            holding_is_fund     INTEGER NOT NULL DEFAULT 0,
            as_of               TEXT,
            source              TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fund_holdings_fund ON fund_holdings(fund_instrument_id)")
    for table in ("fund_sector_weights", "fund_asset_class_weights"):
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {table} (
                fund_instrument_id  INTEGER NOT NULL,
                key                 TEXT NOT NULL,
                weight              REAL NOT NULL,
                as_of               TEXT,
                source              TEXT,
                PRIMARY KEY (fund_instrument_id, key)
            )""")


def _ensure_once() -> None:
    """Create the tables once per process per database file (reads are on the hot path of every exposures build)."""
    from portfolio_agent.tools import db as _db_module
    key = str(_db_module.DB_PATH)
    if key not in _schema_ready:
        with db_conn(setup=lambda c: (_create_schema(c), c.commit())):
            pass
        _schema_ready.add(key)


@contextmanager
def _conn(conn: sqlite3.Connection | None):
    """The caller's connection (never committed here), or a fresh one committed on success."""
    _ensure_once()
    if conn is not None:
        yield conn
        return
    with db_conn() as c:
        try:
            yield c
        except BaseException:
            c.rollback()
            raise
        else:
            c.commit()


# ── Shared fund data ──────────────────────────────────────────────────────────

def save_fund_data(instrument_id: int, data: FundData, *, fetched_at: str | None = None,
                   conn: sqlite3.Connection | None = None) -> None:
    """Replace everything stored for one fund, in one transaction, so a reader never sees a half-written fund."""
    fetched_at = fetched_at or _now()
    p = data.profile or FundProfile()
    with _conn(conn) as c:
        c.execute(
            """INSERT INTO fund_profiles (instrument_id, expense_ratio, fund_family, category, aum, share_class,
                                          share_class_group, holdings_coverage, as_of, source, fetched_at, reason_codes_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(instrument_id) DO UPDATE SET
                 expense_ratio = excluded.expense_ratio, fund_family = excluded.fund_family, category = excluded.category,
                 aum = excluded.aum, share_class = excluded.share_class, share_class_group = excluded.share_class_group,
                 holdings_coverage = excluded.holdings_coverage, as_of = excluded.as_of, source = excluded.source,
                 fetched_at = excluded.fetched_at, reason_codes_json = excluded.reason_codes_json""",
            [instrument_id, p.expense_ratio, p.fund_family, p.category, p.aum, p.share_class, p.share_class_group,
             data.holdings_coverage, data.as_of or fetched_at[:10], data.source, fetched_at,
             json.dumps(list(data.reason_codes))])
        c.execute("DELETE FROM fund_holdings WHERE fund_instrument_id = ?", [instrument_id])
        c.executemany(
            "INSERT INTO fund_holdings (fund_instrument_id, holding_symbol, holding_name, cusip, weight, holding_is_fund, as_of, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(instrument_id, h.symbol, h.name, h.cusip, h.weight, int(h.holding_is_fund), data.as_of or fetched_at[:10], data.source)
             for h in data.holdings])
        for table, weights in (("fund_sector_weights", data.sector_weights), ("fund_asset_class_weights", data.asset_class_weights)):
            c.execute(f"DELETE FROM {table} WHERE fund_instrument_id = ?", [instrument_id])
            c.executemany(f"INSERT INTO {table} (fund_instrument_id, key, weight, as_of, source) VALUES (?, ?, ?, ?, ?)",
                          [(instrument_id, k, w, data.as_of or fetched_at[:10], data.source) for k, w in weights.items()])


def get_fund_profile_row(instrument_id: int, *, conn: sqlite3.Connection | None = None) -> dict | None:
    with _conn(conn) as c:
        r = c.execute("SELECT * FROM fund_profiles WHERE instrument_id = ?", [instrument_id]).fetchone()
    if r is None:
        return None
    d = dict(r)
    d["reason_codes"] = json.loads(d.pop("reason_codes_json") or "[]")
    return d


def get_fund_data(instrument_id: int, ticker: str = "", *, conn: sqlite3.Connection | None = None) -> FundData | None:
    """The stored FundData for an instrument, or None when nothing has been fetched for it."""
    with _conn(conn) as c:
        prof = get_fund_profile_row(instrument_id, conn=c)
        if prof is None:
            return None
        holdings = [FundHolding(symbol=r["holding_symbol"], name=r["holding_name"], weight=r["weight"], cusip=r["cusip"],
                                holding_is_fund=bool(r["holding_is_fund"]))
                    for r in c.execute("SELECT * FROM fund_holdings WHERE fund_instrument_id = ? ORDER BY weight DESC",
                                       [instrument_id]).fetchall()]
        sectors = {r["key"]: r["weight"] for r in c.execute(
            "SELECT key, weight FROM fund_sector_weights WHERE fund_instrument_id = ?", [instrument_id]).fetchall()}
        classes = {r["key"]: r["weight"] for r in c.execute(
            "SELECT key, weight FROM fund_asset_class_weights WHERE fund_instrument_id = ?", [instrument_id]).fetchall()}
    return FundData(
        ticker=ticker.upper(), profile=FundProfile(
            expense_ratio=prof["expense_ratio"], fund_family=prof["fund_family"], category=prof["category"], aum=prof["aum"],
            share_class=prof["share_class"], share_class_group=prof["share_class_group"]),
        holdings=holdings, sector_weights=sectors, asset_class_weights=classes, holdings_coverage=prof["holdings_coverage"],
        as_of=prof["as_of"], source=prof["source"] or "", reason_codes=prof["reason_codes"])


def get_fund_profile_rows(instrument_ids: list[int], *, conn: sqlite3.Connection | None = None) -> dict[int, dict]:
    ids = sorted({int(i) for i in instrument_ids})
    if not ids:
        return {}
    with _conn(conn) as c:
        rows = c.execute(f"SELECT * FROM fund_profiles WHERE instrument_id IN ({','.join('?' * len(ids))})", ids).fetchall()
    out = {}
    for r in rows:
        d = dict(r)
        d["reason_codes"] = json.loads(d.pop("reason_codes_json") or "[]")
        out[d["instrument_id"]] = d
    return out


def fund_data_version(instrument_ids: list[int], *, conn: sqlite3.Connection | None = None) -> str:
    """A short fingerprint of when the given funds' data was last fetched — part of the exposures cache key, so a
    refresh produces a new key and the cached result is recomputed."""
    rows = get_fund_profile_rows(instrument_ids, conn=conn)
    return "|".join(f"{i}:{rows[i]['fetched_at']}" for i in sorted(rows)) or "none"


def list_profiles_in_group(share_class_group: str, *, conn: sqlite3.Connection | None = None) -> list[dict]:
    """Every stored fund profile in one share-class group (joined to its ticker), for the cheaper-class note."""
    with _conn(conn) as c:
        rows = c.execute(
            "SELECT p.*, i.ticker FROM fund_profiles p JOIN instruments i ON i.instrument_id = p.instrument_id "
            "WHERE p.share_class_group = ?", [share_class_group]).fetchall()
    return [dict(r) for r in rows]
