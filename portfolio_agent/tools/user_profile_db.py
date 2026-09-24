"""
User profile database — single-row store for the user's identity, mandate
limits, and preferences.

Table: user_profile — exactly one row (id = 1, enforced by CHECK). The row is
seeded with defaults the first time the table is touched and never overwritten
after that; edits go through update_user_profile().

Columns:
  display_name, base_currency (USD), timezone (America/Chicago),
  max_sector_pct (25.0), max_issuer_pct (15.0),
  max_per_candidate_pct_of_cash (0.4), max_post_trade_position_pct (0.15),
  sector_exclusions (JSON list), created_at, updated_at
  Compliance: employer, pre_clearance_required (bool, default true),
  blackout_start / blackout_end (ISO dates), disclaimer_accepted_at
  Horizons:   preferred_horizons (JSON list, default all of 5d/21d/63d/250d)
  Ownership:  owner (the verified identity this row belongs to — see below)

Ownership / scoping
--------------------
The table's shape (id = 1, CHECK-enforced) still allows exactly one row — this
phase does not lift that, since doing so would itself start enabling multi-user
storage ahead of the isolation gate. What it adds is the `owner` column and an
AUTHORIZED read/write path (get_user_profile_for / update_user_profile_for)
that requires a RequestContext carrying a verified Identity and refuses to
serve or mutate a row it does not own — laying the boundary a later phase can
widen to more than one row without changing any caller of the *_for functions.

A pre-existing row (from before this column existed) has owner backfilled to
LOCAL_OWNER exactly once via an idempotent migration — the one, explicitly
configured existing owner this deployment has always served, never "whoever
logs in first." A fresh install's seeded row is owned by LOCAL_OWNER directly.

get_user_profile() / update_user_profile() (no ctx) remain as the pre-existing
unscoped convenience path — every current caller in the app still uses them.
They are not removed in this phase; see the Phase 2 report's inventory of
callers still on the unscoped path.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from portfolio_agent.domain import ALL_HORIZON_LABELS, LOCAL_OWNER, UserProfile
from portfolio_agent.tools.db import db_conn, migrate_columns, to_json_list

_LIST_FIELDS = frozenset({"sector_exclusions", "preferred_horizons"})
_DEFAULT_HORIZONS_JSON = to_json_list(list(ALL_HORIZON_LABELS))
_BOOL_FIELDS = frozenset({"pre_clearance_required"})

# Every column a caller may set via update_user_profile(). id/created_at/updated_at
# are managed here and rejected as input.
_UPDATABLE_FIELDS = frozenset({
    "display_name",
    "base_currency",
    "timezone",
    "max_sector_pct",
    "max_issuer_pct",
    "max_per_candidate_pct_of_cash",
    "max_post_trade_position_pct",
    "sector_exclusions",
    # compliance
    "employer",
    "pre_clearance_required",
    "blackout_start",
    "blackout_end",
    "disclaimer_accepted_at",
    # horizons
    "preferred_horizons",
})

# Columns added after the first release — applied to existing tables via
# migrate_columns() and also present in CREATE TABLE for fresh installs.
_MIGRATED_COLUMNS: list[tuple[str, str]] = [
    ("employer",               "TEXT"),
    ("pre_clearance_required", "INTEGER NOT NULL DEFAULT 1"),
    ("blackout_start",         "TEXT"),
    ("blackout_end",           "TEXT"),
    ("disclaimer_accepted_at", "TEXT"),
    ("preferred_horizons",     f"TEXT NOT NULL DEFAULT '{_DEFAULT_HORIZONS_JSON}'"),
    ("owner",                  "TEXT"),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _db():
    def _setup(conn: sqlite3.Connection) -> None:
        _create_schema(conn)
        _migrate(conn)
        _seed(conn)
        conn.commit()
    with db_conn(setup=_setup) as conn:
        yield conn


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS user_profile (
            id                            INTEGER PRIMARY KEY CHECK (id = 1),
            display_name                  TEXT,
            base_currency                 TEXT NOT NULL DEFAULT 'USD',
            timezone                      TEXT NOT NULL DEFAULT 'America/Chicago',
            max_sector_pct                REAL NOT NULL DEFAULT 25.0,
            max_issuer_pct                REAL NOT NULL DEFAULT 15.0,
            max_per_candidate_pct_of_cash REAL NOT NULL DEFAULT 0.4,
            max_post_trade_position_pct   REAL NOT NULL DEFAULT 0.15,
            sector_exclusions             TEXT NOT NULL DEFAULT '[]',
            created_at                    TEXT NOT NULL,
            updated_at                    TEXT NOT NULL,
            employer                      TEXT,
            pre_clearance_required        INTEGER NOT NULL DEFAULT 1,
            blackout_start                TEXT,
            blackout_end                  TEXT,
            disclaimer_accepted_at        TEXT,
            preferred_horizons            TEXT NOT NULL DEFAULT '{_DEFAULT_HORIZONS_JSON}',
            owner                         TEXT
        )
    """)


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the first release to an existing table,
    then backfill ownership on any pre-existing row (idempotent: a no-op once
    owner is set, never touches id or any other column)."""
    migrate_columns(conn, "user_profile", _MIGRATED_COLUMNS)
    conn.execute("UPDATE user_profile SET owner = ? WHERE owner IS NULL", (LOCAL_OWNER,))
    # One-time rename: LOCAL_OWNER was the placeholder "local" before this
    # deployment's single user was named "sumanth_b" ahead of registration/
    # login. Idempotent -- a no-op once no row still says "local".
    conn.execute("UPDATE user_profile SET owner = ? WHERE owner = 'local'", (LOCAL_OWNER,))


def _seed(conn: sqlite3.Connection) -> None:
    """Insert the singleton row with column defaults if — and only if — it is
    missing. INSERT OR IGNORE means an existing row is never touched."""
    now = _now()
    conn.execute(
        "INSERT OR IGNORE INTO user_profile (id, owner, created_at, updated_at) VALUES (1, ?, ?, ?)",
        (LOCAL_OWNER, now, now),
    )


def get_user_profile() -> UserProfile:
    """Return the user's profile. Always returns a row (seeded on first access)."""
    with _db() as conn:
        row = conn.execute("SELECT * FROM user_profile WHERE id = 1").fetchone()
    return UserProfile.from_db_row(dict(row))


