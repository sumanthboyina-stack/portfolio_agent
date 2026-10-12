"""
SEC N-PORT fund holdings — the full, quarterly-public holdings of a registered fund (mutual fund or ETF), straight from EDGAR.

How a ticker becomes holdings (3 requests, all through one throttled client):
  1. ticker -> (CIK, series id, class id): SEC's own table https://www.sec.gov/files/company_tickers_mf.json, kept in the
     sec_fund_ids table and re-downloaded when older than portfolio_settings.sec_fund_ids_refresh_days.
  2. series id -> its newest NPORT-P filing: EDGAR's company browse feed accepts a series id in place of a CIK
     (https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=S000006027&type=NPORT-P&output=atom).
  3. the filing's primary_doc.xml (https://www.sec.gov/Archives/edgar/data/<cik>/<accession, no dashes>/primary_doc.xml).

What the XML gives, and what this module does with it
  * pctVal is the position as a PERCENT of net assets; weights here are FRACTIONS (pctVal / 100).
  * Positions are identified by CUSIP / ISIN, not ticker. They are carried as such (FundHolding.cusip / .isin, symbol None);
    turning them into tickers is the identifier-mapping step, never a guess here.
  * Derivatives (a derivativeInfo block, or an asset category starting "D") and SHORT positions are not holdings: their net weight
    is reported in FundData.derivative_short_weight and they stay out of the holdings list, the coverage figure and the asset mix.
  * holding_is_fund: the filing's issuer category RF (registered fund), or a position whose name says it is a fund. Vanguard's
    funds of funds file their underlying funds as CORP / EC, so the name test is needed; it is deliberately narrow (the word
    "fund" or "ETF"). fund_data_service replaces it with the instrument classifier's verdict whenever it has a symbol.
  * There are no sector weights in N-PORT (reason code FUND_SECTORS_MISSING), and no expense ratio.
  * No filing for the fund -> reason code NPORT_NOT_FILED. Nothing here raises into the pipeline.

Rules
  * SEC fair access: a descriptive User-Agent with a contact (env EDGAR_USER_AGENT, never stored or logged) and at most 10 requests a
    second (portfolio_settings.nport_requests_per_second, capped at 10, enforced by one process-wide throttle).
  * All network I/O happens with no database transaction open; the one table write (sec_fund_ids) is its own short transaction.
  * Tests never touch SEC: they feed recorded XML through a fake session (tests/fixtures/sec).
"""
from __future__ import annotations

import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from portfolio_agent.tools import fund_data_db as fdb
from portfolio_agent.tools.fund_data_providers import (
    RC_FUND_ASSET_CLASSES_MISSING, RC_FUND_FETCH_FAILED, RC_FUND_HOLDINGS_MISSING, RC_FUND_SECTORS_MISSING,
    RC_NPORT_NOT_CONFIGURED, RC_NPORT_NOT_FILED, FundData, FundDataProvider, FundHolding, FundProfile,
)

TICKERS_MF_URL = "https://www.sec.gov/files/company_tickers_mf.json"
BROWSE_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}/primary_doc.xml"
SEC_MAX_REQUESTS_PER_SECOND = 10          # SEC's published fair-access limit; a setting can only lower the rate

_NS = {"n": "http://www.sec.gov/edgar/nport"}
_ATOM = {"a": "http://www.w3.org/2005/Atom"}

# N-PORT asset categories -> our four asset classes (long, non-derivative positions only).
# Derivative asset categories (equity, interest-rate, FX, credit, commodity, other). NOT "anything starting with D": DBT is debt.
_DERIVATIVE_CATS = {"DE", "DIR", "DFE", "DCO", "DCR", "DC", "DO", "DCM"}
_ASSET_CLASS = {"EC": "stock", "DBT": "bond", "LON": "bond", "SN": "bond", "STIV": "cash", "RA": "cash", "EP": "other"}


class SecError(Exception):
    """A SEC request failed (HTTP error, timeout, blocked). Callers turn it into a reason code."""


# ── Throttle + client ─────────────────────────────────────────────────────────

class Throttle:
    """At most `rate` calls a second across every thread: each caller waits for its own slot. The clock and sleep are injectable."""

    def __init__(self, rate: float, *, clock=time.monotonic, sleep=time.sleep):
        self._interval = 1.0 / max(0.1, min(float(rate), SEC_MAX_REQUESTS_PER_SECOND))
        self._clock, self._sleep = clock, sleep
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            start = max(now, self._next)
            self._next = start + self._interval
        if start > now:
            self._sleep(start - now)


