"""
Notification service — the single writer of a person's notification contacts, routing and preferences, and of the global
notification settings (admin-only).

Every operation
  * takes a RequestContext and acts only on the actor's OWN rows (the owner is ctx.actor — never an id a page supplied; a
    contact id that belongs to someone else behaves exactly like one that does not exist);
  * runs in ONE transaction with its audit event (actor, operation, before/after, request id), so a change and its audit
    record land together or not at all;
  * never puts a raw address, push topic or secret into an audit event, a log line or an error message: addresses appear
    only as a short non-reversible tag (notification_contacts_db.address_tag);
  * for the global settings, first requires an ENABLED admin — checked here, server-side, not by the UI.

  notify(owner, category, ...)   the ONE entry point producers use: applies the person's filters, fans out to every deliverable
                                 channel and ENQUEUES (a database write — never a network call, never an exception to the caller)

Operations
  Private (the actor's own):
    ensure_contacts / get_notification_state     the sign-in email (verified automatically) + current contacts, routing,
                                                 preferences and why each channel would or would not deliver
    add_contact / add_alternate_email / remove_contact   ONE alternate email, unverified until the code flow (a later step) verifies it
    opt_out / opt_in                             stop / resume a contact
    setup_push / regenerate_push_topic           the person's ntfy topic (unguessable; regenerate if it leaked)
    set_routing                                  one alert category x channel on or off (only EMAIL and PUSH exist yet)
    update_preferences                           content level, quiet-hours mode, digest time, quiet hours, thresholds
  Admin-only (global):
    get_settings_admin / update_settings / reset_settings

  Read helpers for the delivery code (no context: the caller already trusts the owner):
    explain_channels(owner, category)            for each channel, DELIVERABLE or the first reason it is not
    deliverable_contacts(owner, category)        the contacts a message in that category may actually be sent to

A channel delivers only if ALL hold: it is an ACTIVE channel (EMAIL, PUSH), it is switched on globally (with an SMTP host and
from-address for email), routing is enabled for the category, the person has a contact on it, that contact is verified, and
it is not opted out. Nothing in this module sends anything.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from portfolio_agent.domain import ACTIVE_CHANNELS, ALERT_CATEGORIES, CHANNELS, AlertCategory, Channel, ContentLevel, QuietMode
from portfolio_agent.log import get_logger as _get_logger
from portfolio_agent.services.context import RequestContext, require_context
from portfolio_agent.services import notification_senders as senders
from portfolio_agent.services.investor_service import require_admin
from portfolio_agent.tools import auth_users_db
from portfolio_agent.tools import holdings_db as repo
from portfolio_agent.tools import notification_contacts_db as cdb
from portfolio_agent.tools import notification_outbox_db as obx
from portfolio_agent.tools import notification_routing_db as rdb
from portfolio_agent.tools import notification_settings_db as sdb
from portfolio_agent.tools import user_notifications_db as udb

_log = _get_logger("services.notification")

# explain_channels() reasons, in the order they are checked
DELIVERABLE = "DELIVERABLE"
NOT_ACTIVE = "NOT_ACTIVE"                   # WHATSAPP / SMS / SLACK: no sender yet
GLOBALLY_DISABLED = "GLOBALLY_DISABLED"     # switched off by an administrator (or email without a host / from-address)
NOT_ROUTED = "NOT_ROUTED"                   # the person has not routed this category to this channel
NO_CONTACT = "NO_CONTACT"
UNVERIFIED = "UNVERIFIED"
OPTED_OUT = "OPTED_OUT"


# ── Transactions, audit ───────────────────────────────────────────────────────

def _prepare(owner: str | None = None) -> None:
    """Touch every store once BEFORE a write transaction opens: creating a table from a second connection while this one
    holds the write lock would fail with 'database is locked'."""
    sdb._ensure_once()
    cdb.ensure_schema_once()
    rdb.ensure_schema_once()
    obx.ensure_schema_once()
    if owner:
        udb.get_user_notifications_by_owner(owner)       # provisions the row and runs the one-time schema / routing migration


@contextmanager
def _uow(owner: str | None = None):
    _prepare(owner)
    with repo.unit_of_work() as c:
        yield c


def _audit(ctx: RequestContext, conn, operation: str, *, before=None, after=None, reason: str | None = None) -> None:
    portfolio = repo.get_portfolio_by_owner(ctx.actor, conn=conn)
    repo.record_audit_event(actor=ctx.actor, source=ctx.source, request_id=ctx.request_id, operation=operation,
                            portfolio_id=portfolio["portfolio_id"] if portfolio else None,
                            before=before, after=after, reason=reason, conn=conn)


def _safe(contact: dict | None) -> dict | None:
    """What an audit event or log may say about a contact: ids, channel, kind, state and a tag — never the address."""
    if not contact:
        return None
    return {"contact_id": contact["id"], "channel": contact["channel"], "kind": contact["kind"],
            "tag": cdb.address_tag(contact["address"]), "verified": bool(contact["verified"]), "opted_out": bool(contact["opted_out"])}


# ── Reads (private, owner-scoped) ─────────────────────────────────────────────

def ensure_contacts(ctx: RequestContext) -> list[dict]:
    """Create the person's sign-in EMAIL contact (verified: the identity provider verified it) if it is missing. Idempotent;
    audits only when something is created. Returns the person's contacts."""
    require_context(ctx)
    email = auth_users_db.login_email_for_owner(ctx.actor)
    with _uow(ctx.actor) as c:
        before = cdb.find_contact(ctx.actor, Channel.EMAIL.value, kind=cdb.KIND_LOGIN, conn=c)
        after = cdb.ensure_login_email(ctx.actor, email, conn=c)
        if after is not None and (before is None or before["address"] != after["address"]):
            _audit(ctx, c, "ensure_notification_contact", before=_safe(before), after=_safe(after), reason="sign-in email")
        return cdb.list_contacts(ctx.actor, conn=c)


