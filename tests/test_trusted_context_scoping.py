"""
Phase 2: trusted private context and scoped repositories.

WHAT THIS PHASE BUILT
══════════════════════
1. Identity / verify_identity() (services/context.py) — an immutable,
   sentinel-guarded principal type. Identity(subject=..., _proof=...) raises
   AuthenticationError unless _proof is the module-private object only
   verify_identity() (production) or _mint_identity_for_tests() (tests, to
   simulate a SECOND verified principal for cross-user tests — there is no
   real multi-user auth yet) can supply. A hand-typed string, a UI form value,
   or a model's output can never become an Identity by itself.
2. RequestContext now carries `identity: Identity` instead of a bare `actor:
   str` field; `.actor` is a read-only property derived from it. This is
   fully backward compatible — every production call site already went
   through local_context()/system_context()/.child(), never constructed
   RequestContext directly (confirmed by grep; only test files did, and only
   to build a deliberately-unauthorized context for a NotAuthorized test).
3. require_context(ctx) (services/context.py) — raises a clear
   AuthenticationError for a missing/invalid context instead of an incidental
   AttributeError three calls deep. account_service.resolve_portfolio and
   user_profile_db.get_user_profile_for both call it first, before touching
   any private data.
4. Singleton tables migrated to carry an `owner` column, backfilled to
   LOCAL_OWNER (the one explicitly configured existing owner — never "the
   first new login") via an idempotent migration that runs on every schema
   setup and is a no-op once owner is set:
     user_profile        (id still CHECK'd to 1 — see note below)
     user_notifications   (same)
     restricted_list       (ticker stays the primary key)
     watchlist              (ticker stays the primary key)
   Each gained an authorized read path: get_user_profile_for/
   update_user_profile_for, get_user_notifications_for, list_restricted_for,
   load_watchlist_tickers_for. The pre-existing unscoped functions
   (get_user_profile, get_user_notifications, list_restricted,
   load_watchlist_tickers, etc.) are UNCHANGED — every current production
   caller still uses them (see inventory below).
5. pipeline_skip (admin pipeline exclusions) was deliberately NOT touched —
   per the phase's own instruction to keep administrator exclusions separate
   from user preferences, it has no owner column and never will in this
   design; only restricted_list (a personal, per-user compliance blocklist)
   was scoped.
6. account_service.py gained authorized READ functions — list_holdings_for,
   list_accounts_for, get_cash_balances_for, list_import_runs_for,
   get_price_history_for — mirroring the authorization account_service's
   WRITE operations already had. Each calls resolve_portfolio(ctx) before
   touching a row. The underlying holdings_db reads stay global/unscoped
   (single-portfolio deployment) — these wrappers are the authorized
   boundary going forward, not a new storage model.
7. services/private_cache.py::AuthorizedCache — a tiny process-local TTL
   cache keyed by (verified identity, resource key) whose get_or_compute()
   calls resolve_portfolio(ctx) BEFORE consulting the cache, cache hit or
   not. This is the concrete mechanism proving "authorization before cache
   hits": a value cached while an identity was authorized is unreachable
   once that identity's portfolio is gone, even though the cache entry is
   still sitting in memory. Nothing is wired to use it yet (see inventory).

WHY user_profile/user_notifications STILL ENFORCE id = 1
──────────────────────────────────────────────────────────
This phase adds the SCOPE mechanism (owner column + authorized path) without
lifting the CHECK(id = 1) constraint that makes each table a true singleton.
Lifting it would itself start enabling multi-user storage ahead of the
isolation gate the project instructions require staying closed — so today
these tables can still only ever hold LOCAL_OWNER's row, by construction, no
matter what identity a caller presents. The authorized functions are exercised
here against that one real row plus a `_mint_identity_for_tests`-simulated
second identity that intentionally has NO row, to prove the isolation logic
holds even though only one owner can exist in production right now.

INVENTORY — NOT migrated this phase (read this before enabling anything)
══════════════════════════════════════════════════════════════════════════
  - Every existing production caller of get_user_profile(), update_user_
    profile(), get_user_notifications(), list_restricted(), is_restricted(),
    list_pipeline_skip() (correctly, unowned), load_watchlist_tickers(),
    holdings_db.get_holdings()/list_accounts()/get_cash_balances()/
    get_import_runs()/get_price_history() still uses the UNSCOPED path —
    clearance.py, weight_engine/market_context (profile reads for privacy
    term collection), every web/*.py page and web/chat/tools.py, the batch
    pipelines, scenarios/prepare.py. None of these were rewired onto the
    *_for functions in this phase.
  - update_user_notifications_for (write side) was not added — only the
    read side (get_user_notifications_for).
  - add_restricted/remove_restricted/add_tickers/remove_tickers were updated
    only so NEW rows are stamped with LOCAL_OWNER immediately (not left NULL
    until the next migration pass) — they are still unscoped write entry
    points, not ctx-gated.
  - scenarios/prepare.py and web/chat/tools.py were NOT threaded with
    RequestContext at all — they still call get_user_profile()/
    get_all_latest_predictions() etc. with no ctx. Flagged, not touched.
  - "portfolio history" beyond price history (get_price_history_for was
    added) — scoring/validation dashboards, calibration data etc. — still
    read via validation_engine's existing scope filters (SHARED_SCOPES /
    LEGACY_REVIEW_SCOPES from Phase 1), not owner-based; out of scope here.
  - AuthorizedCache exists and is tested standalone; nothing in the app
    (including portfolio_assessment.py) uses it yet.

Given this inventory, hosted multi-user mode MUST stay blocked: the schema
now has the scope column everywhere this phase touched, but the majority of
read paths in the app still bypass it entirely.

EXISTING TEST STATUS: `pytest tests/ -q` → 243 passed, 0 failed, before this
phase's additions — no pre-existing failures to report separately.
"""
from __future__ import annotations

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.domain import LOCAL_OWNER
from portfolio_agent.services import account_service
from portfolio_agent.services.context import (
    AuthenticationError, Identity, RequestContext, _mint_identity_for_tests,
    local_context, require_context, verify_identity,
)
from portfolio_agent.services.private_cache import AuthorizedCache
from portfolio_agent.tools import holdings_db as repo
from portfolio_agent.tools import restricted_list_db, user_profile_db, watchlist_db


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")


