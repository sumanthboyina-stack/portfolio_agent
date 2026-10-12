"""
Fund data service — keeps the shared fund tables fresh, takes a person's typed-in breakdown for a fund the providers
know nothing about, and assembles the FundViews the look-through engine reads.

  refresh_fund_data(ctx, tickers)       SHARED market data. Fetch → (outside any transaction, 4 at a time) → save each
                                        fund in its own short transaction. TTL-guarded (ETFs and mutual funds have
                                        different TTLs, both global settings). Opens fund-of-funds down to
                                        lookthrough_max_depth. Never raises: a failure is a status + reason code.
  set_manual_breakdown / clear_...      PRIVATE, owner-scoped (the owner is ctx.actor; the fund must be one the owner
                                        holds). One transaction with its audit event.
  load_fund_views(owner, tickers)       What is known about each fund FOR THIS OWNER, by priority
                                          1. the fund's own provider data
                                          2. the owner's manual breakdown
                                          3. the data of the fund's proxy_ticker, labeled "via <PROXY>" (analytics only —
                                             a proxy is never a holding)
                                          4. nothing: source "none" → UNKNOWN downstream, never a guess.

Network calls (provider fetches, classification lookups) all happen BEFORE any transaction opens.
"""
from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from portfolio_agent.domain import FUND_INSTRUMENT_TYPES, InstrumentType, is_synthetic_ticker
from portfolio_agent.services.context import RequestContext, require_context
from portfolio_agent.tools import fund_data_db as fdb
from portfolio_agent.tools import fund_manual_db as mdb
from portfolio_agent.tools import holdings_db as repo
from portfolio_agent.tools import identifier_mapping as idmap
from portfolio_agent.tools import investor_settings_db as sdb
from portfolio_agent.tools.fund_data_providers import RC_HOLDINGS_KEPT_PREVIOUS, FundData, FundDataProvider, default_provider
from portfolio_agent.tools.lookthrough import (
    SRC_MANUAL, SRC_NONE, SRC_OWN, SRC_PROXY, FundView, ViewHolding,
)

_FUND_TYPES = {t.value for t in FUND_INSTRUMENT_TYPES}
_FUND_OF_FUNDS_TYPES = {InstrumentType.MUTUAL_FUND.value, InstrumentType.TARGET_DATE_FUND.value}
_ETF_TYPES = {InstrumentType.ETF.value, InstrumentType.ETN.value}
_NO_DATA_TYPES = {InstrumentType.CIT.value, InstrumentType.STABLE_VALUE.value}      # a ticker (if any) is only a proxy

ASSET_CLASS_KEYS = ("stock", "bond", "cash", "other")
RC_MANUAL_SUM = "MANUAL_BREAKDOWN_SUM"


