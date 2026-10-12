"""
Transaction ledger storage (repository layer; no authorization, no audit — services.ledger_service owns those).

  transactions   the ledger. APPEND-ONLY: database triggers refuse any UPDATE or DELETE, so a mistake is corrected with a VOID row (which
                 points at the transaction it cancels), never by editing history. Every row records who wrote it and when.
  tax_lots       DERIVED. One row per lot, keyed by open_transaction_id (the id of the transaction that opened it, so a lot keeps its identity
                 however often lots are rebuilt). Rebuilt from the ledger by replay (tools.ledger_engine) in the same transaction as every
                 ledger change; never edited by hand.
  lot_closures   DERIVED, the same way: which lots each SELL closed, with proceeds, basis, realized gain and SHORT / LONG holding period.

accounts.lot_method (NULL = use portfolio_settings.default_lot_method) is added to the accounts table by holdings_db's migration.
Money is dollars (REAL, rounded to cents where it is an amount); quantities are shares rounded to 8 places.
"""
from __future__ import annotations

import json
import sqlite3

from portfolio_agent.tools.ledger_engine import LedgerState, Txn

_TXN_TYPES = "('BUY','SELL','DEPOSIT','WITHDRAWAL','DIVIDEND','INTEREST','EMPLOYER_MATCH','FEE','SPLIT','VOID','OPENING_BALANCE')"


def _transactions_ddl(name: str = "transactions") -> str:
    return f"""CREATE TABLE IF NOT EXISTS {name} (
        txn_id            INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id        INTEGER NOT NULL REFERENCES accounts(account_id),
        instrument_id     INTEGER REFERENCES instruments(instrument_id),
        txn_type          TEXT NOT NULL CHECK (txn_type IN {_TXN_TYPES}),
        trade_date        TEXT NOT NULL,
        settle_date       TEXT,
        quantity          REAL,
        price             REAL,
        fees              REAL NOT NULL DEFAULT 0,
        amount            REAL NOT NULL DEFAULT 0,
        split_numerator   REAL,
        split_denominator REAL,
        lot_method        TEXT,
        lot_selection_json TEXT,
        voids_txn_id      INTEGER REFERENCES transactions(txn_id),
        external_id       TEXT,
        memo              TEXT,
        source            TEXT,
        contribution_source TEXT,
        clearance_status  TEXT,
        clearance_approval_id INTEGER,
        import_run_id     INTEGER,
        created_by        TEXT NOT NULL,
        request_id        TEXT,
        created_at        TEXT NOT NULL DEFAULT (datetime('now'))
    )"""


