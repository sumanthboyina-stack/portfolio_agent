"""
Step 1 — simple risk profile, target allocation, emergency reserve -> EffectivePolicy.

Everything is in the database (no config files): the global settings are seeded from code constants and versioned like
engine_settings; a person's profile, reserve, custom allocation and goals are private and owner-scoped; the service is
the only writer (RequestContext + audit event); global settings are admin-only (checked server-side).
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
import types
from pathlib import Path

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.domain import LOCAL_OWNER, UserProfile
from portfolio_agent.services import investor_service as svc
from portfolio_agent.services.context import RequestContext, _mint_identity_for_tests, local_context
from portfolio_agent.tools import auth_users_db, user_profile_db
from portfolio_agent.tools import holdings_db as repo
from portfolio_agent.tools import investor_profile_db as pdb
from portfolio_agent.tools import investor_settings_db as sdb
from portfolio_agent.tools.portfolio_policy import effective_policy_from_profile, get_effective_policy
from portfolio_agent.tools.risk_quiz import more_conservative, score_quiz

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")


ME = local_context("test")                         # LOCAL_OWNER


def _ctx(owner: str) -> RequestContext:
    return RequestContext(identity=_mint_identity_for_tests(owner), source="test")


def _make_admin(owner: str = LOCAL_OWNER, *, enabled: bool = True, role: str = "admin") -> None:
    auth_users_db.invite_user(f"{owner}@example.com", owner, role)
    if not enabled:
        auth_users_db.disable_user(f"{owner}@example.com")


def _ops(**kw) -> list[str]:
    return [e["operation"] for e in repo.get_audit_events(limit=200, **kw)]


def _answers(horizon=None, drop=None, priority=None) -> dict:
    return {"horizon": horizon, "drop": drop, "priority": priority}


# ── Defaults: no row, no input ────────────────────────────────────────────────

def test_everyone_starts_moderate_with_the_3000_default_and_no_row():
    state = svc.get_investor_state(ME)
    assert (state["profile"], state["source"], state["is_default"]) == ("MODERATE", "DEFAULT", True)
    assert (state["emergency_reserve_amount"], state["emergency_reserve_source"]) == (3000.0, "DEFAULT")
    assert state["custom_allocation"] is None and state["above_cap"] is False
    assert pdb.get_active_investor_profile(LOCAL_OWNER) is None                      # nothing was written

    p = get_effective_policy(ME)
    assert (p.profile, p.profile_source, p.emergency_reserve_amount, p.emergency_reserve_source) == \
           ("MODERATE", "DEFAULT", 3000.0, "DEFAULT")
    assert p.target_allocation["US_EQUITY"] == {"target": 45.0, "min": 40.0, "max": 50.0}
    assert (p.max_leveraged_inverse_pct, p.limits_source) == (5.0, "profile")        # a brand-new user: profile limits
    assert (p.max_sector_pct, p.max_issuer_pct, p.max_position_pct, p.max_candidate_pct_of_cash) == (25.0, 12.0, 12.0, 40.0)
    assert "ETN" in p.allowed_instrument_types and p.allocation_source == "PROFILE"


def test_global_settings_are_seeded_exactly_as_specified_and_versioned():
    profiles = sdb.get_risk_profile_settings()
    assert list(profiles) == ["CONSERVATIVE", "MODERATE", "AGGRESSIVE"]
    tgt = lambda p: {c: b["target"] for c, b in profiles[p]["allocation"].items()}
    assert tgt("CONSERVATIVE") == {"US_EQUITY": 25, "INTL_EQUITY": 5, "FIXED_INCOME": 55, "CASH": 15}
    assert tgt("MODERATE") == {"US_EQUITY": 45, "INTL_EQUITY": 15, "FIXED_INCOME": 33, "CASH": 7}
    assert tgt("AGGRESSIVE") == {"US_EQUITY": 62, "INTL_EQUITY": 25, "FIXED_INCOME": 10, "CASH": 3}
    mod = profiles["MODERATE"]["allocation"]
    assert (mod["US_EQUITY"]["min"], mod["US_EQUITY"]["max"], mod["CASH"]["min"], mod["CASH"]["max"]) == (40, 50, 4, 10)
    assert [(profiles[p]["max_position_pct"], profiles[p]["max_issuer_pct"], profiles[p]["max_sector_pct"],
             profiles[p]["max_leveraged_inverse_pct"], profiles[p]["max_candidate_pct_of_cash"]) for p in profiles] == \
           [(8, 8, 20, 0, 40), (12, 12, 25, 5, 40), (20, 20, 35, 15, 40)]
    assert "ETN" not in profiles["CONSERVATIVE"]["allowed_instrument_types"] and "ETN" in profiles["MODERATE"]["allowed_instrument_types"]
    assert all(sum(b["target"] for b in r["allocation"].values()) == 100 for r in profiles.values())

    ps = sdb.get_portfolio_settings()
    assert (ps["default_emergency_reserve_amount"], ps["version"], ps["updated_by"]) == (3000.0, 1, None)
    assert sdb.get_quiz_settings()["quiz_version"] == 1
    assert all(r["version"] == 1 for r in profiles.values())


# ── Emergency reserve ─────────────────────────────────────────────────────────

def test_user_reserve_overrides_default_and_none_goes_back_to_it():
    svc.set_emergency_reserve(ME, 8000, reason="bigger cushion")
    s = svc.get_investor_state(ME)
    assert (s["emergency_reserve_amount"], s["emergency_reserve_source"]) == (8000.0, "USER")
    assert (get_effective_policy(ME).emergency_reserve_amount, get_effective_policy(ME).emergency_reserve_source) == (8000.0, "USER")
    assert svc.get_investor_state(ME)["profile"] == "MODERATE" and svc.get_investor_state(ME)["source"] == "DEFAULT"
    svc.set_emergency_reserve(ME, None)
    s = svc.get_investor_state(ME)
    assert (s["emergency_reserve_amount"], s["emergency_reserve_source"]) == (3000.0, "DEFAULT")
    assert svc.set_emergency_reserve(ME, None) == pdb.get_active_investor_profile(LOCAL_OWNER)     # no-op, no extra row
    svc.set_emergency_reserve(ME, 0)                                                              # zero is a real choice
    assert svc.get_investor_state(ME)["emergency_reserve_amount"] == 0.0


def test_admin_changing_the_default_moves_users_without_their_own_value_only():
    _make_admin()
    alice, bob = _ctx("alice"), _ctx("bob")
    svc.set_emergency_reserve(bob, 1500)
    before = (get_effective_policy(alice).fingerprint, get_effective_policy(bob).fingerprint)
    svc.update_portfolio_settings(ME, default_emergency_reserve_amount=5000, reason="raise the default")
    assert svc.get_investor_state(alice)["emergency_reserve_amount"] == 5000.0
    assert svc.get_investor_state(bob)["emergency_reserve_amount"] == 1500.0
    assert get_effective_policy(alice).fingerprint != before[0] and get_effective_policy(bob).fingerprint == before[1]
    assert sdb.get_portfolio_settings()["version"] == 2 and sdb.get_portfolio_settings()["updated_by"] == LOCAL_OWNER


@pytest.mark.parametrize("bad", [-1, -0.01, float("nan"), float("inf"), "lots"])
def test_bad_reserve_amounts_are_rejected(bad):
    with pytest.raises(ValueError):
        svc.set_emergency_reserve(ME, bad)
    assert pdb.get_active_investor_profile(LOCAL_OWNER) is None


def test_reserve_is_usd_only():
    user_profile_db.update_user_profile_for(ME, base_currency="EUR")
    with pytest.raises(ValueError, match="USD"):
        svc.set_emergency_reserve(ME, 1000)
    with pytest.raises(ValueError):
        sdb.update_portfolio_settings(updated_by="t", default_emergency_reserve_amount=-5)


# ── Quiz scoring (pure) ───────────────────────────────────────────────────────

def test_quiz_cap_preference_and_unanswered_are_scored_as_specified():
    q = sdb.get_quiz_settings()
    r = score_quiz(_answers(), q)                                                    # skip everything
    assert (r.profile, r.cap_profile, r.cap_answered, r.preference_profile, r.preference_points) == \
           ("MODERATE", "MODERATE", False, "MODERATE", 2)
    assert set(r.unanswered) == {"horizon", "drop", "priority"} and r.quiz_version == 1

    table = {                                                    # (horizon, drop, priority) -> result
        ("lt3", "buy", "grow"): "CONSERVATIVE",                  # cap CONSERVATIVE beats an aggressive preference
        ("3to10", "buy", "grow"): "MODERATE",                    # cap MODERATE
        ("10plus", "buy", "grow"): "AGGRESSIVE",
        ("10plus", "sell", "protect"): "CONSERVATIVE",           # preference 0 beats an aggressive cap
        ("10plus", "sell", "balance"): "CONSERVATIVE",           # sum 1
        ("10plus", "wait", "balance"): "MODERATE",               # sum 2
        ("10plus", "buy", "balance"): "MODERATE",                # sum 3
        ("10plus", "wait", "grow"): "MODERATE",                  # sum 3
        (None, "buy", "grow"): "MODERATE",                       # Q1 skipped -> middle answer -> MODERATE cap
        ("10plus", None, None): "MODERATE",                      # prefs skipped -> middle (sum 2)
        ("10plus", "sell", None): "CONSERVATIVE",                # wait scored 1 for Q3 -> sum 1
    }
    for (h, d, p), expected in table.items():
        assert score_quiz(_answers(h, d, p), q).profile == expected, (h, d, p)
    r = score_quiz(_answers("10plus", "sell", "grow"), q)
    assert (r.cap_profile, r.preference_profile, r.profile, r.cap_answered) == ("AGGRESSIVE", "MODERATE", "MODERATE", True)
    assert more_conservative("AGGRESSIVE", "CONSERVATIVE") == "CONSERVATIVE" == more_conservative("CONSERVATIVE", "MODERATE")

    with pytest.raises(ValueError):
        score_quiz({"horizon": "forever"}, q)                    # a typo never silently scores as the middle answer
    with pytest.raises(ValueError):
        score_quiz({"nope": "x"}, q)


def test_quiz_scoring_comes_from_the_database_not_from_code():
    _make_admin()
    q = sdb.get_quiz_settings()
    flipped = [dict(x) for x in q["questions"]]
    for x in flipped:
        if x["id"] == "drop":
            x["options"] = [dict(o, points={"sell": 2, "wait": 1, "buy": 0}[o["key"]]) for o in x["options"]]
    svc.update_quiz_settings(ME, questions=flipped)
    assert sdb.get_quiz_settings()["quiz_version"] == 2
    assert score_quiz(_answers("10plus", "sell", "grow"), sdb.get_quiz_settings()).profile == "AGGRESSIVE"   # was MODERATE


def test_taking_the_quiz_records_the_result_the_cap_and_an_audit_event():
    svc.set_emergency_reserve(ME, 4200)
    r = svc.take_quiz(ME, _answers("lt3", "buy", "grow"))
    assert r.profile == "CONSERVATIVE"
    s = svc.get_investor_state(ME)
    assert (s["profile"], s["source"], s["cap_profile"], s["is_default"]) == ("CONSERVATIVE", "QUIZ", "CONSERVATIVE", False)
    assert s["emergency_reserve_amount"] == 4200.0 and s["quiz_version"] == 1                # the reserve is carried over
    assert s["quiz_answers"] == _answers("lt3", "buy", "grow")
    assert "take_risk_quiz" in _ops()

    svc.take_quiz(ME, _answers(None, "wait", "balance"))                                      # Q1 skipped
    s = svc.get_investor_state(ME)
    assert (s["profile"], s["cap_profile"]) == ("MODERATE", None)                            # no cap is recorded for an unanswered Q1


# ── Picking a profile ─────────────────────────────────────────────────────────

def test_pick_without_a_quiz_needs_no_acknowledgment():
    row = svc.pick_profile(ME, "AGGRESSIVE")
    assert (row["profile"], row["source"], row["above_cap_acknowledged"], row["cap_profile"]) == ("AGGRESSIVE", "PICKED", False, None)
    assert svc.get_investor_state(ME)["above_cap"] is False
    with pytest.raises(ValueError):
        svc.pick_profile(ME, "REALLY_AGGRESSIVE")


def test_pick_above_the_quiz_cap_requires_acknowledgment_and_is_audited():
    svc.take_quiz(ME, _answers("lt3", "buy", "grow"))                                         # cap CONSERVATIVE
    n_events = len(repo.get_audit_events(limit=200))
    with pytest.raises(svc.AcknowledgmentRequired):
        svc.pick_profile(ME, "AGGRESSIVE")
    assert svc.get_investor_state(ME)["profile"] == "CONSERVATIVE" and len(repo.get_audit_events(limit=200)) == n_events

    row = svc.pick_profile(ME, "AGGRESSIVE", acknowledge_above_cap=True)
    assert (row["profile"], row["source"], row["above_cap_acknowledged"], row["cap_profile"]) == ("AGGRESSIVE", "PICKED", True, "CONSERVATIVE")
    s = svc.get_investor_state(ME)
    assert s["above_cap"] is True and s["above_cap_acknowledged"] is True                      # the UI shows a small flag
    ev = [e for e in repo.get_audit_events(limit=200) if e["operation"] == "pick_investor_profile"][0]
    assert ev["after"]["above_cap_acknowledged"] is True and "acknowledged" in ev["reason"] and "CONSERVATIVE" in ev["reason"]

    row = svc.pick_profile(ME, "CONSERVATIVE")                                                # back at/below the cap: no ack needed
    assert row["above_cap_acknowledged"] is False and svc.get_investor_state(ME)["above_cap"] is False
    assert svc.pick_profile(ME, "CONSERVATIVE") == row                                        # same pick again: nothing changes


def test_history_is_kept_and_exactly_one_row_is_active():
    svc.pick_profile(ME, "CONSERVATIVE")
    svc.set_emergency_reserve(ME, 5000)
    svc.take_quiz(ME, _answers("10plus", "buy", "grow"))
    hist = pdb.list_investor_profile_history(LOCAL_OWNER)
    assert len(hist) == 3 and [h["superseded_at"] is None for h in hist] == [True, False, False]
    with sqlite3.connect(str(db_module.DB_PATH)) as c:
        assert c.execute("SELECT COUNT(*) FROM investor_profile WHERE owner = ? AND superseded_at IS NULL", [LOCAL_OWNER]).fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):                                          # the unique index enforces it
            c.execute("INSERT INTO investor_profile (owner, profile, source, created_at) VALUES (?, 'MODERATE', 'DEFAULT', 'x')", [LOCAL_OWNER])


# ── Global settings: validation, versioning, reset ────────────────────────────

def test_updates_are_validated_versioned_and_guard_against_stale_writes():
    _make_admin()
    mod = sdb.get_risk_profile_settings("MODERATE")
    ok = {"US_EQUITY": {"target": 50, "min": 45, "max": 55}, "INTL_EQUITY": {"target": 10, "min": 5, "max": 15},
          "FIXED_INCOME": {"target": 33, "min": 28, "max": 38}, "CASH": {"target": 7, "min": 4, "max": 10}}
    out = svc.update_risk_profile_settings(ME, "MODERATE", expected_version=mod["version"], allocation=ok, max_position_pct=10)
    assert (out["version"], out["max_position_pct"], out["updated_by"]) == (mod["version"] + 1, 10.0, LOCAL_OWNER)
    with pytest.raises(sdb.SettingsVersionConflict):
        svc.update_risk_profile_settings(ME, "MODERATE", expected_version=mod["version"], max_position_pct=11)

    bad_allocations = [
        {"US_EQUITY": {"target": 50, "min": 45, "max": 55}, "CASH": {"target": 40, "min": 35, "max": 45}},      # sums to 90
        {"US_EQUITY": {"target": 100, "min": 120, "max": 100}},                                                  # min > target, > 100
        {"US_EQUITY": {"target": 60, "min": 50, "max": 70}, "CASH": {"target": 40, "min": 50, "max": 45}},       # min > target
        {"US_EQUITY": {"target": 100, "min": 100, "max": 90}},                                                   # target > max
        {"MULTI_ASSET": {"target": 100, "min": 90, "max": 100}},                                                 # looked through
        {"UNKNOWN": {"target": 100, "min": 90, "max": 100}},
        {"US_EQUITY": {"target": 100}},                                                                          # missing min/max
        {}, {"BITCOIN": {"target": 100, "min": 90, "max": 100}},
    ]
    for bad in bad_allocations:
        with pytest.raises(ValueError):
            svc.update_risk_profile_settings(ME, "MODERATE", allocation=bad)
    for kwargs in ({"max_position_pct": 101}, {"max_issuer_pct": -1}, {"max_leveraged_inverse_pct": float("nan")},
                   {"allowed_instrument_types": []}, {"allowed_instrument_types": ["STOCK", "MEME"]}, {"display_name": " "}):
        with pytest.raises(ValueError):
            svc.update_risk_profile_settings(ME, "MODERATE", **kwargs)
    with pytest.raises(ValueError):
        svc.update_risk_profile_settings(ME, "RECKLESS", max_position_pct=5)
    with pytest.raises(ValueError):
        sdb.update_portfolio_settings(updated_by="t", default_emergency_reserve_amount=-1)
    assert sdb.get_risk_profile_settings("MODERATE")["version"] == out["version"]              # every bad write changed nothing


def test_quiz_settings_validation():
    _make_admin()
    q = sdb.get_quiz_settings()
    broken = [
        dict(questions=[]),
        dict(questions=[{"id": "a", "text": "x", "options": [{"key": "1", "label": "a", "points": 0}], "default_option": "1"}]),
        dict(cap_rules={"question_id": "missing", "cap_by_option": {}}),
        dict(cap_rules={"question_id": "horizon", "cap_by_option": {"lt3": "CONSERVATIVE"}}),                   # options unmapped
        dict(cap_rules={"question_id": "horizon", "cap_by_option": {"lt3": "X", "3to10": "MODERATE", "10plus": "AGGRESSIVE"}}),
        dict(preference_thresholds={"question_ids": ["drop", "priority"], "bands": [{"max_points": 1, "profile": "CONSERVATIVE"}]}),   # can't reach 4
        dict(preference_thresholds={"question_ids": ["horizon"], "bands": [{"max_points": 2, "profile": "MODERATE"}]}),               # the cap question
        dict(preference_thresholds={"question_ids": ["drop"], "bands": [{"max_points": 2, "profile": "MODERATE"}, {"max_points": 1, "profile": "AGGRESSIVE"}]}),
    ]
    for kwargs in broken:
        with pytest.raises(ValueError):
            svc.update_quiz_settings(ME, **kwargs)
    assert sdb.get_quiz_settings()["version"] == q["version"]


def test_reset_to_defaults_restores_every_table():
    _make_admin()
    svc.update_portfolio_settings(ME, default_emergency_reserve_amount=9999)
    svc.update_risk_profile_settings(ME, "AGGRESSIVE", max_position_pct=1, description="changed")
    svc.update_risk_profile_settings(ME, "MODERATE", max_sector_pct=1)
    svc.update_quiz_settings(ME, cap_rules={"question_id": "horizon", "cap_by_option": {"lt3": "CONSERVATIVE", "3to10": "CONSERVATIVE", "10plus": "MODERATE"}})

    assert svc.reset_portfolio_settings(ME)["default_emergency_reserve_amount"] == 3000.0
    one = svc.reset_risk_profile_settings(ME, "AGGRESSIVE")
    assert one["max_position_pct"] == 20.0 and sdb.get_risk_profile_settings("MODERATE")["max_sector_pct"] == 1.0   # only that profile
    svc.reset_risk_profile_settings(ME)
    assert sdb.get_risk_profile_settings("MODERATE")["max_sector_pct"] == 25.0
    svc.reset_quiz_settings(ME)
    assert sdb.get_quiz_settings()["cap_rules"] == sdb.SEED_QUIZ_CAP_RULES
    assert sdb.get_portfolio_settings()["version"] == 3 and sdb.get_quiz_settings()["version"] == 3     # resets are versioned writes too
    assert {"reset_portfolio_settings", "reset_risk_profile_settings", "reset_quiz_settings"} <= set(_ops())


# ── Admin-only (server-side) ──────────────────────────────────────────────────

def test_global_settings_are_admin_only_even_when_the_ui_is_bypassed():
    member, nobody = _ctx("member_user"), _ctx("not_a_user")
    _make_admin("member_user", role="member")
    _make_admin("sidelined_admin", enabled=False)
    sidelined = _ctx("sidelined_admin")
    calls = [
        lambda c: svc.update_portfolio_settings(c, default_emergency_reserve_amount=1),
        lambda c: svc.reset_portfolio_settings(c),
        lambda c: svc.update_risk_profile_settings(c, "MODERATE", max_position_pct=1),
        lambda c: svc.reset_risk_profile_settings(c),
        lambda c: svc.update_quiz_settings(c, questions=sdb.SEED_QUIZ_QUESTIONS),
        lambda c: svc.reset_quiz_settings(c),
    ]
    for who in (member, nobody, sidelined, ME):                                     # ME has no auth_users row yet either
        for call in calls:
            with pytest.raises(svc.NotAdmin):
                call(who)
    assert sdb.get_portfolio_settings()["version"] == 1 and "update_portfolio_settings" not in _ops()

    _make_admin()
    svc.update_portfolio_settings(ME, default_emergency_reserve_amount=3500)       # an enabled admin may
    assert sdb.get_portfolio_settings()["default_emergency_reserve_amount"] == 3500.0
    assert auth_users_db.is_admin_owner(LOCAL_OWNER) and not auth_users_db.is_admin_owner("") and not auth_users_db.is_admin_owner("member_user")


# ── limits_source ─────────────────────────────────────────────────────────────

_PRE_LIMITS_COLUMNS = """id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL UNIQUE, display_name TEXT,
    base_currency TEXT NOT NULL DEFAULT 'USD', timezone TEXT NOT NULL DEFAULT 'America/Chicago',
    max_sector_pct REAL NOT NULL DEFAULT 25.0, max_issuer_pct REAL NOT NULL DEFAULT 15.0,
    max_per_candidate_pct_of_cash REAL NOT NULL DEFAULT 0.4, max_post_trade_position_pct REAL NOT NULL DEFAULT 0.15,
    sector_exclusions TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, employer TEXT,
    pre_clearance_required INTEGER NOT NULL DEFAULT 1, blackout_start TEXT, blackout_end TEXT, disclaimer_accepted_at TEXT,
    preferred_horizons TEXT NOT NULL DEFAULT '["5d","21d","63d","250d"]'"""


def test_migration_marks_every_existing_profile_custom_so_no_limit_changes_silently():
    c = sqlite3.connect(db_module.DB_PATH)
    c.execute(f"CREATE TABLE user_profile ({_PRE_LIMITS_COLUMNS})")                  # the shape before limits_source
    c.executemany("INSERT INTO user_profile (owner, max_sector_pct, max_issuer_pct, max_per_candidate_pct_of_cash, "
                  "max_post_trade_position_pct, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'x', 'x')",
                  [(LOCAL_OWNER, 30.0, 20.0, 0.5, 0.2), ("old_member", 10.0, 9.0, 0.3, 0.07)])
    c.commit(); c.close()

    assert user_profile_db.get_user_profile_for(ME).limits_source == "custom"
    assert user_profile_db.get_user_profile_by_owner("old_member").limits_source == "custom"
    p = get_effective_policy(ME)                                                     # their own numbers are what is enforced
    assert (p.max_sector_pct, p.max_issuer_pct, p.limits_source) == (30.0, 20.0, "custom")
    assert p.max_position_pct == pytest.approx(20.0) and p.max_candidate_pct_of_cash == pytest.approx(50.0)
    old = get_effective_policy(_ctx("old_member"))
    assert old.max_sector_pct == 10.0 and old.max_position_pct == pytest.approx(7.0)

    newbie = user_profile_db.get_user_profile_by_owner("brand_new")                  # auto-provisioned AFTER the migration
    assert newbie.limits_source == "profile"
    nb = get_effective_policy(_ctx("brand_new"))
    assert (nb.max_sector_pct, nb.max_issuer_pct, nb.max_position_pct, nb.limits_source) == (25.0, 12.0, 12.0, "profile")

    with user_profile_db._db() as conn:                                              # re-running the migration changes nothing
        user_profile_db._migrate(conn); user_profile_db._migrate(conn)
        rows = dict(conn.execute("SELECT owner, limits_source FROM user_profile").fetchall())
    assert rows == {LOCAL_OWNER: "custom", "old_member": "custom", "brand_new": "profile"}


def test_migration_also_preserves_custom_through_the_old_singleton_rebuild():
    c = sqlite3.connect(db_module.DB_PATH)
    c.execute("""CREATE TABLE user_profile (id INTEGER PRIMARY KEY CHECK (id = 1), display_name TEXT,
        base_currency TEXT NOT NULL DEFAULT 'USD', timezone TEXT NOT NULL DEFAULT 'America/Chicago',
        max_sector_pct REAL NOT NULL DEFAULT 25.0, max_issuer_pct REAL NOT NULL DEFAULT 15.0,
        max_per_candidate_pct_of_cash REAL NOT NULL DEFAULT 0.4, max_post_trade_position_pct REAL NOT NULL DEFAULT 0.15,
        sector_exclusions TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, employer TEXT,
        pre_clearance_required INTEGER NOT NULL DEFAULT 1, blackout_start TEXT, blackout_end TEXT,
        disclaimer_accepted_at TEXT, preferred_horizons TEXT NOT NULL DEFAULT '["5d"]')""")
    c.execute("INSERT INTO user_profile (id, max_sector_pct, created_at, updated_at) VALUES (1, 33.0, 'x', 'x')")
    c.commit(); c.close()
    p = user_profile_db.get_user_profile_for(ME)
    assert (p.limits_source, p.max_sector_pct) == ("custom", 33.0)


def test_profile_limits_come_from_the_settings_and_follow_admin_changes_and_picks():
    _make_admin()
    assert get_effective_policy(ME).max_position_pct == 12.0
    svc.pick_profile(ME, "AGGRESSIVE")
    p = get_effective_policy(ME)
    assert (p.max_sector_pct, p.max_issuer_pct, p.max_position_pct, p.max_leveraged_inverse_pct) == (35.0, 20.0, 20.0, 15.0)
    svc.update_risk_profile_settings(ME, "AGGRESSIVE", max_position_pct=18)
    assert get_effective_policy(ME).max_position_pct == 18.0
    assert get_effective_policy(ME).settings_versions["risk_profile_settings"] == 2

    svc.set_limits_source(ME, "custom")                                              # the user's stored numbers win again
    p = get_effective_policy(ME)
    assert (p.max_sector_pct, p.max_issuer_pct, p.max_position_pct, p.limits_source) == (25.0, 15.0, 15.0, "custom")
    with pytest.raises(ValueError):
        svc.set_limits_source(ME, "whatever")
    assert svc.set_limits_source(ME, "custom") == "custom" and _ops().count("set_limits_source") == 1


# ── Custom allocation ─────────────────────────────────────────────────────────

def test_custom_allocation_replaces_the_profile_default_and_can_be_cleared():
    mine = {"US_EQUITY": {"target": 70, "min": 65, "max": 75}, "FIXED_INCOME": {"target": 20, "min": 15, "max": 25},
            "REAL_ESTATE": {"target": 5, "min": 0, "max": 10}, "CASH": {"target": 5, "min": 2, "max": 8}}
    assert svc.get_investor_state(ME)["custom_allocation"] is None
    svc.set_custom_allocation(ME, mine, reason="my plan")
    p = get_effective_policy(ME)
    assert (p.allocation_source, p.target_allocation["US_EQUITY"]["target"], "INTL_EQUITY" in p.target_allocation) == ("CUSTOM", 70.0, False)
    assert p.profile == "MODERATE"                                                  # the profile itself is unchanged
    for bad in ({"US_EQUITY": {"target": 90, "min": 85, "max": 95}}, {"MULTI_ASSET": {"target": 100, "min": 90, "max": 100}}):
        with pytest.raises(ValueError):
            svc.set_custom_allocation(ME, bad)
    assert get_effective_policy(ME).target_allocation["US_EQUITY"]["target"] == 70.0

    assert svc.clear_custom_allocation(ME) is True and svc.clear_custom_allocation(ME) is False
    assert get_effective_policy(ME).allocation_source == "PROFILE" and get_effective_policy(ME).target_allocation["US_EQUITY"]["target"] == 45.0
    assert _ops().count("set_custom_allocation") == 1 and _ops().count("clear_custom_allocation") == 1
    with sqlite3.connect(str(db_module.DB_PATH)) as c:                              # history kept, nothing active
        assert c.execute("SELECT COUNT(*), SUM(superseded_at IS NULL) FROM target_allocation_custom").fetchone() == (1, 0)


# ── Fingerprint ───────────────────────────────────────────────────────────────

def test_fingerprint_changes_with_any_policy_input_and_with_nothing_else():
    _make_admin()
    fp = lambda: get_effective_policy(ME).fingerprint
    base = fp()
    assert fp() == base                                                              # stable when nothing changes

    svc.set_emergency_reserve(ME, 4000);                                    f_reserve = fp(); assert f_reserve != base
    svc.set_emergency_reserve(ME, None);                                    assert fp() == base               # same inputs -> same fingerprint
    svc.pick_profile(ME, "CONSERVATIVE");                                   f_profile = fp(); assert f_profile != base
    svc.pick_profile(ME, "MODERATE")
    f_picked = fp()                                                                  # PICKED MODERATE is not DEFAULT MODERATE
    assert get_effective_policy(ME).profile_source == "PICKED" and f_picked != base
    svc.take_quiz(ME, _answers("10plus", "buy", "grow"));                   f_quiz = fp(); assert f_quiz != f_picked
    svc.set_custom_allocation(ME, {"US_EQUITY": {"target": 100, "min": 90, "max": 100}}); f_alloc = fp(); assert f_alloc != f_quiz
    svc.clear_custom_allocation(ME);                                        assert fp() == f_quiz
    svc.set_limits_source(ME, "custom");                                    f_custom = fp(); assert f_custom != f_quiz
    user_profile_db.update_user_profile_for(ME, max_sector_pct=33.0);       f_limit = fp(); assert f_limit != f_custom
    svc.set_limits_source(ME, "profile")
    f_now = fp()
    for change in (lambda: svc.update_risk_profile_settings(ME, svc.get_investor_state(ME)["profile"], max_issuer_pct=3),
                   lambda: svc.update_risk_profile_settings(ME, svc.get_investor_state(ME)["profile"], max_leveraged_inverse_pct=1),
                   lambda: svc.update_risk_profile_settings(ME, svc.get_investor_state(ME)["profile"], allowed_instrument_types=["STOCK"]),
                   lambda: svc.update_risk_profile_settings(ME, svc.get_investor_state(ME)["profile"],
                                                            allocation={"US_EQUITY": {"target": 100, "min": 90, "max": 100}}),
                   lambda: svc.update_portfolio_settings(ME, default_emergency_reserve_amount=1234)):
        change()
        now = fp()
        assert now != f_now, change
        f_now = now

    before = fp()                                                                    # things that must NOT move it
    user_profile_db.update_user_profile_for(ME, display_name="New Name", employer="Acme", pre_clearance_required=False)
    svc.add_goal(ME, goal_type="HOUSE", name="House")
    svc.update_risk_profile_settings(ME, svc.get_investor_state(ME)["profile"], description="reworded", display_name="Renamed")
    assert fp() == before


def test_effective_policy_from_profile_is_pure_and_keeps_its_four_limits():
    a = effective_policy_from_profile(UserProfile(display_name="Alice", employer="Acme"))
    b = effective_policy_from_profile(UserProfile(display_name="Bob", employer="Globex"))
    c = effective_policy_from_profile(UserProfile(max_sector_pct=50.0))
    assert a.fingerprint == b.fingerprint != c.fingerprint
    assert (a.max_sector_pct, a.max_issuer_pct, a.max_position_pct, a.max_candidate_pct_of_cash) == (25.0, 15.0, 15.0, 40.0)
    assert (a.profile, a.profile_source, a.emergency_reserve_amount, a.emergency_reserve_source) == ("MODERATE", "DEFAULT", 3000.0, "DEFAULT")
    prof = effective_policy_from_profile(UserProfile(limits_source="profile"))
    assert (prof.max_sector_pct, prof.max_issuer_pct, prof.max_position_pct) == (25.0, 12.0, 12.0)
    assert set(a.to_dict()) >= {"profile", "target_allocation", "emergency_reserve_amount", "settings_versions", "limits_source"}


def test_exposures_cache_is_invalidated_by_a_reserve_change_but_not_a_name_change(monkeypatch):
    from portfolio_agent.tools import portfolio_assessment as pa, portfolio_risk as risk
    monkeypatch.setattr(risk, "compute_portfolio_risk_context", lambda *a, **k: {})
    from tests.helpers_holdings import seed_holding
    seed_holding({"ticker": "AAPL", "shares": 10, "current_price": 100.0, "current_value": 1000.0, "account_name": "I",
                  "account_number": "X1", "account_type": "TAXABLE"}, "fidelity", "2026-09-01")
    cache = pa._get_default_cache(); cache.invalidate_all()
    e1 = pa.get_portfolio_exposures(ME, cache=cache)
    user_profile_db.update_user_profile_for(ME, display_name="Someone")
    assert pa.get_portfolio_exposures(ME, cache=cache).version == e1.version
    svc.set_emergency_reserve(ME, 7777)
    e2 = pa.get_portfolio_exposures(ME, cache=cache)
    assert e2.policy_fingerprint != e1.policy_fingerprint and e2.version != e1.version


# ── Goals ─────────────────────────────────────────────────────────────────────

def test_goals_are_optional_validated_and_the_reserve_is_not_one():
    assert svc.list_goals(ME) == [] and svc.get_investor_state(ME)["profile"] == "MODERATE"      # nothing requires a goal
    g = svc.add_goal(ME, goal_type="HOUSE", name="  Down payment ", target_amount=60000, target_date="2029-06-01")
    assert (g["name"], g["goal_type"], g["status"], g["target_amount"]) == ("Down payment", "HOUSE", "ACTIVE", 60000.0)
    for bad in ({"goal_type": "EMERGENCY_RESERVE", "name": "x"}, {"goal_type": "HOUSE", "name": " "},
                {"goal_type": "HOUSE", "name": "x", "target_amount": -1}, {"goal_type": "HOUSE", "name": "x", "target_date": "June 2029"}):
        with pytest.raises(ValueError):
            svc.add_goal(ME, **bad)
    assert svc.update_goal(ME, g["goal_id"], status="DONE")["status"] == "DONE"
    with pytest.raises(ValueError):
        svc.update_goal(ME, g["goal_id"], status="MAYBE")
    assert svc.list_goals(ME, include_done=False) == [] and len(svc.list_goals(ME)) == 1
    svc.remove_goal(ME, g["goal_id"])
    assert svc.list_goals(ME) == [] and {"add_goal", "update_goal", "remove_goal"} <= set(_ops())
    with pytest.raises(LookupError):
        svc.remove_goal(ME, g["goal_id"])


# ── Owner scoping ─────────────────────────────────────────────────────────────

def test_everything_private_is_scoped_to_its_owner():
    alice, bob = _ctx("alice"), _ctx("bob")
    svc.set_emergency_reserve(alice, 9000)
    svc.pick_profile(alice, "AGGRESSIVE")
    svc.set_custom_allocation(alice, {"US_EQUITY": {"target": 100, "min": 90, "max": 100}})
    goal = svc.add_goal(alice, goal_type="RETIREMENT", name="Retire")

    s = svc.get_investor_state(bob)                                                  # Bob sees only defaults
    assert (s["profile"], s["emergency_reserve_amount"], s["custom_allocation"]) == ("MODERATE", 3000.0, None)
    assert svc.list_goals(bob) == [] and get_effective_policy(bob).target_allocation["US_EQUITY"]["target"] == 45.0
    assert get_effective_policy(alice).fingerprint != get_effective_policy(bob).fingerprint

    with pytest.raises(LookupError):                                                 # another owner's goal looks like a missing one
        svc.update_goal(bob, goal["goal_id"], name="Mine now")
    with pytest.raises(LookupError):
        svc.remove_goal(bob, goal["goal_id"])
    assert pdb.get_goal("bob", goal["goal_id"]) is None and pdb.get_goal("alice", goal["goal_id"])["name"] == "Retire"
    svc.set_emergency_reserve(bob, 100)
    assert svc.get_investor_state(alice)["emergency_reserve_amount"] == 9000.0
    assert svc.clear_custom_allocation(bob) is False and svc.get_investor_state(alice)["custom_allocation"]
    with pytest.raises(Exception):                                                   # no verified identity -> no access
        svc.get_investor_state("alice")
    with pytest.raises(Exception):
        get_effective_policy(None)


def test_every_private_verb_writes_an_audit_event_attributed_to_the_actor():
    alice = _ctx("alice")
    svc.take_quiz(alice, _answers("10plus", "buy", "grow"))                          # AGGRESSIVE
    svc.pick_profile(alice, "CONSERVATIVE", reason="r")                              # a real change (picking the same profile is a no-op)
    svc.set_emergency_reserve(alice, 2500)
    svc.set_limits_source(alice, "custom")
    svc.set_custom_allocation(alice, {"CASH": {"target": 100, "min": 90, "max": 100}})
    svc.clear_custom_allocation(alice)
    g = svc.add_goal(alice, goal_type="OTHER", name="x")
    svc.update_goal(alice, g["goal_id"], name="y")
    svc.remove_goal(alice, g["goal_id"])
    ops = [e["operation"] for e in repo.get_audit_events(limit=200) if e["actor"] == "alice"]
    assert sorted(ops) == sorted(["take_risk_quiz", "pick_investor_profile", "set_emergency_reserve", "set_limits_source",
                                  "set_custom_allocation", "clear_custom_allocation", "add_goal", "update_goal", "remove_goal"])
    ev = [e for e in repo.get_audit_events(limit=200) if e["operation"] == "set_emergency_reserve"][0]
    assert ev["before"]["emergency_reserve_amount"] is None and ev["after"]["emergency_reserve_amount"] == 2500.0 and ev["request_id"]


def test_admin_changes_write_audit_events_with_before_and_after():
    _make_admin()
    svc.update_portfolio_settings(ME, default_emergency_reserve_amount=4000, reason="inflation")
    ev = [e for e in repo.get_audit_events(limit=50) if e["operation"] == "update_portfolio_settings"][0]
    assert ev["actor"] == LOCAL_OWNER and ev["before"]["default_emergency_reserve_amount"] == 3000.0
    assert ev["after"]["default_emergency_reserve_amount"] == 4000.0 and ev["reason"] == "inflation"


# ── Privacy boundary ──────────────────────────────────────────────────────────

_SHARED_MODULES = [
    "portfolio_agent/tools/market_context.py", "portfolio_agent/tools/forecast_writer.py",
    "portfolio_agent/tools/apex_dual_run.py", "portfolio_agent/pipeline/daily/apex.py",
    "portfolio_agent/tools/weight_engine.py", "portfolio_agent/tools/prediction_db.py",
    "portfolio_agent/tools/instrument_reference.py", "portfolio_agent/tools/validation_engine.py",
]
_PRIVATE_NAMES = ["investor_profile", "investor_service", "investor_settings_db", "portfolio_policy", "risk_quiz",
                  "investment_goals", "target_allocation_custom", "emergency_reserve", "limits_source"]


def test_shared_market_and_pipeline_code_never_reads_the_private_investor_data():
    """Like test_market_context_boundary: the shared forecast path and the prediction pipeline know nothing about a
    person's profile, reserve, custom allocation or goals — by source and by import graph."""
    for rel in _SHARED_MODULES:
        src = (ROOT / rel).read_text()
        for name in _PRIVATE_NAMES:
            assert name not in src, f"{rel} must not reference {name}"
    code = ("import sys\n"
            "import portfolio_agent.tools.market_context, portfolio_agent.tools.forecast_writer, "
            "portfolio_agent.tools.apex_dual_run, portfolio_agent.tools.weight_engine, portfolio_agent.tools.prediction_db, "
            "portfolio_agent.pipeline.daily.apex\n"
            "bad = [m for m in sys.modules if m.startswith('portfolio_agent') and any(n in m for n in "
            "('investor_profile_db', 'investor_service', 'investor_settings_db', 'portfolio_policy', 'risk_quiz'))]\n"
            "assert not bad, bad\nprint('clean')")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0 and "clean" in out.stdout, out.stderr[-600:]


