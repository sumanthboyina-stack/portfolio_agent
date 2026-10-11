"""
Notification contacts — PRIVATE, owner-scoped. Where a person can be reached, per channel.

  EMAIL  kind LOGIN      the address they sign in with (auth_users), created automatically and VERIFIED from the start:
                         the identity provider already verified it.
  EMAIL  kind ALTERNATE  at most ONE extra address; starts unverified and becomes verified only through the code flow.
  PUSH   kind TOPIC      an ntfy topic generated for the person: "apex-" + 32 random hex characters. The topic IS the
                         credential on a public ntfy server (anyone who knows it can read the pushes), so it is long,
                         unguessable, shown only to its owner and never logged. Unverified until the person confirms a
                         test push arrived.

WHATSAPP, SMS and SLACK contacts are refused ("not available yet"): there is no sender for them.

Addresses are personal data. Nothing here logs one raw: log lines and audit events carry address_tag() (a short
non-reversible tag, the same idea as auth_users_db._hash_email); an admin view may show display_mask().

A contact only ever receives anything when it is verified AND not opted out (and the channel is on globally and routed —
see services.notification_service.explain_channels). This module is the data layer: the audit events for add / remove /
opt-out / opt-in live in the service, the only place the application writes these rows.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from portfolio_agent.domain import ACTIVE_CHANNELS, Channel
from portfolio_agent.tools.db import db_conn, migrate_columns

KIND_LOGIN, KIND_ALTERNATE, KIND_TOPIC = "LOGIN", "ALTERNATE", "TOPIC"
TOPIC_PREFIX = "apex-"
_TOPIC_RE = re.compile(r"^apex-[0-9a-f]{32}$")
_EMAIL_RE = re.compile(r"^[^@\s<>,;:\"'\\]{1,64}@[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?\.[A-Za-z]{2,}$")


class ContactError(ValueError):
    """A contact could not be created or changed (the message is safe to show; it never contains an address)."""


class ChannelUnavailable(ContactError):
    """The channel has no sender yet (WHATSAPP, SMS, SLACK)."""


class ContactNotFound(ContactError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── Masking (never log or show an address raw) ────────────────────────────────

def address_tag(address: str) -> str:
    """A short, non-reversible tag for logs and audit events: 12 hex characters of SHA-256 of the lower-cased address."""
    return hashlib.sha256((address or "").strip().lower().encode()).hexdigest()[:12]


def display_mask(channel: str, address: str) -> str:
    """A human-readable partial mask for admin screens: s***@g***.com, or apex-…a1b2 for a push topic."""
    a = (address or "").strip()
    if str(channel) == Channel.PUSH.value:
        return f"{TOPIC_PREFIX}…{a[-4:]}" if len(a) > 8 else "…"
    if "@" not in a:
        return "***"
    local, domain = a.rsplit("@", 1)
    head, _, tld = domain.rpartition(".")
    return f"{local[:1]}***@{head[:1]}***.{tld}" if head else f"{local[:1]}***@***"


# ── Connection / schema ───────────────────────────────────────────────────────

_schema_ready: set[str] = set()


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notification_contacts (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            owner                    TEXT NOT NULL,
            channel                  TEXT NOT NULL,      -- validated in code (no CHECK): WHATSAPP / SMS arrive later
            kind                     TEXT NOT NULL,      -- LOGIN | ALTERNATE | TOPIC
            address                  TEXT NOT NULL,
            verified                 INTEGER NOT NULL DEFAULT 0,
            verified_at              TEXT,
            verification_code_hash   TEXT,
            verification_expires_at  TEXT,
            verification_attempts    INTEGER NOT NULL DEFAULT 0,
            last_code_sent_at        TEXT,
            opted_out                INTEGER NOT NULL DEFAULT 0,
            opted_out_at             TEXT,
            created_at               TEXT NOT NULL,
            updated_at               TEXT NOT NULL,
            UNIQUE (owner, channel, address)
        )""")
    migrate_columns(conn, "notification_contacts", [("last_code_sent_at", "TEXT")])      # tables created before verification existed
    # one login email, at most one alternate email, one push topic — per person
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_contacts_login ON notification_contacts(owner) WHERE kind = 'LOGIN'")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_contacts_alternate ON notification_contacts(owner) WHERE kind = 'ALTERNATE'")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_contacts_topic ON notification_contacts(owner) WHERE kind = 'TOPIC'")
    # a push topic must be unique across people: it is the only thing that addresses a phone
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_contacts_topic_address ON notification_contacts(address) WHERE channel = 'PUSH'")


def ensure_schema_once() -> None:
    from portfolio_agent.tools import db as _db_module
    key = str(_db_module.DB_PATH)
    if key not in _schema_ready:
        with db_conn(setup=lambda c: (_create_schema(c), c.commit())):
            pass
        _schema_ready.add(key)


@contextmanager
def _conn(conn: sqlite3.Connection | None):
    """The caller's connection (never committed here), or a fresh one committed on success."""
    ensure_schema_once()
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


