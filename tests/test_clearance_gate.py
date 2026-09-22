from __future__ import annotations

from datetime import date

from portfolio_agent.domain import UserProfile
from portfolio_agent.clearance import is_in_blackout, profile_gate_result


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
    # garbage dates are treated as unset
    assert not is_in_blackout(date(2026, 10, 8), "not-a-date", "also-bad")


def test_profile_gate_blocks_inside_blackout_without_llm():
    profile = UserProfile(blackout_start="2026-10-01", blackout_end="2026-10-15")
    result = profile_gate_result("AAPL", profile, today=date(2026, 10, 8))
    assert result is not None
    assert result["clearance_status"] == "BLOCKED"
    assert result["ticker"] == "AAPL"
    assert "blackout" in result["restriction_reason"].lower()
    assert "2026-10-01" in result["restriction_reason"]
    assert "2026-10-15" in result["restriction_reason"]


def test_profile_gate_skips_when_pre_clearance_not_required():
    profile = UserProfile(pre_clearance_required=False)
    result = profile_gate_result("AAPL", profile, today=date(2026, 10, 8))
    assert result is not None
    assert result["clearance_status"] == "SKIPPED"


def test_profile_gate_passes_through_to_llm_otherwise():
    profile = UserProfile(blackout_start="2026-10-01", blackout_end="2026-10-15")
    assert profile_gate_result("AAPL", profile, today=date(2026, 9, 21)) is None
    assert profile_gate_result("AAPL", UserProfile(), today=date(2026, 10, 8)) is None