_throttles: dict[float, Throttle] = {}
_throttles_lock = threading.Lock()


def shared_throttle(rate: float) -> Throttle:
    """One throttle per rate for the whole process, so parallel fund fetches together stay under SEC's limit."""
    rate = min(float(rate), SEC_MAX_REQUESTS_PER_SECOND)
    with _throttles_lock:
        return _throttles.setdefault(rate, Throttle(rate))


class SecClient:
    """requests-based GETs with the SEC User-Agent and the throttle. `session` is injectable (tests pass a fake)."""

    def __init__(self, *, user_agent: str | None = None, rate: float = 8.0, timeout: float = 30.0, session=None,
                 throttle: Throttle | None = None):
        self._ua = (os.getenv("EDGAR_USER_AGENT", "") if user_agent is None else user_agent).strip()
        self._timeout = timeout
        self._session = session
        self._throttle = throttle or shared_throttle(rate)

    @property
    def configured(self) -> bool:
        return bool(self._ua)

    def get(self, url: str, params: dict | None = None) -> bytes:
        if not self.configured:
            raise SecError("EDGAR_USER_AGENT is not set")
        session = self._session
        if session is None:
            import requests
            session = requests
        self._throttle.wait()
        try:
            r = session.get(url, params=params, timeout=self._timeout,
                            headers={"User-Agent": self._ua, "Accept-Encoding": "gzip, deflate"})
        except Exception as exc:                                   # connection errors / timeouts; the message never carries the UA
            raise SecError(f"request failed: {type(exc).__name__}") from None
        if r.status_code == 404:
            raise SecError("HTTP 404")
        if r.status_code != 200:
            raise SecError(f"HTTP {r.status_code}")
        return r.content


# ── Parsing ───────────────────────────────────────────────────────────────────

def parse_ticker_table(raw: bytes | str) -> list[tuple[int, str, str, str]]:
    """company_tickers_mf.json -> [(cik, series_id, class_id, symbol)]. Raises ValueError if the shape is not what SEC publishes."""
    import json
    j = json.loads(raw)
    fields = j.get("fields") or []
    need = ["cik", "seriesId", "classId", "symbol"]
    if [f for f in need if f not in fields]:
        raise ValueError("company_tickers_mf.json: unexpected fields")
    idx = [fields.index(f) for f in need]
    return [(int(r[idx[0]]), r[idx[1]], r[idx[2]], r[idx[3]]) for r in j.get("data", []) if r[idx[3]]]


@dataclass(frozen=True)
class Filing:
    accession: str            # 0000035402-26-004626
    filing_date: str
    cik_dir: str              # the registrant CIK directory the filing sits under in EDGAR's archive


def parse_filing_feed(raw: bytes | str) -> list[Filing]:
    """EDGAR's atom feed of NPORT-P filings, newest first -> [Filing]. Entries that are not NPORT-P* are dropped."""
    root = ET.fromstring(_safe_xml(raw))
    out: list[Filing] = []
    for e in root.findall("a:entry", _ATOM):
        content = e.find("a:content", _ATOM)
        if content is None:
            continue
        g = lambda tag: (content.findtext(f"a:{tag}", default="", namespaces=_ATOM) or "").strip()
        ftype, acc, href = g("filing-type"), g("accession-number"), g("filing-href")
        m = re.search(r"/data/(\d+)/", href)
        if ftype.upper().startswith("NPORT-P") and acc and m:
            out.append(Filing(acc, g("filing-date"), m.group(1)))
    return out


def _safe_xml(raw: bytes | str) -> bytes:
    """Reject documents that declare entities / DTDs (entity-expansion attacks). N-PORT and EDGAR atom never contain them."""
    b = raw.encode() if isinstance(raw, str) else raw
    head = b[:4096].upper()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in b.upper():
        raise ValueError("XML with a DTD / entity declaration refused")
    return b


@dataclass(frozen=True)
class NPortPosition:
    name: str | None
    title: str | None
    cusip: str | None
    isin: str | None
    ticker: str | None
    pct: float                   # percent of net assets, as filed (can be negative for shorts)
    payoff: str | None
    asset_cat: str | None
    issuer_cat: str | None
    is_derivative: bool

    @property
    def is_short(self) -> bool:
        return (self.payoff or "").strip().lower() == "short" or self.pct < 0


