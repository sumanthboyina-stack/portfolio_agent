"""
Notifications, Step 1 — data model: channels, contacts, routing, global settings.

Nothing here sends anything. Covered: the global settings row (seeds, versioning, validation, WHATSAPP / SMS can never be on,
admin-only, secrets never shown), contacts (sign-in email verified automatically, ONE alternate, unguessable push topic,
unavailable channels refused, addresses never logged or audited raw, owner scoping), routing (defaults for a new person, the
five conditions every delivery needs), the new user_notifications columns, and the one-time migration of the old single
`channel` setting on an existing database.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.domain import ACTIVE_CHANNELS, ALERT_CATEGORIES, CHANNELS, LOCAL_OWNER, Channel
from portfolio_agent.services import investor_service as inv
from portfolio_agent.services import notification_service as ns
from portfolio_agent.services.context import RequestContext, _mint_identity_for_tests, local_context
from portfolio_agent.tools import auth_users_db
from portfolio_agent.tools import holdings_db as repo
from portfolio_agent.tools import notification_contacts_db as cdb
from portfolio_agent.tools import notification_routing_db as rdb
from portfolio_agent.tools import notification_settings_db as sdb
from portfolio_agent.tools import user_notifications_db as udb
from portfolio_agent.tools.investor_settings_db import SettingsVersionConflict

ME = local_context("test")                                        # LOCAL_OWNER
MY_EMAIL = "sumanth@example.com"
RAW_ALT = "alt.person@gmail.com"


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    for name in ("APEX_SMTP_USERNAME", "APEX_SMTP_PASSWORD", "APEX_NTFY_TOKEN", "APEX_NOTIFY_SIGNING_KEY"):
        monkeypatch.delenv(name, raising=False)


def _ctx(owner: str) -> RequestContext:
    return RequestContext(identity=_mint_identity_for_tests(owner), source="test")


def _invite(owner: str = LOCAL_OWNER, email: str = MY_EMAIL, role: str = "member") -> None:
    auth_users_db.invite_user(email, owner, role)


def _make_admin() -> None:
    _invite(role="admin")


def _ops(**kw) -> list[str]:
    return [e["operation"] for e in repo.get_audit_events(limit=500, **kw)]


def _audit_text() -> str:
    return json.dumps(repo.get_audit_events(limit=500), default=str)


def _email_ready(**extra) -> None:
    sdb.update_notification_settings(updated_by="t", email_from_address="apex@example.com", **extra)


# ── Global settings ───────────────────────────────────────────────────────────

def test_settings_are_seeded_exactly_as_specified_and_versioned():
    s = sdb.get_notification_settings()
    expected = {
        "email_enabled": 1, "push_enabled": 1, "whatsapp_enabled": 0, "sms_enabled": 0, "smtp_host": "smtp.gmail.com",
        "smtp_port": 587, "smtp_use_tls": 1, "smtp_username_env": "APEX_SMTP_USERNAME", "smtp_password_env": "APEX_SMTP_PASSWORD",
        "email_from_address": "", "email_from_name": "APEX", "ntfy_base_url": "https://ntfy.sh", "ntfy_token_env": "APEX_NTFY_TOKEN",
        "max_messages_per_user_per_hour": 10, "retry_max_attempts": 5, "retry_backoff_seconds": 60,
        "verification_code_ttl_minutes": 10, "verification_max_attempts": 5, "app_base_url": "", "dry_run": 0,
        "unsubscribe_signing_key_env": "APEX_NOTIFY_SIGNING_KEY"}
    assert {k: s[k] for k in expected} == expected
    assert (s["id"], s["version"], s["updated_by"]) == (1, 1, None) and s["created_at"] and s["updated_at"]
    assert sdb.get_notification_settings() == s                         # a second read re-seeds nothing


def test_update_bumps_the_version_conflicts_when_stale_and_resets_to_defaults():
    v1 = sdb.get_notification_settings()["version"]
    s2 = sdb.update_notification_settings(updated_by="admin", expected_version=v1, max_messages_per_user_per_hour=25, app_base_url="https://apex.example.com/")
    assert (s2["version"], s2["updated_by"], s2["max_messages_per_user_per_hour"]) == (v1 + 1, "admin", 25)
    assert s2["app_base_url"] == "https://apex.example.com"             # trailing slash normalised
    with pytest.raises(SettingsVersionConflict):
        sdb.update_notification_settings(updated_by="admin", expected_version=v1, dry_run=1)
    assert sdb.get_notification_settings()["dry_run"] == 0
    assert sdb.update_notification_settings(updated_by="admin")["version"] == v1 + 1       # nothing to change: no bump
    reset = sdb.reset_notification_settings_to_defaults(updated_by="admin")
    assert reset["max_messages_per_user_per_hour"] == 10 and reset["app_base_url"] == "" and reset["version"] == v1 + 2


@pytest.mark.parametrize("fields,message", [
    ({"whatsapp_enabled": 1}, "WHATSAPP sender is not implemented"),
    ({"sms_enabled": True}, "SMS sender is not implemented"),
    ({"smtp_port": 0}, "smtp_port"), ({"smtp_port": 70000}, "smtp_port"), ({"smtp_port": 587.5}, "smtp_port"),
    ({"retry_max_attempts": 0}, "retry_max_attempts"), ({"max_messages_per_user_per_hour": -1}, "max_messages_per_user_per_hour"),
    ({"smtp_password_env": "bad name!"}, "environment variable NAME"), ({"ntfy_token_env": "1BAD"}, "environment variable NAME"),
    ({"ntfy_base_url": "ftp://x.example"}, "http(s) URL"), ({"ntfy_base_url": "https://user:pw@ntfy.example"}, "no credentials"),
    ({"ntfy_base_url": ""}, "required"), ({"app_base_url": "javascript:alert(1)"}, "http(s) URL"),
    ({"email_from_address": "not-an-address"}, "valid email"), ({"email_from_address": "a@b.com\nBcc: x@y.com"}, "line breaks"),
    ({"email_from_name": "APEX\r\nBcc: x@y.com"}, "line breaks"), ({"email_from_name": "x" * 81}, "80 characters"),
    ({"smtp_host": "bad host"}, "smtp_host"), ({"dry_run": "yes"}, "on or off"), ({"nonsense": 1}, "unknown notification setting"),
    ({"smtp_port": None}, "value is required"),
])
def test_invalid_settings_are_refused_and_change_nothing(fields, message):
    before = sdb.get_notification_settings()
    with pytest.raises(ValueError, match=message.replace("(", r"\(").replace(")", r"\)")):
        sdb.update_notification_settings(updated_by="admin", **fields)
    assert sdb.get_notification_settings() == before


def test_email_cannot_be_enabled_without_a_host_and_a_from_address():
    with pytest.raises(ValueError, match="email cannot be enabled"):
        sdb.update_notification_settings(updated_by="a", email_enabled=1)                    # no from-address
    with pytest.raises(ValueError, match="email cannot be enabled"):
        sdb.update_notification_settings(updated_by="a", email_from_address="a@example.com", smtp_host="")
    assert sdb.update_notification_settings(updated_by="a", email_from_address="apex@example.com")["email_from_address"] == "apex@example.com"
    assert sdb.update_notification_settings(updated_by="a", email_enabled=0)["email_enabled"] == 0     # switching off is always allowed


def test_effective_channels_only_email_and_push_ever_deliver():
    s = sdb.get_notification_settings()
    assert sdb.effective_channel_enabled(s, "EMAIL") is False            # the seed is "on" but has no from-address
    assert sdb.effective_channel_enabled(s, "PUSH") is True
    _email_ready()
    s = sdb.get_notification_settings()
    assert sdb.effective_channel_enabled(s, "EMAIL") is True
    with sqlite3.connect(str(db_module.DB_PATH)) as c:                   # even if a flag were forced on in the table
        c.execute("UPDATE notification_settings SET whatsapp_enabled = 1, sms_enabled = 1")
    s = sdb.get_notification_settings()
    assert [sdb.effective_channel_enabled(s, ch) for ch in ("WHATSAPP", "SMS", "SLACK", "nonsense")] == [False] * 4
    assert {c for c in CHANNELS if sdb.effective_channel_enabled(s, c)} == {"EMAIL", "PUSH"} == {c.value for c in ACTIVE_CHANNELS}


def test_env_presence_reports_present_or_missing_and_never_the_value(monkeypatch, caplog):
    secret = "s3cr3t-password-value-123"
    monkeypatch.setenv("APEX_SMTP_PASSWORD", secret)
    caplog.set_level(logging.DEBUG)
    rows = sdb.env_presence()
    by = {r["setting"]: r for r in rows}
    assert by["smtp_password_env"]["present"] is True and by["smtp_password_env"]["env_var"] == "APEX_SMTP_PASSWORD"
    assert by["smtp_username_env"]["present"] is False and by["ntfy_token_env"]["required"] is False
    assert secret not in repr(rows) and secret not in caplog.text
    sdb.update_notification_settings(updated_by="a", smtp_password_env="MY_OTHER_VAR")             # only the NAME is stored
    assert secret not in json.dumps(sdb.get_notification_settings())
    assert {r["env_var"]: r["present"] for r in sdb.env_presence()}["MY_OTHER_VAR"] is False


# ── Settings: admin-only service, audited, no secrets ─────────────────────────

def test_global_settings_are_admin_only_audited_and_never_expose_a_secret(monkeypatch):
    secret = "hunter2-very-secret"
    monkeypatch.setenv("APEX_SMTP_PASSWORD", secret)
    _invite()                                                              # a member, not an admin
    for call in (lambda: ns.get_settings_admin(ME), lambda: ns.update_settings(ME, dry_run=1), lambda: ns.reset_settings(ME)):
        with pytest.raises(inv.NotAdmin):
            call()
    _make_admin()
    view = ns.get_settings_admin(ME)
    assert view["channels"] == {"EMAIL": False, "PUSH": True, "WHATSAPP": False, "SMS": False, "SLACK": False}
    assert {r["env_var"]: r["present"] for r in view["env"]}["APEX_SMTP_PASSWORD"] is True and secret not in repr(view)
    out = ns.update_settings(ME, expected_version=view["settings"]["version"], email_from_address="apex@example.com", reason="go live")
    assert out["email_from_address"] == "apex@example.com" and ns.get_settings_admin(ME)["channels"]["EMAIL"] is True
    ev = [e for e in repo.get_audit_events(limit=50) if e["operation"] == "update_notification_settings"][0]
    assert ev["before"] == {"email_from_address": ""} and ev["after"] == {"email_from_address": "apex@example.com"} and ev["reason"] == "go live"
    with pytest.raises(SettingsVersionConflict):
        ns.update_settings(ME, expected_version=view["settings"]["version"], dry_run=1)             # stale
    with pytest.raises(ValueError):
        ns.update_settings(ME, whatsapp_enabled=1)
    assert sdb.get_notification_settings()["dry_run"] == 0 and secret not in _audit_text()
    assert ns.reset_settings(ME)["email_from_address"] == "" and "reset_notification_settings" in _ops()


# ── Contacts ──────────────────────────────────────────────────────────────────

def test_the_sign_in_email_becomes_a_verified_contact_automatically_and_follows_a_change():
    _invite()
    contacts = ns.ensure_contacts(ME)
    [login] = [c for c in contacts if c["kind"] == "LOGIN"]
    assert (login["channel"], login["address"], login["verified"], login["opted_out"]) == ("EMAIL", MY_EMAIL, 1, 0) and login["verified_at"]
    assert ns.ensure_contacts(ME) == contacts and _ops().count("ensure_notification_contact") == 1       # idempotent, audited once
    auth_users_db.invite_user("new.address@example.com", LOCAL_OWNER, "member")                        # a second sign-in row, same person
    assert len(cdb.list_contacts(LOCAL_OWNER)) == 1


def test_a_person_with_no_sign_in_email_gets_no_email_contact():
    assert [c for c in ns.ensure_contacts(ME) if c["channel"] == "EMAIL"] == []


def test_one_alternate_email_unverified_and_never_the_same_as_the_login():
    _invite()
    alt = ns.add_contact(ME, "email", "  Alt.Person@Gmail.com ")
    assert (alt["kind"], alt["address"], alt["verified"]) == ("ALTERNATE", RAW_ALT, 0)               # normalised, unverified
    for bad, why in (("another@example.com", "already have an alternate"), ):
        with pytest.raises(cdb.ContactError, match=why):
            ns.add_alternate_email(ME, bad)
    ns.remove_contact(ME, alt["id"])
    for bad, why in ((MY_EMAIL, "already your sign-in email"), ("nope", "does not look like"), ("a@b.com\nBcc: x@y.com", "line breaks")):
        with pytest.raises(cdb.ContactError, match=why):
            ns.add_alternate_email(ME, bad)
    assert [c["kind"] for c in cdb.list_contacts(LOCAL_OWNER)] == ["LOGIN"]


def test_push_topic_is_long_random_unique_per_person_and_unverified_until_confirmed():
    other = _ctx("someone-else")
    with repo.unit_of_work() as c:
        c.execute("INSERT INTO portfolios (owner) VALUES (?)", ("someone-else",))
    a, b = ns.setup_push(ME), ns.setup_push(other)
    for row in (a, b):
        assert row["channel"] == "PUSH" and row["kind"] == "TOPIC" and row["verified"] == 0
        assert row["address"].startswith("apex-") and len(row["address"]) == 5 + 32 and set(row["address"][5:]) <= set("0123456789abcdef")
    assert a["address"] != b["address"] and ns.setup_push(ME)["address"] == a["address"]            # stable once made
    fresh = ns.regenerate_push_topic(ME)
    assert fresh["address"] != a["address"] and fresh["verified"] == 0 and fresh["id"] == a["id"]
    cdb.set_verified(LOCAL_OWNER, a["id"], True)
    assert ns.regenerate_push_topic(ME)["verified"] == 0                                           # a new topic must be re-confirmed


@pytest.mark.parametrize("channel", ["WHATSAPP", "SMS", "SLACK", "whatsapp"])
def test_whatsapp_sms_and_slack_contacts_are_refused_clearly(channel):
    with pytest.raises(cdb.ChannelUnavailable, match="not available yet"):
        ns.add_contact(ME, channel, "+15551234567")
    with pytest.raises(cdb.ChannelUnavailable):
        cdb.refuse_unavailable(channel)
    assert cdb.list_contacts(LOCAL_OWNER) == []
    with pytest.raises(cdb.ContactError, match="unknown channel"):
        ns.add_contact(ME, "carrier-pigeon", "x")


def test_addresses_are_masked_tags_in_logs_audit_and_errors(caplog):
    caplog.set_level(logging.DEBUG)
    _invite()
    ns.ensure_contacts(ME)
    alt = ns.add_alternate_email(ME, RAW_ALT)
    topic = ns.setup_push(ME)["address"]
    ns.regenerate_push_topic(ME)
    ns.opt_out(ME, alt["id"]); ns.opt_in(ME, alt["id"]); ns.remove_contact(ME, alt["id"])
    with pytest.raises(cdb.ContactError) as exc:
        ns.add_alternate_email(ME, MY_EMAIL)
    tag = cdb.address_tag(RAW_ALT)
    assert tag in caplog.text and tag in _audit_text()
    for raw in (RAW_ALT, MY_EMAIL, topic, topic[5:]):
        assert raw not in caplog.text and raw not in _audit_text() and raw not in str(exc.value), raw
    assert cdb.address_tag(" ALT.person@gmail.com ") == tag and len(tag) == 12 and tag != cdb.address_tag("other@gmail.com")
    assert cdb.display_mask("EMAIL", RAW_ALT) == "a***@g***.com" and RAW_ALT not in cdb.display_mask("EMAIL", RAW_ALT)
    assert cdb.display_mask("PUSH", "apex-" + "0" * 28 + "a1b2") == "apex-…a1b2" and cdb.display_mask("EMAIL", "garbage") == "***"


def test_contacts_are_owner_scoped_someone_elses_id_looks_like_no_such_contact():
    _invite()
    other = _ctx("someone-else")
    with repo.unit_of_work() as c:
        c.execute("INSERT INTO portfolios (owner) VALUES (?)", ("someone-else",))
    mine = ns.add_alternate_email(ME, RAW_ALT)
    for call in (lambda: ns.opt_out(other, mine["id"]), lambda: ns.remove_contact(other, mine["id"]), lambda: ns.opt_in(other, mine["id"])):
        with pytest.raises(cdb.ContactNotFound, match="no such contact"):
            call()
    with pytest.raises(cdb.ContactNotFound, match="no such contact"):
        ns.remove_contact(other, 999999)                                                            # same message as a missing id
    assert cdb.get_contact(LOCAL_OWNER, mine["id"])["opted_out"] == 0
    assert RAW_ALT not in json.dumps(ns.ensure_contacts(other)) and ns.ensure_contacts(other) == []


def test_login_email_cannot_be_removed_and_opt_out_and_in_are_audited_once_per_change():
    _invite()
    login = [c for c in ns.ensure_contacts(ME) if c["kind"] == "LOGIN"][0]
    with pytest.raises(cdb.ContactError, match="cannot be removed"):
        ns.remove_contact(ME, login["id"])
    assert ns.opt_out(ME, login["id"])["opted_out"] == 1 and ns.opt_out(ME, login["id"])["opted_out"] == 1
    assert ns.opt_in(ME, login["id"])["opted_out"] == 0 and ns.opt_in(ME, login["id"])["opted_out"] == 0
    assert _ops().count("opt_out_notification_contact") == 1 and _ops().count("opt_in_notification_contact") == 1


# ── Routing ───────────────────────────────────────────────────────────────────

def test_a_new_person_gets_the_specified_routing_defaults():
    r = rdb.get_routing("brand-new")
    assert r == {"MARKET_EVENT": {"PUSH": True, "EMAIL": True}, "PORTFOLIO_HEALTH": {"EMAIL": True, "PUSH": False},
                 "COMPLIANCE": {"PUSH": True, "EMAIL": True}, "DAILY_DIGEST": {"EMAIL": False}, "SYSTEM": {"EMAIL": True, "PUSH": True}}
    assert "PUSH" not in r["DAILY_DIGEST"]                                                          # a digest is email only
    assert rdb.get_routing("brand-new") == r and set(r) == set(ALERT_CATEGORIES)


def _deliverable_world(category="MARKET_EVENT"):
    """EMAIL + PUSH fully deliverable for LOCAL_OWNER: global on, routed, contacts verified, nobody opted out."""
    _invite()
    _email_ready()
    ns.ensure_contacts(ME)
    push = ns.setup_push(ME)
    cdb.set_verified(LOCAL_OWNER, push["id"], True)
    assert ns.explain_channels(LOCAL_OWNER, category)["EMAIL"] == ns.DELIVERABLE
    assert ns.explain_channels(LOCAL_OWNER, category)["PUSH"] == ns.DELIVERABLE
    return push


def test_a_channel_delivers_only_if_all_five_conditions_hold():
    push = _deliverable_world()
    email = [c for c in cdb.list_contacts(LOCAL_OWNER) if c["channel"] == "EMAIL"][0]
    why = ns.explain_channels(LOCAL_OWNER, "MARKET_EVENT")
    assert why == {"EMAIL": "DELIVERABLE", "PUSH": "DELIVERABLE", "WHATSAPP": "NOT_ACTIVE", "SMS": "NOT_ACTIVE", "SLACK": "NOT_ACTIVE"}
    assert {c["channel"] for c in ns.deliverable_contacts(LOCAL_OWNER, "MARKET_EVENT")} == {"EMAIL", "PUSH"}

    sdb.update_notification_settings(updated_by="a", push_enabled=0)                                 # 1. globally disabled
    assert ns.explain_channels(LOCAL_OWNER, "MARKET_EVENT")["PUSH"] == ns.GLOBALLY_DISABLED
    sdb.update_notification_settings(updated_by="a", push_enabled=1)
    ns.set_routing(ME, "MARKET_EVENT", "PUSH", False)                                                # 2. not routed
    assert ns.explain_channels(LOCAL_OWNER, "MARKET_EVENT")["PUSH"] == ns.NOT_ROUTED
    assert ns.explain_channels(LOCAL_OWNER, "COMPLIANCE")["PUSH"] == ns.DELIVERABLE                 # routing is per category
    ns.set_routing(ME, "MARKET_EVENT", "PUSH", True)
    cdb.set_verified(LOCAL_OWNER, push["id"], False)                                                 # 3. contact unverified
    assert ns.explain_channels(LOCAL_OWNER, "MARKET_EVENT")["PUSH"] == ns.UNVERIFIED
    cdb.set_verified(LOCAL_OWNER, push["id"], True)
    ns.opt_out(ME, push["id"])                                                                       # 4. opted out
    assert ns.explain_channels(LOCAL_OWNER, "MARKET_EVENT")["PUSH"] == ns.OPTED_OUT
    assert {c["channel"] for c in ns.deliverable_contacts(LOCAL_OWNER, "MARKET_EVENT")} == {"EMAIL"}
    ns.opt_in(ME, push["id"])
    ns.remove_contact(ME, push["id"])                                                                # 5. no contact
    assert ns.explain_channels(LOCAL_OWNER, "MARKET_EVENT")["PUSH"] == ns.NO_CONTACT
    sdb.update_notification_settings(updated_by="a", email_from_address="", email_enabled=0)         # email off globally
    assert ns.explain_channels(LOCAL_OWNER, "MARKET_EVENT")["EMAIL"] == ns.GLOBALLY_DISABLED
    assert ns.deliverable_contacts(LOCAL_OWNER, "MARKET_EVENT") == [] and email["verified"] == 1


def test_a_verified_alternate_email_is_a_second_deliverable_contact_and_an_unverified_one_is_not():
    _deliverable_world()
    alt = ns.add_alternate_email(ME, RAW_ALT)
    assert [c["address"] for c in ns.deliverable_contacts(LOCAL_OWNER, "MARKET_EVENT") if c["channel"] == "EMAIL"] == [MY_EMAIL]
    cdb.set_verified(LOCAL_OWNER, alt["id"], True)
    assert {c["address"] for c in ns.deliverable_contacts(LOCAL_OWNER, "MARKET_EVENT") if c["channel"] == "EMAIL"} == {MY_EMAIL, RAW_ALT}


def test_routing_refuses_unavailable_channels_and_combinations_and_audits_only_changes():
    _invite()
    for channel in ("WHATSAPP", "SMS", "SLACK"):
        with pytest.raises(ValueError, match="not available yet"):
            ns.set_routing(ME, "MARKET_EVENT", channel, True)
    with pytest.raises(ValueError, match="not sent by Push"):
        ns.set_routing(ME, "DAILY_DIGEST", "PUSH", True)
    with pytest.raises(ValueError, match="unknown alert category"):
        ns.set_routing(ME, "GOSSIP", "EMAIL", True)
    assert _ops().count("set_notification_routing") == 0
    grid = ns.set_routing(ME, "DAILY_DIGEST", "EMAIL", True)
    assert grid["DAILY_DIGEST"]["EMAIL"] is True and ns.set_routing(ME, "DAILY_DIGEST", "EMAIL", True)
    assert _ops().count("set_notification_routing") == 1                                              # the repeat changed nothing
    other = _ctx("someone-else")
    with repo.unit_of_work() as c:
        c.execute("INSERT INTO portfolios (owner) VALUES (?)", ("someone-else",))
    assert rdb.get_routing("someone-else")["DAILY_DIGEST"]["EMAIL"] is False                         # routing is per person


# ── Preferences (the user_notifications row) ──────────────────────────────────

def test_new_preference_columns_default_validate_and_audit_only_what_changed():
    prefs = udb.get_user_notifications_for(ME)
    assert (prefs.content_level, prefs.quiet_hours_mode, prefs.digest_time, prefs.channel) == ("BRIEF", "DEFER", "07:30", "none")
    out = ns.update_preferences(ME, content_level="DETAILED", quiet_hours_mode="SUPPRESS", digest_time="6:5",
                                quiet_hours_start="22:00", quiet_hours_end="07:00")
    assert (out["content_level"], out["quiet_hours_mode"], out["digest_time"]) == ("DETAILED", "SUPPRESS", "06:05")
    ev = [e for e in repo.get_audit_events(limit=20) if e["operation"] == "update_notification_preferences"][0]
    assert set(ev["after"]) == {"content_level", "quiet_hours_mode", "digest_time", "quiet_hours_start", "quiet_hours_end"}
    ns.update_preferences(ME, content_level="DETAILED")                                              # no change -> no audit event
    assert _ops().count("update_notification_preferences") == 1
    for bad in ({"content_level": "LOUD"}, {"quiet_hours_mode": "DROP"}, {"digest_time": "25:99"}, {"digest_time": ""}, {"mystery": 1}):
        with pytest.raises(ValueError):
            ns.update_preferences(ME, **bad)
    assert udb.get_user_notifications_for(ME).content_level == "DETAILED"                            # failed updates changed nothing
    assert ns.update_preferences(ME, channel="email")["channel"] == "email"                          # the legacy field still works


def test_the_legacy_should_notify_is_unchanged():
    assert udb.should_notify(severity=5) == (False, "channel is 'none'")
    udb.update_user_notifications_for(ME, channel="email", min_severity_threshold=3)
    ok, detail = udb.should_notify(severity=4, prefs=udb.get_user_notifications_for(ME))
    assert (ok, detail) in ((True, "email"),) or "quiet hours" in detail


def test_notification_state_bundles_contacts_routing_preferences_and_reasons():
    _invite()
    _email_ready()
    state = ns.get_notification_state(ME)
    assert set(state) == {"contacts", "routing", "preferences", "channels"}
    assert state["channels"]["MARKET_EVENT"]["EMAIL"] == "DELIVERABLE" and state["channels"]["MARKET_EVENT"]["PUSH"] == "NO_CONTACT"
    assert state["channels"]["DAILY_DIGEST"]["EMAIL"] == "NOT_ROUTED" and state["channels"]["SYSTEM"]["SMS"] == "NOT_ACTIVE"
    assert state["preferences"]["content_level"] == "BRIEF"


# ── Migration of the old single `channel` setting ─────────────────────────────

_OLD_PER_OWNER = """CREATE TABLE user_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL UNIQUE, channel TEXT NOT NULL DEFAULT 'none',
    min_severity_threshold INTEGER NOT NULL DEFAULT 3, min_conviction_threshold INTEGER,
    quiet_hours_start TEXT, quiet_hours_end TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
