"""
Notifications, Step 2 — outbox, delivery worker and senders.

No network, ever: every sender here is a fake, and the real EmailSender / NtfyPushSender are driven through a fake SMTP class
and a fake HTTP post. Covered: fan-out, dedup per channel, push always brief, quiet hours, the hourly limit, retry / backoff /
FAILED, atomic concurrent claims, no database connection held while sending, secrets and addresses never logged or stored,
signed one-click unsubscribe, an opted-out contact never receiving, alternate-email codes, test sends, the stubs, dry run and
the senders' timeouts.
"""
from __future__ import annotations

import json
import logging
import smtplib
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.domain import LOCAL_OWNER
from portfolio_agent.services import notification_senders as snd
from portfolio_agent.services import notification_service as ns
from portfolio_agent.services import notification_worker as worker
from portfolio_agent.services.context import RequestContext, _mint_identity_for_tests, local_context
from portfolio_agent.tools import auth_users_db
from portfolio_agent.tools import holdings_db as repo
from portfolio_agent.tools import notification_contacts_db as cdb
from portfolio_agent.tools import notification_log_db
from portfolio_agent.tools import notification_outbox_db as obx
from portfolio_agent.tools import notification_settings_db as sdb
from portfolio_agent.tools import user_notifications_db as udb

ME = local_context("test")
MY_EMAIL = "sumanth@example.com"
ALT = "alt.person@gmail.com"
NOW = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)
CHI = ZoneInfo("America/Chicago")


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    for name in ("APEX_SMTP_USERNAME", "APEX_SMTP_PASSWORD", "APEX_NTFY_TOKEN", "APEX_NOTIFY_SIGNING_KEY"):
        monkeypatch.delenv(name, raising=False)
    ns._last_test.clear()


def _world(*, push_verified=True, content_level=None):
    """EMAIL + PUSH fully deliverable for LOCAL_OWNER (market events), in a clean world with notifications switched on."""
    auth_users_db.invite_user(MY_EMAIL, LOCAL_OWNER, "member")
    sdb.update_notification_settings(updated_by="t", email_from_address="apex@example.com", app_base_url="https://apex.example.com")
    ns.ensure_contacts(ME)
    push = ns.setup_push(ME)
    if push_verified:
        cdb.set_verified(LOCAL_OWNER, push["id"], True)
    if content_level:
        ns.update_preferences(ME, content_level=content_level)
    return push


def _fakes():
    return {"EMAIL": snd.FakeSender("EMAIL"), "PUSH": snd.FakeSender("PUSH"), "WHATSAPP": snd.WhatsAppSender(), "SMS": snd.SmsSender()}


def _notify(key="event:1", category="MARKET_EVENT", **kw):
    args = dict(dedup_key=key, title="APEX: NVDA — material news", brief="APEX: NVDA - material news (severity 4). Open APEX for details.",
                severity=4, link="/Predictions", now=NOW.astimezone(CHI))
    args.update(kw)
    return ns.notify(LOCAL_OWNER, category, **args)


def _rows(**where):
    with sqlite3.connect(str(db_module.DB_PATH)) as c:
        c.row_factory = sqlite3.Row
        sql = "SELECT * FROM notification_outbox" + (" WHERE " + " AND ".join(f"{k} = ?" for k in where) if where else "") + " ORDER BY id"
        return [dict(r) for r in c.execute(sql, list(where.values()))]


def _second_owner(name="someone-else"):
    with repo.unit_of_work() as c:
        c.execute("INSERT INTO portfolios (owner) VALUES (?)", (name,))
    return RequestContext(identity=_mint_identity_for_tests(name), source="test")


# ── notify(): fan-out, filters, quiet hours, rate limit, dedup ────────────────

def test_notify_fans_out_to_every_deliverable_channel_and_only_enqueues():
    _world()
    out = _notify()
    assert sorted(e["channel"] for e in out["enqueued"]) == ["EMAIL", "PUSH"] and out["error"] is None and out["duplicates"] == []
    rows = _rows()
    assert [(r["channel"], r["status"], r["attempts"], r["category"]) for r in rows] == [("EMAIL", "PENDING", 0, "MARKET_EVENT"), ("PUSH", "PENDING", 0, "MARKET_EVENT")]
    assert rows[0]["link_url"] == "https://apex.example.com/Predictions" and rows[0]["severity"] == 4
    assert all(r["contact_id"] for r in rows)
    assert {"channel": "SLACK", "reason": "NOT_ACTIVE"} not in out["skipped"]                    # reserved channels are not even mentioned


def test_nothing_is_enqueued_for_a_channel_that_is_not_deliverable_and_the_reason_is_reported():
    _world(push_verified=False)
    out = _notify()
    assert [e["channel"] for e in out["enqueued"]] == ["EMAIL"] and {"channel": "PUSH", "reason": "UNVERIFIED"} in out["skipped"]
    ns.set_routing(ME, "COMPLIANCE", "EMAIL", False)
    assert _notify("c:1", category="COMPLIANCE")["enqueued"] == [] and {"channel": "EMAIL", "reason": "NOT_ROUTED"} in _notify("c:2", category="COMPLIANCE")["skipped"]


def test_dedup_is_per_owner_key_and_channel():
    _world()
    assert len(_notify("event:7")["enqueued"]) == 2
    again = _notify("event:7")
    assert again["enqueued"] == [] and sorted(again["duplicates"]) == ["EMAIL", "PUSH"]
    assert len(_notify("event:8")["enqueued"]) == 2                                              # another event: new rows
    ns.set_routing(ME, "MARKET_EVENT", "PUSH", False)
    assert [e["channel"] for e in _notify("event:9")["enqueued"]] == ["EMAIL"] and len(_rows()) == 5
    assert len([r for r in _rows() if r["dedup_key"] == "event:7"]) == 2