def _seed_second_owner(owner: str) -> None:
    """Directly insert a portfolios row for a second, test-only owner — there is
    no production path to create one (single-user deployment); this is purely
    to exercise the authorization/isolation logic against a real second row."""
    with repo.unit_of_work() as conn:
        conn.execute("INSERT INTO portfolios (owner) VALUES (?)", (owner,))


def _revoke_owner(owner: str) -> None:
    with repo.unit_of_work() as conn:
        conn.execute("DELETE FROM portfolios WHERE owner = ?", (owner,))


# ── Identity: immutable, verification-only construction ──────────────────────

def test_identity_cannot_be_constructed_without_the_verification_proof():
    with pytest.raises(AuthenticationError):
        Identity(subject="bob", _proof=object())    # a hand-rolled fake proof


def test_verify_identity_always_resolves_to_the_configured_local_owner():
    assert verify_identity().subject == LOCAL_OWNER
    assert local_context("test").actor == LOCAL_OWNER


def test_request_context_rejects_a_bare_typed_actor_kwarg():
    with pytest.raises(TypeError):
        RequestContext(actor="someone-typed-this", source="test")   # no such field anymore


def test_mint_identity_for_tests_still_requires_the_real_proof_path():
    """Even the test helper can't be bypassed by hand-constructing Identity."""
    ident = _mint_identity_for_tests("bob")
    assert ident.subject == "bob"
    with pytest.raises(AuthenticationError):
        Identity(subject="bob", _proof=None)


# ── Missing context fails cleanly, not with an incidental AttributeError ─────

def test_missing_context_fails_with_a_clear_error_not_an_attributeerror():
    with pytest.raises(AuthenticationError):
        require_context(None)
    with pytest.raises(AuthenticationError):
        account_service.resolve_portfolio(None)
    with pytest.raises(AuthenticationError):
        account_service.list_holdings_for(None)
    with pytest.raises(AuthenticationError):
        user_profile_db.get_user_profile_for(None)


def test_missing_context_fails_for_a_spoofed_non_context_object():
    """A dict or plain string masquerading as a context is rejected the same way."""
    with pytest.raises(AuthenticationError):
        account_service.resolve_portfolio({"actor": LOCAL_OWNER})
    with pytest.raises(AuthenticationError):
        account_service.resolve_portfolio("local")


# ── Cross-user reads/writes fail ──────────────────────────────────────────────

