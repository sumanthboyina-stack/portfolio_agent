"""
Phase 6: action-specific policy evaluation.

Decision values: ALLOWED_BY_RULES | BLOCKED | PENDING_APPROVAL | UNKNOWN
(renamed from the earlier CLEARED/SKIPPED/INSUFFICIENT_DATA — see
policy_decisions_db.LEGACY_DECISIONS for the mapping old rows may still hold).
ALLOWED_BY_RULES is explicitly NOT external compliance approval — it means
nothing in THIS system's rules blocks the action.

Evaluation order: restricted list -> blackout windows (tools.
blackout_windows_db, admin/global, multiple, timezone-aware) -> holding
period (opt-in, always UNKNOWN — no reliable per-lot data exists) ->
approval workflow (tools.trade_approvals_db, private, tied to one exact
(ticker, action)). Every applicable check contributes reason codes; the
first three are independent of pre_clearance_required, so disabling that
preference can never bypass a restricted-list hit or an active blackout.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from portfolio_agent.domain import UserProfile
from portfolio_agent.clearance import (
    DECISION_ALLOWED, DECISION_BLOCKED, DECISION_PENDING_APPROVAL, DECISION_UNKNOWN,
    RC_APPROVAL_ACTIVE, RC_APPROVAL_MISSING, RC_BLACKOUT_ACTIVE, RC_BLACKOUT_DATA_INVALID,
    RC_HOLDING_PERIOD_UNAVAILABLE, RC_PRE_CLEARANCE_DISABLED, RC_RESTRICTED_LIST_HIT,
    evaluate_and_record_clearance, evaluate_blackout_windows, evaluate_clearance, is_in_blackout,
)


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    import portfolio_agent.tools.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")


def _no_restriction(monkeypatch):
    import portfolio_agent.tools.restricted_list_db as rl
    monkeypatch.setattr(rl, "is_restricted", lambda t: (False, None))


# ── is_in_blackout: legacy single-window helper ──────────────────────────────

def test_is_in_blackout_inclusive_window():
    assert is_in_blackout(date(2026, 10, 1), "2026-10-01", "2026-10-15")
    assert is_in_blackout(date(2026, 10, 15), "2026-10-01", "2026-10-15")
    assert is_in_blackout(date(2026, 10, 8), "2026-10-01", "2026-10-15")
    assert not is_in_blackout(date(2026, 9, 30), "2026-10-01", "2026-10-15")
    assert not is_in_blackout(date(2026, 10, 16), "2026-10-01", "2026-10-15")


def test_is_in_blackout_open_ended_and_unset():
    assert not is_in_blackout(date(2026, 10, 8), None, None)
    assert not is_in_blackout(date(2026, 10, 8), "", "")
    assert is_in_blackout(date(2026, 10, 8), "2026-10-01", None)
    assert not is_in_blackout(date(2026, 9, 8), "2026-10-01", None)
    assert is_in_blackout(date(2026, 10, 8), None, "2026-10-15")
    assert not is_in_blackout(date(2026, 10, 16), None, "2026-10-15")


def test_is_in_blackout_raises_on_invalid_data_never_treats_it_as_unset():
    """Phase 6 fix: invalid policy data is not 'no restriction' — previously
    garbage dates silently meant 'no blackout'; now they raise."""
    with pytest.raises(ValueError):
        is_in_blackout(date(2026, 10, 8), "not-a-date", "also-bad")


# ── evaluate_blackout_windows: multi-window, scoped, timezone-aware ─────────

def _win(id_, scope, start, end, tz="America/Chicago", source="test", reason="test window"):
    return {"id": id_, "scope": scope, "start_date": start, "end_date": end, "timezone": tz,
           "source": source, "reason": reason}


def test_overlapping_windows_both_contribute_and_active_wins():
    windows = [
        _win(1, "global", "2026-10-01", "2026-10-20", reason="earnings"),
        _win(2, "ticker:AAPL", "2026-10-10", "2026-10-15", reason="M&A"),
    ]
    at = datetime(2026, 10, 12, tzinfo=timezone.utc)
    out = evaluate_blackout_windows(windows, "AAPL", at_utc=at)
    assert out["active"] is True
    assert out["reason_codes"].count(RC_BLACKOUT_ACTIVE) == 2   # both windows active simultaneously
    assert len(out["details"]) == 2


def test_window_scoped_to_a_different_ticker_does_not_apply():
    windows = [_win(1, "ticker:MSFT", "2026-10-01", "2026-10-20")]
    at = datetime(2026, 10, 12, tzinfo=timezone.utc)
    out = evaluate_blackout_windows(windows, "AAPL", at_utc=at)
    assert out["active"] is False and out["details"] == []


def test_invalid_window_data_is_flagged_not_treated_as_no_restriction():
    windows = [_win(1, "global", "not-a-date", "also-bad")]
    at = datetime(2026, 10, 12, tzinfo=timezone.utc)
    out = evaluate_blackout_windows(windows, "AAPL", at_utc=at)
    assert out["invalid"] is True
    assert RC_BLACKOUT_DATA_INVALID in out["reason_codes"]
    assert out["active"] is False   # not flagged active, but NOT "clean" either -- invalid is a separate signal


def test_dst_boundary_local_date_stays_correct_across_the_spring_forward_transition():
    """2026-03-08 is America/Chicago's spring-forward day. A window scoped to
    that single local date must still be 'active' both just after local
    midnight (CST, UTC-6) and just before local midnight (CDT, UTC-5) --
    proving the evaluator converts via real timezone rules, not a fixed offset."""
    windows = [_win(1, "global", "2026-03-08", "2026-03-08")]
    just_after_midnight_utc = datetime(2026, 3, 8, 6, 30, tzinfo=timezone.utc)    # 00:30 CST local
    just_before_next_midnight_utc = datetime(2026, 3, 9, 4, 30, tzinfo=timezone.utc)  # 23:30 CDT local, still Mar 8

    out1 = evaluate_blackout_windows(windows, "AAPL", at_utc=just_after_midnight_utc)
    out2 = evaluate_blackout_windows(windows, "AAPL", at_utc=just_before_next_midnight_utc)
    assert out1["active"] is True and out2["active"] is True

    # One minute after local midnight the NEXT day (now outside the window) must not be active.
    next_day_utc = datetime(2026, 3, 9, 5, 1, tzinfo=timezone.utc)   # 00:01 CDT Mar 9
    out3 = evaluate_blackout_windows(windows, "AAPL", at_utc=next_day_utc)
    assert out3["active"] is False


def test_open_ended_window_has_no_earliest_block_end():
    windows = [_win(1, "global", "2026-10-01", None)]
    at = datetime(2026, 10, 12, tzinfo=timezone.utc)
    out = evaluate_blackout_windows(windows, "AAPL", at_utc=at)
    assert out["active"] is True and out["earliest_block_end"] is None


# ── evaluate_clearance: priority order + reason-code collection ─────────────

def test_blackout_blocks_even_when_pre_clearance_is_disabled(monkeypatch):
    """Disabling the approval workflow must never bypass an enforced blackout."""
    _no_restriction(monkeypatch)
    import portfolio_agent.tools.blackout_windows_db as bw
    monkeypatch.setattr(bw, "list_active_blackout_windows",
                        lambda **kw: [_win(1, "global", "2026-10-01", "2026-10-15")])
    profile = UserProfile(pre_clearance_required=False)
    decision = evaluate_clearance("AAPL", profile, today=date(2026, 10, 8))
    assert decision.decision == DECISION_BLOCKED
    assert RC_BLACKOUT_ACTIVE in decision.evidence_refs["reason_codes"]
    assert decision.evidence_refs["pre_clearance_required"] is False   # recorded, but did not decide the outcome


def test_restricted_list_blocks_even_when_pre_clearance_is_disabled_and_no_blackout(monkeypatch):
    import portfolio_agent.tools.restricted_list_db as rl
    import portfolio_agent.tools.blackout_windows_db as bw
    monkeypatch.setattr(rl, "is_restricted", lambda t: (True, "insider information risk"))
    monkeypatch.setattr(bw, "list_active_blackout_windows", lambda **kw: [])
    profile = UserProfile(pre_clearance_required=False)
    decision = evaluate_clearance("AAPL", profile, today=date(2026, 10, 8))
    assert decision.decision == DECISION_BLOCKED
    assert RC_RESTRICTED_LIST_HIT in decision.evidence_refs["reason_codes"]
    assert "insider" in decision.evidence_refs["restricted_reason"]


def test_both_restricted_and_blackout_apply_both_reason_codes_collected(monkeypatch):
    import portfolio_agent.tools.restricted_list_db as rl
    import portfolio_agent.tools.blackout_windows_db as bw
    monkeypatch.setattr(rl, "is_restricted", lambda t: (True, "M&A"))
    monkeypatch.setattr(bw, "list_active_blackout_windows",
                        lambda **kw: [_win(1, "global", "2026-10-01", "2026-10-15")])
    decision = evaluate_clearance("AAPL", UserProfile(), today=date(2026, 10, 8))
    codes = decision.evidence_refs["reason_codes"]
    assert RC_RESTRICTED_LIST_HIT in codes and RC_BLACKOUT_ACTIVE in codes
    assert decision.decision == DECISION_BLOCKED


def test_pre_clearance_disabled_with_no_blocks_is_allowed_by_rules(monkeypatch):
    _no_restriction(monkeypatch)
    import portfolio_agent.tools.blackout_windows_db as bw
    monkeypatch.setattr(bw, "list_active_blackout_windows", lambda **kw: [])
    profile = UserProfile(pre_clearance_required=False)
    decision = evaluate_clearance("AAPL", profile, today=date(2026, 10, 8))
    assert decision.decision == DECISION_ALLOWED
    assert RC_PRE_CLEARANCE_DISABLED in decision.evidence_refs["reason_codes"]


def test_allowed_by_rules_is_not_external_compliance_approval_by_construction(monkeypatch):
    """ALLOWED_BY_RULES only ever means this system's own rules passed -- there
    is no code path that asserts anything about an outside approval process."""
    _no_restriction(monkeypatch)
    import portfolio_agent.tools.blackout_windows_db as bw
    monkeypatch.setattr(bw, "list_active_blackout_windows", lambda **kw: [])
    decision = evaluate_clearance("AAPL", UserProfile(pre_clearance_required=False), today=date(2026, 10, 8))
    assert "not" in decision.memo.lower() and "compliance" in decision.memo.lower()


# ── Approval workflow: tied to a specific (ticker, action), can expire ──────

def test_pending_approval_when_required_and_no_approval_exists(monkeypatch):
    _no_restriction(monkeypatch)
    import portfolio_agent.tools.blackout_windows_db as bw
    monkeypatch.setattr(bw, "list_active_blackout_windows", lambda **kw: [])
    decision = evaluate_clearance("AAPL", UserProfile(), action="buy", owner_scope="user:alice",
                                  today=date(2026, 10, 8))
    assert decision.decision == DECISION_PENDING_APPROVAL
    assert RC_APPROVAL_MISSING in decision.evidence_refs["reason_codes"]


def test_active_approval_for_the_exact_action_allows(monkeypatch):
    _no_restriction(monkeypatch)
    import portfolio_agent.tools.blackout_windows_db as bw
    from portfolio_agent.tools.trade_approvals_db import record_approval
    monkeypatch.setattr(bw, "list_active_blackout_windows", lambda **kw: [])
    record_approval(owner_scope="user:alice", ticker="AAPL", action="buy", granted_by="compliance:jane")

    decision = evaluate_clearance("AAPL", UserProfile(), action="buy", owner_scope="user:alice",
                                  today=date(2026, 10, 8))
    assert decision.decision == DECISION_ALLOWED
    assert RC_APPROVAL_ACTIVE in decision.evidence_refs["reason_codes"]


def test_approval_for_a_different_action_does_not_cover_this_one(monkeypatch):
    """Tied to its specific action: a 'buy' approval never covers a 'sell'."""
    _no_restriction(monkeypatch)
    import portfolio_agent.tools.blackout_windows_db as bw
    from portfolio_agent.tools.trade_approvals_db import record_approval
    monkeypatch.setattr(bw, "list_active_blackout_windows", lambda **kw: [])
    record_approval(owner_scope="user:alice", ticker="AAPL", action="buy", granted_by="compliance:jane")

    decision = evaluate_clearance("AAPL", UserProfile(), action="sell", owner_scope="user:alice",
                                  today=date(2026, 10, 8))
    assert decision.decision == DECISION_PENDING_APPROVAL


def test_expired_approval_is_pending_not_allowed(monkeypatch):
    _no_restriction(monkeypatch)
    import portfolio_agent.tools.blackout_windows_db as bw
    from portfolio_agent.tools.trade_approvals_db import record_approval
    monkeypatch.setattr(bw, "list_active_blackout_windows", lambda **kw: [])
    record_approval(owner_scope="user:alice", ticker="AAPL", action="buy", granted_by="compliance:jane",
                    expires_at="2020-01-01T00:00:00+00:00")   # long expired

    decision = evaluate_clearance("AAPL", UserProfile(), action="buy", owner_scope="user:alice",
                                  today=date(2026, 10, 8))
    assert decision.decision == DECISION_PENDING_APPROVAL
    assert RC_APPROVAL_MISSING in decision.evidence_refs["reason_codes"]


def test_revoked_approval_is_pending_not_allowed(monkeypatch):
    _no_restriction(monkeypatch)
    import portfolio_agent.tools.blackout_windows_db as bw
    from portfolio_agent.tools.trade_approvals_db import record_approval, revoke_approval
    monkeypatch.setattr(bw, "list_active_blackout_windows", lambda **kw: [])
    record_approval(owner_scope="user:alice", ticker="AAPL", action="buy", granted_by="compliance:jane")
    revoke_approval(owner_scope="user:alice", ticker="AAPL", action="buy", revoked_by="compliance:jane")

    decision = evaluate_clearance("AAPL", UserProfile(), action="buy", owner_scope="user:alice",
                                  today=date(2026, 10, 8))
    assert decision.decision == DECISION_PENDING_APPROVAL


def test_trade_approvals_db_refuses_an_llm_as_granted_by(tmp_path, monkeypatch):
    import portfolio_agent.tools.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    from portfolio_agent.tools.trade_approvals_db import record_approval
    with pytest.raises(ValueError, match="LLM"):
        record_approval(owner_scope="user:alice", ticker="AAPL", action="buy", granted_by="llm")


# ── Missing data is never "safe" ────────────────────────────────────────────

def test_restricted_list_lookup_failure_is_unknown_not_allowed(monkeypatch):
    import portfolio_agent.tools.restricted_list_db as rl

    def _boom(t):
        raise RuntimeError("restricted_list table is locked")

    monkeypatch.setattr(rl, "is_restricted", _boom)
    decision = evaluate_clearance("AAPL", UserProfile(pre_clearance_required=False), today=date(2026, 10, 8))
    assert decision.decision == DECISION_UNKNOWN
    assert decision.evidence_refs["restricted_list_checked"] is False
    assert "restricted_list_error" in decision.evidence_refs


def test_blackout_lookup_failure_is_unknown(monkeypatch):
    _no_restriction(monkeypatch)
    import portfolio_agent.tools.blackout_windows_db as bw

    def _boom(**kw):
        raise RuntimeError("blackout_windows table is locked")

    monkeypatch.setattr(bw, "list_active_blackout_windows", _boom)
    decision = evaluate_clearance("AAPL", UserProfile(pre_clearance_required=False), today=date(2026, 10, 8))
    assert decision.decision == DECISION_UNKNOWN


def test_invalid_blackout_data_makes_the_whole_decision_unknown(monkeypatch):
    _no_restriction(monkeypatch)
    import portfolio_agent.tools.blackout_windows_db as bw
    monkeypatch.setattr(bw, "list_active_blackout_windows",
                        lambda **kw: [_win(1, "global", "garbage", "also-garbage")])
    decision = evaluate_clearance("AAPL", UserProfile(pre_clearance_required=False), today=date(2026, 10, 8))
    assert decision.decision == DECISION_UNKNOWN
    assert RC_BLACKOUT_DATA_INVALID in decision.evidence_refs["reason_codes"]


def test_holding_period_check_is_always_unknown_and_caps_an_otherwise_clean_decision(monkeypatch):
    """No reliable per-lot acquisition data exists in this codebase -- a
    holding-period-sensitive action can never be answered, only flagged."""
    _no_restriction(monkeypatch)
    import portfolio_agent.tools.blackout_windows_db as bw
    monkeypatch.setattr(bw, "list_active_blackout_windows", lambda **kw: [])
    decision = evaluate_clearance("AAPL", UserProfile(pre_clearance_required=False), today=date(2026, 10, 8),
                                  check_holding_period=True)
    assert decision.decision == DECISION_UNKNOWN
    assert RC_HOLDING_PERIOD_UNAVAILABLE in decision.evidence_refs["reason_codes"]


def test_holding_period_check_does_not_upgrade_an_already_blocked_decision(monkeypatch):
    import portfolio_agent.tools.restricted_list_db as rl
    monkeypatch.setattr(rl, "is_restricted", lambda t: (True, "reason"))
    decision = evaluate_clearance("AAPL", UserProfile(), today=date(2026, 10, 8), check_holding_period=True)
    assert decision.decision == DECISION_BLOCKED   # BLOCKED stays BLOCKED, not somehow "more unknown"


# ── next_transition_at / valid_until ──────────────────────────────────────────

def test_next_transition_at_matches_the_blackout_window_end(monkeypatch):
    _no_restriction(monkeypatch)
    import portfolio_agent.tools.blackout_windows_db as bw
    monkeypatch.setattr(bw, "list_active_blackout_windows",
                        lambda **kw: [_win(1, "global", "2026-10-01", "2026-10-15", tz="America/Chicago")])
    decision = evaluate_clearance("AAPL", UserProfile(pre_clearance_required=False), today=date(2026, 10, 8))
    assert decision.next_transition_at is not None
    assert decision.next_transition_at.startswith("2026-10-16")   # the day after the window ends
    assert decision.valid_until == decision.next_transition_at


def test_next_transition_at_defaults_to_start_of_tomorrow_when_nothing_else_known(monkeypatch):
    _no_restriction(monkeypatch)
    import portfolio_agent.tools.blackout_windows_db as bw
    monkeypatch.setattr(bw, "list_active_blackout_windows", lambda **kw: [])
    decision = evaluate_clearance("AAPL", UserProfile(pre_clearance_required=False), today=date(2026, 10, 8))
    assert decision.next_transition_at.startswith("2026-10-09")


# ── The LLM cannot decide policy: the profile gate resolves every input, zero LLM calls ──

def test_profile_gate_resolves_every_ticker_profile_combination_without_a_model_call(monkeypatch):
    import portfolio_agent.tools.restricted_list_db as rl
    import portfolio_agent.tools.blackout_windows_db as bw
    monkeypatch.setattr(rl, "is_restricted", lambda t: (False, None))
    monkeypatch.setattr(bw, "list_active_blackout_windows", lambda **kw: [])

    import litellm
    def _no_llm(*a, **kw):
        raise AssertionError("clearance must never call an LLM")
    monkeypatch.setattr(litellm, "acompletion", _no_llm)

    from portfolio_agent.clearance import _profile_gate

    class _FakeState(dict):
        pass

    class _FakeCtx:
        def __init__(self, ticker):
            self.state = _FakeState(current_ticker=ticker)

    for profile_kwargs in ({}, {"pre_clearance_required": False},
                           {"blackout_start": "2026-01-01", "blackout_end": "2099-01-01"}):
        import portfolio_agent.tools.user_profile_db as updb
        monkeypatch.setattr(updb, "get_user_profile", lambda pk=profile_kwargs: UserProfile(**pk))
        result = _profile_gate(_FakeCtx("AAPL"))
        assert result is not None   # always short-circuits


def test_profile_gate_writes_state_and_never_falls_through_to_the_llm(monkeypatch):
    import portfolio_agent.tools.restricted_list_db as rl
    import portfolio_agent.tools.blackout_windows_db as bw
    import portfolio_agent.tools.user_profile_db as updb
    monkeypatch.setattr(rl, "is_restricted", lambda t: (False, None))
    monkeypatch.setattr(bw, "list_active_blackout_windows", lambda **kw: [])
    monkeypatch.setattr(updb, "get_user_profile", lambda: UserProfile())
    from portfolio_agent.clearance import _profile_gate

    class _State(dict):
        pass

    class _Ctx:
        state = _State(current_ticker="AAPL")

    ctx = _Ctx()
    result = _profile_gate(ctx)
    assert result is not None
    written = json.loads(ctx.state["clearance"])
    # UserProfile()'s default requires pre-clearance and no approval is on
    # record for this action -- PENDING_APPROVAL is the correct new outcome
    # (previously this fell through to CLEARED, effectively trusting the now-
    # dead LLM step to have approved it; that's exactly the gap this phase closes).
    assert written["clearance_status"] == DECISION_PENDING_APPROVAL


def test_clearance_agent_instruction_never_lets_the_model_choose_status():
    from portfolio_agent.clearance import INSTRUCTION, clearance_agent
    assert clearance_agent.before_agent_callback is not None
    assert "never run" in INSTRUCTION or "deterministically" in INSTRUCTION


# ── Persistence: private, owner-scoped, action-tagged, append-only ──────────

def test_evaluate_and_record_persists_a_private_owner_scoped_action_tagged_decision(monkeypatch):
    import portfolio_agent.tools.restricted_list_db as rl
    import portfolio_agent.tools.blackout_windows_db as bw
    monkeypatch.setattr(rl, "is_restricted", lambda t: (False, None))
    monkeypatch.setattr(bw, "list_active_blackout_windows", lambda **kw: [])
    from portfolio_agent.tools.policy_decisions_db import (
        DECIDED_BY_DETERMINISTIC, get_latest_policy_decision, get_policy_decision_history,
    )

    d1 = evaluate_and_record_clearance("AAPL", UserProfile(pre_clearance_required=False), owner_scope="user:alice",
                                       action="buy", today=date(2026, 10, 8))
    assert d1.decision == DECISION_ALLOWED
    row = get_latest_policy_decision("user:alice", "AAPL", action="buy")
    assert row["decision"] == DECISION_ALLOWED and row["owner_scope"] == "user:alice"
    assert row["decided_by"] == DECIDED_BY_DETERMINISTIC and row["action"] == "buy"
    assert row["request_id"] == d1.request_id and row["next_transition_at"] == d1.next_transition_at
    assert get_latest_policy_decision("user:bob", "AAPL", action="buy") is None   # a different owner sees nothing

    # A different action for the same ticker is independent.
    d2 = evaluate_and_record_clearance("AAPL", UserProfile(pre_clearance_required=False), owner_scope="user:alice",
                                       action="sell", today=date(2026, 10, 8))
    assert get_policy_decision_history("user:alice", "AAPL", action="buy") != []
    sell_history = get_policy_decision_history("user:alice", "AAPL", action="sell")
    assert len(sell_history) == 1 and sell_history[0]["request_id"] == d2.request_id


def test_record_policy_decision_rejects_an_llm_as_the_decider(tmp_path, monkeypatch):
    import portfolio_agent.tools.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    from portfolio_agent.tools.policy_decisions_db import record_policy_decision
    with pytest.raises(ValueError, match="deterministic-policy-engine"):
        record_policy_decision(owner_scope="user:alice", ticker="AAPL", decision="ALLOWED_BY_RULES", reason=None,
                               evidence_refs={}, policy_version="x", decided_by="llm")


# ── Blackout window migration: never broadens what was already in effect ────

def test_evaluate_clearance_self_migrates_and_enforces_a_pre_existing_legacy_blackout(monkeypatch):
    """The real gap this closes: evaluate_clearance only reads
    blackout_windows_db now, not profile.blackout_start/end directly, so a
    caller who never ran a migration step must still see a pre-existing
    profile blackout enforced on the very first evaluation, not silently
    dropped because the new table started out empty."""
    _no_restriction(monkeypatch)
    profile = UserProfile(pre_clearance_required=False, blackout_start="2026-10-01", blackout_end="2026-10-15")
    decision = evaluate_clearance("AAPL", profile, today=date(2026, 10, 8))
    assert decision.decision == DECISION_BLOCKED
    assert RC_BLACKOUT_ACTIVE in decision.evidence_refs["reason_codes"]

    # A second evaluation (migration already ran) still enforces it, without duplicating windows.
    from portfolio_agent.tools.blackout_windows_db import list_active_blackout_windows
    decision2 = evaluate_clearance("AAPL", profile, today=date(2026, 10, 9))
    assert decision2.decision == DECISION_BLOCKED
    assert len(list_active_blackout_windows(scope_in=("global",))) == 1


def test_migrate_legacy_profile_blackout_is_idempotent_and_never_broadens(tmp_path, monkeypatch):
    import portfolio_agent.tools.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    from portfolio_agent.tools.blackout_windows_db import list_active_blackout_windows, migrate_legacy_profile_blackout

    migrate_legacy_profile_blackout("2026-01-01", "2026-01-31")
    migrate_legacy_profile_blackout("2026-01-01", "2026-01-31")   # re-run: must not duplicate
    migrate_legacy_profile_blackout("2026-06-01", "2026-06-30")   # a later call must not ADD a second window either

    windows = list_active_blackout_windows(scope_in=("global",))
    assert len(windows) == 1
    assert windows[0]["start_date"] == "2026-01-01" and windows[0]["end_date"] == "2026-01-31"


def test_migrate_legacy_profile_blackout_no_legacy_data_creates_nothing(tmp_path, monkeypatch):
    import portfolio_agent.tools.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    from portfolio_agent.tools.blackout_windows_db import list_active_blackout_windows, migrate_legacy_profile_blackout

    migrate_legacy_profile_blackout(None, None)
    assert list_active_blackout_windows() == []
