"""
The one effective portfolio policy — everything about how the actor wants their money invested that a computation may
use, normalized to one unit (percentage points, 0-100) and fingerprinted on exactly the fields that change a result.

Where each part comes from
  * the four numeric limits (sector / issuer / position / candidate-share-of-cash):
      user_profile.limits_source = 'custom'  -> the user's own user_profile numbers (what every pre-existing user has)
      user_profile.limits_source = 'profile' -> risk_profile_settings for the user's investor profile
  * the investor profile (CONSERVATIVE / MODERATE / AGGRESSIVE), its source (DEFAULT / QUIZ / PICKED) and whether an
    above-the-quiz-cap pick was acknowledged: the owner's active investor_profile row — MODERATE / DEFAULT when
    there is none (no row is required)
  * target allocation bands, the leveraged/inverse limit and the allowed instrument types: the profile's settings;
    the target allocation is replaced by the owner's custom allocation when they have one
  * the emergency reserve, in dollars: the owner's own amount if set, else portfolio_settings'
    default_emergency_reserve_amount ($3,000 seed). One value — not a goal, not a number of months.

Deliberately NOT part of the policy, and never touching the fingerprint: display_name, employer, notification and
horizon preferences, goals. Changing them must not invalidate anything derived from the policy (see
tools.portfolio_assessment's exposures cache, keyed in part by the fingerprint). Everything that IS in the policy
changes the fingerprint — including the reserve, the profile and the settings values behind it.

effective_policy_from_profile(profile) stays pure (no database): it builds the policy from the profile row alone,
using the code seed defaults for everything else. The authorized, DB-backed path is get_effective_policy(ctx).

Privacy: this module reads PRIVATE data (the investor profile) and must only ever be used on the owner-scoped path.
The shared market forecast and prediction pipeline never import it (see tests/test_investor_profile.py).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from portfolio_agent.domain import RiskProfile

# Where limits came from — also the user_profile.limits_source values.
LIMITS_PROFILE = "profile"
LIMITS_CUSTOM = "custom"


@dataclass(frozen=True)
class EffectivePolicy:
    max_sector_pct: float                # percentage points, 0-100
    max_issuer_pct: float
    max_position_pct: float              # profile's max_post_trade_position_pct (a 0-1 fraction) * 100 when custom
    max_candidate_pct_of_cash: float     # profile's max_per_candidate_pct_of_cash (a 0-1 fraction) * 100 when custom
    fingerprint: str
    # ── investor profile ──
    profile: str = RiskProfile.MODERATE.value
    profile_source: str = "DEFAULT"                       # DEFAULT | QUIZ | PICKED
    above_cap_acknowledged: bool = False
    # ── allocation & instrument rules (from the profile's settings; the user's custom allocation replaces the bands) ──
    target_allocation: dict = field(default_factory=dict)  # {asset_class: {target, min, max}} percentage points
    allocation_source: str = "PROFILE"                    # PROFILE | CUSTOM
    max_leveraged_inverse_pct: float = 0.0
    allowed_instrument_types: tuple = ()
    # ── emergency reserve (dollars) ──
    emergency_reserve_amount: float = 0.0
    emergency_reserve_source: str = "DEFAULT"             # USER | DEFAULT
    # ── provenance ──
    limits_source: str = LIMITS_CUSTOM
    settings_versions: dict = field(default_factory=dict)  # versions of the settings rows used (info; not fingerprinted)

    def to_dict(self) -> dict:
        return {
            "max_sector_pct": self.max_sector_pct, "max_issuer_pct": self.max_issuer_pct,
            "max_position_pct": self.max_position_pct, "max_candidate_pct_of_cash": self.max_candidate_pct_of_cash,
            "fingerprint": self.fingerprint, "profile": self.profile, "profile_source": self.profile_source,
            "above_cap_acknowledged": self.above_cap_acknowledged, "target_allocation": self.target_allocation,
            "allocation_source": self.allocation_source, "max_leveraged_inverse_pct": self.max_leveraged_inverse_pct,
            "allowed_instrument_types": list(self.allowed_instrument_types),
            "emergency_reserve_amount": self.emergency_reserve_amount,
            "emergency_reserve_source": self.emergency_reserve_source, "limits_source": self.limits_source,
            "settings_versions": self.settings_versions,
        }


def _fingerprint(payload: dict) -> str:
    """Canonical JSON of every value that changes a computation, floats rounded to 6 places, keys sorted."""
    def norm(v):
        if isinstance(v, float):
            return round(v, 6)
        if isinstance(v, dict):
            return {k: norm(x) for k, x in sorted(v.items())}
        if isinstance(v, (list, tuple)):
            return [norm(x) for x in v]
        return v
    text = json.dumps(norm(payload), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def build_effective_policy(profile, *, investor: dict | None, profile_settings: dict, portfolio_settings: dict,
                           custom_allocation: dict | None = None) -> EffectivePolicy:
    """
    Pure: assemble the policy from already-loaded inputs. *profile* is a UserProfile (the four custom limits and
    limits_source); *investor* the active investor_profile row or None (= MODERATE / DEFAULT); *profile_settings* the
    risk_profile_settings row FOR THE INVESTOR'S PROFILE; *portfolio_settings* the portfolio_settings row.
    """
    limits_source = getattr(profile, "limits_source", LIMITS_CUSTOM)
    if limits_source == LIMITS_PROFILE:
        sector = float(profile_settings["max_sector_pct"])
        issuer = float(profile_settings["max_issuer_pct"])
        position = float(profile_settings["max_position_pct"])
        candidate = float(profile_settings["max_candidate_pct_of_cash"])
    else:
        sector = float(profile.max_sector_pct)
        issuer = float(profile.max_issuer_pct)
        position = float(profile.max_post_trade_position_pct) * 100
        candidate = float(profile.max_per_candidate_pct_of_cash) * 100

    investor = investor or {}
    own_reserve = investor.get("emergency_reserve_amount")
    reserve = float(own_reserve if own_reserve is not None else portfolio_settings["default_emergency_reserve_amount"])
    allocation = custom_allocation if custom_allocation else profile_settings["allocation"]
    values = {
        "limits": [sector, issuer, position, candidate],
        "profile": investor.get("profile", RiskProfile.MODERATE.value),
        "profile_source": investor.get("source", "DEFAULT"),
        "above_cap_acknowledged": bool(investor.get("above_cap_acknowledged", False)),
        "allocation": allocation,
        "allocation_source": "CUSTOM" if custom_allocation else "PROFILE",
        "max_leveraged_inverse_pct": float(profile_settings["max_leveraged_inverse_pct"]),
        "allowed_instrument_types": sorted(profile_settings["allowed_instrument_types"]),
        "emergency_reserve_amount": reserve,
        "emergency_reserve_source": "USER" if own_reserve is not None else "DEFAULT",
        "limits_source": limits_source,
    }
    return EffectivePolicy(
        max_sector_pct=sector, max_issuer_pct=issuer, max_position_pct=position, max_candidate_pct_of_cash=candidate,
        fingerprint=_fingerprint(values),
        profile=values["profile"], profile_source=values["profile_source"],
        above_cap_acknowledged=values["above_cap_acknowledged"], target_allocation=allocation,
        allocation_source=values["allocation_source"], max_leveraged_inverse_pct=values["max_leveraged_inverse_pct"],
        allowed_instrument_types=tuple(values["allowed_instrument_types"]), emergency_reserve_amount=reserve,
        emergency_reserve_source=values["emergency_reserve_source"], limits_source=limits_source,
        settings_versions={"risk_profile_settings": profile_settings.get("version"),
                           "portfolio_settings": portfolio_settings.get("version")},
    )


def effective_policy_from_profile(profile) -> EffectivePolicy:
    """
    Pure, no database: the policy for *profile* (a UserProfile, or anything with its attributes) with every other
    input at its code SEED default — MODERATE, source DEFAULT, the seeded $3,000 reserve. This is the function the
    optimizer/decision tests use; the authorized, DB-backed path is get_effective_policy(ctx).
    """
    from portfolio_agent.tools.investor_settings_db import DEFAULT_EMERGENCY_RESERVE_AMOUNT, SEED_RISK_PROFILES
    seed = SEED_RISK_PROFILES[RiskProfile.MODERATE.value]
    return build_effective_policy(
        profile, investor=None,
        profile_settings={**seed, "allocation": seed["allocation"], "version": None},
        portfolio_settings={"default_emergency_reserve_amount": DEFAULT_EMERGENCY_RESERVE_AMOUNT, "version": None})


def get_effective_policy(ctx) -> EffectivePolicy:
    """
    Authorized: reads ctx's OWN profile (user_profile_db.get_user_profile_for, which auto-provisions it and never
    serves another owner's), investor profile, custom allocation and the global settings. An owner with no
    investor_profile row gets MODERATE / DEFAULT and the global default reserve. A missing or unreadable settings row
    raises — a policy is never silently filled in with made-up numbers.
    """
    from portfolio_agent.services.context import require_context

    require_context(ctx)
    return get_effective_policy_for_owner(ctx.actor)


def get_effective_policy_for_owner(owner: str) -> EffectivePolicy:
    """
    The same policy from a raw owner string, for server-side callers that already trust *owner* and have no
    RequestContext to construct (a chat tool reading the owner the web layer put in its session — see
    user_profile_db.get_user_profile_by_owner). Reads only that owner's own rows. Authorization is the caller's:
    get_effective_policy(ctx) is the checked entry point.
    """
    from portfolio_agent.tools import investor_profile_db as pdb
    from portfolio_agent.tools import investor_settings_db as sdb
    from portfolio_agent.tools.user_profile_db import get_user_profile_by_owner

    profile = get_user_profile_by_owner(owner)
    investor = pdb.get_active_investor_profile(owner)
    name = (investor or {}).get("profile", RiskProfile.MODERATE.value)
    return build_effective_policy(
        profile, investor=investor, profile_settings=sdb.get_risk_profile_settings(name),
        portfolio_settings=sdb.get_portfolio_settings(), custom_allocation=pdb.get_active_custom_allocation(owner))