def get_notification_state(ctx: RequestContext) -> dict:
    """Everything the Notifications screen needs about the actor: {contacts, routing, preferences, channels} where
    channels[category][channel] is DELIVERABLE or the reason it is not."""
    contacts = ensure_contacts(ctx)
    return {"contacts": contacts, "routing": rdb.get_routing(ctx.actor),
            "preferences": udb.get_user_notifications_for(ctx).to_dict(),
            "channels": {cat: explain_channels(ctx.actor, cat) for cat in ALERT_CATEGORIES}}


def explain_channels(owner: str, category: str, *, settings: dict | None = None) -> dict[str, str]:
    """For each channel: DELIVERABLE, or the FIRST reason it would not deliver a message in *category* to *owner*."""
    s = settings or sdb.get_notification_settings()
    routing = rdb.get_routing(owner)
    contacts = cdb.list_contacts(owner)
    out: dict[str, str] = {}
    for ch in CHANNELS:
        if Channel(ch) not in ACTIVE_CHANNELS:
            out[ch] = NOT_ACTIVE
        elif not sdb.effective_channel_enabled(s, ch):
            out[ch] = GLOBALLY_DISABLED
        elif not routing.get(str(category), {}).get(ch, False):
            out[ch] = NOT_ROUTED
        else:
            mine = [c for c in contacts if c["channel"] == ch]
            if not mine:
                out[ch] = NO_CONTACT
            elif not any(c["verified"] for c in mine):
                out[ch] = UNVERIFIED
            elif not any(c["verified"] and not c["opted_out"] for c in mine):
                out[ch] = OPTED_OUT
            else:
                out[ch] = DELIVERABLE
    return out


def deliverable_contacts(owner: str, category: str, *, settings: dict | None = None) -> list[dict]:
    """The contacts a message in *category* may be sent to: every verified, not-opted-out contact on every channel that
    explain_channels() reports DELIVERABLE."""
    ok = {ch for ch, why in explain_channels(owner, category, settings=settings).items() if why == DELIVERABLE}
    return [c for c in cdb.list_contacts(owner) if c["channel"] in ok and c["verified"] and not c["opted_out"]]


# ── Contact writes (private) ──────────────────────────────────────────────────