class NotYourFund(PermissionError):
    """The fund is not one the actor holds."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Refresh (shared market data) ──────────────────────────────────────────────

def _is_stale(row: dict | None, ttl_hours: float, now: datetime) -> bool:
    if row is None:
        return True
    try:
        fetched = datetime.fromisoformat(row["fetched_at"])
    except Exception:
        return True
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    return now - fetched >= timedelta(hours=ttl_hours)


def refresh_fund_data(ctx: RequestContext, tickers: list[str], *, provider: FundDataProvider | None = None,
                      force: bool = False, workers: int = 4, now: datetime | None = None) -> dict[str, str]:
    """
    {ticker: status} where status is fetched | fresh | no_data | skipped:<type> | via_proxy:<PROXY>. Never raises.
    """
    require_context(ctx)
    now = now or _now()
    provider = provider or default_provider()
    status: dict[str, str] = {}
    try:
        settings = sdb.get_portfolio_settings()
        ttl_etf, ttl_mf = float(settings["etf_data_ttl_hours"]), float(settings["mutual_fund_data_ttl_hours"])
        max_depth = int(settings["lookthrough_max_depth"])
    except Exception:
        return {t: "no_settings" for t in tickers}

    from portfolio_agent.services.instrument_classifier import ensure_classified
    frontier = [t.upper() for t in dict.fromkeys(tickers) if t]
    seen: set[str] = set()
    fetched_summary: dict[str, dict] = {}
    for depth in range(1, max_depth + 1):
        frontier = [t for t in frontier if t not in seen]
        if not frontier:
            break
        seen.update(frontier)
        real = [t for t in frontier if not is_synthetic_ticker(t)]
        types = ensure_classified(ctx, real)                                  # lookups first, outside any transaction
        todo: list[str] = []
        proxies: list[str] = []
        for t in frontier:
            inst = repo.find_instrument(t)
            itype = (inst or {}).get("instrument_type") or types.get(t)
            if inst is None:
                status[t] = "skipped:unclassified"
            elif itype in _NO_DATA_TYPES or (itype not in _FUND_TYPES and inst.get("proxy_ticker")):
                proxy = (inst.get("proxy_ticker") or "").strip().upper()
                if proxy:
                    status[t] = f"via_proxy:{proxy}"
                    proxies.append(proxy)
                else:
                    status[t] = f"skipped:{itype}"
            elif itype not in _FUND_TYPES:
                status[t] = f"skipped:{itype}"
            else:
                ttl = ttl_etf if itype in _ETF_TYPES else ttl_mf
                row = fdb.get_fund_profile_row(inst["instrument_id"])
                if not force and not _is_stale(row, ttl, now):
                    status[t] = "fresh"
                    todo_children = [h for h in (fdb.get_fund_data(inst["instrument_id"], t) or FundData(t)).holdings if h.holding_is_fund]
                    proxies.extend((h.symbol or "").upper() for h in todo_children if h.symbol)
                else:
                    todo.append(t)
        results: dict[str, FundData] = {}
        if todo:
            def _one(t: str) -> FundData:
                try:
                    return provider.fetch(t)
                except Exception:
                    from portfolio_agent.tools.fund_data_providers import RC_FUND_FETCH_FAILED
                    return FundData(ticker=t, source=getattr(provider, "name", "provider"), reason_codes=[RC_FUND_FETCH_FAILED])
            with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                for t, data in zip(todo, pool.map(_one, todo)):
                    results[t] = data
        children: list[str] = []
        for t, data in results.items():
            if not data.ok:
                status[t] = "no_data"
                continue
            if data.holdings:
                try:                                                          # CUSIP / ISIN -> ticker: network, no transaction open
                    idmap.map_fund_holdings(data, settings)
                except Exception:
                    pass                                                      # unmapped holdings stay keyed by CUSIP; never fatal
            itype = (repo.find_instrument(t) or {}).get("instrument_type")
            syms = [h.symbol.upper() for h in data.holdings if h.symbol]
            if itype in _FUND_OF_FUNDS_TYPES and syms and depth < max_depth:
                htypes = ensure_classified(ctx, syms)                         # lookups outside transactions
                data.holdings = [replace(h, holding_is_fund=htypes.get((h.symbol or "").upper()) in _FUND_TYPES)
                                 for h in data.holdings]
                children.extend(h.symbol.upper() for h in data.holdings if h.holding_is_fund and h.symbol)
            try:
                inst = repo.find_instrument(t)
                _keep_previous_holdings(inst["instrument_id"], t, data)
                fdb.save_fund_data(inst["instrument_id"], data, fetched_at=now.isoformat(timespec="seconds"))
                status[t] = "fetched"
                fetched_summary[t] = {"holdings": len(data.holdings), "coverage": data.holdings_coverage,
                                      "mapped_coverage": data.mapped_coverage,
                                      "reason_codes": data.reason_codes}
            except Exception:
                status[t] = "no_data"
        frontier = [*proxies, *children]

    if fetched_summary:
        try:
            with repo.unit_of_work() as c:
                portfolio = repo.get_portfolio_by_owner(ctx.actor, conn=c)
                repo.record_audit_event(actor=ctx.actor, source=ctx.source, request_id=ctx.request_id,
                                        operation="refresh_fund_data", portfolio_id=portfolio["portfolio_id"] if portfolio else None,
                                        after={"funds": fetched_summary}, conn=c)
        except Exception:
            pass                                                              # the data is saved; the audit row is best-effort here
    return status


def _keep_previous_holdings(instrument_id: int, ticker: str, data: FundData) -> None:
    """A refresh that came back WITHOUT holdings (a source down, a filing not found) must not wipe the holdings already stored: the previous
    list, its coverage, as-of date and source stay, marked FUND_HOLDINGS_KEPT_PREVIOUS. The write that follows is one transaction either way."""
    if data.holdings:
        return
    prev = fdb.get_fund_data(instrument_id, ticker)
    if prev is None or not prev.holdings:
        return
    data.holdings, data.holdings_coverage, data.mapped_coverage = list(prev.holdings), prev.holdings_coverage, prev.mapped_coverage
    data.derivative_short_weight, data.as_of = prev.derivative_short_weight, prev.as_of
    data.source = prev.source
    data.source_detail = {**data.source_detail, "holdings": prev.source_detail.get("holdings") or prev.source}
    data.reason_codes = list(dict.fromkeys([*data.reason_codes, RC_HOLDINGS_KEPT_PREVIOUS]))


def held_fund_tickers() -> list[str]:
    """Every fund ticker held in ANY portfolio (the shared fund tables serve everyone), real market symbols only."""
    out = []
    for t in repo.get_portfolio_tickers():
        inst = repo.find_instrument(t)
        if inst is not None and inst.get("instrument_type") in _FUND_TYPES:
            out.append(t)
    return out


def refresh_all_fund_data(ctx: RequestContext, *, force: bool = False, provider: FundDataProvider | None = None) -> dict:
    """Refresh every held fund through the provider chain (TTL-guarded unless force). Used by the evening batch and the admin "refresh now".
    Returns {"funds": n, "status": {ticker: status}, "counts": {status: n}}; never raises."""
    tickers = held_fund_tickers()
    status = refresh_fund_data(ctx, tickers, provider=provider, force=force) if tickers else {}
    counts: dict[str, int] = {}
    for st in status.values():
        k = st.split(":")[0]
        counts[k] = counts.get(k, 0) + 1
    return {"funds": len(tickers), "status": status, "counts": counts}


# ── Manual breakdown (private) ────────────────────────────────────────────────

def _validate_pct_map(name: str, m: dict, *, allowed: tuple[str, ...] | None) -> dict[str, float]:
    if not isinstance(m, dict) or not m:
        raise ValueError(f"{name} must be a non-empty mapping of label -> percent")
    out: dict[str, float] = {}
    for k, v in m.items():
        key = str(k).strip()
        if allowed is not None and key not in allowed:
            raise ValueError(f"{name}: unknown key {key!r} (allowed: {', '.join(allowed)})")
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{name}[{key}] must be a number") from None
        if not math.isfinite(f) or not 0 <= f <= 100:
            raise ValueError(f"{name}[{key}] must be between 0 and 100")
        out[key] = f
    total = sum(out.values())
    if abs(total - 100.0) > 1.0:
        raise ValueError(f"{name} must add up to 100% (it adds up to {total:.2f}%)")
    return out


def _owned_instrument(ctx: RequestContext, ticker: str, conn) -> dict:
    inst = repo.find_instrument(ticker, conn=conn)
    portfolio = repo.get_portfolio_by_owner(ctx.actor, conn=conn)
    if inst is None or portfolio is None or not repo.portfolio_holds_instrument(portfolio["portfolio_id"], inst["instrument_id"], conn=conn):
        raise NotYourFund(f"{ticker!r} is not a holding of {ctx.actor!r}")
    return inst


def set_manual_breakdown(ctx: RequestContext, ticker: str, asset_class_pct: dict, sector_pct: dict | None = None,
                         as_of: str | None = None) -> dict:
    """Record the actor's own asset-class (and optional sector) mix for a fund they hold, from a plan fact sheet."""
    require_context(ctx)
    asset_class = _validate_pct_map("asset_class", asset_class_pct, allowed=ASSET_CLASS_KEYS)
    sector = _validate_pct_map("sector", sector_pct, allowed=None) if sector_pct else None
    fdb._ensure_once()
    mdb._ensure_once()
    with repo.unit_of_work() as c:
        inst = _owned_instrument(ctx, ticker, c)
        before = mdb.get_manual_breakdown(ctx.actor, inst["instrument_id"], conn=c)
        mdb.replace_manual_breakdown(ctx.actor, inst["instrument_id"], asset_class=asset_class, sector=sector, as_of=as_of, conn=c)
        portfolio = repo.get_portfolio_by_owner(ctx.actor, conn=c)
        repo.record_audit_event(actor=ctx.actor, source=ctx.source, request_id=ctx.request_id, operation="set_fund_manual_breakdown",
                                portfolio_id=portfolio["portfolio_id"], before=before,
                                after={"ticker": inst["ticker"], "asset_class": asset_class, "sector": sector, "as_of": as_of}, conn=c)
    return {"ticker": inst["ticker"], "asset_class": asset_class, "sector": sector, "as_of": as_of}


