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
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from portfolio_agent.domain import ALL_HORIZON_LABELS, UserProfile
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
            preferred_horizons            TEXT NOT NULL DEFAULT '{_DEFAULT_HORIZONS_JSON}'
        )
    """)


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the first release to an existing table."""
    migrate_columns(conn, "user_profile", _MIGRATED_COLUMNS)


def _seed(conn: sqlite3.Connection) -> None:
    """Insert the singleton row with column defaults if — and only if — it is
    missing. INSERT OR IGNORE means an existing row is never touched."""
    now = _now()
    conn.execute(
        "INSERT OR IGNORE INTO user_profile (id, created_at, updated_at) VALUES (1, ?, ?)",
        (now, now),
    )


def get_user_profile() -> UserProfile:
    """Return the user's profile. Always returns a row (seeded on first access)."""
    with _db() as conn:
        row = conn.execute("SELECT * FROM user_profile WHERE id = 1").fetchone()
    return UserProfile.from_db_row(dict(row))


def update_user_profile(**fields) -> UserProfile:
    """
    Update one or more profile fields and return the refreshed profile.

    Raises ValueError for unknown field names. List fields (sector_exclusions)
    are JSON-encoded, bool fields are stored as 0/1; updated_at is bumped
    automatically.
    """
    unknown = set(fields) - _UPDATABLE_FIELDS
    if unknown:
        raise ValueError(
            f"Unknown user_profile field(s): {', '.join(sorted(unknown))}. "
            f"Allowed: {', '.join(sorted(_UPDATABLE_FIELDS))}"
        )
    if not fields:
        return get_user_profile()

    values: dict = {}
    for key, val in fields.items():
        if key in _LIST_FIELDS:
            values[key] = to_json_list(val)
        elif key in _BOOL_FIELDS:
            values[key] = 1 if val else 0
        else:
            values[key] = val
    values["updated_at"] = _now()

    assignments = ", ".join(f"{col} = ?" for col in values)
    with _db() as conn:
        conn.execute(
            f"UPDATE user_profile SET {assignments} WHERE id = 1",
            list(values.values()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM user_profile WHERE id = 1").fetchone()
    return UserProfile.from_db_row(dict(row))