def add_alternate_email(ctx: RequestContext, address: str) -> dict:
    """Add the actor's ONE alternate email. It starts unverified, so it receives nothing until verified."""
    require_context(ctx)
    ensure_contacts(ctx)
    with _uow(ctx.actor) as c:
        row = cdb.add_alternate_email(ctx.actor, address, conn=c)
        _audit(ctx, c, "add_notification_contact", after=_safe(row))
    _log.info(f"  [notifications] alternate email added tag={cdb.address_tag(row['address'])}", event_type="notification_contact")
    return row


def add_contact(ctx: RequestContext, channel: str, address: str | None = None) -> dict:
    """One entry point for "add a way to reach me". EMAIL adds the alternate email; PUSH sets up the ntfy topic (no address
    is taken: it is generated). WHATSAPP, SMS and SLACK are refused with "not available yet" — there is no sender."""
    require_context(ctx)
    ch = cdb.refuse_unavailable(channel)
    if ch == Channel.PUSH:
        return setup_push(ctx)
    return add_alternate_email(ctx, address or "")


def remove_contact(ctx: RequestContext, contact_id: int) -> dict:
    """Remove one of the actor's contacts (not the sign-in email). Returns what was removed."""
    require_context(ctx)
    with _uow(ctx.actor) as c:
        row = cdb.delete_contact(ctx.actor, contact_id, conn=c)
        _audit(ctx, c, "delete_notification_contact", before=_safe(row))
    _log.info(f"  [notifications] contact removed tag={cdb.address_tag(row['address'])}", event_type="notification_contact")
    return row


def opt_out(ctx: RequestContext, contact_id: int) -> dict:
    return _set_opt(ctx, contact_id, True)


def opt_in(ctx: RequestContext, contact_id: int) -> dict:
    return _set_opt(ctx, contact_id, False)


def _set_opt(ctx: RequestContext, contact_id: int, opted_out: bool) -> dict:
    require_context(ctx)
    with _uow(ctx.actor) as c:
        before = cdb.get_contact(ctx.actor, contact_id, conn=c)
        after = cdb.set_opted_out(ctx.actor, contact_id, opted_out, conn=c)
        if bool(before["opted_out"]) != opted_out:
            _audit(ctx, c, "opt_out_notification_contact" if opted_out else "opt_in_notification_contact",
                   before=_safe(before), after=_safe(after))
    return after


def setup_push(ctx: RequestContext) -> dict:
    """The actor's ntfy topic (generated the first time). Unverified until they confirm a test push arrived."""
    require_context(ctx)
    with _uow(ctx.actor) as c:
        before = cdb.find_contact(ctx.actor, Channel.PUSH.value, kind=cdb.KIND_TOPIC, conn=c)
        row = cdb.ensure_push_contact(ctx.actor, conn=c)
        if before is None:
            _audit(ctx, c, "add_notification_contact", after=_safe(row))
    return row


def regenerate_push_topic(ctx: RequestContext) -> dict:
    """Replace the actor's topic with a fresh one (the old topic stops working); the new one is unverified."""
    require_context(ctx)
    with _uow(ctx.actor) as c:
        before = cdb.find_contact(ctx.actor, Channel.PUSH.value, kind=cdb.KIND_TOPIC, conn=c)
        row = cdb.regenerate_push_topic(ctx.actor, conn=c)
        _audit(ctx, c, "regenerate_push_topic", before=_safe(before), after=_safe(row), reason="topic replaced")
    return row


# ── Routing and preferences (private) ─────────────────────────────────────────

def set_routing(ctx: RequestContext, category: str, channel: str, enabled: bool) -> dict:
    """Switch one alert category x channel on or off. WHATSAPP, SMS and SLACK are refused ("not available yet")."""
    require_context(ctx)
    with _uow(ctx.actor) as c:
        before = rdb.set_routing(ctx.actor, category, channel, bool(enabled), conn=c)
        if before != bool(enabled):
            _audit(ctx, c, "set_notification_routing", before={"category": category, "channel": channel, "enabled": before},
                   after={"category": category, "channel": channel, "enabled": bool(enabled)})
        return rdb.get_routing(ctx.actor, conn=c)