@dataclass
class NPortReport:
    series_id: str | None
    series_name: str | None
    registrant: str | None
    period_date: str | None             # repPdDate: the date the holdings are as of
    net_assets: float | None
    positions: list[NPortPosition] = field(default_factory=list)


def _f(text) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _clean_id(v: str | None) -> str | None:
    v = (v or "").strip()
    return None if v.upper() in ("", "N/A", "NA", "NONE") else v


def parse_nport_xml(raw: bytes | str) -> NPortReport:
    """primary_doc.xml of an NPORT-P filing -> NPortReport. Raises ValueError when it is not a readable N-PORT document."""
    try:
        root = ET.fromstring(_safe_xml(raw))
    except ET.ParseError as exc:
        raise ValueError(f"not XML: {exc}") from None
    gen = root.find("n:formData/n:genInfo", _NS)
    fund = root.find("n:formData/n:fundInfo", _NS)
    secs = root.find("n:formData/n:invstOrSecs", _NS)
    if gen is None or secs is None:
        raise ValueError("not an N-PORT document (no genInfo / invstOrSecs)")
    t = lambda el, path: (el.findtext(path, default="", namespaces=_NS) or "").strip() or None
    rep = NPortReport(series_id=t(gen, "n:seriesId"), series_name=t(gen, "n:seriesName"), registrant=t(gen, "n:regName"),
                      period_date=t(gen, "n:repPdDate"), net_assets=_f(t(fund, "n:netAssets")) if fund is not None else None)
    for s in secs.findall("n:invstOrSec", _NS):
        pct = _f(t(s, "n:pctVal"))
        if pct is None:
            continue
        asset_cat = t(s, "n:assetCat")
        is_deriv = s.find("n:derivativeInfo", _NS) is not None or (asset_cat or "").upper() in _DERIVATIVE_CATS
        ident = s.find("n:identifiers", _NS)
        isin = ident.find("n:isin", _NS).get("value") if ident is not None and ident.find("n:isin", _NS) is not None else None
        tick = ident.find("n:ticker", _NS).get("value") if ident is not None and ident.find("n:ticker", _NS) is not None else None
        rep.positions.append(NPortPosition(
            name=t(s, "n:name"), title=t(s, "n:title"), cusip=_clean_id(t(s, "n:cusip")), isin=_clean_id(isin),
            ticker=_clean_id(tick), pct=pct, payoff=t(s, "n:payoffProfile"), asset_cat=asset_cat,
            issuer_cat=t(s, "n:issuerCat"), is_derivative=is_deriv))
    return rep


_FUND_NAME = re.compile(r"\b(fund|etf)\b", re.I)


def looks_like_fund(p: NPortPosition) -> bool:
    """Registered-fund issuer category, or (for equity-class positions) a name that says fund / ETF. Best effort; see module docstring."""
    if (p.issuer_cat or "").upper() == "RF":
        return True
    if (p.asset_cat or "").upper() not in ("EC", "STIV"):
        return False
    return bool(_FUND_NAME.search(p.name or "") or _FUND_NAME.search(p.title or ""))


def report_to_fund_data(ticker: str, rep: NPortReport) -> FundData:
    """The FundData the rest of the app reads. Long, non-derivative positions are the holdings (weights are fractions); derivatives and
    shorts are only summed into derivative_short_weight."""
    data = FundData(ticker=ticker.upper(), source="nport", as_of=rep.period_date)
    deriv_short = 0.0
    classes: dict[str, float] = {}
    holdings: list[FundHolding] = []
    for p in rep.positions:
        w = p.pct / 100.0
        if p.is_derivative or p.is_short:
            deriv_short += w
            continue
        if w <= 0:
            continue
        holdings.append(FundHolding(symbol=None, name=p.name or p.title, weight=round(w, 12), cusip=p.cusip,
                                    holding_is_fund=looks_like_fund(p), isin=p.isin))
        cat = (p.asset_cat or "").upper()
        cls = "bond" if cat.startswith("ABS") else _ASSET_CLASS.get(cat, "other")
        classes[cls] = classes.get(cls, 0.0) + w
    holdings.sort(key=lambda h: -h.weight)
    data.holdings = holdings
    data.holdings_coverage = round(min(1.0, sum(h.weight for h in holdings)), 12)
    data.derivative_short_weight = round(deriv_short, 12)
    data.asset_class_weights = {k: round(v, 12) for k, v in classes.items() if v > 0}
    data.profile = FundProfile(fund_family=rep.registrant, aum=rep.net_assets)
    data.reason_codes = [RC_FUND_SECTORS_MISSING]
    if not holdings:
        data.reason_codes.append(RC_FUND_HOLDINGS_MISSING)
    if not data.asset_class_weights:
        data.reason_codes.append(RC_FUND_ASSET_CLASSES_MISSING)
    return data


