"""
PortfolioAssessment — private, owner-scoped numerical risk and policy
assessment of how one ticker sits against the actor's actual holdings.

Distinct from the other result types:
  MarketForecast (tools.market_context)       shared, ticker-only, no portfolio
  PolicyDecision (tools.policy_decisions_db)  may I trade this right now
  PortfolioAssessment (here)                  how does this fit MY book, and
                                               what does MY policy say

Deterministic Python only — the pandas math already in portfolio_risk.py,
public price data (via tools.price_acquisition, Phase 3), and one effective
portfolio policy (tools.portfolio_policy, sourced from the actor's own
profile). No LLM ever decides an exposure, limit check, or sizing number
here, and this module never invents a risk-tolerance-to-score mapping or any
other stand-in for the old LLM risk_score — see PortfolioExposures'
reason_codes/coverage for what IS computed and why anything isn't.

Two layers, computed at different costs:
  PortfolioExposures  — the portfolio-WIDE state: total value (holdings +
    cash), per-sector/per-issuer totals from STATIC holdings data (shares x
    stored current_price — no network, no return history needed), and
    best-effort cross-holding correlation/beta from portfolio_risk.py (which
    DOES need price history and may be unavailable independently of the
    static part above — see coverage). Computed ONCE per (holdings_version,
    policy_fingerprint) and cached (services.private_cache.AuthorizedCache) —
    a second ticker asked about against the same portfolio state reuses it.
  PortfolioAssessment — one ticker's findings, DERIVED from the shared
    PortfolioExposures plus the effective policy's limits. Concentration
    findings never depend on whether correlation/beta could be computed —
    missing return history must not suppress a concentration check that's
    computable from holdings data alone (see build_portfolio_assessment).

Every missing value is UNKNOWN/INSUFFICIENT_DATA explicitly, never a
"safe" default — a limit that couldn't be evaluated is never reported as
"not breached."
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from portfolio_agent.services.account_service import resolve_portfolio
from portfolio_agent.services.context import RequestContext
from portfolio_agent.tools import holdings_db as repo
from portfolio_agent.tools import portfolio_risk as risk
from portfolio_agent.tools.portfolio_policy import EffectivePolicy, get_effective_policy

ASSESSMENT_VERSION = "portfolio-assessment/2.0"
EXPOSURES_VERSION = "portfolio-exposures/1.0"

UNKNOWN = "UNKNOWN"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
SEVERITY_ADVISORY = "advisory"
SEVERITY_ENFORCEABLE = "enforceable"

DEFAULT_POSITION_PCT = 0.02   # hypothetical add size for a not-yet-held candidate's sizing/correlation math
SUPPORTED_CURRENCY = "USD"    # the only currency price_acquisition/portfolio_risk's math is valid for


# ── Reason codes (coverage gaps and notable portfolio shapes) ────────────────

RC_NO_HOLDINGS = "NO_HOLDINGS"
RC_SINGLE_POSITION = "SINGLE_POSITION_PORTFOLIO"
RC_UNPRICED_HOLDINGS = "UNPRICED_HOLDINGS"
RC_UNKNOWN_SECTOR = "SECTOR_UNKNOWN_FOR_SOME_HOLDINGS"
RC_UNSUPPORTED_CURRENCY = "UNSUPPORTED_CURRENCY"
RC_PRICE_HISTORY_UNAVAILABLE = "PRICE_HISTORY_UNAVAILABLE"


def _holding_value(h) -> float:
    cv = h.get("current_value")
    if cv is not None:
        return float(cv)
    return float(h.get("shares") or 0) * float(h.get("current_price") or 0)


@dataclass(frozen=True)
class PortfolioExposures:
    owner_scope: str
    holdings_version: str
    policy_fingerprint: str
    version: str                          # = f"{holdings_version}:{policy_fingerprint}" — the cache/invalidation key
    currency: str
    currency_supported: bool
    total_value: float                    # holdings + cash
    cash_total: float
    holdings_count: int
    per_holding_value: dict                # {ticker: dollar value}
    per_holding_weight_pct: dict            # {ticker: pct of total_value}
    sector_totals: dict                     # {sector: dollar value}
    sector_pct: dict                        # {sector: pct of total_value}
    issuer_totals: dict                     # {issuer_key: dollar value}
    issuer_pct: dict                        # {issuer_key: pct of total_value}
    issuer_of: dict                         # {ticker: issuer_key}
    sector_of: dict                         # {ticker: sector} — resolved: holding's own field, else universe_db cache
    raw_holdings: tuple                     # the actual holdings rows — needed for a not-yet-held candidate's
                                             # hypothetical-impact call, which weights by shares x LIVE yfinance
                                             # close (not this snapshot's static per_holding_value)
    risk_ctx: dict                          # portfolio_risk.compute_portfolio_risk_context output, {} if unavailable
    price_history_available: bool
    coverage: dict
    reason_codes: tuple
    built_at: str
    exposures_version: str = EXPOSURES_VERSION

    def to_dict(self) -> dict:
        return {
            "owner_scope": self.owner_scope, "holdings_version": self.holdings_version,
            "policy_fingerprint": self.policy_fingerprint, "version": self.version,
            "currency": self.currency, "currency_supported": self.currency_supported,
            "total_value": self.total_value, "cash_total": self.cash_total, "holdings_count": self.holdings_count,
            "sector_pct": self.sector_pct, "issuer_pct": self.issuer_pct,
            "price_history_available": self.price_history_available, "coverage": self.coverage,
            "reason_codes": list(self.reason_codes), "built_at": self.built_at,
            "exposures_version": self.exposures_version,
        }


def _static_sector_issuer_totals(holdings: list) -> tuple[dict, dict, dict, dict, dict, tuple]:
    """Sector/issuer aggregation from STATIC holdings data only (shares x stored
    current_price) — no network, no return history. Returns (per_holding_value,
    sector_totals, issuer_totals, issuer_of, sector_of, reason_codes)."""
    from portfolio_agent.tools.edgar_check import _load_cik_map
    from portfolio_agent.tools.universe_db import get_sectors

    reason_codes: list = []
    per_value: dict = {}
    for h in holdings:
        t = str(h.get("ticker", "")).upper()
        if not t:
            continue
        per_value[t] = per_value.get(t, 0.0) + _holding_value(h)

    sector_of: dict = {str(h["ticker"]).upper(): h.get("sector") for h in holdings if h.get("sector")}
    cached_sectors = get_sectors([t for t in per_value])
    unknown_sector_count = 0
    for t in per_value:
        if t not in sector_of:
            sector_of[t] = cached_sectors.get(t, "Unknown")
        if sector_of[t] == "Unknown":
            unknown_sector_count += 1
    if unknown_sector_count:
        reason_codes.append(RC_UNKNOWN_SECTOR)

    sector_totals: dict = {}
    for t, v in per_value.items():
        s = sector_of.get(t, "Unknown")
        sector_totals[s] = sector_totals.get(s, 0.0) + v

    try:
        cik_map = _load_cik_map()
    except Exception:
        cik_map = {}
    issuer_of = {t: cik_map.get(t, t) for t in per_value}
    issuer_totals: dict = {}
    for t, v in per_value.items():
        k = issuer_of[t]
        issuer_totals[k] = issuer_totals.get(k, 0.0) + v

    return per_value, sector_totals, issuer_totals, issuer_of, sector_of, tuple(reason_codes)


def compute_portfolio_exposures(ctx: RequestContext, *, holdings: list | None = None,
                                cash: list | None = None, policy: EffectivePolicy | None = None) -> PortfolioExposures:
    """
    Uncached. Prefer get_portfolio_exposures() in production — this is the
    pure(ish) compute get_portfolio_exposures() caches; exposed directly for
    tests and for a caller that has already loaded holdings/cash itself.
    """
    portfolio = resolve_portfolio(ctx)
    owner_scope = f"user:{ctx.actor}"
    raw_holdings = list(repo.get_holdings()) if holdings is None else list(holdings)
    raw_cash = list(repo.get_cash_balances()) if cash is None else list(cash)
    policy = policy or get_effective_policy(ctx)
    holdings_version = repo.get_holdings_version() if holdings is None else f"override:{len(raw_holdings)}"

    currency = portfolio.get("base_currency") or SUPPORTED_CURRENCY
    currency_supported = (currency == SUPPORTED_CURRENCY)

    cash_total = sum(float(c.get("amount") or 0) for c in raw_cash)
    per_holding_value, sector_totals, issuer_totals, issuer_of, sector_of, reason_codes = _static_sector_issuer_totals(raw_holdings)
    holdings_value = sum(per_holding_value.values())
    total_value = holdings_value + cash_total

    reason_codes = list(reason_codes)
    if not raw_holdings:
        reason_codes.append(RC_NO_HOLDINGS)
    elif len(raw_holdings) == 1:
        reason_codes.append(RC_SINGLE_POSITION)
    if holdings_value <= 0 and raw_holdings:
        reason_codes.append(RC_UNPRICED_HOLDINGS)
    if not currency_supported:
        reason_codes.append(RC_UNSUPPORTED_CURRENCY)

    per_holding_weight_pct = ({t: round(v / total_value * 100, 2) for t, v in per_holding_value.items()}
                              if total_value > 0 else {})
    sector_pct = {s: round(v / total_value * 100, 2) for s, v in sector_totals.items()} if total_value > 0 else {}
    issuer_pct = {k: round(v / total_value * 100, 2) for k, v in issuer_totals.items()} if total_value > 0 else {}

    risk_ctx: dict = {}
    price_history_available = False
    if currency_supported and len(raw_holdings) >= 2:
        try:
            risk_ctx = risk.compute_portfolio_risk_context(raw_holdings) or {}
        except Exception:
            risk_ctx = {}
        price_history_available = bool(risk_ctx)
        if not price_history_available:
            reason_codes.append(RC_PRICE_HISTORY_UNAVAILABLE)

    coverage = {
        "static_concentration": total_value > 0 and currency_supported,
        "price_history_correlation": price_history_available,
        "cash_included": True,
        "currency_supported": currency_supported,
    }

    policy_fingerprint = policy.fingerprint
    return PortfolioExposures(
        owner_scope=owner_scope, holdings_version=holdings_version, policy_fingerprint=policy_fingerprint,
        version=f"{holdings_version}:{policy_fingerprint}", currency=currency, currency_supported=currency_supported,
        total_value=total_value, cash_total=cash_total, holdings_count=len(raw_holdings),
        per_holding_value=per_holding_value, per_holding_weight_pct=per_holding_weight_pct,
        sector_totals=sector_totals, sector_pct=sector_pct, issuer_totals=issuer_totals, issuer_pct=issuer_pct,
        issuer_of=issuer_of, sector_of=sector_of, raw_holdings=tuple(raw_holdings),
        risk_ctx=risk_ctx, price_history_available=price_history_available,
        coverage=coverage, reason_codes=tuple(dict.fromkeys(reason_codes)),
        built_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def get_portfolio_exposures(ctx: RequestContext, *, cache=None) -> PortfolioExposures:
    """
    Authorized + cached: computed once per (holdings_version, policy_
    fingerprint) — a holdings or policy-limit change produces a different
    cache key and is recomputed; a name/employer/notification-only profile
    edit changes neither, so this is NOT recomputed (see portfolio_policy's
    fingerprint, which deliberately excludes those fields).
    """
    resolve_portfolio(ctx)
    policy = get_effective_policy(ctx)
    holdings_version = repo.get_holdings_version()
    key = f"exposures:{holdings_version}:{policy.fingerprint}"
    cache = cache or _get_default_cache()
    return cache.get_or_compute(ctx, key, lambda: compute_portfolio_exposures(ctx, policy=policy))


_default_exposures_cache = None


def _get_default_cache():
    global _default_exposures_cache
    if _default_exposures_cache is None:
        from portfolio_agent.services.private_cache import AuthorizedCache
        _default_exposures_cache = AuthorizedCache(ttl_seconds=300.0)
    return _default_exposures_cache


@dataclass(frozen=True)
class LimitCheck:
    name: str
    severity: str                 # SEVERITY_ADVISORY | SEVERITY_ENFORCEABLE
    limit_pct: float | None
    actual_pct: object             # float or UNKNOWN
    breached: object                # bool or UNKNOWN
    reason_code: str | None = None

    def to_dict(self) -> dict:
        return {"name": self.name, "severity": self.severity, "limit_pct": self.limit_pct,
                "actual_pct": self.actual_pct, "breached": self.breached, "reason_code": self.reason_code}


@dataclass(frozen=True)
class PortfolioAssessment:
    owner_scope: str
    ticker: str
    exposures: dict
    limits: list
    fit: dict
    sizing: dict
    evidence_refs: dict
    holdings_version: str
    policy_fingerprint: str
    built_at: str
    digest: str
    version: str = ASSESSMENT_VERSION

    def to_dict(self) -> dict:
        return {
            "owner_scope": self.owner_scope, "ticker": self.ticker, "exposures": self.exposures,
            "limits": [c.to_dict() if isinstance(c, LimitCheck) else c for c in self.limits],
            "fit": self.fit, "sizing": self.sizing, "evidence_refs": self.evidence_refs,
            "holdings_version": self.holdings_version, "policy_fingerprint": self.policy_fingerprint,
            "built_at": self.built_at, "digest": self.digest, "version": self.version,
        }


def build_portfolio_assessment(ctx: RequestContext, ticker: str, *,
                               position_pct: float = DEFAULT_POSITION_PCT,
                               exposures: PortfolioExposures | None = None,
                               policy: EffectivePolicy | None = None,
                               holdings: list | None = None, cache=None) -> PortfolioAssessment:
    """
    *ctx* must resolve to a portfolio the caller is authorized to see — checked
    FIRST (via get_portfolio_exposures/compute_portfolio_exposures), before
    any holdings/cash are read. *exposures*/*policy* let a caller supply an
    already-computed shared state (tests, or a batch deriving findings for
    many tickers against one exposures snapshot); *holdings* overrides the
    repository read (tests only).
    """
    ticker = ticker.upper().strip()
    if not ticker:
        raise ValueError("build_portfolio_assessment: ticker is required")

    policy = policy or get_effective_policy(ctx)
    if exposures is None:
        exposures = (compute_portfolio_exposures(ctx, holdings=holdings, policy=policy) if holdings is not None
                    else get_portfolio_exposures(ctx, cache=cache or _get_default_cache()))

    owner_scope = f"user:{ctx.actor}"
    is_held = ticker in exposures.per_holding_value
    reason_codes = list(exposures.reason_codes)

    evidence = {
        "assessment_version": ASSESSMENT_VERSION, "exposures_version": exposures.version,
        "policy_fingerprint": policy.fingerprint, "is_held": is_held,
        "position_pct_used": position_pct, "currency_supported": exposures.currency_supported,
        "coverage": exposures.coverage, "reason_codes": reason_codes,
    }

    # ── Concentration: STATIC data only — never gated on price-history success ──
    if not exposures.currency_supported:
        current_weight_pct = UNKNOWN
        candidate_sector = UNKNOWN
        sector_before_pct = UNKNOWN
        sector_after_pct = UNKNOWN
        issuer_pct = UNKNOWN
    elif exposures.total_value <= 0:
        current_weight_pct = UNKNOWN
        candidate_sector = UNKNOWN
        sector_before_pct = UNKNOWN
        sector_after_pct = UNKNOWN
        issuer_pct = UNKNOWN
    else:
        current_weight_pct = exposures.per_holding_weight_pct.get(ticker, 0.0)
        candidate_sector = _sector_for(ticker, exposures, is_held)
        sector_before_pct = exposures.sector_pct.get(candidate_sector, 0.0)
        if is_held:
            sector_after_pct = sector_before_pct
            issuer_key = exposures.issuer_of.get(ticker, ticker)
            issuer_pct = exposures.issuer_pct.get(issuer_key, 0.0)
        else:
            position_value = exposures.total_value * position_pct
            new_total = exposures.total_value + position_value
            bumped_sector = exposures.sector_totals.get(candidate_sector, 0.0) + position_value
            sector_after_pct = round(bumped_sector / new_total * 100, 2) if new_total else UNKNOWN
            issuer_pct = 0.0   # a brand-new candidate has no pre-existing issuer exposure to report as "current"

    # ── Correlation/beta/risk-adjusted-return: genuinely needs price history ──
    correlation_with_portfolio = UNKNOWN
    most_correlated_peer = UNKNOWN
    beta_vs_spy = UNKNOWN
    fit_score = None
    if is_held and exposures.price_history_available:
        row = exposures.risk_ctx.get(ticker) or {}
        correlation_with_portfolio = row.get("correlation_with_portfolio", UNKNOWN)
        most_correlated_peer = row.get("most_correlated_peer", UNKNOWN)
        beta_vs_spy = row.get("beta_vs_spy", UNKNOWN)
        if correlation_with_portfolio is None:
            correlation_with_portfolio = UNKNOWN
        if beta_vs_spy is None:
            beta_vs_spy = UNKNOWN
    elif not is_held and exposures.currency_supported and exposures.holdings_count >= 1:
        candidate_holdings = list(exposures.raw_holdings)
        try:
            impact = risk.compute_hypothetical_addition_impact(ticker, candidate_holdings, position_pct=position_pct)
        except Exception:
            impact = None
        if impact is not None:
            correlation_with_portfolio = impact.get("correlation_with_portfolio", UNKNOWN)
            beta_vs_spy = impact.get("beta_vs_spy", UNKNOWN)
        try:
            fit_score = risk.compute_portfolio_fit_score(ticker, candidate_holdings, position_pct=position_pct)
        except Exception:
            fit_score = None

    fit = {"fit_score": fit_score, "status": "OK" if fit_score is not None else INSUFFICIENT_DATA}

    # ── Limits: centralized, severity-tagged, each independently UNKNOWN-able ──
    limits = [
        _limit_check("position_concentration", SEVERITY_ENFORCEABLE, policy.max_position_pct,
                    current_weight_pct, RC_UNPRICED_HOLDINGS),
        _limit_check("sector_concentration", SEVERITY_ENFORCEABLE, policy.max_sector_pct,
                    sector_after_pct, RC_UNKNOWN_SECTOR),
        _limit_check("issuer_concentration", SEVERITY_ENFORCEABLE, policy.max_issuer_pct,
                    issuer_pct, RC_UNPRICED_HOLDINGS),
    ]
    cash_pct_of_candidate = UNKNOWN
    if exposures.currency_supported and exposures.cash_total > 0 and not is_held:
        candidate_dollar_size = exposures.total_value * position_pct
        cash_pct_of_candidate = round(candidate_dollar_size / exposures.cash_total * 100, 2)
    limits.append(_limit_check("cash_reserve_for_candidate", SEVERITY_ADVISORY, policy.max_candidate_pct_of_cash,
                               cash_pct_of_candidate, None))

    known_limits = [c for c in limits if c.breached != UNKNOWN]
    enforceable_breached = [c for c in known_limits if c.severity == SEVERITY_ENFORCEABLE and c.breached]
    limits_status = "OK" if len(known_limits) == len(limits) else INSUFFICIENT_DATA

    # ── Sizing: never a permissive fallback on missing evidence ───────────────
    if limits_status != "OK" and any(c.breached == UNKNOWN and c.severity == SEVERITY_ENFORCEABLE for c in limits):
        sizing = {"recommended_position_pct": None, "status": INSUFFICIENT_DATA,
                 "rationale": "one or more enforceable limits could not be evaluated"}
    elif enforceable_breached:
        sizing = {"recommended_position_pct": 0.0, "status": "OK",
                 "rationale": f"enforceable limit(s) already breached: {', '.join(c.name for c in enforceable_breached)}"}
    elif current_weight_pct == UNKNOWN:
        sizing = {"recommended_position_pct": None, "status": INSUFFICIENT_DATA,
                 "rationale": "current position weight is unknown (unpriced holdings or unsupported currency)"}
    else:
        headroom_pct = max(0.0, policy.max_position_pct - current_weight_pct)
        sizing = {"recommended_position_pct": round(min(position_pct * 100, headroom_pct), 2), "status": "OK",
                 "rationale": f"capped at {policy.max_position_pct:.1f}% position limit, {headroom_pct:.2f}% headroom"}

    exposures_dict = {
        "current_weight_pct": current_weight_pct, "sector": candidate_sector if exposures.total_value > 0 else UNKNOWN,
        "sector_concentration_pct": sector_after_pct, "issuer_concentration_pct": issuer_pct,
        "correlation_with_portfolio": correlation_with_portfolio, "most_correlated_peer": most_correlated_peer,
        "beta_vs_spy": beta_vs_spy, "cash_total": exposures.cash_total, "total_value": exposures.total_value,
    }

    text = json.dumps({"exposures": exposures_dict, "limits": [c.to_dict() for c in limits], "fit": fit,
                       "sizing": sizing}, default=str)
    built_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return PortfolioAssessment(
        owner_scope=owner_scope, ticker=ticker, exposures=exposures_dict, limits=limits, fit=fit, sizing=sizing,
        evidence_refs=evidence, holdings_version=exposures.holdings_version, policy_fingerprint=policy.fingerprint,
        built_at=built_at, digest=hashlib.sha256(text.encode()).hexdigest()[:16],
    )


def _sector_for(ticker: str, exposures: PortfolioExposures, is_held: bool) -> str:
    if is_held:
        # The exact sector this ticker's value was already bucketed under in
        # sector_totals/sector_pct — must match, or sector_pct.get(...) below
        # would silently look up the wrong (empty) bucket.
        return exposures.sector_of.get(ticker, "Unknown")
    from portfolio_agent.tools.universe_db import get_sectors
    return get_sectors([ticker]).get(ticker, "Unknown")


def _limit_check(name: str, severity: str, limit_pct: float, actual_pct, reason_code: str | None) -> LimitCheck:
    if not isinstance(actual_pct, (int, float)):
        return LimitCheck(name, severity, limit_pct, UNKNOWN, UNKNOWN, reason_code)
    return LimitCheck(name, severity, limit_pct, actual_pct, actual_pct > limit_pct, None)
