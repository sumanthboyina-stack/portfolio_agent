"""
Investor service — the single authority for writing an investor's risk profile, emergency reserve, custom target
allocation and goals (all PRIVATE, owner-scoped) and for changing the global investor settings (admin-only).

Every operation
  * takes a RequestContext and acts only on the actor's own data (the owner is ctx.actor — never an id a page or a
    chat tool supplied);
  * runs in ONE transaction with an audit event (actor, operation, before/after, reason, request id), so a change and
    its audit record land together or not at all;
  * for the global settings, first requires an ENABLED admin (auth_users role = 'admin') — checked here, server-side,
    not by the UI.

Operations
  Private (the actor's own):
    get_investor_state            what applies to this person right now (computed defaults when there is no row)
    take_quiz                     score the optional 3-question quiz and record the result
    pick_profile                  choose a profile directly; above the quiz cap it needs an acknowledgment
    set_emergency_reserve         the one reserve amount in dollars (None = use the global default)
    set_limits_source             'profile' (limits from the profile) or 'custom' (the user's own numbers)
    set_custom_allocation / clear_custom_allocation
    add_goal / update_goal / remove_goal
  Admin-only (global):
    update_quiz_settings / reset_quiz_settings
    update_risk_profile_settings / reset_risk_profile_settings
    update_portfolio_settings / reset_portfolio_settings   (reserve, fund-data TTLs, look-through, fees, health snapshots)
    update_instrument_type_horizons / reset_instrument_type_horizons   (which forecast horizons each kind of security gets)

Nothing here, and nothing it writes, is ever read by the shared market-forecast path or the prediction pipeline.
"""

from __future__ import annotations

import math

from portfolio_agent.domain import (
    LIMITS_SOURCES, RISK_PROFILES, ProfileSource, RiskProfile,
)
from portfolio_agent.services.context import RequestContext, require_context
from portfolio_agent.tools import holdings_db as repo
from portfolio_agent.tools import investor_profile_db as pdb
from portfolio_agent.tools import investor_settings_db as sdb
from portfolio_agent.tools.risk_quiz import QuizResult, is_above_cap, score_quiz


class NotAdmin(PermissionError):
    """The actor is not an enabled administrator; global settings are admin-only."""


class AcknowledgmentRequired(ValueError):
    """The chosen profile is riskier than the quiz cap; the person must acknowledge it explicitly."""


# ── Authorization & audit ─────────────────────────────────────────────────────

from contextlib import contextmanager


@contextmanager
def _uow():
    """One transaction for a write + its audit event. Both stores' schemas are ensured FIRST: creating a table from a
    second connection while this one holds a write lock would fail with 'database is locked'."""
    sdb._ensure_once()
    pdb._ensure_schema_once()
    with repo.unit_of_work() as c:
        yield c


def require_admin(ctx: RequestContext) -> None:
    """Raise NotAdmin unless ctx's verified identity maps to an enabled auth_users row with role 'admin'."""
    from portfolio_agent.tools.auth_users_db import is_admin_owner
    require_context(ctx)
    if not is_admin_owner(ctx.actor):
        raise NotAdmin(f"{ctx.actor!r} is not an administrator; this setting is global and admin-only")


def _audit(ctx: RequestContext, conn, operation: str, *, before: dict | None = None, after: dict | None = None,
           reason: str | None = None) -> None:
    portfolio = repo.get_portfolio_by_owner(ctx.actor, conn=conn)           # None for an owner with no portfolio yet
    repo.record_audit_event(actor=ctx.actor, source=ctx.source, request_id=ctx.request_id, operation=operation,
                            portfolio_id=portfolio["portfolio_id"] if portfolio else None,
                            before=before, after=after, reason=reason, conn=conn)


def _snapshot(row: dict | None) -> dict | None:
    if row is None:
        return None
    return {k: row.get(k) for k in ("profile", "source", "cap_profile", "above_cap_acknowledged",
                                    "emergency_reserve_amount", "quiz_version")}


