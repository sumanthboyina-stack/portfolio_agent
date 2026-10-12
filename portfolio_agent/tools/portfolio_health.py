"""
PortfolioHealth — a private, owner-scoped checkup of one person's portfolio against their own policy.

Not a score. Each check gets one of four statuses and the page shows how many are ACTION / WATCH / UNKNOWN:
    GOOD     inside the limit or band
    WATCH    close to a limit or outside a band
    ACTION   over a limit, or far outside a band — something to decide
    UNKNOWN  the data needed to judge it is missing or too partial; never shown as GOOD
Every status comes from a number compared with the person's effective policy (tools.portfolio_policy) or a global health
threshold (portfolio_settings, admin-editable, versioned) — no model decides any number, label or status. Missing data is
an explicit UNKNOWN with a reason code, never a "safe" default. Not investment advice.

Scopes: "household" (all non-paper accounts), "account:<id>", "tax:<TAXABLE|TAX_DEFERRED|TAX_FREE|UNKNOWN>". The
household gets every check. An account or tax bucket gets the ones that make sense on a slice (concentration,
diversification, leveraged/inverse, fees, data quality) and judged against the same limits on that slice's own value; the
allocation targets, emergency reserve, profile and compliance checks belong to the whole household only.

Inputs are gathered once (gather_inputs) and judged by pure functions (evaluate), so every rule is testable without a
database. Funds are opened up with tools.lookthrough; cash balances are counted as cash. Position concentration looks at
single securities — a fund's own weight is judged through what it holds (issuer / sector look-through), and leveraged
or inverse funds have their own check.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from portfolio_agent.domain import (
    ALLOCATION_ASSET_CLASSES, FUND_INSTRUMENT_TYPES, HEALTH_STATUSES, HealthStatus, InstrumentType, tax_treatment,
)
from portfolio_agent.tools import holdings_db as repo
from portfolio_agent.tools import lookthrough as lt
from portfolio_agent.tools.lookthrough import RC_LOOKTHROUGH_PARTIAL, ScopeResult

HEALTH_VERSION = "portfolio-health/1.0"

G, W, A, U = (HealthStatus.GOOD.value, HealthStatus.WATCH.value, HealthStatus.ACTION.value, HealthStatus.UNKNOWN.value)
_SEVERITY = {G: 0, U: 1, W: 2, A: 3}               # UNKNOWN outranks GOOD: a check with a gap is never GOOD

# Reason codes (the ones owned here; look-through and price ones come from their own modules)
RC_NO_HOLDINGS = "NO_HOLDINGS"
RC_NO_CASH_DATA = "NO_CASH_DATA"
RC_ACCOUNT_TYPE_UNKNOWN = "ACCOUNT_TYPE_UNKNOWN"
RC_PROFILE_DEFAULT = "PROFILE_DEFAULT"
RC_ABOVE_CAP = "ABOVE_QUIZ_CAP_ACKNOWLEDGED"
RC_ALLOCATION_UNKNOWN_SHARE = "ALLOCATION_UNKNOWN_SHARE"
RC_EQUITY_GEOGRAPHY_UNKNOWN = "EQUITY_GEOGRAPHY_UNKNOWN"
RC_SECTOR_UNKNOWN = "SECTOR_UNKNOWN_SHARE"
RC_CORRELATION_UNAVAILABLE = "CORRELATION_UNAVAILABLE"
RC_EXPENSE_RATIO_UNKNOWN = "EXPENSE_RATIO_UNKNOWN"
RC_TYPE_UNKNOWN = "INSTRUMENT_TYPE_UNKNOWN"
RC_UNPRICED = "UNPRICED_HOLDINGS"
RC_PRICE_STALE = "PRICE_STALE"
RC_PRICE_DATE_UNKNOWN = "PRICE_DATE_UNKNOWN"
RC_FUND_DATA_STALE = "FUND_DATA_STALE"
RC_FUND_DATA_MISSING = "FUND_DATA_MISSING"
RC_NO_BREAKDOWN = "CIT_NO_BREAKDOWN"
RC_RESTRICTED_UNREADABLE = "RESTRICTED_LIST_UNREADABLE"
RC_TRADES_WITHOUT_CLEARANCE = "TRADES_WITHOUT_CLEARANCE"     # ledger trades with clearance_status 'missing'
RC_CLEARANCE_UNKNOWN = "TRADE_CLEARANCE_UNKNOWN"             # ledger trades whose approvals could not be read

_FUND_TYPES = {t.value for t in FUND_INSTRUMENT_TYPES}
_NO_DATA_FUND_TYPES = {InstrumentType.CIT.value, InstrumentType.STABLE_VALUE.value}
_CASH_TYPES = {InstrumentType.CASH.value, InstrumentType.MONEY_MARKET.value}
_NON_DECISION_CLASSES = ("CASH", "FIXED_INCOME", "INTL_EQUITY")
_BAND_FLOOR_PP = 1.0          # a band this narrow (target = edge) is treated as at least a point wide

_CLASS_LABEL = {"US_EQUITY": "US stocks", "INTL_EQUITY": "International stocks", "FIXED_INCOME": "Bonds",
                "REAL_ESTATE": "Real estate", "COMMODITY": "Commodities", "CASH": "Cash"}


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class HealthCheck:
    key: str
    title: str
    status: str
    summary: str
    metrics: dict = field(default_factory=dict)
    reason_codes: tuple = ()
    notes: tuple = ()

    def to_dict(self) -> dict:
        return {"key": self.key, "title": self.title, "status": self.status, "summary": self.summary,
                "metrics": self.metrics, "reason_codes": list(self.reason_codes), "notes": list(self.notes)}


@dataclass(frozen=True)
class ScopeHealth:
    scope: str
    label: str
    total_value: float
    checks: tuple
    mix: dict                      # asset-class mix, % of the scope's value (numbers only: targets are household-level)
    counts: dict                   # {ACTION: n, WATCH: n, UNKNOWN: n, GOOD: n}

    def check(self, key: str) -> HealthCheck | None:
        return next((c for c in self.checks if c.key == key), None)

    def to_dict(self) -> dict:
        return {"scope": self.scope, "label": self.label, "total_value": self.total_value, "mix": self.mix,
                "counts": self.counts, "checks": [c.to_dict() for c in self.checks]}


@dataclass(frozen=True)
class PortfolioHealth:
    owner_scope: str
    as_of: str
    built_at: str
    version: str                   # cache key: holdings, policy, fund data, thresholds, day
    household: ScopeHealth
    by_account: dict               # {account_id: ScopeHealth}
    by_tax_treatment: dict         # {bucket: ScopeHealth}
    profile: dict                  # {profile, source, reserve_amount, reserve_source}
    reason_codes: tuple
    health_version: str = HEALTH_VERSION

    def scopes(self) -> list[ScopeHealth]:
        return [self.household, *self.by_account.values(), *self.by_tax_treatment.values()]

    def to_dict(self) -> dict:
        return {"owner_scope": self.owner_scope, "as_of": self.as_of, "built_at": self.built_at, "version": self.version,
                "health_version": self.health_version, "profile": self.profile, "reason_codes": list(self.reason_codes),
                "household": self.household.to_dict(),
                "accounts": {k: v.to_dict() for k, v in self.by_account.items()},
                "tax_treatments": {k: v.to_dict() for k, v in self.by_tax_treatment.items()}}


@dataclass
class HealthInputs:
    """Everything evaluate() needs, already read (so evaluate itself does no I/O)."""
    owner: str
    policy: object                         # tools.portfolio_policy.EffectivePolicy
    settings: dict                         # portfolio_settings row (thresholds + TTLs + fee / coverage settings)
    holdings: list                         # Holding rows (non-paper, active accounts)
    cash: list                             # account_cash rows
    accounts: dict                         # {account_id: account row}
    exposures: object                      # tools.portfolio_assessment.PortfolioExposures
    views: dict                            # {TICKER: lookthrough.FundView}
    restricted: list | None                # [{ticker, reason}] or None when it could not be read
    share_class_peers: dict = field(default_factory=dict)   # {share_class_group: [{ticker, expense_ratio}]}
    fund_fetched_at: dict = field(default_factory=dict)     # {TICKER: iso fetched_at} of stored fund data
    now: datetime | None = None
    issuer_key: dict = field(default_factory=dict)          # {symbol: merged issuer key} (e.g. GOOG / GOOGL share a CIK)
    clearance: dict | None = None                           # ledger_db.clearance_counts: {missing, unknown, approved, not_required, missing_tickers}


# ── Small pure helpers ────────────────────────────────────────────────────────

def worst(statuses) -> str:
    """The most severe status: ACTION > WATCH > UNKNOWN > GOOD. An empty set is UNKNOWN, never GOOD."""
    statuses = list(statuses)
    return max(statuses, key=_SEVERITY.__getitem__) if statuses else U


def _counts(checks) -> dict:
    out = {s: 0 for s in (A, W, U, G)}
    for c in checks:
        out[c.status] += 1
    return out


def _pct(value: float, total: float) -> float:
    return round(100.0 * value / total, 2) if total > 0 else 0.0


def _value(h) -> float:
    cv = h.get("current_value")
    if cv is not None:
        return float(cv)
    return float(h.get("shares") or 0) * float(h.get("current_price") or 0)


def _is_fund_like(itype) -> bool:
    return str(itype or "") in _FUND_TYPES | _NO_DATA_FUND_TYPES


def _limit_status(actual: float, limit: float, watch_fraction: float) -> str:
    """ACTION above the limit, WATCH from watch_fraction of it, else GOOD."""
    if actual > limit:
        return A
    if limit > 0 and actual >= watch_fraction * limit and actual > 0:
        return W
    return G


def _band_status(actual: float, band: dict, multiple: float) -> str:
    """GOOD inside [min, max]; WATCH outside; ACTION when beyond `multiple` band-widths from the target."""
    target, lo, hi = float(band["target"]), float(band["min"]), float(band["max"])
    if lo <= actual <= hi:
        return G
    if actual < lo:
        return A if actual < target - multiple * max(target - lo, _BAND_FLOOR_PP) else W
    return A if actual > target + multiple * max(hi - target, _BAND_FLOOR_PP) else W


def _money(v: float) -> str:
    return f"${v:,.0f}" if abs(v) >= 100 else f"${v:,.2f}"


def _check(key, title, status, summary, metrics=None, codes=(), notes=()) -> HealthCheck:
    return HealthCheck(key, title, status, summary, metrics or {}, tuple(dict.fromkeys(codes)), tuple(notes))


def _empty(key, title) -> HealthCheck:
    return _check(key, title, U, "No holdings or cash on record yet, so there is nothing to judge.", codes=[RC_NO_HOLDINGS])


# ── Positions fed to the look-through engine (holdings + cash) ────────────────

def _bucket(account_type) -> str:
    t = tax_treatment(account_type)
    return t.value if t is not None else "UNKNOWN"


def _positions(inp: HealthInputs) -> list[dict]:
    out = []
    for h in inp.holdings:
        out.append({"account_id": h.get("account_id"), "tax_treatment": _bucket(h.get("account_type")),
                    "ticker": str(h.get("ticker", "")).upper(), "name": h.get("description"), "market_value": _value(h),
                    "instrument_type": h.get("instrument_type") or "UNKNOWN", "leverage_factor": h.get("leverage_factor") or 1.0,
                    "asset_class": h.get("asset_class"), "is_paper": False})
    for c in inp.cash:
        acct = inp.accounts.get(c.get("account_id")) or {}
        amount = float(c.get("amount") or 0)
        if amount > 0:
            out.append({"account_id": c.get("account_id"), "tax_treatment": _bucket(acct.get("account_type")), "ticker": "CASH",
                        "name": "Cash", "market_value": amount, "instrument_type": "CASH", "leverage_factor": 1.0,
                        "asset_class": "CASH", "is_paper": False})
    return out


# ── Per-scope numbers ─────────────────────────────────────────────────────────

@dataclass
class _Scope:
    key: str
    label: str
    res: ScopeResult
    positions: list
    issuer_pct: dict               # named issuer (its largest symbol) -> % of the scope's value
    sector_pct: dict               # sector -> % (includes "Unknown")
    mix_pct: dict                  # US_EQUITY / INTL_EQUITY / FIXED_INCOME / CASH / REAL_ESTATE / COMMODITY / UNCLASSIFIED
    equity_geography_unknown_pct: float
    lookthrough_used: bool
    has_funds: bool


def _build_scope(key: str, label: str, res: ScopeResult, positions: list, inp: HealthInputs) -> _Scope:
    total = res.total_value
    min_cov = float(inp.settings["lookthrough_min_coverage_pct"])
    fund_set = {p["ticker"] for p in positions if _is_fund_like(p["instrument_type"])} | set(inp.views)
    ikey = inp.issuer_key
    issuer: dict = {}
    label_of: dict = {}
    for sym, item in sorted(res.named_issuers().items(), key=lambda kv: -kv[1]["value"]):
        k = ikey.get(sym, sym)
        issuer[k] = issuer.get(k, 0.0) + item["value"]
        label_of.setdefault(k, sym)                                  # the biggest contributing symbol names the issuer
    sector_of = inp.exposures.sector_of or {}
    sector: dict = {}
    for p in positions:
        if p["instrument_type"] in _CASH_TYPES or _is_fund_like(p["instrument_type"]) or p["market_value"] <= 0:
            continue
        s = sector_of.get(p["ticker"]) or "Unknown"
        sector[s] = sector.get(s, 0.0) + p["market_value"]
    for s, v in res.sector_value.items():
        sector[s] = sector.get(s, 0.0) + v
    if res.sector_unknown_value > 0:
        sector["Unknown"] = sector.get("Unknown", 0.0) + res.sector_unknown_value
    acv = res.asset_class_value
    intl = res.intl_equity_value
    geo_unknown = res.intl_unknown_value
    us = max(0.0, acv.get("stock", 0.0) - intl - geo_unknown)
    unclassified = acv.get("other", 0.0) + res.asset_class_unknown_value
    mix = {"US_EQUITY": us, "INTL_EQUITY": intl, "FIXED_INCOME": acv.get("bond", 0.0), "CASH": acv.get("cash", 0.0),
           "REAL_ESTATE": acv.get("real_estate", 0.0), "COMMODITY": acv.get("commodity", 0.0), "UNCLASSIFIED": unclassified,
           "EQUITY_GEOGRAPHY_UNKNOWN": geo_unknown}
    return _Scope(
        key=key, label=label, res=res, positions=positions,
        issuer_pct={label_of[k]: _pct(v, total) for k, v in issuer.items()}, sector_pct={k: _pct(v, total) for k, v in sector.items()},
        mix_pct={k: _pct(v, total) for k, v in mix.items()},
        equity_geography_unknown_pct=_pct(geo_unknown, total),
        lookthrough_used=bool(res.fund_value <= 0 or res.coverage_pct >= min_cov), has_funds=res.fund_value > 0)


# ── The checks ────────────────────────────────────────────────────────────────

def _allocation_checks(sc: _Scope, inp: HealthInputs) -> list[HealthCheck]:
    """Household only: the mix against the policy's target bands, split into the three checks that partition the classes."""
    pol, st = inp.policy, inp.settings
    targets = pol.target_allocation or {}
    mult = float(st["health_action_band_multiple"])
    tol = float(st["health_unknown_share_tolerance_pct"])
    unclassified = sc.mix_pct["UNCLASSIFIED"]
    classes = [c for c in dict.fromkeys([*ALLOCATION_ASSET_CLASSES, *targets]) if c in sc.mix_pct or c in targets]
    zero = {"target": 0.0, "min": 0.0, "max": 0.0}

    def judge(cls: str) -> tuple[str, str, list]:
        actual = sc.mix_pct.get(cls, 0.0)
        band = targets.get(cls, zero)
        status = _band_status(actual, band, mult)
        codes = []
        # A share that is partly unclassified is only a LOWER bound. Over the band is still over; but "inside" or "under"
        # cannot be trusted, so those become UNKNOWN rather than GOOD or a false alarm.
        under_or_in = actual <= float(band["max"])
        if cls in ("US_EQUITY", "INTL_EQUITY") and sc.equity_geography_unknown_pct > tol and under_or_in:
            status, codes = U, [RC_EQUITY_GEOGRAPHY_UNKNOWN]
        elif unclassified > tol and under_or_in:
            status, codes = U, [RC_ALLOCATION_UNKNOWN_SHARE]
        text = (f"{_CLASS_LABEL.get(cls, cls)} {actual:.1f}% vs target {band['target']:.0f}% "
                f"(band {band['min']:.0f}–{band['max']:.0f}%)")
        return status, text, codes

    def group(key, title, members, only_above=False):
        rows = []
        for cls in members:
            if cls not in targets and sc.mix_pct.get(cls, 0.0) <= 0 and cls not in ("US_EQUITY", "INTL_EQUITY", "FIXED_INCOME", "CASH"):
                continue                                          # a class with no target and nothing held is not worth a line
            status, text, codes = judge(cls)
            if only_above and sc.mix_pct.get(cls, 0.0) <= float(targets.get(cls, zero)["max"]) and status != U:
                status = G                                        # excess cash: being under the band is not this check's concern
            rows.append((cls, status, text, codes))
        status = worst(r[1] for r in rows)
        codes = [c for r in rows for c in r[3]]
        metrics = {r[0]: {"actual_pct": sc.mix_pct.get(r[0], 0.0), "status": r[1], **{k: float(v) for k, v in targets.get(r[0], zero).items()}}
                   for r in rows}
        metrics["unclassified_pct"] = unclassified
        if key == "fixed_income_international":
            metrics["international_share_of_equity_pct"] = sc.res.intl_share_pct()
            metrics["equity_geography_unknown_pct"] = sc.equity_geography_unknown_pct
        notes = []
        if unclassified > tol:
            notes.append(f"{unclassified:.1f}% of the portfolio could not be classified, so these numbers may be off.")
        return _check(key, title, status, "; ".join(r[2] for r in rows), metrics, codes, notes)

    rest = [c for c in classes if c not in _NON_DECISION_CLASSES]
    return [group("allocation", "Stock and other allocation", rest),
            group("fixed_income_international", "Bonds and international", [c for c in ("FIXED_INCOME", "INTL_EQUITY") if c in classes or c in targets]),
            group("excess_cash", "Excess cash", ["CASH"], only_above=True)]


