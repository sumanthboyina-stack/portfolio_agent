"""
auth_users_db.resolve_login() is the only path that turns a successful OIDC
login into an app identity — these tests cover every branch: first-login
bind, repeat login by subject, and every way a login must be denied.
"""
from __future__ import annotations

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.tools import auth_users_db as auth_db


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")


def test_invite_then_first_login_binds_issuer_and_subject():
    auth_db.invite_user("sumanth@example.com", owner="sumanth_b", role="admin")

    user = auth_db.resolve_login("https://issuer", "sub-1", "sumanth@example.com")

    assert user is not None
    assert user.owner == "sumanth_b"
    assert user.role == "admin"
    assert user.issuer == "https://issuer"
    assert user.subject == "sub-1"
    assert user.last_login_at is not None


def test_second_login_resolves_by_subject_alone():
    auth_db.invite_user("sumanth@example.com", owner="sumanth_b")
    auth_db.resolve_login("https://issuer", "sub-1", "sumanth@example.com")

    # email need not be supplied (or could differ) once bound — subject is identity now.
    user = auth_db.resolve_login("https://issuer", "sub-1", None)

    assert user is not None
    assert user.subject == "sub-1"


def test_disabled_user_denied():
    auth_db.invite_user("sumanth@example.com", owner="sumanth_b")
    auth_db.resolve_login("https://issuer", "sub-1", "sumanth@example.com")
    auth_db.disable_user("sumanth@example.com")

    assert auth_db.resolve_login("https://issuer", "sub-1", "sumanth@example.com") is None


def test_unknown_email_denied():
    assert auth_db.resolve_login("https://issuer", "sub-1", "nobody@example.com") is None


def test_same_email_different_subject_denied():
    auth_db.invite_user("sumanth@example.com", owner="sumanth_b")
    auth_db.resolve_login("https://issuer", "sub-1", "sumanth@example.com")

    # A second person registering/logging in with the same email can't take
    # over the already-bound account.
    assert auth_db.resolve_login("https://issuer", "sub-2", "sumanth@example.com") is None
    # The original binding still works.
    assert auth_db.resolve_login("https://issuer", "sub-1", None) is not None


def test_issuer_mismatch_denied():
    auth_db.invite_user("sumanth@example.com", owner="sumanth_b")
    auth_db.resolve_login("https://issuer-a", "sub-1", "sumanth@example.com")

    assert auth_db.resolve_login("https://issuer-b", "sub-1", "sumanth@example.com") is None


def test_invite_user_rejects_unknown_role():
    with pytest.raises(ValueError):
        auth_db.invite_user("sumanth@example.com", owner="sumanth_b", role="superuser")


def test_list_users_and_disable_returns_whether_a_row_matched():
    auth_db.invite_user("a@example.com", owner="sumanth_b")
    auth_db.invite_user("b@example.com", owner="sumanth_b", role="admin")

    emails = {u.email for u in auth_db.list_users()}
    assert emails == {"a@example.com", "b@example.com"}

    assert auth_db.disable_user("a@example.com") is True
    assert auth_db.disable_user("nobody@example.com") is False
