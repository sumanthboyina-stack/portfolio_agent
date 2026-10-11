"""
Notification senders — the ONLY code that talks to a mail or push server.

    NotificationSender.send(Message) -> SendResult        (never raises; every failure is a SendResult)

  EmailSender       smtplib + STARTTLS (works with a Gmail app password). Plain text + simple HTML, a "Manage notifications"
                    link and a one-click unsubscribe link in the footer and the List-Unsubscribe header.
  NtfyPushSender    one JSON POST per recipient to the ntfy server (UTF-8 safe; the topic travels in the body, not the URL, so
                    it does not end up in access logs), optional bearer token, priority mapped from severity, click URL.
  FakeSender        records what it was asked to send; used by every test (no test touches a network).
  WhatsAppSender    STUB — returns "skipped: channel not implemented". See its TODO for what a real one needs.
  SmsSender         STUB — same.

Rules every sender follows
  * Secrets (SMTP username / password, ntfy token) are read from the environment at SEND time, by the env var NAME stored in
    notification_settings; they are never stored, cached, logged or put into an error message.
  * Every network call has a timeout (NETWORK_TIMEOUT_SECONDS).
  * An error string handed back is scrubbed: any secret value is replaced by [secret], any email address by its short tag, any
    push topic by [topic].
  * A transient failure (timeout, dropped connection, 4xx SMTP, ntfy 429/5xx) is "retry"; a permanent one (bad login, refused
    recipient, ntfy 401/403/404, missing credentials) is "failed"; nothing to do is "skipped".
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html as _html
import json
import os
import re
import smtplib
import ssl
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from portfolio_agent.domain import Channel
from portfolio_agent.tools.notification_contacts_db import address_tag

NETWORK_TIMEOUT_SECONDS = 15

SENT, RETRY, FAILED, SKIPPED = "sent", "retry", "failed", "skipped"
_EMAIL_IN_TEXT = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_TOPIC_IN_TEXT = re.compile(r"apex-[0-9a-f]{32}")


@dataclass(frozen=True)
class Recipient:
    contact_id: int
    address: str                                     # raw: handed to the provider, never logged or stored in an error


@dataclass
class Message:
    owner: str
    channel: str
    category: str
    subject: str
    body: str                                        # plain text (for push: the brief text)
    recipients: list[Recipient]
    dedup_key: str = ""
    severity: int | None = None
    link_url: str | None = None
    unsubscribe_urls: dict[int, str] = field(default_factory=dict)     # contact_id -> signed one-click unsubscribe URL
    manage_url: str | None = None
    is_test: bool = False


@dataclass(frozen=True)
class SendResult:
    status: str                                      # sent | retry | failed | skipped
    provider_message_id: str | None = None
    error: str | None = None                         # scrubbed; safe to store
    reason: str | None = None                        # machine-readable, for skipped


def scrub(text: object, secrets: list[str | None] = (), addresses: list[str] = ()) -> str:
    """Remove anything sensitive from an error string before it is stored or logged."""
    out = str(text)
    for value in [*secrets, *addresses]:
        if value and len(value) >= 3:
            out = out.replace(value, "[secret]" if value in secrets else f"<{address_tag(value)}>")
    out = _TOPIC_IN_TEXT.sub("[topic]", out)
    return _EMAIL_IN_TEXT.sub(lambda m: f"<{address_tag(m.group(0))}>", out)[:300]


class NotificationSender(ABC):
    channel: str = ""

    @abstractmethod
    def send(self, message: Message) -> SendResult: ...


# ── Unsubscribe tokens (signed, no login needed) ──────────────────────────────

def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign_unsubscribe_token(owner: str, contact_id: int, key: str) -> str:
    """A tamper-proof token naming one contact of one person. HMAC-SHA256 over the payload with a secret key that never
    leaves the environment; anyone holding the link can opt THAT contact out and nothing else."""
    payload = _b64(json.dumps({"o": owner, "c": int(contact_id)}, separators=(",", ":")).encode())
    sig = _b64(hmac.new(key.encode(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def verify_unsubscribe_token(token: str, key: str) -> tuple[str, int] | None:
    """(owner, contact_id) for a genuine token, else None. Constant-time comparison; never raises."""
    try:
        payload, sig = (token or "").split(".", 1)
        good = _b64(hmac.new(key.encode(), payload.encode(), hashlib.sha256).digest())
        if not key or not hmac.compare_digest(sig, good):
            return None
        data = json.loads(_unb64(payload))
        return str(data["o"]), int(data["c"])
    except Exception:
        return None


def unsubscribe_url(settings: dict, owner: str, contact_id: int, env: dict | None = None) -> str | None:
    """The one-click unsubscribe link for a contact, or None when the app URL or the signing key is not configured."""
    env = os.environ if env is None else env
    base, key = (settings.get("app_base_url") or "").rstrip("/"), env.get(settings.get("unsubscribe_signing_key_env") or "")
    if not base or not key:
        return None
    return f"{base}/Unsubscribe?token={sign_unsubscribe_token(owner, contact_id, key)}"


# ── Email ─────────────────────────────────────────────────────────────────────

class EmailSender(NotificationSender):
    channel = Channel.EMAIL.value

    def __init__(self, settings: dict, *, env: dict | None = None, smtp_factory=None, timeout: float = NETWORK_TIMEOUT_SECONDS):
        self.s, self.env, self.timeout = settings, (os.environ if env is None else env), timeout
        self.smtp_factory = smtp_factory or smtplib.SMTP

    def _build(self, message: Message, recipient: Recipient) -> EmailMessage:
        s = self.s
        msg = EmailMessage()
        msg["From"] = formataddr((s["email_from_name"], s["email_from_address"]))
        msg["To"] = recipient.address
        msg["Subject"] = message.subject.replace("\r", " ").replace("\n", " ")
        msg["Message-ID"] = make_msgid(domain=s["email_from_address"].rsplit("@", 1)[-1])
        msg["Auto-Submitted"] = "auto-generated"
        msg["X-APEX-Category"] = message.category
        unsub = message.unsubscribe_urls.get(recipient.contact_id)
        if unsub:
            msg["List-Unsubscribe"] = f"<{unsub}>"
        footer_lines = ["You are receiving this because of your APEX notification settings."]
        if message.manage_url:
            footer_lines.append(f"Manage notifications: {message.manage_url}")
        if unsub:
            footer_lines.append(f"Stop emails to this address: {unsub}")
        footer_lines.append("Informational only — not investment advice.")
        text = message.body + (f"\n\nOpen in APEX: {message.link_url}" if message.link_url else "") + "\n\n--\n" + "\n".join(footer_lines)
        msg.set_content(text)
        esc = _html.escape
        link = f'<p><a href="{esc(message.link_url)}">Open in APEX</a></p>' if message.link_url else ""
        foot = "".join(f'<div>{esc(l)}</div>' if "http" not in l else f'<div>{esc(l.split(": ", 1)[0])}: <a href="{esc(l.split(": ", 1)[1])}">link</a></div>'
                       for l in footer_lines)
        msg.add_alternative(
            f'<html><body style="font-family:Arial,sans-serif;font-size:14px;color:#111827">'
            f'{"".join(f"<p>{esc(p)}</p>" for p in message.body.split(chr(10) + chr(10)))}{link}'
            f'<hr style="border:none;border-top:1px solid #E5E7EB"><div style="font-size:12px;color:#6B7280">{foot}</div></body></html>',
            subtype="html")
        return msg

    def send(self, message: Message) -> SendResult:
        s = self.s
        user, password = self.env.get(s["smtp_username_env"]), self.env.get(s["smtp_password_env"])
        secrets = [user, password]
        addresses = [r.address for r in message.recipients]
        if not user or not password:
            return SendResult(FAILED, error=f"SMTP credentials are not set in the environment ({s['smtp_username_env']}, {s['smtp_password_env']})")
        if not s.get("smtp_host") or not s.get("email_from_address"):
            return SendResult(SKIPPED, reason="EMAIL_NOT_CONFIGURED")
        if not message.recipients:
            return SendResult(SKIPPED, reason="NO_RECIPIENT")
        sent_ids: list[str] = []
        server = None
        try:
            server = self.smtp_factory(s["smtp_host"], int(s["smtp_port"]), timeout=self.timeout)
            server.ehlo()
            if s["smtp_use_tls"]:
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
            server.login(user, password)
            for r in message.recipients:
                built = self._build(message, r)
                server.send_message(built)
                sent_ids.append(built["Message-ID"])
            return SendResult(SENT, provider_message_id=sent_ids[-1])
        except smtplib.SMTPAuthenticationError:
            return SendResult(FAILED, error="SMTP login was rejected — check the app password and username")
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPNotSupportedError) as exc:
            return SendResult(FAILED, error=scrub(f"{type(exc).__name__}: {exc}", secrets, addresses))
        except smtplib.SMTPResponseException as exc:
            kind = RETRY if 400 <= int(exc.smtp_code) < 500 else FAILED
            return SendResult(kind, error=scrub(f"SMTP {exc.smtp_code}: {exc.smtp_error!r}", secrets, addresses))
        except (smtplib.SMTPException, OSError, TimeoutError, ssl.SSLError) as exc:          # dropped connection, DNS, timeout...
            return SendResult(RETRY, error=scrub(f"{type(exc).__name__}: {exc}", secrets, addresses))
        except Exception as exc:                                                                  # a bug here must not stop the worker
            return SendResult(RETRY, error=scrub(f"{type(exc).__name__}: {exc}", secrets, addresses))
        finally:
            if server is not None:
                try:
                    server.quit()
                except Exception:
                    pass


# ── ntfy push ─────────────────────────────────────────────────────────────────

def ntfy_priority(severity: int | None) -> int:
    """ntfy priority 1 (min) .. 5 (max) from a 1..5 severity; no severity = default (3)."""
    if severity is None:
        return 3
    return max(1, min(5, int(severity)))


class NtfyPushSender(NotificationSender):
    channel = Channel.PUSH.value

    def __init__(self, settings: dict, *, env: dict | None = None, http_post=None, timeout: float = NETWORK_TIMEOUT_SECONDS):
        self.s, self.env, self.timeout = settings, (os.environ if env is None else env), timeout
        self._post = http_post

    def _http_post(self):
        if self._post is not None:
            return self._post
        import requests
        return requests.post

    def send(self, message: Message) -> SendResult:
        s = self.s
        token = self.env.get(s["ntfy_token_env"]) or None
        topics = [r.address for r in message.recipients]
        if not s.get("ntfy_base_url"):
            return SendResult(SKIPPED, reason="PUSH_NOT_CONFIGURED")
        if not topics:
            return SendResult(SKIPPED, reason="NO_RECIPIENT")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        last_id = None
        try:
            post = self._http_post()
            for topic in topics:
                payload = {"topic": topic, "title": message.subject[:250], "message": message.body[:3900],
                           "priority": ntfy_priority(message.severity), "tags": ["bar_chart"]}
                if message.link_url:
                    payload["click"] = message.link_url
                resp = post(s["ntfy_base_url"], json=payload, headers=headers, timeout=self.timeout)
                code = int(getattr(resp, "status_code", 0))
                if code == 200:
                    try:
                        last_id = resp.json().get("id")
                    except Exception:
                        last_id = None
                    continue
                if code in (401, 403):
                    return SendResult(FAILED, error=f"ntfy refused the request ({code}) — check the access token")
                if code == 429 or code >= 500:
                    return SendResult(RETRY, error=f"ntfy returned {code}")
                return SendResult(FAILED, error=f"ntfy returned {code}")
            return SendResult(SENT, provider_message_id=last_id)
        except Exception as exc:                                    # timeouts, DNS, connection resets: all transient
            return SendResult(RETRY, error=scrub(f"{type(exc).__name__}: {exc}", [token], topics))


# ── Fake (tests) ──────────────────────────────────────────────────────────────

class FakeSender(NotificationSender):
    """Records every message it is asked to send and returns scripted results (default: sent). No network, ever."""

    def __init__(self, channel: str, script: list[SendResult] | None = None, on_send=None):
        self.channel, self.script, self.on_send = channel, list(script or []), on_send
        self.sent: list[Message] = []
        self.calls = 0

    def send(self, message: Message) -> SendResult:
        self.calls += 1
        if self.on_send is not None:
            self.on_send(message)
        self.sent.append(message)
        return self.script.pop(0) if self.script else SendResult(SENT, provider_message_id=f"fake-{self.calls}")


# ── Stubs: reserved channels (no network code) ────────────────────────────────

class WhatsAppSender(NotificationSender):
    """
    STUB. Returns "skipped: channel not implemented" and sends nothing.

    TODO — what a real WhatsApp sender needs (Twilio or the Meta WhatsApp Cloud API):
      * a verified business sender and its API credentials, read from environment variables by NAME like every other secret;
      * phone-number contacts in E.164 format (+15551234567) with the same 6-digit code verification as an alternate email,
        and the contact table's channel validation / the notification_settings.whatsapp_enabled block lifted;
      * pre-approved message TEMPLATES for anything sent outside the 24-hour customer-service window (WhatsApp rejects
        free-form text there) — so Message needs a template id + variables, not just a body;
      * opt-in capture and a STOP / unsubscribe reply handler (an inbound webhook), which this app cannot receive today;
      * delivery-status callbacks to move SENT to delivered / failed.
    Enabling it later = implement send() here, lift the validation in notification_settings_db and notification_contacts_db,
    and add WHATSAPP to domain.ACTIVE_CHANNELS.
    """
    channel = Channel.WHATSAPP.value

    def send(self, message: Message) -> SendResult:
        return SendResult(SKIPPED, reason="channel not implemented")


class SmsSender(NotificationSender):
    """
    STUB. Returns "skipped: channel not implemented" and sends nothing.

    TODO — what a real SMS sender needs (Twilio or similar):
      * an account, a sending number or messaging service, and credentials from environment variables;
      * US carriers require A2P 10DLC brand and campaign registration before application-to-person traffic is delivered;
      * phone contacts in E.164 format with code verification and explicit consent recorded;
      * STOP / HELP keyword handling (an inbound webhook) — carriers require honouring STOP immediately;
      * length limits (160 GSM-7 characters per segment) — brief text only, never an amount or a position size;
      * quiet-hours rules (SMS is more intrusive than push or email) and per-message cost controls.
    Enabling it later = implement send() here, lift the validation in notification_settings_db and notification_contacts_db,
    and add SMS to domain.ACTIVE_CHANNELS.
    """
    channel = Channel.SMS.value

    def send(self, message: Message) -> SendResult:
        return SendResult(SKIPPED, reason="channel not implemented")


def build_registry(settings: dict, *, env: dict | None = None, smtp_factory=None, http_post=None) -> dict[str, NotificationSender]:
    """One sender per channel. Only EMAIL and PUSH can send; the stubs exist so enabling a channel later is just
    implementing its send()."""
    return {Channel.EMAIL.value: EmailSender(settings, env=env, smtp_factory=smtp_factory),
            Channel.PUSH.value: NtfyPushSender(settings, env=env, http_post=http_post),
            Channel.WHATSAPP.value: WhatsAppSender(), Channel.SMS.value: SmsSender()}