def update_preferences(ctx: RequestContext, **fields) -> dict:
    """Change the actor's content level, quiet-hours mode, digest time, quiet hours or thresholds (validated). Returns the
    refreshed preferences. The legacy `channel` field is still accepted so old screens keep working."""
    require_context(ctx)
    _prepare(ctx.actor)
    with repo.unit_of_work() as c:
        before = udb.get_user_notifications_for(ctx).to_dict()
        after = udb.update_user_notifications_for(ctx, conn=c, **fields).to_dict()
        changed = {k: (before[k], after[k]) for k in fields if before.get(k) != after.get(k)}
        if changed:
            _audit(ctx, c, "update_notification_preferences", before={k: v[0] for k, v in changed.items()},
                   after={k: v[1] for k, v in changed.items()})
    return after


# ── The Notifications screens: own history, admin outbox ──────────────────────

def list_my_notifications(ctx: RequestContext, limit: int = 50) -> list[dict]:
    """The actor's OWN recent notifications, newest first — what was queued, sent, held or suppressed, and why. No addresses,
    no error text, nothing about anyone else."""
    require_context(ctx)
    _prepare(ctx.actor)
    return [{"id": r["id"], "created_at": r["created_at"], "category": r["category"], "channel": r["channel"], "status": r["status"],
             "subject": r["subject"], "reason_code": r["reason_code"], "attempts": r["attempts"], "sent_at": r["sent_at"],
             "next_attempt_at": r["next_attempt_at"]} for r in obx.list_recent(ctx.actor, limit)]


def admin_outbox_overview(ctx: RequestContext, failed_limit: int = 20) -> dict:
    """Admin-only: {counts, failed}. counts = outbox rows by status (system-wide); failed = the newest FAILED rows with the
    channel, category, attempts, the scrubbed error, and the recipient only as a MASKED address — never a raw address, never a
    message body, never the person's name (an owner appears only as a short tag)."""
    require_admin(ctx)
    _prepare()
    failed = []
    for r in obx.list_by_status(obx.FAILED, failed_limit):
        try:
            c = cdb.get_contact(r["owner"], r["contact_id"]) if r["contact_id"] else None
        except cdb.ContactNotFound:
            c = None
        failed.append({"id": r["id"], "created_at": r["created_at"], "category": r["category"], "channel": r["channel"],
                       "attempts": r["attempts"], "error": r["last_error"], "recipient": cdb.display_mask(r["channel"], c["address"]) if c else "—",
                       "owner_tag": cdb.address_tag(r["owner"])})
    return {"counts": obx.counts_by_status(), "failed": failed}


def admin_requeue(ctx: RequestContext, row_id: int) -> bool:
    """Admin-only: put one FAILED notification back in the queue (fresh attempt count). Audited. False if it is not FAILED."""
    require_admin(ctx)
    with _uow() as c:
        done = obx.requeue(row_id, conn=c)
        if done:
            _audit(ctx, c, "requeue_notification", after={"outbox_id": row_id})
    return done


# ── Admin-only: global settings ───────────────────────────────────────────────

def get_settings_admin(ctx: RequestContext) -> dict:
    """{settings, env, channels}: the settings row, each secret env var as present / missing (NEVER its value), and whether
    each channel can deliver globally right now. Admin-only."""
    require_admin(ctx)
    s = sdb.get_notification_settings()
    return {"settings": s, "env": sdb.env_presence(s), "channels": {ch: sdb.effective_channel_enabled(s, ch) for ch in CHANNELS}}


def update_settings(ctx: RequestContext, *, expected_version: int | None = None, reason: str | None = None, **fields) -> dict:
    require_admin(ctx)
    _prepare()
    with repo.unit_of_work() as c:
        before = sdb.get_notification_settings(conn=c)
        after = sdb.update_notification_settings(updated_by=ctx.actor, expected_version=expected_version, conn=c, **fields)
        changed = {k: (before[k], after[k]) for k in fields if before.get(k) != after.get(k)}
        if changed:
            _audit(ctx, c, "update_notification_settings", before={k: v[0] for k, v in changed.items()},
                   after={k: v[1] for k, v in changed.items()}, reason=reason)
    return after