def _emergency_reserve(inp: HealthInputs) -> HealthCheck:
    """Counts ONLY cash and CASH / MONEY_MARKET holdings in taxable-treatment accounts (TAXABLE and CASH account types)."""
    pol = inp.policy
    reserve = float(pol.emergency_reserve_amount)
    own = str(pol.emergency_reserve_source).upper() == "USER"
    label = "your own reserve" if own else "the default reserve"
    counted, known, excluded_unknown = 0.0, False, 0.0
    detail = []
    for c in inp.cash:
        acct = inp.accounts.get(c.get("account_id")) or {}
        t = tax_treatment(acct.get("account_type"))
        amount = float(c.get("amount") or 0)
        if t is None:
            excluded_unknown += amount
        elif t.value == "TAXABLE":
            counted += amount
            known = True
            detail.append(f"{acct.get('display_name') or acct.get('broker') or 'account'}: {_money(amount)} cash")
    for h in inp.holdings:
        if (h.get("instrument_type") or "") not in _CASH_TYPES:
            continue
        acct = inp.accounts.get(h.get("account_id")) or {}
        t = tax_treatment(acct.get("account_type") or h.get("account_type"))
        v = _value(h)
        if t is None:
            excluded_unknown += v
        elif t.value == "TAXABLE":
            counted += v
            known = True
            detail.append(f"{h.get('ticker')}: {_money(v)}")
    codes, notes = [], ["Cash inside a 401(k), IRA, HSA or 529 is not counted: it is not available in an emergency without taxes or penalties."]
    if excluded_unknown > 0:
        codes.append(RC_ACCOUNT_TYPE_UNKNOWN)
        notes.append(f"{_money(excluded_unknown)} is in an account whose type is not set, so it was not counted.")
    metrics = {"reserve_amount": reserve, "reserve_source": "USER" if own else "DEFAULT", "counted_cash": round(counted, 2),
               "excluded_unknown_type": round(excluded_unknown, 2)}
    if not known:
        return _check("emergency_reserve", "Emergency reserve", U,
                      f"No cash balance is known in a taxable or cash account, so the {_money(reserve)} reserve ({label}) cannot be checked.",
                      metrics, codes + [RC_NO_CASH_DATA], notes)
    if counted < reserve:
        short = reserve - counted
        metrics["shortfall"] = round(short, 2)
        return _check("emergency_reserve", "Emergency reserve", A,
                      f"{_money(counted)} in reserve cash vs {_money(reserve)} ({label}): {_money(short)} short.",
                      metrics, codes, notes + detail)
    metrics["surplus"] = round(counted - reserve, 2)
    return _check("emergency_reserve", "Emergency reserve", G,
                  f"{_money(counted)} in reserve cash vs {_money(reserve)} ({label}).", metrics, codes, notes + detail)