def test_push_is_always_brief_and_email_is_detailed_only_when_chosen_and_supplied():
    _world(content_level="DETAILED")
    _notify("d:1", detailed="NVDA is 14% of your portfolio ($52,000).")
    email, push = _rows(channel="EMAIL")[0], _rows(channel="PUSH")[0]
    assert email["body_detailed"] == "NVDA is 14% of your portfolio ($52,000)." and push["body_detailed"] is None
    regs = _fakes()
    worker.run_once(now=NOW, registry=regs)
    assert regs["EMAIL"].sent[0].body.startswith("NVDA is 14%") and "$" not in regs["PUSH"].sent[0].body and regs["PUSH"].sent[0].body.startswith("APEX: NVDA")
    ns.update_preferences(ME, content_level="BRIEF")
    _notify("d:2", detailed="NVDA is 14% of your portfolio ($52,000).")
    assert _rows(dedup_key="d:2", channel="EMAIL")[0]["body_detailed"] is None                    # the person asked for brief


def test_severity_and_conviction_filters_use_the_persons_thresholds():
    _world()
    ns.update_preferences(ME, min_severity_threshold=4)
    low = _notify("s:1", severity=3)
    assert low["enqueued"] == [] and {"channel": "EMAIL", "reason": "BELOW_SEVERITY"} in low["skipped"]
    assert len(_notify("s:2", severity=4)["enqueued"]) == 2 and len(_notify("s:3", severity=None)["enqueued"]) == 2       # no severity: not filtered
    ns.update_preferences(ME, min_conviction_threshold=7)
    assert _notify("s:4", severity=5, conviction=5)["skipped"][0]["reason"] == "BELOW_CONVICTION"


def test_quiet_hours_defer_holds_until_they_end_in_the_recipients_timezone():
    _world()
    ns.update_preferences(ME, quiet_hours_start="22:00", quiet_hours_end="07:00")                  # DEFER is the default mode
    out = _notify("q:1", now=datetime(2026, 10, 12, 23, 30, tzinfo=CHI))
    assert {e["status"] for e in out["enqueued"]} == {"DEFERRED"}
    row = _rows(channel="EMAIL")[0]
    assert datetime.fromisoformat(row["next_attempt_at"]) == datetime(2026, 10, 13, 7, 0, tzinfo=CHI)        # tomorrow 07:00 Chicago
    out = _notify("q:2", now=datetime(2026, 10, 12, 3, 0, tzinfo=CHI))                              # 03:00: ends this morning
    assert datetime.fromisoformat(_rows(dedup_key="q:2", channel="PUSH")[0]["next_attempt_at"]) == datetime(2026, 10, 12, 7, 0, tzinfo=CHI)
    assert {e["status"] for e in _notify("q:3", now=datetime(2026, 10, 12, 12, 0, tzinfo=CHI))["enqueued"]} == {"PENDING"}   # daytime
    # the 23:30 items are not due until 07:00 the next morning; the 03:00 and daytime ones are due already
    regs = _fakes()
    assert worker.run_once(now=datetime(2026, 10, 13, 6, 59, tzinfo=CHI), registry=regs)["claimed"] == 4
    assert worker.run_once(now=datetime(2026, 10, 13, 6, 59, tzinfo=CHI), registry=regs)["claimed"] == 0
    assert worker.run_once(now=datetime(2026, 10, 13, 7, 0, tzinfo=CHI), registry=regs)["claimed"] == 2 and regs["EMAIL"].calls == 3


def test_quiet_hours_suppress_drops_what_arrives_inside_them():
    _world()
    ns.update_preferences(ME, quiet_hours_start="22:00", quiet_hours_end="07:00", quiet_hours_mode="SUPPRESS")
    out = _notify("q:9", now=datetime(2026, 10, 12, 23, 0, tzinfo=CHI))
    assert {e["status"] for e in out["enqueued"]} == {"SUPPRESSED"} and {r["reason_code"] for r in _rows()} == {"QUIET_HOURS"}
    regs = _fakes()
    assert worker.run_once(now=NOW, registry=regs)["claimed"] == 0 and regs["EMAIL"].calls == 0


def test_over_the_hourly_limit_a_message_is_folded_into_the_digest_not_dropped():
    _world()
    sdb.update_notification_settings(updated_by="t", max_messages_per_user_per_hour=2)
    for i in range(2):
        assert {e["status"] for e in _notify(f"r:{i}")["enqueued"]} == {"PENDING"}
    third = _notify("r:2")
    assert {e["status"] for e in third["enqueued"]} == {"SUPPRESSED"}
    over = _rows(dedup_key="r:2")
    assert all(r["reason_code"] == "RATE_LIMITED" and r["fold_into_digest"] == 1 for r in over)
    assert {r["dedup_key"] for r in obx.digest_candidates(LOCAL_OWNER, NOW - timedelta(hours=2))} == {"r:2"}
    assert {e["status"] for e in _notify("digest:1", category="DAILY_DIGEST")["enqueued"]} <= {"PENDING"}     # the digest itself is exempt (if routed)
    later = NOW + timedelta(hours=2)
    assert {e["status"] for e in ns.notify(LOCAL_OWNER, "MARKET_EVENT", dedup_key="r:3", title="t", brief="b", severity=4,
                                           now=(later).astimezone(CHI))["enqueued"]} == {"PENDING"}               # an hour later: allowed again


def test_notify_never_raises_into_its_caller(monkeypatch):
    _world()
    monkeypatch.setattr(sdb, "get_notification_settings", lambda **k: (_ for _ in ()).throw(RuntimeError("db exploded")))
    out = _notify("x:1")
    assert out["error"] == "RuntimeError" and out["enqueued"] == []
    assert ns.notify("", "MARKET_EVENT", dedup_key="k", title="t", brief="b")["error"] == "ValueError"
    assert ns.notify(LOCAL_OWNER, "GOSSIP", dedup_key="k", title="t", brief="b")["error"] == "ValueError"


