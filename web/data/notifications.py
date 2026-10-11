"""
Notifications screen data functions — everything the Notifications section (Profile page) and the admin editor show,
shaped here so the page files only lay it out and every rule is testable without Streamlit.

  load_user_view(ctx)        the actor's own contacts, routing grid, preferences and recent notifications
  routing_grid(...)          category x channel cells: on / off, and which are not editable (WhatsApp, SMS: "Coming later";
                             a channel a category does not use)
  contact_rows(...)          one row per contact with a status badge and what the person can do with it
  push_setup(...)            what the phone-push setup shows: subscribe URL, steps, QR code (SVG, or None without segno)
  load_admin_view(ctx)       admin only: the settings row, each secret env var as PRESENT / MISSING (never its value),
                             which channels can deliver globally, outbox counts and recent FAILED rows with MASKED recipients
  routing_changes(...)       what a submitted routing grid changes compared with what is stored

Owner scoping: every read is the actor's own (ctx.actor) through services.notification_service; the admin view is refused
for anyone who is not an enabled administrator; no admin row carries a raw address, a message body or a person's name.
"""
from __future__ import annotations

from portfolio_agent.domain import ACTIVE_CHANNELS, ALERT_CATEGORIES, Channel
from portfolio_agent.services import notification_service as ns
from portfolio_agent.tools import notification_contacts_db as cdb
from portfolio_agent.tools import notification_routing_db as rdb
from portfolio_agent.tools import notification_settings_db as sdb

CATEGORY_LABELS = {
    "MARKET_EVENT": "Market events on tickers I hold or watch",
    "PORTFOLIO_HEALTH": "Portfolio Health needs action",
    "COMPLIANCE": "Compliance (approvals, restricted list)",
    "DAILY_DIGEST": "Daily digest",
    "SYSTEM": "System messages",
}
CHANNEL_LABELS = {"EMAIL": "Email", "PUSH": "Phone push", "WHATSAPP": "WhatsApp", "SMS": "SMS"}
GRID_CHANNELS = ("EMAIL", "PUSH", "WHATSAPP", "SMS")             # the columns shown (SLACK is not offered)
STATUS_LABELS = {"PENDING": "Waiting", "DEFERRED": "Held until quiet hours end", "SENDING": "Sending", "SENT": "Sent",
                 "FAILED": "Failed", "SUPPRESSED": "Not sent"}
REASON_TEXT = {
    ns.DELIVERABLE: "Will be delivered",
    ns.NOT_ACTIVE: "Not available yet",
    ns.GLOBALLY_DISABLED: "Switched off by an administrator",
    ns.NOT_ROUTED: "Turned off for this alert type",
    ns.NO_CONTACT: "No contact set up",
    ns.UNVERIFIED: "Not verified yet",
    ns.OPTED_OUT: "You opted out of this contact",
}
SUPPRESS_TEXT = {"QUIET_HOURS": "arrived during quiet hours", "RATE_LIMITED": "held back by the hourly limit (goes in your digest)",
                 "OPTED_OUT": "you opted out", "ROUTING_OFF": "turned off for this alert type", "CHANNEL_DISABLED": "switched off",
                 "NO_CONTACT": "no contact", "UNVERIFIED": "contact not verified", "EMPTY_DIGEST": "nothing to report",
                 "RATE_LIMITED_DIGESTED": "included in a digest", "CHANNEL_NOT_ACTIVE": "channel not available"}


# ── Contacts ──────────────────────────────────────────────────────────────────

def contact_badge(contact: dict) -> tuple[str, str]:
    """(label, tone) — tone is success | warning | neutral."""
    if contact["opted_out"]:
        return "Opted out", "neutral"
    return ("Verified", "success") if contact["verified"] else ("Not verified yet", "warning")