def _concentration(sc: _Scope, inp: HealthInputs, *, household: bool) -> HealthCheck:
    pol, st = inp.policy, inp.settings
    frac = float(st["health_watch_limit_fraction"])
    tol = float(st["health_unknown_share_tolerance_pct"])
    total = sc.res.total_value
    singles = [p for p in sc.positions if p["instrument_type"] not in _CASH_TYPES and not _is_fund_like(p["instrument_type"])
               and p["market_value"] > 0]
    by_ticker: dict = {}
    for p in singles:
        by_ticker[p["ticker"]] = by_ticker.get(p["ticker"], 0.0) + p["market_value"]
    top_pos = max(by_ticker.items(), key=lambda kv: kv[1], default=None)
    named = {k: v for k, v in sc.issuer_pct.items()}
    top_issuer = max(named.items(), key=lambda kv: kv[1], default=None)
    sectors = {k: v for k, v in sc.sector_pct.items() if k != "Unknown"}
    top_sector = max(sectors.items(), key=lambda kv: kv[1], default=None)
    sector_unknown = sc.sector_pct.get("Unknown", 0.0)

    partial = sc.has_funds and not sc.lookthrough_used          # what the funds hold is only partly known
    rows, codes, notes = [], [], []
    if top_pos:
        pct = _pct(top_pos[1], total)
        rows.append(("position", _limit_status(pct, pol.max_position_pct, frac), f"largest single security {top_pos[0]} {pct:.1f}% (limit {pol.max_position_pct:.0f}%)"))
    elif total > 0:
        rows.append(("position", G, "no single-security positions"))
    if top_issuer:
        pct = top_issuer[1]
        status = _limit_status(pct, pol.max_issuer_pct, frac)
        text = f"largest issuer {top_issuer[0]} {pct:.1f}% (limit {pol.max_issuer_pct:.0f}%)" + (" counting what funds hold" if sc.has_funds else "")
        if partial and status == G:
            status = U
            codes.append(RC_LOOKTHROUGH_PARTIAL)
            text += " — fund contents only partly known, so this may understate it"
        rows.append(("issuer", status, text))
    if top_sector:
        pct = top_sector[1]
        status = _limit_status(pct, pol.max_sector_pct, frac)
        text = f"largest sector {top_sector[0]} {pct:.1f}% (limit {pol.max_sector_pct:.0f}%)"
        if status == G and partial:
            status = U
            codes.append(RC_LOOKTHROUGH_PARTIAL)
            text += " — fund contents only partly known"
        elif status == G and sector_unknown > tol:
            status = U
            codes.append(RC_SECTOR_UNKNOWN)
            text += f" — {sector_unknown:.0f}% has no sector"
        rows.append(("sector", status, text))
    if not rows:
        return _empty("concentration", "Concentration")
    if partial:
        notes.append(f"Fund contents are {sc.res.coverage_pct:.0f}% known (minimum {inp.settings['lookthrough_min_coverage_pct']:.0f}% to look through).")
    metrics = {"top_position": {"ticker": top_pos[0], "pct": _pct(top_pos[1], total)} if top_pos else None,
               "top_issuer": {"issuer": top_issuer[0], "pct": top_issuer[1]} if top_issuer else None,
               "top_sector": {"sector": top_sector[0], "pct": top_sector[1]} if top_sector else None,
               "sector_unknown_pct": sector_unknown, "coverage_pct": sc.res.coverage_pct,
               "limits": {"position": pol.max_position_pct, "issuer": pol.max_issuer_pct, "sector": pol.max_sector_pct}}
    scope_note = "" if household else " (within this slice)"
    return _check("concentration", "Concentration", worst(r[1] for r in rows), "; ".join(r[2] for r in rows) + scope_note, metrics, codes, notes)