# ── Reading ───────────────────────────────────────────────────────────────────

def get_investor_state(ctx: RequestContext) -> dict:
    """
    What applies to the actor right now. With no investor_profile row the answer is computed, not stored: MODERATE,
    source DEFAULT, and the global default reserve. Keys: profile, source, is_default, cap_profile, above_cap (the
    pick is riskier than the quiz cap), above_cap_acknowledged, emergency_reserve_amount (effective),
    emergency_reserve_source ('USER' | 'DEFAULT'), custom_allocation (or None), quiz_version.
    """
    require_context(ctx)
    row = pdb.get_active_investor_profile(ctx.actor)
    default_reserve = sdb.get_portfolio_settings()["default_emergency_reserve_amount"]
    own = row["emergency_reserve_amount"] if row else None
    profile = row["profile"] if row else RiskProfile.MODERATE.value
    return {
        "profile": profile,
        "source": row["source"] if row else ProfileSource.DEFAULT.value,
        "is_default": row is None or row["source"] == ProfileSource.DEFAULT.value,
        "cap_profile": row["cap_profile"] if row else None,
        "above_cap": bool(row) and is_above_cap(profile, row["cap_profile"]),
        "above_cap_acknowledged": bool(row and row["above_cap_acknowledged"]),
        "emergency_reserve_amount": float(own if own is not None else default_reserve),
        "emergency_reserve_source": "USER" if own is not None else "DEFAULT",
        "custom_allocation": pdb.get_active_custom_allocation(ctx.actor),
        "quiz_version": row["quiz_version"] if row else None,
        "quiz_answers": row["quiz_answers"] if row else None,
    }


# ── Private writes ────────────────────────────────────────────────────────────

def take_quiz(ctx: RequestContext, answers: dict) -> QuizResult:
    """
    Score the quiz (every question optional; unanswered = the middle answer; result = the more conservative of the
    cap and the preference) and record it as the actor's profile with source QUIZ. The reserve is carried over.
    Returns the QuizResult. A previous 'above the cap' acknowledgment does not carry over: the new result stands alone.
    """
    require_context(ctx)
    with _uow() as c:
        result = score_quiz(answers, sdb.get_quiz_settings(conn=c))
        before = pdb.get_active_investor_profile(ctx.actor, conn=c)
        row = pdb.replace_investor_profile(
            ctx.actor, profile=result.profile, source=ProfileSource.QUIZ.value,
            quiz_answers={k: v for k, v in answers.items()}, quiz_version=result.quiz_version,
            cap_profile=result.cap_profile if result.cap_answered else None, above_cap_acknowledged=False,
            emergency_reserve_amount=before["emergency_reserve_amount"] if before else None, conn=c)
        _audit(ctx, c, "take_risk_quiz", before=_snapshot(before),
               after=_snapshot(row) | {"preference_profile": result.preference_profile,
                                       "preference_points": result.preference_points,
                                       "unanswered": list(result.unanswered)})
    return result


