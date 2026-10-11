"""
Notifications UI — the person's Notifications section of the Profile page, and the administrator's notification settings.

Layout only. Every rule — what may be edited, what each cell means, who may see what, what is validated — lives in
web.data.notifications (shaping) and portfolio_agent.services.notification_service (the only writer, audited, owner-scoped,
admin-checked server-side). Nothing here decides who is notified or sends anything; "Send test" asks the service, which
sends directly and tells the person plainly whether it worked.

For the person: contacts (the verified sign-in email, ONE alternate email verified by a code, phone push with its private
topic, QR code and test), the routing grid (Email and Push live; WhatsApp and SMS shown disabled, "Coming later"), content
level, quiet hours and what to do with them, digest time, the severity and conviction thresholds, and their own recent
notifications. For the administrator: the global settings, each secret environment variable as present / missing (never its
value), and the outbox with failed messages shown with MASKED recipients and a retry button.
"""
from __future__ import annotations

from datetime import datetime, time

import pandas as pd
import streamlit as st

from portfolio_agent.services import notification_service as ns
from portfolio_agent.services.investor_service import NotAdmin
from portfolio_agent.tools import notification_contacts_db as cdb
from portfolio_agent.tools import notification_settings_db as sdb
from portfolio_agent.tools.investor_settings_db import SettingsVersionConflict
from web.data import notifications as nd
from web.styles import NEUTRAL, NEUTRAL_LIGHT, PRIMARY, SUCCESS, SUCCESS_LIGHT, WARNING, WARNING_LIGHT, badge_html, material, section_tile

_TONE = {"success": (SUCCESS, SUCCESS_LIGHT), "warning": (WARNING, WARNING_LIGHT), "neutral": (NEUTRAL, NEUTRAL_LIGHT)}
_PERSON_ERRORS = (ValueError, PermissionError)               # ContactError, VerificationError, validation, NotAuthorized


def _flash(msg: str) -> None:
    st.session_state["profile_flash"] = msg
    st.rerun()


def _call(fn, ok_message: str | None = None) -> bool:
    """Run one service call; show its (safe) error message, or flash success and rerun. Returns True on success."""
    try:
        fn()
    except _PERSON_ERRORS as exc:
        st.error(str(exc), icon=material("error"))
        return False
    if ok_message:
        _flash(ok_message)
    return True


def _parse_time(raw: str | None) -> time | None:
    try:
        hh, mm = str(raw).split(":")[:2]
        return time(int(hh), int(mm))
    except Exception:
        return None


def _badge(label: str, tone: str) -> str:
    color, bg = _TONE[tone]
    return badge_html(label, color, bg)


# ═════════════════════════════════════════════════════════════════════════════
# The person's section
# ═════════════════════════════════════════════════════════════════════════════

def render_notifications(ctx, view: dict | None = None) -> None:
    """The Notifications section. `view` is nd.load_user_view(ctx) when the page already loaded it."""
    view = view or nd.load_user_view(ctx)
    tile = section_tile("Notifications", badge_text=nd.summary_line(view), icon="notifications", expanded=False,
                        key="profile_notifications")
    if not tile:
        return
    with tile:
        st.caption("Choose where alerts reach you and which kinds you want. Alerts are informational — they never tell you to "
                   "trade — and phone push is always the short form with no amounts.")
        _contacts(ctx, view)
        _routing(ctx, view)
        _preferences(ctx, view)
        _tests(ctx, view)
        _recent(view)