def _diversification(sc: _Scope, inp: HealthInputs, *, household: bool) -> HealthCheck:
    st = inp.settings
    watch_n, action_n = float(st["health_min_effective_n_watch"]), float(st["health_min_effective_n_action"])
    corr_w, corr_a = float(st["health_max_avg_correlation_watch"]), float(st["health_max_avg_correlation_action"])
    values = [v["value"] for v in sc.res.named_issuers().values()]
    if not values and sc.res.total_value <= 0:
        return _empty("diversification", "Diversification")
    codes, notes = [], []
    stats = lt.concentration_stats(values, explained_value=sum(values)) if values else {}
    n = stats.get("effective_n")
    metrics = {"effective_n": n, "named_issuers": len(values), "coverage_pct": sc.res.coverage_pct}
    partial = sc.has_funds and not sc.lookthrough_used
    if partial or n is None:
        status = U
        codes.append(RC_LOOKTHROUGH_PARTIAL if partial else RC_NO_HOLDINGS)
        summary = ("Cannot judge diversification: what the funds hold is only partly known." if partial
                   else "No issuers to measure yet.")
    else:
        status = A if n < action_n else W if n < watch_n else G
        summary = f"Effective number of holdings {n:.1f} (WATCH below {watch_n:g}, ACTION below {action_n:g})" + \
                  (" counting what funds hold" if sc.has_funds else "")
    if household:
        rows = [r for r in (inp.exposures.risk_ctx or {}).values() if isinstance(r, dict) and isinstance(r.get("correlation_with_portfolio"), (int, float))]
        if rows:
            vals = [float(r["correlation_with_portfolio"]) for r in rows]
            avg = sum(vals) / len(vals)
            top = max((t for t, r in inp.exposures.risk_ctx.items() if isinstance(r, dict) and isinstance(r.get("correlation_with_portfolio"), (int, float))),
                      key=lambda t: inp.exposures.risk_ctx[t]["correlation_with_portfolio"])
            metrics.update({"avg_correlation": round(avg, 3), "max_correlation": round(max(vals), 3), "most_correlated": top})
            cstatus = A if avg > corr_a else W if avg > corr_w else G
            status = worst([status, cstatus])
            summary += f"; average correlation with the rest {avg:.2f} (WATCH above {corr_w:g}, ACTION above {corr_a:g})"
        else:
            codes.append(RC_CORRELATION_UNAVAILABLE)
            if status == G:
                status = U
            summary += "; correlation not available (no price history)"
    return _check("diversification", "Diversification", status, summary, metrics, codes, notes)


