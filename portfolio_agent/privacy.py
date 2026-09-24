"""
Private-data boundary for shared (market-only) artifacts.

Shared forecasts, the market context that feeds them, and their history may
not carry anything that identifies a person or their accounts. This module
knows what those private terms are and checks text against them.

Detection is deliberately simple and strict: a private term is a whole phrase
(display names, employer, account nicknames, account numbers) matched
case-insensitively as a substring. Errors never echo the private term.
"""

from __future__ import annotations

import re

MIN_TERM_LEN = 5


class PrivateDataLeak(ValueError):
    """Raised when text destined for a shared artifact contains private data."""


def _clean(value) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s if len(s) >= MIN_TERM_LEN else None


def collect_private_terms() -> frozenset[str]:
    """
    Private phrases known to this deployment: profile name/employer, account
    nicknames and numbers. An account nickname equal to its broker's name
    (e.g. "Vanguard") is not private and is skipped.
    """
    terms: set[str] = set()
    try:
        from portfolio_agent.tools.user_profile_db import get_user_profile
        p = get_user_profile()
        for v in (p.display_name, p.employer):
            t = _clean(v)
            if t:
                terms.add(t)
    except Exception:
        pass
    try:
        from portfolio_agent.tools import holdings_db as repo
        for a in repo.list_accounts(include_archived=True):
            broker = (a.get("broker") or "").lower()
            for v in (a.get("display_name"), a.get("account_number")):
                t = _clean(v)
                if t and t.lower() != broker:
                    terms.add(t)
    except Exception:
        pass
    return frozenset(terms)


def find_private_terms(text: str, terms: frozenset[str] | set[str]) -> list[str]:
    low = (text or "").lower()
    return sorted(t for t in terms if t.lower() in low)


def assert_market_only(text: str, terms: frozenset[str] | set[str] | None = None, *, what: str = "shared content") -> None:
    """Raise PrivateDataLeak if *text* contains any private term. The message never includes the term."""
    if terms is None:
        terms = collect_private_terms()
    found = find_private_terms(text, terms)
    if found:
        raise PrivateDataLeak(f"{what} contains {len(found)} private term(s); refusing to treat it as market-only")


def redact(text: str, terms: frozenset[str] | set[str]) -> str:
    out = text or ""
    for t in sorted(terms, key=len, reverse=True):
        out = re.sub(re.escape(t), "[private]", out, flags=re.IGNORECASE)
    return out