def test_cross_user_cannot_read_or_write_holdings():
    from tests.helpers_holdings import seed_holding
    seed_holding({"ticker": "AAPL", "shares": 1, "avg_cost": 100.0, "cost_basis_total": 100.0,
                 "account_name": "Individual", "account_number": "X1", "account_type": "TAXABLE"},
                "fidelity", "2026-09-01")

    stranger = RequestContext(identity=_mint_identity_for_tests("stranger"), source="test")
    with pytest.raises(account_service.NotAuthorized):
        account_service.list_holdings_for(stranger)
    with pytest.raises(account_service.NotAuthorized):
        account_service.list_accounts_for(stranger)
    with pytest.raises(account_service.NotAuthorized):
        account_service.get_cash_balances_for(stranger)
    with pytest.raises(account_service.NotAuthorized):
        account_service.create_account(stranger, broker="manual", display_name="Theirs")

    # The legitimate owner's read is unaffected.
    assert len(account_service.list_holdings_for(local_context("test"))) == 1


def test_cross_user_cannot_read_or_write_user_profile():
    user_profile_db.update_user_profile_for(local_context("test"), display_name="Real Owner")

    stranger = RequestContext(identity=_mint_identity_for_tests("stranger"), source="test")
    with pytest.raises(account_service.NotAuthorized):
        user_profile_db.get_user_profile_for(stranger)
    with pytest.raises(account_service.NotAuthorized):
        user_profile_db.update_user_profile_for(stranger, display_name="Hijacked")

    # The legitimate owner's row is untouched by the failed cross-user attempt.
    assert user_profile_db.get_user_profile_for(local_context("test")).display_name == "Real Owner"


def test_a_second_verified_identity_with_no_portfolio_is_refused_not_given_an_empty_result():
    """Authorization failure raises — it never silently degrades to 'here's an empty list.'"""
    bob = RequestContext(identity=_mint_identity_for_tests("bob"), source="test")
    with pytest.raises(account_service.NotAuthorized):
        account_service.list_holdings_for(bob)


# ── Repeated migrations preserve ownership ────────────────────────────────────

def test_repeated_user_profile_migrations_preserve_id_and_owner():
    p1 = user_profile_db.get_user_profile()
    assert p1.id == 1   # owner isn't on the UserProfile domain object; checked via raw row below

    # Re-run the migration path several times (every _db() call does) and confirm
    # id/owner never move, and an intervening edit is never reverted by a later pass.
    for _ in range(3):
        with user_profile_db._db() as conn:
            pass   # each entry re-runs _create_schema + _migrate + _seed
    user_profile_db.update_user_profile(display_name="Stable Name")
    for _ in range(3):
        with user_profile_db._db() as conn:
            pass
    row = user_profile_db.get_user_profile()
    assert row.id == 1
    assert row.display_name == "Stable Name"   # not reverted by a later migration pass

    with user_profile_db._db() as conn:
        raw = conn.execute("SELECT owner FROM user_profile WHERE id = 1").fetchone()
    assert raw["owner"] == LOCAL_OWNER


def test_repeated_restricted_list_and_watchlist_migrations_preserve_owner_and_dont_duplicate():
    restricted_list_db.add_restricted("AAPL", "insider list")
    watchlist_db.add_tickers(["NVDA"])

    for _ in range(3):
        with restricted_list_db._db():
            pass
        with watchlist_db._db():
            pass

    restricted = restricted_list_db.list_restricted_for(local_context("test"))
    watch = watchlist_db.load_watchlist_tickers_for(local_context("test"))
    assert [r["ticker"] for r in restricted] == ["AAPL"]   # exactly one row, not duplicated
    assert watch == ["NVDA"]


# ── Revoked access cannot retrieve a warm private cache ───────────────────────

def test_revoked_access_cannot_retrieve_a_warm_private_cache():
    _seed_second_owner("bob")
    bob = RequestContext(identity=_mint_identity_for_tests("bob"), source="test")
    cache = AuthorizedCache(ttl_seconds=999)

    calls = []
    def _compute():
        calls.append(1)
        return {"secret": "bob's private result"}

    warm = cache.get_or_compute(bob, "assessment:AAPL", _compute)
    assert warm == {"secret": "bob's private result"} and len(calls) == 1

    hit = cache.get_or_compute(bob, "assessment:AAPL", _compute)
    assert hit == warm and len(calls) == 1   # served from cache, compute not called again

    _revoke_owner("bob")   # access revoked -- the warm entry is still sitting in memory

    with pytest.raises(account_service.NotAuthorized):
        cache.get_or_compute(bob, "assessment:AAPL", _compute)
    assert len(calls) == 1   # never recomputed either -- authorization failed before that could happen