def contact_rows(contacts: list[dict]) -> list[dict]:
    """One row per contact, in display order: sign-in email, alternate email, phone push. What the person may do is spelled
    out so the page does not decide it: remove (never the sign-in email), opt out / back in, verify (alternate email only)."""
    order = {"LOGIN": 0, "ALTERNATE": 1, "TOPIC": 2}
    rows = []
    for c in sorted(contacts, key=lambda c: order.get(c["kind"], 9)):
        label, tone = contact_badge(c)
        title = {"LOGIN": "Sign-in email", "ALTERNATE": "Alternate email", "TOPIC": "Phone push (ntfy)"}.get(c["kind"], c["channel"])
        rows.append({
            "id": c["id"], "kind": c["kind"], "channel": c["channel"], "title": title, "badge": label, "tone": tone,
            "address": c["address"] if c["channel"] == "EMAIL" else None,                    # your own email is yours to see
            "masked": cdb.display_mask(c["channel"], c["address"]),
            "can_remove": c["kind"] != "LOGIN", "opted_out": bool(c["opted_out"]), "verified": bool(c["verified"]),
            "can_verify_by_code": c["kind"] == "ALTERNATE" and not c["verified"],
            "can_confirm_push": c["kind"] == "TOPIC" and not c["verified"]})
    return rows


# ── Routing grid ──────────────────────────────────────────────────────────────

def routing_grid(routing: dict, channels: dict) -> list[dict]:
    """
    One row per alert category; one cell per column (Email, Push, WhatsApp, SMS):
      editable cells are EMAIL / PUSH where the category uses that channel; WhatsApp and SMS are shown disabled, "Coming later";
      a channel a category does not use (a digest has no push) is shown disabled, "n/a".
    channels[category][channel] (explain_channels) supplies the plain-words reason a switched-on cell would not deliver.
    """
    rows = []
    for cat in ALERT_CATEGORIES:
        allowed = rdb.ALLOWED_CHANNELS[cat]
        cells = []
        for ch in GRID_CHANNELS:
            if Channel(ch) not in ACTIVE_CHANNELS:
                cells.append({"channel": ch, "label": CHANNEL_LABELS[ch], "enabled": False, "editable": False, "note": "Coming later"})
            elif ch not in allowed:
                cells.append({"channel": ch, "label": CHANNEL_LABELS[ch], "enabled": False, "editable": False, "note": "n/a"})
            else:
                on = bool(routing.get(cat, {}).get(ch, False))
                why = channels.get(cat, {}).get(ch, "")
                cells.append({"channel": ch, "label": CHANNEL_LABELS[ch], "enabled": on, "editable": True,
                              "note": "" if not on or why == ns.DELIVERABLE else REASON_TEXT.get(why, why)})
        rows.append({"category": cat, "label": CATEGORY_LABELS[cat], "cells": cells})
    return rows


def routing_changes(routing: dict, submitted: dict[tuple[str, str], bool]) -> list[tuple[str, str, bool]]:
    """[(category, channel, new value)] for every submitted cell that differs from what is stored. Cells that cannot be edited
    (WhatsApp, SMS, a channel the category does not use) are ignored even if a client sent them."""
    out = []
    for (cat, ch), value in submitted.items():
        if cat not in rdb.ALLOWED_CHANNELS or ch not in rdb.ALLOWED_CHANNELS[cat] or Channel(ch) not in ACTIVE_CHANNELS:
            continue
        if bool(routing.get(cat, {}).get(ch, False)) != bool(value):
            out.append((cat, ch, bool(value)))
    return out


# ── Phone push setup ──────────────────────────────────────────────────────────

def qr_svg(text: str) -> str | None:
    """An inline SVG QR code for *text*, or None when the optional segno library is not installed (the topic is then shown
    as text only)."""
    try:
        import io

        import segno
        buf = io.BytesIO()
        segno.make(text, error="m").save(buf, kind="svg", scale=5, border=2, xmldecl=False, svgns=True, nl=False)
        return buf.getvalue().decode()
    except Exception:
        return None


def push_setup(settings: dict, topic: str) -> dict:
    """What the phone-push setup shows: the topic URL, the same address for the ntfy app's own scheme, the steps, and a QR code
    of the URL. The topic is the secret: callers show it only to its owner."""
    base = (settings.get("ntfy_base_url") or "https://ntfy.sh").rstrip("/")
    host = base.split("://", 1)[-1]
    url = f"{base}/{topic}"
    return {"topic": topic, "url": url, "app_url": f"ntfy://{host}/{topic}", "server": base, "qr_svg": qr_svg(url),
            "public_server": host == "ntfy.sh",
            "steps": ["Install the ntfy app (iPhone: App Store · Android: Google Play or F-Droid).",
                      "In the app tap + and choose Subscribe to topic.",
                      "Enter your topic exactly as shown (or scan the QR code)." + ("" if host == "ntfy.sh" else f" Use the server {base}."),
                      "Back here, press Send test, and when it appears on your phone press I got it."]}


