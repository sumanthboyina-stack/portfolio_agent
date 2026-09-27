"""
User profile database — per-owner store for each member's identity, mandate
limits, and preferences.

Table: user_profile — one row per owner, UNIQUE(owner). A row is auto-
provisioned with defaults the first time that owner is looked up (via
get_user_profile_for / get_user_profile_by_owner) and never overwritten after
that except through an explicit update.

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
This used to be a single-row table (id = 1, CHECK-enforced) — every member
shared one profile, so opening the Profile page as anyone but the deployment's
original owner showed (and could overwrite) that owner's name, employer and
mandate limits. _migrate_owner_scoping() rebuilds a pre-existing table into a
real per-owner shape once (mirroring restricted_list_db's identical rebuild
for the same reason), carrying the one existing row's data forward under
LOCAL_OWNER.

get_user_profile_for(ctx) / update_user_profile_for(ctx, ...) are the
AUTHORIZED path: they require a RequestContext carrying a verified Identity,
auto-provision a default row for that identity on first access, and never
serve or mutate a row belonging to a different owner.
get_user_profile_by_owner(owner) is the same auto-provisioning lookup for
server-side callers that already trust a raw owner string but have no
RequestContext to construct (see its own docstring).

get_user_profile() / update_user_profile() (no ctx, no owner) remain as an
unscoped convenience path that always resolves to LOCAL_OWNER's row (id = 1,
preserved across the rebuild) — used by CLI/batch callers that have no
logged-in identity at all.
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
            id                            INTEGER PRIMARY KEY AUTOINCREMENT,
            owner                         TEXT NOT NULL UNIQUE,
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


_LEGACY_COLUMNS = [
    "id", "display_name", "base_currency", "timezone", "max_sector_pct", "max_issuer_pct",
    "max_per_candidate_pct_of_cash", "max_post_trade_position_pct", "sector_exclusions",
    "created_at", "updated_at", "employer", "pre_clearance_required", "blackout_start",
    "blackout_end", "disclaimer_accepted_at", "preferred_horizons", "owner",
]


def _migrate_owner_scoping(conn: sqlite3.Connection) -> None:
    """
    Rebuild user_profile from the CHECK(id = 1) singleton shape to a real
    per-owner table (UNIQUE(owner)) — the bug this closes: every member
    shared ONE profile row, so opening the Profile page as anyone but
    LOCAL_OWNER showed (and could overwrite) their name, employer and
    mandate limits. Mirrors restricted_list_db._migrate_owner_scoping's
    identical rebuild. Runs once; a no-op once the new shape already exists.
    owner is already backfilled by the caller (_migrate) before this runs, so
    the one existing row carries it forward untouched, keeping its original
    id (1) so get_user_profile()'s WHERE id = 1 still resolves to it.
    """
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'user_profile'"
    ).fetchone()
    if not ddl or "CHECK" not in (ddl[0] or ""):
        return
    conn.execute("ALTER TABLE user_profile RENAME TO user_profile_legacy_v1")
    _create_schema(conn)
    cols = ", ".join(_LEGACY_COLUMNS)
    conn.execute(f"INSERT INTO user_profile ({cols}) SELECT {cols} FROM user_profile_legacy_v1")
    conn.execute("DROP TABLE user_profile_legacy_v1")


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the first release to an existing table,
    backfill ownership on any pre-existing row (idempotent: a no-op once
    owner is set), then rebuild the table from the old CHECK(id = 1) shape to
    a real per-owner one — see _migrate_owner_scoping()."""
    migrate_columns(conn, "user_profile", _MIGRATED_COLUMNS)
    conn.execute("UPDATE user_profile SET owner = ? WHERE owner IS NULL", (LOCAL_OWNER,))
    # One-time rename: LOCAL_OWNER was the placeholder "local" before this
    # deployment's single user was named "sumanth_b" ahead of registration/
    # login. Idempotent -- a no-op once no row still says "local".
    conn.execute("UPDATE user_profile SET owner = ? WHERE owner = 'local'", (LOCAL_OWNER,))
    _migrate_owner_scoping(conn)


def _seed(conn: sqlite3.Connection) -> None:
    """Insert LOCAL_OWNER's row with column defaults if — and only if — it is
    missing. INSERT OR IGNORE means an existing row is never touched."""
    now = _now()
    conn.execute(
        "INSERT OR IGNORE INTO user_profile (id, owner, created_at, updated_at) VALUES (1, ?, ?, ?)",
        (LOCAL_OWNER, now, now),
    )


def _seed_owner(conn: sqlite3.Connection, owner: str) -> None:
    """Auto-provision a default row for *owner* if it doesn't already have
    one. INSERT OR IGNORE + UNIQUE(owner) means a race just loses to the
    unique constraint and is silently ignored, same idiom as _seed()."""
    now = _now()
    conn.execute(
        "INSERT OR IGNORE INTO user_profile (owner, created_at, updated_at) VALUES (?, ?, ?)",
        (owner, now, now),
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

def get_user_profile_by_owner(owner: str) -> UserProfile:
    """
    Unauthenticated-by-design lookup by a raw owner string, mirroring
    holdings_db.get_portfolio_by_owner(). For server-side callers that
    already trust *owner* (e.g. an ADK tool reading tool_context.state["owner"],
    set by the web layer from a verified login — never from model output) but
    have no RequestContext/Identity to construct, since only
    identity_from_verified_login() (web/auth.py) or a real login can mint one.

    Auto-provisions a default row for *owner* on first access (mirrors
    get_user_profile()'s seed-on-first-touch for LOCAL_OWNER) — a newly
    invited member gets their own mandate limits/notification prefs the
    moment anything reads them, never a different owner's row.
    """
    if not owner:
        raise ValueError("user_profile: owner is required")
    with _db() as conn:
        _seed_owner(conn, owner)
        conn.commit()
        row = conn.execute("SELECT * FROM user_profile WHERE owner = ?", (owner,)).fetchone()
    return UserProfile.from_db_row(dict(row))


def get_user_profile_for(ctx) -> UserProfile:
    """Authorized read: the profile owned by ctx's verified identity,
    auto-provisioned on first access — never falls back to serving a
    different owner's row."""
    from portfolio_agent.services.context import require_context

    require_context(ctx)
    return get_user_profile_by_owner(ctx.actor)


def update_user_profile_for(ctx, **fields) -> UserProfile:
    """Authorized write: only updates the row owned by ctx's verified
    identity (auto-provisioned by get_user_profile_for if this is that
    identity's first write)."""
    get_user_profile_for(ctx)   # authorization check + auto-provision before any write
    if not fields:
        return get_user_profile_for(ctx)
    values = _normalize_fields(fields)
    assignments = ", ".join(f"{col} = ?" for col in values)
    with _db() as conn:
        conn.execute(
            f"UPDATE user_profile SET {assignments} WHERE owner = ?",
            [*values.values(), ctx.actor],
        )
        conn.commit()
        row = conn.execute("SELECT * FROM user_profile WHERE owner = ?", (ctx.actor,)).fetchone()
    return UserProfile.from_db_row(dict(row))


def list_all_profiles() -> list[UserProfile]:
    """Every owner's profile row — for callers that must consider every
    member (e.g. privacy.collect_private_terms(), which must not leak any
    member's name/employer into a shared/market-only artifact, not just
    LOCAL_OWNER's). Read-only: never auto-provisions."""
    with _db() as conn:
        rows = conn.execute("SELECT * FROM user_profile").fetchall()
    return [UserProfile.from_db_row(dict(r)) for r in rows]