def _contacts(ctx, view: dict) -> None:
    st.markdown("**Where to reach me**")
    for row in view["contacts"]:
        if row["kind"] == "TOPIC":
            continue                                                   # phone push has its own block below
        c1, c2, c3 = st.columns([3, 1.6, 2.4], vertical_alignment="center")
        c1.markdown(f"**{row['title']}** · {row['address']}")
        c2.markdown(_badge(row["badge"], row["tone"]), unsafe_allow_html=True)
        with c3:
            b1, b2 = st.columns(2)
            if row["opted_out"]:
                if b1.button("Opt back in", key=f"nt_optin_{row['id']}"):
                    _call(lambda r=row: ns.opt_in(ctx, r["id"]), "Opted back in.")
            elif b1.button("Opt out", key=f"nt_optout_{row['id']}", help="Stop messages to this address (you can opt back in)."):
                _call(lambda r=row: ns.opt_out(ctx, r["id"]), "Opted out.")
            if row["can_remove"] and b2.button("Remove", key=f"nt_remove_{row['id']}"):
                _call(lambda r=row: ns.remove_contact(ctx, r["id"]), "Removed.")
        if row["can_verify_by_code"]:
            v1, v2, v3 = st.columns([2, 1.4, 1.4], vertical_alignment="bottom")
            code = v1.text_input("6-digit code from the email we sent", key=f"nt_code_{row['id']}", max_chars=6)
            if v2.button("Send code", key=f"nt_sendcode_{row['id']}", help="Emails a code to this address (one per minute)."):
                if _call(lambda r=row: ns.start_email_verification(ctx, r["id"])):
                    st.success("Code sent. It is valid for a few minutes.", icon=material("mail"))
            if v3.button("Verify", key=f"nt_verify_{row['id']}", type="primary"):
                _call(lambda r=row, c=code: ns.confirm_email_code(ctx, r["id"], c), "Address verified.")
    if not any(r["kind"] == "ALTERNATE" for r in view["contacts"]):
        a1, a2 = st.columns([3, 1.4], vertical_alignment="bottom")
        alt = a1.text_input("Add one alternate email", key="nt_alt_email", placeholder="name@example.com",
                            help="You can have one extra address. It receives nothing until you verify it with a code.")
        if a2.button("Add email", key="nt_add_alt"):
            _call(lambda: ns.add_alternate_email(ctx, alt), "Alternate email added — now verify it.")
    _push_block(ctx, view)


def _push_block(ctx, view: dict) -> None:
    push_row = next((r for r in view["contacts"] if r["kind"] == "TOPIC"), None)
    st.markdown("**Phone push**")
    if push_row is None:
        st.caption("Get short alerts on your phone through the free ntfy app.")
        if st.button("Set up phone push", key="nt_push_setup", icon=material("smartphone")):
            _call(lambda: ns.setup_push(ctx), "Phone push set up — follow the steps below.")
        return
    push = view["push"]
    st.markdown(f"{_badge(push_row['badge'], push_row['tone'])}", unsafe_allow_html=True)
    with st.expander("Show my topic and setup steps", expanded=not push_row["verified"]):
        st.markdown("\n".join(f"{i}. {s}" for i, s in enumerate(push["steps"], 1)))
        st.code(push["topic"], language=None)
        if push["qr_svg"]:
            st.markdown(f'<div style="width:180px;margin:6px 0">{push["qr_svg"]}</div>', unsafe_allow_html=True)
        st.warning(
            "Your topic works like a password: anyone who knows it can read your pushes"
            + (" (this is the public ntfy.sh server)" if push["public_server"] else "")
            + ". Don't share it. Push messages never contain amounts or position sizes.", icon=material("lock"))
    p1, p2, p3 = st.columns(3)
    if not push_row["verified"] and p2.button("I got it", key="nt_push_confirm", type="primary",
                                              help="Press after the test notification appears on your phone."):
        _call(lambda: ns.confirm_push_received(ctx), "Phone push verified.")
    if push_row["opted_out"]:
        if p1.button("Opt back in", key=f"nt_optin_{push_row['id']}"):
            _call(lambda: ns.opt_in(ctx, push_row["id"]), "Opted back in.")
    elif p1.button("Opt out", key=f"nt_optout_{push_row['id']}"):
        _call(lambda: ns.opt_out(ctx, push_row["id"]), "Opted out.")
    if p3.button("Replace topic", key="nt_push_regen", help="Generates a new topic; the old one stops working. Use it if the topic leaked."):
        _call(lambda: ns.regenerate_push_topic(ctx), "New topic created — subscribe to it and verify again.")