_OLD_SINGLETON = """CREATE TABLE user_notifications (
    id INTEGER PRIMARY KEY CHECK (id = 1), channel TEXT NOT NULL DEFAULT 'none',
    min_severity_threshold INTEGER NOT NULL DEFAULT 3, min_conviction_threshold INTEGER,
    quiet_hours_start TEXT, quiet_hours_end TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""


def _old_db(ddl: str, rows: list[tuple]) -> None:
    path = Path(db_module.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as c:
        c.execute(ddl)
        for r in rows:
            if "owner" in ddl:
                c.execute("INSERT INTO user_notifications (owner, channel, min_severity_threshold, created_at, updated_at) VALUES (?, ?, ?, 'x', 'y')", r)
            else:
                c.execute("INSERT INTO user_notifications (id, channel, min_severity_threshold, created_at, updated_at) VALUES (1, ?, ?, 'x', 'y')", r)


def _routing_rows(owner: str) -> dict:
    with sqlite3.connect(str(db_module.DB_PATH)) as c:
        return {(r[0], r[1]): r[2] for r in c.execute("SELECT category, channel, enabled FROM notification_routing WHERE owner = ?", [owner])}


def test_existing_email_users_keep_exactly_what_they_had_and_everyone_else_is_switched_off():
    _old_db(_OLD_PER_OWNER, [("alice", "email", 4), ("bob", "none", 3), ("carol", "slack", 3)])
    prefs = udb.get_user_notifications_by_owner("alice")                                            # first touch runs the migration
    assert (prefs.channel, prefs.min_severity_threshold, prefs.content_level, prefs.quiet_hours_mode, prefs.digest_time) == ("email", 4, "BRIEF", "DEFER", "07:30")
    a = _routing_rows("alice")
    assert {k for k, v in a.items() if v} == {("MARKET_EVENT", "EMAIL"), ("PORTFOLIO_HEALTH", "EMAIL")}      # what 'email' covered, nothing more
    assert len(a) == 9                                                                                # the whole grid exists
    assert not any(_routing_rows("bob").values()) and not any(_routing_rows("carol").values())
    assert len(_routing_rows("bob")) == 9 and len(_routing_rows("carol")) == 9
    with sqlite3.connect(str(db_module.DB_PATH)) as c:
        assert dict(c.execute("SELECT owner, routing_source FROM user_notifications")) == {"alice": "MIGRATED", "bob": "MIGRATED", "carol": "MIGRATED"}
        assert dict(c.execute("SELECT owner, channel FROM user_notifications")) == {"alice": "email", "bob": "none", "carol": "slack"}   # still readable


def test_the_migration_is_idempotent_and_never_overwrites_a_later_choice():
    _old_db(_OLD_PER_OWNER, [("alice", "email", 3)])
    udb.get_user_notifications_by_owner("alice")
    ns.set_routing(_ctx("alice"), "COMPLIANCE", "EMAIL", True)                                      # alice opts into more
    ns.set_routing(_ctx("alice"), "MARKET_EVENT", "EMAIL", False)
    before = _routing_rows("alice")
    for _ in range(3):
        udb.get_user_notifications_by_owner("alice")                                                # every connection re-runs setup
        udb.get_user_notifications_by_owner("late-joiner")
    assert _routing_rows("alice") == before and before[("COMPLIANCE", "EMAIL")] == 1 and before[("MARKET_EVENT", "EMAIL")] == 0
    assert rdb.migrate_legacy_rows(sqlite3.connect(str(db_module.DB_PATH))) == 0                    # nothing left to migrate


def test_someone_added_after_routing_existed_gets_the_new_defaults_not_the_legacy_mapping():
    _old_db(_OLD_PER_OWNER, [("alice", "none", 3)])
    udb.get_user_notifications_by_owner("alice")
    udb.get_user_notifications_by_owner("newcomer")                                                  # created after the upgrade
    assert rdb.get_routing("newcomer")["MARKET_EVENT"] == {"PUSH": True, "EMAIL": True}
    assert not any(rdb.get_routing("alice")[c].get("EMAIL") for c in ALERT_CATEGORIES)                # migrated 'none': all off
    with sqlite3.connect(str(db_module.DB_PATH)) as c:
        assert c.execute("SELECT routing_source FROM user_notifications WHERE owner = 'newcomer'").fetchone()[0] is None


def test_the_oldest_singleton_table_shape_also_migrates():
    _old_db(_OLD_SINGLETON, [("email", 5)])
    prefs = udb.get_user_notifications_by_owner(LOCAL_OWNER)
    assert (prefs.channel, prefs.min_severity_threshold) == ("email", 5)
    assert {k for k, v in _routing_rows(LOCAL_OWNER).items() if v} == {("MARKET_EVENT", "EMAIL"), ("PORTFOLIO_HEALTH", "EMAIL")}


def test_a_fresh_database_needs_no_migration_and_creates_every_table():
    udb.get_user_notifications_for(ME)
    ns.get_notification_state(ME)
    with sqlite3.connect(str(db_module.DB_PATH)) as c:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        cols = {r[1] for r in c.execute("PRAGMA table_info(user_notifications)")}
    assert {"notification_settings", "notification_contacts", "notification_routing", "user_notifications"} <= tables
    assert {"content_level", "quiet_hours_mode", "digest_time", "routing_source"} <= cols


def test_no_sender_or_network_code_exists_yet():
    root = Path(__file__).resolve().parents[1] / "portfolio_agent"
    for mod in ("services/notification_service.py", "tools/notification_contacts_db.py", "tools/notification_routing_db.py",
                "tools/notification_settings_db.py"):
        src = (root / mod).read_text()
        for banned in ("smtplib", "import requests", "urllib.request", "socket", "twilio"):
            assert banned not in src, f"{mod} must not send anything in this step ({banned})"