LEDGER_DDL = [
    _transactions_ddl(),
    "CREATE INDEX IF NOT EXISTS idx_transactions_account ON transactions (account_id, trade_date, txn_id)",
    "CREATE INDEX IF NOT EXISTS idx_transactions_instrument ON transactions (instrument_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_external ON transactions (account_id, external_id) WHERE external_id IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_one_void ON transactions (voids_txn_id) WHERE voids_txn_id IS NOT NULL",
    """CREATE TRIGGER IF NOT EXISTS trg_transactions_no_update BEFORE UPDATE ON transactions
       BEGIN SELECT RAISE(ABORT, 'transactions are append-only: void a transaction instead of editing it'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_transactions_no_delete BEFORE DELETE ON transactions
       BEGIN SELECT RAISE(ABORT, 'transactions are append-only: void a transaction instead of deleting it'); END""",
    """CREATE TABLE IF NOT EXISTS tax_lots (
        open_transaction_id INTEGER PRIMARY KEY REFERENCES transactions(txn_id),
        account_id          INTEGER NOT NULL REFERENCES accounts(account_id),
        instrument_id       INTEGER NOT NULL REFERENCES instruments(instrument_id),
        open_date           TEXT NOT NULL,
        original_quantity   REAL NOT NULL,
        remaining_quantity  REAL NOT NULL,
        unit_cost           REAL NOT NULL,
        basis_remaining     REAL NOT NULL,
        source_type         TEXT NOT NULL,
        status              TEXT NOT NULL CHECK (status IN ('OPEN', 'CLOSED'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_tax_lots_account ON tax_lots (account_id, instrument_id, status)",
    """CREATE TABLE IF NOT EXISTS lot_closures (
        closure_id           INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id           INTEGER NOT NULL REFERENCES accounts(account_id),
        open_transaction_id  INTEGER NOT NULL REFERENCES transactions(txn_id),
        close_transaction_id INTEGER NOT NULL REFERENCES transactions(txn_id),
        instrument_id        INTEGER NOT NULL REFERENCES instruments(instrument_id),
        quantity             REAL NOT NULL,
        proceeds             REAL NOT NULL,
        cost_basis           REAL NOT NULL,
        realized_gain        REAL NOT NULL,
        open_date            TEXT NOT NULL,
        close_date           TEXT NOT NULL,
        holding_period       TEXT NOT NULL CHECK (holding_period IN ('SHORT', 'LONG')),
        method               TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_lot_closures_account ON lot_closures (account_id, close_date)",
]


_ADDED_COLUMNS = [("contribution_source", "TEXT"), ("clearance_status", "TEXT"), ("clearance_approval_id", "INTEGER"),
                  ("import_run_id", "INTEGER")]


def create_schema(conn: sqlite3.Connection) -> None:
    from portfolio_agent.tools.db import migrate_columns
    for ddl in LEDGER_DDL:
        conn.execute(ddl)
    _upgrade_type_check(conn)
    migrate_columns(conn, "transactions", _ADDED_COLUMNS)           # a ledger created before these columns existed gains them (ADD COLUMN is not an UPDATE)


def _upgrade_type_check(conn: sqlite3.Connection) -> None:
    """
    A database whose transactions table was created before OPENING_BALANCE existed has the old CHECK on txn_type, which SQLite cannot alter.
    Rebuild it once: a new table with the current definition, every row copied with its id, the old table dropped, the new one renamed (the
    parent is never renamed, so the foreign keys other tables hold on it keep pointing at the right name), then indexes and triggers are
    recreated. Nothing in the ledger changes. A table that already allows OPENING_BALANCE is left alone.
    """
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'transactions'").fetchone()
    if row is None or "OPENING_BALANCE" in (row[0] or ""):
        return
    cols = [r[1] for r in conn.execute("PRAGMA table_info(transactions)").fetchall()]
    conn.execute(_transactions_ddl("transactions_rebuilt"))
    conn.execute(f"INSERT INTO transactions_rebuilt ({', '.join(cols)}) SELECT {', '.join(cols)} FROM transactions")
    conn.execute("DROP TABLE transactions")                       # its indexes and triggers go with it
    conn.execute("ALTER TABLE transactions_rebuilt RENAME TO transactions")
    for ddl in LEDGER_DDL[1:]:                                    # indexes and triggers (idempotent), the derived tables already exist
        conn.execute(ddl)


# ── transactions ──────────────────────────────────────────────────────────────

_INSERT_COLS = ("account_id", "instrument_id", "txn_type", "trade_date", "settle_date", "quantity", "price", "fees", "amount",
                "split_numerator", "split_denominator", "lot_method", "lot_selection_json", "voids_txn_id", "external_id", "memo",
                "source", "contribution_source", "clearance_status", "clearance_approval_id", "import_run_id", "created_by", "request_id")


def insert_transaction(conn: sqlite3.Connection, fields: dict) -> int:
    row = {c: fields.get(c) for c in _INSERT_COLS}
    row["fees"] = row["fees"] or 0.0
    row["amount"] = row["amount"] or 0.0
    cur = conn.execute(f"INSERT INTO transactions ({', '.join(_INSERT_COLS)}) VALUES ({', '.join('?' * len(_INSERT_COLS))})",
                       [row[c] for c in _INSERT_COLS])
    return cur.lastrowid


def get_transaction(conn: sqlite3.Connection, txn_id: int) -> dict | None:
    r = conn.execute("SELECT t.*, i.ticker FROM transactions t LEFT JOIN instruments i ON i.instrument_id = t.instrument_id "
                     "WHERE t.txn_id = ?", [txn_id]).fetchone()
    return _row(r) if r else None


def find_by_external_id(conn: sqlite3.Connection, account_id: int, external_id: str) -> dict | None:
    r = conn.execute("SELECT t.*, i.ticker FROM transactions t LEFT JOIN instruments i ON i.instrument_id = t.instrument_id "
                     "WHERE t.account_id = ? AND t.external_id = ?", [account_id, external_id]).fetchone()
    return _row(r) if r else None


def _row(r) -> dict:
    d = dict(r)
    sel = d.pop("lot_selection_json", None)
    d["lot_selection"] = json.loads(sel) if sel else None
    return d


def list_transactions(conn: sqlite3.Connection, account_id: int, *, include_voided: bool = True) -> list[dict]:
    """The account's rows oldest first, each with `voided_by` (the id of the VOID row that cancels it, or None)."""
    rows = [_row(r) for r in conn.execute(
        "SELECT t.*, i.ticker FROM transactions t LEFT JOIN instruments i ON i.instrument_id = t.instrument_id "
        "WHERE t.account_id = ? ORDER BY t.trade_date, t.txn_id", [account_id]).fetchall()]
    voided = {r["voids_txn_id"]: r["txn_id"] for r in rows if r["txn_type"] == "VOID"}
    for r in rows:
        r["voided_by"] = voided.get(r["txn_id"])
    return rows if include_voided else [r for r in rows if r["voided_by"] is None and r["txn_type"] != "VOID"]


def count_transactions(conn: sqlite3.Connection, account_id: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM transactions WHERE account_id = ?", [account_id]).fetchone()[0]


def txns_for_replay(conn: sqlite3.Connection, account_id: int, *, through: str | None = None) -> list[Txn]:
    """
    The account's transactions as the replay engine reads them. A ledger account has a `ledger_start_date` (the day its opening balances
    were recorded): transactions dated before it are history only and are left out, together with any VOID that points at one. `through`
    (an ISO date) drops everything dated after it, for "as of" views.
    """
    rows = list_transactions(conn, account_id)
    acct = conn.execute("SELECT ledger_start_date FROM accounts WHERE account_id = ?", [account_id]).fetchone()
    start = acct[0] if acct else None
    if start:
        rows = [r for r in rows if r["trade_date"] >= start]
    if through:
        rows = [r for r in rows if r["trade_date"] <= through]
    kept = {r["txn_id"] for r in rows}
    rows = [r for r in rows if r["txn_type"] != "VOID" or r["voids_txn_id"] in kept]
    return [to_txn(r) for r in rows]


def to_txn(r: dict) -> Txn:
    return Txn(txn_id=r["txn_id"], account_id=r["account_id"], txn_type=r["txn_type"], trade_date=r["trade_date"],
               instrument_id=r["instrument_id"], quantity=r["quantity"], price=r["price"], fees=r["fees"] or 0.0, amount=r["amount"] or 0.0,
               split_numerator=r["split_numerator"], split_denominator=r["split_denominator"], lot_method=r["lot_method"],
               lot_selection=r.get("lot_selection"), voids_txn_id=r["voids_txn_id"])


# ── derived: lots and closures ────────────────────────────────────────────────

def replace_derived(conn: sqlite3.Connection, account_id: int, state: LedgerState) -> None:
    """Rebuild the account's tax_lots and lot_closures from a replay result (same transaction as the ledger change that caused it)."""
    conn.execute("DELETE FROM lot_closures WHERE account_id = ?", [account_id])
    conn.execute("DELETE FROM tax_lots WHERE account_id = ?", [account_id])
    conn.executemany(
        "INSERT INTO tax_lots (open_transaction_id, account_id, instrument_id, open_date, original_quantity, remaining_quantity, unit_cost, "
        "basis_remaining, source_type, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(l.open_transaction_id, account_id, l.instrument_id, l.open_date, l.original_quantity, l.remaining_quantity, l.unit_cost,
          l.basis_remaining, l.source_type, "OPEN" if l.remaining_quantity > 1e-9 else "CLOSED") for l in state.lots.values()])
    conn.executemany(
        "INSERT INTO lot_closures (account_id, open_transaction_id, close_transaction_id, instrument_id, quantity, proceeds, cost_basis, "
        "realized_gain, open_date, close_date, holding_period, method) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(account_id, c.open_transaction_id, c.close_transaction_id, c.instrument_id, c.quantity, c.proceeds, c.cost_basis, c.realized_gain,
          c.open_date, c.close_date, c.holding_period, c.method) for c in state.closures])


