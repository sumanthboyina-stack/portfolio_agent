"""
privacy.collect_private_terms() — repro test for the "Profile page uses your
row" review's privacy-side finding: the guard only knew LOCAL_OWNER's
name/employer, so another member's name/employer could leak into a
shared/market-only artifact untouched. Fixed by reading
user_profile_db.list_all_profiles() (every owner) instead of
get_user_profile() (LOCAL_OWNER only).
"""
from __future__ import annotations

import portfolio_agent.tools.db as db_module
import pytest

from portfolio_agent.privacy import collect_private_terms, find_private_terms
from portfolio_agent.services.context import RequestContext, _mint_identity_for_tests
from portfolio_agent.tools.user_profile_db import update_user_profile, update_user_profile_for


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")


def test_collect_private_terms_includes_every_owners_name_and_employer():
    update_user_profile(display_name="Sumanth Boyina", employer="Skyzone")

    bob = RequestContext(identity=_mint_identity_for_tests("bob"), source="test")
    update_user_profile_for(bob, display_name="Bob Robertson", employer="Northwind Bank")

    terms = collect_private_terms()

    assert "Sumanth Boyina" in terms and "Skyzone" in terms
    assert "Bob Robertson" in terms and "Northwind Bank" in terms


def test_find_private_terms_catches_a_non_local_owners_name():
    """The bug this guards against: a shared forecast mentioning a FRIEND's
    name used to sail through untouched because collect_private_terms() never
    looked past LOCAL_OWNER's own profile row."""
    bob = RequestContext(identity=_mint_identity_for_tests("bob"), source="test")
    update_user_profile_for(bob, display_name="Bob Robertson")

    terms = collect_private_terms()
    found = find_private_terms("Bob Robertson mentioned this stock on a call", terms)
    assert found == ["Bob Robertson"]