def clear_manual_breakdown(ctx: RequestContext, ticker: str) -> bool:
    require_context(ctx)
    fdb._ensure_once()
    mdb._ensure_once()
    with repo.unit_of_work() as c:
        inst = _owned_instrument(ctx, ticker, c)
        before = mdb.get_manual_breakdown(ctx.actor, inst["instrument_id"], conn=c)
        n = mdb.clear_manual_breakdown(ctx.actor, inst["instrument_id"], conn=c)
        if n:
            portfolio = repo.get_portfolio_by_owner(ctx.actor, conn=c)
            repo.record_audit_event(actor=ctx.actor, source=ctx.source, request_id=ctx.request_id,
                                    operation="clear_fund_manual_breakdown", portfolio_id=portfolio["portfolio_id"],
                                    before=before, after=None, conn=c)
    return bool(n)


# ── Views for the look-through engine ─────────────────────────────────────────

def _view_from_data(inst: dict, data: FundData, source: str, via: str | None = None) -> FundView:
    return FundView(
        ticker=inst["ticker"], name=inst.get("name"), source=source, via=via,
        holdings=[ViewHolding(h.symbol, h.name, h.weight, h.holding_is_fund, h.cusip, h.isin) for h in data.holdings],
        sector_weights=dict(data.sector_weights), asset_class_weights=dict(data.asset_class_weights),
        country_weights=data.country_weights, asset_class=inst.get("asset_class"),
        expense_ratio=data.profile.expense_ratio if data.profile else None,
        holdings_coverage=data.holdings_coverage, mapped_coverage=data.mapped_coverage, as_of=data.as_of,
        reason_codes=list(data.reason_codes))


