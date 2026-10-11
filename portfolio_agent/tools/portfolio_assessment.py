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

Funds are opened up (tools.lookthrough): issuer and sector exposure count what the funds HOLD, not just the fund
tickers — when enough of each fund's contents is known (the global look-through coverage minimum). Below it the
direct numbers are used and every affected check says so (RC_LOOKTHROUGH_PARTIAL) and can never read "not breached".

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
from portfolio_agent.domain import FUND_INSTRUMENT_TYPES, InstrumentType, tax_treatment
from portfolio_agent.tools import lookthrough as lt
from portfolio_agent.tools.lookthrough import RC_LOOKTHROUGH_PARTIAL
from portfolio_agent.tools.portfolio_policy import EffectivePolicy, get_effective_policy

ASSESSMENT_VERSION = "portfolio-assessment/3.0"
EXPOSURES_VERSION = "portfolio-exposures/2.0"

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
RC_INSTRUMENT_TYPE_UNKNOWN = "INSTRUMENT_TYPE_UNKNOWN"

_FUND_LIKE = {t.value for t in FUND_INSTRUMENT_TYPES} | {InstrumentType.CIT.value, InstrumentType.STABLE_VALUE.value}


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
    # ── fund look-through (all empty / False for a portfolio with no funds) ──
    fund_data_version: str = ""
    lookthrough_applicable: bool = False            # the portfolio holds at least one fund
    lookthrough_used: bool = False                  # …and enough of it is known (coverage >= the minimum)
    lookthrough_coverage_pct: float | None = None
    lookthrough_min_coverage_pct: float | None = None
    lookthrough_issuer_pct: dict = None             # {issuer_key: % of total} direct + via funds
    lookthrough_issuer_via_pct: dict = None         # {issuer_key: % of total} that part reached through funds
    lookthrough_sector_totals: dict = None          # {sector: dollars} direct non-fund + via funds (+ "Unknown")
    lookthrough_sector_pct: dict = None
    lookthrough_sector_via_pct: dict = None
    lookthrough_reason_codes: tuple = ()
    leveraged_inverse_pct: float = 0.0              # % of total value in leveraged / inverse funds
    lookthrough: object = None                      # the full tools.lookthrough.LookThrough, for health / reports

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
            "lookthrough_applicable": self.lookthrough_applicable, "lookthrough_used": self.lookthrough_used,
            "lookthrough_coverage_pct": self.lookthrough_coverage_pct,
            "lookthrough_issuer_pct": self.lookthrough_issuer_pct, "lookthrough_sector_pct": self.lookthrough_sector_pct,
            "leveraged_inverse_pct": self.leveraged_inverse_pct, "fund_data_version": self.fund_data_version,
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


def _tax_bucket(account_type) -> str:
    """TAXABLE / TAX_DEFERRED / TAX_FREE, or UNKNOWN for an unset or unrecognised account type (never assumed taxable)."""
    t = tax_treatment(account_type)
    return t.value if t is not None else "UNKNOWN"


def _positions_for_lookthrough(holdings: list) -> list[dict]:
    out = []
    for h in holdings:
        out.append({
            "account_id": h.get("account_id"), "tax_treatment": _tax_bucket(h.get("account_type")),
            "ticker": str(h.get("ticker", "")).upper(), "name": h.get("description"), "market_value": _holding_value(h),
            "instrument_type": h.get("instrument_type") or "UNKNOWN", "leverage_factor": h.get("leverage_factor") or 1.0,
            "asset_class": h.get("asset_class"), "is_paper": False,     # the holdings view already excludes paper accounts
        })
    return out


def _lookthrough_settings() -> tuple[float, int]:
    from portfolio_agent.tools import investor_settings_db as sdb
    s = sdb.get_portfolio_settings()
    return float(s["lookthrough_min_coverage_pct"]), int(s["lookthrough_max_depth"])


def fund_data_fingerprint(owner: str, holdings: list) -> str:
    """Everything the look-through of these holdings depends on besides the holdings themselves: when each fund's
    data was fetched, the owner's manual breakdowns, and the two look-through settings. Part of the cache key."""
    tickers = sorted({str(h.get("ticker", "")).upper() for h in holdings
                      if (h.get("instrument_type") or "") in _FUND_LIKE})
    if not tickers:
        return "none"
    from portfolio_agent.services import fund_data_service as fds
    min_cov, depth = _lookthrough_settings()
    return f"{fds.views_version(owner, tickers)}|{min_cov}|{depth}"