# ── Recent notifications ──────────────────────────────────────────────────────

def recent_rows(notifications: list[dict]) -> list[dict]:
    """The own-notifications table, in plain words."""
    out = []
    for n in notifications:
        status = STATUS_LABELS.get(n["status"], n["status"])
        if n["status"] == "SUPPRESSED" and n.get("reason_code"):
            status = f"Not sent — {SUPPRESS_TEXT.get(n['reason_code'], n['reason_code'].lower().replace('_', ' '))}"
        out.append({"When": (n["created_at"] or "")[:16].replace("T", " "), "Type": CATEGORY_LABELS.get(n["category"], n["category"]),
                    "Channel": CHANNEL_LABELS.get(n["channel"], n["channel"]), "Status": status, "Message": n["subject"]})
    return out


# ── Views ─────────────────────────────────────────────────────────────────────

def load_user_view(ctx, *, recent_limit: int = 50) -> dict:
    """Everything the Notifications section needs, for the actor only."""
    state = ns.get_notification_state(ctx)
    settings = sdb.get_notification_settings()
    topic = next((c["address"] for c in state["contacts"] if c["kind"] == "TOPIC"), None)
    return {
        "contacts": contact_rows(state["contacts"]), "routing": state["routing"], "grid": routing_grid(state["routing"], state["channels"]),
        "preferences": state["preferences"], "channels": state["channels"],
        "push": push_setup(settings, topic) if topic else None,
        "can_test": {"EMAIL": sdb.effective_channel_enabled(settings, "EMAIL"), "PUSH": sdb.effective_channel_enabled(settings, "PUSH")},
        "recent": recent_rows(ns.list_my_notifications(ctx, recent_limit)),
    }


def load_admin_view(ctx) -> dict:
    """Admin only (refused with NotAdmin for anyone else): settings, secret env var PRESENT / MISSING, which channels can
    deliver globally, outbox counts and recent FAILED rows with masked recipients."""
    admin = ns.get_settings_admin(ctx)
    outbox = ns.admin_outbox_overview(ctx)
    return {"settings": admin["settings"], "env": admin["env"], "channels": admin["channels"], "counts": outbox["counts"],
            "failed": outbox["failed"]}


# ── Preferences ───────────────────────────────────────────────────────────────

def validate_preferences(values: dict) -> list[str]:
    """Plain-words problems with a submitted preferences form; empty means it is safe to save. (The service validates again;
    this lets the page say it nicely before calling it.)"""
    problems = []
    if bool(values.get("quiet_start")) != bool(values.get("quiet_end")):
        problems.append("Quiet hours need both a start and an end time (or neither).")
    if not 1 <= int(values.get("min_severity", 3)) <= 5:
        problems.append("Minimum severity must be between 1 and 5.")
    if values.get("use_conviction") and not 0 <= int(values.get("min_conviction", 6)) <= 10:
        problems.append("Minimum conviction must be between 0 and 10.")
    if values.get("content_level") not in ("BRIEF", "DETAILED"):
        problems.append("Choose brief or detailed messages.")
    if values.get("quiet_hours_mode") not in ("DEFER", "SUPPRESS"):
        problems.append("Choose what to do with alerts that arrive in quiet hours.")
    return problems


def summary_line(view: dict) -> str:
    """One short line for the Profile status strip: where alerts can actually reach you right now. A channel is "on" only if a
    verified contact exists AND at least one alert type is routed to it; a reachable channel with nothing routed says so."""
    routed = {ch: any(cells.get(ch, False) for cells in view["routing"].values()) for ch in ("EMAIL", "PUSH")}
    parts = []
    for ch, label in (("EMAIL", "Email"), ("PUSH", "Push")):
        mine = [c for c in view["contacts"] if c["channel"] == ch]
        if not mine:
            parts.append(f"{label} not set up")
        elif any(c["verified"] and not c["opted_out"] for c in mine):
            parts.append(f"{label} on" if routed[ch] else f"{label} ready, no alerts routed")
        elif any(c["verified"] for c in mine):
            parts.append(f"{label} opted out")
        else:
            parts.append(f"{label} not verified")
    return " · ".join(parts) + f" · severity ≥ {view['preferences']['min_severity_threshold']}"