def test_notifications_are_per_owner_nobody_else_gets_anyone_elses():
    _world()
    other = _second_owner()
    out = ns.notify("someone-else", "MARKET_EVENT", dedup_key="event:1", title="t", brief="b", severity=4, now=NOW.astimezone(CHI))
    assert out["enqueued"] == []                                                                  # they have no verified contact
    assert [r["owner"] for r in _rows()] == []
    assert len(_notify("event:1")["enqueued"]) == 2 and {r["owner"] for r in _rows()} == {LOCAL_OWNER}
    assert obx.list_recent("someone-else") == [] and len(obx.list_recent(LOCAL_OWNER)) == 2 and other is not None


# ── Worker: sending, retry, failure, claims ───────────────────────────────────

def test_the_worker_sends_records_the_outcome_and_keeps_notification_log_in_step():
    _world()
    _notify("w:1")
    regs = _fakes()
    summary = worker.run_once(now=NOW, registry=regs)
    assert summary["claimed"] == 2 and summary["sent"] == 2 and summary["error"] is None
    rows = _rows()
    assert {r["status"] for r in rows} == {"SENT"} and all(r["sent_at"] and r["provider_message_id"].startswith("fake-") and r["attempts"] == 1 for r in rows)
    assert notification_log_db.was_delivered(f"user:{LOCAL_OWNER}", "w:1", "EMAIL") and notification_log_db.was_delivered(f"user:{LOCAL_OWNER}", "w:1", "PUSH")
    sent = regs["EMAIL"].sent[0]
    assert [r.address for r in sent.recipients] == [MY_EMAIL] and sent.subject == "APEX: NVDA — material news"
    assert worker.run_once(now=NOW, registry=regs)["claimed"] == 0                                 # nothing left: never sent twice


def test_transient_failures_retry_with_exponential_backoff_then_fail_after_the_limit():
    _world()
    sdb.update_notification_settings(updated_by="t", retry_max_attempts=3, retry_backoff_seconds=60, push_enabled=0)
    _notify("b:1")
    retry = snd.SendResult("retry", error="timeout")
    regs = {"EMAIL": snd.FakeSender("EMAIL", [retry, retry, retry])}
    t = NOW
    assert worker.run_once(now=t, registry=regs)["retried"] == 1
    row = _rows()[0]
    assert (row["status"], row["attempts"], row["last_error"]) == ("PENDING", 1, "timeout") and datetime.fromisoformat(row["next_attempt_at"]) == t + timedelta(seconds=60)
    assert worker.run_once(now=t + timedelta(seconds=30), registry=regs)["claimed"] == 0           # not due yet
    t2 = t + timedelta(seconds=60)
    assert worker.run_once(now=t2, registry=regs)["retried"] == 1
    assert datetime.fromisoformat(_rows()[0]["next_attempt_at"]) == t2 + timedelta(seconds=120)    # 60 x 2
    t3 = t2 + timedelta(seconds=120)
    assert worker.run_once(now=t3, registry=regs)["failed"] == 1
    row = _rows()[0]
    assert (row["status"], row["attempts"], row["next_attempt_at"]) == ("FAILED", 3, None) and regs["EMAIL"].calls == 3
    assert worker.backoff_seconds({"retry_backoff_seconds": 60}, 30) == 86400                       # capped at a day
    assert obx.requeue(row["id"]) and _rows()[0]["status"] == "PENDING" and _rows()[0]["attempts"] == 0   # an admin can requeue it
    assert obx.requeue(row["id"]) is False


def test_a_permanent_failure_fails_at_once_and_a_skip_is_suppressed():
    _world()
    _notify("p:1")
    regs = {"EMAIL": snd.FakeSender("EMAIL", [snd.SendResult("failed", error="SMTP login was rejected")]),
            "PUSH": snd.FakeSender("PUSH", [snd.SendResult("skipped", reason="PUSH_NOT_CONFIGURED")])}
    summary = worker.run_once(now=NOW, registry=regs)
    assert (summary["failed"], summary["suppressed"]) == (1, 1)
    by = {r["channel"]: r for r in _rows()}
    assert (by["EMAIL"]["status"], by["EMAIL"]["attempts"]) == ("FAILED", 1) and (by["PUSH"]["status"], by["PUSH"]["reason_code"]) == ("SUPPRESSED", "PUSH_NOT_CONFIGURED")


def test_one_bad_row_never_strands_the_others():
    _world()
    _notify("e:1")
    regs = {"EMAIL": snd.FakeSender("EMAIL", on_send=lambda m: (_ for _ in ()).throw(RuntimeError("boom"))), "PUSH": snd.FakeSender("PUSH")}
    summary = worker.run_once(now=NOW, registry=regs)
    assert (summary["sent"], summary["failed"]) == (1, 1)
    assert {r["channel"]: r["status"] for r in _rows()} == {"EMAIL": "FAILED", "PUSH": "SENT"}
    assert "worker error: RuntimeError" in _rows(channel="EMAIL")[0]["last_error"]


def test_concurrent_workers_can_never_claim_the_same_row():
    _world()
    for i in range(30):
        obx.enqueue(LOCAL_OWNER, "MARKET_EVENT", "EMAIL", dedup_key=f"c:{i}", subject="s", body_brief="b")
    got: dict[int, list[int]] = {}
    barrier = threading.Barrier(4)

    def go(n):
        barrier.wait()
        got[n] = [r["id"] for r in obx.claim_due(limit=30, now=NOW)]
    threads = [threading.Thread(target=go, args=(n,)) for n in range(4)]
    [t.start() for t in threads]; [t.join(timeout=60) for t in threads]
    flat = [i for ids in got.values() for i in ids]
    assert len(flat) == 30 and len(set(flat)) == 30                                                 # every row claimed exactly once
    assert {r["attempts"] for r in _rows()} == {1} and {r["status"] for r in _rows()} == {"SENDING"}


