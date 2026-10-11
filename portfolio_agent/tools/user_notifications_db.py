"""
User notification preferences — per-owner store for the personal filter that
sits on top of the pipeline-wide event trigger.

Table: user_notifications — one row per owner, UNIQUE(owner), auto-provisioned
with defaults on first access and never overwritten after that except through
an explicit update. Rebuilt from a former CHECK(id = 1) singleton shape by
_migrate_owner_scoping() — see user_profile_db's identical rebuild for why.

Columns:
  channel                  'none' | 'email' | 'slack'   (default 'none') — DEPRECATED, kept readable. Delivery is decided by
                           notification routing (tools.notification_routing_db); this column is only what routing was
                           migrated FROM, and the legacy should_notify() below still honours it for old callers.
  min_severity_threshold   INTEGER, default 3 — mirrors config._DEFAULT_EVENT_DRIVEN
                           ["intraday_severity_threshold"]; the pipeline floor
                           still applies first, this is a second personal filter
  min_conviction_threshold INTEGER, nullable — only applied when a conviction is known
  quiet_hours_start/end    "HH:MM" in the profile's timezone, nullable;
                           start > end spans midnight (e.g. 22:00 → 07:00)
  content_level            'BRIEF' | 'DETAILED' (default BRIEF). BRIEF carries no amounts, position sizes or portfolio
                           values — safe on a lock screen. Email may be DETAILED; push is ALWAYS brief.
  quiet_hours_mode         'DEFER' | 'SUPPRESS' (default DEFER: hold and send when quiet hours end)
  digest_time              "HH:MM" in the profile's timezone (default 07:30) for the daily digest
  routing_source           'LEGACY' (a row that existed before routing) -> 'MIGRATED' once its routing rows were created
                           from the old channel; NULL for people added after routing existed (they get the defaults)
  created_at, updated_at

should_notify(...) is the single decision function the event detector and
scheduler call. It never raises on bad data — it fails closed (no notification)
with a reason string.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from portfolio_agent.domain import CONTENT_LEVELS, LOCAL_OWNER, NOTIFICATION_CHANNELS, QUIET_MODES, UserNotificationPrefs
from portfolio_agent.tools.db import db_conn, migrate_columns

_UPDATABLE_FIELDS = frozenset({
    "channel",
    "min_severity_threshold",
    "min_conviction_threshold",
    "quiet_hours_start",
    "quiet_hours_end",
    "content_level",
    "quiet_hours_mode",
    "digest_time",
})


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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_notifications (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            owner                    TEXT NOT NULL UNIQUE,
            channel                  TEXT NOT NULL DEFAULT 'none',
            min_severity_threshold   INTEGER NOT NULL DEFAULT 3,
            min_conviction_threshold INTEGER,
            quiet_hours_start        TEXT,
            quiet_hours_end          TEXT,
            created_at               TEXT NOT NULL,
            updated_at               TEXT NOT NULL,
            content_level            TEXT NOT NULL DEFAULT 'BRIEF',
            quiet_hours_mode         TEXT NOT NULL DEFAULT 'DEFER',
            digest_time              TEXT NOT NULL DEFAULT '07:30',
            routing_source           TEXT
        )
    """)


_LEGACY_COLUMNS = [
    "id", "channel", "min_severity_threshold", "min_conviction_threshold",
    "quiet_hours_start", "quiet_hours_end", "created_at", "updated_at", "owner",
]