def reset_settings(ctx: RequestContext, *, reason: str | None = None) -> dict:
    require_admin(ctx)
    _prepare()
    with repo.unit_of_work() as c:
        before = sdb.get_notification_settings(conn=c)
        after = sdb.reset_notification_settings_to_defaults(updated_by=ctx.actor, conn=c)
        _audit(ctx, c, "reset_notification_settings", before={"version": before["version"]}, after={"version": after["version"]},
               reason=reason or "reset to defaults")
    return after


# ═════════════════════════════════════════════════════════════════════════════
# Step 2 — enqueue (notify), verification, test sends, unsubscribe
# ═════════════════════════════════════════════════════════════════════════════

CODE_COOLDOWN_SECONDS = 60
TEST_COOLDOWN_SECONDS = 30
PUSH_CONFIRM_WINDOW_MINUTES = 15
_last_test: dict[tuple[str, str], float] = {}            # (owner, channel) -> monotonic time of the last test send (process-local)

SKIP_BELOW_SEVERITY, SKIP_BELOW_CONVICTION = "BELOW_SEVERITY", "BELOW_CONVICTION"
SUPPRESS_QUIET, SUPPRESS_RATE = "QUIET_HOURS", "RATE_LIMITED"


class VerificationError(ValueError):
    """A code could not be sent or confirmed (the message is safe to show; it never contains an address or a code)."""


def full_link(settings: dict, link: str | None) -> str | None:
    """An absolute URL for a link given as an app path ("/Portfolio_Health"); an absolute one is kept; no link when the
    public app URL is not configured (a relative link in an email is useless)."""
    if not link:
        return None
    if link.lower().startswith(("http://", "https://")):
        return link
    base = (settings.get("app_base_url") or "").rstrip("/")
    return f"{base}/{link.lstrip('/')}" if base else None