def _routing(ctx, view: dict) -> None:
    st.markdown("**Which alerts go where**")
    grid = view["grid"]
    submitted: dict[tuple[str, str], bool] = {}
    with st.form("nt_routing"):
        head = st.columns([3.2, 1, 1, 1, 1])
        head[0].caption("Alert type")
        for col, cell in zip(head[1:], grid[0]["cells"]):
            col.caption(cell["label"] + (" · coming later" if cell["note"] == "Coming later" else ""))
        for row in grid:
            cols = st.columns([3.2, 1, 1, 1, 1], vertical_alignment="center")
            cols[0].write(row["label"])
            for col, cell in zip(cols[1:], row["cells"]):
                submitted[(row["category"], cell["channel"])] = col.checkbox(
                    f"{row['label']} — {cell['label']}", value=cell["enabled"], disabled=not cell["editable"],
                    key=f"nt_route_{row['category']}_{cell['channel']}", label_visibility="collapsed",
                    help=cell["note"] or None)
        go = st.form_submit_button("Save routing", type="primary")
    notes = [f"{row['label']} — {c['label']}: {c['note']}" for row in grid for c in row["cells"]
             if c["editable"] and c["enabled"] and c["note"]]
    if notes:
        with st.expander("Why some switched-on alerts won't arrive yet"):
            st.markdown("\n".join(f"- {n}" for n in notes))
    if go:
        changes = nd.routing_changes(view["routing"], submitted)
        if not changes:
            st.info("Nothing changed.", icon=material("info"))
        elif _call(lambda: [ns.set_routing(ctx, cat, ch, on) for cat, ch, on in changes]):
            _flash("Routing saved.")


def _preferences(ctx, view: dict) -> None:
    st.markdown("**How and when**")
    p = view["preferences"]
    with st.form("nt_prefs"):
        k1, k2 = st.columns(2)
        level = k1.radio("Message detail", ["BRIEF", "DETAILED"], index=0 if p["content_level"] == "BRIEF" else 1, horizontal=True,
                         format_func=lambda v: {"BRIEF": "Brief (no amounts)", "DETAILED": "Detailed (email only)"}[v],
                         help="Phone push is always brief. Detailed email may include amounts and account names.")
        digest = k2.time_input("Daily digest time", value=_parse_time(p["digest_time"]) or time(7, 30), step=900,
                               help="In your profile timezone. The digest is an email and is off until you switch it on above.")
        q1, q2, q3 = st.columns([1.3, 1.3, 2])
        qs = q1.time_input("Quiet hours start", value=_parse_time(p["quiet_hours_start"]), step=900)
        qe = q2.time_input("Quiet hours end", value=_parse_time(p["quiet_hours_end"]), step=900,
                           help="A start later than the end spans midnight (e.g. 22:00 → 07:00).")
        mode = q3.radio("Alerts that arrive in quiet hours", ["DEFER", "SUPPRESS"], index=0 if p["quiet_hours_mode"] == "DEFER" else 1,
                        horizontal=True, format_func=lambda v: {"DEFER": "Hold until they end", "SUPPRESS": "Drop them"}[v])
        s1, s2, s3 = st.columns([1.3, 1.3, 2])
        sev = s1.number_input("Minimum severity (1–5)", 1, 5, int(p["min_severity_threshold"]), 1)
        use_conv = s2.checkbox("Apply a conviction floor", value=p["min_conviction_threshold"] is not None)
        conv = s3.number_input("Minimum conviction (0–10)", 0, 10, int(p["min_conviction_threshold"] if p["min_conviction_threshold"] is not None else 6), 1,
                               help="Only enforced when a conviction is known and the box is ticked.")
        go = st.form_submit_button("Save preferences", type="primary")
    if go:
        values = {"quiet_start": qs, "quiet_end": qe, "min_severity": sev, "use_conviction": use_conv, "min_conviction": conv,
                  "content_level": level, "quiet_hours_mode": mode}
        problems = nd.validate_preferences(values)
        for msg in problems:
            st.error(msg, icon=material("error"))
        if not problems:
            _call(lambda: ns.update_preferences(
                ctx, content_level=level, quiet_hours_mode=mode, digest_time=digest.strftime("%H:%M"),
                quiet_hours_start=qs.strftime("%H:%M") if qs else None, quiet_hours_end=qe.strftime("%H:%M") if qe else None,
                min_severity_threshold=int(sev), min_conviction_threshold=int(conv) if use_conv else None), "Preferences saved.")