def pick_profile(ctx: RequestContext, profile: str, *, acknowledge_above_cap: bool = False,
                 reason: str | None = None) -> dict:
    """
    Choose a profile directly (no quiz needed). If a quiz recorded a cap and the pick is riskier, the person must pass
    acknowledge_above_cap=True (else AcknowledgmentRequired); the acknowledgment is stored on the row and in the audit
    event, and the UI shows a small flag while it stands. Returns the active row.
    """
    require_context(ctx)
    if profile not in RISK_PROFILES:
        raise ValueError(f"profile must be one of {list(RISK_PROFILES)}, got {profile!r}")
    with _uow() as c:
        before = pdb.get_active_investor_profile(ctx.actor, conn=c)
        cap = before["cap_profile"] if before else None
        above = is_above_cap(profile, cap)
        if above and not acknowledge_above_cap:
            raise AcknowledgmentRequired(
                f"{profile.title()} is riskier than the {cap.title()} your answers suggested. "
                "Tick the acknowledgment to choose it anyway.")
        if before and before["profile"] == profile and before["above_cap_acknowledged"] == above:
            return before                                                         # nothing changes
        row = pdb.replace_investor_profile(
            ctx.actor, profile=profile, source=ProfileSource.PICKED.value,
            quiz_answers=before["quiz_answers"] if before else None, quiz_version=before["quiz_version"] if before else None,
            cap_profile=cap, above_cap_acknowledged=above,
            emergency_reserve_amount=before["emergency_reserve_amount"] if before else None, conn=c)
        note = f"picked above the quiz cap ({cap}) — acknowledged" if above else None
        _audit(ctx, c, "pick_investor_profile", before=_snapshot(before), after=_snapshot(row),
               reason=" · ".join(x for x in (reason, note) if x) or None)
        return row


def set_emergency_reserve(ctx: RequestContext, amount: float | None, *, reason: str | None = None) -> dict:
    """
    The actor's own emergency cash reserve in DOLLARS (>= 0), or None to go back to the global default. It is one
    value — not a goal and not a number of months. Only USD is supported (the portfolio's base currency).
    """
    require_context(ctx)
    new_amount: float | None = None
    if amount is not None:
        try:
            new_amount = float(amount)
        except (TypeError, ValueError):
            raise ValueError(f"emergency reserve must be a dollar amount, got {amount!r}")
        if not math.isfinite(new_amount) or new_amount < 0:
            raise ValueError("emergency reserve cannot be negative")
        new_amount = round(new_amount, 2)
        from portfolio_agent.tools.user_profile_db import get_user_profile_for
        if (get_user_profile_for(ctx).base_currency or "USD").upper() != "USD":
            raise ValueError("the emergency reserve is a USD amount and your base currency is not USD")
    with _uow() as c:
        before = pdb.get_active_investor_profile(ctx.actor, conn=c)
        if (before["emergency_reserve_amount"] if before else None) == new_amount:
            return before or {}
        row = pdb.replace_investor_profile(
            ctx.actor, profile=before["profile"] if before else RiskProfile.MODERATE.value,
            source=before["source"] if before else ProfileSource.DEFAULT.value,
            quiz_answers=before["quiz_answers"] if before else None, quiz_version=before["quiz_version"] if before else None,
            cap_profile=before["cap_profile"] if before else None,
            above_cap_acknowledged=bool(before and before["above_cap_acknowledged"]),
            emergency_reserve_amount=new_amount, conn=c)
        _audit(ctx, c, "set_emergency_reserve", before=_snapshot(before), after=_snapshot(row), reason=reason)
        return row


def set_limits_source(ctx: RequestContext, limits_source: str, *, reason: str | None = None) -> str:
    """'profile': the numeric limits come from the investor profile's settings. 'custom': the user's own user_profile
    numbers win (what every pre-existing user has)."""
    from portfolio_agent.tools.user_profile_db import get_user_profile_for, update_user_profile_for
    require_context(ctx)
    if limits_source not in LIMITS_SOURCES:
        raise ValueError(f"limits_source must be one of {sorted(LIMITS_SOURCES)}, got {limits_source!r}")
    before = get_user_profile_for(ctx).limits_source
    if before == limits_source:
        return limits_source
    update_user_profile_for(ctx, limits_source=limits_source)
    with _uow() as c:
        _audit(ctx, c, "set_limits_source", before={"limits_source": before}, after={"limits_source": limits_source},
               reason=reason)
    return limits_source


def set_custom_allocation(ctx: RequestContext, allocation: dict, *, reason: str | None = None) -> dict:
    """Replace the profile's target allocation with the actor's own bands (validated: targets sum to 100,
    min <= target <= max, only allocatable asset classes)."""
    require_context(ctx)
    clean = sdb.validate_allocation(allocation, what="custom allocation")
    with _uow() as c:
        before = pdb.get_active_custom_allocation(ctx.actor, conn=c)
        pdb.replace_custom_allocation(ctx.actor, clean, conn=c)
        _audit(ctx, c, "set_custom_allocation", before={"allocation": before}, after={"allocation": clean}, reason=reason)
    return clean


