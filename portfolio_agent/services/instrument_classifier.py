"""
Instrument classifier — decides what KIND of security a ticker is (stock, ETF, mutual fund, money market,
target-date fund, ...), its asset class, leverage and fund details, from yfinance metadata plus name keywords.

Split so the network never meets a database transaction (the rule holdings_prices.py follows):

    fetch_info(ticker)              the ONLY network call — yfinance Ticker.info; None on any failure
    classify_from_info(...)         pure: info dict (or None) + name -> Classification
    classify_instruments(ctx, ...)  fetch for every instrument OUTSIDE a transaction, then write each result
                                    through account_service.apply_auto_classification (audit event per change)
    classify_new_holdings(ctx, ...) the hook used after an import / add_position: best effort, never raises
    ensure_classified(ctx, tickers) the pipeline's entry: any ticker it is about to analyse (portfolio, watchlist,
                                    trending) gets an instrument row and a type; returns {ticker: instrument_type}

Rules
  * yfinance quoteType is primary (EQUITY / ETF / MUTUALFUND / MONEYMARKET); name and category keywords are
    secondary (leveraged / inverse, target-date, money market, ETN, bond).
  * A manual classification (classification_source = 'manual') is never touched; an automatic one is re-checked
    once it is older than domain.RECLASSIFY_AFTER_DAYS.
  * A failed lookup yields UNKNOWN and is logged, with classified_at left empty so the next run retries.
    It never raises into the pipeline or an import.
  * Synthetic ("CIT:...") instruments have nothing to look up and are never classified automatically.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from portfolio_agent.domain import (
    AssetClass, InstrumentType, RECLASSIFY_AFTER_DAYS, is_synthetic_ticker,
)
from portfolio_agent.log import get_logger as _get_logger

_log = _get_logger("instrument_classifier")


@dataclass(frozen=True)
class Classification:
    instrument_type: str = InstrumentType.UNKNOWN.value
    asset_class: str = AssetClass.UNKNOWN.value
    sub_asset_class: str | None = None
    leverage_factor: float = 1.0
    expense_ratio: float | None = None        # a fraction: 0.0003 = 0.03% a year
    fund_family: str | None = None
    category: str | None = None
    lookup_ok: bool = True                    # False: the lookup failed, nothing is known
    basis: str = ""                           # how it was decided (logged, not stored)
    notes: list[str] = field(default_factory=list)


UNKNOWN_CLASSIFICATION = Classification(lookup_ok=False, basis="lookup_failed")

# ── The one network call ──────────────────────────────────────────────────────

def _fetch_info_impl(ticker: str) -> dict | None:
    """yfinance Ticker(ticker).info, or None when the lookup fails or returns nothing useful. Never raises."""
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
    except Exception as exc:
        _log.warning(f"  [classify] {ticker} — lookup failed ({type(exc).__name__}: {str(exc)[:120]})",
                     event_type="warning", ticker=ticker)
        return None
    if not isinstance(info, dict) or not (info.get("quoteType") or info.get("longName") or info.get("shortName")):
        return None
    return info


fetch_info = _fetch_info_impl      # the name callers use; tests replace it so nothing reaches the network


# ── Pure classification ───────────────────────────────────────────────────────

_FUND_QUOTE_TYPES = {"ETF": InstrumentType.ETF, "MUTUALFUND": InstrumentType.MUTUAL_FUND,
                     "MONEYMARKET": InstrumentType.MONEY_MARKET}
_FUNDISH = {InstrumentType.ETF, InstrumentType.ETN, InstrumentType.MUTUAL_FUND}

_MONEY_MARKET_RE = re.compile(r"money\s*(market|mkt)", re.I)
_STABLE_VALUE_RE = re.compile(r"stable\s+(value|income)", re.I)
_TARGET_DATE_RE = re.compile(r"target[\s-]*(date|retirement)|lifepath|lifecycle|\bage[\s-]based\b", re.I)
_TARGET_YEAR_RE = re.compile(r"target.*\b(19|20)\d{2}\b|\b(19|20)\d{2}\b.*target", re.I)
_ETN_RE = re.compile(r"\bETN\b|exchange[\s-]traded\s+note", re.I)
_BOND_RE = re.compile(r"\b\d+(\.\d+)?%\s.*\b(due|notes?|bonds?)\b|treasury\s+(bill|note|bond)s?\b|\bcusip\b", re.I)
_LEV3_RE = re.compile(r"\b3\s?x\b|\bultrapro\b|\btriple\b", re.I)
_LEV2_RE = re.compile(r"\b2\s?x\b|\bdouble\b|\bultrashort\b|\bultra\b(?![\s-]*short\b)", re.I)
# Inverse: "Inverse", "Bear", "UltraShort", "-1x/-2x/-3x", or a bare "Short" — but not "Ultra-Short Bond" /
# "Short-Term Treasury" / "Short Duration", which are ordinary short-maturity bond funds.
_INVERSE_RE = re.compile(r"\binverse\b|\bbear(ish)?\b|\bultrashort\b|(?<![\w.])-[123]\s?x\b"
                         r"|(?<!ultra )(?<!ultra-)\bshort\b(?![\s-]*(term|duration|maturit))", re.I)

_CATEGORY_RULES = (            # (regex on category / name, asset_class, sub_asset_class); first match wins
    (r"money market|cash", AssetClass.CASH, "MONEY_MARKET"),
    (r"high[\s-]*yield|junk", AssetClass.FIXED_INCOME, "HIGH_YIELD"),
    (r"treasur|government|govt|\btips\b|inflation[\s-]*protected", AssetClass.FIXED_INCOME, "TREASURY"),
    (r"muni", AssetClass.FIXED_INCOME, "MUNICIPAL"),
    (r"corporate", AssetClass.FIXED_INCOME, "CORPORATE"),
    (r"\bbond\b|fixed[\s-]*income|\bcore plus\b", AssetClass.FIXED_INCOME, None),
    (r"real estate|reit", AssetClass.REAL_ESTATE, None),
    (r"commodit|precious metals|gold|silver|natural resources|\boil\b|energy limited", AssetClass.COMMODITY, None),
    (r"target[\s-]*(date|retirement)|allocation|balanced|lifecycle|lifepath", AssetClass.MULTI_ASSET, None),
    (r"emerging|diversified emerging", AssetClass.INTL_EQUITY, "EMERGING_MARKETS"),
    (r"foreign|international|world|global|europe|pacific|china|japan|india|latin|ex[\s-]*u\.?s", AssetClass.INTL_EQUITY, None),
    (r"large[\s-]*(cap)?[\s-]*growth", AssetClass.US_EQUITY, "LARGE_CAP_GROWTH"),
    (r"large[\s-]*(cap)?[\s-]*value", AssetClass.US_EQUITY, "LARGE_CAP_VALUE"),
    (r"large[\s-]*(cap)?[\s-]*blend", AssetClass.US_EQUITY, "LARGE_CAP_BLEND"),
    (r"mid[\s-]*(cap)?[\s-]*(growth|value|blend)", AssetClass.US_EQUITY, "MID_CAP"),
    (r"small[\s-]*(cap)?[\s-]*(growth|value|blend)", AssetClass.US_EQUITY, "SMALL_CAP"),
    (r"technology|health|financial|utilities|consumer|industrial|communications|sector", AssetClass.US_EQUITY, "SECTOR"),
)


def _lev_from_name(name: str) -> float:
    """Leverage factor implied by a fund NAME: 2x / 3x / ultra -> 2 / 3; inverse / bear / short -> negative."""
    lev = 3.0 if _LEV3_RE.search(name) else 2.0 if _LEV2_RE.search(name) else 1.0
    return -lev if _INVERSE_RE.search(name) else lev


def _asset_class_for(category: str, name: str, instrument_type: InstrumentType,
                     country: str | None) -> tuple[AssetClass, str | None]:
    if instrument_type == InstrumentType.MONEY_MARKET:
        return AssetClass.CASH, "MONEY_MARKET"
    if instrument_type == InstrumentType.STABLE_VALUE:
        return AssetClass.FIXED_INCOME, "STABLE_VALUE"          # cash-like, but it is a fixed-income wrapper
    if instrument_type == InstrumentType.TARGET_DATE_FUND:
        return AssetClass.MULTI_ASSET, "TARGET_DATE"
    if instrument_type == InstrumentType.BOND:
        return AssetClass.FIXED_INCOME, None
    for text in (category, name if instrument_type in _FUNDISH else ""):
        if text:
            for pat, cls, sub in _CATEGORY_RULES:
                if re.search(pat, text, re.I):
                    return cls, sub
    if instrument_type == InstrumentType.STOCK:
        us = (country or "").strip().lower() in ("united states", "usa", "us", "")
        return (AssetClass.US_EQUITY if us else AssetClass.INTL_EQUITY), None
    return AssetClass.UNKNOWN, None


def _expense_ratio(info: dict) -> float | None:
    v = info.get("annualReportExpenseRatio")
    if isinstance(v, (int, float)) and v > 0:
        return float(v)                                    # already a fraction
    v = info.get("netExpenseRatio")
    if isinstance(v, (int, float)) and v > 0:
        return float(v) / 100.0                            # yfinance reports this one in percent
    return None


def classify_from_info(ticker: str, info: dict | None, name: str | None = None) -> Classification:
    """
    Pure. *info* is yfinance's Ticker.info (None = lookup failed); *name* is the broker's description, used
    when info has no name. A failed lookup (no info) is UNKNOWN with lookup_ok=False: nothing is stored and
    the next run retries.
    """
    if not info:
        return UNKNOWN_CLASSIFICATION
    full_name = " ".join(x for x in (info.get("longName"), info.get("shortName"), name) if x)
    category = (info.get("category") or "").strip()
    quote = str(info.get("quoteType") or "").upper()

    itype: InstrumentType | None = None
    basis = ""
    if quote in ("EQUITY",):
        itype, basis = InstrumentType.STOCK, "quoteType=EQUITY"
    elif quote in _FUND_QUOTE_TYPES:
        itype, basis = _FUND_QUOTE_TYPES[quote], f"quoteType={quote}"
    elif quote in ("INDEX", "CURRENCY", "CRYPTOCURRENCY", "FUTURE", "OPTION"):
        itype, basis = InstrumentType.OTHER, f"quoteType={quote}"

    # Secondary: name / category keywords refine (or, with no quoteType, supply) the type.
    if itype != InstrumentType.MONEY_MARKET and (_MONEY_MARKET_RE.search(full_name) or _MONEY_MARKET_RE.search(category)):
        itype, basis = InstrumentType.MONEY_MARKET, "keyword: money market"
    elif itype != InstrumentType.STABLE_VALUE and (_STABLE_VALUE_RE.search(full_name) or _STABLE_VALUE_RE.search(category)):
        itype, basis = InstrumentType.STABLE_VALUE, "keyword: stable value"
    elif itype in (None, InstrumentType.ETF, InstrumentType.MUTUAL_FUND) and (
            _TARGET_DATE_RE.search(category) or _TARGET_DATE_RE.search(full_name)
            and (_TARGET_YEAR_RE.search(full_name) or re.search(r"target[\s-]*date|retirement", full_name, re.I))):
        itype, basis = InstrumentType.TARGET_DATE_FUND, "keyword: target-date"
    elif itype in (None, InstrumentType.ETF) and _ETN_RE.search(full_name):
        itype, basis = InstrumentType.ETN, "keyword: ETN"
    elif itype in (None, InstrumentType.STOCK, InstrumentType.OTHER) and _BOND_RE.search(full_name) and quote != "EQUITY":
        itype, basis = InstrumentType.BOND, "keyword: bond"

    if itype is None:
        return Classification(basis="no_quote_type", lookup_ok=True)

    leverage = _lev_from_name(full_name) if itype in _FUNDISH else 1.0
    cls, sub = _asset_class_for(category, full_name, itype, info.get("country"))
    return Classification(
        instrument_type=itype.value, asset_class=cls.value, sub_asset_class=sub, leverage_factor=leverage,
        expense_ratio=_expense_ratio(info) if itype in _FUNDISH | {InstrumentType.TARGET_DATE_FUND,
                                                                    InstrumentType.MONEY_MARKET} else None,
        fund_family=(info.get("fundFamily") or None) if itype != InstrumentType.STOCK else None,
        category=category or None, lookup_ok=True, basis=basis,
    )


# ── Orchestration (network first, then audited writes) ────────────────────────

_FAILED_LOOKUP_TTL = 3600.0                      # seconds a failed lookup is not retried within one process
_failed_lookups: dict[str, float] = {}


def _reset_failed_lookups() -> None:
    _failed_lookups.clear()


def classify_instruments(ctx, instruments: list[dict], *, respect_failure_memo: bool = False) -> dict[int, Classification]:
    """
    Look up and store a classification for each instrument row given. Lookups happen first, with no
    transaction open; each result is then written by account_service.apply_auto_classification in its own
    short transaction. A manual classification or a synthetic instrument is skipped. Returns
    {instrument_id: Classification} for every instrument that was looked up. Never raises.
    """
    from portfolio_agent.services import account_service as svc

    import time
    now = time.monotonic()
    todo = [i for i in instruments
            if i.get("classification_source") != "manual" and not is_synthetic_ticker(i.get("ticker"))
            and (not respect_failure_memo or _failed_lookups.get(i["ticker"], 0.0) <= now)]   # pipeline: don't hammer a failure
    def _lookup(inst: dict) -> Classification:             # network only — no DB handle is open here
        try:
            return classify_from_info(inst["ticker"], fetch_info(inst["ticker"]), inst.get("name"))
        except Exception as exc:                           # defensive: classification must never break a caller
            _log.warning(f"  [classify] {inst.get('ticker')} — {type(exc).__name__}: {exc}", event_type="warning")
            return UNKNOWN_CLASSIFICATION

    looked_up: dict[int, Classification] = {}
    if todo:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(4, len(todo))) as pool:
            for inst, c in zip(todo, pool.map(_lookup, todo)):
                looked_up[inst["instrument_id"]] = c
                if not c.lookup_ok:
                    _failed_lookups[inst["ticker"]] = time.monotonic() + _FAILED_LOOKUP_TTL
    for inst in todo:
        c = looked_up[inst["instrument_id"]]
        try:
            svc.apply_auto_classification(ctx, inst["instrument_id"], c)
            if not c.lookup_ok:
                _log.info(f"  [classify] {inst['ticker']} — no data; left unchanged, will retry", event_type="info",
                          ticker=inst["ticker"])
                continue
            _log.info(f"  [classify] {inst['ticker']} → {c.instrument_type}/{c.asset_class}"
                      f"{f' {c.leverage_factor:g}x' if c.leverage_factor != 1.0 else ''} ({c.basis or 'unknown'})",
                      event_type="db_write", ticker=inst["ticker"])
        except Exception as exc:
            _log.warning(f"  [classify] {inst['ticker']} — could not store ({type(exc).__name__}: {exc})",
                         event_type="warning", ticker=inst["ticker"])
    return looked_up


def ensure_classified(ctx, tickers: list[str]) -> dict[str, str]:
    """
    {ticker: instrument_type} for every ticker given that has an instrument row (a synthetic "CIT:..." key is CIT). Tickers with no row yet (watchlist,
    trending, extras) get one, and any whose automatic classification is missing or older than 30 days is looked up —
    lookups first, outside any transaction. Idempotent and cheap once classified. Never raises: on any failure it
    returns whatever is already stored (a ticker with no stored type is absent, i.e. treated as a stock).
    """
    from portfolio_agent.services import account_service as svc
    from portfolio_agent.tools import holdings_db as repo
    synthetic = {t: InstrumentType.CIT.value for t in tickers if t and is_synthetic_ticker(t)}
    try:
        tickers = [t for t in dict.fromkeys(tickers) if t and not is_synthetic_ticker(t)]
        svc.ensure_instruments(ctx, tickers)
        need = repo.list_instruments_needing_classification(tickers=tickers)
        if need:
            classify_instruments(ctx, need, respect_failure_memo=True)
    except Exception as exc:
        _log.warning(f"  [classify] ensure_classified skipped ({type(exc).__name__}: {exc})", event_type="warning")
    try:
        return {**{t: p["instrument_type"] for t, p in repo.get_instrument_profiles(tickers).items()}, **synthetic}
    except Exception:
        return dict(synthetic)


def split_company_filers(ctx, tickers: list[str]) -> tuple[list[str], dict[str, str]]:
    """
    Split *tickers* into (those that file with the SEC, {ticker: type} of those that don't): funds, money market, cash
    and ticker-less funds have no 10-K / 10-Q / 8-K, so EDGAR checks, fundamentals prefetch and 8-K guidance extraction
    skip them cleanly instead of failing on each. Unclassified tickers stay with the filers (the unchanged path).
    """
    from portfolio_agent.domain import NO_COMPANY_FILINGS_TYPES
    types = ensure_classified(ctx, tickers)
    skipped = {t: types[t] for t in tickers if types.get(t) in NO_COMPANY_FILINGS_TYPES}
    return [t for t in tickers if t not in skipped], skipped


def classify_new_holdings(ctx, tickers: list[str] | None = None, *, force: bool = False) -> dict[int, Classification]:
    """
    Hook for instrument creation (import, add_position) and the backfill: classify instruments whose
    automatic classification is missing or older than RECLASSIFY_AFTER_DAYS (all of them with force=True),
    limited to *tickers* when given. Call it AFTER the creating transaction has committed. Never raises.
    """
    from portfolio_agent.tools import holdings_db as repo
    try:
        instruments = repo.list_instruments_needing_classification(
            older_than_days=0 if force else RECLASSIFY_AFTER_DAYS, tickers=tickers)
        return classify_instruments(ctx, instruments) if instruments else {}
    except Exception as exc:
        _log.warning(f"  [classify] skipped ({type(exc).__name__}: {exc})", event_type="warning")
        return {}
