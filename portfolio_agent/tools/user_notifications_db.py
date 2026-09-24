"""
User notification preferences — single-row store for the personal filter that
sits on top of the pipeline-wide event trigger.

Table: user_notifications — exactly one row (id = 1, enforced by CHECK), seeded
with defaults on first access and never overwritten after that.

Columns:
  channel                  'none' | 'email' | 'slack'   (default 'none' = no notifications)
  min_severity_threshold   INTEGER, default 3 — mirrors config._DEFAULT_EVENT_DRIVEN
                           ["intraday_severity_threshold"]; the pipeline floor
                           still applies first, this is a second personal filter
  min_conviction_threshold INTEGER, nullable — only applied when a conviction is known
  quiet_hours_start/end    "HH:MM" in the profile's timezone, nullable;
                           start > end spans midnight (e.g. 22:00 → 07:00)
  created_at, updated_at

should_notify(...) is the single decision function the event detector and
scheduler call. It never raises on bad data — it fails closed (no notification)
with a reason string.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, time, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from portfolio_agent.domain import LOCAL_OWNER, NOTIFICATION_CHANNELS, UserNotificationPrefs
from portfolio_agent.tools.db import db_conn, migrate_columns

_UPDATABLE_FIELDS = frozenset({
    "channel",
    "min_severity_threshold",
    "min_conviction_threshold",
    "quiet_hours_start",
    "quiet_hours_end",
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
            id                       INTEGER PRIMARY KEY CHECK (id = 1),
            channel                  TEXT NOT NULL DEFAULT 'none',
            min_severity_threshold   INTEGER NOT NULL DEFAULT 3,
            min_conviction_threshold INTEGER,
            quiet_hours_start        TEXT,
            quiet_hours_end          TEXT,
            created_at               TEXT NOT NULL,
            updated_at               TEXT NOT NULL,
            owner                    TEXT
        )
    """)


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the first release, then backfill ownership
    on any pre-existing row to LOCAL_OWNER (idempotent — a no-op once set)."""
    migrate_columns(conn, "user_notifications", [
        ("min_conviction_threshold", "INTEGER"),
        ("quiet_hours_start",        "TEXT"),
        ("quiet_hours_end",          "TEXT"),
        ("owner",                    "TEXT"),
    ])
    conn.execute("UPDATE user_notifications SET owner = ? WHERE owner IS NULL", (LOCAL_OWNER,))


def _seed(conn: sqlite3.Connection) -> None:
    now = _now()
    conn.execute(
        "INSERT OR IGNORE INTO user_notifications (id, owner, created_at, updated_at) VALUES (1, ?, ?, ?)",
        (LOCAL_OWNER, now, now),
    )


def get_user_notifications() -> UserNotificationPrefs:
    """Return the notification preferences. Always returns a row. Unscoped — see
    get_user_notifications_for for the authorized, owner-checked path."""
    with _db() as conn:
        row = conn.execute("SELECT * FROM user_notifications WHERE id = 1").fetchone()
    return UserNotificationPrefs.from_db_row(dict(row))


def get_user_notifications_for(ctx) -> UserNotificationPrefs:
    """Authorized read: the notification prefs owned by ctx's verified identity."""
    from portfolio_agent.services.account_service import NotAuthorized

    with _db() as conn:
        row = conn.execute("SELECT * FROM user_notifications WHERE owner = ?", (ctx.actor,)).fetchone()
    if row is None:
        raise NotAuthorized(f"{ctx.actor!r} has no notification preferences here")
    return UserNotificationPrefs.from_db_row(dict(row))


def _parse_hhmm(raw: Optional[str]) -> Optional[time]:
    if not raw:
        return None
    try:
        hh, mm = str(raw).strip().split(":")[:2]
        return time(int(hh), int(mm))
    except (ValueError, AttributeError):
        return None


def update_user_notifications(**fields) -> UserNotificationPrefs:
    """
    Update one or more preference fields and return the refreshed row.

    Raises ValueError for unknown field names, an unknown channel, or a
    quiet-hours value that is not "HH:MM". updated_at is bumped automatically.
    """
    unknown = set(fields) - _UPDATABLE_FIELDS
    if unknown:
        raise ValueError(
            f"Unknown user_notifications field(s): {', '.join(sorted(unknown))}. "
            f"Allowed: {', '.join(sorted(_UPDATABLE_FIELDS))}"
        )
    if not fields:
        return get_user_notifications()

    if "channel" in fields and fields["channel"] not in NOTIFICATION_CHANNELS:
        raise ValueError(
            f"Unknown channel {fields['channel']!r}. Allowed: {', '.join(NOTIFICATION_CHANNELS)}"
        )
    for key in ("quiet_hours_start", "quiet_hours_end"):
        if key in fields and fields[key] not in (None, "") and _parse_hhmm(fields[key]) is None:
            raise ValueError(f"{key} must be 'HH:MM', got {fields[key]!r}")

    values = dict(fields)
    for key in ("quiet_hours_start", "quiet_hours_end"):
        if key in values and values[key] == "":
            values[key] = None
    values["updated_at"] = _now()

    assignments = ", ".join(f"{col} = ?" for col in values)
    with _db() as conn:
        conn.execute(
            f"UPDATE user_notifications SET {assignments} WHERE id = 1",
            list(values.values()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM user_notifications WHERE id = 1").fetchone()
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