def clear_custom_allocation(ctx: RequestContext, *, reason: str | None = None) -> bool:
    """'Use profile default': supersede the custom allocation. True if there was one."""
    require_context(ctx)
    with _uow() as c:
        before = pdb.get_active_custom_allocation(ctx.actor, conn=c)
        if before is None:
            return False
        pdb.clear_custom_allocation(ctx.actor, conn=c)
        _audit(ctx, c, "clear_custom_allocation", before={"allocation": before}, after={"allocation": None}, reason=reason)
    return True


def add_goal(ctx: RequestContext, *, goal_type: str, name: str, target_amount: float | None = None,
             target_date: str | None = None) -> dict:
    require_context(ctx)
    with _uow() as c:
        goal = pdb.add_goal(ctx.actor, goal_type=goal_type, name=name, target_amount=target_amount,
                            target_date=target_date, conn=c)
        _audit(ctx, c, "add_goal", after={k: goal[k] for k in ("goal_id", "goal_type", "name", "target_amount",
                                                                 "target_date", "status")})
        return goal


def update_goal(ctx: RequestContext, goal_id: int, **fields) -> dict:
    """Another owner's goal id is indistinguishable from a missing one (LookupError)."""
    require_context(ctx)
    with _uow() as c:
        before = pdb.get_goal(ctx.actor, goal_id, conn=c)
        if before is None:
            raise LookupError(f"goal {goal_id} does not exist")
        pdb.update_goal(ctx.actor, goal_id, conn=c, **fields)
        after = pdb.get_goal(ctx.actor, goal_id, conn=c)
        _audit(ctx, c, "update_goal", before=before, after=after)
        return after


def remove_goal(ctx: RequestContext, goal_id: int) -> None:
    require_context(ctx)
    with _uow() as c:
        before = pdb.get_goal(ctx.actor, goal_id, conn=c)
        if before is None:
            raise LookupError(f"goal {goal_id} does not exist")
        pdb.delete_goal(ctx.actor, goal_id, conn=c)
        _audit(ctx, c, "remove_goal", before=before)


def list_goals(ctx: RequestContext, *, include_done: bool = True) -> list[dict]:
    require_context(ctx)
    return pdb.list_goals(ctx.actor, include_done=include_done)


# ── Admin-only: global settings ───────────────────────────────────────────────

def _admin_write(ctx: RequestContext, operation: str, do, before_fn, reason: str | None) -> dict:
    require_admin(ctx)
    with _uow() as c:
        before = before_fn(c)
        after = do(c)
        _audit(ctx, c, operation, before=before, after=after, reason=reason)
        return after


def update_quiz_settings(ctx: RequestContext, *, expected_version: int | None = None, reason: str | None = None,
                         **fields) -> dict:
    return _admin_write(ctx, "update_quiz_settings",
                        lambda c: sdb.update_quiz_settings(updated_by=ctx.actor, expected_version=expected_version,
                                                           conn=c, **fields),
                        lambda c: sdb.get_quiz_settings(conn=c), reason)


def reset_quiz_settings(ctx: RequestContext, *, reason: str | None = None) -> dict:
    return _admin_write(ctx, "reset_quiz_settings", lambda c: sdb.reset_quiz_settings_to_defaults(updated_by=ctx.actor, conn=c),
                        lambda c: sdb.get_quiz_settings(conn=c), reason)


def update_risk_profile_settings(ctx: RequestContext, profile: str, *, expected_version: int | None = None,
                                 reason: str | None = None, **fields) -> dict:
    return _admin_write(ctx, "update_risk_profile_settings",
                        lambda c: sdb.update_risk_profile_settings(profile, updated_by=ctx.actor,
                                                                   expected_version=expected_version, conn=c, **fields),
                        lambda c: sdb.get_risk_profile_settings(profile, conn=c), reason)


