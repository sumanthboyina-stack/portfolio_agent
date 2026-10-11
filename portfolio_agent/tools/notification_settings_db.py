"""
Notification settings — global, admin-only, database-backed (the engine_settings_db pattern).

One row (id = 1) holds everything an administrator tunes about outbound notifications: which channels are switched on, the
SMTP server and sender identity, the ntfy server, rate and retry limits, verification-code rules, the public URL used in
links, and a dry-run switch. The constants below are SEED DEFAULTS only — they create or reset the row; what the application
uses at runtime is read from the table. The row is versioned (version, updated_at, updated_by), updates take an
expected_version, and reset_notification_settings_to_defaults() restores the seed. No file holds a setting.

SECRETS ARE NEVER HERE. The SMTP username / password, the ntfy access token and the unsubscribe-signing key come only from
environment variables; this table stores the env var NAME (e.g. "APEX_SMTP_PASSWORD"), never a value. env_presence() reports
whether each named variable is set — a yes/no, never the value — so the admin screen can show "present" or "missing".

WHATSAPP and SMS have no sender in this release, so their switches can never be turned on. EMAIL cannot be switched on
without an SMTP host and a from-address (the seed row is "on" with an empty from-address: it simply cannot deliver until an
administrator sets one — effective_channel_enabled() is what routing asks).

This module is the data layer only (no RequestContext): the admin check and the audit event live in
services.notification_service, the only place the application writes this table.
"""
from __future__ import annotations

import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from portfolio_agent.domain import Channel
from portfolio_agent.tools.db import db_conn
from portfolio_agent.tools.investor_settings_db import SettingsVersionConflict

# ── Seed defaults (code constants: used ONLY to create / reset the row) ───────

SEED_NOTIFICATION_SETTINGS: dict = {
    "email_enabled": 1, "push_enabled": 1, "whatsapp_enabled": 0, "sms_enabled": 0,
    "smtp_host": "smtp.gmail.com", "smtp_port": 587, "smtp_use_tls": 1,
    "smtp_username_env": "APEX_SMTP_USERNAME", "smtp_password_env": "APEX_SMTP_PASSWORD",
    "email_from_address": "", "email_from_name": "APEX",
    "ntfy_base_url": "https://ntfy.sh", "ntfy_token_env": "APEX_NTFY_TOKEN",
    "max_messages_per_user_per_hour": 10, "retry_max_attempts": 5, "retry_backoff_seconds": 60,
    "verification_code_ttl_minutes": 10, "verification_max_attempts": 5,
    "app_base_url": "", "dry_run": 0,
    "unsubscribe_signing_key_env": "APEX_NOTIFY_SIGNING_KEY",
}

_BOOLS = ("email_enabled", "push_enabled", "whatsapp_enabled", "sms_enabled", "smtp_use_tls", "dry_run")
_INTS = {                                   # name -> (min, max)
    "smtp_port": (1, 65535), "max_messages_per_user_per_hour": (1, 1000), "retry_max_attempts": (1, 20),
    "retry_backoff_seconds": (1, 86400), "verification_code_ttl_minutes": (1, 1440), "verification_max_attempts": (1, 20),
}
_ENV_NAMES = ("smtp_username_env", "smtp_password_env", "ntfy_token_env", "unsubscribe_signing_key_env")
_NOT_IMPLEMENTED = {"whatsapp_enabled": Channel.WHATSAPP, "sms_enabled": Channel.SMS}
_UPDATABLE = frozenset(SEED_NOTIFICATION_SETTINGS)