def notify(owner: str, category: str, *, dedup_key: str, title: str, brief: str, detailed: str | None = None,
           severity: int | None = None, conviction: float | None = None, link: str | None = None,
           now: datetime | None = None) -> dict:
    """
    Decide, for ONE person, whether and how a notification goes out — and enqueue it. Nothing is sent here: this only writes
    outbox rows (no network call, nothing inside a network call's transaction) and NEVER raises into the caller; a failure
    is logged and summarised in the result.

      * the person's filters first: minimum severity / conviction (skipped when the notification has no severity or the
        person set no conviction threshold), judged in the RECIPIENT's timezone for quiet hours;
      * then every channel that explain_channels() reports DELIVERABLE for this category gets its own outbox row;
      * PUSH is ALWAYS the brief text. EMAIL gets the detailed text only if the person chose DETAILED and one was supplied;
      * quiet hours: DEFER -> held (DEFERRED) until they end; SUPPRESS -> SUPPRESSED;
      * over the hourly limit -> SUPPRESSED with fold_into_digest = 1: rolled into the next digest, not dropped;
      * the same (owner, dedup_key, channel) is enqueued once however often a detector re-runs.

    Returns {"enqueued": [{channel, id, status}], "skipped": [{channel, reason}], "duplicates": [channel], "error": str|None}.
    """
    out: dict = {"enqueued": [], "skipped": [], "duplicates": [], "error": None}
    try:
        cat = AlertCategory(str(category).upper()).value
        if not owner or not dedup_key or not title or not brief:
            raise ValueError("owner, dedup_key, title and brief are required")
        _prepare(owner)
        settings = sdb.get_notification_settings()
        prefs = udb.get_user_notifications_by_owner(owner)
        why = explain_channels(owner, cat, settings=settings)
        deliver = [ch for ch, r in why.items() if r == DELIVERABLE]
        out["skipped"] = [{"channel": ch, "reason": r} for ch, r in why.items() if r not in (DELIVERABLE, NOT_ACTIVE)]
        if not deliver:
            return out
        local_now = udb.recipient_now(owner) if now is None else now
        decision = udb.evaluate_personal_filters(prefs, severity=severity, conviction=conviction, now_local=local_now)
        if not decision.ok and decision.reason in (SKIP_BELOW_SEVERITY, SKIP_BELOW_CONVICTION):
            out["skipped"] += [{"channel": ch, "reason": decision.reason} for ch in deliver]
            return out
        link_url = full_link(settings, link)
        clock = now or datetime.now(timezone.utc)
        window_start = clock - timedelta(hours=1)
        contacts = deliverable_contacts(owner, cat, settings=settings)
        for ch in deliver:
            primary = next((c for c in contacts if c["channel"] == ch), None)
            detailed_body = detailed if (ch == Channel.EMAIL.value and prefs.content_level == ContentLevel.DETAILED.value and detailed) else None
            status, next_at, reason, fold = obx.PENDING, None, None, False
            if cat != AlertCategory.DAILY_DIGEST.value and obx.count_recent(owner, ch, window_start) >= int(settings["max_messages_per_user_per_hour"]):
                status, reason, fold = obx.SUPPRESSED, SUPPRESS_RATE, True
            elif not decision.ok:                                           # quiet hours
                if prefs.quiet_hours_mode == QuietMode.SUPPRESS.value:
                    status, reason = obx.SUPPRESSED, SUPPRESS_QUIET
                else:
                    status, next_at = obx.DEFERRED, obx.iso(decision.defer_until)
            row_id = obx.enqueue(owner, cat, ch, dedup_key=dedup_key, subject=title, body_brief=brief, body_detailed=detailed_body,
                                 link_url=link_url, severity=severity, contact_id=primary["id"] if primary else None, status=status,
                                 next_attempt_at=next_at, reason_code=reason, fold_into_digest=fold, created_at=obx.iso(clock))
            if row_id is None:
                out["duplicates"].append(ch)
            else:
                out["enqueued"].append({"channel": ch, "id": row_id, "status": status})
    except Exception as exc:                                                 # a notification must never break its caller
        out["error"] = type(exc).__name__
        _log.warning(f"  [notifications] notify skipped ({type(exc).__name__})", event_type="warning")
    return out


# ── Alternate-email verification (6-digit code) ───────────────────────────────

def _code_hash(contact_id: int, code: str, env: dict | None = None) -> str:
    """Only this hash is stored. It is peppered with the signing key when one is set; the guess limit, not the hash, is what
    protects a 6-digit code."""
    env = os.environ if env is None else env
    pepper = env.get(sdb.get_notification_settings()["unsubscribe_signing_key_env"]) or ""
    return hashlib.sha256(f"{contact_id}:{code}:{pepper}".encode()).hexdigest()