def test_a_row_abandoned_by_a_dead_worker_is_reclaimed_after_the_timeout():
    _world()
    obx.enqueue(LOCAL_OWNER, "MARKET_EVENT", "EMAIL", dedup_key="dead:1", subject="s", body_brief="b")
    assert len(obx.claim_due(now=NOW)) == 1
    assert obx.claim_due(now=NOW + timedelta(seconds=60)) == []                                      # a live worker still has it
    again = obx.claim_due(now=NOW + timedelta(seconds=obx.STALE_CLAIM_SECONDS + 5))
    assert [r["id"] for r in again] == [1] and again[0]["attempts"] == 2


def test_no_database_connection_is_held_while_a_message_is_being_sent():
    _world()
    _notify("g:1")
    seen = []

    def guard(message):
        c = sqlite3.connect(str(db_module.DB_PATH), timeout=0.3)
        try:
            c.execute("BEGIN IMMEDIATE")                                                             # fails with 'database is locked' if anything holds a write lock
            c.execute("CREATE TABLE IF NOT EXISTS guard_probe (x)")
            c.rollback()
            seen.append(message.channel)
        finally:
            c.close()
    regs = {"EMAIL": snd.FakeSender("EMAIL", on_send=guard), "PUSH": snd.FakeSender("PUSH", on_send=guard)}
    assert worker.run_once(now=NOW, registry=regs)["sent"] == 2 and sorted(seen) == ["EMAIL", "PUSH"]


def test_dry_run_sends_nothing_and_logs_what_it_would_send(caplog):
    _world()
    sdb.update_notification_settings(updated_by="t", dry_run=1)
    _notify("dry:1")
    regs = _fakes()
    caplog.set_level(logging.INFO)
    summary = worker.run_once(now=NOW, registry=regs)
    assert summary["dry_run"] == 2 and regs["EMAIL"].calls == 0 and regs["PUSH"].calls == 0
    assert {r["provider_message_id"] for r in _rows()} == {"dry-run"} and "would send" in caplog.text
    assert not notification_log_db.was_delivered(f"user:{LOCAL_OWNER}", "dry:1", "EMAIL")
    assert MY_EMAIL not in caplog.text


def test_an_opt_out_after_enqueue_is_honoured_at_send_time():
    _world()
    _notify("o:1")
    email = [c for c in cdb.list_contacts(LOCAL_OWNER) if c["channel"] == "EMAIL"][0]
    ns.opt_out(ME, email["id"])
    regs = _fakes()
    summary = worker.run_once(now=NOW, registry=regs)
    assert (summary["sent"], summary["suppressed"]) == (1, 1) and regs["EMAIL"].calls == 0
    assert [r["reason_code"] for r in _rows(channel="EMAIL")] == ["OPTED_OUT"]
    assert _notify("o:2")["skipped"] and {e["channel"] for e in _notify("o:3")["enqueued"]} == {"PUSH"}


def test_routing_or_global_changes_after_enqueue_also_suppress_at_send_time():
    _world()
    _notify("t:1")
    ns.set_routing(ME, "MARKET_EVENT", "PUSH", False)
    sdb.update_notification_settings(updated_by="t", email_enabled=0)
    regs = _fakes()
    assert worker.run_once(now=NOW, registry=regs)["suppressed"] == 2 and regs["EMAIL"].calls == regs["PUSH"].calls == 0
    assert {r["channel"]: r["reason_code"] for r in _rows()} == {"EMAIL": "CHANNEL_DISABLED", "PUSH": "ROUTING_OFF"}


def test_the_whatsapp_and_sms_stubs_send_nothing_and_are_never_reachable():
    for stub in (snd.WhatsAppSender(), snd.SmsSender()):
        r = stub.send(snd.Message(owner="o", channel=stub.channel, category="SYSTEM", subject="s", body="b", recipients=[]))
        assert (r.status, r.reason) == ("skipped", "channel not implemented")
        assert "TODO" in type(stub).__doc__ and ("Twilio" in type(stub).__doc__)
    assert "A2P 10DLC" in snd.SmsSender.__doc__ and "template" in snd.WhatsAppSender.__doc__.lower() and "STOP" in snd.SmsSender.__doc__
    reg = snd.build_registry(sdb.get_notification_settings())
    assert set(reg) == {"EMAIL", "PUSH", "WHATSAPP", "SMS"} and isinstance(reg["WHATSAPP"], snd.WhatsAppSender)
    obx.enqueue(LOCAL_OWNER, "MARKET_EVENT", "WHATSAPP", dedup_key="wa:1", subject="s", body_brief="b")        # even a row forced into the table
    assert worker.run_once(now=NOW, registry=reg)["suppressed"] == 1 and _rows()[0]["reason_code"] == "CHANNEL_NOT_ACTIVE"


# ── Real senders, driven with fakes ───────────────────────────────────────────

class FakeSMTP:
    instances: list = []
    script: dict = {}

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout, self.calls, self.sent = host, port, timeout, [], []
        FakeSMTP.instances.append(self)
        if "connect" in FakeSMTP.script:
            raise FakeSMTP.script["connect"]

    def ehlo(self): self.calls.append("ehlo")
    def starttls(self, context=None): self.calls.append("starttls")
    def quit(self): self.calls.append("quit")

    def login(self, user, password):
        self.calls.append("login")
        self.creds = (user, password)
        if "login" in FakeSMTP.script:
            raise FakeSMTP.script["login"]

    def send_message(self, msg):
        if "send" in FakeSMTP.script:
            raise FakeSMTP.script["send"]
        self.sent.append(msg)