def reset_risk_profile_settings(ctx: RequestContext, profile: str | None = None, *, reason: str | None = None) -> dict:
    def _do(c):
        out = sdb.reset_risk_profile_settings_to_defaults(updated_by=ctx.actor, profile=profile, conn=c)
        return out[profile] if profile else out          # one profile -> its settings; none -> {profile: settings}

    return _admin_write(ctx, "reset_risk_profile_settings", _do,
                        lambda c: sdb.get_risk_profile_settings(profile, conn=c) if profile else sdb.get_risk_profile_settings(conn=c),
                        reason)


def update_portfolio_settings(ctx: RequestContext, *, expected_version: int | None = None, reason: str | None = None,
                              **fields) -> dict:
    return _admin_write(ctx, "update_portfolio_settings",
                        lambda c: sdb.update_portfolio_settings(updated_by=ctx.actor, expected_version=expected_version,
                                                                conn=c, **fields),
                        lambda c: sdb.get_portfolio_settings(conn=c), reason)


def reset_portfolio_settings(ctx: RequestContext, *, reason: str | None = None) -> dict:
    return _admin_write(ctx, "reset_portfolio_settings",
                        lambda c: sdb.reset_portfolio_settings_to_defaults(updated_by=ctx.actor, conn=c),
                        lambda c: sdb.get_portfolio_settings(conn=c), reason)


def update_instrument_type_horizons(ctx: RequestContext, horizons: dict, *, expected_version: int | None = None,
                                    reason: str | None = None) -> dict:
    """Admin: which forecast horizons (days) each instrument type gets — stored in engine_settings. One transaction with
    its audit event (the change and its before/after record land together or not at all)."""
    require_admin(ctx)
    from portfolio_agent.tools import engine_settings_db as esdb
    esdb.get_engine_settings()                       # make sure the table exists BEFORE the write transaction opens
    with _uow() as c:
        before = dict(esdb.get_engine_settings(conn=c)["instrument_type_horizons"])
        after = esdb.update_engine_settings(updated_by=ctx.actor, expected_version=expected_version,
                                            instrument_type_horizons=horizons, conn=c)["instrument_type_horizons"]
        _audit(ctx, c, "update_instrument_type_horizons", before=before, after=after, reason=reason)
    return after


def reset_instrument_type_horizons(ctx: RequestContext, *, reason: str | None = None) -> dict:
    require_admin(ctx)
    from portfolio_agent.tools import engine_settings_db as esdb
    return update_instrument_type_horizons(ctx, {t: list(h) for t, h in esdb.SEED_INSTRUMENT_TYPE_HORIZONS.items()},
                                           reason=reason or "reset to defaults")


def update_horizon_bucket_edges(ctx: RequestContext, edges: dict, *, expected_version: int | None = None,
                                reason: str | None = None) -> dict:
    """Admin: the flat / significant / strong return cut-offs per forecast horizon ({horizon: {flat, strong, significant}},
    fractions; a partial dict changes only the horizons given). A change bumps engine_settings.bucket_edges_version and
    appends the table to bucket_edges_history; predictions already made keep the edges they were made under. One
    transaction with its audit event. Returns {"version": int, "edges": {horizon: {...}}}."""
    require_admin(ctx)
    from portfolio_agent.tools import bucket_edges as be
    from portfolio_agent.tools import engine_settings_db as esdb
    esdb.get_engine_settings()                       # make sure the table exists BEFORE the write transaction opens
    with _uow() as c:
        b = esdb.get_engine_settings(conn=c)
        before = {"version": b["bucket_edges_version"], "edges": {h: e.to_dict() for h, e in b["horizon_bucket_edges"].items()}}
        a = esdb.update_engine_settings(updated_by=ctx.actor, expected_version=expected_version,
                                        horizon_bucket_edges=edges, conn=c)
        after = {"version": a["bucket_edges_version"], "edges": {h: e.to_dict() for h, e in a["horizon_bucket_edges"].items()}}
        _audit(ctx, c, "update_horizon_bucket_edges", before=before, after=after, reason=reason)
    be.clear_cache()
    return after