def _view_from_manual(inst: dict, manual: dict) -> FundView:
    return FundView(
        ticker=inst["ticker"], name=inst.get("name"), source=SRC_MANUAL, holdings=[],
        sector_weights={k: v / 100.0 for k, v in (manual.get("sector") or {}).items()},
        asset_class_weights={k: v / 100.0 for k, v in manual["asset_class"].items()},
        asset_class=inst.get("asset_class"), expense_ratio=inst.get("expense_ratio"), as_of=manual.get("as_of"))


def load_fund_views(owner: str, tickers: list[str], *, max_depth: int | None = None) -> dict[str, FundView]:
    """
    {TICKER: FundView} for every fund ticker given, plus the funds inside them (to max_depth). Reads shared fund data
    and THIS owner's manual breakdowns only. A ticker with no instrument row is absent.
    """
    if max_depth is None:
        max_depth = int(sdb.get_portfolio_settings()["lookthrough_max_depth"])
    fdb._ensure_once()
    mdb._ensure_once()
    manual = mdb.list_manual_breakdowns(owner)
    views: dict[str, FundView] = {}
    frontier = [t.upper() for t in dict.fromkeys(tickers) if t]
    for _ in range(max(1, max_depth)):
        nxt: list[str] = []
        for t in frontier:
            if t in views:
                continue
            inst = repo.find_instrument(t)
            if inst is None:
                continue
            own = fdb.get_fund_data(inst["instrument_id"], t)
            view: FundView | None = None
            if own is not None and own.ok:
                view = _view_from_data(inst, own, SRC_OWN)
            elif inst["instrument_id"] in manual:
                view = _view_from_manual(inst, manual[inst["instrument_id"]])
            else:
                proxy = (inst.get("proxy_ticker") or "").strip().upper()
                pinst = repo.find_instrument(proxy) if proxy else None
                pdata = fdb.get_fund_data(pinst["instrument_id"], proxy) if pinst else None
                if pdata is not None and pdata.ok:
                    view = _view_from_data(inst, pdata, SRC_PROXY, via=proxy)
                    # the proxy's holdings are analytics for THIS fund; its asset class stays the instrument's own
            views[t] = view or FundView(ticker=t, name=inst.get("name"), source=SRC_NONE, asset_class=inst.get("asset_class"),
                                        expense_ratio=inst.get("expense_ratio"))
            nxt.extend((h.symbol or "").upper() for h in views[t].holdings if h.is_fund and h.symbol)
        frontier = [t for t in nxt if t not in views]
        if not frontier:
            break
    return views


def views_version(owner: str, tickers: list[str]) -> str:
    """Fingerprint of everything load_fund_views would read — shared fetch times plus this owner's manual rows — for cache keys."""
    ids = [i["instrument_id"] for t in tickers if (i := repo.find_instrument(t)) is not None]
    proxy_ids = []
    for t in tickers:
        inst = repo.find_instrument(t)
        proxy = (inst or {}).get("proxy_ticker")
        if proxy and (p := repo.find_instrument(proxy)) is not None:
            proxy_ids.append(p["instrument_id"])
    fdb._ensure_once()
    mdb._ensure_once()
    return f"{fdb.fund_data_version(ids + proxy_ids)}#{mdb.manual_breakdown_version(owner)}"