def _email_sender(env=None, **settings):
    FakeSMTP.instances, FakeSMTP.script = [], {}
    s = {**sdb.get_notification_settings(), "email_from_address": "apex@example.com", "app_base_url": "https://apex.example.com", **settings}
    env = {"APEX_SMTP_USERNAME": "apex@example.com", "APEX_SMTP_PASSWORD": "app-password-xyz", "APEX_NOTIFY_SIGNING_KEY": "signing-key-123"} if env is None else env
    return snd.EmailSender(s, env=env, smtp_factory=FakeSMTP), s


def _msg(**kw):
    base = dict(owner=LOCAL_OWNER, channel="EMAIL", category="MARKET_EVENT", subject="APEX: NVDA — material news",
                body="APEX: NVDA - material news (severity 4).\n\nOpen APEX for details.", recipients=[snd.Recipient(1, MY_EMAIL)],
                link_url="https://apex.example.com/Predictions", manage_url="https://apex.example.com/Profile")
    base.update(kw)
    return snd.Message(**base)


def test_email_sender_speaks_smtp_with_starttls_login_from_env_and_a_footer_with_unsubscribe():
    sender, s = _email_sender()
    token_url = snd.unsubscribe_url(s, LOCAL_OWNER, 1, sender.env)
    result = sender.send(_msg(unsubscribe_urls={1: token_url}))
    assert result.status == "sent" and result.provider_message_id.startswith("<")
    smtp = FakeSMTP.instances[0]
    assert smtp.calls == ["ehlo", "starttls", "ehlo", "login", "quit"] and (smtp.host, smtp.port) == ("smtp.gmail.com", 587) and smtp.creds == ("apex@example.com", "app-password-xyz")
    m = smtp.sent[0]
    assert m["To"] == MY_EMAIL and "apex@example.com" in m["From"] and m["Subject"] == "APEX: NVDA — material news"
    assert m["List-Unsubscribe"] == f"<{token_url}>" and m["Auto-Submitted"] == "auto-generated"
    text = m.get_body(("plain",)).get_content()
    assert "Manage notifications: https://apex.example.com/Profile" in text and token_url in text and "not investment advice" in text
    assert m.get_body(("html",)) is not None and "Open in APEX" in text


def test_email_html_is_escaped_and_links_are_omitted_when_the_app_url_or_key_is_missing():
    sender, s = _email_sender(env={"APEX_SMTP_USERNAME": "u@example.com", "APEX_SMTP_PASSWORD": "pw-12345"})
    assert snd.unsubscribe_url(s, LOCAL_OWNER, 1, sender.env) is None                              # no signing key
    assert snd.unsubscribe_url({**s, "app_base_url": ""}, LOCAL_OWNER, 1, {"APEX_NOTIFY_SIGNING_KEY": "k"}) is None
    sender.send(_msg(body="<script>alert(1)</script> & more", link_url=None, manage_url=None))
    m = FakeSMTP.instances[0].sent[0]
    html_body = m.get_body(("html",)).get_content()
    assert "<script>" not in html_body and "&lt;script&gt;" in html_body and "List-Unsubscribe" not in m
    assert "Manage notifications" not in m.get_body(("plain",)).get_content()


@pytest.mark.parametrize("failure,status", [
    (smtplib.SMTPAuthenticationError(535, b"bad credentials"), "failed"),
    (smtplib.SMTPRecipientsRefused({"x@y.com": (550, b"no such user")}), "failed"),
    (smtplib.SMTPResponseException(421, b"try later"), "retry"), (smtplib.SMTPResponseException(554, b"rejected"), "failed"),
    (smtplib.SMTPServerDisconnected("gone"), "retry"), (TimeoutError("timed out"), "retry"), (ConnectionRefusedError("refused"), "retry"),
])
def test_email_errors_are_classified_retry_or_failed_and_scrubbed(failure, status):
    sender, _ = _email_sender()
    FakeSMTP.script = {"login": failure} if isinstance(failure, smtplib.SMTPAuthenticationError) else {"send": failure}
    r = sender.send(_msg())
    assert r.status == status
    for secret in ("app-password-xyz", MY_EMAIL):
        assert secret not in (r.error or "")
    assert FakeSMTP.instances[0].calls[-1] == "quit"                                                 # always closes the connection


def test_email_sender_needs_credentials_and_a_recipient_and_always_sets_a_timeout():
    sender, _ = _email_sender(env={})
    r = sender.send(_msg())
    assert r.status == "failed" and "APEX_SMTP_PASSWORD" in r.error and FakeSMTP.instances == []       # the NAME, never a value
    sender, _ = _email_sender()
    assert sender.send(_msg(recipients=[])).status == "skipped"
    sender.send(_msg())
    assert FakeSMTP.instances[0].timeout == snd.NETWORK_TIMEOUT_SECONDS
    sender, _ = _email_sender()
    FakeSMTP.script = {"connect": TimeoutError("connect timed out")}
    assert sender.send(_msg()).status == "retry"


class FakeResponse:
    def __init__(self, code, body=None):
        self.status_code, self._body = code, body or {}

    def json(self):
        return self._body


def _ntfy(env=None, response=None, boom=None, **settings):
    calls = []

    def post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        if boom:
            raise boom
        return response or FakeResponse(200, {"id": "nt-123"})
    s = {**sdb.get_notification_settings(), **settings}
    return snd.NtfyPushSender(s, env=env or {}, http_post=post), calls


TOPIC = "apex-" + "ab12cd34" * 4


def _pmsg(**kw):
    base = dict(channel="PUSH", recipients=[snd.Recipient(2, TOPIC)], body="APEX: NVDA - material news (severity 4). Open APEX for details.")
    base.update(kw)
    return _msg(**base)