def _leveraged(sc: _Scope, inp: HealthInputs) -> HealthCheck:
    pol, st = inp.policy, inp.settings
    if sc.res.total_value <= 0:
        return _empty("leveraged_inverse", "Leveraged / inverse funds")
    pct = sc.res.leveraged_inverse_pct()
    limit = float(pol.max_leveraged_inverse_pct)
    status = _limit_status(pct, limit, float(st["health_watch_limit_fraction"]))
    codes = []
    unknown_type_pct = _pct(sum(p["market_value"] for p in sc.positions if p["instrument_type"] in ("UNKNOWN", "", None)), sc.res.total_value)
    if status == G and unknown_type_pct > float(st["health_unknown_share_tolerance_pct"]):
        status, codes = U, [RC_TYPE_UNKNOWN]
    summary = f"Leveraged or inverse funds {pct:.1f}% of the portfolio (limit {limit:.0f}%)"
    if codes:
        summary += f"; {unknown_type_pct:.0f}% is not classified yet, so leverage cannot be ruled out"
    return _check("leveraged_inverse", "Leveraged / inverse funds", status, summary,
                  {"pct": pct, "limit_pct": limit, "leverage_adjustment": round(sc.res.leverage_adjustment, 2)}, codes)


def _fees(sc: _Scope, inp: HealthInputs) -> HealthCheck:
    st = inp.settings
    high = float(inp.settings["high_expense_ratio_pct"])
    tol = float(st["health_unknown_share_tolerance_pct"])
    funds = [p for p in sc.positions if _is_fund_like(p["instrument_type"]) and p["market_value"] > 0]
    if sc.res.total_value <= 0:
        return _empty("fees", "Fund fees")
    if not funds:
        return _check("fees", "Fund fees", G, "No funds held, so there are no fund expenses.", {"fund_value": 0.0})
    by_ticker: dict = {}
    for p in funds:
        by_ticker[p["ticker"]] = by_ticker.get(p["ticker"], 0.0) + p["market_value"]
    fund_value = sum(by_ticker.values())
    known_value = cost = 0.0
    high_list, unknown_list = [], []
    for t, v in by_ticker.items():
        er = (inp.views.get(t).expense_ratio if inp.views.get(t) else None)
        if er is None:
            unknown_list.append(t)
            continue
        known_value += v
        cost += v * er
        if er * 100.0 > high:
            high_list.append({"ticker": t, "expense_ratio_pct": round(er * 100, 3), "value": round(v, 2)})
    weighted = (cost / known_value * 100.0) if known_value > 0 else None
    unknown_pct = _pct(fund_value - known_value, fund_value)
    notes = ["Estimated fund expenses: each fund's published expense ratio × its value. They are taken from the fund's price, not billed to you separately."]
    codes = []
    for t in sorted(by_ticker):
        notes.extend(_cheaper_class_notes(t, inp))
    if high_list:
        status = W
        summary = f"{len(high_list)} fund(s) cost more than {high:.2f}% a year: " + ", ".join(f"{h['ticker']} {h['expense_ratio_pct']:.2f}%" for h in high_list)
    elif weighted is not None and weighted > high:
        status = W
        summary = f"Weighted expense ratio {weighted:.2f}% is above {high:.2f}%"
    else:
        status = G
        summary = (f"Weighted expense ratio {weighted:.2f}%" if weighted is not None else "Expense ratios unknown") + f" (flag above {high:.2f}%)"
    if unknown_pct > 0 and status == G and (unknown_pct > tol or weighted is None):
        status = U
        codes.append(RC_EXPENSE_RATIO_UNKNOWN)
        summary += f"; {unknown_pct:.0f}% of fund value has no expense ratio on record"
    elif unknown_pct > 0:
        codes.append(RC_EXPENSE_RATIO_UNKNOWN)
    if weighted is not None:
        summary += f". Estimated fund expenses about {_money(cost)} a year"
    metrics = {"fund_value": round(fund_value, 2), "weighted_expense_ratio_pct": round(weighted, 4) if weighted is not None else None,
               "estimated_annual_fund_cost": round(cost, 2), "expense_ratio_known_pct": round(100 - unknown_pct, 2),
               "high_threshold_pct": high, "above_threshold": high_list, "expense_ratio_unknown": sorted(unknown_list)}
    return _check("fees", "Fund fees", status, summary, metrics, codes, notes)


