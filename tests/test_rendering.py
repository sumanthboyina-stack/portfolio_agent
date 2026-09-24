"""
Phase 7: CLI action-permission qualifier — root_agent's recommendation and
clearance sections are ephemeral ADK session state (never persisted), so
they can't go through tools.decision_composer; this proves pretty_print
still links them into one explicit qualifier instead of leaving a blocked
action printed next to an unqualified-looking recommendation.
"""
from __future__ import annotations

import json

from portfolio_agent.pipeline.rendering import pretty_print


def _state(action: str, status: str, memo: str = "memo text") -> dict:
    return {
        "recommendation": json.dumps({"action": action, "conviction": 8}),
        "clearance": json.dumps({"clearance_status": status, "memo": memo}),
    }


def test_blocked_recommendation_prints_not_actionable(capsys, caplog):
    import logging
    logger = logging.getLogger("portfolio_agent.cli")
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    logger.addHandler(handler)
    try:
        pretty_print("AAPL", "all", _state("STRONG BUY", "BLOCKED", "AAPL trade: BLOCKED — restricted."))
    finally:
        logger.removeHandler(handler)

    text = " ".join(r.getMessage() for r in records)
    assert "NOT actionable" in text
    assert "BLOCKED" in text
    assert "restricted" in text


def test_allowed_recommendation_prints_allowed_by_rules_qualifier():
    import logging
    logger = logging.getLogger("portfolio_agent.cli")
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    logger.addHandler(handler)
    try:
        pretty_print("AAPL", "all", _state("BUY", "ALLOWED_BY_RULES"))
    finally:
        logger.removeHandler(handler)

    text = " ".join(r.getMessage() for r in records)
    assert "ALLOWED_BY_RULES" in text
    assert "not external compliance approval" in text


def test_missing_recommendation_or_clearance_prints_nothing_and_does_not_raise():
    pretty_print("AAPL", "all", {})
    pretty_print("AAPL", "all", {"recommendation": json.dumps({"action": "BUY"})})   # no clearance yet