def test_ntfy_sender_posts_json_with_the_topic_in_the_body_priority_click_and_optional_token():
    sender, calls = _ntfy(env={"APEX_NTFY_TOKEN": "tk_secret_token"})
    r = sender.send(_pmsg(severity=5))
    assert r.status == "sent" and r.provider_message_id == "nt-123"
    c = calls[0]
    assert c["url"] == "https://ntfy.sh" and TOPIC not in c["url"] and c["json"]["topic"] == TOPIC           # topic never in the URL
    assert c["json"]["priority"] == 5 and c["json"]["click"] == "https://apex.example.com/Predictions" and c["json"]["title"].startswith("APEX: NVDA")
    assert c["headers"] == {"Authorization": "Bearer tk_secret_token"} and c["timeout"] == snd.NETWORK_TIMEOUT_SECONDS
    sender, calls = _ntfy()
    sender.send(_pmsg(severity=None, link_url=None))
    assert calls[0]["headers"] == {} and calls[0]["json"]["priority"] == 3 and "click" not in calls[0]["json"]
    assert [snd.ntfy_priority(x) for x in (None, 0, 1, 4, 9)] == [3, 1, 1, 4, 5]
    assert sender.send(_pmsg(recipients=[])).status == "skipped"


@pytest.mark.parametrize("code,status", [(401, "failed"), (403, "failed"), (404, "failed"), (400, "failed"), (429, "retry"), (500, "retry"), (503, "retry")])
def test_ntfy_status_codes_map_to_retry_or_failed(code, status):
    sender, _ = _ntfy(response=FakeResponse(code))
    assert sender.send(_pmsg()).status == status


def test_ntfy_network_errors_retry_and_never_leak_the_topic_or_token():
    sender, _ = _ntfy(env={"APEX_NTFY_TOKEN": "tk_secret_token"}, boom=ConnectionError(f"failed to reach ntfy.sh/{TOPIC} with tk_secret_token"))
    r = sender.send(_pmsg())
    assert r.status == "retry" and TOPIC not in r.error and "tk_secret_token" not in r.error and "[secret]" in r.error
    sender, _ = _ntfy(boom=TimeoutError("read timed out"))
    assert sender.send(_pmsg()).status == "retry"


def test_secrets_and_addresses_never_reach_the_outbox_the_logs_or_the_audit_trail(caplog):
    _world()
    secret, token = "app-password-xyz", "tk_secret_token"
    caplog.set_level(logging.DEBUG)
    _notify("sec:1")
    topic = [c for c in cdb.list_contacts(LOCAL_OWNER) if c["channel"] == "PUSH"][0]["address"]
    FakeSMTP.script = {"send": smtplib.SMTPResponseException(421, f"rejected {MY_EMAIL} using {secret}".encode())}
    FakeSMTP.instances = []
    env = {"APEX_SMTP_USERNAME": "apex@example.com", "APEX_SMTP_PASSWORD": secret, "APEX_NTFY_TOKEN": token}
    regs = snd.build_registry(sdb.get_notification_settings(), env=env, smtp_factory=FakeSMTP,
                              http_post=lambda *a, **k: (_ for _ in ()).throw(ConnectionError(f"to {topic} with {token}")))
    worker.run_once(now=NOW, registry=regs, env=env)
    assert {r["status"] for r in _rows()} == {"PENDING"} and all(r["last_error"] for r in _rows())     # both failed transiently, errors recorded
    blob = json.dumps(_rows()) + caplog.text + json.dumps(repo.get_audit_events(limit=200), default=str)
    for bad in (secret, token, MY_EMAIL, topic):
        assert bad not in blob, bad
    assert cdb.address_tag(MY_EMAIL) in blob or "to=" in caplog.text


# ── Unsubscribe tokens ────────────────────────────────────────────────────────

def test_unsubscribe_tokens_are_signed_and_tamper_proof():
    t = snd.sign_unsubscribe_token("alice", 7, "key-1")
    assert snd.verify_unsubscribe_token(t, "key-1") == ("alice", 7)
    payload, sig = t.split(".")
    forged = snd.sign_unsubscribe_token("bob", 7, "key-1").split(".")[0] + "." + sig
    for bad in (t, forged, t[:-2] + "xx", "", "garbage", payload, "a.b", None):
        assert bad is t or snd.verify_unsubscribe_token(bad, "wrong-key") is None
    assert snd.verify_unsubscribe_token(forged, "key-1") is None and snd.verify_unsubscribe_token(t[:-2] + "xx", "key-1") is None
    assert snd.verify_unsubscribe_token("", "key-1") is None and snd.verify_unsubscribe_token("garbage", "key-1") is None


def test_one_click_unsubscribe_opts_out_exactly_one_contact_idempotently_and_audits_once(monkeypatch):
    _world()
    monkeypatch.setenv("APEX_NOTIFY_SIGNING_KEY", "signing-key-123")
    alt = ns.add_alternate_email(ME, ALT)
    email = [c for c in cdb.list_contacts(LOCAL_OWNER) if c["kind"] == "LOGIN"][0]
    token = snd.sign_unsubscribe_token(LOCAL_OWNER, email["id"], "signing-key-123")
    assert ns.unsubscribe_with_token(token) == {"ok": True} and ns.unsubscribe_with_token(token) == {"ok": True}
    after = {c["kind"]: c["opted_out"] for c in cdb.list_contacts(LOCAL_OWNER)}
    assert after == {"LOGIN": 1, "ALTERNATE": 0, "TOPIC": 0} and alt["id"] != email["id"]
    assert [e["operation"] for e in repo.get_audit_events(limit=100)].count("opt_out_notification_contact") == 1
    assert ns.explain_channels(LOCAL_OWNER, "MARKET_EVENT")["EMAIL"] == "OPTED_OUT"                 # the other email contact is unverified
    for bad in ("garbage", token[:-3] + "abc", snd.sign_unsubscribe_token(LOCAL_OWNER, email["id"], "other-key"),
                snd.sign_unsubscribe_token(LOCAL_OWNER, 9999, "signing-key-123"), snd.sign_unsubscribe_token("nobody", email["id"], "signing-key-123")):
        assert ns.unsubscribe_with_token(bad) == {"ok": False}
    monkeypatch.delenv("APEX_NOTIFY_SIGNING_KEY")
    assert ns.unsubscribe_with_token(token) == {"ok": False}                                         # no key configured: nothing is accepted