def reset_horizon_bucket_edges(ctx: RequestContext, *, reason: str | None = None) -> dict:
    from portfolio_agent.tools import bucket_edges as be
    return update_horizon_bucket_edges(ctx, {h: e.to_dict() for h, e in be.SEED_EDGES.items()},
                                       reason=reason or "reset to defaults")


def update_horizon_cadence(ctx: RequestContext, cadence: dict, *, expected_version: int | None = None,
                           reason: str | None = None) -> dict:
    """Admin: how often the scheduled batch generates each horizon ({horizon_days: rule}; rules in
    engine_settings_db.CADENCE_RULES, "never" turns a horizon's routine run off). Stored in engine_settings; one transaction
    with its audit event. Returns the full {horizon_days: rule} in force afterwards."""
    require_admin(ctx)
    from portfolio_agent.tools import engine_settings_db as esdb
    esdb.get_engine_settings()                       # make sure the table exists BEFORE the write transaction opens
    with _uow() as c:
        before = dict(esdb.get_engine_settings(conn=c)["horizon_cadence"])
        after = esdb.update_engine_settings(updated_by=ctx.actor, expected_version=expected_version,
                                            horizon_cadence=cadence, conn=c)["horizon_cadence"]
        _audit(ctx, c, "update_horizon_cadence", before=before, after=after, reason=reason)
    return after


def reset_horizon_cadence(ctx: RequestContext, *, reason: str | None = None) -> dict:
    from portfolio_agent.tools import engine_settings_db as esdb
    return update_horizon_cadence(ctx, dict(esdb.SEED_HORIZON_CADENCE), reason=reason or "reset to defaults")


def upsert_fund_issuer_source(ctx: RequestContext, ticker: str, issuer: str, url: str, enabled: bool, *, notes: str | None = None,
                              expected_version: int | None = None, reason: str | None = None) -> dict:
    """Admin: add or change the issuer holdings file (iShares / SPDR) used for one fund. The URL must be https on the issuer's own domain.
    Rows are seeded disabled; nothing is downloaded from an issuer until one is enabled here. One transaction with its audit event."""
    require_admin(ctx)
    from portfolio_agent.tools import fund_data_db as fdb
    from portfolio_agent.tools.issuer_holdings import validate_issuer_url
    t = (ticker or "").strip().upper()
    if not t:
        raise ValueError("ticker is required")
    clean_url = validate_issuer_url(issuer, url)
    fdb._ensure_once()
    with _uow() as c:
        before = fdb.get_issuer_source(t, enabled_only=False, conn=c)
        after = fdb.upsert_issuer_source(t, issuer, clean_url, enabled, notes=notes, updated_by=ctx.actor,
                                         expected_version=expected_version, conn=c)
        _audit(ctx, c, "upsert_fund_issuer_source", before=before, after=after, reason=reason)
    return after


def refresh_fund_data_now(ctx: RequestContext, *, reason: str | None = None) -> dict:
    """Admin "refresh now": re-fetch every held fund through the provider chain, ignoring the freshness window. Network happens outside any
    transaction (inside refresh_fund_data); the audit event is its own short transaction. Returns refresh_all_fund_data's summary."""
    require_admin(ctx)
    from portfolio_agent.services.fund_data_service import refresh_all_fund_data
    summary = refresh_all_fund_data(ctx, force=True)
    with _uow() as c:
        _audit(ctx, c, "refresh_fund_data_now", after={"funds": summary["funds"], "counts": summary["counts"]}, reason=reason)
    return summary
