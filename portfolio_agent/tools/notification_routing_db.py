"""
Notification routing — PRIVATE, owner-scoped. For each person: which alert categories go to which channels.

  Table notification_routing (owner, category, channel, enabled)  — one row per category × active channel.

Defaults for a new person (a row per category × EMAIL / PUSH, created the first time their routing is read):

  MARKET_EVENT      PUSH + EMAIL
  PORTFOLIO_HEALTH  EMAIL
  COMPLIANCE        PUSH + EMAIL
  DAILY_DIGEST      EMAIL only, OFF by default (a digest is a single email; there is no push digest)
  SYSTEM            every active channel

People who had the old single `channel` setting keep exactly what they had: 'email' turns EMAIL on for the categories that
setting covered (MARKET_EVENT, PORTFOLIO_HEALTH) and nothing else; 'none' (and the unsupported 'slack') turns everything off.
That migration runs once when the user_notifications table gains its new columns and never changes a row that already exists.

Routing alone never sends anything. A channel receives a message only if ALL of these hold — see
services.notification_service.explain_channels: it is an active channel, it is switched on globally, routing is enabled for
this category, the person has a contact on it, that contact is verified, and it is not opted out.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from portfolio_agent.domain import ACTIVE_CHANNELS, AlertCategory, Channel
from portfolio_agent.tools.db import db_conn

C = AlertCategory
E, P = Channel.EMAIL.value, Channel.PUSH.value

# Which channels a category can use at all (a digest is an email).
ALLOWED_CHANNELS: dict[str, tuple[str, ...]] = {
    C.MARKET_EVENT.value: (E, P), C.PORTFOLIO_HEALTH.value: (E, P), C.COMPLIANCE.value: (E, P),
    C.DAILY_DIGEST.value: (E,), C.SYSTEM.value: (E, P),
}
# Seed defaults for a NEW person (category -> channel -> enabled).
DEFAULT_ROUTING: dict[str, dict[str, bool]] = {
    C.MARKET_EVENT.value: {P: True, E: True},
    C.PORTFOLIO_HEALTH.value: {E: True, P: False},
    C.COMPLIANCE.value: {P: True, E: True},
    C.DAILY_DIGEST.value: {E: False},
    C.SYSTEM.value: {E: True, P: True},
}
# What the legacy single `channel = 'email'` setting actually covered.
LEGACY_EMAIL_CATEGORIES = (C.MARKET_EVENT.value, C.PORTFOLIO_HEALTH.value)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def legacy_routing(legacy_channel: str | None) -> dict[str, dict[str, bool]]:
    """The routing equivalent of the old single channel: email -> EMAIL for the covered categories only; anything else
    ('none', 'slack', unknown) -> everything off."""
    on = str(legacy_channel or "").lower() == "email"
    return {cat: {ch: bool(on and ch == E and cat in LEGACY_EMAIL_CATEGORIES) for ch in chans}
            for cat, chans in ALLOWED_CHANNELS.items()}


# ── Connection / schema ───────────────────────────────────────────────────────

_schema_ready: set[str] = set()


def create_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notification_routing (
            owner       TEXT NOT NULL,
            category    TEXT NOT NULL,
            channel     TEXT NOT NULL,
            enabled     INTEGER NOT NULL DEFAULT 0,
            updated_at  TEXT NOT NULL,
            PRIMARY KEY (owner, category, channel)
        )""")


def ensure_schema_once() -> None:
    from portfolio_agent.tools import db as _db_module
    key = str(_db_module.DB_PATH)
    if key not in _schema_ready:
        with db_conn(setup=lambda c: (create_schema(c), c.commit())):
            pass
        _schema_ready.add(key)


@contextmanager
def _conn(conn: sqlite3.Connection | None):
    if conn is not None:
        # The caller's transaction may already hold the write lock (the schema upgrade of user_notifications does), so the
        # table is created ON THIS connection — never from a second one, which would wait on that lock forever.
        create_schema(conn)
        yield conn
        return
    ensure_schema_once()
    with db_conn() as c:
        try:
            yield c
        except BaseException:
            c.rollback()
            raise
        else:
            c.commit()