# ── Alternate-email verification ──────────────────────────────────────────────

def _code_from(sender: snd.FakeSender) -> str:
    import re
    return re.search(r"\b(\d{6})\b", sender.sent[-1].body).group(1)


def test_email_code_flow_hashes_expires_limits_attempts_and_rate_limits_resends():
    _world()
    alt = ns.add_alternate_email(ME, ALT)
    fake = snd.FakeSender("EMAIL")
    reg = {"EMAIL": fake}
    out = ns.start_email_verification(ME, alt["id"], registry=reg, now=NOW)
    code = _code_from(fake)
    assert out["sent"] and out["cooldown_seconds"] == 60 and fake.sent[-1].recipients[0].address == ALT and "verification code" in fake.sent[-1].subject
    stored = cdb.get_contact(LOCAL_OWNER, alt["id"])
    assert code not in json.dumps(stored) and code not in json.dumps(repo.get_audit_events(limit=100), default=str)    # only a hash is stored
    assert len(stored["verification_code_hash"]) == 64 and stored["verified"] == 0
    with pytest.raises(ns.VerificationError, match="please wait"):                                           # 60-second resend cooldown
        ns.start_email_verification(ME, alt["id"], registry=reg, now=NOW + timedelta(seconds=30))
    wrong = f"{(int(code) + 1) % 10**6:06d}"
    for _ in range(4):
        with pytest.raises(ns.VerificationError, match="not right"):
            ns.confirm_email_code(ME, alt["id"], wrong, now=NOW + timedelta(seconds=40))
    assert cdb.get_contact(LOCAL_OWNER, alt["id"])["verification_attempts"] == 4
    ok = ns.confirm_email_code(ME, alt["id"], code, now=NOW + timedelta(seconds=50))                         # the 5th guess still counts and succeeds
    assert ok["verified"] == 1 and ok["verification_code_hash"] is None and ok["verification_attempts"] == 0
    assert "verify_notification_contact" in [e["operation"] for e in repo.get_audit_events(limit=100)]
    assert ns.explain_channels(LOCAL_OWNER, "MARKET_EVENT")["EMAIL"] == "DELIVERABLE" and {c["address"] for c in ns.deliverable_contacts(LOCAL_OWNER, "MARKET_EVENT") if c["channel"] == "EMAIL"} == {MY_EMAIL, ALT}
    with pytest.raises(ns.VerificationError, match="already verified"):
        ns.start_email_verification(ME, alt["id"], registry=reg, now=NOW + timedelta(minutes=5))


def test_too_many_wrong_codes_and_expired_codes_need_a_new_code():
    _world()
    alt = ns.add_alternate_email(ME, ALT)
    fake = snd.FakeSender("EMAIL")
    ns.start_email_verification(ME, alt["id"], registry={"EMAIL": fake}, now=NOW)
    code = _code_from(fake)
    for _ in range(5):
        with pytest.raises(ns.VerificationError, match="not right"):
            ns.confirm_email_code(ME, alt["id"], "000000" if code != "000000" else "111111", now=NOW + timedelta(seconds=5))
    with pytest.raises(ns.VerificationError, match="too many attempts"):
        ns.confirm_email_code(ME, alt["id"], code, now=NOW + timedelta(seconds=6))                          # even the right code is refused now
    with pytest.raises(ns.VerificationError, match="expired"):
        ns.confirm_email_code(ME, alt["id"], code, now=NOW + timedelta(seconds=7))                          # the code was cleared
    ns.start_email_verification(ME, alt["id"], registry={"EMAIL": fake}, now=NOW + timedelta(minutes=2))
    new_code = _code_from(fake)
    with pytest.raises(ns.VerificationError, match="expired"):
        ns.confirm_email_code(ME, alt["id"], new_code, now=NOW + timedelta(minutes=2) + timedelta(minutes=11))   # ttl is 10 minutes
    ns.start_email_verification(ME, alt["id"], registry={"EMAIL": fake}, now=NOW + timedelta(minutes=20))
    assert ns.confirm_email_code(ME, alt["id"], _code_from(fake), now=NOW + timedelta(minutes=21))["verified"] == 1


def test_a_code_that_could_not_be_sent_clears_itself_and_does_not_start_the_cooldown():
    _world()
    alt = ns.add_alternate_email(ME, ALT)
    broken = {"EMAIL": snd.FakeSender("EMAIL", [snd.SendResult("retry", error="smtp down")])}
    with pytest.raises(ns.VerificationError, match="could not send the code"):
        ns.start_email_verification(ME, alt["id"], registry=broken, now=NOW)
    c = cdb.get_contact(LOCAL_OWNER, alt["id"])
    assert c["verification_code_hash"] is None and c["last_code_sent_at"] is None
    ns.start_email_verification(ME, alt["id"], registry={"EMAIL": snd.FakeSender("EMAIL")}, now=NOW + timedelta(seconds=1))   # no 60s wait


