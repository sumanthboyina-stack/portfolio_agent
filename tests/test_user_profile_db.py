from __future__ import annotations

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.domain import UserProfile
from portfolio_agent.tools.user_profile_db import (
    _db,
    get_user_profile,
    get_user_profile_by_owner,
    list_all_profiles,
    update_user_profile,
)


def test_get_user_profile_seeds_defaults_on_first_access(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")

    profile = get_user_profile()

    assert isinstance(profile, UserProfile)
    assert profile.id == 1
    assert profile.display_name is None
    assert profile.base_currency == "USD"
    assert profile.timezone == "America/Chicago"
    assert profile.max_sector_pct == 25.0
    assert profile.max_issuer_pct == 15.0
    assert profile.max_per_candidate_pct_of_cash == 0.4
    assert profile.max_post_trade_position_pct == 0.15
    assert profile.sector_exclusions == []
    assert profile.created_at
    assert profile.updated_at

    # Dict-compat API keeps working for legacy call-sites.
    assert profile["base_currency"] == "USD"
    assert profile.get("missing", "dflt") == "dflt"


def test_update_user_profile_round_trips(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")

    before = get_user_profile()
    updated = update_user_profile(
        display_name="Sumanth",
        max_sector_pct=30.0,
        sector_exclusions=["Tobacco", "Gambling"],
    )

    assert updated.display_name == "Sumanth"
    assert updated.max_sector_pct == 30.0
    assert updated.sector_exclusions == ["Tobacco", "Gambling"]
    assert updated.updated_at >= before.updated_at
    # created_at is never rewritten by an update
    assert updated.created_at == before.created_at

    # Untouched fields keep their defaults
    assert updated.max_issuer_pct == 15.0

    # A fresh read sees the same values (round trip through SQLite)
    again = get_user_profile()
    assert again.to_dict() == updated.to_dict()

    # sector_exclusions is stored as a JSON list string, not a Python repr
    with _db() as c:
        raw = c.execute("SELECT sector_exclusions FROM user_profile WHERE id = 1").fetchone()[0]
    assert raw == '["Tobacco", "Gambling"]'


def test_seed_never_overwrites_existing_row(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")

    update_user_profile(display_name="Keep me", max_issuer_pct=12.5)

    # Re-opening the DB re-runs the setup/seed path; the row must survive.
    for _ in range(3):
        profile = get_user_profile()
    assert profile.display_name == "Keep me"
    assert profile.max_issuer_pct == 12.5

    with _db() as c:
        assert c.execute("SELECT COUNT(*) FROM user_profile").fetchone()[0] == 1


def test_one_row_per_owner_is_enforced_by_schema(tmp_path, monkeypatch):
    """user_profile is a real per-owner table now (UNIQUE(owner), not
    CHECK(id = 1)) — a second row for the SAME owner is rejected, but a
    different owner gets their own row. See test_trusted_context_scoping.py
    for the authorized get_user_profile_for/update_user_profile_for path."""
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")

    get_user_profile()   # seeds LOCAL_OWNER's row
    with _db() as c:
        with pytest.raises(Exception):
            c.execute(
                "INSERT INTO user_profile (owner, created_at, updated_at) VALUES "
                "((SELECT owner FROM user_profile WHERE id = 1), 'x', 'x')"
            )
        c.execute("INSERT INTO user_profile (owner, created_at, updated_at) VALUES ('bob', 'x', 'x')")
        c.commit()
        assert c.execute("SELECT COUNT(*) FROM user_profile").fetchone()[0] == 2


def test_update_user_profile_rejects_unknown_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")

    with pytest.raises(ValueError, match="not_a_field"):
        update_user_profile(not_a_field=1)

    # Managed columns are not user-settable either
    with pytest.raises(ValueError, match="created_at"):
        update_user_profile(created_at="2000-01-01")

    # Nothing was written by the failed calls
    assert get_user_profile().display_name is None


def test_update_user_profile_with_no_fields_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")

    before = get_user_profile()
    after = update_user_profile()
    assert after.to_dict() == before.to_dict()


# ── Step 3: compliance fields ──────────────────────────────────────────────────

def test_compliance_defaults_and_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")

    p = get_user_profile()
    assert p.employer is None
    assert p.pre_clearance_required is True
    assert p.blackout_start is None and p.blackout_end is None
    assert p.disclaimer_accepted_at is None

    p = update_user_profile(
        employer="Acme Corp",
        pre_clearance_required=False,
        blackout_start="2026-10-01",
        blackout_end="2026-10-15",
        disclaimer_accepted_at="2026-09-21T10:00:00+00:00",
    )
    assert p.employer == "Acme Corp"
    assert p.pre_clearance_required is False
    assert p.blackout_start == "2026-10-01"
    assert p.blackout_end == "2026-10-15"
    assert p.disclaimer_accepted_at == "2026-09-21T10:00:00+00:00"

    # bool is persisted as 0/1 and read back as a real bool
    with _db() as c:
        assert c.execute("SELECT pre_clearance_required FROM user_profile").fetchone()[0] == 0
    assert get_user_profile().pre_clearance_required is False
    assert update_user_profile(pre_clearance_required=True).pre_clearance_required is True


def test_migrate_adds_compliance_columns_to_step1_table(tmp_path, monkeypatch):
    """A DB created before the compliance columns existed must be upgraded in
    place, keeping the existing row's values."""
    import sqlite3
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")

    conn = sqlite3.connect(str(tmp_path / "test.db"))
    conn.execute("""
        CREATE TABLE user_profile (
            id                            INTEGER PRIMARY KEY CHECK (id = 1),
            display_name                  TEXT,
            base_currency                 TEXT NOT NULL DEFAULT 'USD',
            timezone                      TEXT NOT NULL DEFAULT 'America/Chicago',
            max_sector_pct                REAL NOT NULL DEFAULT 25.0,
            max_issuer_pct                REAL NOT NULL DEFAULT 15.0,
            max_per_candidate_pct_of_cash REAL NOT NULL DEFAULT 0.4,
            max_post_trade_position_pct   REAL NOT NULL DEFAULT 0.15,
            sector_exclusions             TEXT NOT NULL DEFAULT '[]',
            created_at                    TEXT NOT NULL,
            updated_at                    TEXT NOT NULL
        )
    """)
    conn.execute(
        "INSERT INTO user_profile (id, display_name, max_sector_pct, created_at, updated_at) "
        "VALUES (1, 'Legacy', 33.0, 't0', 't0')"
    )
    conn.commit()
    conn.close()

    p = get_user_profile()
    assert p.display_name == "Legacy"
    assert p.max_sector_pct == 33.0
    assert p.created_at == "t0"
    # new columns present with their defaults
    assert p.pre_clearance_required is True
    assert p.employer is None
    assert update_user_profile(employer="Acme").employer == "Acme"


# ── Step 5: preferred horizons ─────────────────────────────────────────────────

def test_preferred_horizons_default_to_all_and_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")

    assert get_user_profile().preferred_horizons == ["5d", "21d", "63d", "250d"]
    p = update_user_profile(preferred_horizons=["5d", "63d"])
    assert p.preferred_horizons == ["5d", "63d"]
    assert get_user_profile().preferred_horizons == ["5d", "63d"]


# ── Per-owner rows (the "friends share your profile" fix) ─────────────────────

def test_get_user_profile_by_owner_auto_provisions_a_distinct_row_per_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")

    update_user_profile(display_name="Sumanth", max_sector_pct=40.0)   # LOCAL_OWNER's row

    bob = get_user_profile_by_owner("bob")
    assert bob.display_name is None          # bob gets his OWN defaults, not Sumanth's row
    assert bob.max_sector_pct == 25.0

    # It's a real row now, not a view onto LOCAL_OWNER's — an update to one
    # never touches the other.
    from portfolio_agent.services.context import RequestContext, _mint_identity_for_tests
    from portfolio_agent.tools.user_profile_db import update_user_profile_for
    bob_ctx = RequestContext(identity=_mint_identity_for_tests("bob"), source="test")
    update_user_profile_for(bob_ctx, display_name="Bob", max_sector_pct=10.0)

    assert get_user_profile().display_name == "Sumanth"
    assert get_user_profile().max_sector_pct == 40.0
    assert get_user_profile_by_owner("bob").display_name == "Bob"
    assert get_user_profile_by_owner("bob").max_sector_pct == 10.0


def test_list_all_profiles_includes_every_owner_not_just_local(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")

    update_user_profile(display_name="Sumanth", employer="Acme")
    get_user_profile_by_owner("bob")   # auto-provisions bob's row

    owners = {p.owner for p in list_all_profiles()}
    assert owners == {get_user_profile().owner, "bob"}


def test_migrate_adds_preferred_horizons_with_all_default(tmp_path, monkeypatch):
    import sqlite3
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")

    conn = sqlite3.connect(str(tmp_path / "test.db"))
    conn.execute("""
        CREATE TABLE user_profile (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            display_name TEXT, base_currency TEXT NOT NULL DEFAULT 'USD',
            timezone TEXT NOT NULL DEFAULT 'America/Chicago',
            max_sector_pct REAL NOT NULL DEFAULT 25.0, max_issuer_pct REAL NOT NULL DEFAULT 15.0,
            max_per_candidate_pct_of_cash REAL NOT NULL DEFAULT 0.4,
            max_post_trade_position_pct REAL NOT NULL DEFAULT 0.15,
            sector_exclusions TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )
    """)
    conn.execute("INSERT INTO user_profile (id, created_at, updated_at) VALUES (1, 't0', 't0')")
    conn.commit()
    conn.close()

    assert get_user_profile().preferred_horizons == ["5d", "21d", "63d", "250d"]