_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_EMAIL_RE = re.compile(r"^[^@\s<>,;:\"'\\]{1,64}@[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?\.[A-Za-z]{2,}$")
_URL_RE = re.compile(r"^https?://[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(:\d{1,5})?(/[^\s?#@]*)?$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Connection / schema / seed ────────────────────────────────────────────────

_schema_ready: set[str] = set()


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notification_settings (
            id                              INTEGER PRIMARY KEY CHECK (id = 1),
            email_enabled                   INTEGER NOT NULL DEFAULT 1,
            push_enabled                    INTEGER NOT NULL DEFAULT 1,
            whatsapp_enabled                INTEGER NOT NULL DEFAULT 0,
            sms_enabled                     INTEGER NOT NULL DEFAULT 0,
            smtp_host                       TEXT NOT NULL DEFAULT 'smtp.gmail.com',
            smtp_port                       INTEGER NOT NULL DEFAULT 587,
            smtp_use_tls                    INTEGER NOT NULL DEFAULT 1,
            smtp_username_env               TEXT NOT NULL DEFAULT 'APEX_SMTP_USERNAME',
            smtp_password_env               TEXT NOT NULL DEFAULT 'APEX_SMTP_PASSWORD',
            email_from_address              TEXT NOT NULL DEFAULT '',
            email_from_name                 TEXT NOT NULL DEFAULT 'APEX',
            ntfy_base_url                   TEXT NOT NULL DEFAULT 'https://ntfy.sh',
            ntfy_token_env                  TEXT NOT NULL DEFAULT 'APEX_NTFY_TOKEN',
            max_messages_per_user_per_hour  INTEGER NOT NULL DEFAULT 10,
            retry_max_attempts              INTEGER NOT NULL DEFAULT 5,
            retry_backoff_seconds           INTEGER NOT NULL DEFAULT 60,
            verification_code_ttl_minutes   INTEGER NOT NULL DEFAULT 10,
            verification_max_attempts       INTEGER NOT NULL DEFAULT 5,
            app_base_url                    TEXT NOT NULL DEFAULT '',
            dry_run                         INTEGER NOT NULL DEFAULT 0,
            unsubscribe_signing_key_env     TEXT NOT NULL DEFAULT 'APEX_NOTIFY_SIGNING_KEY',
            version                         INTEGER NOT NULL DEFAULT 1,
            created_at                      TEXT NOT NULL,
            updated_at                      TEXT NOT NULL,
            updated_by                      TEXT
        )""")


def _seed(conn: sqlite3.Connection) -> None:
    """Insert the row if it is missing. NEVER overwrites an existing row."""
    now = _now()
    cols = list(SEED_NOTIFICATION_SETTINGS)
    conn.execute(
        f"INSERT OR IGNORE INTO notification_settings (id, {', '.join(cols)}, created_at, updated_at) "
        f"VALUES (1, {', '.join('?' * len(cols))}, ?, ?)",
        [*SEED_NOTIFICATION_SETTINGS.values(), now, now])


def _ensure_once() -> None:
    """Create + seed once per process per database file (this is read on every notification decision)."""
    from portfolio_agent.tools import db as _db_module
    key = str(_db_module.DB_PATH)
    if key not in _schema_ready:
        def _setup(conn: sqlite3.Connection) -> None:
            _create_schema(conn)
            _seed(conn)
            conn.commit()
        with db_conn(setup=_setup):
            pass
        _schema_ready.add(key)


@contextmanager
def _db(conn: sqlite3.Connection | None = None):
    """The caller's connection (never committed here: the caller owns the transaction, e.g. a service writing a setting
    and its audit event together), or a fresh one."""
    _ensure_once()
    if conn is not None:
        yield conn
        return
    with db_conn() as c:
        yield c


# ── Reads ─────────────────────────────────────────────────────────────────────

def get_notification_settings(*, conn: sqlite3.Connection | None = None) -> dict:
    with _db(conn) as cx:
        return dict(cx.execute("SELECT * FROM notification_settings WHERE id = 1").fetchone())


def effective_channel_enabled(settings: dict, channel: str) -> bool:
    """Can this channel deliver right now, globally? Only EMAIL and PUSH ever can: EMAIL needs its switch AND an SMTP host
    AND a from-address; PUSH needs its switch AND an ntfy server. WHATSAPP / SMS / SLACK are never enabled."""
    ch = str(channel)
    if ch == Channel.EMAIL.value:
        return bool(settings.get("email_enabled") and settings.get("smtp_host") and settings.get("email_from_address"))
    if ch == Channel.PUSH.value:
        return bool(settings.get("push_enabled") and settings.get("ntfy_base_url"))
    return False


def env_presence(settings: dict | None = None) -> list[dict]:
    """
    For each secret the senders read from the environment: which variable, and whether it is SET — nothing else. The value
    is never read into the result (only its truthiness), logged or returned, so this is safe to show an administrator.
    """
    s = settings or get_notification_settings()
    email_on = bool(s.get("email_enabled"))
    rows = [("smtp_username_env", "SMTP username", email_on), ("smtp_password_env", "SMTP password", email_on),
            ("ntfy_token_env", "ntfy access token (optional)", False),
            ("unsubscribe_signing_key_env", "Unsubscribe-link signing key", email_on)]
    return [{"setting": key, "label": label, "env_var": s[key], "present": bool(os.environ.get(s[key])), "required": req}
            for key, label, req in rows]


# ── Validation ────────────────────────────────────────────────────────────────

def _no_ctl(value: str, what: str) -> str:
    if any(ch in value for ch in "\r\n\0"):
        raise ValueError(f"{what}: must not contain line breaks")
    return value


def _clean(fields: dict, current: dict) -> dict:
    unknown = set(fields) - _UPDATABLE
    if unknown:
        raise ValueError(f"unknown notification setting(s): {', '.join(sorted(unknown))}")
    out: dict = {}
    for name, raw in fields.items():
        if raw is None:
            raise ValueError(f"{name}: a value is required")
        if name in _BOOLS:
            if isinstance(raw, str) or raw not in (0, 1, True, False):
                raise ValueError(f"{name}: must be on or off")
            out[name] = 1 if raw else 0
        elif name in _INTS:
            lo, hi = _INTS[name]
            if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw != int(raw) or not lo <= int(raw) <= hi:
                raise ValueError(f"{name}: must be a whole number from {lo} to {hi}")
            out[name] = int(raw)
        else:
            v = _no_ctl(str(raw).strip(), name)
            if name in _ENV_NAMES:
                if not _ENV_RE.match(v):
                    raise ValueError(f"{name}: must be an environment variable NAME (letters, digits, underscore)")
            elif name == "smtp_host":
                if v and not _HOST_RE.match(v):
                    raise ValueError("smtp_host: not a valid host name")
            elif name == "email_from_address":
                if v and not _EMAIL_RE.match(v):
                    raise ValueError("email_from_address: not a valid email address")
            elif name == "email_from_name":
                if len(v) > 80:
                    raise ValueError("email_from_name: at most 80 characters")
            elif name in ("ntfy_base_url", "app_base_url"):
                v = v.rstrip("/")
                if name == "ntfy_base_url" and not v:
                    raise ValueError("ntfy_base_url: required")
                if v and not _URL_RE.match(v):
                    raise ValueError(f"{name}: must be an http(s) URL with no credentials, query or fragment")
            out[name] = v
    for name, channel in _NOT_IMPLEMENTED.items():
        if out.get(name):
            raise ValueError(f"{name}: the {channel.value} sender is not implemented yet, so it cannot be turned on")
    merged = {**current, **out}
    if merged["email_enabled"] and ({"email_enabled", "smtp_host", "email_from_address"} & set(out)):
        if not merged["smtp_host"] or not merged["email_from_address"]:
            raise ValueError("email cannot be enabled without an smtp_host and an email_from_address")
    return out


# ── Writes ────────────────────────────────────────────────────────────────────

def update_notification_settings(*, updated_by: str, expected_version: int | None = None,
                                 conn: sqlite3.Connection | None = None, **fields) -> dict:
    """Update any of the settings above (each validated). Raises ValueError on bad input and SettingsVersionConflict when
    expected_version is stale. Joins the caller's transaction when conn is given."""
    with _db(conn) as cx:
        current = get_notification_settings(conn=cx)
        values = _clean(fields, current)
        if not values:
            return current
        return _write(cx, values, updated_by, expected_version, commit=conn is None)


def reset_notification_settings_to_defaults(*, updated_by: str, conn: sqlite3.Connection | None = None) -> dict:
    with _db(conn) as cx:
        return _write(cx, dict(SEED_NOTIFICATION_SETTINGS), updated_by, None, commit=conn is None)


def _write(cx: sqlite3.Connection, values: dict, updated_by: str, expected_version: int | None, *, commit: bool) -> dict:
    values = {**values, "updated_at": _now(), "updated_by": updated_by}
    sql = f"UPDATE notification_settings SET {', '.join(f'{c} = ?' for c in values)}, version = version + 1 WHERE id = 1"
    params = list(values.values())
    if expected_version is not None:
        sql += " AND version = ?"
        params.append(expected_version)
    n = cx.execute(sql, params).rowcount
    if commit:
        cx.commit()
    if n == 0:
        raise SettingsVersionConflict("notification settings changed since you loaded them; reload and retry")
    return get_notification_settings(conn=cx)