def start_email_verification(ctx: RequestContext, contact_id: int, *, registry: dict | None = None, now: datetime | None = None,
                             env: dict | None = None) -> dict:
    """Email the actor's ALTERNATE address a 6-digit code (stored only as a hash; expires after the configured minutes; at
    most one per 60 seconds). Sent directly, outside any transaction."""
    require_context(ctx)
    now = now or datetime.now(timezone.utc)
    settings = sdb.get_notification_settings()
    contact = cdb.get_contact(ctx.actor, contact_id)
    if contact["channel"] != Channel.EMAIL.value or contact["kind"] != cdb.KIND_ALTERNATE:
        raise VerificationError("only an alternate email needs verifying")
    if contact["verified"]:
        raise VerificationError("that address is already verified")
    if not sdb.effective_channel_enabled(settings, Channel.EMAIL.value):
        raise VerificationError("email notifications are not set up yet; ask an administrator")
    if contact["last_code_sent_at"]:
        wait = CODE_COOLDOWN_SECONDS - (now - datetime.fromisoformat(contact["last_code_sent_at"])).total_seconds()
        if wait > 0:
            raise VerificationError(f"please wait {int(wait) + 1} seconds before asking for another code")
    code = f"{secrets.randbelow(10 ** 6):06d}"
    expires = now + timedelta(minutes=int(settings["verification_code_ttl_minutes"]))
    with _uow(ctx.actor) as c:
        cdb.store_verification_code(ctx.actor, contact_id, _code_hash(contact_id, code, env), obx.iso(expires), obx.iso(now), conn=c)
        _audit(ctx, c, "start_contact_verification", after=_safe(contact))
    registry = registry or senders.build_registry(settings, env=env)
    message = senders.Message(
        owner=ctx.actor, channel=Channel.EMAIL.value, category=AlertCategory.SYSTEM.value, subject="Your APEX verification code",
        body=f"Your APEX verification code is {code}. It expires in {settings['verification_code_ttl_minutes']} minutes. "
             f"If you did not ask for this, you can ignore this email.",
        recipients=[senders.Recipient(contact_id, contact["address"])])
    result = registry[Channel.EMAIL.value].send(message)                # network: outside any transaction
    if result.status != senders.SENT:
        with _uow(ctx.actor) as c:                                     # a code nobody received must not start the cooldown
            c.execute("UPDATE notification_contacts SET verification_code_hash = NULL, verification_expires_at = NULL, "
                      "last_code_sent_at = NULL WHERE id = ? AND owner = ?", [contact_id, ctx.actor])
        _log.warning(f"  [notifications] verification email not sent tag={cdb.address_tag(contact['address'])} status={result.status}",
                     event_type="warning")
        raise VerificationError("we could not send the code right now; try again in a minute")
    return {"sent": True, "expires_at": obx.iso(expires), "cooldown_seconds": CODE_COOLDOWN_SECONDS}


def confirm_email_code(ctx: RequestContext, contact_id: int, code: str, *, now: datetime | None = None, env: dict | None = None) -> dict:
    """Check a code. Every guess counts (a wrong one included); after the allowed attempts, or once the code expired, a new code
    must be requested. A correct code verifies the contact."""
    require_context(ctx)
    now = now or datetime.now(timezone.utc)
    settings = sdb.get_notification_settings()
    outcome = "BAD"
    with _uow(ctx.actor) as c:
        contact = cdb.get_contact(ctx.actor, contact_id, conn=c)
        if contact["verified"]:
            return contact
        if not contact["verification_code_hash"] or not contact["verification_expires_at"] or \
                datetime.fromisoformat(contact["verification_expires_at"]) <= now:
            outcome = "EXPIRED"
        else:
            attempts = cdb.bump_verification_attempts(ctx.actor, contact_id, conn=c)
            if attempts > int(settings["verification_max_attempts"]):
                cdb.clear_verification_code(ctx.actor, contact_id, conn=c)
                outcome = "TOO_MANY"
            elif hmac.compare_digest(contact["verification_code_hash"], _code_hash(contact_id, str(code).strip(), env)):
                after = cdb.set_verified(ctx.actor, contact_id, True, conn=c)
                _audit(ctx, c, "verify_notification_contact", before=_safe(contact), after=_safe(after), reason="email code")
                return after
            else:
                outcome = "BAD"
    messages = {"EXPIRED": "that code has expired; ask for a new one", "TOO_MANY": "too many attempts; ask for a new code",
                "BAD": "that code is not right"}
    raise VerificationError(messages[outcome])


# ── Test sends and push confirmation ──────────────────────────────────────────

