"""
Phase 7: web/chat/rendering.render_action_permission_and_fit — the fix for
the clearest "legacy tool able to bypass policy checks" this phase's
instructions warn about: before this phase, web/pages/4_🤖_Chat.py's APEX
flow rendered a prediction card and saved a private forecast with ZERO
clearance/policy check (confirmed absent by grep in this phase's report).
This banner is now rendered alongside every prediction card, both freshly
generated and replayed from history, and is independent of the model's own
prose (Phase 5/6 deterministic checks only).
"""
from __future__ import annotations

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.services import account_service
from portfolio_agent.services.context import local_context
from portfolio_agent.tools import portfolio_risk as risk


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    # This suite exercises rendering, not the web login gate — stand in for a
    # verified web session so current_context() (called by rendering.py) works
    # without a real Streamlit auth flow.
    import web.auth as auth
    monkeypatch.setattr(auth, "current_context", lambda source: local_context(source))


def _capture_streamlit(monkeypatch):
    import web.chat.rendering as rendering
    calls = {"success": [], "error": [], "warning": [], "caption": []}
    for kind in calls:
        monkeypatch.setattr(rendering.st, kind, lambda msg, _k=kind: calls[_k].append(msg))
    return calls


def _no_restriction(monkeypatch):
    import portfolio_agent.tools.restricted_list_db as rl
    monkeypatch.setattr(rl, "is_restricted", lambda t: (False, None))
    import portfolio_agent.tools.blackout_windows_db as bw
    monkeypatch.setattr(bw, "list_active_blackout_windows", lambda **kw: [])


def test_blocked_ticker_renders_an_error_banner_not_a_success(monkeypatch):
    import portfolio_agent.tools.restricted_list_db as rl
    monkeypatch.setattr(rl, "is_restricted", lambda t: (True, "insider list"))
    calls = _capture_streamlit(monkeypatch)

    from web.chat.rendering import render_action_permission_and_fit
    render_action_permission_and_fit("AAPL")

    assert calls["success"] == []
    assert len(calls["error"]) == 1 and "BLOCKED" in calls["error"][0]


def test_pending_approval_renders_a_warning_not_a_success(monkeypatch):
    _no_restriction(monkeypatch)
    calls = _capture_streamlit(monkeypatch)

    from web.chat.rendering import render_action_permission_and_fit
    render_action_permission_and_fit("AAPL")   # default profile requires pre-clearance, no approval on file

    assert calls["success"] == []
    assert len(calls["warning"]) == 1 and "PENDING_APPROVAL" in calls["warning"][0]


def test_allowed_ticker_renders_a_success_banner(monkeypatch):
    _no_restriction(monkeypatch)
    calls = _capture_streamlit(monkeypatch)
    from portfolio_agent.tools.trade_approvals_db import record_approval
    ctx = local_context("web:chat")
    record_approval(owner_scope=f"user:{ctx.actor}", ticker="AAPL", action="trade", granted_by="compliance:jane")

    from web.chat.rendering import render_action_permission_and_fit
    render_action_permission_and_fit("AAPL")

    assert len(calls["success"]) == 1 and "ALLOWED_BY_RULES" in calls["success"][0]
    assert calls["error"] == [] and calls["warning"] == []


def test_portfolio_fit_caption_shown_when_a_portfolio_exists(monkeypatch):
    _no_restriction(monkeypatch)
    monkeypatch.setattr(risk, "compute_portfolio_risk_context", lambda *a, **k: {})
    calls = _capture_streamlit(monkeypatch)
    from portfolio_agent.tools.trade_approvals_db import record_approval
    ctx = local_context("web:chat")
    record_approval(owner_scope=f"user:{ctx.actor}", ticker="AAPL", action="trade", granted_by="compliance:jane")
    acct = account_service.create_account(ctx, broker="manual", display_name="acct")
    account_service.add_position(ctx, acct["account_id"], {"ticker": "AAPL", "shares": 10, "current_price": 100.0})

    from web.chat.rendering import render_action_permission_and_fit
    render_action_permission_and_fit("AAPL")

    assert len(calls["caption"]) == 1 and "current weight" in calls["caption"][0]


def test_evaluation_failure_never_raises_and_says_not_actionable(monkeypatch):
    import portfolio_agent.tools.restricted_list_db as rl

    def _boom(t):
        raise RuntimeError("db locked")

    monkeypatch.setattr(rl, "is_restricted", _boom)
    calls = _capture_streamlit(monkeypatch)

    from web.chat.rendering import render_action_permission_and_fit
    render_action_permission_and_fit("AAPL")   # must not raise

    # clearance.evaluate_clearance itself already fails closed to UNKNOWN on a
    # lookup error (Phase 6) — this render function's own try/except is a
    # second line of defense for anything ELSE that might raise; either way,
    # nothing here is ever a bare success/actionable banner.
    assert calls["success"] == []
    assert len(calls["warning"]) == 1
    assert "UNKNOWN" in calls["warning"][0] and "NOT treated as allowed" in calls["warning"][0]
