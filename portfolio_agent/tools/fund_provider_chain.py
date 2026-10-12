"""
The fund-data provider chain: ask several sources in a stored order and merge what they return into one FundData.

Order and limits live in portfolio_settings (fund_provider_order_json, <source>_max_age_days, fund_holdings_complete_pct), edited by an admin.
The default order is issuer_csv -> nport -> yfinance: the issuer's own daily file, then the SEC's full quarterly filing, then yfinance's
top-10 holdings / sector mix / expense ratio.

Which source is ASKED. A source is called only if it could still add a piece that is missing (PROVIDES below): once fresh, complete holdings
and a sector mix are in hand, N-PORT is not called. A piece counts as in hand only if its source's data is FRESH (as-of date within that
source's allowed age); stale data does not stop the chain from asking the next source.

How results are MERGED (every rule is per piece, and the merged FundData records which source supplied each in `source_detail`)
  holdings           the FRESH source whose holdings explain the most of the fund (ties: earlier in the order). If no source is fresh, the
                     best stale one is used and the fund carries FUND_DATA_STALE.
  derivative/short   from the holdings source (N-PORT is the only one that has it).
  asset-class mix    the holdings source's own if it has one, else the first fresh source's, else the first stale one's.
  sector mix         the first fresh source that has one (stale: first available, with FUND_DATA_STALE).
  profile            field by field, first source in the order that has the field (expense ratio, category and share class come from yfinance;
                     AUM and family from N-PORT or yfinance). Never taken from a source that is merely older.
  as_of / source     those of the holdings source (the data's own date, not the fetch time).
  reason codes       the union, minus "this piece is missing" codes for pieces the merge now has, minus FUND_HOLDINGS_TOP_N_ONLY unless the
                     holdings came from yfinance, minus "no filing / not configured" codes once another source supplied holdings.
Nothing here raises: a failing source is a reason code on its own result.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone

from portfolio_agent.tools.fund_data_providers import (
    RC_FUND_ASSET_CLASSES_MISSING, RC_FUND_DATA_STALE, RC_FUND_FETCH_FAILED, RC_FUND_HOLDINGS_MISSING,
    RC_FUND_HOLDINGS_TOP_N_ONLY, RC_FUND_PROFILE_MISSING, RC_FUND_SECTORS_MISSING, RC_ISSUER_NOT_CONFIGURED,
    RC_NPORT_NOT_CONFIGURED, RC_NPORT_NOT_FILED, FundData, FundDataProvider, FundProfile,
)

# The pieces each source can supply. "profile" means the cost / category / class fields (an expense ratio), which only yfinance has.
PROVIDES = {
    "issuer_csv": {"holdings", "sectors", "asset_classes"},
    "nport": {"holdings", "asset_classes"},
    "yfinance": {"holdings", "sectors", "asset_classes", "profile"},
}
_MAX_AGE_KEY = {"issuer_csv": "issuer_csv_max_age_days", "nport": "nport_max_age_days", "yfinance": "yfinance_max_age_days"}
_ABSENCE_CODES = {RC_NPORT_NOT_FILED, RC_NPORT_NOT_CONFIGURED, RC_ISSUER_NOT_CONFIGURED}


def _age_days(as_of: str | None, today: date) -> float | None:
    if not as_of:
        return None
    try:
        return float((today - date.fromisoformat(as_of[:10])).days)
    except ValueError:
        return None


class ChainFundProvider(FundDataProvider):
    name = "chain"

    def __init__(self, order: list[str], providers: dict[str, FundDataProvider], settings: dict, *, today=None):
        self._order = [o for o in order if o in providers]
        self._providers = providers
        self._cfg = settings
        self._today = today or (lambda: datetime.now(timezone.utc).date())

    # ── freshness ──
    def _fresh(self, source: str, d: FundData) -> bool:
        """Data with no as-of date was fetched just now (yfinance): fresh. Otherwise its age must be within the source's limit."""
        age = _age_days(d.as_of, self._today())
        return age is None or age <= float(self._cfg[_MAX_AGE_KEY[source]])

    def _in_hand(self, source: str, d: FundData) -> set[str]:
        have: set[str] = set()
        if not self._fresh(source, d):
            return have
        if d.holdings and d.holdings_coverage >= float(self._cfg["fund_holdings_complete_pct"]) / 100.0:
            have.add("holdings")
        if d.sector_weights:
            have.add("sectors")
        if d.asset_class_weights:
            have.add("asset_classes")
        if d.profile and d.profile.expense_ratio is not None:
            have.add("profile")
        return have

    # ── fetch ──
    def fetch(self, ticker: str) -> FundData:
        results: list[tuple[str, FundData]] = []
        have: set[str] = set()
        for name in self._order:
            if not (PROVIDES[name] - have):
                continue                                          # nothing this source could still add
            try:
                d = self._providers[name].fetch(ticker)
            except Exception:
                d = FundData(ticker=ticker.upper(), source=name, reason_codes=[RC_FUND_FETCH_FAILED])
            results.append((name, d))
            have |= self._in_hand(name, d)
        return self.merge(ticker, results)

    # ── merge ──
    def merge(self, ticker: str, results: list[tuple[str, FundData]]) -> FundData:
        out = FundData(ticker=ticker.upper(), source=self.name)
        codes: list[str] = [c for _, d in results for c in d.reason_codes]
        usable = [(n, d) for n, d in results if d.ok]
        if not usable:
            out.reason_codes = list(dict.fromkeys(codes))
            return out
        fresh = lambda n, d: self._fresh(n, d)
        stale_used = False

        # holdings: the fresh source explaining the most, else the best stale one
        with_h = [(n, d) for n, d in usable if d.holdings]
        h_src: tuple[str, FundData] | None = None
        if with_h:
            pool = [x for x in with_h if fresh(*x)]
            if not pool:
                pool, stale_used = with_h, True
            h_src = max(pool, key=lambda x: (x[1].holdings_coverage, -with_h.index(x)))
        if h_src:
            n, d = h_src
            out.holdings, out.holdings_coverage, out.mapped_coverage = list(d.holdings), d.holdings_coverage, d.mapped_coverage
            out.derivative_short_weight, out.as_of, out.source = d.derivative_short_weight, d.as_of, n
            out.source_detail["holdings"] = n

        def first(piece_getter, label, prefer=None):
            nonlocal stale_used
            cands = [(n, d) for n, d in usable if piece_getter(d)]
            if prefer is not None and prefer[0] in [n for n, _ in cands] and piece_getter(prefer[1]):
                return prefer[0], piece_getter(prefer[1])
            pool = [x for x in cands if fresh(*x)]
            if not pool and cands:
                pool, stale_used = cands, True
            return (pool[0][0], piece_getter(pool[0][1])) if pool else (None, None)

        n, v = first(lambda d: d.asset_class_weights, "asset_classes", prefer=h_src)
        if v:
            out.asset_class_weights = dict(v); out.source_detail["asset_classes"] = n
        n, v = first(lambda d: d.sector_weights, "sectors")
        if v:
            out.sector_weights = dict(v); out.source_detail["sectors"] = n
        n, v = first(lambda d: d.country_weights, "countries")
        if v:
            out.country_weights = dict(v)

        # profile, field by field in order
        fields: dict = {}
        src_of_profile: list[str] = []
        for n, d in usable:
            if d.profile is None:
                continue
            for f in ("expense_ratio", "fund_family", "category", "aum", "share_class", "share_class_group"):
                if f not in fields and getattr(d.profile, f) is not None:
                    fields[f] = getattr(d.profile, f)
                    if n not in src_of_profile:
                        src_of_profile.append(n)
        if fields:
            out.profile = FundProfile(**fields)
            out.source_detail["profile"] = (next(n for n, d in usable if d.profile and d.profile.expense_ratio is not None)
                                            if "expense_ratio" in fields else src_of_profile[0])
        if not out.source:
            out.source = usable[0][0]
        if out.as_of is None:
            out.as_of = next((d.as_of for _, d in usable if d.as_of), None)

        # reason codes
        drop = set()
        if out.holdings:
            drop |= {RC_FUND_HOLDINGS_MISSING} | _ABSENCE_CODES
            if out.source_detail.get("holdings") != "yfinance":
                drop.add(RC_FUND_HOLDINGS_TOP_N_ONLY)
        if out.sector_weights:
            drop.add(RC_FUND_SECTORS_MISSING)
        if out.asset_class_weights:
            drop.add(RC_FUND_ASSET_CLASSES_MISSING)
        if out.profile:
            drop.add(RC_FUND_PROFILE_MISSING)
        merged_codes = [c for c in dict.fromkeys(codes) if c not in drop]
        if stale_used:
            merged_codes.append(RC_FUND_DATA_STALE)
        out.reason_codes = list(dict.fromkeys(merged_codes))
        return out

    # The interface's five getters (callers that want one piece pay for the whole chain).
    def get_profile(self, ticker): return self.fetch(ticker).profile
    def get_holdings(self, ticker): return self.fetch(ticker).holdings
    def get_sector_weights(self, ticker): return self.fetch(ticker).sector_weights
    def get_asset_class_weights(self, ticker): return self.fetch(ticker).asset_class_weights


def build_default_chain(settings: dict | None = None, *, providers: dict[str, FundDataProvider] | None = None) -> ChainFundProvider:
    """The chain as configured in the database (order + limits); `providers` lets tests substitute fakes by name."""
    from portfolio_agent.tools import investor_settings_db as sdb
    ps = settings or sdb.get_portfolio_settings()
    order = sdb.parse_fund_provider_order(ps.get("fund_provider_order_json"))
    if providers is None:
        from portfolio_agent.tools.fund_data_providers import YFinanceFundProvider
        from portfolio_agent.tools.issuer_holdings import IssuerCSVProvider
        from portfolio_agent.tools.sec_nport import NPortProvider
        providers = {"issuer_csv": IssuerCSVProvider(), "nport": NPortProvider(), "yfinance": YFinanceFundProvider()}
    cfg = {k: ps[k] for k in sdb.FUND_SOURCE_SETTINGS_SPEC}
    return ChainFundProvider(order, providers, cfg)
