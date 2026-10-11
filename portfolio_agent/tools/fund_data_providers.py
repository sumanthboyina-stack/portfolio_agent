"""
Fund data providers — where a fund's holdings, sector mix, asset-class mix and cost come from.

FundDataProvider is the interface; today's implementation is YFinanceFundProvider (ETFs and mutual funds, through
yfinance's funds_data and info). IssuerCSVProvider and NPortProvider are STUBS for later (an issuer's published holdings
file; an SEC N-PORT filing) — they exist so the interface is real, and they return an explicit "not implemented" reason
code rather than pretending to have data.

Rules
  * A provider NEVER raises into the pipeline: any failure becomes a reason code on the returned FundData.
  * Fetching is network I/O: callers fetch OUTSIDE any database transaction (services.fund_data_service does).
  * yfinance returns only a fund's TOP-10 holdings. holdings_coverage is the sum of their weights, so a caller always
    knows how much of the fund the issuer list actually explains; the rest is reported, never guessed.
  * Weights are FRACTIONS (0.07 = 7%). Expense ratios are fractions too (0.0003 = 0.03% a year).
  * Nothing here knows about any person's portfolio: this is shared market data.
"""
from __future__ import annotations

import math
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

# ── Reason codes (a missing piece is a code, never a silent default) ──────────
RC_FUND_FETCH_FAILED = "FUND_FETCH_FAILED"
RC_FUND_PROFILE_MISSING = "FUND_PROFILE_MISSING"
RC_FUND_HOLDINGS_MISSING = "FUND_HOLDINGS_MISSING"
RC_FUND_SECTORS_MISSING = "FUND_SECTORS_MISSING"
RC_FUND_ASSET_CLASSES_MISSING = "FUND_ASSET_CLASSES_MISSING"
RC_FUND_HOLDINGS_TOP_N_ONLY = "FUND_HOLDINGS_TOP_N_ONLY"          # provider only exposes the largest holdings
RC_PROVIDER_NOT_IMPLEMENTED = "FUND_PROVIDER_NOT_IMPLEMENTED"


@dataclass(frozen=True)
class FundHolding:
    symbol: str | None
    name: str | None
    weight: float                       # fraction of the fund
    cusip: str | None = None
    holding_is_fund: bool = False       # set by fund_data_service once the holding's own instrument type is known


@dataclass(frozen=True)
class FundProfile:
    expense_ratio: float | None = None  # fraction a year, for the EXACT share class of the ticker asked about
    fund_family: str | None = None
    category: str | None = None
    aum: float | None = None            # total net assets, dollars
    share_class: str | None = None      # e.g. ADMIRAL / INVESTOR / INSTITUTIONAL / CLASS A
    share_class_group: str | None = None  # same fund, different class — see derive_share_class_group


@dataclass
class FundData:
    ticker: str
    profile: FundProfile | None = None
    holdings: list[FundHolding] = field(default_factory=list)
    sector_weights: dict[str, float] = field(default_factory=dict)        # fractions, sector name -> weight
    asset_class_weights: dict[str, float] = field(default_factory=dict)   # fractions: stock / bond / cash / other
    country_weights: dict[str, float] | None = None                       # optional
    holdings_coverage: float = 0.0                                        # sum of holding weights, 0-1
    as_of: str | None = None
    source: str = ""
    reason_codes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Something usable came back (at least a profile or holdings or a mix)."""
        return bool(self.profile or self.holdings or self.sector_weights or self.asset_class_weights)


# ── Share class ───────────────────────────────────────────────────────────────

_SHARE_CLASS_TOKENS = (
    ("ADMIRAL", r"\badmiral\b"), ("INVESTOR", r"\binvestor\b"), ("INSTITUTIONAL PLUS", r"\binstitutional plus\b"),
    ("INSTITUTIONAL", r"\binstitutional\b|\binst\b"), ("ETF SHARES", r"\betf shares\b"), ("RETAIL", r"\bretail\b"),
    ("PREMIER", r"\bpremier\b"), ("SIGNATURE", r"\bsignature\b"), ("SELECT", r"\bselect\b"), ("ADVISOR", r"\badvisor\b"),
    ("SERVICE", r"\bservice\b"),
    ("CLASS", r"\bclass\s+[a-z0-9]{1,3}\b"), ("CLASS", r"\b[a-z0-9]{1,3}\s+shares\b"),
)


def derive_share_class(name: str | None) -> str | None:
    """The share class named in a fund's name ("Vanguard 500 Index Fund Admiral Shares" -> ADMIRAL), else None."""
    text = (name or "").lower()
    for label, pat in _SHARE_CLASS_TOKENS:
        m = re.search(pat, text)
        if m:
            return label if label != "CLASS" else re.sub(r"\s+", " ", m.group(0).upper()).replace(" SHARES", "")
    return None


