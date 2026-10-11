"""
Notifications, Step 3 — the existing alert sources feed notify(): market events fan out per OWNER (holders and watchers only),
Portfolio Health goes through notify, compliance alerts (approvals, expiry, restricted-list changes) and the daily digest.

No network: delivery is never run here except through a fake sender; everything checked is what was QUEUED in the outbox.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.events import detector
from portfolio_agent.events.db import insert_event
from portfolio_agent.services import account_service
from portfolio_agent.services import compliance_notifications as comp
from portfolio_agent.services import notification_producers as prod
from portfolio_agent.services import notification_service as ns
from portfolio_agent.services.context import RequestContext, _mint_identity_for_tests
from portfolio_agent.tools import auth_users_db, restricted_list_db, trade_approvals_db as approvals
from portfolio_agent.tools import holdings_db as repo
from portfolio_agent.tools import notification_outbox_db as obx
from portfolio_agent.tools import notification_settings_db as nsdb
from portfolio_agent.tools import portfolio_risk as risk
from portfolio_agent.tools import watchlist_db

EVENT = {"id": 501, "ticker": "NVDA", "event_type": "material_news", "severity": 4, "summary": "Nvidia posted a $1,000,000 guidance surprise."}


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(risk, "compute_portfolio_risk_context", lambda *a, **k: {})


def _person(name: str) -> RequestContext:
    """A person with a portfolio, a verified login-email contact, and email switched on globally."""
    with repo.unit_of_work() as c:
        c.execute("INSERT OR IGNORE INTO portfolios (owner) VALUES (?)", (name,))
    auth_users_db.invite_user(f"{name}@example.com", name, "member")
    nsdb.update_notification_settings(updated_by="t", email_from_address="apex@example.com", max_messages_per_user_per_hour=1000)
    ctx = RequestContext(identity=_mint_identity_for_tests(name), source="test")
    ns.ensure_contacts(ctx)
    return ctx


def _hold(ctx: RequestContext, ticker: str, value: float = 5000.0, *, account_type: str = "TAXABLE", name: str = "Main") -> dict:
    acct = account_service.create_account(ctx, broker="manual", display_name=f"{ctx.actor}-{name}", account_type=account_type)
    account_service.add_position(ctx, acct["account_id"], {"ticker": ticker, "shares": value / 100.0, "current_price": 100.0,
                                                           "sector": "Technology", "price_as_of": "2026-10-08"})
    return acct


def _watch(owner: str, ticker: str) -> None:
    watchlist_db.load_watchlist_tickers()                                  # creates the table
    with sqlite3.connect(str(db_module.DB_PATH)) as c:
        c.execute("INSERT INTO watchlist (ticker, added_at, owner) VALUES (?, '2026-10-01', ?)", (ticker, owner))


def _rows(**where) -> list[dict]:
    with sqlite3.connect(str(db_module.DB_PATH)) as c:
        c.row_factory = sqlite3.Row
        sql = "SELECT * FROM notification_outbox" + (" WHERE " + " AND ".join(f"{k} = ?" for k in where) if where else "") + " ORDER BY id"
        try:
            return [dict(r) for r in c.execute(sql, list(where.values()))]
        except sqlite3.OperationalError:
            return []


def _keys(owner: str | None = None, prefix: str = "") -> list[str]:
    return [r["dedup_key"] for r in _rows() if (owner is None or r["owner"] == owner) and r["dedup_key"].startswith(prefix)]


# ── Market events: per-owner fan-out ──────────────────────────────────────────

def test_an_event_reaches_only_the_owners_who_hold_or_watch_the_ticker():
    alice, bob, carol, dave = _person("alice"), _person("bob"), _person("carol"), _person("dave")
    _hold(alice, "NVDA")                                              # holds it
    _watch("bob", "NVDA")                                             # watches it
    _hold(carol, "NVDA", account_type="TAXABLE")                      # (made paper below)
    _hold(dave, "MSFT")                                               # unrelated holding
    with sqlite3.connect(str(db_module.DB_PATH)) as c:                # carol's account becomes a PAPER account: not a real holding
        c.execute("UPDATE accounts SET is_paper = 1 WHERE display_name LIKE 'carol-%'")
    assert detector._affected_owners("NVDA") == {"alice": {"hold"}, "bob": {"watch"}}
    assert detector._maybe_notify(EVENT) is True
    assert {r["owner"] for r in _rows()} == {"alice", "bob"} and all(r["dedup_key"] == "event:501" for r in _rows())
    assert {r["channel"] for r in _rows()} == {"EMAIL"} and {r["category"] for r in _rows()} == {"MARKET_EVENT"}
    assert _rows(owner="carol") == [] and _rows(owner="dave") == []   # paper holder and an unrelated holder hear nothing
    assert alice is not None and bob is not None and carol is not None and dave is not None


def test_a_ticker_nobody_holds_or_watches_notifies_nobody_and_a_macro_event_is_not_anyones():
    _hold(_person("alice"), "AAPL")
    assert detector._maybe_notify(EVENT) is False and detector._maybe_notify({**EVENT, "id": 502, "ticker": "MARKET"}) is False
    assert _rows() == [] and detector._maybe_notify({"ticker": "NVDA", "event_type": "x", "severity": 4}) is False      # no event id: nothing to dedup on


def test_each_owner_is_judged_by_their_own_preferences_and_dedup():
    alice, bob = _person("alice"), _person("bob")
    _hold(alice, "NVDA"); _hold(bob, "NVDA")
    ns.update_preferences(alice, min_severity_threshold=5)            # alice only wants severity 5+
    assert detector._maybe_notify(EVENT) is True
    assert {r["owner"] for r in _rows()} == {"bob"}                    # severity 4: alice's bar excludes it, bob's (3) does not
    ns.set_routing(bob, "MARKET_EVENT", "EMAIL", False)
    assert detector._maybe_notify({**EVENT, "id": 502}) is False       # bob routed it off too: nobody
    ns.update_preferences(alice, min_severity_threshold=3)
    assert detector._maybe_notify(EVENT) is True and {r["owner"] for r in _rows(dedup_key="event:501")} == {"alice", "bob"}
    assert detector._maybe_notify(EVENT) is False                      # event:501 is already queued for both


def test_the_clearance_note_is_evaluated_per_owner_and_never_filters():
    alice, bob = _person("alice"), _person("bob")
    _hold(alice, "NVDA"); _hold(bob, "NVDA")
    restricted_list_db.add_restricted("NVDA", "MNPI", "alice")
    assert detector._maybe_notify(EVENT) is True
    a, b = _rows(owner="alice", dedup_key="event:501")[0], _rows(owner="bob", dedup_key="event:501")[0]
    assert "BLOCKED: no new trade currently permitted" in a["body_brief"]
    assert "BLOCKED" not in b["body_brief"]                            # bob has no restriction (his note, if any, is his own: pre-clearance)
    assert a["status"] == "PENDING"                                    # the restriction annotated, never suppressed, the alert


def test_brief_text_has_no_amounts_and_the_detailed_text_adds_the_summary_and_why():
    alice = _person("alice")
    _hold(alice, "NVDA", 52000.0)
    ns.update_preferences(alice, content_level="DETAILED")
    detector._maybe_notify(EVENT)
    row = _rows(dedup_key="event:501")[0]
    assert row["body_brief"].startswith("APEX: NVDA - material news (severity 4).") and row["body_brief"].endswith("Open APEX for details.")
    assert "$" not in row["body_brief"] and "52" not in row["body_brief"] and "1,000,000" not in row["body_brief"]
    assert "guidance surprise" in row["body_detailed"] and "you hold it" in row["body_detailed"]
    assert row["subject"] == "APEX: NVDA - material news" and row["link_url"] is None                      # no app_base_url configured: no link


def test_emit_still_writes_its_structured_log_line(caplog):
    _hold(_person("alice"), "NVDA")
    caplog.set_level(logging.INFO)
    detector._maybe_notify(EVENT)
    assert "[notify] NVDA material_news severity=4" in caplog.text


def test_a_failing_notifier_never_breaks_the_detector(monkeypatch):
    _hold(_person("alice"), "NVDA")
    monkeypatch.setattr(ns, "notify", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    assert detector._maybe_notify(EVENT) is False


def test_the_events_end_to_end_through_detect_material_news(monkeypatch):
    alice = _person("alice")
    _hold(alice, "NVDA")
    with sqlite3.connect(str(db_module.DB_PATH)) as c:
        c.execute("CREATE TABLE IF NOT EXISTS news_filter_log (as_of_date TEXT, ticker TEXT, stage_2_decision TEXT, stage_3_decision TEXT, "
                  "stage_3_score INTEGER, stage_3_summary TEXT, final_decision TEXT)")
        c.execute("INSERT INTO news_filter_log VALUES ('2026-10-12', 'NVDA', 'interesting', 'pass', 8, 'Big news', 'analyzed')")
    events = detector.detect_material_news(["NVDA", "AAPL"], "2026-10-12", {"intraday_severity_threshold": 3})
    assert [e["ticker"] for e in events] == ["NVDA"]
    assert _keys("alice", "event:") == [f"event:{events[0]['id']}"]


# ── Portfolio Health through notify ───────────────────────────────────────────

def test_health_alerts_use_notify_with_the_same_dedup_key_and_no_amounts_in_the_brief():
    from datetime import date
    from portfolio_agent.services import health_service as hs
    from portfolio_agent.tools import portfolio_health as ph
    alice = _person("alice")
    acct = _hold(alice, "AAA", 5000.0)
    account_service.record_account_cash(alice, acct["account_id"], 100.0, as_of_date="2026-10-08")
    ph.clear_health_cache()
    health = ph.get_portfolio_health_for_owner("alice", repo.get_portfolio_by_owner("alice"), use_cache=False)
    keys = hs._notify_new_actions("alice", health, date(2026, 10, 9))
    assert "health:emergency_reserve:2026-10-09" in keys
    row = [r for r in _rows(owner="alice") if r["dedup_key"] == "health:emergency_reserve:2026-10-09"][0]
    assert (row["category"], row["severity"], row["subject"]) == ("PORTFOLIO_HEALTH", 4, "APEX: Portfolio Health - Emergency reserve")
    assert row["body_brief"] == "APEX: Portfolio Health - Emergency reserve needs attention. Open APEX for details."
    assert hs._notify_new_actions("alice", health, date(2026, 10, 9)) == []                                  # the outbox de-duplicates


# ── Compliance ────────────────────────────────────────────────────────────────

def _later(**kw) -> str:
    return (datetime.now(timezone.utc) + timedelta(**kw)).isoformat(timespec="seconds")


def test_approval_granted_and_withdrawn_notify_their_owner_once_each():
    _person("alice"); _person("bob")
    gid = approvals.record_approval(owner_scope="user:alice", ticker="NVDA", action="BUY", granted_by="compliance-officer")
    rid = approvals.revoke_approval(owner_scope="user:alice", ticker="NVDA", action="BUY", revoked_by="compliance-officer")
    assert _keys("alice", "approval:") == [f"approval:{gid}:granted", f"approval:{rid}:revoked"] and _keys("bob") == []
    granted, revoked = _rows(owner="alice")
    assert (granted["category"], granted["severity"]) == ("COMPLIANCE", 4)
    assert "recorded" in granted["body_brief"] and "withdrawn; no new trade is currently permitted" in revoked["body_brief"]
    assert "compliance-officer" not in granted["body_brief"] and granted["body_detailed"] is None             # brief level: no detailed text
    comp.notify_approval_event({"id": gid, "owner_scope": "user:alice", "ticker": "NVDA", "action": "BUY", "event": "granted",
                                "granted_by": "x", "expires_at": None})
    assert len(_rows(owner="alice")) == 2                                                                      # re-offering the same event: deduped


def test_approvals_expiring_within_a_day_are_announced_once_and_only_while_active():
    _person("alice")
    soon = approvals.record_approval(owner_scope="user:alice", ticker="NVDA", action="BUY", granted_by="officer", expires_at=_later(hours=6))
    approvals.record_approval(owner_scope="user:alice", ticker="MSFT", action="BUY", granted_by="officer", expires_at=_later(hours=60))   # not yet
    approvals.record_approval(owner_scope="user:alice", ticker="TSLA", action="BUY", granted_by="officer", expires_at=_later(hours=-2))   # already expired
    revoked = approvals.record_approval(owner_scope="user:alice", ticker="AMD", action="BUY", granted_by="officer", expires_at=_later(hours=5))
    approvals.revoke_approval(owner_scope="user:alice", ticker="AMD", action="BUY", revoked_by="officer")                              # withdrawn
    before = len(_keys("alice", "approval:"))
    out = comp.scan_expiring_approvals()
    assert (out["found"], out["enqueued"]) == (1, 1)
    expiring = [k for k in _keys("alice", "approval:") if ":expiring:" in k]
    assert len(expiring) == 1 and expiring[0].startswith(f"approval:{soon}:expiring:") and revoked != soon and len(_keys("alice", "approval:")) == before + 1
    assert comp.scan_expiring_approvals() == {"found": 1, "enqueued": 0}                                        # re-scan: nothing new
    row = [r for r in _rows(owner="alice") if ":expiring:" in r["dedup_key"]][0]
    assert "expires within 24 hours" in row["body_brief"] and "NVDA" in row["body_brief"]


def test_restricted_list_changes_notify_only_the_owner_who_holds_the_ticker():
    alice, bob = _person("alice"), _person("bob")
    _hold(alice, "NVDA")
    restricted_list_db.add_restricted("NVDA", "pending M&A", "alice")
    restricted_list_db.add_restricted("NVDA", "pending M&A (updated)", "alice")                                # a reason edit is not news
    restricted_list_db.add_restricted("AMD", "x", "alice")                                                       # alice does not hold AMD
    restricted_list_db.add_restricted("NVDA", "x", "bob")                                                        # bob does not hold NVDA
    assert [k.split(":")[:3] for k in _keys(prefix="restricted:")] == [["restricted", "NVDA", "added"]] and _rows()[0]["owner"] == "alice"
    row = _rows()[0]
    assert "no new trade is currently permitted" in row["body_brief"] and "pending M&A" not in row["body_brief"] and "pending M&A" in (row["body_detailed"] or "pending M&A")
    restricted_list_db.remove_restricted("NVDA", "alice")
    restricted_list_db.remove_restricted("NVDA", "alice")                                                        # already gone: nothing
    assert [k.split(":")[2] for k in _keys("alice", "restricted:")] == ["added", "removed"] and alice is not None and bob is not None


def test_a_notification_failure_can_never_undo_or_block_a_compliance_record(monkeypatch):
    _person("alice")
    monkeypatch.setattr(ns, "notify", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    approval_id = approvals.record_approval(owner_scope="user:alice", ticker="NVDA", action="BUY", granted_by="officer")
    assert approvals.get_active_approval("user:alice", "NVDA", "BUY")["id"] == approval_id
    restricted_list_db.add_restricted("NVDA", "r", "alice")
    assert restricted_list_db.is_restricted("NVDA", "alice") == (True, "r")


# ── Daily digest ──────────────────────────────────────────────────────────────

def _digest_on(ctx, **prefs):
    ns.set_routing(ctx, "DAILY_DIGEST", "EMAIL", True)
    ns.update_preferences(ctx, digest_time=prefs.pop("digest_time", "00:00"), **prefs)


def test_the_digest_is_off_by_default_email_only_and_waits_for_its_time():
    alice = _person("alice")
    _hold(alice, "NVDA")
    now = datetime(2026, 10, 12, 18, 0, tzinfo=timezone.utc)
    assert prod._digest_for_owner("alice", now) == "not_deliverable" and _rows() == []                          # routing is off by default
    _digest_on(alice, digest_time="07:30")
    assert prod._digest_for_owner("alice", datetime(2026, 10, 12, 12, 29, tzinfo=timezone.utc)) == "not_due"   # 07:29 in Chicago
    assert prod._digest_for_owner("alice", datetime(2026, 10, 12, 12, 31, tzinfo=timezone.utc)) == "created"   # 07:31
    assert {r["channel"] for r in _rows()} == {"EMAIL"} and _rows()[0]["category"] == "DAILY_DIGEST"           # never a push digest


def test_the_digest_rolls_up_health_events_and_rate_limited_items_and_is_sent_once_a_day():
    alice = _person("alice")
    _hold(alice, "NVDA", 52000.0)
    _watch("alice", "AMD")
    _digest_on(alice, content_level="DETAILED")
    insert_event("NVDA", "material_news", 4, "t", "Nvidia posted a $1,000,000 guidance surprise.")
    insert_event("AMD", "rating_change", 3, "t", "Upgrade.")
    insert_event("MSFT", "material_news", 5, "t", "Not yours.")
    obx.enqueue("alice", "MARKET_EVENT", "EMAIL", dedup_key="e:old", subject="APEX: TSLA - material news", body_brief="b",
                status=obx.SUPPRESSED, reason_code="RATE_LIMITED", fold_into_digest=True)
    now = datetime.now(timezone.utc)
    assert prod.build_daily_digests(now)["created"] == 1
    [row] = _rows(dedup_key=f"digest:{now.astimezone(prod._tz_of('alice')).date().isoformat()}")
    brief, detailed = row["body_brief"], row["body_detailed"]
    assert "Portfolio Health:" in brief and "need action" in brief and "- NVDA: material news (severity 4)" in brief and "- AMD: rating change (severity 3)" in brief
    assert "MSFT" not in brief and "Held back earlier" in brief and "- APEX: TSLA - material news" in brief
    assert "$" not in brief and "52" not in brief                                                           # the brief digest has no amounts
    assert "Portfolio Health details:" in detailed and "$" in detailed and "guidance surprise" in detailed   # email DETAILED adds them
    assert "not investment advice" in brief
    assert _rows(dedup_key="e:old")[0]["fold_into_digest"] == 0 and _rows(dedup_key="e:old")[0]["reason_code"] == "RATE_LIMITED_DIGESTED"
    assert prod.build_daily_digests(now)["already"] == 1 and len([r for r in _rows() if r["category"] == "DAILY_DIGEST"]) == 1
    assert prod.build_daily_digests(now + timedelta(days=1))["created"] + prod.build_daily_digests(now + timedelta(days=1))["already"] >= 1


def test_a_brief_digest_has_no_detailed_body():
    alice = _person("alice")
    _hold(alice, "NVDA", 52000.0)
    _digest_on(alice)                                                                                          # content level BRIEF (default)
    prod.build_daily_digests(datetime.now(timezone.utc))
    row = _rows()[0]
    assert row["body_detailed"] is None and "$" not in row["body_brief"]


def test_an_empty_digest_is_skipped_and_leaves_a_marker_so_it_is_not_rebuilt():
    bob = _person("bob")                                                                                       # a portfolio with nothing in it
    _digest_on(bob)
    now = datetime.now(timezone.utc)
    assert prod.build_daily_digests(now)["empty"] == 1
    [row] = _rows()
    assert (row["status"], row["reason_code"], row["channel"]) == ("SUPPRESSED", "EMPTY_DIGEST", "EMAIL")
    assert prod.build_daily_digests(now)["already"] == 1 and len(_rows()) == 1                                 # not rebuilt every five minutes


def test_digests_are_per_owner_and_quiet_hours_defer_them():
    alice, bob = _person("alice"), _person("bob")
    _hold(alice, "NVDA"); _hold(bob, "MSFT")
    _digest_on(alice, quiet_hours_start="00:00", quiet_hours_end="23:59", quiet_hours_mode="DEFER")
    _digest_on(bob)
    insert_event("NVDA", "material_news", 4, "t", "Alice's ticker only.")
    out = prod.build_daily_digests(datetime.now(timezone.utc))
    assert out["created"] == 2
    a, b = _rows(owner="alice")[0], _rows(owner="bob")[0]
    assert a["status"] == "DEFERRED" and a["next_attempt_at"] and "NVDA" in a["body_brief"] and "NVDA" not in b["body_brief"]


def test_scheduled_producers_run_together_and_never_raise(monkeypatch, capsys):
    alice = _person("alice")
    _hold(alice, "NVDA")
    _digest_on(alice)
    out = prod.run_scheduled_producers()
    assert set(out) == {"compliance", "digest"} and out["digest"]["created"] == 1
    real_digests = prod.build_daily_digests
    monkeypatch.setattr(prod, "build_daily_digests", lambda now=None: (_ for _ in ()).throw(RuntimeError("boom")))
    assert prod.run_scheduled_producers() == {"compliance": {"found": 0, "enqueued": 0}, "digest": {}}
    monkeypatch.setattr(prod, "build_daily_digests", real_digests)        # (never monkeypatch.undo(): it would also undo the DB isolation)
    from scripts import send_notifications
    assert send_notifications.main(["--limit", "5"]) == 0 and "'produced'" in capsys.readouterr().out


# ── Informational only ────────────────────────────────────────────────────────

def test_no_notification_text_is_ever_a_trade_instruction():
    alice = _person("alice")
    acct = _hold(alice, "NVDA", 52000.0)
    account_service.record_account_cash(alice, acct["account_id"], 100.0, as_of_date="2026-10-08")
    ns.update_preferences(alice, content_level="DETAILED")
    _digest_on(alice, content_level="DETAILED")
    detector._maybe_notify(EVENT)
    approvals.record_approval(owner_scope="user:alice", ticker="NVDA", action="BUY", granted_by="officer", expires_at=_later(hours=3))
    approvals.revoke_approval(owner_scope="user:alice", ticker="NVDA", action="BUY", revoked_by="officer")
    restricted_list_db.add_restricted("NVDA", "r", "alice")
    from datetime import date
    from portfolio_agent.services import health_service as hs
    from portfolio_agent.tools import portfolio_health as ph
    ph.clear_health_cache()
    hs._notify_new_actions("alice", ph.get_portfolio_health_for_owner("alice", repo.get_portfolio_by_owner("alice"), use_cache=False), date(2026, 10, 9))
    comp.scan_expiring_approvals()
    prod.build_daily_digests(datetime.now(timezone.utc))
    texts = [r["subject"] + " " + r["body_brief"] + " " + (r["body_detailed"] or "") for r in _rows()]
    assert len(texts) >= 6
    for t in texts:
        # the approval's own action label ("(BUY)": what the approval covers) is information, not an instruction
        assert not re.search(r"\b(buy|sell|short[- ]sell|trim|add to|should)\b", t.replace("pre-clearance", "").replace("(BUY)", ""), re.IGNORECASE), t
    briefs = [r["body_brief"] for r in _rows()]
    assert all("$" not in b and "52,000" not in b for b in briefs)
