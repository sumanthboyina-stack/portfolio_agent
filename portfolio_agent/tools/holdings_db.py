"""
Holdings repository — SQLite reads and writes for portfolios, accounts,
positions, cash, import receipts, audit events and daily price history.

Rules of this module
  * It knows HOW to read and write records. It never decides whether a change
    is allowed or what else must change with it — that is
    portfolio_agent.services.account_service, the only writer of accounts,
    positions and cash.
  * Every helper takes an optional ``conn``. With one it joins the caller's
    transaction and never commits (see unit_of_work / transaction); without
    one it is atomic on its own.
  * ``holdings`` is a VIEW (positions ⋈ active accounts) that keeps the row
    shape the many read-only consumers were written against; its ``id`` is
    the position_id. Legacy tables are renamed *_legacy_v1, never dropped, by
    the one-time migration below.

Identity
  account  = accounts.account_id. (broker, account_number) is the external
             reference a broker file carries and stays UNIQUE per portfolio;
             display_name is a label the user may change freely.
  position = positions.position_id, UNIQUE(account_id, instrument_id), stable
             across imports (a re-import updates the row in place).
  instrument = instruments.instrument_id. A ticker is an attribute of an
             instrument (UNIQUE while current) and may change; the rest of the
             app still joins on ticker, so the `holdings` view exposes the
             instrument's current ticker and a ticker change relabels that
             instrument's daily history rows in the same transaction.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Optional

from portfolio_agent.domain import LOCAL_OWNER, Holding, summarize_holdings
from portfolio_agent.tools.db import db_conn, migrate_columns

# Freshness fields on a position — deliberately separate, they answer different
# questions:
#   as_of_date   – statement date the broker file reports, else the import date
#   synced_at    – when the row was last written by an import/edit ("imported at")
#   price_as_of  – the trading date the current_price was observed
# Research/prediction timestamps live on the predictions tables, not here.

ACCOUNT_ACTIVE = "active"
ACCOUNT_ARCHIVED = "archived"


# ── Daily price history ───────────────────────────────────────────────────────
#
# Grain: one row per (date, account, instrument).
#   account    = (broker, account_number) — the external account reference, so
#                history survives an account row being deleted and re-created.
#   instrument = ticker (upper-cased).
# Portfolio-level numbers are produced by summing rows at read time; nothing
# is ever aggregated on write, so two accounts at one broker holding the same
# ticker keep separate rows.
#
# `source` records how a row was produced:
#   snapshot            – evening pipeline, from live holdings on that day
#   backfill            – historical close × the account's current shares
#   reconstructed       – rebuilt from a legacy broker-grain row's close price
#                         × the account's current shares (see repair_legacy_history)
#   attributed          – legacy row whose broker has exactly one account in the
#                         ticker; the OBSERVED shares/value are kept, only the
#                         account is filled in
#   legacy_broker_grain – pre-migration row keyed only by (date, ticker, broker);
#                         where a broker had 2+ accounts in the same ticker, the
#                         last account written overwrote the others, so the value
#                         may be undercounted. Cannot be attributed to an account.
HISTORY_SOURCE_SNAPSHOT = "snapshot"
HISTORY_SOURCE_BACKFILL = "backfill"
HISTORY_SOURCE_RECONSTRUCTED = "reconstructed"
HISTORY_SOURCE_LEGACY = "legacy_broker_grain"
HISTORY_SOURCE_ATTRIBUTED = "attributed"      # observed legacy row, attributed to its single owner account
OBSERVED_SOURCES = frozenset({HISTORY_SOURCE_SNAPSHOT, HISTORY_SOURCE_ATTRIBUTED})
ESTIMATED_SOURCES = frozenset({HISTORY_SOURCE_BACKFILL, HISTORY_SOURCE_RECONSTRUCTED, HISTORY_SOURCE_LEGACY})

_PRICE_HISTORY_DDL = """
    CREATE TABLE IF NOT EXISTS portfolio_price_history (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        date           TEXT NOT NULL,
        broker         TEXT NOT NULL DEFAULT '',
        account_number TEXT NOT NULL DEFAULT '',
        ticker         TEXT NOT NULL,
        account_name   TEXT,
        close_price    REAL,
        shares         REAL,
        market_value   REAL,
        cost_basis     REAL,
        source         TEXT NOT NULL DEFAULT 'snapshot',
        UNIQUE(date, broker, account_number, ticker)
    )
"""

_PRICE_HISTORY_INSERT = """INSERT OR {conflict} INTO portfolio_price_history
       (date, broker, account_number, ticker, account_name,
        close_price, shares, market_value, cost_basis, source)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""


def position_key(h: dict) -> tuple[str, str, str]:
    """(broker, account_number, ticker) — the grain every history row is written at."""
    return (
        (h.get("broker") or "").strip(),
        (h.get("account_number") or "").strip(),
        (h.get("ticker") or "").upper().strip(),
    )


def history_row(date_str: str, h: dict, close: float | None, source: str) -> tuple:
    broker, acct, ticker = position_key(h)
    shares = h.get("shares")
    mv = round(close * shares, 2) if (close is not None and shares) else None
    return (date_str, broker, acct, ticker, h.get("account_name"),
            close, shares, mv, h.get("cost_basis_total"), source)


# ── Connections & transactions ────────────────────────────────────────────────

@contextmanager
def unit_of_work(conn: sqlite3.Connection | None = None):
    """
    Called with no argument: open a connection, ensure the schema, yield it,
    then COMMIT on success or ROLLBACK on error — a single helper is atomic on
    its own. Called with *conn*: yield it untouched and never commit; the
    caller owns the transaction.
    """
    if conn is not None:
        yield conn
        return

    def _setup(c: sqlite3.Connection) -> None:
        _create_schema(c)
        c.commit()

    with db_conn(setup=_setup) as c:
        try:
            yield c
        except BaseException:
            c.rollback()
            raise
        else:
            c.commit()