def derive_share_class_group(name: str | None, family: str | None) -> str | None:
    """
    A deterministic key shared by the share classes of ONE fund: the fund's name with share-class words and
    punctuation removed, plus its fund family. yfinance gives no series id, so this is a best-effort name match —
    callers must treat "same group" as "very likely the same fund", and the fee note derived from it as an estimate.
    """
    if not name:
        return None
    text = name.lower()
    for _, pat in _SHARE_CLASS_TOKENS:
        text = re.sub(pat, " ", text)
    text = re.sub(r"\b(shares|share|fund|trust|inc|etf)\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    if not text:
        return None
    return f"{(family or '').strip().lower()}|{text}"


# ── Interface ─────────────────────────────────────────────────────────────────

class FundDataProvider(ABC):
    """Subclasses implement the five getters (each returns empty/None when it has nothing) or override fetch()."""
    name = "provider"

    @abstractmethod
    def get_profile(self, ticker: str) -> FundProfile | None: ...

    @abstractmethod
    def get_holdings(self, ticker: str) -> list[FundHolding]: ...

    @abstractmethod
    def get_sector_weights(self, ticker: str) -> dict[str, float]: ...

    @abstractmethod
    def get_asset_class_weights(self, ticker: str) -> dict[str, float]: ...

    def get_country_weights(self, ticker: str) -> dict[str, float] | None:
        return None                                                    # optional

    def fetch(self, ticker: str) -> FundData:
        """Everything about *ticker*, never raising: a failing getter becomes a reason code."""
        data = FundData(ticker=ticker.upper(), source=self.name)
        for label, getter, code in (
            ("profile", self.get_profile, RC_FUND_PROFILE_MISSING),
            ("holdings", self.get_holdings, RC_FUND_HOLDINGS_MISSING),
            ("sectors", self.get_sector_weights, RC_FUND_SECTORS_MISSING),
            ("asset_classes", self.get_asset_class_weights, RC_FUND_ASSET_CLASSES_MISSING),
        ):
            try:
                value = getter(ticker)
            except Exception:
                data.reason_codes.append(RC_FUND_FETCH_FAILED)
                continue
            if not value:
                data.reason_codes.append(code)
                continue
            if label == "profile":
                data.profile = value
            elif label == "holdings":
                data.holdings = list(value)
            elif label == "sectors":
                data.sector_weights = dict(value)
            else:
                data.asset_class_weights = dict(value)
        try:
            data.country_weights = self.get_country_weights(ticker)
        except Exception:
            data.country_weights = None
        data.holdings_coverage = round(min(1.0, sum(h.weight for h in data.holdings)), 6)
        data.reason_codes = list(dict.fromkeys(data.reason_codes))
        return data


# ── Pure parsing of yfinance's shapes (tested with canned data) ───────────────

_SECTOR_NAMES = {          # yfinance funds_data sector keys -> the sector names the rest of the app uses
    "technology": "Technology", "healthcare": "Healthcare", "financial_services": "Financial Services",
    "consumer_cyclical": "Consumer Cyclical", "communication_services": "Communication Services",
    "consumer_defensive": "Consumer Defensive", "basic_materials": "Basic Materials", "energy": "Energy",
    "industrials": "Industrials", "utilities": "Utilities", "realestate": "Real Estate", "real_estate": "Real Estate",
}


def _fnum(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def parse_top_holdings(df) -> list[FundHolding]:
    """funds_data.top_holdings: a DataFrame indexed by Symbol with 'Name' and 'Holding Percent' (a fraction)."""
    if df is None or getattr(df, "empty", True):
        return []
    try:
        frame = df.reset_index()
    except Exception:
        return []
    cols = {str(c).strip().lower(): c for c in frame.columns}
    sym_col = cols.get("symbol") or cols.get("index") or frame.columns[0]
    name_col = cols.get("name")
    w_col = cols.get("holding percent") or cols.get("holdingpercent") or cols.get("weight")
    if w_col is None:
        return []
    rows = []
    for _, r in frame.iterrows():
        w = _fnum(r[w_col])
        if w is None or w <= 0:
            continue
        sym = str(r[sym_col]).strip().upper() if r[sym_col] is not None and str(r[sym_col]).strip() not in ("", "nan") else None
        nm = str(r[name_col]).strip() if name_col is not None and r[name_col] is not None else None
        rows.append((sym, nm, w))
    if rows and sum(w for *_, w in rows) > 1.5:                       # delivered as percents, not fractions
        rows = [(s, n, w / 100.0) for s, n, w in rows]
    return [FundHolding(symbol=s, name=n, weight=round(w, 6)) for s, n, w in rows]


def parse_sector_weights(d) -> dict[str, float]:
    if not isinstance(d, dict):
        return {}
    out: dict[str, float] = {}
    for k, v in d.items():
        w = _fnum(v)
        if w is None or w <= 0:
            continue
        name = _SECTOR_NAMES.get(str(k).strip().lower())
        if name:
            out[name] = round(out.get(name, 0.0) + w, 6)
    if out and sum(out.values()) > 1.5:
        out = {k: round(v / 100.0, 6) for k, v in out.items()}
    return out


def parse_asset_classes(d) -> dict[str, float]:
    """funds_data.asset_classes (cashPosition, stockPosition, bondPosition, preferred, convertible, other) -> stock/bond/cash/other."""
    if not isinstance(d, dict):
        return {}
    pick = lambda *keys: sum(x for x in (_fnum(d.get(k)) for k in keys) if x)
    out = {"stock": pick("stockPosition"), "bond": pick("bondPosition"), "cash": pick("cashPosition"),
           "other": pick("preferredPosition", "convertiblePosition", "otherPosition")}
    if sum(out.values()) > 1.5:
        out = {k: v / 100.0 for k, v in out.items()}
    out = {k: round(v, 6) for k, v in out.items() if v > 0}
    return out


def _expense_from(info: dict, operations) -> float | None:
    v = _fnum((info or {}).get("netExpenseRatio"))
    if v is not None and v > 0:
        return round(v / 100.0, 8)                                    # yfinance reports this one in percent
    v = _fnum((info or {}).get("annualReportExpenseRatio"))
    if v is not None and v > 0:
        return round(v, 8)                                            # already a fraction
    try:
        if operations is not None and not operations.empty:
            for label in operations.index:
                if "expense ratio" in str(label).lower():
                    row = operations.loc[label]
                    first = _fnum(row.iloc[0] if hasattr(row, "iloc") else row)
                    if first is not None and first > 0:
                        return round(first / 100.0 if first > 0.1 else first, 8)
    except Exception:
        pass
    return None


def parse_profile(name: str | None, info: dict, overview: dict | None, operations) -> FundProfile | None:
    info = info or {}
    overview = overview or {}
    family = overview.get("family") or info.get("fundFamily")
    category = overview.get("categoryName") or info.get("category")
    expense = _expense_from(info, operations)
    aum = _fnum(info.get("totalAssets"))
    if not any((family, category, expense, aum)):
        return None
    return FundProfile(expense_ratio=expense, fund_family=family, category=category, aum=aum,
                       share_class=derive_share_class(name), share_class_group=derive_share_class_group(name, family))


class YFinanceFundProvider(FundDataProvider):
    """ETFs and mutual funds through yfinance (`Ticker.funds_data` + `Ticker.info`). Top-10 holdings only."""
    name = "yfinance"

    def __init__(self, ticker_factory=None):
        self._factory = ticker_factory                  # tests inject a fake instead of importing yfinance

    def _ticker(self, symbol: str):
        if self._factory is not None:
            return self._factory(symbol)
        import yfinance as yf
        return yf.Ticker(symbol)

    # One network round-trip per ticker: fetch() reads info + funds_data once and parses every piece from them.
    def fetch(self, ticker: str) -> FundData:
        data = FundData(ticker=ticker.upper(), source=self.name)
        try:
            t = self._ticker(ticker)
            info = t.info if isinstance(t.info, dict) else {}
        except Exception:
            data.reason_codes.append(RC_FUND_FETCH_FAILED)
            return data
        try:
            fd = t.funds_data
        except Exception:
            fd = None
            data.reason_codes.append(RC_FUND_FETCH_FAILED)

        def part(attr):
            try:
                return getattr(fd, attr) if fd is not None else None
            except Exception:
                return None

        name = info.get("longName") or info.get("shortName")
        data.profile = parse_profile(name, info, part("fund_overview"), part("fund_operations"))
        data.holdings = parse_top_holdings(part("top_holdings"))
        data.sector_weights = parse_sector_weights(part("sector_weightings"))
        data.asset_class_weights = parse_asset_classes(part("asset_classes"))
        data.holdings_coverage = round(min(1.0, sum(h.weight for h in data.holdings)), 6)
        for present, code in ((data.profile, RC_FUND_PROFILE_MISSING), (data.holdings, RC_FUND_HOLDINGS_MISSING),
                              (data.sector_weights, RC_FUND_SECTORS_MISSING),
                              (data.asset_class_weights, RC_FUND_ASSET_CLASSES_MISSING)):
            if not present:
                data.reason_codes.append(code)
        if data.holdings:
            data.reason_codes.append(RC_FUND_HOLDINGS_TOP_N_ONLY)
        data.reason_codes = list(dict.fromkeys(data.reason_codes))
        return data

    # The five getters, for callers that want one piece (they cost a round-trip each).
    def get_profile(self, ticker):
        return self.fetch(ticker).profile

    def get_holdings(self, ticker):
        return self.fetch(ticker).holdings

    def get_sector_weights(self, ticker):
        return self.fetch(ticker).sector_weights

    def get_asset_class_weights(self, ticker):
        return self.fetch(ticker).asset_class_weights


# ── Stubs (TODO) ──────────────────────────────────────────────────────────────

class _NotImplementedProvider(FundDataProvider):
    def get_profile(self, ticker): return None
    def get_holdings(self, ticker): return []
    def get_sector_weights(self, ticker): return {}
    def get_asset_class_weights(self, ticker): return {}

    def fetch(self, ticker: str) -> FundData:
        return FundData(ticker=ticker.upper(), source=self.name, reason_codes=[RC_PROVIDER_NOT_IMPLEMENTED])


class IssuerCSVProvider(_NotImplementedProvider):
    """TODO: an issuer's published holdings file (full holdings, not just the top 10). Not built yet."""
    name = "issuer_csv"


class NPortProvider(_NotImplementedProvider):
    """TODO: SEC Form N-PORT filings (full quarterly holdings for registered funds). Not built yet."""
    name = "nport"


def default_provider() -> FundDataProvider:
    return YFinanceFundProvider()