def _issuer_key_map() -> dict:
    try:
        from portfolio_agent.tools.edgar_check import _load_cik_map
        return _load_cik_map()
    except Exception:
        return {}


def _apply_lookthrough(owner: str, raw_holdings: list, per_holding_value: dict, sector_of: dict, total_value: float) -> dict:
    """The look-through fields of PortfolioExposures ({} defaults when the portfolio has no funds)."""
    fund_tickers = sorted({str(h.get("ticker", "")).upper() for h in raw_holdings
                           if (h.get("instrument_type") or "") in _FUND_LIKE and _holding_value(h) > 0})
    if not fund_tickers or total_value <= 0:
        return {}
    from portfolio_agent.services import fund_data_service as fds
    min_cov, depth = _lookthrough_settings()
    views = fds.load_fund_views(owner, fund_tickers, max_depth=depth)
    result = lt.analyze(_positions_for_lookthrough(raw_holdings), views, max_depth=depth)
    hh = result.household
    cik = _issuer_key_map()
    fund_set = set(fund_tickers) | set(views)
    issuer_total: dict = {}
    issuer_via: dict = {}
    for sym, item in hh.named_issuers().items():
        k = cik.get(sym, sym)
        issuer_total[k] = issuer_total.get(k, 0.0) + item["value"]
        via = sum(v for f, v in item["via"].items() if f in fund_set and f != sym)
        issuer_via[k] = issuer_via.get(k, 0.0) + via
    sector_total: dict = {}
    sector_via: dict = {}
    for t, v in per_holding_value.items():
        if t in fund_set:
            continue                                                       # a fund's sector comes from what it holds
        s = sector_of.get(t, "Unknown")
        sector_total[s] = sector_total.get(s, 0.0) + v
    for s, v in hh.sector_value.items():
        sector_total[s] = sector_total.get(s, 0.0) + v
        sector_via[s] = sector_via.get(s, 0.0) + v
    if hh.sector_unknown_value > 0:
        sector_total["Unknown"] = sector_total.get("Unknown", 0.0) + hh.sector_unknown_value
    pct = lambda d: {k: round(v / total_value * 100, 2) for k, v in d.items()}
    used = hh.coverage_pct >= min_cov
    codes = list(hh.reason_codes)
    if not used:
        codes.insert(0, RC_LOOKTHROUGH_PARTIAL)
    return {
        "lookthrough_applicable": True, "lookthrough_used": used, "lookthrough_coverage_pct": hh.coverage_pct,
        "lookthrough_min_coverage_pct": min_cov, "lookthrough_issuer_pct": pct(issuer_total),
        "lookthrough_issuer_via_pct": pct(issuer_via), "lookthrough_sector_totals": sector_total,
        "lookthrough_sector_pct": pct(sector_total), "lookthrough_sector_via_pct": pct(sector_via),
        "lookthrough_reason_codes": tuple(dict.fromkeys(codes)), "leveraged_inverse_pct": hh.leveraged_inverse_pct(),
        "lookthrough": result,
    }


def compute_portfolio_exposures(ctx: RequestContext, *, holdings: list | None = None,
                                cash: list | None = None, policy: EffectivePolicy | None = None) -> PortfolioExposures:
    """
    Uncached. Prefer get_portfolio_exposures() in production — this is the
    pure(ish) compute get_portfolio_exposures() caches; exposed directly for
    tests and for a caller that has already loaded holdings/cash itself.
    """
    portfolio = resolve_portfolio(ctx)
    return compute_exposures_for_owner(ctx.actor, portfolio, holdings=holdings, cash=cash, policy=policy)