def _migrate_owner_scoping(conn: sqlite3.Connection) -> None:
    """Rebuild user_notifications from the CHECK(id = 1) singleton shape to a
    real per-owner table (UNIQUE(owner)) — see user_profile_db's identical
    rebuild. Runs once; a no-op once the new shape already exists."""
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'user_notifications'"
    ).fetchone()
    if not ddl or "CHECK" not in (ddl[0] or ""):
        return
    conn.execute("ALTER TABLE user_notifications RENAME TO user_notifications_legacy_v1")
    _create_schema(conn)
    cols = ", ".join(_LEGACY_COLUMNS)
    conn.execute(f"INSERT INTO user_notifications ({cols}) SELECT {cols} FROM user_notifications_legacy_v1")
    conn.execute("DROP TABLE user_notifications_legacy_v1")


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the first release, backfill ownership on
    any pre-existing row to LOCAL_OWNER (idempotent — a no-op once set), then
    rebuild to a real per-owner shape — see _migrate_owner_scoping()."""
    existing = {r[1] for r in conn.execute("PRAGMA table_info(user_notifications)").fetchall()}
    routing_is_new = "routing_source" not in existing           # an existing table that predates notification routing
    migrate_columns(conn, "user_notifications", [
        ("min_conviction_threshold", "INTEGER"),
        ("quiet_hours_start",        "TEXT"),
        ("quiet_hours_end",          "TEXT"),
        ("owner",                    "TEXT"),
        ("content_level",            "TEXT NOT NULL DEFAULT 'BRIEF'"),
        ("quiet_hours_mode",         "TEXT NOT NULL DEFAULT 'DEFER'"),
        ("digest_time",              "TEXT NOT NULL DEFAULT '07:30'"),
        ("routing_source",           "TEXT"),
    ])
    conn.execute("UPDATE user_notifications SET owner = ? WHERE owner IS NULL", (LOCAL_OWNER,))
    _migrate_owner_scoping(conn)
    if routing_is_new:
        # Everyone already here keeps what they had: their old single channel becomes routing (see notification_routing_db).
        conn.execute("UPDATE user_notifications SET routing_source = 'LEGACY' WHERE routing_source IS NULL")
    from portfolio_agent.tools import notification_routing_db
    notification_routing_db.migrate_legacy_rows(conn)           # idempotent: only rows still marked LEGACY


def _seed(conn: sqlite3.Connection) -> None:
    now = _now()
    conn.execute(
        "INSERT OR IGNORE INTO user_notifications (id, owner, created_at, updated_at) VALUES (1, ?, ?, ?)",
        (LOCAL_OWNER, now, now),
    )


def _seed_owner(conn: sqlite3.Connection, owner: str) -> None:
    """Auto-provision a default row for *owner* if it doesn't already have
    one — same idiom as user_profile_db._seed_owner."""
    now = _now()
    conn.execute(
        "INSERT OR IGNORE INTO user_notifications (owner, created_at, updated_at) VALUES (?, ?, ?)",
        (owner, now, now),
    )


def get_user_notifications() -> UserNotificationPrefs:
    """Return the notification preferences. Always returns a row. Unscoped — see
    get_user_notifications_for for the authorized, owner-checked path."""
    with _db() as conn:
        row = conn.execute("SELECT * FROM user_notifications WHERE id = 1").fetchone()
    return UserNotificationPrefs.from_db_row(dict(row))


def get_user_notifications_by_owner(owner: str) -> UserNotificationPrefs:
    """Unauthenticated-by-design lookup by a raw owner string, mirroring
    user_profile_db.get_user_profile_by_owner(). Auto-provisions a default
    row for *owner* on first access."""
    if not owner:
        raise ValueError("user_notifications: owner is required")
    with _db() as conn:
        _seed_owner(conn, owner)
        conn.commit()
        row = conn.execute("SELECT * FROM user_notifications WHERE owner = ?", (owner,)).fetchone()
    return UserNotificationPrefs.from_db_row(dict(row))


def get_user_notifications_for(ctx) -> UserNotificationPrefs:
    """Authorized read: the notification prefs owned by ctx's verified
    identity, auto-provisioned on first access."""
    from portfolio_agent.services.context import require_context

    require_context(ctx)
    return get_user_notifications_by_owner(ctx.actor)


def _parse_hhmm(raw: Optional[str]) -> Optional[time]:
    if not raw:
        return None
    try:
        hh, mm = str(raw).strip().split(":")[:2]
        return time(int(hh), int(mm))
    except (ValueError, AttributeError):
        return None


def _validate_and_normalize(fields: dict) -> dict:
    unknown = set(fields) - _UPDATABLE_FIELDS
    if unknown:
        raise ValueError(
            f"Unknown user_notifications field(s): {', '.join(sorted(unknown))}. "
            f"Allowed: {', '.join(sorted(_UPDATABLE_FIELDS))}"
        )
    if "channel" in fields and fields["channel"] not in NOTIFICATION_CHANNELS:
        raise ValueError(
            f"Unknown channel {fields['channel']!r}. Allowed: {', '.join(NOTIFICATION_CHANNELS)}"
        )
    for key in ("quiet_hours_start", "quiet_hours_end"):
        if key in fields and fields[key] not in (None, "") and _parse_hhmm(fields[key]) is None:
            raise ValueError(f"{key} must be 'HH:MM', got {fields[key]!r}")
    if "content_level" in fields and fields["content_level"] not in CONTENT_LEVELS:
        raise ValueError(f"content_level must be one of {', '.join(CONTENT_LEVELS)}, got {fields['content_level']!r}")
    if "quiet_hours_mode" in fields and fields["quiet_hours_mode"] not in QUIET_MODES:
        raise ValueError(f"quiet_hours_mode must be one of {', '.join(QUIET_MODES)}, got {fields['quiet_hours_mode']!r}")
    if "digest_time" in fields and _parse_hhmm(fields["digest_time"]) is None:
        raise ValueError(f"digest_time must be 'HH:MM', got {fields['digest_time']!r}")

    values = dict(fields)
    if "digest_time" in values:
        t = _parse_hhmm(values["digest_time"])
        values["digest_time"] = f"{t.hour:02d}:{t.minute:02d}"
    for key in ("quiet_hours_start", "quiet_hours_end"):
        if key in values and values[key] == "":
            values[key] = None
    values["updated_at"] = _now()
    return values


def update_user_notifications(**fields) -> UserNotificationPrefs:
    """
    Update one or more preference fields and return the refreshed row.

    Raises ValueError for unknown field names, an unknown channel, or a
    quiet-hours value that is not "HH:MM". updated_at is bumped automatically.
    Unscoped: always LOCAL_OWNER's row — see update_user_notifications_for
    for the authorized path.
    """
    if not fields:
        return get_user_notifications()
    values = _validate_and_normalize(fields)
    assignments = ", ".join(f"{col} = ?" for col in values)
    with _db() as conn:
        conn.execute(
            f"UPDATE user_notifications SET {assignments} WHERE id = 1",
            list(values.values()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM user_notifications WHERE id = 1").fetchone()
    return UserNotificationPrefs.from_db_row(dict(row))


def update_user_notifications_for(ctx, *, conn: Optional[sqlite3.Connection] = None, **fields) -> UserNotificationPrefs:
    """Authorized write: only updates the row owned by ctx's verified
    identity (auto-provisioned by get_user_notifications_for if this is that
    identity's first write). Same validation as update_user_notifications.

    conn: join the caller's transaction (nothing is committed here), so a preference change and its audit event land
    together — the caller must have called get_user_notifications_for(ctx) first (it provisions the row and runs the
    schema setup, which cannot run inside someone else's write transaction)."""
    get_user_notifications_for(ctx)   # authorization check + auto-provision before any write
    if not fields:
        return get_user_notifications_for(ctx)
    values = _validate_and_normalize(fields)
    assignments = ", ".join(f"{col} = ?" for col in values)
    sql, params = f"UPDATE user_notifications SET {assignments} WHERE owner = ?", [*values.values(), ctx.actor]
    if conn is not None:
        conn.execute(sql, params)
        row = conn.execute("SELECT * FROM user_notifications WHERE owner = ?", (ctx.actor,)).fetchone()
        return UserNotificationPrefs.from_db_row(dict(row))
    with _db() as c:
        c.execute(sql, params)
        c.commit()
        row = c.execute("SELECT * FROM user_notifications WHERE owner = ?", (ctx.actor,)).fetchone()
    return UserNotificationPrefs.from_db_row(dict(row))


# ── Decision helper ───────────────────────────────────────────────────────────

def in_quiet_hours(now: time, start: Optional[str], end: Optional[str]) -> bool:
    """
    True when *now* (a wall-clock time) falls inside [start, end). Either bound
    missing or unparseable → no quiet hours. start > end spans midnight;
    start == end is treated as disabled rather than "all day".
    """
    s, e = _parse_hhmm(start), _parse_hhmm(end)
    if s is None or e is None or s == e:
        return False
    if s < e:
        return s <= now < e
    return now >= s or now < e  # overnight window


@dataclass(frozen=True)
class FilterDecision:
    """The outcome of a person's PERSONAL filters for one notification (routing is decided separately)."""
    ok: bool
    reason: Optional[str] = None                 # BELOW_SEVERITY | BELOW_CONVICTION | QUIET_HOURS
    defer_until: Optional[datetime] = None       # timezone-aware; only for QUIET_HOURS: when quiet hours end


def next_quiet_end(now_local: datetime, start: Optional[str], end: Optional[str]) -> Optional[datetime]:
    """When the quiet-hours window that contains *now_local* ends, as a timezone-aware datetime in the same zone; None when
    now is not inside a window. (An overnight window 22:00-07:00 entered at 23:00 ends tomorrow 07:00; at 03:00, today 07:00.)"""
    if not in_quiet_hours(now_local.time(), start, end):
        return None
    s, e = _parse_hhmm(start), _parse_hhmm(end)
    end_today = now_local.replace(hour=e.hour, minute=e.minute, second=0, microsecond=0)
    if s < e or now_local.time() < e:
        return end_today
    return end_today + timedelta(days=1)


def evaluate_personal_filters(prefs: UserNotificationPrefs, *, severity: Optional[int], conviction: Optional[float] = None,
                              now_local: datetime) -> FilterDecision:
    """
    The person's own filters, WITHOUT the deprecated single-channel gate (routing replaces it): minimum severity (skipped
    when the notification has no severity), minimum conviction (only when both a threshold and a conviction exist), then
    quiet hours (evaluated in the RECIPIENT's local time, passed in as now_local). Never raises.
    """
    if severity is not None and int(severity) < int(prefs.min_severity_threshold):
        return FilterDecision(False, "BELOW_SEVERITY")
    if (prefs.min_conviction_threshold is not None and conviction is not None
            and float(conviction) < float(prefs.min_conviction_threshold)):
        return FilterDecision(False, "BELOW_CONVICTION")
    until = next_quiet_end(now_local, prefs.quiet_hours_start, prefs.quiet_hours_end)
    if until is not None:
        return FilterDecision(False, "QUIET_HOURS", until)
    return FilterDecision(True)


def recipient_now(owner: str) -> datetime:
    """The current time in this person's profile timezone (the legacy should_notify used the LOCAL owner's for everyone)."""
    tz = None
    try:
        from portfolio_agent.tools.user_profile_db import get_user_profile_by_owner
        tz = get_user_profile_by_owner(owner).timezone
    except Exception:
        pass
    return _local_now(tz)


def _local_now(tz_name: Optional[str]) -> datetime:
    try:
        return datetime.now(ZoneInfo(tz_name or "America/Chicago"))
    except Exception:
        return datetime.now()


def should_notify(
    severity: Optional[int],
    conviction: Optional[float] = None,
    now: Optional[datetime] = None,
    prefs: Optional[UserNotificationPrefs] = None,
) -> tuple[bool, str]:
    """
    Apply the personal notification filter to one event.

    Returns (True, channel) when a notification should fire, otherwise
    (False, reason). This is a *second* filter on top of the pipeline-wide
    intraday_severity_threshold in config.py — callers reach here only for
    events that already cleared that floor.

    - channel 'none'                        → never notify
    - severity below min_severity_threshold → no
    - min_conviction_threshold set AND a conviction is known AND it is below → no
      (when conviction is unknown, e.g. at detection time, the check is skipped)
    - current time inside quiet hours       → no
    """
    try:
        prefs = prefs or get_user_notifications()
    except Exception as exc:  # fail closed — never let a prefs read break the pipeline
        return False, f"notification prefs unavailable: {exc}"

    if not prefs.enabled:
        return False, "channel is 'none'"

    if severity is None or int(severity) < int(prefs.min_severity_threshold):
        return False, (
            f"severity {severity} below personal threshold {prefs.min_severity_threshold}"
        )

    if (
        prefs.min_conviction_threshold is not None
        and conviction is not None
        and float(conviction) < float(prefs.min_conviction_threshold)
    ):
        return False, (
            f"conviction {conviction} below personal threshold {prefs.min_conviction_threshold}"
        )

    if now is None:
        tz_name = None
        try:
            from portfolio_agent.tools.user_profile_db import get_user_profile
            tz_name = get_user_profile().timezone
        except Exception:
            pass
        now = _local_now(tz_name)
    if in_quiet_hours(now.time(), prefs.quiet_hours_start, prefs.quiet_hours_end):
        return False, (
            f"inside quiet hours {prefs.quiet_hours_start}-{prefs.quiet_hours_end} "
            f"(now {now.strftime('%H:%M')})"
        )

    return True, prefs.channel
