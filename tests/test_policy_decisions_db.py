"""Policy decisions store: append-only, owner-scoped, action-tagged, deterministic-only."""
from __future__ import annotations

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.tools.policy_decisions_db import (
    DECIDED_BY_DETERMINISTIC, get_latest_policy_decision, get_policy_decision_history, record_policy_decision,
)


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")


def test_record_and_read_round_trip():
    row_id = record_policy_decision(owner_scope="user:alice", ticker="aapl", action="buy",
                                    decision="ALLOWED_BY_RULES", reason=None,
                                    evidence_refs={"is_restricted": False}, policy_version="v1", request_id="r1",
                                    next_transition_at="2026-10-09T00:00:00+00:00", valid_until="2026-10-09T00:00:00+00:00")
    assert row_id > 0
    row = get_latest_policy_decision("user:alice", "AAPL", action="buy")
    assert row["ticker"] == "AAPL" and row["action"] == "buy"
    assert row["decision"] == "ALLOWED_BY_RULES" and row["evidence_refs"] == {"is_restricted": False}
    assert row["decided_by"] == DECIDED_BY_DETERMINISTIC and row["request_id"] == "r1"
    assert row["next_transition_at"] == "2026-10-09T00:00:00+00:00"


def test_rejects_unknown_decision_value():
    with pytest.raises(ValueError, match="ALLOWED_BY_RULES"):
        record_policy_decision(owner_scope="user:alice", ticker="AAPL", decision="MAYBE", reason=None,
                               evidence_refs={}, policy_version="v1")


def test_append_only_keeps_every_evaluation_newest_first():
    record_policy_decision(owner_scope="user:alice", ticker="AAPL", decision="ALLOWED_BY_RULES", reason=None,
                           evidence_refs={}, policy_version="v1")
    record_policy_decision(owner_scope="user:alice", ticker="AAPL", decision="BLOCKED", reason="blackout",
                           evidence_refs={}, policy_version="v1")
    history = get_policy_decision_history("user:alice", "AAPL")
    assert [h["decision"] for h in history] == ["BLOCKED", "ALLOWED_BY_RULES"]
    assert get_latest_policy_decision("user:alice", "AAPL")["decision"] == "BLOCKED"


def test_decisions_never_cross_owners():
    record_policy_decision(owner_scope="user:alice", ticker="AAPL", decision="ALLOWED_BY_RULES", reason=None,
                           evidence_refs={}, policy_version="v1")
    assert get_latest_policy_decision("user:bob", "AAPL") is None
    assert get_policy_decision_history("user:bob", "AAPL") == []


def test_decisions_are_independent_per_action():
    record_policy_decision(owner_scope="user:alice", ticker="AAPL", action="buy",
                           decision="ALLOWED_BY_RULES", reason=None, evidence_refs={}, policy_version="v1")
    record_policy_decision(owner_scope="user:alice", ticker="AAPL", action="sell",
                           decision="PENDING_APPROVAL", reason=None, evidence_refs={}, policy_version="v1")
    assert get_latest_policy_decision("user:alice", "AAPL", action="buy")["decision"] == "ALLOWED_BY_RULES"
    assert get_latest_policy_decision("user:alice", "AAPL", action="sell")["decision"] == "PENDING_APPROVAL"
    # action=None matches the most recent row regardless of action (legacy callers).
    assert get_latest_policy_decision("user:alice", "AAPL") is not None