def send_test(ctx: RequestContext, channel: str, *, registry: dict | None = None, env: dict | None = None) -> dict:
    """Send a test notification to the actor on EMAIL or PUSH (directly, outside any transaction; at most one per 30 seconds
    per channel). A PUSH test is what lets the person confirm the phone received it (confirm_push_received)."""
    require_context(ctx)
    ch = cdb.refuse_unavailable(channel)
    settings = sdb.get_notification_settings()
    if not sdb.effective_channel_enabled(settings, ch.value):
        raise VerificationError(f"{ch.value.title()} notifications are not switched on yet; ask an administrator")
    key = (ctx.actor, ch.value)
    if time.monotonic() - _last_test.get(key, -1e9) < TEST_COOLDOWN_SECONDS:
        raise VerificationError("a test was just sent; wait a moment")
    contacts = ensure_contacts(ctx)
    if ch == Channel.PUSH:
        targets = [c for c in contacts if c["channel"] == "PUSH" and not c["opted_out"]]
        body = "This is a test notification from APEX. If you can read this on your phone, push is working — tap \"I got it\" in APEX."
    else:
        targets = [c for c in contacts if c["channel"] == "EMAIL" and c["verified"] and not c["opted_out"]]
        body = "This is a test notification from APEX. If you can read this, email notifications are working."
    if not targets:
        raise VerificationError("there is no verified contact on that channel to test")
    _last_test[key] = time.monotonic()
    registry = registry or senders.build_registry(settings, env=env)
    message = senders.Message(
        owner=ctx.actor, channel=ch.value, category=AlertCategory.SYSTEM.value, subject="APEX test notification", body=body, is_test=True,
        recipients=[senders.Recipient(c["id"], c["address"]) for c in targets],
        unsubscribe_urls={c["id"]: u for c in targets if (u := senders.unsubscribe_url(settings, ctx.actor, c["id"], env))},
        manage_url=full_link(settings, "/Profile"))
    result = registry[ch.value].send(message)                          # network: outside any transaction
    with _uow(ctx.actor) as c:
        if ch == Channel.PUSH and result.status == senders.SENT:
            c.execute("UPDATE notification_contacts SET last_code_sent_at = ? WHERE channel = 'PUSH' AND owner = ?",
                      [obx.now_iso(), ctx.actor])                      # remembers a test went out, so "I got it" can be accepted
        _audit(ctx, c, "send_test_notification", after={"channel": ch.value, "status": result.status, "contacts": len(targets)})
    if result.status != senders.SENT:
        raise VerificationError("the test could not be sent right now" + ("" if result.status == senders.RETRY else " — check the settings"))
    return {"sent": True, "channel": ch.value}


def confirm_push_received(ctx: RequestContext, *, now: datetime | None = None) -> dict:
    """The person says the test push arrived on their phone: verify the push contact. Only accepted shortly after a test was
    actually sent."""
    require_context(ctx)
    now = now or datetime.now(timezone.utc)
    with _uow(ctx.actor) as c:
        contact = cdb.find_contact(ctx.actor, Channel.PUSH.value, kind=cdb.KIND_TOPIC, conn=c)
        if contact is None:
            raise VerificationError("set up push first")
        sent_at = contact["last_code_sent_at"]
        if not sent_at or now - datetime.fromisoformat(sent_at) > timedelta(minutes=PUSH_CONFIRM_WINDOW_MINUTES):
            raise VerificationError("send a test notification first, then confirm it arrived")
        after = cdb.set_verified(ctx.actor, contact["id"], True, conn=c)
        _audit(ctx, c, "verify_notification_contact", before=_safe(contact), after=_safe(after), reason="push test confirmed")
    return after


# ── One-click unsubscribe (no login) ──────────────────────────────────────────

def unsubscribe_with_token(token: str, *, env: dict | None = None) -> dict:
    """Opt ONE contact out from the signed link in an email. No login: the signature is the proof. Idempotent, and it says
    nothing about why a bad token was bad. Returns {"ok": bool}."""
    settings = sdb.get_notification_settings()
    env = os.environ if env is None else env
    key = env.get(settings["unsubscribe_signing_key_env"]) or ""
    who = senders.verify_unsubscribe_token(token, key) if key else None
    if who is None:
        return {"ok": False}
    owner, contact_id = who
    try:
        _prepare(owner)
        with repo.unit_of_work() as c:
            before = cdb.get_contact(owner, contact_id, conn=c)
            after = cdb.set_opted_out(owner, contact_id, True, conn=c)
            if not before["opted_out"]:
                portfolio = repo.get_portfolio_by_owner(owner, conn=c)
                repo.record_audit_event(actor=owner, source="email-unsubscribe-link", request_id=None,
                                        operation="opt_out_notification_contact", portfolio_id=portfolio["portfolio_id"] if portfolio else None,
                                        before=_safe(before), after=_safe(after), reason="one-click unsubscribe", conn=c)
        return {"ok": True}
    except cdb.ContactNotFound:
        return {"ok": False}