def _cheaper_class_notes(ticker: str, inp: HealthInputs) -> list[str]:
    """A cheaper class of the SAME fund (same share_class_group) that we hold data for. Never a different product."""
    view = inp.views.get(ticker)
    group = getattr(view, "share_class_group", None) if view is not None else None
    if not group or view.expense_ratio is None:
        return []
    out = []
    for peer in inp.share_class_peers.get(group, []):
        if peer["ticker"] != ticker and peer.get("expense_ratio") is not None and peer["expense_ratio"] < view.expense_ratio:
            out.append(f"{ticker} ({view.expense_ratio * 100:.2f}%) is also sold as {peer['ticker']} at {peer['expense_ratio'] * 100:.2f}% — "
                       f"same fund, different share class (minimums and account rules apply; not a recommendation).")
    return out


def _profile(inp: HealthInputs) -> HealthCheck:
    pol = inp.policy
    metrics = {"profile": pol.profile, "source": pol.profile_source, "above_cap_acknowledged": bool(pol.above_cap_acknowledged)}
    if str(pol.profile_source).upper() == "DEFAULT":
        return _check("profile", "Risk profile", U,
                      f"You are on the default {pol.profile.title()} profile — nobody has chosen it yet. Take the 3-question quiz on the Profile page "
                      f"so the limits and targets reflect you.", metrics, [RC_PROFILE_DEFAULT])
    if pol.above_cap_acknowledged:
        return _check("profile", "Risk profile", W,
                      f"{pol.profile.title()} is riskier than your time horizon suggests; you acknowledged that.", metrics, [RC_ABOVE_CAP])
    return _check("profile", "Risk profile", G, f"{pol.profile.title()} profile ({str(pol.profile_source).lower()}).", metrics)


def _restricted_part(inp: HealthInputs) -> HealthCheck:
    """The restricted-list half of the compliance check (what the check was before the ledger existed)."""
    if inp.restricted is None:
        return _check("compliance", "Restricted list", U, "The restricted list could not be read, so it was not checked.",
                      codes=[RC_RESTRICTED_UNREADABLE])
    held = {str(h.get("ticker", "")).upper() for h in inp.holdings if _value(h) > 0}
    hits = [r for r in inp.restricted if str(r.get("ticker", "")).upper() in held]
    if hits:
        return _check("compliance", "Restricted list", A,
                      "Held but on your restricted list: " + ", ".join(f"{r['ticker']} ({r.get('reason') or 'no reason given'})" for r in hits),
                      {"held_restricted": [r["ticker"] for r in hits]})
    return _check("compliance", "Restricted list", G, f"None of your holdings are on your restricted list ({len(inp.restricted)} listed).",
                  {"held_restricted": []})


def _compliance(inp: HealthInputs) -> HealthCheck:
    restricted = _restricted_part(inp)
    cl = inp.clearance or {}
    missing, unknown = int(cl.get("missing") or 0), int(cl.get("unknown") or 0)
    if not missing and not unknown:
        return restricted                                  # no ledger trade needs attention: exactly the restricted-list check, as before
    parts, statuses, codes, metrics = [], [restricted.status], list(restricted.reason_codes), dict(restricted.metrics)
    if restricted.status != G:
        parts.append(restricted.summary)
    if missing:
        tk = ", ".join(cl.get("missing_tickers") or [])
        parts.append(f"{missing} recorded trade{'s' if missing != 1 else ''} had no pre-clearance approval on file" + (f" ({tk})" if tk else "") +
                     ". Review them on the Accounts page.")
        statuses.append(A)
        codes.append(RC_TRADES_WITHOUT_CLEARANCE)
    if unknown:
        parts.append(f"{unknown} recorded trade{'s' if unknown != 1 else ''} could not be checked for pre-clearance (approvals unreadable).")
        statuses.append(W)
        codes.append(RC_CLEARANCE_UNKNOWN)
    metrics.update({"trades_missing_clearance": missing, "trades_clearance_unknown": unknown, "missing_clearance_tickers": cl.get("missing_tickers") or []})
    return _check("compliance", "Compliance", worst(statuses), " ".join(parts), metrics, codes)