def compute_exposures_for_owner(owner: str, portfolio: dict, *, holdings: list | None = None, cash: list | None = None,
                                policy: EffectivePolicy | None = None) -> PortfolioExposures:
    """
    compute_portfolio_exposures() for an owner the caller has already authorized (*portfolio* is that owner's own, e.g.
    from resolve_portfolio(ctx) or holdings_db.get_portfolio_by_owner(owner)) — the entry point for server-side callers
    that have an owner string but no RequestContext (the Portfolio Health tool for chat).
    """
    from portfolio_agent.tools.portfolio_policy import get_effective_policy_for_owner
    owner_scope = f"user:{owner}"
    raw_holdings = list(repo.get_holdings(portfolio_id=portfolio["portfolio_id"])) if holdings is None else list(holdings)
    raw_cash = list(repo.get_cash_balances(portfolio_id=portfolio["portfolio_id"])) if cash is None else list(cash)
    policy = policy or get_effective_policy_for_owner(owner)
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

    lt_fields = _apply_lookthrough(owner, raw_holdings, per_holding_value, sector_of, total_value) if currency_supported else {}
    reason_codes.extend(lt_fields.get("lookthrough_reason_codes", ()))
    fund_version = fund_data_fingerprint(owner, raw_holdings)

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
        version=f"{holdings_version}:{policy_fingerprint}:{fund_version}", currency=currency, currency_supported=currency_supported,
        total_value=total_value, cash_total=cash_total, holdings_count=len(raw_holdings),
        per_holding_value=per_holding_value, per_holding_weight_pct=per_holding_weight_pct,
        sector_totals=sector_totals, sector_pct=sector_pct, issuer_totals=issuer_totals, issuer_pct=issuer_pct,
        issuer_of=issuer_of, sector_of=sector_of, raw_holdings=tuple(raw_holdings),
        risk_ctx=risk_ctx, price_history_available=price_history_available,
        coverage=coverage, reason_codes=tuple(dict.fromkeys(reason_codes)),
        built_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), fund_data_version=fund_version, **lt_fields,
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
    portfolio = resolve_portfolio(ctx)
    fund_version = fund_data_fingerprint(ctx.actor, list(repo.get_holdings(portfolio_id=portfolio["portfolio_id"])))
    key = f"exposures:{holdings_version}:{policy.fingerprint}:{fund_version}"
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
    detail: str | None = None      # plain-words basis, e.g. how a look-through figure splits into direct + via funds

    def to_dict(self) -> dict:
        d = {"name": self.name, "severity": self.severity, "limit_pct": self.limit_pct,
             "actual_pct": self.actual_pct, "breached": self.breached, "reason_code": self.reason_code}
        if self.detail is not None:
            d["detail"] = self.detail
        return d


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
    profile_row = _candidate_profile(ticker)
    candidate_type = (profile_row or {}).get("instrument_type")
    candidate_leverage = float((profile_row or {}).get("leverage_factor") or 1.0)

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
        use_lt = exposures.lookthrough_used
        sec_pct = exposures.lookthrough_sector_pct if use_lt else exposures.sector_pct
        sec_totals = exposures.lookthrough_sector_totals if use_lt else exposures.sector_totals
        sector_before_pct = sec_pct.get(candidate_sector, 0.0)
        issuer_key = issuer_key_for(ticker, exposures)
        # a fund is not an issuer: its own concentration is the position check; a stock / candidate stock counts
        # what it is directly AND what the funds hold of it
        candidate_is_fund = (candidate_type or "") in _FUND_LIKE
        if use_lt and not candidate_is_fund:
            issuer_pct = exposures.lookthrough_issuer_pct.get(issuer_key, 0.0)
        elif is_held:
            issuer_pct = exposures.issuer_pct.get(issuer_key, 0.0)
        else:
            issuer_pct = 0.0   # a brand-new candidate has no pre-existing issuer exposure to report as "current"
        if is_held:
            sector_after_pct = sector_before_pct
        else:
            position_value = exposures.total_value * position_pct
            new_total = exposures.total_value + position_value
            bumped_sector = sec_totals.get(candidate_sector, 0.0) + position_value
            sector_after_pct = round(bumped_sector / new_total * 100, 2) if new_total else UNKNOWN

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
    sector_detail, issuer_detail, partial = _lookthrough_details(
        exposures, candidate_sector if exposures.total_value > 0 else None, issuer_key_for(ticker, exposures),
        candidate_is_fund=(candidate_type or "") in _FUND_LIKE)
    limits = [
        _limit_check("position_concentration", SEVERITY_ENFORCEABLE, policy.max_position_pct,
                    current_weight_pct, RC_UNPRICED_HOLDINGS),
        _limit_check("sector_concentration", SEVERITY_ENFORCEABLE, policy.max_sector_pct,
                    sector_after_pct, RC_UNKNOWN_SECTOR, detail=sector_detail, partial=partial),
        _limit_check("issuer_concentration", SEVERITY_ENFORCEABLE, policy.max_issuer_pct,
                    issuer_pct, RC_UNPRICED_HOLDINGS, detail=issuer_detail, partial=partial),
    ]
    cash_pct_of_candidate = UNKNOWN
    if exposures.currency_supported and exposures.cash_total > 0 and not is_held:
        candidate_dollar_size = exposures.total_value * position_pct
        cash_pct_of_candidate = round(candidate_dollar_size / exposures.cash_total * 100, 2)
    limits.append(_limit_check("cash_reserve_for_candidate", SEVERITY_ADVISORY, policy.max_candidate_pct_of_cash,
                               cash_pct_of_candidate, None))

    # advisory: leveraged / inverse share, and whether the candidate's kind of security is one the profile allows
    lev_actual = UNKNOWN
    if exposures.currency_supported and exposures.total_value > 0:
        lev_actual = exposures.leveraged_inverse_pct
        if not is_held and candidate_leverage != 1.0:
            lev_actual = round(lev_actual + position_pct * 100, 2)
    limits.append(_limit_check("leveraged_inverse_exposure", SEVERITY_ADVISORY, policy.max_leveraged_inverse_pct,
                               lev_actual, RC_UNPRICED_HOLDINGS))
    limits.append(_instrument_type_check(candidate_type, policy))

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

    if exposures.lookthrough_applicable:                      # only a portfolio with funds carries these keys
        exposures_dict.update({
            "lookthrough_used": exposures.lookthrough_used, "lookthrough_coverage_pct": exposures.lookthrough_coverage_pct,
            "leveraged_inverse_pct": exposures.leveraged_inverse_pct})

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