_db = unit_of_work   # short internal alias


@contextmanager
def transaction():
    """
    One transaction spanning several repository calls. Pass the yielded
    connection as ``conn=`` to each of them. Nothing is visible to other
    connections until the block exits cleanly; any exception rolls back
    everything written inside. Keep network calls outside the block.
    """
    with unit_of_work() as conn:
        yield conn


# ── Schema & migrations ───────────────────────────────────────────────────────

_POSITIONS_DDL = """CREATE TABLE IF NOT EXISTS positions (
        position_id      INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id       INTEGER NOT NULL REFERENCES accounts(account_id),
        instrument_id    INTEGER NOT NULL REFERENCES instruments(instrument_id),
        description      TEXT,
        shares           REAL,
        avg_cost         REAL,
        cost_basis_total REAL,
        current_price    REAL,
        current_value    REAL,
        sector           TEXT,
        as_of_date       TEXT,
        synced_at        TEXT NOT NULL DEFAULT (datetime('now')),
        price_as_of      TEXT,
        source_import_id INTEGER,
        version          INTEGER NOT NULL DEFAULT 1,
        UNIQUE(account_id, instrument_id)
    )"""

_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS portfolios (
        portfolio_id  INTEGER PRIMARY KEY AUTOINCREMENT,
        owner         TEXT NOT NULL UNIQUE,
        name          TEXT NOT NULL DEFAULT 'My portfolio',
        base_currency TEXT NOT NULL DEFAULT 'USD',
        version       INTEGER NOT NULL DEFAULT 1,
        created_at    TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    """CREATE TABLE IF NOT EXISTS accounts (
        account_id     INTEGER PRIMARY KEY AUTOINCREMENT,
        portfolio_id   INTEGER NOT NULL REFERENCES portfolios(portfolio_id),
        broker         TEXT NOT NULL,
        account_number TEXT NOT NULL DEFAULT '',
        display_name   TEXT,
        account_type   TEXT,
        status         TEXT NOT NULL DEFAULT 'active',
        version        INTEGER NOT NULL DEFAULT 1,
        created_at     TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(portfolio_id, broker, account_number)
    )""",
    """CREATE TABLE IF NOT EXISTS instruments (
        instrument_id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker        TEXT NOT NULL UNIQUE,
        name          TEXT,
        asset_class   TEXT,
        status        TEXT NOT NULL DEFAULT 'active',
        version       INTEGER NOT NULL DEFAULT 1,
        created_at    TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    """CREATE TABLE IF NOT EXISTS instrument_ticker_changes (
        change_id     INTEGER PRIMARY KEY AUTOINCREMENT,
        instrument_id INTEGER NOT NULL REFERENCES instruments(instrument_id),
        old_ticker    TEXT NOT NULL,
        new_ticker    TEXT NOT NULL,
        changed_at    TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    _POSITIONS_DDL,
    """CREATE TABLE IF NOT EXISTS account_cash (
        account_id  INTEGER PRIMARY KEY REFERENCES accounts(account_id),
        amount      REAL,
        as_of_date  TEXT,
        detail      TEXT,
        imported_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    """CREATE TABLE IF NOT EXISTS import_runs (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        imported_at    TEXT DEFAULT (datetime('now')),
        broker         TEXT NOT NULL,
        accounts       TEXT,
        filename       TEXT,
        statement_date TEXT,
        added          INTEGER,
        updated        INTEGER,
        removed        INTEGER,
        positions      INTEGER,
        cash_accounts  INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS audit_events (
        event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
        recorded_at  TEXT NOT NULL DEFAULT (datetime('now')),
        actor        TEXT NOT NULL,
        source       TEXT,
        request_id   TEXT,
        operation    TEXT NOT NULL,
        portfolio_id INTEGER,
        account_id   INTEGER,
        position_id  INTEGER,
        before       TEXT,
        after        TEXT,
        reason       TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_audit_account ON audit_events (account_id, event_id)",
    _PRICE_HISTORY_DDL,
    "CREATE INDEX IF NOT EXISTS idx_pph_date ON portfolio_price_history (date)",
    "CREATE INDEX IF NOT EXISTS idx_pph_ticker ON portfolio_price_history (ticker)",
]

# Read-compatibility view: the old `holdings` row shape over the new tables.
_HOLDINGS_VIEW = """CREATE VIEW holdings AS
    SELECT p.position_id AS id, i.ticker, p.description, p.shares, p.avg_cost,
           p.cost_basis_total, p.current_price, p.current_value,
           a.display_name AS account_name, a.account_number, a.account_type, a.broker,
           p.sector, p.as_of_date, p.synced_at, p.price_as_of,
           p.account_id, a.portfolio_id, p.version, p.source_import_id,
           p.instrument_id, i.name AS instrument_name
    FROM positions p
    JOIN accounts a ON a.account_id = p.account_id
    JOIN instruments i ON i.instrument_id = p.instrument_id
    WHERE a.status = 'active'"""


def _object_type(conn: sqlite3.Connection, name: str) -> str | None:
    row = conn.execute("SELECT type FROM sqlite_master WHERE name = ?", [name]).fetchone()
    return row[0] if row else None


def _create_schema(conn: sqlite3.Connection) -> None:
    if _object_type(conn, "holdings") == "view":
        conn.execute("DROP VIEW holdings")          # recreated below from the current definition
    for ddl in _SCHEMA:
        conn.execute(ddl)
    conn.execute("INSERT OR IGNORE INTO portfolios (owner) VALUES (?)", [LOCAL_OWNER])
    _migrate_legacy_holdings(conn)
    _migrate_positions_to_instruments(conn)
    _migrate_price_history_grain(conn)
    # Indexes on rebuilt tables come after the migrations, once the final shape is in place.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_positions_instrument ON positions (instrument_id)")
    conn.execute(_HOLDINGS_VIEW)


def _migrate_legacy_holdings(conn: sqlite3.Connection) -> None:
    """
    One-time move from the flat `holdings` / `cash_balances` tables to
    accounts + positions + account_cash. Position ids are preserved (so
    nothing keyed on a holdings id detaches), every distinct
    (broker, account_number) becomes an account under the local portfolio,
    and the old tables are renamed *_legacy_v1 rather than dropped.
    """
    if _object_type(conn, "holdings") != "table":
        return
    migrate_columns(conn, "holdings", [
        ("description", "TEXT"), ("shares", "REAL"), ("avg_cost", "REAL"), ("cost_basis_total", "REAL"),
        ("current_price", "REAL"), ("current_value", "REAL"), ("account_name", "TEXT"),
        ("account_number", "TEXT"), ("account_type", "TEXT"), ("broker", "TEXT"), ("sector", "TEXT"),
        ("as_of_date", "TEXT"), ("synced_at", "TEXT"), ("price_as_of", "TEXT"),
    ])
    # Manually-added positions used to be stored with account_number = "" no
    # matter which account name the user typed. Key them by name, as unnamed
    # accounts in file imports are keyed by their nickname.
    conn.execute(
        "UPDATE holdings SET account_number = account_name "
        "WHERE broker = 'manual' AND COALESCE(account_number, '') = '' AND COALESCE(account_name, '') <> ''"
    )
    pid = conn.execute("SELECT portfolio_id FROM portfolios WHERE owner = ?", [LOCAL_OWNER]).fetchone()[0]
    has_cash = _object_type(conn, "cash_balances") == "table"

    conn.execute(
        """INSERT OR IGNORE INTO accounts (portfolio_id, broker, account_number, display_name, account_type)
           SELECT ?, COALESCE(NULLIF(TRIM(broker), ''), 'unknown'), TRIM(COALESCE(account_number, '')),
                  MAX(account_name), MAX(account_type)
           FROM holdings GROUP BY 2, 3""", [pid])
    if has_cash:
        conn.execute(
            """INSERT OR IGNORE INTO accounts (portfolio_id, broker, account_number, display_name)
               SELECT ?, broker, TRIM(COALESCE(account_number, '')), account_name FROM cash_balances""", [pid])
    conn.execute(
        """INSERT OR IGNORE INTO instruments (ticker, name)
           SELECT UPPER(TRIM(ticker)), MAX(description) FROM holdings WHERE TRIM(ticker) <> '' GROUP BY 1""")
    conn.execute(
        """INSERT OR IGNORE INTO positions
               (position_id, account_id, instrument_id, description, shares, avg_cost, cost_basis_total,
                current_price, current_value, sector, as_of_date, synced_at, price_as_of)
           SELECT h.id, a.account_id, i.instrument_id, h.description, h.shares, h.avg_cost,
                  h.cost_basis_total, h.current_price, h.current_value, h.sector, h.as_of_date,
                  COALESCE(h.synced_at, datetime('now')), h.price_as_of
           FROM holdings h
           JOIN accounts a ON a.portfolio_id = ?
                          AND a.broker = COALESCE(NULLIF(TRIM(h.broker), ''), 'unknown')
                          AND a.account_number = TRIM(COALESCE(h.account_number, ''))
           JOIN instruments i ON i.ticker = UPPER(TRIM(h.ticker))
           ORDER BY h.synced_at DESC, h.id DESC""", [pid])
    if has_cash:
        conn.execute(
            """INSERT OR REPLACE INTO account_cash (account_id, amount, as_of_date, detail, imported_at)
               SELECT a.account_id, c.amount, c.as_of_date, c.detail, COALESCE(c.imported_at, datetime('now'))
               FROM cash_balances c
               JOIN accounts a ON a.portfolio_id = ? AND a.broker = c.broker
                              AND a.account_number = TRIM(COALESCE(c.account_number, ''))""", [pid])
        conn.execute("ALTER TABLE cash_balances RENAME TO cash_balances_legacy_v1")
    conn.execute("DROP INDEX IF EXISTS idx_holdings_broker")
    conn.execute("DROP INDEX IF EXISTS idx_holdings_ticker")
    conn.execute("ALTER TABLE holdings RENAME TO holdings_legacy_v1")


def _migrate_positions_to_instruments(conn: sqlite3.Connection) -> None:
    """
    One-time rebuild of a positions table that still carries `ticker` as a
    column (no instrument_id): create one instrument per distinct ticker,
    then recreate positions keyed by instrument_id with every position_id,
    version and value preserved.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(positions)").fetchall()}
    if not cols or "instrument_id" in cols:
        return
    conn.execute(
        """INSERT OR IGNORE INTO instruments (ticker, name)
           SELECT ticker, MAX(description) FROM positions GROUP BY ticker""")
    conn.execute("DROP INDEX IF EXISTS idx_positions_ticker")
    conn.execute("ALTER TABLE positions RENAME TO positions_v2_tmp")
    conn.execute(_POSITIONS_DDL)
    conn.execute(
        """INSERT INTO positions
               (position_id, account_id, instrument_id, description, shares, avg_cost, cost_basis_total,
                current_price, current_value, sector, as_of_date, synced_at, price_as_of, source_import_id, version)
           SELECT p.position_id, p.account_id, i.instrument_id, p.description, p.shares, p.avg_cost,
                  p.cost_basis_total, p.current_price, p.current_value, p.sector, p.as_of_date, p.synced_at,
                  p.price_as_of, p.source_import_id, p.version
           FROM positions_v2_tmp p JOIN instruments i ON i.ticker = p.ticker""")
    conn.execute("DROP TABLE positions_v2_tmp")


def _migrate_price_history_grain(conn: sqlite3.Connection) -> None:
    """
    Rebuild portfolio_price_history at (date, broker, account_number, ticker)
    grain if it still has the old UNIQUE(date, ticker, broker) constraint.

    Existing rows are carried over verbatim, tagged source='legacy_broker_grain'
    with account_number=''. They are NOT silently attributed to an account:
    where a broker had several accounts in one ticker the stored value is the
    last account's only, and the others are gone. repair_legacy_history()
    reconstructs those from account-level holdings; the coverage UI flags them
    until then.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(portfolio_price_history)").fetchall()}
    if not cols or "account_number" in cols:
        return
    conn.execute("ALTER TABLE portfolio_price_history RENAME TO portfolio_price_history_legacy")
    conn.execute("DROP INDEX IF EXISTS idx_pph_date")
    conn.execute("DROP INDEX IF EXISTS idx_pph_ticker")
    conn.execute(_PRICE_HISTORY_DDL)
    conn.execute(
        """INSERT OR IGNORE INTO portfolio_price_history
           (date, broker, account_number, ticker, account_name,
            close_price, shares, market_value, cost_basis, source)
           SELECT date, COALESCE(broker, ''), '', UPPER(ticker), account_name,
                  close_price, shares, market_value, cost_basis, ?
           FROM portfolio_price_history_legacy""",
        [HISTORY_SOURCE_LEGACY],
    )
    conn.execute("DROP TABLE portfolio_price_history_legacy")


# ── Portfolios ────────────────────────────────────────────────────────────────

def get_portfolio_by_owner(owner: str, *, conn: sqlite3.Connection | None = None) -> dict | None:
    with _db(conn) as conn:
        r = conn.execute("SELECT * FROM portfolios WHERE owner = ?", [owner]).fetchone()
    return dict(r) if r else None


# ── Accounts ──────────────────────────────────────────────────────────────────

_ACCOUNT_EDITABLE = frozenset({"display_name", "account_type", "status"})


def get_account(account_id: int, *, conn: sqlite3.Connection | None = None) -> dict | None:
    with _db(conn) as conn:
        r = conn.execute("SELECT * FROM accounts WHERE account_id = ?", [account_id]).fetchone()
    return dict(r) if r else None


def find_account(portfolio_id: int, broker: str, account_number: str, *,
                 conn: sqlite3.Connection | None = None) -> dict | None:
    """Look an account up by its external reference, whatever its status."""
    with _db(conn) as conn:
        r = conn.execute(
            "SELECT * FROM accounts WHERE portfolio_id = ? AND broker = ? AND account_number = ?",
            [portfolio_id, broker, (account_number or "").strip()]).fetchone()
    return dict(r) if r else None


def list_accounts(portfolio_id: int | None = None, *, include_archived: bool = False,
                  conn: sqlite3.Connection | None = None) -> list[dict]:
    clauses, params = [], []
    if portfolio_id is not None:
        clauses.append("portfolio_id = ?"); params.append(portfolio_id)
    if not include_archived:
        clauses.append("status = ?"); params.append(ACCOUNT_ACTIVE)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with _db(conn) as conn:
        rows = conn.execute(f"SELECT * FROM accounts {where} ORDER BY broker, display_name, account_number",
                            params).fetchall()
    return [dict(r) for r in rows]


def insert_account(portfolio_id: int, broker: str, account_number: str, display_name: str | None,
                   account_type: str | None, *, conn: sqlite3.Connection | None = None) -> dict:
    with _db(conn) as conn:
        cur = conn.execute(
            "INSERT INTO accounts (portfolio_id, broker, account_number, display_name, account_type) "
            "VALUES (?, ?, ?, ?, ?)",
            [portfolio_id, broker, (account_number or "").strip(), display_name, account_type])
        return get_account(cur.lastrowid, conn=conn)


def update_account(account_id: int, *, expected_version: int | None = None,
                   conn: sqlite3.Connection | None = None, **fields) -> int:
    """Set display_name / account_type / status; bumps version. Returns rows changed (0 = missing or stale)."""
    bad = set(fields) - _ACCOUNT_EDITABLE
    if bad:
        raise ValueError(f"update_account: cannot set column(s) {sorted(bad)}")
    if not fields:
        return 0
    cols = list(fields)
    sql = (f"UPDATE accounts SET {', '.join(f'{c} = ?' for c in cols)}, "
           "updated_at = datetime('now'), version = version + 1 WHERE account_id = ?")
    params = [fields[c] for c in cols] + [account_id]
    if expected_version is not None:
        sql += " AND version = ?"; params.append(expected_version)
    with _db(conn) as conn:
        return conn.execute(sql, params).rowcount


def delete_account(account_id: int, *, conn: sqlite3.Connection | None = None) -> int:
    """Hard-delete an account with its positions and cash. Price history (keyed externally) is untouched."""
    with _db(conn) as conn:
        n = conn.execute("DELETE FROM positions WHERE account_id = ?", [account_id]).rowcount
        conn.execute("DELETE FROM account_cash WHERE account_id = ?", [account_id])
        conn.execute("DELETE FROM accounts WHERE account_id = ?", [account_id])
    return n


# ── Instruments ───────────────────────────────────────────────────────────────

_INSTRUMENT_EDITABLE = frozenset({"ticker", "name", "asset_class", "status"})


def _norm_ticker(ticker: str | None) -> str:
    return (ticker or "").upper().strip()


def get_instrument(instrument_id: int, *, conn: sqlite3.Connection | None = None) -> dict | None:
    with _db(conn) as conn:
        r = conn.execute("SELECT * FROM instruments WHERE instrument_id = ?", [instrument_id]).fetchone()
    return dict(r) if r else None


def find_instrument(ticker: str, *, conn: sqlite3.Connection | None = None) -> dict | None:
    """The instrument whose CURRENT ticker is *ticker*."""
    with _db(conn) as conn:
        r = conn.execute("SELECT * FROM instruments WHERE ticker = ?", [_norm_ticker(ticker)]).fetchone()
    return dict(r) if r else None


def get_or_create_instrument(ticker: str, *, name: str | None = None,
                             conn: sqlite3.Connection | None = None) -> dict:
    """Resolve a ticker to its instrument, creating one on first sight (name = first description seen)."""
    t = _norm_ticker(ticker)
    if not t:
        raise ValueError("instrument: ticker is required")
    with _db(conn) as conn:
        conn.execute("INSERT OR IGNORE INTO instruments (ticker, name) VALUES (?, ?)", [t, name or None])
        return find_instrument(t, conn=conn)


def list_instruments(*, conn: sqlite3.Connection | None = None) -> list[dict]:
    with _db(conn) as conn:
        rows = conn.execute("SELECT * FROM instruments ORDER BY ticker").fetchall()
    return [dict(r) for r in rows]


def update_instrument(instrument_id: int, *, expected_version: int | None = None,
                      conn: sqlite3.Connection | None = None, **fields) -> int:
    """Set ticker / name / asset_class / status; bumps version. Returns rows changed (0 = missing or stale)."""
    bad = set(fields) - _INSTRUMENT_EDITABLE
    if bad:
        raise ValueError(f"update_instrument: cannot set column(s) {sorted(bad)}")
    if "ticker" in fields:
        fields["ticker"] = _norm_ticker(fields["ticker"])
        if not fields["ticker"]:
            raise ValueError("update_instrument: ticker cannot be blank")
    if not fields:
        return 0
    cols = list(fields)
    sql = (f"UPDATE instruments SET {', '.join(f'{c} = ?' for c in cols)}, "
           "updated_at = datetime('now'), version = version + 1 WHERE instrument_id = ?")
    params = [fields[c] for c in cols] + [instrument_id]
    if expected_version is not None:
        sql += " AND version = ?"; params.append(expected_version)
    with _db(conn) as conn:
        return conn.execute(sql, params).rowcount


def record_ticker_change(instrument_id: int, old_ticker: str, new_ticker: str, *,
                         conn: sqlite3.Connection | None = None) -> int:
    with _db(conn) as conn:
        return conn.execute(
            "INSERT INTO instrument_ticker_changes (instrument_id, old_ticker, new_ticker) VALUES (?, ?, ?)",
            [instrument_id, _norm_ticker(old_ticker), _norm_ticker(new_ticker)]).lastrowid


def get_ticker_changes(instrument_id: int, *, conn: sqlite3.Connection | None = None) -> list[dict]:
    with _db(conn) as conn:
        rows = conn.execute("SELECT * FROM instrument_ticker_changes WHERE instrument_id = ? ORDER BY change_id",
                            [instrument_id]).fetchall()
    return [dict(r) for r in rows]


def relabel_price_history_ticker(old_ticker: str, new_ticker: str, *,
                                 conn: sqlite3.Connection | None = None) -> dict:
    """
    Move daily history rows from *old_ticker* to *new_ticker*. A day that
    already has a row under the new ticker for the same account is left
    under the old one and counted in `skipped`, never overwritten.
    """
    old, new = _norm_ticker(old_ticker), _norm_ticker(new_ticker)
    with _db(conn) as conn:
        moved = conn.execute("UPDATE OR IGNORE portfolio_price_history SET ticker = ? WHERE ticker = ?",
                             [new, old]).rowcount
        skipped = conn.execute("SELECT COUNT(*) FROM portfolio_price_history WHERE ticker = ?", [old]).fetchone()[0]
    return {"moved": moved, "skipped": skipped}


# ── Positions ─────────────────────────────────────────────────────────────────

_POSITION_COLUMNS = ("instrument_id", "description", "shares", "avg_cost", "cost_basis_total", "current_price",
                     "current_value", "sector", "as_of_date", "price_as_of", "source_import_id")
_POSITION_EDITABLE = frozenset(_POSITION_COLUMNS) | {"ticker"}   # ticker is translated to instrument_id
_POSITION_SELECT = "SELECT p.*, i.ticker FROM positions p JOIN instruments i ON i.instrument_id = p.instrument_id"


def position_fields(h: dict, as_of_date: str) -> dict:
    """
    Normalise an incoming position dict (parser row or form input) to the
    positions columns. Per-row statement date wins over the import-wide
    default; a price that came from the broker file is as of that statement
    date unless the row says otherwise.
    """
    row_as_of = h.get("as_of_date") or as_of_date
    price_as_of = h.get("price_as_of") or (row_as_of if h.get("current_price") is not None else None)
    return {
        "ticker": (h.get("ticker") or "").upper().strip(),
        "description": h.get("description"),
        "shares": h.get("shares"),
        "avg_cost": h.get("avg_cost"),
        "cost_basis_total": h.get("cost_basis_total"),
        "current_price": h.get("current_price"),
        "current_value": h.get("current_value"),
        "sector": h.get("sector"),
        "as_of_date": row_as_of,
        "price_as_of": price_as_of,
    }


def get_position(position_id: int, *, conn: sqlite3.Connection | None = None) -> dict | None:
    with _db(conn) as conn:
        r = conn.execute(f"{_POSITION_SELECT} WHERE p.position_id = ?", [position_id]).fetchone()
    return dict(r) if r else None


def find_position(account_id: int, ticker: str, *, conn: sqlite3.Connection | None = None) -> dict | None:
    """The account's position in the instrument whose current ticker is *ticker*."""
    with _db(conn) as conn:
        r = conn.execute(f"{_POSITION_SELECT} WHERE p.account_id = ? AND i.ticker = ?",
                         [account_id, _norm_ticker(ticker)]).fetchone()
    return dict(r) if r else None


def list_positions(account_id: int, *, conn: sqlite3.Connection | None = None) -> list[dict]:
    with _db(conn) as conn:
        rows = conn.execute(f"{_POSITION_SELECT} WHERE p.account_id = ? ORDER BY i.ticker", [account_id]).fetchall()
    return [dict(r) for r in rows]


def insert_position(account_id: int, fields: dict, *, source_import_id: int | None = None,
                    conn: sqlite3.Connection | None = None) -> dict:
    """
    Insert one position from position_fields() output. The ticker is resolved
    to its instrument (created on first sight). Raises sqlite3.IntegrityError
    if the account already holds that instrument.
    """
    if not fields.get("ticker"):
        raise ValueError("insert_position: ticker is required")
    cols = [c for c in _POSITION_COLUMNS if c not in ("source_import_id", "instrument_id")]
    with _db(conn) as conn:
        inst = get_or_create_instrument(fields["ticker"], name=fields.get("description"), conn=conn)
        cur = conn.execute(
            f"INSERT INTO positions (account_id, instrument_id, {', '.join(cols)}, source_import_id) "
            f"VALUES (?, ?, {', '.join('?' for _ in cols)}, ?)",
            [account_id, inst["instrument_id"]] + [fields.get(c) for c in cols] + [source_import_id])
        return get_position(cur.lastrowid, conn=conn)


def update_position(position_id: int, *, expected_version: int | None = None, touch: bool = True,
                    conn: sqlite3.Connection | None = None, **fields) -> int:
    """
    Update columns on one position and bump its version. touch=False leaves
    synced_at alone (a price refresh is not an import). Returns rows changed:
    0 means the id is missing or expected_version is stale.
    """
    bad = set(fields) - _POSITION_EDITABLE
    if bad:
        raise ValueError(f"update_position: cannot set column(s) {sorted(bad)}")
    if not fields:
        return 0
    with _db(conn) as conn:
        if "ticker" in fields:                      # moving a position to another instrument
            t = _norm_ticker(fields.pop("ticker"))
            if not t:
                raise ValueError("update_position: ticker cannot be blank")
            fields["instrument_id"] = get_or_create_instrument(t, conn=conn)["instrument_id"]
        cols = list(fields)
        sets = [f"{c} = ?" for c in cols] + ["version = version + 1"] + (["synced_at = datetime('now')"] if touch else [])
        sql = f"UPDATE positions SET {', '.join(sets)} WHERE position_id = ?"
        params = [fields[c] for c in cols] + [position_id]
        if expected_version is not None:
            sql += " AND version = ?"; params.append(expected_version)
        return conn.execute(sql, params).rowcount


def delete_position(position_id: int, *, conn: sqlite3.Connection | None = None) -> int:
    with _db(conn) as conn:
        return conn.execute("DELETE FROM positions WHERE position_id = ?", [position_id]).rowcount


# ── Holdings (read view) ──────────────────────────────────────────────────────

def get_holdings(broker: Optional[str] = None, *, account_id: int | None = None,
                 conn: sqlite3.Connection | None = None) -> list[Holding]:
    """Positions of ACTIVE accounts in the legacy holdings row shape (id = position_id)."""
    clauses, params = [], []
    if broker:
        clauses.append("broker = ?"); params.append(broker)
    if account_id is not None:
        clauses.append("account_id = ?"); params.append(account_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with _db(conn) as conn:
        rows = conn.execute(f"SELECT * FROM holdings {where} ORDER BY ticker", params).fetchall()
    return [Holding.from_db_row(dict(r)) for r in rows]


def get_portfolio_tickers() -> list[str]:
    """Unique uppercase tickers across all current holdings, sorted — the
    one shared helper every pipeline/UI consumer uses instead of each
    re-reading a holdings file of its own."""
    with _db() as conn:
        rows = conn.execute("SELECT DISTINCT ticker FROM holdings ORDER BY ticker").fetchall()
    return [r["ticker"] for r in rows]


def get_holdings_summary() -> dict:
    """Portfolio-level totals over every stored holding and cash balance (see domain.summarize_holdings)."""
    return summarize_holdings(get_holdings(), get_cash_balances())


def get_holdings_version() -> str:
    """
    Cheap fingerprint of current holdings — changes whenever a position is
    added, edited, removed or re-priced. Used as a cache key component for
    derived results (rebalance scenarios) so they go stale on mutation, not
    on a timer.
    """
    with _db() as conn:
        r = conn.execute(
            "SELECT COUNT(*) n, MAX(synced_at) s, MAX(price_as_of) p, TOTAL(version) ver, "
            "TOTAL(current_value) v, TOTAL(shares) q FROM holdings"
        ).fetchone()
    return f"{r['n']}|{r['s']}|{r['p']}|{int(r['ver'] or 0)}|{round(r['v'] or 0, 2)}|{round(r['q'] or 0, 4)}"


# ── Cash balances (separate from securities) ─────────────────────────────────

def get_account_cash(account_id: int, *, conn: sqlite3.Connection | None = None) -> dict | None:
    with _db(conn) as conn:
        r = conn.execute("SELECT * FROM account_cash WHERE account_id = ?", [account_id]).fetchone()
    return dict(r) if r else None


def set_account_cash(account_id: int, amount: float | None, as_of_date: str, detail: str | None = None, *,
                     conn: sqlite3.Connection | None = None) -> None:
    """Record the cash / money-market balance of one account (replaces prior value)."""
    with _db(conn) as conn:
        conn.execute(
            """INSERT INTO account_cash (account_id, amount, as_of_date, detail)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(account_id) DO UPDATE SET
                 amount = excluded.amount, as_of_date = excluded.as_of_date,
                 detail = excluded.detail, imported_at = datetime('now')""",
            [account_id, amount, as_of_date, detail])


def clear_account_cash(account_id: int, *, conn: sqlite3.Connection | None = None) -> int:
    """Forget an account's cash balance (e.g. its latest import had no cash line)."""
    with _db(conn) as conn:
        return conn.execute("DELETE FROM account_cash WHERE account_id = ?", [account_id]).rowcount


def get_cash_balances(broker: Optional[str] = None, *, conn: sqlite3.Connection | None = None) -> list[dict]:
    """Cash rows of ACTIVE accounts, with the account's broker / number / name attached."""
    sql = ("SELECT c.account_id, a.broker, a.account_number, a.display_name AS account_name, "
           "c.amount, c.as_of_date, c.detail, c.imported_at "
           "FROM account_cash c JOIN accounts a ON a.account_id = c.account_id WHERE a.status = ?")
    params: list = [ACCOUNT_ACTIVE]
    if broker:
        sql += " AND a.broker = ?"; params.append(broker)
    with _db(conn) as conn:
        rows = conn.execute(sql + " ORDER BY a.broker, a.account_number", params).fetchall()
    return [dict(r) for r in rows]


# ── Import receipts ───────────────────────────────────────────────────────────

def record_import_run(*, broker: str, accounts: list[str], filename: str | None, statement_date: str | None,
                      added: int | None = None, updated: int | None = None, removed: int | None = None,
                      positions: int | None = None, cash_accounts: int | None = None,
                      conn: sqlite3.Connection | None = None) -> int:
    """Append one row to import history. Counts may be filled in later with update_import_run()."""
    with _db(conn) as conn:
        cur = conn.execute(
            """INSERT INTO import_runs (broker, accounts, filename, statement_date, added, updated, removed,
                                        positions, cash_accounts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [broker, json.dumps(accounts), filename, statement_date, added, updated, removed, positions, cash_accounts])
        return cur.lastrowid


def update_import_run(run_id: int, *, conn: sqlite3.Connection | None = None, **counts) -> int:
    bad = set(counts) - {"added", "updated", "removed", "positions", "cash_accounts"}
    if bad:
        raise ValueError(f"update_import_run: cannot set {sorted(bad)}")
    cols = list(counts)
    with _db(conn) as conn:
        return conn.execute(f"UPDATE import_runs SET {', '.join(f'{c} = ?' for c in cols)} WHERE id = ?",
                            [counts[c] for c in cols] + [run_id]).rowcount


def get_import_runs(limit: int = 25) -> list[dict]:
    with _db() as conn:
        rows = conn.execute("SELECT * FROM import_runs ORDER BY imported_at DESC, id DESC LIMIT ?", [limit]).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["accounts"] = json.loads(d.get("accounts") or "[]")
        except Exception:
            d["accounts"] = [d.get("accounts")]
        out.append(d)
    return out


# ── Audit events ──────────────────────────────────────────────────────────────

def record_audit_event(*, actor: str, source: str | None, request_id: str | None, operation: str,
                       portfolio_id: int | None = None, account_id: int | None = None,
                       position_id: int | None = None, before: dict | None = None, after: dict | None = None,
                       reason: str | None = None, conn: sqlite3.Connection | None = None) -> int:
    """Append one audit row. Callers pass the SAME conn as the mutation so both land or neither does."""
    with _db(conn) as conn:
        cur = conn.execute(
            """INSERT INTO audit_events (actor, source, request_id, operation, portfolio_id, account_id,
                                         position_id, before, after, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [actor, source, request_id, operation, portfolio_id, account_id, position_id,
             json.dumps(before, default=str) if before is not None else None,
             json.dumps(after, default=str) if after is not None else None, reason])
        return cur.lastrowid


def get_audit_events(limit: int = 50, *, account_id: int | None = None, position_id: int | None = None,
                     conn: sqlite3.Connection | None = None) -> list[dict]:
    clauses, params = [], []
    if account_id is not None:
        clauses.append("account_id = ?"); params.append(account_id)
    if position_id is not None:
        clauses.append("position_id = ?"); params.append(position_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with _db(conn) as conn:
        rows = conn.execute(f"SELECT * FROM audit_events {where} ORDER BY event_id DESC LIMIT ?",
                            params + [limit]).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("before", "after"):
            d[k] = json.loads(d[k]) if d.get(k) else None
        out.append(d)
    return out


# ── Daily price history ───────────────────────────────────────────────────────

def insert_price_history(rows: list[tuple], *, replace: bool, conn: sqlite3.Connection | None = None) -> int:
    """
    Write history rows built by history_row(). replace=True refreshes a
    position's row for that date (nightly snapshot); replace=False keeps any
    existing row and only fills gaps (backfill).
    """
    if not rows:
        return 0
    with _db(conn) as conn:
        conn.executemany(_PRICE_HISTORY_INSERT.format(conflict="REPLACE" if replace else "IGNORE"), rows)
    return len(rows)


def get_price_history_keys(*, conn: sqlite3.Connection | None = None) -> set[tuple[str, str, str, str]]:
    """Every (date, broker, account_number, ticker) that already has a history row."""
    with _db(conn) as conn:
        rows = conn.execute("SELECT date, broker, account_number, ticker FROM portfolio_price_history").fetchall()
    return {(r["date"], r["broker"] or "", r["account_number"] or "", r["ticker"]) for r in rows}


def get_price_history(
    tickers: list[str] | None = None,
    brokers: list[str] | None = None,
) -> list[dict]:
    """
    Return account-level price history rows (one per date × account × ticker),
    optionally filtered by ticker/broker. Callers wanting a portfolio or broker
    total must SUM market_value across rows per date — never pick one row.
    """
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


def get_observed_dates(*, conn: sqlite3.Connection | None = None) -> set[str]:
    """
    Dates with at least one OBSERVED row. A snapshot is the complete record of
    what was held at that close, so on these days a position with no observed
    row was not held: estimating one would invent a position.
    """
    marks = ",".join("?" for _ in OBSERVED_SOURCES)
    with _db(conn) as conn:
        rows = conn.execute(f"SELECT DISTINCT date FROM portfolio_price_history WHERE source IN ({marks})",
                            sorted(OBSERVED_SOURCES)).fetchall()
    return {r[0] for r in rows}


def delete_phantom_estimates(*, conn: sqlite3.Connection | None = None) -> int:
    """Remove backfill rows on observed days (see get_observed_dates). Returns rows removed."""
    marks = ",".join("?" for _ in OBSERVED_SOURCES)
    with _db(conn) as conn:
        return conn.execute(
            f"""DELETE FROM portfolio_price_history WHERE source = ? AND date IN
                (SELECT DISTINCT date FROM portfolio_price_history WHERE source IN ({marks}))""",
            [HISTORY_SOURCE_BACKFILL] + sorted(OBSERVED_SOURCES)).rowcount


def repair_legacy_history() -> dict:
    """
    Resolve rows tagged legacy_broker_grain using the account-level holdings
    table as the trustworthy record of which accounts hold which ticker:

      * exactly ONE account at that broker holds the ticker → the legacy row's
        observed shares/value belong to that account; fill in the account and
        tag it 'attributed'. Nothing is recomputed.
      * TWO OR MORE accounts hold it → the stored value is only the last
        account written. Rebuild one row per account from the legacy row's
        close price (that part is reliable) × each account's current shares,
        then drop the collapsed legacy row.
      * NO current account holds it (sold since) → if the broker has exactly
        one account in the portfolio the row can only be that account's, so it
        is attributed; otherwise it is left untouched and flagged.

    An observation always outranks an estimate: where a backfill row already
    stands at the (date, account, ticker) the legacy row belongs to, the
    estimate is removed and the observation takes its place. Only another
    OBSERVED row keeps a legacy row from being attributed. Finally, backfill
    rows on observed days are removed — a snapshot is the complete record of
    that close, so a position missing from it was not held (see
    get_observed_dates). Reconstructed rows assume today's share counts held
    on that date, the same assumption backfill makes. Idempotent.
    """
    holdings = get_holdings()
    by_broker_ticker: dict[tuple[str, str], list[dict]] = {}
    for h in holdings:
        broker, _acct, ticker = position_key(h)
        by_broker_ticker.setdefault((broker, ticker), []).append(h)

    accounts_by_broker: dict[str, list[dict]] = {}
    for a in list_accounts(include_archived=True):
        accounts_by_broker.setdefault(a["broker"], []).append(a)

    rekeyed = rebuilt = unresolved = estimates_replaced = 0
    with _db() as conn:
        legacy = conn.execute(
            "SELECT * FROM portfolio_price_history WHERE source = ?",
            [HISTORY_SOURCE_LEGACY],
        ).fetchall()
        for r in legacy:
            r = dict(r)
            broker = r["broker"] or ""
            owners = by_broker_ticker.get((broker, r["ticker"]), [])
            if not owners:
                # Sold since. With a single account at that broker there is no ambiguity.
                accts = accounts_by_broker.get(broker, [])
                if len(accts) != 1:
                    unresolved += 1
                    continue
                owners = [{"broker": broker, "account_number": accts[0]["account_number"],
                           "account_name": accts[0]["display_name"], "ticker": r["ticker"]}]
            if len(owners) == 1:
                _b, acct, _t = position_key(owners[0])
                clash = conn.execute(
                    "SELECT id, source FROM portfolio_price_history WHERE date = ? AND broker = ? "
                    "AND account_number = ? AND ticker = ?",
                    [r["date"], broker, acct, r["ticker"]],
                ).fetchone()
                if clash and clash["source"] in OBSERVED_SOURCES:
                    # A real per-account observation already exists for that day; the legacy copy is redundant.
                    conn.execute("DELETE FROM portfolio_price_history WHERE id = ?", [r["id"]])
                else:
                    if clash:   # an estimate is standing where the observation belongs
                        conn.execute("DELETE FROM portfolio_price_history WHERE id = ?", [clash["id"]])
                        estimates_replaced += 1
                    conn.execute(
                        "UPDATE portfolio_price_history SET account_number = ?, account_name = ?, "
                        "source = ? WHERE id = ?",
                        [acct, owners[0].get("account_name"), HISTORY_SOURCE_ATTRIBUTED, r["id"]],
                    )
                rekeyed += 1
                continue
            new_rows = [
                history_row(r["date"], h, r["close_price"], HISTORY_SOURCE_RECONSTRUCTED)
                for h in owners
            ]
            conn.execute("DELETE FROM portfolio_price_history WHERE id = ?", [r["id"]])
            conn.executemany(_PRICE_HISTORY_INSERT.format(conflict="IGNORE"), new_rows)
            rebuilt += 1
        phantoms_removed = delete_phantom_estimates(conn=conn)
    return {"rekeyed": rekeyed, "rebuilt": rebuilt, "unresolved": unresolved,
            "estimates_replaced": estimates_replaced, "phantoms_removed": phantoms_removed,
            "legacy_rows_seen": len(legacy)}


def get_history_coverage() -> dict:
    """
    Coverage stats for the history UI: how many trading days have a row for
    every current position, how many are partial, and how many rows are of
    each source (so legacy / reconstructed data can be flagged).
    """
    holdings = get_holdings()
    positions = {position_key(h) for h in holdings if h.get("ticker")}
    with _db() as conn:
        rows = conn.execute(
            "SELECT date, broker, account_number, ticker, source FROM portfolio_price_history"
        ).fetchall()

    if not rows:
        return {"days": 0, "complete_days": 0, "partial_days": 0, "first_date": None,
                "last_date": None, "by_source": {}, "partial_dates": [],
                "legacy_dates": [], "positions": len(positions)}

    by_date: dict[str, set[tuple[str, str, str]]] = {}
    by_source: dict[str, int] = {}
    legacy_dates: set[str] = set()
    for r in rows:
        by_date.setdefault(r["date"], set()).add(
            (r["broker"] or "", r["account_number"] or "", r["ticker"])
        )
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1
        if r["source"] == HISTORY_SOURCE_LEGACY:
            legacy_dates.add(r["date"])

    observed_days = get_observed_dates()
    partial = sorted(d for d, keys in by_date.items()
                     if positions and d not in observed_days and not positions <= keys)
    return {
        "days": len(by_date),
        "complete_days": len(by_date) - len(partial),
        "partial_days": len(partial),
        "partial_dates": partial,
        "first_date": min(by_date),
        "last_date": max(by_date),
        "by_source": by_source,
        "legacy_dates": sorted(legacy_dates),
        "positions": len(positions),
    }
