"""
Look-through engine — what a person REALLY owns once funds are opened up. Pure and deterministic: positions and fund
views in, numbers out. No database, no network, no model; the same inputs always give the same answer.

Inputs
  positions   [{account_id, tax_treatment, ticker, market_value, instrument_type, leverage_factor, asset_class, is_paper}]
  views       {TICKER: FundView}   what is known about each fund (from fund_data_service.load_fund_views)
  max_depth   how many fund-in-fund levels to open (a target-date fund holding index funds is depth 2)

Output  analyze(...) -> LookThrough with one ScopeResult per scope:
  "household"                    every non-paper position
  "account:<account_id>"         one account
  "tax:<TAXABLE|TAX_DEFERRED|…>" one tax treatment
Paper accounts never appear in any scope.

How a fund is opened
  * Each named holding becomes issuer exposure: fund value × holding weight, with the fund recorded in "via".
  * A holding that is itself a fund is opened in turn, up to max_depth, with a cycle guard (a fund is never opened
    twice on one path).
  * Whatever the named holdings do not explain stays in a bucket named "<FUND>_other" — never dropped, never spread
    over the named issuers. A fund with no data at all is one "<FUND>_unknown" bucket.
  * Sector / asset-class mix comes from the fund's own mix when it has one, else from its sub-funds.

Missing data is a reason code (RC_*), never a guess. coverage_pct is the share of the scope's value that is explained by
real issuers or a known mix; callers decide what coverage is enough.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from portfolio_agent.domain import FUND_INSTRUMENT_TYPES

RC_LOOKTHROUGH_PARTIAL = "LOOKTHROUGH_PARTIAL"            # coverage below the minimum: direct numbers used instead
RC_LOOKTHROUGH_NO_DATA = "LOOKTHROUGH_NO_FUND_DATA"       # a fund with no holdings, manual breakdown or proxy
RC_LOOKTHROUGH_CYCLE = "LOOKTHROUGH_CYCLE"                # a fund (indirectly) holds itself
RC_LOOKTHROUGH_DEPTH_LIMIT = "LOOKTHROUGH_DEPTH_LIMIT"    # sub-fund not opened: max_depth reached
RC_LOOKTHROUGH_MISSING_LEVEL = "LOOKTHROUGH_MISSING_LEVEL"  # a holding is a fund but nothing is stored for it
RC_LOOKTHROUGH_TOP_N_ONLY = "LOOKTHROUGH_TOP_HOLDINGS_ONLY"
RC_LOOKTHROUGH_MIX_UNKNOWN = "LOOKTHROUGH_MIX_UNKNOWN"    # a fund's sector / asset-class mix is not known
RC_LOOKTHROUGH_UNMAPPED = "LOOKTHROUGH_UNMAPPED_HOLDINGS"  # holdings known only by CUSIP / ISIN: explained, but not tied to a ticker

SRC_OWN, SRC_MANUAL, SRC_PROXY, SRC_NONE = "own", "manual", "proxy", "none"

_EPS = 1e-9
_CLASS_KEY = {"US_EQUITY": "stock", "INTL_EQUITY": "stock", "FIXED_INCOME": "bond", "CASH": "cash",
              "REAL_ESTATE": "real_estate", "COMMODITY": "commodity"}


def holding_key(h: "ViewHolding") -> str:
    """How a holding is keyed in the exposures: its ticker; else its CUSIP ("CUSIP:037833100") or ISIN, so the same security reached through
    two funds is ONE exposure even without a ticker; else its name. Never a guess at a ticker."""
    if h.symbol:
        return h.symbol.upper()
    if h.cusip:
        return f"CUSIP:{h.cusip.upper()}"
    if h.isin:
        return f"ISIN:{h.isin.upper()}"
    return (h.name or "?").upper()


@dataclass(frozen=True)
class ViewHolding:
    symbol: str | None
    name: str | None
    weight: float                                   # fraction of the fund
    is_fund: bool = False
    cusip: str | None = None                        # identifiers, kept for a holding that has no ticker (yet)
    isin: str | None = None


@dataclass
class FundView:
    """What is known about one fund, and where that knowledge came from."""
    ticker: str
    name: str | None = None
    source: str = SRC_NONE                          # own | manual | proxy | none
    via: str | None = None                          # the proxy ticker, when source == proxy
    holdings: list[ViewHolding] = field(default_factory=list)
    sector_weights: dict[str, float] = field(default_factory=dict)      # fractions of the fund's equity part
    asset_class_weights: dict[str, float] = field(default_factory=dict)  # fractions: stock/bond/cash/other
    country_weights: dict[str, float] | None = None
    asset_class: str | None = None                  # the instrument's own class (US_EQUITY / INTL_EQUITY / …)
    expense_ratio: float | None = None
    holdings_coverage: float = 0.0                  # 0-1: how much the named holdings explain
    mapped_coverage: float | None = None            # 0-1: how much of that has a ticker (None = not known; equal for ticker-keyed data)
    as_of: str | None = None
    reason_codes: list[str] = field(default_factory=list)

    @property
    def has_data(self) -> bool:
        return self.source != SRC_NONE and bool(self.holdings or self.asset_class_weights or self.sector_weights)


@dataclass
class ScopeResult:
    scope: str
    total_value: float = 0.0
    issuers: dict[str, dict] = field(default_factory=dict)       # key -> {name, value, exposure, via: {fund: value}, bucket}
    sector_value: dict[str, float] = field(default_factory=dict)
    sector_unknown_value: float = 0.0
    asset_class_value: dict[str, float] = field(default_factory=dict)
    asset_class_unknown_value: float = 0.0
    intl_equity_value: float = 0.0
    equity_value: float = 0.0
    intl_unknown_value: float = 0.0
    explained_value: float = 0.0
    fund_value: float = 0.0
    fund_explained_value: float = 0.0
    fund_mapped_value: float = 0.0                                # the part of fund_explained_value whose holdings have a ticker
    leveraged_inverse_value: float = 0.0
    leverage_adjustment: float = 0.0                              # Σ value × (leverage_factor − 1)
    reason_codes: list[str] = field(default_factory=list)
    proxy_funds: dict[str, str] = field(default_factory=dict)     # fund ticker -> proxy ticker (analytics only)

    # ── derived ────────────────────────────────────────────────────────────────
    @property
    def coverage_pct(self) -> float:
        return 100.0 if self.total_value <= 0 else round(100.0 * self.explained_value / self.total_value, 2)

    @property
    def fund_mapped_coverage_pct(self) -> float | None:
        """Share of the money in funds that is tied to real tickers (<= fund_coverage_pct: unmapped holdings are explained, not mapped)."""
        return None if self.fund_value <= 0 else round(100.0 * self.fund_mapped_value / self.fund_value, 2)

    @property
    def fund_coverage_pct(self) -> float | None:
        return None if self.fund_value <= 0 else round(100.0 * self.fund_explained_value / self.fund_value, 2)

    def named_issuers(self) -> dict[str, dict]:
        return {k: v for k, v in self.issuers.items() if not v.get("bucket")}

    def issuer_pct(self, key: str) -> float:
        return 0.0 if self.total_value <= 0 else 100.0 * self.issuers.get(key, {}).get("value", 0.0) / self.total_value

    def top_issuers(self, n: int = 10) -> list[tuple[str, float]]:
        """[(issuer, % of the scope's total value)], largest first, named issuers only (buckets are not issuers)."""
        named = sorted(self.named_issuers().items(), key=lambda kv: -kv[1]["value"])[:n]
        return [(k, round(100.0 * v["value"] / self.total_value, 4)) for k, v in named] if self.total_value > 0 else []

    def sector_pct(self) -> dict[str, float]:
        """Sector -> % of the scope's total value (the unknown remainder is reported separately, not spread)."""
        return {} if self.total_value <= 0 else {k: round(100.0 * v / self.total_value, 4)
                                                   for k, v in sorted(self.sector_value.items(), key=lambda kv: -kv[1])}

    def asset_class_pct(self) -> dict[str, float]:
        return {} if self.total_value <= 0 else {k: round(100.0 * v / self.total_value, 4)
                                                   for k, v in sorted(self.asset_class_value.items(), key=lambda kv: -kv[1])}

    def intl_share_pct(self) -> float | None:
        """International share of the KNOWN equity part (None when no equity is known)."""
        return None if self.equity_value <= 0 else round(100.0 * self.intl_equity_value / self.equity_value, 2)

    def leveraged_inverse_pct(self) -> float:
        return 0.0 if self.total_value <= 0 else round(100.0 * self.leveraged_inverse_value / self.total_value, 4)

    def concentration(self) -> dict:
        return concentration_stats([v["value"] for v in self.named_issuers().values()], explained_value=self.explained_value)

    def overlap(self, min_sources: int = 2) -> list[dict]:
        """Issuers reached through more than one position: [{issuer, value, pct, sources: [{via, value}]}]."""
        out = []
        for k, v in self.named_issuers().items():
            if len(v["via"]) >= min_sources:
                out.append({"issuer": k, "name": v.get("name"), "value": round(v["value"], 2),
                            "pct": round(100.0 * v["value"] / self.total_value, 4) if self.total_value > 0 else 0.0,
                            "sources": [{"via": s, "value": round(x, 2)} for s, x in sorted(v["via"].items(), key=lambda kv: -kv[1])]})
        return sorted(out, key=lambda o: -o["value"])


@dataclass
class LookThrough:
    household: ScopeResult
    by_account: dict[str, ScopeResult]
    by_tax_treatment: dict[str, ScopeResult]
    max_depth: int


# ── Concentration helpers (pure) ──────────────────────────────────────────────

def concentration_stats(values: list[float], *, explained_value: float | None = None) -> dict:
    """top1/top5/top10 %, HHI and effective N of a set of exposures, NORMALIZED by what is explained, so a fund
    whose holdings are only partly known is not made to look more concentrated than it is by the missing rest.
    Percentages here are of the explained value; the key 'explained_value' says what that base was."""
    vals = sorted((v for v in values if v > 0), reverse=True)
    base = explained_value if explained_value is not None else sum(vals)
    if not vals or base <= 0:
        return {"top1_pct": None, "top5_pct": None, "top10_pct": None, "hhi": None, "effective_n": None,
                "explained_value": round(base, 2)}
    w = [v / base for v in vals]
    hhi = sum(x * x for x in w)
    return {"top1_pct": round(100 * w[0], 4), "top5_pct": round(100 * sum(w[:5]), 4), "top10_pct": round(100 * sum(w[:10]), 4),
            "hhi": round(hhi, 6), "effective_n": round(1.0 / hhi, 2) if hhi > 0 else None, "explained_value": round(base, 2)}


# ── The engine ────────────────────────────────────────────────────────────────

def _is_fund_position(p: dict) -> bool:
    return str(p.get("instrument_type") or "") in {t.value for t in FUND_INSTRUMENT_TYPES}


def _add(codes: list[str], code: str) -> None:
    if code not in codes:
        codes.append(code)


def _issuer(res: ScopeResult, key: str, name: str | None, value: float, exposure: float, via: str, bucket: bool = False) -> None:
    d = res.issuers.setdefault(key, {"name": name, "value": 0.0, "exposure": 0.0, "via": {}, "bucket": bucket})
    d["value"] += value
    d["exposure"] += exposure
    d["via"][via] = d["via"].get(via, 0.0) + value


def _is_intl(view: FundView | None, asset_class: str | None) -> float | None:
    """Share of a fund's equity that is outside the US: from country weights if given, else from the fund's own
    class; None when neither says."""
    if view is not None and view.country_weights:
        us = sum(w for c, w in view.country_weights.items() if str(c).strip().lower() in ("us", "usa", "united states"))
        return max(0.0, min(1.0, 1.0 - us))
    ac = asset_class or (view.asset_class if view is not None else None)
    if ac == "INTL_EQUITY":
        return 1.0
    if ac == "US_EQUITY":
        return 0.0
    return None


def _mix(res: ScopeResult, ticker: str, value: float, views: dict[str, FundView], depth: int, max_depth: int,
         path: tuple[str, ...], asset_class: str | None) -> None:
    """Sector / asset-class / international contribution of `value` held in `ticker` (a fund, opened recursively)."""
    view = views.get(ticker)
    if view is None or not view.has_data:
        res.asset_class_unknown_value += value
        res.sector_unknown_value += value
        res.intl_unknown_value += value
        _add(res.reason_codes, RC_LOOKTHROUGH_MIX_UNKNOWN)
        return
    if view.asset_class_weights:
        total_w = sum(view.asset_class_weights.values()) or 1.0
        for k, w in view.asset_class_weights.items():
            res.asset_class_value[k] = res.asset_class_value.get(k, 0.0) + value * w / total_w
        stock_w = view.asset_class_weights.get("stock", 0.0) / total_w
        eq_value = value * stock_w
        res.equity_value += eq_value
        share = _is_intl(view, asset_class)
        if share is None:
            res.intl_unknown_value += eq_value
        else:
            res.intl_equity_value += eq_value * share
        if view.sector_weights:
            sw_total = sum(view.sector_weights.values()) or 1.0
            for k, w in view.sector_weights.items():
                res.sector_value[k] = res.sector_value.get(k, 0.0) + eq_value * w / sw_total
        elif eq_value > _EPS:
            res.sector_unknown_value += eq_value
        return
    # No own asset-class mix: build it from the sub-funds / holdings.
    named = [h for h in view.holdings if h.symbol]
    explained = 0.0
    for h in named:
        hv = value * h.weight
        explained += hv
        child = (h.symbol or "").upper()
        if h.is_fund and child in views and child not in path and depth < max_depth:
            _mix(res, child, hv, views, depth + 1, max_depth, path + (ticker,), None)
        elif h.is_fund:
            res.asset_class_unknown_value += hv
            res.sector_unknown_value += hv
            res.intl_unknown_value += hv
        else:                                                          # a stock inside: equity, sector unknown here
            res.asset_class_value["stock"] = res.asset_class_value.get("stock", 0.0) + hv
            res.equity_value += hv
            res.sector_unknown_value += hv
            res.intl_unknown_value += hv
    rest = max(0.0, value - explained)
    res.asset_class_unknown_value += rest
    res.sector_unknown_value += rest
    res.intl_unknown_value += rest
    if rest > _EPS:
        _add(res.reason_codes, RC_LOOKTHROUGH_MIX_UNKNOWN)


def _expand(res: ScopeResult, ticker: str, name: str | None, value: float, lf: float, root: str, views: dict[str, FundView],
            depth: int, max_depth: int, path: tuple[str, ...]) -> float:
    """Open one fund; returns the value explained by real issuers (or an explicit named sub-structure)."""
    view = views.get(ticker)
    if view is None or not view.has_data or not view.holdings:
        key = f"{ticker}_unknown"
        _issuer(res, key, f"{name or ticker} (no holdings data)", value, value * lf, root, bucket=True)
        _add(res.reason_codes, RC_LOOKTHROUGH_NO_DATA)
        return 0.0
    if view.source == SRC_PROXY and view.via:
        res.proxy_funds[ticker] = view.via
    explained = 0.0
    weight_sum = 0.0
    for h in view.holdings:
        sym = holding_key(h)
        weight_sum += h.weight
        hv = value * h.weight
        if h.is_fund:
            if sym in path or sym == ticker:
                _add(res.reason_codes, RC_LOOKTHROUGH_CYCLE)
                _issuer(res, f"{ticker}_other", f"Other holdings of {ticker}", hv, hv * lf, root, bucket=True)
            elif depth >= max_depth:
                _add(res.reason_codes, RC_LOOKTHROUGH_DEPTH_LIMIT)
                _issuer(res, f"{ticker}_other", f"Other holdings of {ticker}", hv, hv * lf, root, bucket=True)
            elif sym not in views or not views[sym].has_data:
                _add(res.reason_codes, RC_LOOKTHROUGH_MISSING_LEVEL)
                _issuer(res, f"{sym}_unknown", f"{h.name or sym} (no holdings data)", hv, hv * lf, root, bucket=True)
            else:
                explained += _expand(res, sym, h.name, hv, lf, root, views, depth + 1, max_depth, path + (ticker,))
        else:
            _issuer(res, sym, h.name, hv, hv * lf, root)
            explained += hv
            if h.symbol:
                res.fund_mapped_value += hv
            else:
                _add(res.reason_codes, RC_LOOKTHROUGH_UNMAPPED)
    rest = value * max(0.0, 1.0 - weight_sum)
    if rest > _EPS:
        _issuer(res, f"{ticker}_other", f"Other holdings of {ticker}", rest, rest * lf, root, bucket=True)
        if view.source == SRC_OWN:
            _add(res.reason_codes, RC_LOOKTHROUGH_TOP_N_ONLY)
    return explained


def _scope(scope: str, positions: list[dict], views: dict[str, FundView], max_depth: int) -> ScopeResult:
    res = ScopeResult(scope=scope)
    for p in positions:
        value = float(p.get("market_value") or 0.0)
        if value <= 0:
            continue
        ticker = str(p["ticker"]).upper()
        lf = float(p.get("leverage_factor") or 1.0)
        itype = str(p.get("instrument_type") or "")
        res.total_value += value
        if lf != 1.0:
            res.leveraged_inverse_value += value
            res.leverage_adjustment += value * (lf - 1.0)
        if itype in ("CASH", "MONEY_MARKET"):
            res.asset_class_value["cash"] = res.asset_class_value.get("cash", 0.0) + value
            res.explained_value += value
            continue
        if _is_fund_position(p) or itype in ("CIT", "STABLE_VALUE"):
            res.fund_value += value
            view = views.get(ticker)
            if view is not None and view.has_data and not view.holdings and view.asset_class_weights:
                # a manual breakdown / mix-only fund: the mix is known, the issuers are not
                _issuer(res, f"{ticker}_unknown", f"{view.name or ticker} (mix only)", value, value * lf, ticker, bucket=True)
                _add(res.reason_codes, RC_LOOKTHROUGH_NO_DATA)
                explained_issuers = 0.0
            else:
                explained_issuers = _expand(res, ticker, view.name if view else None, value, lf, ticker, views, 1, max_depth, ())
            res.fund_explained_value += explained_issuers
            _mix(res, ticker, value, views, 1, max_depth, (), p.get("asset_class"))
            mix_known = view is not None and view.has_data and bool(view.asset_class_weights or view.holdings)
            res.explained_value += explained_issuers if explained_issuers > 0 else (value if (mix_known and not view.holdings) else 0.0)
        else:
            _issuer(res, ticker, p.get("name"), value, value * lf, ticker)
            ac = p.get("asset_class")
            key = _CLASS_KEY.get(str(ac), "other")
            res.asset_class_value[key] = res.asset_class_value.get(key, 0.0) + value
            if ac in ("US_EQUITY", "INTL_EQUITY"):
                res.equity_value += value
                if ac == "INTL_EQUITY":
                    res.intl_equity_value += value
            res.explained_value += value
    return res


def analyze(positions: list[dict], views: dict[str, FundView], *, max_depth: int = 3) -> LookThrough:
    """All scopes at once. Paper positions are dropped first; tickers are upper-cased."""
    live = [p for p in positions if not p.get("is_paper")]
    views = {k.upper(): v for k, v in views.items()}
    household = _scope("household", live, views, max_depth)
    accounts: dict[str, list[dict]] = {}
    taxes: dict[str, list[dict]] = {}
    for p in live:
        accounts.setdefault(str(p.get("account_id")), []).append(p)
        taxes.setdefault(str(p.get("tax_treatment") or "UNKNOWN"), []).append(p)
    return LookThrough(
        household=household,
        by_account={a: _scope(f"account:{a}", ps, views, max_depth) for a, ps in accounts.items()},
        by_tax_treatment={t: _scope(f"tax:{t}", ps, views, max_depth) for t, ps in taxes.items()},
        max_depth=max_depth)