def _owner(owner: str) -> str:
    if not owner:
        raise ContactError("notification_contacts: owner is required")
    return owner


# ── Validation ────────────────────────────────────────────────────────────────

def refuse_unavailable(channel: str) -> Channel:
    """The Channel for a known, DELIVERABLE channel; ChannelUnavailable for WHATSAPP / SMS / SLACK; ContactError if unknown."""
    try:
        ch = Channel(str(channel).upper())
    except ValueError:
        raise ContactError(f"unknown channel {channel!r}") from None
    if ch not in ACTIVE_CHANNELS:
        raise ChannelUnavailable(f"{ch.value} contacts are not available yet")
    return ch


def normalize_address(channel: Channel, address: str) -> str:
    a = (address or "").strip()
    if any(c in a for c in "\r\n\0"):
        raise ContactError("address must not contain line breaks")
    if channel == Channel.EMAIL:
        if not _EMAIL_RE.match(a):
            raise ContactError("that does not look like an email address")
        return a.lower()
    if not _TOPIC_RE.match(a):
        raise ContactError("not a valid push topic")
    return a


def new_topic() -> str:
    return TOPIC_PREFIX + secrets.token_hex(16)


# ── Reads ─────────────────────────────────────────────────────────────────────

def list_contacts(owner: str, *, conn: sqlite3.Connection | None = None) -> list[dict]:
    with _conn(conn) as c:
        rows = c.execute("SELECT * FROM notification_contacts WHERE owner = ? ORDER BY channel, kind", [_owner(owner)]).fetchall()
    return [dict(r) for r in rows]


def get_contact(owner: str, contact_id: int, *, conn: sqlite3.Connection | None = None) -> dict:
    """The owner's contact, or ContactNotFound — a contact id that belongs to someone else is indistinguishable from none."""
    with _conn(conn) as c:
        r = c.execute("SELECT * FROM notification_contacts WHERE id = ? AND owner = ?", [contact_id, _owner(owner)]).fetchone()
    if r is None:
        raise ContactNotFound("no such contact")
    return dict(r)


def find_contact(owner: str, channel: str, *, kind: str | None = None, conn: sqlite3.Connection | None = None) -> dict | None:
    sql, params = "SELECT * FROM notification_contacts WHERE owner = ? AND channel = ?", [_owner(owner), str(channel)]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    with _conn(conn) as c:
        r = c.execute(sql + " ORDER BY id LIMIT 1", params).fetchone()
    return dict(r) if r else None


# ── Writes (data layer; the service adds ownership checks' audit events) ──────