def _data_quality(sc: _Scope, inp: HealthInputs) -> HealthCheck:
    st = inp.settings
    now = inp.now or datetime.now(timezone.utc)
    total = sc.res.total_value
    if not sc.positions:
        return _empty("data_quality", "Data quality")
    from portfolio_agent.tools.price_freshness import is_price_stale
    issues: dict = {}

    def add(code, ticker, value=0.0):
        d = issues.setdefault(code, {"code": code, "tickers": [], "value": 0.0})
        if ticker not in d["tickers"]:
            d["tickers"].append(ticker)
        d["value"] += value

    holdings_here = [h for h in inp.holdings if _scope_has(sc, h)]
    unpriced_value = 0.0
    for h in holdings_here:
        t, itype = str(h.get("ticker", "")).upper(), h.get("instrument_type") or "UNKNOWN"
        if itype in _CASH_TYPES:
            continue
        if _value(h) <= 0:
            add(RC_UNPRICED, t)
            unpriced_value += float(h.get("shares") or 0) * float(h.get("avg_cost") or 0)     # best-effort size of what is unpriced
        if itype == "UNKNOWN":
            add(RC_TYPE_UNKNOWN, t, _value(h))
        stale = is_price_stale(h.get("price_as_of"), itype, now)
        if stale is True:
            add(RC_PRICE_STALE, t)
        elif stale is None and _value(h) > 0:
            add(RC_PRICE_DATE_UNKNOWN, t)
    ttl = {"etf": float(st["etf_data_ttl_hours"]), "mf": float(st["mutual_fund_data_ttl_hours"])}
    mult = float(st["health_stale_fund_data_multiple"])
    for h in holdings_here:
        t, itype = str(h.get("ticker", "")).upper(), h.get("instrument_type") or "UNKNOWN"
        if not _is_fund_like(itype):
            continue
        view = inp.views.get(t)
        if itype in _NO_DATA_FUND_TYPES:
            if view is None or view.source == lt.SRC_NONE:
                add(RC_NO_BREAKDOWN, t, _value(h))
            continue
        fetched = inp.fund_fetched_at.get(t)
        if view is None or view.source == lt.SRC_NONE or fetched is None:
            if view is None or view.source in (lt.SRC_NONE,):
                add(RC_FUND_DATA_MISSING, t, _value(h))
            continue
        hours = (now - _aware(fetched)).total_seconds() / 3600.0
        limit_h = (ttl["etf"] if itype in ("ETF", "ETN") else ttl["mf"]) * mult
        if hours > limit_h:
            add(RC_FUND_DATA_STALE, t, _value(h))
    codes = list(issues)
    if sc.has_funds and sc.res.coverage_pct < float(st["lookthrough_min_coverage_pct"]):
        codes.append(RC_LOOKTHROUGH_PARTIAL)
        issues[RC_LOOKTHROUGH_PARTIAL] = {"code": RC_LOOKTHROUGH_PARTIAL, "tickers": [], "value": 0.0,
                                          "coverage_pct": sc.res.coverage_pct}
    if not issues:
        return _check("data_quality", "Data quality", G, "Every holding is priced and classified, prices are current and fund data is fresh.",
                      {"issues": []})
    unpriced_pct = _pct(unpriced_value, total + unpriced_value)
    status = A if (RC_UNPRICED in issues and unpriced_pct > float(st["health_unpriced_action_pct"])) else W
    labels = {RC_UNPRICED: "no price", RC_TYPE_UNKNOWN: "type not classified", RC_PRICE_STALE: "price out of date",
              RC_PRICE_DATE_UNKNOWN: "price date unknown", RC_FUND_DATA_STALE: "fund data stale",
              RC_FUND_DATA_MISSING: "no fund data", RC_NO_BREAKDOWN: "no breakdown or proxy (CIT / stable value)",
              RC_LOOKTHROUGH_PARTIAL: f"fund contents only {sc.res.coverage_pct:.0f}% known"}
    parts = [f"{labels[c]}{' (' + ', '.join(issues[c]['tickers'][:4]) + (', …' if len(issues[c]['tickers']) > 4 else '') + ')' if issues[c]['tickers'] else ''}"
             for c in codes]
    return _check("data_quality", "Data quality", status, "Gaps: " + "; ".join(parts) + ".",
                  {"issues": [{**v, "value": round(v["value"], 2)} for v in issues.values()], "unpriced_pct": unpriced_pct}, codes)


def _aware(iso: str) -> datetime:
    d = datetime.fromisoformat(iso)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _scope_has(sc: _Scope, h) -> bool:
    return any(p["ticker"] == str(h.get("ticker", "")).upper() and p["account_id"] == h.get("account_id") for p in sc.positions)


# ── Evaluate ──────────────────────────────────────────────────────────────────

def evaluate(inp: HealthInputs, *, version: str = "", as_of: date | None = None) -> PortfolioHealth:
    """Pure: judge the gathered inputs. Same inputs, same result."""
    positions = _positions(inp)
    depth = int(inp.settings["lookthrough_max_depth"])
    result = lt.analyze(positions, inp.views, max_depth=depth)
    empty = not positions

    def scope(key, label, res, plist) -> _Scope:
        return _build_scope(key, label, res, plist, inp)

    hh = scope("household", "Household", result.household, positions)
    checks: list[HealthCheck] = []
    if empty:
        for k, t in (("allocation", "Stock and other allocation"), ("fixed_income_international", "Bonds and international"),
                     ("excess_cash", "Excess cash"), ("emergency_reserve", "Emergency reserve"), ("concentration", "Concentration"),
                     ("diversification", "Diversification"), ("leveraged_inverse", "Leveraged / inverse funds"), ("fees", "Fund fees")):
            checks.append(_empty(k, t))
    else:
        checks += _allocation_checks(hh, inp)
        checks += [_emergency_reserve(inp), _concentration(hh, inp, household=True), _diversification(hh, inp, household=True),
                   _leveraged(hh, inp), _fees(hh, inp)]
    checks += [_profile(inp), _compliance(inp)]
    checks.append(_data_quality(hh, inp) if not empty else _empty("data_quality", "Data quality"))
    household = ScopeHealth("household", "Household", round(hh.res.total_value, 2), tuple(checks),
                            {k: v for k, v in hh.mix_pct.items()}, _counts(checks))

    def slice_health(sc: _Scope) -> ScopeHealth:
        cs = (_concentration(sc, inp, household=False), _diversification(sc, inp, household=False), _leveraged(sc, inp),
              _fees(sc, inp), _data_quality(sc, inp))
        return ScopeHealth(sc.key, sc.label, round(sc.res.total_value, 2), cs, dict(sc.mix_pct), _counts(cs))

    by_account, by_tax = {}, {}
    for aid, res in result.by_account.items():
        acct = inp.accounts.get(int(aid)) if str(aid).lstrip("-").isdigit() else None
        label = (acct or {}).get("display_name") or (acct or {}).get("broker") or f"Account {aid}"
        plist = [p for p in positions if str(p["account_id"]) == aid]
        by_account[aid] = slice_health(scope(f"account:{aid}", label, res, plist))
    names = {"TAXABLE": "Taxable", "TAX_DEFERRED": "Tax-deferred", "TAX_FREE": "Tax-free", "UNKNOWN": "Account type not set"}
    for tx, res in result.by_tax_treatment.items():
        plist = [p for p in positions if p["tax_treatment"] == tx]
        by_tax[tx] = slice_health(scope(f"tax:{tx}", names.get(tx, tx), res, plist))
    pol = inp.policy
    codes = list(dict.fromkeys(c for s in (household, *by_account.values(), *by_tax.values()) for ch in s.checks for c in ch.reason_codes))
    return PortfolioHealth(
        owner_scope=f"user:{inp.owner}", as_of=(as_of or date.today()).isoformat(),
        built_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), version=version,
        household=household, by_account=by_account, by_tax_treatment=by_tax,
        profile={"profile": pol.profile, "source": pol.profile_source, "reserve_amount": pol.emergency_reserve_amount,
                 "reserve_source": pol.emergency_reserve_source},
        reason_codes=tuple(codes))