def _limit_check(name: str, severity: str, limit_pct: float, actual_pct, reason_code: str | None, *,
                 detail: str | None = None, partial: bool = False) -> LimitCheck:
    if not isinstance(actual_pct, (int, float)):
        return LimitCheck(name, severity, limit_pct, UNKNOWN, UNKNOWN, reason_code, detail)
    if partial:
        # Fund contents are only partly known, so this is the DIRECT number: over the limit is still over, but under
        # it can never be called "not breached" — the funds may hold more.
        return LimitCheck(name, severity, limit_pct, actual_pct, True if actual_pct > limit_pct else UNKNOWN,
                          RC_LOOKTHROUGH_PARTIAL, detail)
    return LimitCheck(name, severity, limit_pct, actual_pct, actual_pct > limit_pct, None, detail)


def _candidate_profile(ticker: str) -> dict | None:
    try:
        return repo.get_instrument_profiles([ticker]).get(ticker)
    except Exception:
        return None


def issuer_key_for(ticker: str, exposures: PortfolioExposures) -> str:
    if ticker in exposures.issuer_of:
        return exposures.issuer_of[ticker]
    return _issuer_key_map().get(ticker, ticker) if exposures.lookthrough_applicable else ticker


def _instrument_type_check(candidate_type: str | None, policy: EffectivePolicy) -> LimitCheck:
    name = "instrument_type_allowed"
    if not candidate_type or candidate_type == InstrumentType.UNKNOWN.value:
        return LimitCheck(name, SEVERITY_ADVISORY, None, UNKNOWN, UNKNOWN, RC_INSTRUMENT_TYPE_UNKNOWN,
                          "the kind of security is not known yet")
    allowed = candidate_type in policy.allowed_instrument_types
    return LimitCheck(name, SEVERITY_ADVISORY, None, candidate_type, not allowed, None,
                      f"{candidate_type} is {'allowed' if allowed else 'not allowed'} by the {policy.profile} profile")


def _lookthrough_details(exposures: PortfolioExposures, sector: str | None, issuer_key: str, *,
                         candidate_is_fund: bool) -> tuple[str | None, str | None, bool]:
    """(sector text, issuer text, partial?) — the plain-words split of a look-through figure, or the reason the
    direct figure is all there is. (None, None, False) for a portfolio with no funds: nothing to explain."""
    if not exposures.lookthrough_applicable:
        return None, None, False
    cov, need = exposures.lookthrough_coverage_pct, exposures.lookthrough_min_coverage_pct
    if not exposures.lookthrough_used:
        msg = (f"direct holdings only: fund contents are {cov:.0f}% known (minimum {need:.0f}% to look through), "
               f"so the funds may hold more")
        return msg, msg, True
    def split(total_map, via_map, key) -> str:
        total, via = total_map.get(key, 0.0), via_map.get(key, 0.0)
        return f"{total:.2f}% = {max(0.0, total - via):.2f}% direct + {via:.2f}% via funds (look-through, {cov:.0f}% known)"
    sector_text = split(exposures.lookthrough_sector_pct, exposures.lookthrough_sector_via_pct, sector) if sector else None
    issuer_text = None if candidate_is_fund else split(exposures.lookthrough_issuer_pct, exposures.lookthrough_issuer_via_pct, issuer_key)
    return sector_text, issuer_text, False