def _insert(c: sqlite3.Connection, owner: str, channel: Channel, kind: str, address: str, *, verified: bool) -> dict:
    now = _now()
    try:
        cur = c.execute(
            "INSERT INTO notification_contacts (owner, channel, kind, address, verified, verified_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [owner, channel.value, kind, address, int(verified), now if verified else None, now, now])
    except sqlite3.IntegrityError:
        raise ContactError("that contact already exists") from None
    return dict(c.execute("SELECT * FROM notification_contacts WHERE id = ?", [cur.lastrowid]).fetchone())


def ensure_login_email(owner: str, email: str | None, *, conn: sqlite3.Connection | None = None) -> dict | None:
    """The VERIFIED EMAIL contact for the sign-in address. Created if missing; if the sign-in address changed it follows
    (still verified — the identity provider verified it). None when the person has no sign-in email on record."""
    if not email:
        return None
    addr = normalize_address(Channel.EMAIL, email)
    with _conn(conn) as c:
        row = c.execute("SELECT * FROM notification_contacts WHERE owner = ? AND kind = 'LOGIN'", [_owner(owner)]).fetchone()
        if row is None:
            return _insert(c, owner, Channel.EMAIL, KIND_LOGIN, addr, verified=True)
        if row["address"] != addr:
            c.execute("UPDATE notification_contacts SET address = ?, verified = 1, verified_at = ?, updated_at = ? WHERE id = ?",
                      [addr, _now(), _now(), row["id"]])
        return dict(c.execute("SELECT * FROM notification_contacts WHERE id = ?", [row["id"]]).fetchone())


def add_alternate_email(owner: str, address: str, *, conn: sqlite3.Connection | None = None) -> dict:
    """The person's ONE alternate email, unverified until the code flow confirms it."""
    addr = normalize_address(refuse_unavailable("EMAIL"), address)
    with _conn(conn) as c:
        if c.execute("SELECT 1 FROM notification_contacts WHERE owner = ? AND kind = 'ALTERNATE'", [_owner(owner)]).fetchone():
            raise ContactError("you already have an alternate email; remove it first to add another")
        if c.execute("SELECT 1 FROM notification_contacts WHERE owner = ? AND kind = 'LOGIN' AND address = ?", [owner, addr]).fetchone():
            raise ContactError("that is already your sign-in email")
        return _insert(c, owner, Channel.EMAIL, KIND_ALTERNATE, addr, verified=False)


def ensure_push_contact(owner: str, *, conn: sqlite3.Connection | None = None) -> dict:
    """The person's push contact, generating its unguessable ntfy topic the first time."""
    with _conn(conn) as c:
        row = c.execute("SELECT * FROM notification_contacts WHERE owner = ? AND kind = 'TOPIC'", [_owner(owner)]).fetchone()
        if row is not None:
            return dict(row)
        for _ in range(5):                                       # a collision of 128 random bits is not a thing; retry anyway
            try:
                return _insert(c, owner, Channel.PUSH, KIND_TOPIC, new_topic(), verified=False)
            except ContactError:
                continue
    raise ContactError("could not generate a push topic")


def regenerate_push_topic(owner: str, *, conn: sqlite3.Connection | None = None) -> dict:
    """A fresh topic (the old one stops working — use this if a topic leaked). The new topic starts unverified."""
    with _conn(conn) as c:
        row = c.execute("SELECT * FROM notification_contacts WHERE owner = ? AND kind = 'TOPIC'", [_owner(owner)]).fetchone()
        if row is None:
            return ensure_push_contact(owner, conn=c)
        c.execute("UPDATE notification_contacts SET address = ?, verified = 0, verified_at = NULL, verification_code_hash = NULL, "
                  "verification_expires_at = NULL, verification_attempts = 0, updated_at = ? WHERE id = ?",
                  [new_topic(), _now(), row["id"]])
        return dict(c.execute("SELECT * FROM notification_contacts WHERE id = ?", [row["id"]]).fetchone())


def set_verified(owner: str, contact_id: int, verified: bool, *, conn: sqlite3.Connection | None = None) -> dict:
    """Data-layer switch used by the verification flows (code for an alternate email, test-push confirmation)."""
    with _conn(conn) as c:
        get_contact(owner, contact_id, conn=c)
        c.execute("UPDATE notification_contacts SET verified = ?, verified_at = ?, verification_code_hash = NULL, "
                  "verification_expires_at = NULL, verification_attempts = 0, updated_at = ? WHERE id = ? AND owner = ?",
                  [int(verified), _now() if verified else None, _now(), contact_id, owner])
        return get_contact(owner, contact_id, conn=c)


def store_verification_code(owner: str, contact_id: int, code_hash: str, expires_at: str, sent_at: str, *,
                            conn: sqlite3.Connection | None = None) -> dict:
    """Remember ONLY the hash of a freshly sent code, when it expires and when it was sent (for the resend cooldown); a new
    code resets the attempt counter."""
    with _conn(conn) as c:
        get_contact(owner, contact_id, conn=c)
        c.execute("UPDATE notification_contacts SET verification_code_hash = ?, verification_expires_at = ?, last_code_sent_at = ?, "
                  "verification_attempts = 0, updated_at = ? WHERE id = ? AND owner = ?",
                  [code_hash, expires_at, sent_at, _now(), contact_id, owner])
        return get_contact(owner, contact_id, conn=c)


def bump_verification_attempts(owner: str, contact_id: int, *, conn: sqlite3.Connection | None = None) -> int:
    """Count one wrong-or-right guess and return the new total (the caller refuses once it exceeds the allowed attempts)."""
    with _conn(conn) as c:
        get_contact(owner, contact_id, conn=c)
        c.execute("UPDATE notification_contacts SET verification_attempts = verification_attempts + 1, updated_at = ? "
                  "WHERE id = ? AND owner = ?", [_now(), contact_id, owner])
        return int(c.execute("SELECT verification_attempts FROM notification_contacts WHERE id = ?", [contact_id]).fetchone()[0])


def clear_verification_code(owner: str, contact_id: int, *, conn: sqlite3.Connection | None = None) -> None:
    with _conn(conn) as c:
        c.execute("UPDATE notification_contacts SET verification_code_hash = NULL, verification_expires_at = NULL, updated_at = ? "
                  "WHERE id = ? AND owner = ?", [_now(), contact_id, owner])


def set_opted_out(owner: str, contact_id: int, opted_out: bool, *, conn: sqlite3.Connection | None = None) -> dict:
    with _conn(conn) as c:
        get_contact(owner, contact_id, conn=c)
        c.execute("UPDATE notification_contacts SET opted_out = ?, opted_out_at = ?, updated_at = ? WHERE id = ? AND owner = ?",
                  [int(opted_out), _now() if opted_out else None, _now(), contact_id, owner])
        return get_contact(owner, contact_id, conn=c)


def delete_contact(owner: str, contact_id: int, *, conn: sqlite3.Connection | None = None) -> dict:
    """Remove a contact (returns the row that was removed). The sign-in email cannot be removed: it is who they are."""
    with _conn(conn) as c:
        row = get_contact(owner, contact_id, conn=c)
        if row["kind"] == KIND_LOGIN:
            raise ContactError("your sign-in email cannot be removed; opt out of it instead")
        c.execute("DELETE FROM notification_contacts WHERE id = ? AND owner = ?", [contact_id, owner])
    return row