def test_verification_is_refused_for_the_login_email_or_when_email_is_off_or_for_someone_elses_contact():
    _world()
    login = [c for c in cdb.list_contacts(LOCAL_OWNER) if c["kind"] == "LOGIN"][0]
    alt = ns.add_alternate_email(ME, ALT)
    reg = {"EMAIL": snd.FakeSender("EMAIL")}
    with pytest.raises(ns.VerificationError, match="only an alternate"):
        ns.start_email_verification(ME, login["id"], registry=reg)
    other = _second_owner()
    with pytest.raises(cdb.ContactNotFound):
        ns.start_email_verification(other, alt["id"], registry=reg)
    with pytest.raises(cdb.ContactNotFound):
        ns.confirm_email_code(other, alt["id"], "123456")
    sdb.update_notification_settings(updated_by="t", email_enabled=0)
    with pytest.raises(ns.VerificationError, match="not set up"):
        ns.start_email_verification(ME, alt["id"], registry=reg)


# ── Test sends and push confirmation ──────────────────────────────────────────

def test_a_push_test_then_i_got_it_verifies_the_push_contact():
    _world(push_verified=False)
    assert ns.explain_channels(LOCAL_OWNER, "MARKET_EVENT")["PUSH"] == "UNVERIFIED"
    with pytest.raises(ns.VerificationError, match="send a test notification first"):
        ns.confirm_push_received(ME)
    push = {"PUSH": snd.FakeSender("PUSH")}
    assert ns.send_test(ME, "push", registry=push) == {"sent": True, "channel": "PUSH"}
    assert push["PUSH"].sent[0].is_test and push["PUSH"].sent[0].recipients[0].address.startswith("apex-")
    assert ns.confirm_push_received(ME)["verified"] == 1 and ns.explain_channels(LOCAL_OWNER, "MARKET_EVENT")["PUSH"] == "DELIVERABLE"
    ops = [e["operation"] for e in repo.get_audit_events(limit=100)]
    assert "send_test_notification" in ops and "verify_notification_contact" in ops
    with pytest.raises(ns.VerificationError, match="wait a moment"):
        ns.send_test(ME, "PUSH", registry=push)                                                          # at most one every 30 seconds


def test_confirming_push_is_refused_when_the_test_was_long_ago_and_a_failed_test_does_not_count():
    _world(push_verified=False)
    failing = {"PUSH": snd.FakeSender("PUSH", [snd.SendResult("retry", error="down")])}
    with pytest.raises(ns.VerificationError, match="could not be sent"):
        ns.send_test(ME, "PUSH", registry=failing)
    with pytest.raises(ns.VerificationError, match="send a test notification first"):
        ns.confirm_push_received(ME)
    ns._last_test.clear()
    ns.send_test(ME, "PUSH", registry={"PUSH": snd.FakeSender("PUSH")})
    with pytest.raises(ns.VerificationError, match="send a test notification first"):
        ns.confirm_push_received(ME, now=datetime.now(timezone.utc) + timedelta(minutes=ns.PUSH_CONFIRM_WINDOW_MINUTES + 1))


def test_an_email_test_goes_only_to_verified_not_opted_out_addresses_and_unavailable_channels_are_refused():
    _world()
    alt = ns.add_alternate_email(ME, ALT)
    email = {"EMAIL": snd.FakeSender("EMAIL")}
    ns.send_test(ME, "EMAIL", registry=email)
    assert [r.address for r in email["EMAIL"].sent[0].recipients] == [MY_EMAIL] and alt["verified"] == 0       # the unverified alternate is not tested
    for ch in ("WHATSAPP", "SMS", "SLACK"):
        with pytest.raises(cdb.ChannelUnavailable, match="not available yet"):
            ns.send_test(ME, ch, registry=email)
    sdb.update_notification_settings(updated_by="t", push_enabled=0)
    with pytest.raises(ns.VerificationError, match="not switched on"):
        ns.send_test(ME, "PUSH", registry=email)


# ── Quiet-hours maths, links, CLI ─────────────────────────────────────────────

def test_personal_filters_and_quiet_hour_boundaries():
    prefs = udb.UserNotificationPrefs(min_severity_threshold=3, quiet_hours_start="22:00", quiet_hours_end="07:00")
    at = lambda h, m=0: datetime(2026, 10, 12, h, m, tzinfo=CHI)
    assert udb.evaluate_personal_filters(prefs, severity=2, now_local=at(12)).reason == "BELOW_SEVERITY"
    assert udb.evaluate_personal_filters(prefs, severity=None, now_local=at(12)).ok
    d = udb.evaluate_personal_filters(prefs, severity=4, now_local=at(22))
    assert (d.ok, d.reason, d.defer_until) == (False, "QUIET_HOURS", datetime(2026, 10, 13, 7, 0, tzinfo=CHI))
    assert udb.evaluate_personal_filters(prefs, severity=4, now_local=at(6, 59)).defer_until == at(7)
    assert udb.evaluate_personal_filters(prefs, severity=4, now_local=at(7)).ok and udb.evaluate_personal_filters(prefs, severity=4, now_local=at(21, 59)).ok
    day = udb.UserNotificationPrefs(quiet_hours_start="09:00", quiet_hours_end="17:00")
    assert udb.next_quiet_end(at(10), "09:00", "17:00") == at(17) and udb.next_quiet_end(at(18), "09:00", "17:00") is None
    assert day.min_severity_threshold == 3


def test_links_are_absolute_only_when_the_app_url_is_configured():
    s = {"app_base_url": "https://apex.example.com"}
    assert ns.full_link(s, "/Predictions") == "https://apex.example.com/Predictions" and ns.full_link(s, "Predictions") == "https://apex.example.com/Predictions"
    assert ns.full_link(s, "https://other.example/x") == "https://other.example/x" and ns.full_link({"app_base_url": ""}, "/Predictions") is None and ns.full_link(s, None) is None


def test_the_cli_reports_status_and_delivers_with_no_rows(capsys):
    from scripts import send_notifications
    assert send_notifications.main(["--status"]) == 0
    assert "PENDING" in capsys.readouterr().out
    assert send_notifications.main(["--limit", "5"]) == 0
    assert "'claimed': 0" in capsys.readouterr().out