def _tests(ctx, view: dict) -> None:
    st.markdown("**Send a test**")
    t1, t2, t3 = st.columns([1.4, 1.4, 3])
    if t1.button("Send test email", key="nt_test_email", disabled=not view["can_test"]["EMAIL"],
                 help=None if view["can_test"]["EMAIL"] else "Email isn't switched on yet — ask an administrator."):
        if _call(lambda: ns.send_test(ctx, "EMAIL")):
            st.success("Test email sent.", icon=material("mail"))
    has_push = any(r["kind"] == "TOPIC" for r in view["contacts"])
    if t2.button("Send test push", key="nt_test_push", disabled=not (view["can_test"]["PUSH"] and has_push),
                 help=None if has_push else "Set up phone push first."):
        if _call(lambda: ns.send_test(ctx, "PUSH")):
            st.success("Test push sent — press “I got it” once it arrives.", icon=material("smartphone"))
    t3.caption("WhatsApp and SMS are not available yet.")


def _recent(view: dict) -> None:
    st.markdown("**My recent notifications**")
    rows = view["recent"]
    if not rows:
        st.caption("Nothing yet. Alerts you are sent, held or skipped will be listed here.")
        return
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")


# ═════════════════════════════════════════════════════════════════════════════
# The administrator's section
# ═════════════════════════════════════════════════════════════════════════════

def _admin_call(fn, ok_message: str) -> None:
    try:
        fn()
    except (ValueError, RuntimeError, NotAdmin, SettingsVersionConflict) as exc:
        st.error(str(exc), icon=material("error"))
    else:
        _flash(ok_message)


def render_notifications_admin(ctx) -> None:
    """Admin-only: notification settings, secret env var status, and the outbox. Every write goes through
    notification_service, which re-checks admin server-side — hiding this from non-admins is only a courtesy."""
    tile = section_tile("Notification settings", badge_text="admin", badge_color=PRIMARY, expanded=False, key="admin_notifications")
    if not tile:
        return
    with tile:
        try:
            view = nd.load_admin_view(ctx)
        except NotAdmin as exc:
            st.error(str(exc), icon=material("error"))
            return
        t_set, t_env, t_out = st.tabs(["Channels & servers", "Environment", "Outbox"])
        with t_set:
            _admin_settings(ctx, view)
        with t_env:
            _admin_env(view)
        with t_out:
            _admin_outbox(ctx, view)