def list_lots(conn: sqlite3.Connection, account_id: int, *, open_only: bool = True) -> list[dict]:
    rows = conn.execute(
        "SELECT l.*, i.ticker FROM tax_lots l JOIN instruments i ON i.instrument_id = l.instrument_id WHERE l.account_id = ?"
        + (" AND l.status = 'OPEN'" if open_only else "") + " ORDER BY i.ticker, l.open_date, l.open_transaction_id", [account_id]).fetchall()
    return [dict(r) for r in rows]


def list_closures(conn: sqlite3.Connection, account_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT c.*, i.ticker FROM lot_closures c JOIN instruments i ON i.instrument_id = c.instrument_id WHERE c.account_id = ? "
        "ORDER BY c.close_date, c.closure_id", [account_id]).fetchall()
    return [dict(r) for r in rows]


# ── compliance / import helpers ───────────────────────────────────────────────

def clearance_counts(conn: sqlite3.Connection, portfolio_id: int) -> dict:
    """Counts of the portfolio's EFFECTIVE (not voided) trades by clearance_status, and the tickers with a 'missing' one. Non-paper accounts only."""
    rows = conn.execute(
        """SELECT t.clearance_status AS s, COUNT(*) AS n, GROUP_CONCAT(DISTINCT i.ticker) AS tickers
           FROM transactions t JOIN accounts a ON a.account_id = t.account_id LEFT JOIN instruments i ON i.instrument_id = t.instrument_id
           WHERE a.portfolio_id = ? AND a.is_paper = 0 AND t.clearance_status IS NOT NULL AND t.txn_type IN ('BUY', 'SELL')
             AND NOT EXISTS (SELECT 1 FROM transactions v WHERE v.voids_txn_id = t.txn_id)
           GROUP BY t.clearance_status""", [portfolio_id]).fetchall()
    out = {"missing": 0, "unknown": 0, "approved": 0, "not_required": 0, "missing_tickers": []}
    for r in rows:
        if r["s"] in out:
            out[r["s"]] = r["n"]
        if r["s"] == "missing":
            out["missing_tickers"] = sorted(t for t in (r["tickers"] or "").split(",") if t)
    return out


def existing_for_dedupe(conn: sqlite3.Connection, account_id: int) -> tuple[set, dict]:
    """(external ids in use, {(trade_date, txn_type, ticker, quantity, cash_amount): count}) over the account's NON-voided transactions."""
    ext, keys = set(), {}
    for r in list_transactions(conn, account_id, include_voided=False):
        if r["external_id"]:
            ext.add(r["external_id"])
        k = dedupe_key(r["trade_date"], r["txn_type"], r["ticker"], r["quantity"], r["amount"])
        keys[k] = keys.get(k, 0) + 1
    return ext, keys


def dedupe_key(trade_date, txn_type, ticker, quantity, amount) -> tuple:
    return (trade_date, txn_type, (ticker or "").upper(), None if quantity is None else round(float(quantity), 6), round(float(amount or 0.0), 2))