def test_a_forecast_context_is_identical_whatever_a_person_chose():
    from portfolio_agent.tools.market_context import build_market_context
    loaders = {"fundamentals": lambda t: {"as_of_date": "2026-08-01"}, "research": lambda t: {"as_of_date": "2026-09-15"},
               "news": lambda t: [], "valuation": lambda t: {"as_of_date": "2026-09-01"}, "macro": lambda: {"vix": 20.0},
               "history": lambda t: [], "failure_patterns": lambda: []}
    a = build_market_context("AAPL", [5, 21], loaders=loaders, private_terms=frozenset())
    svc.pick_profile(ME, "CONSERVATIVE"); svc.set_emergency_reserve(ME, 99999); svc.add_goal(ME, goal_type="HOUSE", name="Secret plan")
    svc.set_custom_allocation(ME, {"CASH": {"target": 100, "min": 90, "max": 100}})
    b = build_market_context("AAPL", [5, 21], loaders=loaders, private_terms=frozenset())
    assert a.digest == b.digest and a.payload == b.payload


# ── Profile page ──────────────────────────────────────────────────────────────

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
    admin = bool(st.session_state.get("_test_admin", False))
    styles.top_nav = lambda *a, **k: None
    auth.require_login = lambda: None
    auth.current_user = lambda: types.SimpleNamespace(is_admin=admin, email="a@b.c")
    auth.current_context = lambda source: lc(source)
    runpy.run_path(str(root / "web/pages/12_👤_Profile.py"), run_name="__main__")