# ── Gather (the only part that reads) ─────────────────────────────────────────

def gather_inputs(owner: str, portfolio: dict, *, now: datetime | None = None) -> HealthInputs:
    """Read everything the checks need for *owner*'s own portfolio (the caller has already authorized it)."""
    from portfolio_agent.services import fund_data_service as fds
    from portfolio_agent.tools import fund_data_db as fdb
    from portfolio_agent.tools import investor_settings_db as sdb
    from portfolio_agent.tools import portfolio_assessment as pa
    from portfolio_agent.tools.portfolio_policy import get_effective_policy_for_owner
    from portfolio_agent.tools.restricted_list_db import list_restricted_by_owner

    pid = portfolio["portfolio_id"]
    policy = get_effective_policy_for_owner(owner)
    settings = sdb.get_portfolio_settings()
    holdings = list(repo.get_holdings(portfolio_id=pid))
    cash = list(repo.get_cash_balances(portfolio_id=pid))
    accounts = {a["account_id"]: a for a in repo.list_accounts(pid)}
    exposures = pa.compute_exposures_for_owner(owner, portfolio, holdings=holdings, cash=cash, policy=policy)
    fund_tickers = sorted({str(h.get("ticker", "")).upper() for h in holdings if _is_fund_like(h.get("instrument_type"))})
    views = fds.load_fund_views(owner, fund_tickers, max_depth=int(settings["lookthrough_max_depth"])) if fund_tickers else {}
    fetched, peers = {}, {}
    for t in views:
        inst = repo.find_instrument(t)
        if inst is None:
            continue
        row = fdb.get_fund_profile_row(inst["instrument_id"])
        if row:
            fetched[t] = row["fetched_at"]
            grp = row.get("share_class_group")
            if grp:
                views[t].share_class_group = grp
                peers[grp] = [{"ticker": p["ticker"], "expense_ratio": p["expense_ratio"]} for p in fdb.list_profiles_in_group(grp)]
    try:
        restricted = list_restricted_by_owner(owner)
    except Exception:
        restricted = None
    try:                                                  # ledger trades with no pre-clearance approval (empty for a portfolio with no ledger trades)
        from portfolio_agent.tools import ledger_db as ldb
        with repo.unit_of_work() as lc:
            clearance = ldb.clearance_counts(lc, pid)
    except Exception:
        clearance = None
    ikey = {}
    cik = pa._issuer_key_map()
    for v in views.values():
        for h in v.holdings:
            if h.symbol:
                ikey[h.symbol.upper()] = cik.get(h.symbol.upper(), h.symbol.upper())
    for h in holdings:
        t = str(h.get("ticker", "")).upper()
        ikey[t] = exposures.issuer_of.get(t) or cik.get(t, t)
    return HealthInputs(owner=owner, policy=policy, settings=settings, holdings=holdings, cash=cash, accounts=accounts,
                        exposures=exposures, views=views, restricted=restricted, share_class_peers=peers,
                        fund_fetched_at=fetched, now=now, issuer_key=ikey, clearance=clearance)


def _cache_key(inp: HealthInputs, day: date) -> str:
    cash = sorted((c.get("account_id"), round(float(c.get("amount") or 0), 2)) for c in inp.cash)
    restricted = sorted(str(r.get("ticker")) for r in (inp.restricted or []))
    thresholds = {k: inp.settings.get(k) for k in sorted(inp.settings) if k.startswith("health_") or k.startswith("lookthrough")
                  or k.endswith("_ttl_hours") or k == "high_expense_ratio_pct"}
    raw = json.dumps([inp.exposures.version, inp.policy.fingerprint, cash, restricted, thresholds, day.isoformat(),
                      [(inp.clearance or {}).get("missing"), (inp.clearance or {}).get("unknown")],
                      [(h.get("ticker"), h.get("account_id"), h.get("shares"), h.get("current_price"), h.get("price_as_of")) for h in inp.holdings]],
                     default=str, sort_keys=True)
    import hashlib
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 300.0


def clear_health_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def get_portfolio_health_for_owner(owner: str, portfolio: dict, *, now: datetime | None = None, use_cache: bool = True) -> PortfolioHealth:
    """
    PortfolioHealth for an owner the caller has authorized (portfolio is that owner's own). Cached per
    (owner, everything that feeds the checks, day): changing a holding, the policy, a threshold, fund data or the
    restricted list produces a new key, so a stale result is never served.
    """
    inp = gather_inputs(owner, portfolio, now=now)
    day = (now or datetime.now()).date()
    key = _cache_key(inp, day)
    if use_cache:
        with _CACHE_LOCK:
            hit = _CACHE.get((owner, key))
            if hit and time.monotonic() - hit[0] < _CACHE_TTL:
                return hit[1]
    health = evaluate(inp, version=key, as_of=day)
    if use_cache:
        with _CACHE_LOCK:
            _CACHE[(owner, key)] = (time.monotonic(), health)
    return health


def get_portfolio_health(ctx, *, now: datetime | None = None) -> PortfolioHealth:
    """Authorized: the actor's own portfolio only (NotAuthorized for anyone without one)."""
    from portfolio_agent.services.account_service import resolve_portfolio
    portfolio = resolve_portfolio(ctx)
    return get_portfolio_health_for_owner(ctx.actor, portfolio, now=now)
