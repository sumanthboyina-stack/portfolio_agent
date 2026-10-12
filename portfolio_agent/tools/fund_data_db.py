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

from portfolio_agent.tools.db import db_conn, migrate_columns
from portfolio_agent.tools.fund_data_providers import FundData, FundHolding, FundProfile

_schema_ready: set[str] = set()

# Seed rows for fund_issuer_sources. The URLs follow each issuer's published pattern but were NOT verified when they were written (no request
# was made): they are seeded disabled, and an administrator checks one in a browser before enabling it.
SEED_ISSUER_SOURCES: list[tuple[str, str, str, str]] = [
    ("IVV", "ishares", "https://www.ishares.com/us/products/239726/ishares-core-sp-500-etf/1467271812596.ajax?fileType=csv&fileName=IVV_holdings&dataType=fund",
     "iShares Core S&P 500 ETF. URL pattern from iShares' site; unverified."),
    ("SPY", "spdr", "https://www.ssga.com/us/en/intermediary/etfs/library-content/products/fund-data/etfs/us/holdings-daily-us-en-spy.xlsx",
     "SPDR S&P 500 ETF Trust (an .xlsx file). URL pattern from SSGA's site; unverified."),
]


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
    # Columns added after the first release: idempotent, an existing row keeps everything it has.
    migrate_columns(conn, "fund_profiles", [("derivative_short_weight", "REAL NOT NULL DEFAULT 0"),
                                            ("mapped_coverage", "REAL"),
                                            ("source_detail_json", "TEXT")])
    migrate_columns(conn, "fund_holdings", [("isin", "TEXT")])
    # SEC's ticker -> (CIK, series id, class id) table (company_tickers_mf.json): which N-PORT series a fund ticker is. Shared market data.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sec_fund_ids (
            class_id    TEXT PRIMARY KEY,
            symbol      TEXT NOT NULL,
            cik         INTEGER NOT NULL,
            series_id   TEXT NOT NULL,
            fetched_at  TEXT NOT NULL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sec_fund_ids_symbol ON sec_fund_ids(symbol)")
    # CUSIP / ISIN -> ticker, one row per identifier, INCLUDING "looked up and not found" (so a miss is not asked again until it is
    # due). Shared market data: nothing here is about any person.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS security_identifiers (
            id_type        TEXT NOT NULL CHECK (id_type IN ('CUSIP', 'ISIN')),
            id_value       TEXT NOT NULL,
            status         TEXT NOT NULL CHECK (status IN ('mapped', 'not_found')),
            ticker         TEXT,
            name           TEXT,
            exch_code      TEXT,
            security_type  TEXT,
            figi           TEXT,
            source         TEXT NOT NULL DEFAULT 'openfigi',
            resolved_at    TEXT NOT NULL,
            PRIMARY KEY (id_type, id_value)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_security_identifiers_ticker ON security_identifiers(ticker)")
    # Issuer holdings files (iShares, SPDR): one row per fund ticker. SEEDED DISABLED: nothing is downloaded from an issuer until an
    # administrator checks the URL and turns the row on. Admin-editable (services.investor_service.upsert_fund_issuer_source).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fund_issuer_sources (
            ticker      TEXT PRIMARY KEY,
            issuer      TEXT NOT NULL CHECK (issuer IN ('ishares', 'spdr')),
            url         TEXT NOT NULL,
            enabled     INTEGER NOT NULL DEFAULT 0,
            notes       TEXT,
            version     INTEGER NOT NULL DEFAULT 1,
            updated_at  TEXT NOT NULL,
            updated_by  TEXT
        )""")
    for ticker, issuer, url, note in SEED_ISSUER_SOURCES:
        conn.execute("INSERT OR IGNORE INTO fund_issuer_sources (ticker, issuer, url, enabled, notes, updated_at, updated_by) "
                     "VALUES (?, ?, ?, 0, ?, ?, 'system')", [ticker, issuer, url, note, _now()])


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
    # holdings that carry a ticker are mapped by definition; a provider that did not fill mapped_coverage is not stored as 0
    mapped = max(data.mapped_coverage, min(1.0, sum(h.weight for h in data.holdings if h.symbol and h.weight is not None)))
    with _conn(conn) as c:
        c.execute(
            """INSERT INTO fund_profiles (instrument_id, expense_ratio, fund_family, category, aum, share_class,
                                          share_class_group, holdings_coverage, derivative_short_weight, mapped_coverage, as_of, source,
                                          fetched_at, reason_codes_json, source_detail_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(instrument_id) DO UPDATE SET
                 expense_ratio = excluded.expense_ratio, fund_family = excluded.fund_family, category = excluded.category,
                 aum = excluded.aum, share_class = excluded.share_class, share_class_group = excluded.share_class_group,
                 holdings_coverage = excluded.holdings_coverage, derivative_short_weight = excluded.derivative_short_weight,
                 mapped_coverage = excluded.mapped_coverage, as_of = excluded.as_of, source = excluded.source,
                 fetched_at = excluded.fetched_at, reason_codes_json = excluded.reason_codes_json,
                 source_detail_json = excluded.source_detail_json""",
            [instrument_id, p.expense_ratio, p.fund_family, p.category, p.aum, p.share_class, p.share_class_group,
             data.holdings_coverage, data.derivative_short_weight, mapped, data.as_of or fetched_at[:10],
             data.source, fetched_at,
             json.dumps(list(data.reason_codes)), json.dumps(data.source_detail)])
        c.execute("DELETE FROM fund_holdings WHERE fund_instrument_id = ?", [instrument_id])
        c.executemany(
            "INSERT INTO fund_holdings (fund_instrument_id, holding_symbol, holding_name, cusip, isin, weight, holding_is_fund, as_of, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(instrument_id, h.symbol, h.name, h.cusip, h.isin, h.weight, int(h.holding_is_fund), data.as_of or fetched_at[:10], data.source)
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
                                holding_is_fund=bool(r["holding_is_fund"]), isin=r["isin"])
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
        derivative_short_weight=prof.get("derivative_short_weight") or 0.0,
        mapped_coverage=(prof["mapped_coverage"] if prof.get("mapped_coverage") is not None else prof["holdings_coverage"]),
        source_detail=json.loads(prof.get("source_detail_json") or "{}"),
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


# ── SEC fund identifiers (ticker -> CIK / series / class) ─────────────────────

def replace_sec_fund_ids(rows: list[tuple[int, str, str, str]], *, fetched_at: str | None = None,
                         conn: sqlite3.Connection | None = None) -> int:
    """Replace the whole table with `rows` = [(cik, series_id, class_id, symbol)] in ONE transaction (readers see the old table or
    the new one, never half). Symbols are upper-cased; rows without a symbol or series are skipped. Returns rows stored."""
    fetched_at = fetched_at or _now()
    clean = {r[2]: (int(r[0]), str(r[1]), str(r[2]), str(r[3]).strip().upper()) for r in rows if r[1] and r[2] and r[3]}
    if not clean:
        return 0                                  # a blank / failed download must never wipe the table we have
    with _conn(conn) as c:
        c.execute("DELETE FROM sec_fund_ids")
        c.executemany("INSERT INTO sec_fund_ids (class_id, symbol, cik, series_id, fetched_at) VALUES (?, ?, ?, ?, ?)",
                      [(cl, sym, cik, ser, fetched_at) for cik, ser, cl, sym in clean.values()])
    return len(clean)


def get_sec_fund_id(symbol: str, *, conn: sqlite3.Connection | None = None) -> dict | None:
    with _conn(conn) as c:
        r = c.execute("SELECT * FROM sec_fund_ids WHERE symbol = ? LIMIT 1", [symbol.strip().upper()]).fetchone()
    return dict(r) if r else None


def sec_fund_ids_fetched_at(*, conn: sqlite3.Connection | None = None) -> str | None:
    """When the table was last downloaded (None = never / empty)."""
    with _conn(conn) as c:
        r = c.execute("SELECT MAX(fetched_at) FROM sec_fund_ids").fetchone()
    return r[0] if r and r[0] else None


# ── CUSIP / ISIN -> ticker cache ──────────────────────────────────────────────

def get_security_identifiers(refs: list[tuple[str, str]], *, conn: sqlite3.Connection | None = None) -> dict[tuple[str, str], dict]:
    """{(id_type, id_value): row} for the identifiers that have a stored result (mapped OR not_found)."""
    refs = list(dict.fromkeys(refs))
    out: dict[tuple[str, str], dict] = {}
    with _conn(conn) as c:
        for i in range(0, len(refs), 400):                       # stay under SQLite's variable limit
            chunk = refs[i:i + 400]
            q = " OR ".join("(id_type = ? AND id_value = ?)" for _ in chunk)
            for r in c.execute(f"SELECT * FROM security_identifiers WHERE {q}", [x for ref in chunk for x in ref]).fetchall():
                out[(r["id_type"], r["id_value"])] = dict(r)
    return out


def save_security_identifiers(rows: list[dict], *, conn: sqlite3.Connection | None = None) -> int:
    """Upsert resolved identifiers in ONE transaction. Each row: id_type, id_value, status ('mapped' | 'not_found'), resolved_at and,
    for a mapped row, ticker (+ optional name / exch_code / security_type / figi / source)."""
    if not rows:
        return 0
    with _conn(conn) as c:
        c.executemany(
            """INSERT INTO security_identifiers (id_type, id_value, status, ticker, name, exch_code, security_type, figi, source, resolved_at)
               VALUES (:id_type, :id_value, :status, :ticker, :name, :exch_code, :security_type, :figi, :source, :resolved_at)
               ON CONFLICT(id_type, id_value) DO UPDATE SET status = excluded.status, ticker = excluded.ticker, name = excluded.name,
                 exch_code = excluded.exch_code, security_type = excluded.security_type, figi = excluded.figi,
                 source = excluded.source, resolved_at = excluded.resolved_at""",
            [{"ticker": None, "name": None, "exch_code": None, "security_type": None, "figi": None, "source": "openfigi", **r}
             for r in rows])
    return len(rows)


# ── Issuer holdings-file sources ──────────────────────────────────────────────

def list_issuer_sources(*, conn: sqlite3.Connection | None = None) -> list[dict]:
    with _conn(conn) as c:
        return [dict(r) for r in c.execute("SELECT * FROM fund_issuer_sources ORDER BY ticker").fetchall()]


def get_issuer_source(ticker: str, *, enabled_only: bool = True, conn: sqlite3.Connection | None = None) -> dict | None:
    with _conn(conn) as c:
        r = c.execute("SELECT * FROM fund_issuer_sources WHERE ticker = ?" + (" AND enabled = 1" if enabled_only else ""),
                      [ticker.strip().upper()]).fetchone()
    return dict(r) if r else None


def upsert_issuer_source(ticker: str, issuer: str, url: str, enabled: bool, *, notes: str | None, updated_by: str,
                         expected_version: int | None = None, conn: sqlite3.Connection | None = None) -> dict:
    """Create or update one source row. An update with a stale expected_version raises RuntimeError. Validation of the URL / issuer is the
    caller's (services.investor_service); this layer only stores."""
    t = ticker.strip().upper()
    with _conn(conn) as c:
        cur = c.execute("SELECT version FROM fund_issuer_sources WHERE ticker = ?", [t]).fetchone()
        if cur is None:
            c.execute("INSERT INTO fund_issuer_sources (ticker, issuer, url, enabled, notes, updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
                      [t, issuer, url, int(bool(enabled)), notes, _now(), updated_by])
        else:
            if expected_version is not None and cur["version"] != expected_version:
                raise RuntimeError("issuer source changed since you loaded it; reload and retry")
            c.execute("UPDATE fund_issuer_sources SET issuer = ?, url = ?, enabled = ?, notes = ?, version = version + 1, updated_at = ?, "
                      "updated_by = ? WHERE ticker = ?", [issuer, url, int(bool(enabled)), notes, _now(), updated_by, t])
        return dict(c.execute("SELECT * FROM fund_issuer_sources WHERE ticker = ?", [t]).fetchone())


def list_fund_staleness(*, conn: sqlite3.Connection | None = None) -> list[dict]:
    """One row per stored fund: ticker, source, as_of, fetched_at, coverage figures and per-piece sources (for the admin freshness table)."""
    with _conn(conn) as c:
        rows = c.execute(
            "SELECT i.ticker, p.source, p.as_of, p.fetched_at, p.holdings_coverage, p.mapped_coverage, p.source_detail_json, p.reason_codes_json "
            "FROM fund_profiles p JOIN instruments i ON i.instrument_id = p.instrument_id ORDER BY i.ticker").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["source_detail"] = json.loads(d.pop("source_detail_json") or "{}")
        d["reason_codes"] = json.loads(d.pop("reason_codes_json") or "[]")
        out.append(d)
    return out