def _admin_settings(ctx, view: dict) -> None:
    s = view["settings"]
    live = [CH for CH, on in view["channels"].items() if on]
    st.markdown("Can deliver right now: " + (" ".join(_badge(nd.CHANNEL_LABELS.get(c, c), "success") for c in live) or _badge("nothing", "warning")),
                unsafe_allow_html=True)
    with st.form("admin_notification_settings"):
        c1, c2, c3, c4 = st.columns(4)
        email_on = c1.checkbox("Email on", value=bool(s["email_enabled"]))
        push_on = c2.checkbox("Phone push on", value=bool(s["push_enabled"]))
        c3.checkbox("WhatsApp on", value=False, disabled=True, help="Coming later: there is no WhatsApp sender yet.")
        c4.checkbox("SMS on", value=False, disabled=True, help="Coming later: there is no SMS sender yet.")
        e1, e2, e3 = st.columns([2, 1, 1])
        host = e1.text_input("SMTP host", s["smtp_host"])
        port = e2.number_input("SMTP port", 1, 65535, int(s["smtp_port"]))
        tls = e3.checkbox("STARTTLS", value=bool(s["smtp_use_tls"]))
        f1, f2 = st.columns(2)
        from_addr = f1.text_input("From address", s["email_from_address"], help="For Gmail, the same address as the SMTP username.")
        from_name = f2.text_input("From name", s["email_from_name"])
        g1, g2, g3 = st.columns(3)
        user_env = g1.text_input("SMTP username env var NAME", s["smtp_username_env"], help="The NAME of an environment variable, never the value.")
        pw_env = g2.text_input("SMTP password env var NAME", s["smtp_password_env"])
        sign_env = g3.text_input("Signing key env var NAME", s["unsubscribe_signing_key_env"])
        h1, h2 = st.columns(2)
        ntfy_url = h1.text_input("ntfy server URL", s["ntfy_base_url"])
        ntfy_env = h2.text_input("ntfy token env var NAME (optional)", s["ntfy_token_env"])
        app_url = st.text_input("App URL (used in links)", s["app_base_url"], placeholder="https://apex.example.com")
        i1, i2, i3, i4, i5 = st.columns(5)
        per_hour = i1.number_input("Max messages per person per hour", 1, 1000, int(s["max_messages_per_user_per_hour"]))
        retries = i2.number_input("Retry attempts", 1, 20, int(s["retry_max_attempts"]))
        backoff = i3.number_input("Retry backoff (seconds)", 1, 86400, int(s["retry_backoff_seconds"]))
        ttl = i4.number_input("Code lifetime (minutes)", 1, 1440, int(s["verification_code_ttl_minutes"]))
        tries = i5.number_input("Code attempts", 1, 20, int(s["verification_max_attempts"]))
        dry = st.checkbox("Dry run — log what would be sent, send nothing", value=bool(s["dry_run"]))
        st.caption(f"Version {s['version']} · updated {(s['updated_at'] or '')[:16].replace('T', ' ')}"
                   + (f" by {s['updated_by']}" if s.get("updated_by") else ""))
        b1, b2 = st.columns(2)
        save = b1.form_submit_button("Save", type="primary")
        reset = b2.form_submit_button("Reset to defaults")
    if save or reset:
        _admin_call(lambda: ns.reset_settings(ctx) if reset else ns.update_settings(
            ctx, expected_version=s["version"], email_enabled=email_on, push_enabled=push_on, smtp_host=host, smtp_port=int(port),
            smtp_use_tls=tls, email_from_address=from_addr, email_from_name=from_name, smtp_username_env=user_env,
            smtp_password_env=pw_env, unsubscribe_signing_key_env=sign_env, ntfy_base_url=ntfy_url, ntfy_token_env=ntfy_env,
            app_base_url=app_url, max_messages_per_user_per_hour=int(per_hour), retry_max_attempts=int(retries),
            retry_backoff_seconds=int(backoff), verification_code_ttl_minutes=int(ttl), verification_max_attempts=int(tries),
            dry_run=dry), "Notification settings " + ("reset." if reset else "saved."))


def _admin_env(view: dict) -> None:
    st.caption("Secrets come only from environment variables on the server. This shows whether each one is set — never its value.")
    for row in view["env"]:
        c1, c2, c3 = st.columns([3, 3, 1.4], vertical_alignment="center")
        c1.markdown(f"**{row['label']}**")
        c2.code(row["env_var"], language=None)
        c3.markdown(_badge("present", "success") if row["present"] else _badge("missing", "warning" if row["required"] else "neutral"),
                    unsafe_allow_html=True)


def _admin_outbox(ctx, view: dict) -> None:
    counts = view["counts"]
    cols = st.columns(len(counts))
    for col, (status, n) in zip(cols, counts.items()):
        col.metric(nd.STATUS_LABELS.get(status, status), n)
    failed = view["failed"]
    if not failed:
        st.caption("No failed notifications.")
        return
    st.markdown("**Recent failures** (recipients are masked)")
    for row in failed:
        c1, c2, c3 = st.columns([5, 3, 1.2], vertical_alignment="center")
        c1.markdown(f"#{row['id']} · {nd.CATEGORY_LABELS.get(row['category'], row['category'])} · {nd.CHANNEL_LABELS.get(row['channel'], row['channel'])} "
                    f"→ `{row['recipient']}` · {row['attempts']} attempt(s)")
        c2.caption(row["error"] or "no detail")
        if c3.button("Retry", key=f"nt_retry_{row['id']}"):
            _admin_call(lambda r=row: ns.admin_requeue(ctx, r["id"]), "Queued for another try.")