def _owner(owner: str) -> str:
    if not owner:
        raise ValueError("notification_routing: owner is required")
    return owner


# ── Seeding ───────────────────────────────────────────────────────────────────

def seed_owner(owner: str, *, grid: dict[str, dict[str, bool]] | None = None, conn: sqlite3.Connection | None = None) -> int:
    """Create any missing rows of the person's grid (default: DEFAULT_ROUTING). Never overwrites an existing row.
    Returns the number of rows created."""
    grid = grid or DEFAULT_ROUTING
    now, created = _now(), 0
    with _conn(conn) as c:
        for cat, chans in grid.items():
            for ch, enabled in chans.items():
                created += c.execute(
                    "INSERT OR IGNORE INTO notification_routing (owner, category, channel, enabled, updated_at) VALUES (?, ?, ?, ?, ?)",
                    [_owner(owner), cat, ch, int(bool(enabled)), now]).rowcount
    return created


def migrate_legacy_rows(conn: sqlite3.Connection) -> int:
    """
    One-time: for every user_notifications row flagged routing_source = 'LEGACY' (a row that existed before routing did),
    create its routing from the old channel and mark it 'MIGRATED'. Runs on the caller's connection inside the schema
    upgrade; idempotent (a MIGRATED row is never touched again, and existing routing rows are never overwritten).
    """
    create_schema(conn)
    rows = conn.execute("SELECT owner, channel FROM user_notifications WHERE routing_source = 'LEGACY'").fetchall()
    for owner, channel in [(r[0], r[1]) for r in rows]:
        seed_owner(owner, grid=legacy_routing(channel), conn=conn)
        conn.execute("UPDATE user_notifications SET routing_source = 'MIGRATED' WHERE owner = ?", [owner])
    return len(rows)


# ── Reads / writes ────────────────────────────────────────────────────────────

def get_routing(owner: str, *, conn: sqlite3.Connection | None = None) -> dict[str, dict[str, bool]]:
    """{category: {channel: enabled}} for the person's grid (default rows are created on first read)."""
    with _conn(conn) as c:
        if c.execute("SELECT 1 FROM notification_routing WHERE owner = ? LIMIT 1", [_owner(owner)]).fetchone() is None:
            seed_owner(owner, conn=c)
        rows = c.execute("SELECT category, channel, enabled FROM notification_routing WHERE owner = ?", [owner]).fetchall()
    out: dict[str, dict[str, bool]] = {cat: {} for cat in ALLOWED_CHANNELS}
    for r in rows:
        out.setdefault(r["category"], {})[r["channel"]] = bool(r["enabled"])
    return out


def is_routed(owner: str, category: str, channel: str, *, conn: sqlite3.Connection | None = None) -> bool:
    return bool(get_routing(owner, conn=conn).get(str(category), {}).get(str(channel), False))


def set_routing(owner: str, category: str, channel: str, enabled: bool, *, conn: sqlite3.Connection | None = None) -> bool:
    """Turn one category × channel on or off. Only ACTIVE channels, and only the channels a category allows, can be
    switched; anything else is refused with a clear message. Returns the previous value."""
    try:
        cat = AlertCategory(str(category).upper()).value
    except ValueError:
        raise ValueError(f"unknown alert category {category!r}") from None
    try:
        ch = Channel(str(channel).upper())
    except ValueError:
        raise ValueError(f"unknown channel {channel!r}") from None
    if ch not in ACTIVE_CHANNELS:
        raise ValueError(f"{ch.value} is not available yet")
    if ch.value not in ALLOWED_CHANNELS[cat]:
        raise ValueError(f"{cat.replace('_', ' ').title()} is not sent by {ch.value.title()}")
    with _conn(conn) as c:
        before = get_routing(owner, conn=c)[cat].get(ch.value, False)
        c.execute("INSERT INTO notification_routing (owner, category, channel, enabled, updated_at) VALUES (?, ?, ?, ?, ?) "
                  "ON CONFLICT(owner, category, channel) DO UPDATE SET enabled = excluded.enabled, updated_at = excluded.updated_at",
                  [owner, cat, ch.value, int(bool(enabled)), _now()])
    return before
