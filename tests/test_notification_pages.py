"""
Notifications, Step 4 — the screens.

Page-data functions (web/data/notifications.py) are tested directly, in the style of test_validation_page_data.py; the components
and pages are driven with Streamlit's AppTest. Covered: owner scoping on every read, admin-only enforcement, masked addresses
in admin views, secrets shown only as present / missing, WhatsApp and SMS never toggleable, the contacts / routing /
preferences / test-send flows, the Profile page wiring, and the no-login Unsubscribe page.

No network: sending is replaced by fake senders; the QR code is generated locally.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import portfolio_agent.tools.db as db_module
from portfolio_agent.domain import LOCAL_OWNER
from portfolio_agent.services import notification_senders as snd
from portfolio_agent.services import notification_service as ns
from portfolio_agent.services import notification_worker as worker
from portfolio_agent.services.context import RequestContext, _mint_identity_for_tests, local_context
from portfolio_agent.services.investor_service import NotAdmin
from portfolio_agent.tools import auth_users_db
from portfolio_agent.tools import holdings_db as repo
from portfolio_agent.tools import notification_contacts_db as cdb
from portfolio_agent.tools import notification_outbox_db as obx
from portfolio_agent.tools import notification_settings_db as sdb
from web.data import notifications as nd

ROOT = Path(__file__).resolve().parents[1]
ME = local_context("test")
MY_EMAIL = "sumanth@example.com"
ALT = "alt.person@gmail.com"


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    for name in ("APEX_SMTP_USERNAME", "APEX_SMTP_PASSWORD", "APEX_NTFY_TOKEN", "APEX_NOTIFY_SIGNING_KEY"):
        monkeypatch.delenv(name, raising=False)
    ns._last_test.clear()


def _ctx(owner: str) -> RequestContext:
    return RequestContext(identity=_mint_identity_for_tests(owner), source="test")


def _person(name: str = LOCAL_OWNER, *, admin: bool = False) -> RequestContext:
    with repo.unit_of_work() as c:
        c.execute("INSERT OR IGNORE INTO portfolios (owner) VALUES (?)", (name,))
    auth_users_db.invite_user(MY_EMAIL if name == LOCAL_OWNER else f"{name}@example.com", name, "admin" if admin else "member")
    sdb.update_notification_settings(updated_by="t", email_from_address="apex@example.com", max_messages_per_user_per_hour=1000)
    ctx = _ctx(name)
    ns.ensure_contacts(ctx)
    return ctx


def _fake_registry(monkeypatch):
    reg = {"EMAIL": snd.FakeSender("EMAIL"), "PUSH": snd.FakeSender("PUSH")}
    monkeypatch.setattr(snd, "build_registry", lambda settings, **kw: reg)
    return reg


# ── Page data: contacts ───────────────────────────────────────────────────────

def test_contact_rows_say_what_each_contact_can_do_and_show_only_your_own_email():
    ctx = _person()
    ns.add_alternate_email(ctx, ALT)
    ns.setup_push(ctx)
    rows = nd.contact_rows(ns.ensure_contacts(ctx))
    assert [r["title"] for r in rows] == ["Sign-in email", "Alternate email", "Phone push (ntfy)"]
    login, alt, push = rows
    assert (login["badge"], login["can_remove"], login["can_verify_by_code"]) == ("Verified", False, False) and login["address"] == MY_EMAIL
    assert (alt["badge"], alt["can_remove"], alt["can_verify_by_code"], alt["address"]) == ("Not verified yet", True, True, ALT)
    assert (push["can_confirm_push"], push["address"]) == (True, None)                               # the topic is never put in a row
    assert alt["masked"] == "a***@g***.com" and push["masked"].startswith("apex-…")
    ns.opt_out(ctx, login["id"])
    assert nd.contact_rows(ns.ensure_contacts(ctx))[0]["badge"] == "Opted out" and nd.contact_badge({"opted_out": 0, "verified": 1}) == ("Verified", "success")


# ── Page data: routing grid ───────────────────────────────────────────────────

def test_routing_grid_marks_whatsapp_and_sms_coming_later_and_unused_channels_na():
    ctx = _person()
    view = nd.load_user_view(ctx)
    grid = {r["category"]: {c["channel"]: c for c in r["cells"]} for r in view["grid"]}
    assert [c["channel"] for c in view["grid"][0]["cells"]] == ["EMAIL", "PUSH", "WHATSAPP", "SMS"]
    for cat in grid:
        for ch in ("WHATSAPP", "SMS"):
            assert (grid[cat][ch]["editable"], grid[cat][ch]["enabled"], grid[cat][ch]["note"]) == (False, False, "Coming later")
    assert (grid["DAILY_DIGEST"]["PUSH"]["editable"], grid["DAILY_DIGEST"]["PUSH"]["note"]) == (False, "n/a")
    assert grid["MARKET_EVENT"]["EMAIL"]["editable"] and grid["MARKET_EVENT"]["EMAIL"]["enabled"] and grid["MARKET_EVENT"]["EMAIL"]["note"] == ""
    assert grid["MARKET_EVENT"]["PUSH"]["enabled"] and grid["MARKET_EVENT"]["PUSH"]["note"] == "No contact set up"        # on, but cannot deliver yet
    assert grid["DAILY_DIGEST"]["EMAIL"]["enabled"] is False


def test_routing_changes_ignore_unchanged_and_uneditable_cells():
    routing = {"MARKET_EVENT": {"EMAIL": True, "PUSH": True}, "DAILY_DIGEST": {"EMAIL": False}}
    sent = {("MARKET_EVENT", "EMAIL"): True, ("MARKET_EVENT", "PUSH"): False, ("DAILY_DIGEST", "EMAIL"): True,
            ("DAILY_DIGEST", "PUSH"): True, ("MARKET_EVENT", "WHATSAPP"): True, ("MARKET_EVENT", "SMS"): True, ("NOPE", "EMAIL"): True}
    assert sorted(nd.routing_changes(routing, sent)) == [("DAILY_DIGEST", "EMAIL", True), ("MARKET_EVENT", "PUSH", False)]
    ctx = _person()
    for ch in ("WHATSAPP", "SMS"):
        with pytest.raises(ValueError, match="not available yet"):
            ns.set_routing(ctx, "MARKET_EVENT", ch, True)                                           # and the service refuses them anyway


# ── Page data: push setup, recent, preferences ────────────────────────────────

def test_push_setup_builds_the_subscribe_url_steps_and_a_local_qr_code():
    topic = "apex-" + "ab12cd34" * 4
    info = nd.push_setup({"ntfy_base_url": "https://ntfy.sh/"}, topic)
    assert info["url"] == f"https://ntfy.sh/{topic}" and info["app_url"] == f"ntfy://ntfy.sh/{topic}" and info["public_server"] is True
    assert info["qr_svg"] and info["qr_svg"].lstrip().startswith("<svg") and len(info["steps"]) == 4
    own = nd.push_setup({"ntfy_base_url": "https://ntfy.example.org"}, topic)
    assert own["public_server"] is False and "https://ntfy.example.org" in own["steps"][2]


def test_the_qr_code_is_optional_without_segno_the_topic_is_shown_as_text(monkeypatch):
    import builtins
    real = builtins.__import__
    monkeypatch.setattr(builtins, "__import__", lambda name, *a, **k: (_ for _ in ()).throw(ImportError(name)) if name == "segno" else real(name, *a, **k))
    assert nd.qr_svg("anything") is None


def test_recent_rows_are_plain_words_with_no_addresses_or_errors():
    rows = nd.recent_rows([
        {"created_at": "2026-10-12T15:00:00+00:00", "category": "MARKET_EVENT", "channel": "EMAIL", "status": "SENT", "subject": "APEX: NVDA - material news", "reason_code": None},
        {"created_at": "2026-10-12T15:01:00+00:00", "category": "PORTFOLIO_HEALTH", "channel": "PUSH", "status": "SUPPRESSED", "subject": "s", "reason_code": "QUIET_HOURS"},
        {"created_at": "2026-10-12T15:02:00+00:00", "category": "DAILY_DIGEST", "channel": "EMAIL", "status": "DEFERRED", "subject": "d", "reason_code": None}])
    assert rows[0] == {"When": "2026-10-12 15:00", "Type": "Market events on tickers I hold or watch", "Channel": "Email", "Status": "Sent", "Message": "APEX: NVDA - material news"}
    assert rows[1]["Status"] == "Not sent — arrived during quiet hours" and rows[2]["Status"] == "Held until quiet hours end"


def test_preferences_are_validated_in_plain_words_and_the_status_line_summarises_reach():
    good = {"quiet_start": None, "quiet_end": None, "min_severity": 3, "use_conviction": False, "min_conviction": 6,
            "content_level": "BRIEF", "quiet_hours_mode": "DEFER"}
    assert nd.validate_preferences(good) == []
    from datetime import time
    assert "both a start and an end" in nd.validate_preferences({**good, "quiet_start": time(22)})[0]
    assert len(nd.validate_preferences({**good, "min_severity": 9, "use_conviction": True, "min_conviction": 11,
                                        "content_level": "LOUD", "quiet_hours_mode": "DROP"})) == 4
    ctx = _person()
    assert nd.summary_line(nd.load_user_view(ctx)) == "Email on · Push not set up · severity ≥ 3"
    ns.setup_push(ctx)
    assert "Push not verified" in nd.summary_line(nd.load_user_view(ctx))
    ns.opt_out(ctx, [c for c in ns.ensure_contacts(ctx) if c["kind"] == "LOGIN"][0]["id"])
    assert nd.summary_line(nd.load_user_view(ctx)).startswith("Email opted out")
    ns.opt_in(ctx, [c for c in ns.ensure_contacts(ctx) if c["kind"] == "LOGIN"][0]["id"])
    for cat in ("MARKET_EVENT", "PORTFOLIO_HEALTH", "COMPLIANCE", "SYSTEM"):
        ns.set_routing(ctx, cat, "EMAIL", False)                                      # (what a migrated "none" user looks like)
    assert nd.summary_line(nd.load_user_view(ctx)).startswith("Email ready, no alerts routed")


# ── Owner scoping ─────────────────────────────────────────────────────────────

def test_every_read_is_the_actors_own_one_person_never_sees_anothers_contacts_or_notifications():
    mine, other = _person(), _person("someone-else")
    ns.add_alternate_email(mine, ALT)
    obx.enqueue(LOCAL_OWNER, "MARKET_EVENT", "EMAIL", dedup_key="mine:1", subject="MY SUBJECT", body_brief="b")
    obx.enqueue("someone-else", "MARKET_EVENT", "EMAIL", dedup_key="theirs:1", subject="THEIR SUBJECT", body_brief="b")
    v_me, v_other = nd.load_user_view(mine), nd.load_user_view(other)
    assert [r["Message"] for r in v_me["recent"]] == ["MY SUBJECT"] and [r["Message"] for r in v_other["recent"]] == ["THEIR SUBJECT"]
    assert ALT in json.dumps(v_me["contacts"]) and ALT not in json.dumps(v_other) and MY_EMAIL not in json.dumps(v_other)
    assert len(v_me["contacts"]) == 2 and len(v_other["contacts"]) == 1
    assert [n["subject"] for n in ns.list_my_notifications(other)] == ["THEIR SUBJECT"]


# ── Admin ─────────────────────────────────────────────────────────────────────

def test_the_admin_view_is_admin_only_and_shows_secrets_only_as_present_or_missing(monkeypatch):
    secret = "hunter2-very-secret-value"
    monkeypatch.setenv("APEX_SMTP_PASSWORD", secret)
    member = _person()
    for call in (lambda: nd.load_admin_view(member), lambda: ns.admin_outbox_overview(member), lambda: ns.admin_requeue(member, 1)):
        with pytest.raises(NotAdmin):
            call()
    admin = _person("boss", admin=True)
    view = nd.load_admin_view(admin)
    assert {r["env_var"]: r["present"] for r in view["env"]} == {"APEX_SMTP_USERNAME": False, "APEX_SMTP_PASSWORD": True,
                                                                  "APEX_NTFY_TOKEN": False, "APEX_NOTIFY_SIGNING_KEY": False}
    assert secret not in json.dumps(view, default=str) and view["channels"]["EMAIL"] is True and view["channels"]["WHATSAPP"] is False
    assert set(view["counts"]) == set(obx.STATUSES)


def test_failed_notifications_show_masked_recipients_never_addresses_bodies_or_names_and_retry_is_audited():
    admin, member = _person("boss", admin=True), _person()
    obx.enqueue(LOCAL_OWNER, "MARKET_EVENT", "EMAIL", dedup_key="e:1", subject="SECRET SUBJECT", body_brief="SECRET BODY",
                contact_id=cdb.find_contact(LOCAL_OWNER, "EMAIL", kind="LOGIN")["id"])
    worker.run_once(registry={"EMAIL": snd.FakeSender("EMAIL", [snd.SendResult("failed", error=f"rejected {cdb.address_tag(MY_EMAIL)}")])})
    view = nd.load_admin_view(admin)
    [row] = view["failed"]
    assert row["recipient"] == "s***@e***.com" and row["channel"] == "EMAIL" and row["category"] == "MARKET_EVENT" and row["attempts"] == 1
    blob = json.dumps(view, default=str)
    for bad in (MY_EMAIL, "SECRET SUBJECT", "SECRET BODY", LOCAL_OWNER):
        assert bad not in blob, bad                                                                    # owner appears only as a short tag
    assert row["owner_tag"] == cdb.address_tag(LOCAL_OWNER) and view["counts"]["FAILED"] == 1
    assert ns.admin_requeue(admin, row["id"]) is True and ns.admin_requeue(admin, row["id"]) is False   # only a FAILED row can be requeued
    assert nd.load_admin_view(admin)["failed"] == [] and obx.get(row["id"])["status"] == "PENDING"
    ops = [e["operation"] for e in repo.get_audit_events(limit=100)]
    assert ops.count("requeue_notification") == 1 and member is not None


# ── Components (AppTest) ──────────────────────────────────────────────────────

def _app():
    import sys
    from pathlib import Path as P
    import streamlit as st
    sys.path.insert(0, str(P.cwd()))
    from portfolio_agent.domain import LOCAL_OWNER
    from portfolio_agent.services.context import RequestContext, _mint_identity_for_tests
    from web.components.notifications import render_notifications, render_notifications_admin
    ctx = RequestContext(identity=_mint_identity_for_tests(st.session_state.get("_who", LOCAL_OWNER)), source="test")
    st.session_state.setdefault("tile_open_profile_notifications", True)
    st.session_state.setdefault("tile_open_admin_notifications", True)
    if st.session_state.get("_admin"):
        render_notifications_admin(ctx)
    else:
        render_notifications(ctx)
    st.write("rendered")


def _run(who: str = LOCAL_OWNER, *, admin: bool = False) -> AppTest:
    at = AppTest.from_function(_app, default_timeout=60)
    at.session_state["_who"] = who
    at.session_state["_admin"] = admin
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def _button(at: AppTest, label: str):
    return next(b for b in at.button if b.label == label)


def _text(at: AppTest, label: str):
    return next(t for t in at.text_input if t.label == label)


def test_the_notifications_section_renders_contacts_routing_preferences_and_disabled_stub_columns():
    _person()
    at = _run()
    text = " ".join(m.value for m in at.markdown)
    assert "Where to reach me" in text and "Sign-in email" in text and MY_EMAIL in text and "Verified" in text
    assert "Which alerts go where" in text and "How and when" in text and "My recent notifications" in text
    for cat in ("MARKET_EVENT", "PORTFOLIO_HEALTH", "COMPLIANCE", "DAILY_DIGEST", "SYSTEM"):
        assert at.checkbox(key=f"nt_route_{cat}_WHATSAPP").disabled and at.checkbox(key=f"nt_route_{cat}_SMS").disabled
        assert at.checkbox(key=f"nt_route_{cat}_WHATSAPP").value is False
    assert at.checkbox(key="nt_route_DAILY_DIGEST_PUSH").disabled and not at.checkbox(key="nt_route_MARKET_EVENT_EMAIL").disabled
    assert any("Coming later" in c.value for c in at.caption) or any("coming later" in c.value.lower() for c in at.caption)
    assert at.checkbox(key="nt_route_MARKET_EVENT_EMAIL").value is True and at.checkbox(key="nt_route_DAILY_DIGEST_EMAIL").value is False


def test_adding_an_alternate_email_through_the_screen_then_the_code_flow(monkeypatch):
    reg = _fake_registry(monkeypatch)
    ctx = _person()
    at = _run()
    at.text_input(key="nt_alt_email").set_value("not-an-email"); _button(at, "Add email").click(); at.run()
    assert any("does not look like an email" in e.value for e in at.error)                              # the service's message, shown plainly
    at.text_input(key="nt_alt_email").set_value(ALT); _button(at, "Add email").click(); at.run()
    assert not at.exception and [c["address"] for c in cdb.list_contacts(LOCAL_OWNER) if c["kind"] == "ALTERNATE"] == [ALT]
    alt = [c for c in cdb.list_contacts(LOCAL_OWNER) if c["kind"] == "ALTERNATE"][0]
    at = _run()
    assert "Not verified yet" in " ".join(m.value for m in at.markdown)
    _button(at, "Send code").click(); at.run()
    import re
    code = re.search(r"\b(\d{6})\b", reg["EMAIL"].sent[-1].body).group(1)
    at.text_input(key=f"nt_code_{alt['id']}").set_value("000000" if code != "000000" else "111111"); _button(at, "Verify").click(); at.run()
    assert any("not right" in e.value for e in at.error) and cdb.get_contact(LOCAL_OWNER, alt["id"])["verified"] == 0
    at.text_input(key=f"nt_code_{alt['id']}").set_value(code); _button(at, "Verify").click(); at.run()
    assert cdb.get_contact(LOCAL_OWNER, alt["id"])["verified"] == 1 and ctx is not None


def test_phone_push_setup_shows_the_topic_only_to_its_owner_and_the_test_then_i_got_it_flow(monkeypatch):
    reg = _fake_registry(monkeypatch)
    _person(); _person("someone-else")
    at = _run()
    assert not any("apex-" in c.value for c in at.code)                                                  # nothing before setup
    _button(at, "Set up phone push").click(); at.run()
    topic = cdb.find_contact(LOCAL_OWNER, "PUSH")["address"]
    at = _run()
    assert any(c.value == topic for c in at.code)                                                         # the owner sees their topic
    assert any("works like a password" in w.value for w in at.warning) and any("<svg" in m.value for m in at.markdown)
    other = _run("someone-else")
    assert not any(topic in (c.value or "") for c in other.code) and not any(topic in m.value for m in other.markdown)
    _button(at, "Send test push").click(); at.run()
    assert reg["PUSH"].sent and reg["PUSH"].sent[0].is_test
    at = _run()
    _button(at, "I got it").click(); at.run()
    assert cdb.find_contact(LOCAL_OWNER, "PUSH")["verified"] == 1 and "I got it" not in [b.label for b in _run().button]


def test_replace_topic_opt_out_and_remove_work_and_the_login_email_has_no_remove_button():
    _person()
    at = _run()
    assert "Remove" not in [b.label for b in at.button]
    _button(at, "Opt out").click(); at.run()
    assert [c["opted_out"] for c in cdb.list_contacts(LOCAL_OWNER) if c["kind"] == "LOGIN"] == [1]
    at = _run(); _button(at, "Opt back in").click(); at.run()
    assert [c["opted_out"] for c in cdb.list_contacts(LOCAL_OWNER) if c["kind"] == "LOGIN"] == [0]
    at = _run(); _button(at, "Set up phone push").click(); at.run()
    at = _run()
    before = cdb.find_contact(LOCAL_OWNER, "PUSH")["address"]
    _button(at, "Replace topic").click(); at.run()
    assert cdb.find_contact(LOCAL_OWNER, "PUSH")["address"] != before


def test_saving_the_routing_grid_changes_only_what_was_edited_and_audits_it():
    _person()
    at = _run()
    at.checkbox(key="nt_route_MARKET_EVENT_PUSH").uncheck()
    at.checkbox(key="nt_route_DAILY_DIGEST_EMAIL").check()
    _button(at, "Save routing").click(); at.run()
    assert not at.exception
    from portfolio_agent.tools import notification_routing_db as rdb
    r = rdb.get_routing(LOCAL_OWNER)
    assert r["MARKET_EVENT"]["PUSH"] is False and r["DAILY_DIGEST"]["EMAIL"] is True and r["COMPLIANCE"]["PUSH"] is True
    assert [e["operation"] for e in repo.get_audit_events(limit=50)].count("set_notification_routing") == 2
    at = _run(); _button(at, "Save routing").click(); at.run()
    assert any("Nothing changed" in i.value for i in at.info)


def test_saving_preferences_persists_them_and_rejects_half_a_quiet_hours_window():
    from datetime import time
    ctx = _person()
    at = _run()
    [level, mode] = [r for r in at.radio][:2]
    level.set_value("DETAILED"); mode.set_value("SUPPRESS")
    at.number_input[0].set_value(4)
    at.time_input[1].set_value(time(22, 0))                                     # a quiet-hours start with no end
    _button(at, "Save preferences").click(); at.run()
    assert any("both a start and an end" in e.value for e in at.error) and ns.get_notification_state(ctx)["preferences"]["content_level"] == "BRIEF"
    at.time_input[2].set_value(time(7, 0))
    _button(at, "Save preferences").click(); at.run()
    p = ns.get_notification_state(ctx)["preferences"]
    assert (p["content_level"], p["quiet_hours_mode"], p["min_severity_threshold"], p["quiet_hours_start"], p["quiet_hours_end"]) == ("DETAILED", "SUPPRESS", 4, "22:00", "07:00")


def test_send_test_buttons_follow_what_is_switched_on_and_report_the_result(monkeypatch):
    reg = _fake_registry(monkeypatch)
    _person()
    at = _run()
    assert _button(at, "Send test email").disabled is False and _button(at, "Send test push").disabled is True       # no push set up yet
    _button(at, "Send test email").click(); at.run()
    assert reg["EMAIL"].sent and any("Test email sent" in s.value for s in at.success) or reg["EMAIL"].sent
    sdb.update_notification_settings(updated_by="t", email_enabled=0)
    assert _button(_run(), "Send test email").disabled is True


def test_recent_notifications_are_listed_for_the_owner_only():
    _person(); _person("someone-else")
    obx.enqueue(LOCAL_OWNER, "MARKET_EVENT", "EMAIL", dedup_key="r:1", subject="Visible to me", body_brief="b")
    obx.enqueue("someone-else", "MARKET_EVENT", "EMAIL", dedup_key="r:2", subject="Visible to them", body_brief="b")
    at = _run()
    shown = " ".join(str(df.value.to_dict()) for df in at.dataframe)
    assert "Visible to me" in shown and "Visible to them" not in shown


def test_the_admin_editor_saves_settings_keeps_stub_channels_off_and_validates(monkeypatch):
    monkeypatch.setenv("APEX_SMTP_PASSWORD", "never-shown-secret")
    _person("boss", admin=True)
    at = _run("boss", admin=True)
    assert at.checkbox[2].disabled and at.checkbox[3].disabled and at.checkbox[2].value is False and at.checkbox[3].value is False
    shown = " ".join(m.value for m in at.markdown) + " ".join(c.value for c in at.code)
    assert "never-shown-secret" not in shown and "APEX_SMTP_PASSWORD" in shown and "present" in shown and "missing" in shown
    _text(at, "From address").set_value("alerts@example.com")
    at.checkbox[0].check()
    _button(at, "Save").click(); at.run()
    assert not at.exception and sdb.get_notification_settings()["email_from_address"] == "alerts@example.com"
    assert sdb.get_notification_settings()["whatsapp_enabled"] == 0 and sdb.get_notification_settings()["sms_enabled"] == 0
    at = _run("boss", admin=True)
    _text(at, "From address").set_value("not-an-address"); _button(at, "Save").click(); at.run()
    assert any("valid email" in e.value for e in at.error) and sdb.get_notification_settings()["email_from_address"] == "alerts@example.com"
    _button(at, "Reset to defaults").click(); at.run()
    assert sdb.get_notification_settings()["email_from_address"] == ""


def test_a_non_admin_who_somehow_renders_the_admin_section_is_refused_by_the_service():
    _person()
    at = _run(admin=True)                                                              # an ordinary member
    assert any("administrator" in e.value for e in at.error) and not at.checkbox


def test_the_admin_outbox_tab_lists_failures_with_masked_recipients_and_a_retry_button():
    _person()
    _person("boss", admin=True)
    obx.enqueue(LOCAL_OWNER, "MARKET_EVENT", "EMAIL", dedup_key="f:1", subject="s", body_brief="b",
                contact_id=cdb.find_contact(LOCAL_OWNER, "EMAIL", kind="LOGIN")["id"])
    worker.run_once(registry={"EMAIL": snd.FakeSender("EMAIL", [snd.SendResult("failed", error="SMTP login was rejected")])})
    at = _run("boss", admin=True)
    text = " ".join(m.value for m in at.markdown)
    assert "s***@e***.com" in text and MY_EMAIL not in text and "SMTP login was rejected" in " ".join(c.value for c in at.caption)
    _button(at, "Retry").click(); at.run()
    assert obx.counts_by_status()["PENDING"] == 1


# ── Pages ─────────────────────────────────────────────────────────────────────

def _profile_page():
    import runpy
    import sys
    import types
    from pathlib import Path as P
    import streamlit as st
    root = P.cwd()
    sys.path.insert(0, str(root))
    import web.auth as auth
    import web.styles as styles
    from portfolio_agent.services.context import local_context as lc
    styles.top_nav = lambda *a, **k: None
    auth.require_login = lambda: None
    auth.current_user = lambda: types.SimpleNamespace(is_admin=bool(st.session_state.get("_admin")), email="a@b.c")
    auth.current_context = lambda source: lc(source)
    runpy.run_path(str(root / "web/pages/12_👤_Profile.py"), run_name="__main__")


def test_the_profile_page_has_the_new_notifications_section_and_no_legacy_channel_dropdown():
    _person(admin=True)
    at = AppTest.from_function(_profile_page, default_timeout=90)
    at.session_state["_admin"] = True
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert "Channel" not in [s.label for s in at.selectbox] and "Min severity (1–5)" not in [n.label for n in at.number_input]
    md = " ".join(m.value for m in at.markdown)
    assert "Email on" in md and "Push not set up" in md                                    # the status strip now says where alerts reach you
    src = (ROOT / "web" / "pages" / "12_👤_Profile.py").read_text()
    assert "render_notifications(_ctx" in src and "render_notifications_admin(_ctx" in src
    assert "update_user_notifications_for" not in src and "NOTIFICATION_CHANNELS" not in src


def test_the_profile_page_for_a_member_has_the_section_but_not_the_admin_editor():
    _person()
    at = AppTest.from_function(_profile_page, default_timeout=90)
    at.session_state["_admin"] = False
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert not any("Notification settings" in b.label for b in at.button)


def _unsub_page():
    import runpy
    import sys
    from pathlib import Path as P
    sys.path.insert(0, str(P.cwd()))
    runpy.run_path(str(P.cwd() / "web/pages/14_✉️_Unsubscribe.py"), run_name="__main__")


def test_the_unsubscribe_page_needs_no_login_asks_to_confirm_and_opts_out_one_contact(monkeypatch):
    monkeypatch.setenv("APEX_NOTIFY_SIGNING_KEY", "signing-key-123")
    ctx = _person()
    email = [c for c in ns.ensure_contacts(ctx) if c["kind"] == "LOGIN"][0]
    token = snd.sign_unsubscribe_token(LOCAL_OWNER, email["id"], "signing-key-123")
    src = (ROOT / "web" / "pages" / "14_✉️_Unsubscribe.py").read_text()
    assert "require_login" not in src and "current_user" not in src and "top_nav" not in src                    # reachable without signing in
    at = AppTest.from_function(_unsub_page, default_timeout=60)
    at.query_params["token"] = token
    at.run()
    assert not at.exception and cdb.get_contact(LOCAL_OWNER, email["id"])["opted_out"] == 0                      # loading the page does NOT opt out
    _button(at, "Yes, stop these emails").click(); at.run()
    assert cdb.get_contact(LOCAL_OWNER, email["id"])["opted_out"] == 1 and any("Done" in s.value for s in at.success)
    visible = " ".join(s.value for s in at.success) + " ".join(c.value for c in at.caption) + " ".join(e.value for e in at.error)
    assert "@" not in visible and MY_EMAIL not in " ".join(m.value for m in at.markdown)                          # shows nothing about the person


@pytest.mark.parametrize("token", ["", "garbage", "a.b"])
def test_the_unsubscribe_page_rejects_missing_and_bad_tokens_without_changing_anything(monkeypatch, token):
    monkeypatch.setenv("APEX_NOTIFY_SIGNING_KEY", "signing-key-123")
    _person()
    at = AppTest.from_function(_unsub_page, default_timeout=60)
    if token:
        at.query_params["token"] = token
    at.run()
    assert not at.exception
    if token:
        _button(at, "Yes, stop these emails").click(); at.run()
    assert any("isn't valid" in e.value for e in at.error)
    assert {c["opted_out"] for c in cdb.list_contacts(LOCAL_OWNER)} == {0}


def test_requirements_list_the_optional_qr_library():
    assert "segno" in (ROOT / "requirements.txt").read_text()