# ── Provider ──────────────────────────────────────────────────────────────────

class NPortProvider(FundDataProvider):
    """Full holdings from SEC N-PORT. Never raises: every failure is a reason code on the returned FundData."""
    name = "nport"

    def __init__(self, *, client: SecClient | None = None, settings: dict | None = None, now=None):
        self._client_override = client
        self._settings = settings
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._reports: dict[str, NPortReport] = {}            # accession -> parsed report: share classes of one fund fetch it once

    # settings come from portfolio_settings (DB); the seed defaults are used only if the table cannot be read
    def _cfg(self) -> dict:
        if self._settings is not None:
            return self._settings
        from portfolio_agent.tools import investor_settings_db as sdb
        try:
            ps = sdb.get_portfolio_settings()
            return {k: ps[k] for k in sdb.FUND_SOURCE_SETTINGS_SPEC}
        except Exception:
            return dict(sdb.FUND_SOURCE_SETTINGS_DEFAULTS)

    def _client(self, cfg: dict) -> SecClient:
        return self._client_override or SecClient(rate=float(cfg["nport_requests_per_second"]),
                                                  timeout=float(cfg["nport_http_timeout_seconds"]))

    def _series_for(self, ticker: str, client: SecClient, cfg: dict) -> dict | None:
        """The sec_fund_ids row for a ticker; downloads SEC's table first when it has never been fetched or is older than the refresh
        interval. The download happens outside any transaction; storing it is one short transaction."""
        fetched = fdb.sec_fund_ids_fetched_at()
        stale = fetched is None
        if fetched is not None:
            try:
                stale = self._now() - datetime.fromisoformat(fetched) >= timedelta(days=float(cfg["sec_fund_ids_refresh_days"]))
            except ValueError:
                stale = True
        if stale:
            try:
                rows = parse_ticker_table(client.get(TICKERS_MF_URL))
                fdb.replace_sec_fund_ids(rows, fetched_at=self._now().isoformat(timespec="seconds"))
            except (SecError, ValueError):
                if fetched is None:
                    raise                                     # nothing to fall back on
        return fdb.get_sec_fund_id(ticker)

    def fetch(self, ticker: str) -> FundData:
        ticker = ticker.upper()
        cfg = self._cfg()
        client = self._client(cfg)
        if not client.configured:
            return FundData(ticker=ticker, source=self.name, reason_codes=[RC_NPORT_NOT_CONFIGURED])
        try:
            ids = self._series_for(ticker, client, cfg)
            if ids is None:
                return FundData(ticker=ticker, source=self.name, reason_codes=[RC_NPORT_NOT_FILED])
            filings = parse_filing_feed(client.get(BROWSE_URL, params={
                "action": "getcompany", "CIK": ids["series_id"], "type": "NPORT-P", "dateb": "", "owner": "include",
                "count": "5", "output": "atom"}))
            if not filings:
                return FundData(ticker=ticker, source=self.name, reason_codes=[RC_NPORT_NOT_FILED])
            latest = filings[0]
            rep = self._reports.get(latest.accession)
            if rep is None:
                rep = parse_nport_xml(client.get(ARCHIVES_URL.format(
                    cik=latest.cik_dir, accession_nodash=latest.accession.replace("-", ""))))
                self._reports[latest.accession] = rep
            if rep.series_id and rep.series_id != ids["series_id"]:
                return FundData(ticker=ticker, source=self.name, reason_codes=[RC_FUND_FETCH_FAILED])
            return report_to_fund_data(ticker, rep)
        except (SecError, ValueError):
            return FundData(ticker=ticker, source=self.name, reason_codes=[RC_FUND_FETCH_FAILED])
        except Exception:
            return FundData(ticker=ticker, source=self.name, reason_codes=[RC_FUND_FETCH_FAILED])

    # The five getters, for callers that want one piece.
    def get_profile(self, ticker):
        return self.fetch(ticker).profile

    def get_holdings(self, ticker):
        return self.fetch(ticker).holdings

    def get_sector_weights(self, ticker):
        return {}

    def get_asset_class_weights(self, ticker):
        return self.fetch(ticker).asset_class_weights
