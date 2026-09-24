"""
The one effective portfolio policy — the risk-relevant subset of the user's
profile (tools.user_profile_db), normalized to a single unit (percentage
points, 0-100) and fingerprinted on exactly those fields.

Deliberately narrow: display_name, employer, notification/preference fields
and anything else on the profile are NOT part of this policy and NEVER
touch the fingerprint — changing them must not invalidate anything derived
from this policy (see tools.portfolio_assessment's exposures cache, keyed
in part by this fingerprint). Only the four numeric limits below do.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class EffectivePolicy:
    max_sector_pct: float                # already 0-100 on the profile row
    max_issuer_pct: float                # already 0-100 on the profile row
    max_position_pct: float              # profile's max_post_trade_position_pct (a 0-1 fraction) * 100
    max_candidate_pct_of_cash: float     # profile's max_per_candidate_pct_of_cash (a 0-1 fraction) * 100
    fingerprint: str

    def to_dict(self) -> dict:
        return {"max_sector_pct": self.max_sector_pct, "max_issuer_pct": self.max_issuer_pct,
                "max_position_pct": self.max_position_pct,
                "max_candidate_pct_of_cash": self.max_candidate_pct_of_cash, "fingerprint": self.fingerprint}


def _fingerprint(*values: float) -> str:
    text = "|".join(f"{v:.6f}" for v in values)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def effective_policy_from_profile(profile) -> EffectivePolicy:
    """Pure: profile is a UserProfile (or anything with the four attributes)."""
    max_sector_pct = float(profile.max_sector_pct)
    max_issuer_pct = float(profile.max_issuer_pct)
    max_position_pct = float(profile.max_post_trade_position_pct) * 100
    max_candidate_pct_of_cash = float(profile.max_per_candidate_pct_of_cash) * 100
    return EffectivePolicy(
        max_sector_pct=max_sector_pct, max_issuer_pct=max_issuer_pct, max_position_pct=max_position_pct,
        max_candidate_pct_of_cash=max_candidate_pct_of_cash,
        fingerprint=_fingerprint(max_sector_pct, max_issuer_pct, max_position_pct, max_candidate_pct_of_cash),
    )


def get_effective_policy(ctx) -> EffectivePolicy:
    """Authorized: reads ctx's own profile via user_profile_db.get_user_profile_for,
    which itself raises NotAuthorized for an identity with no profile row."""
    from portfolio_agent.tools.user_profile_db import get_user_profile_for

    return effective_policy_from_profile(get_user_profile_for(ctx))