def _normalize_fields(fields: dict) -> dict:
    unknown = set(fields) - _UPDATABLE_FIELDS
    if unknown:
        raise ValueError(
            f"Unknown user_profile field(s): {', '.join(sorted(unknown))}. "
            f"Allowed: {', '.join(sorted(_UPDATABLE_FIELDS))}"
        )
    values: dict = {}
    for key, val in fields.items():
        if key in _LIST_FIELDS:
            values[key] = to_json_list(val)
        elif key in _BOOL_FIELDS:
            values[key] = 1 if val else 0
        else:
            values[key] = val
    values["updated_at"] = _now()
    return values


def update_user_profile(**fields) -> UserProfile:
    """
    Update one or more profile fields and return the refreshed profile.

    Raises ValueError for unknown field names. List fields (sector_exclusions)
    are JSON-encoded, bool fields are stored as 0/1; updated_at is bumped
    automatically. Unscoped: always the id=1 row, regardless of who is asking
    — see get_user_profile_for/update_user_profile_for for the authorized path.
    """
    if not fields:
        return get_user_profile()
    values = _normalize_fields(fields)
    assignments = ", ".join(f"{col} = ?" for col in values)
    with _db() as conn:
        conn.execute(
            f"UPDATE user_profile SET {assignments} WHERE id = 1",
            list(values.values()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM user_profile WHERE id = 1").fetchone()
    return UserProfile.from_db_row(dict(row))


# ── Authorized (ctx-scoped) path ──────────────────────────────────────────────

def get_user_profile_for(ctx) -> UserProfile:
    """
    Authorized read: the profile owned by ctx's verified identity.

    Raises account_service.NotAuthorized if no profile is owned by that
    identity — never falls back to serving a different owner's row.
    """
    from portfolio_agent.services.account_service import NotAuthorized
    from portfolio_agent.services.context import require_context

    require_context(ctx)
    with _db() as conn:
        row = conn.execute("SELECT * FROM user_profile WHERE owner = ?", (ctx.actor,)).fetchone()
    if row is None:
        raise NotAuthorized(f"{ctx.actor!r} has no profile here")
    return UserProfile.from_db_row(dict(row))


def update_user_profile_for(ctx, **fields) -> UserProfile:
    """Authorized write: only updates the row owned by ctx's verified identity.
    Raises NotAuthorized if that identity owns no row — see get_user_profile_for."""
    from portfolio_agent.services.account_service import NotAuthorized

    get_user_profile_for(ctx)   # authorization check before any write, same as a bare read
    if not fields:
        return get_user_profile_for(ctx)
    values = _normalize_fields(fields)
    assignments = ", ".join(f"{col} = ?" for col in values)
    with _db() as conn:
        n = conn.execute(
            f"UPDATE user_profile SET {assignments} WHERE owner = ?",
            [*values.values(), ctx.actor],
        ).rowcount
        conn.commit()
        if n == 0:
            raise NotAuthorized(f"{ctx.actor!r} has no profile here")
        row = conn.execute("SELECT * FROM user_profile WHERE owner = ?", (ctx.actor,)).fetchone()
    return UserProfile.from_db_row(dict(row))