def _run_page(is_admin: bool):
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_function(_profile_page, default_timeout=90)
    at.session_state["_test_admin"] = is_admin
    at.run()
    return at


def test_profile_page_shows_the_default_profile_reserve_and_optional_controls():
    at = _run_page(False)
    assert not at.exception, [e.value for e in at.exception]
    md = " ".join(m.value for m in at.markdown)
    assert "Moderate" in md and "(default)" in md and "US stocks" in md and "45%" in md
    assert any(n.key == "reserve_amount" and n.value == 3000.0 for n in at.number_input)
    assert any("default" in n.label for n in at.number_input if n.key == "reserve_amount") and not any(b.key == "reserve_reset" for b in at.button)
    assert [e.label for e in at.expander if "quiz" in e.label.lower()] and "Pick a profile" in " ".join(e.label for e in at.expander)
    assert not any("Investor settings" in b.label for b in at.button)                 # no admin tile for a non-admin


def test_profile_page_saving_a_reserve_and_picking_above_the_cap_work_end_to_end():
    at = _run_page(False)
    at.number_input(key="reserve_amount").set_value(5000.0).run()
    at.button(key="reserve_save").click().run()
    assert not at.exception and svc.get_investor_state(ME)["emergency_reserve_amount"] == 5000.0
    assert any(b.key == "reserve_reset" for b in at.button)                           # now offers the way back

    svc.take_quiz(ME, _answers("lt3", "buy", "grow"))                                # cap CONSERVATIVE
    at = _run_page(False)
    at.radio(key="pick_profile").set_value("AGGRESSIVE").run()
    assert any("riskier than" in w.value for w in at.warning)
    assert at.button(key="pick_submit").disabled                                      # blocked until acknowledged
    at.checkbox(key="pick_ack").check().run()
    at.button(key="pick_submit").click().run()
    assert not at.exception and svc.get_investor_state(ME)["profile"] == "AGGRESSIVE" and svc.get_investor_state(ME)["above_cap_acknowledged"]
    at = _run_page(False)
    assert "above your quiz result" in " ".join(m.value for m in at.markdown)         # the small flag


def test_profile_page_limits_are_read_only_when_they_come_from_the_profile():
    at = _run_page(False)
    sector = next(n for n in at.number_input if n.label.startswith("Max sector concentration"))
    assert sector.disabled and sector.value == 25.0
    issuer = next(n for n in at.number_input if n.label.startswith("Max issuer concentration"))
    assert issuer.value == 12.0                                                       # the MODERATE profile's number
    svc.set_limits_source(ME, "custom")
    at = _run_page(False)
    assert not next(n for n in at.number_input if n.label.startswith("Max sector concentration")).disabled


def test_profile_page_admin_section_is_only_for_admins_and_edits_go_through_the_service():
    _make_admin()
    at = _run_page(True)
    assert not at.exception, [e.value for e in at.exception]
    assert any("Investor settings" in b.label for b in at.button)                     # the admin tile header
    at.session_state["tile_open_admin_investor"] = True
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert any(t.label == "Default emergency reserve" for t in at.tabs)
    reserve = next(n for n in at.number_input if n.label == "Default emergency reserve ($)")
    assert reserve.value == 3000.0
