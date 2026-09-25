"""
access_requests_db backs the landing page's "Request an invite" form and the
Profile page's admin review section — submit_request() only ever writes a
pending row; approve_request() is the one path that turns it into an
auth_users invite.
"""
from __future__ import annotations

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.tools import access_requests_db as req_db
from portfolio_agent.tools import auth_users_db as auth_db


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")


def test_submit_request_is_pending_and_grants_nothing():
    req = req_db.submit_request("Jane Visitor", "jane@example.com", "need access")

    assert req.status == "pending"
    assert req_db.list_requests() == [req]
    # Submitting alone never touches the allow-list.
    assert auth_db.resolve_login("iss", "sub", "jane@example.com") is None


def test_submit_request_requires_name_and_email():
    with pytest.raises(ValueError):
        req_db.submit_request("", "jane@example.com")
    with pytest.raises(ValueError):
        req_db.submit_request("Jane Visitor", "")


def test_approve_request_invites_and_marks_approved():
    req = req_db.submit_request("Jane Visitor", "jane@example.com")

    user = req_db.approve_request(req.id, owner="sumanth_b", role="member")

    assert user.email == "jane@example.com"
    assert user.owner == "sumanth_b"
    assert req_db.list_requests(status="pending") == []
    [reviewed] = req_db.list_requests(status="approved")
    assert reviewed.id == req.id
    # Now actually invited — a real login resolves.
    assert auth_db.resolve_login("iss", "sub", "jane@example.com") is not None


def test_approve_request_twice_raises():
    req = req_db.submit_request("Jane Visitor", "jane@example.com")
    req_db.approve_request(req.id, owner="sumanth_b")

    with pytest.raises(ValueError):
        req_db.approve_request(req.id, owner="sumanth_b")


def test_deny_request_marks_denied_and_grants_nothing():
    req = req_db.submit_request("Bob", "bob@example.com")

    assert req_db.deny_request(req.id) is True
    assert req_db.list_requests(status="pending") == []
    assert req_db.deny_request(req.id) is False  # already resolved
    assert auth_db.resolve_login("iss", "sub", "bob@example.com") is None
