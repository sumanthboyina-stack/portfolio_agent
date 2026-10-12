"""SEC N-PORT provider (Step 2.1). No test touches SEC: a fake session serves the recorded fixtures in tests/fixtures/sec
(real EDGAR responses, fetched once; fxaix_nport.xml has its holdings trimmed to a handful of real positions)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.services import investor_service as svc
from portfolio_agent.services.context import local_context
from portfolio_agent.tools import auth_users_db
from portfolio_agent.tools import fund_data_db as fdb
from portfolio_agent.tools import investor_settings_db as sdb
from portfolio_agent.tools import sec_nport as sn
from portfolio_agent.tools.fund_data_providers import (
    RC_FUND_FETCH_FAILED, RC_FUND_SECTORS_MISSING, RC_NPORT_NOT_CONFIGURED, RC_NPORT_NOT_FILED, FundData, FundHolding,
    FundProfile, NPortProvider,
)

FIX = Path(__file__).parent / "fixtures" / "sec"
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")


class Resp:
    def __init__(self, status=200, content=b""):
        self.status_code, self.content = status, content


class FakeSession:
    """Serves the fixtures by URL / series id and records every request (url, params, headers)."""

    def __init__(self, *, tickers=True, feeds=None, docs=None, fail=None):
        self.calls: list[tuple] = []
        self.tickers = tickers
        self.feeds = feeds if feeds is not None else {"S000006027": "fxaix", "S000012762": "vfifx"}
        self.docs = docs if docs is not None else {"819118": "fxaix", "752177": "vfifx"}
        self.fail = fail or {}

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append((url, params, headers, timeout))
        for frag, status in self.fail.items():
            if frag in url:
                return Resp(status)
        if url == sn.TICKERS_MF_URL:
            return Resp(200, (FIX / "company_tickers_mf_sample.json").read_bytes()) if self.tickers else Resp(500)
        if url == sn.BROWSE_URL:
            name = self.feeds.get(params["CIK"])
            return Resp(200, (FIX / f"{name}_atom.xml").read_bytes()) if name else Resp(200, b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>')
        for cik, name in self.docs.items():
            if f"/data/{cik}/" in url and url.endswith("/primary_doc.xml"):
                return Resp(200, (FIX / f"{name}_nport.xml").read_bytes())
        return Resp(404)


class NoSleep:
    def __init__(self): self.t = 0.0; self.slept = []
    def clock(self): return self.t
    def sleep(self, d): self.slept.append(d); self.t += d


def _provider(session=None, ua="Test Agent test@example.org", **kw):
    session = session or FakeSession()
    client = sn.SecClient(user_agent=ua, session=session, throttle=sn.Throttle(1000))
    cfg = dict(sdb.FUND_SOURCE_SETTINGS_DEFAULTS)
    return NPortProvider(client=client, settings=cfg, now=lambda: NOW, **kw), session


# ── parsing the recorded filings ──────────────────────────────────────────────

def test_ticker_table_and_feed_parse():
    rows = sn.parse_ticker_table((FIX / "company_tickers_mf_sample.json").read_bytes())
    assert (36405, "S000002839", "C000007774", "VFIAX") in rows
    feed = sn.parse_filing_feed((FIX / "fxaix_atom.xml").read_bytes())
    assert feed[0] == sn.Filing("0000035402-26-004626", "2026-07-24", "819118")      # newest first
    assert len(feed) == 10 and all(f.accession for f in feed)
    with pytest.raises(ValueError):
        sn.parse_ticker_table(json.dumps({"fields": ["x"], "data": []}))


def test_fxaix_report_weights_are_fractions_and_derivatives_are_separate():
    rep = sn.parse_nport_xml((FIX / "fxaix_nport.xml").read_bytes())
    assert (rep.series_id, rep.series_name, rep.period_date) == ("S000006027", "Fidelity 500 Index Fund", "2026-05-31")
    data = sn.report_to_fund_data("fxaix", rep)
    by = {h.name: h for h in data.holdings}
    assert by["GENERAL MOTORS CO"].weight == pytest.approx(0.115715191490 / 100)     # pctVal is a percent -> fraction
    assert by["GENERAL MOTORS CO"].cusip and by["GENERAL MOTORS CO"].isin.startswith("US") and by["GENERAL MOTORS CO"].symbol is None
    assert "CHICAGO MERCANTILE EXCH INC" not in by                                    # the S&P future is a derivative
    assert data.derivative_short_weight == pytest.approx(0.005769985706 / 100)
    assert data.holdings_coverage == pytest.approx(sum(h.weight for h in data.holdings))
    assert data.asset_class_weights["stock"] == pytest.approx(sum(h.weight for h in data.holdings if h.name.split()[0] in
                                                                  ("UNITED", "DIGITAL", "TAPESTRY", "GENERAL")))
    assert set(data.asset_class_weights) == {"stock", "bond", "cash"}                 # EC / DBT (UST bills) / STIV
    assert data.as_of == "2026-05-31" and data.source == "nport"
    assert data.profile.aum == pytest.approx(832153251408.41) and data.profile.fund_family == "Fidelity Concord Street Trust"
    assert RC_FUND_SECTORS_MISSING in data.reason_codes                               # N-PORT has no sectors


def test_target_date_fund_flags_its_underlying_funds_even_though_filed_as_corp():
    data = sn.report_to_fund_data("VFIFX", sn.parse_nport_xml((FIX / "vfifx_nport.xml").read_bytes()))
    assert len(data.holdings) == 5                                                    # 5 long positions; 2 derivatives set aside
    assert all(h.holding_is_fund for h in data.holdings)                              # Vanguard files them as CORP / EC
    assert [h.name for h in data.holdings][0] == "Vanguard Total Stock Market Index Fund"
    assert data.holdings[0].weight == pytest.approx(0.5369047486989)
    assert data.derivative_short_weight == pytest.approx((0.000908718938 + 0.001802664341) / 100)
    assert data.asset_class_weights["cash"] == pytest.approx(0.00508109486818)        # the money-market vehicle (STIV)


def test_stock_positions_are_not_flagged_as_funds_and_short_positions_leave_the_holdings():
    rep = sn.parse_nport_xml((FIX / "fxaix_nport.xml").read_bytes())
    assert not any(sn.looks_like_fund(p) for p in rep.positions if p.name in ("GENERAL MOTORS CO", "TAPESTRY INC"))
    # synthetic edit of the recorded filing (NOT real data): turn GM into a short position
    xml = (FIX / "fxaix_nport.xml").read_text()
    gm = xml.index("<name>GENERAL MOTORS CO</name>")
    seg_end = xml.index("</invstOrSec>", gm)
    seg = xml[gm:seg_end].replace("<payoffProfile>Long</payoffProfile>", "<payoffProfile>Short</payoffProfile>") \
                         .replace("<pctVal>0.115715191490</pctVal>", "<pctVal>-0.115715191490</pctVal>")
    data = sn.report_to_fund_data("X", sn.parse_nport_xml((xml[:gm] + seg + xml[seg_end:]).encode()))
    assert "GENERAL MOTORS CO" not in {h.name for h in data.holdings}
    assert data.derivative_short_weight == pytest.approx((0.005769985706 - 0.115715191490) / 100)


def test_dtd_and_garbage_are_refused():
    with pytest.raises(ValueError):
        sn.parse_nport_xml(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>')
    with pytest.raises(ValueError):
        sn.parse_nport_xml(b"not xml at all")
    with pytest.raises(ValueError):
        sn.parse_nport_xml(b'<edgarSubmission xmlns="http://www.sec.gov/edgar/nport"/>')


# ── the provider end to end (fake session) ────────────────────────────────────

def test_fetch_follows_ticker_to_series_to_filing_and_sends_the_user_agent():
    prov, ses = _provider()
    data = prov.fetch("fxaix")
    assert data.ok and data.source == "nport" and len(data.holdings) == 6 and data.as_of == "2026-05-31"
    urls = [c[0] for c in ses.calls]
    assert urls[0] == sn.TICKERS_MF_URL and urls[1] == sn.BROWSE_URL
    assert urls[2] == "https://www.sec.gov/Archives/edgar/data/819118/000003540226004626/primary_doc.xml"
    assert ses.calls[1][1]["CIK"] == "S000006027" and ses.calls[1][1]["type"] == "NPORT-P"
    assert all(c[2]["User-Agent"] == "Test Agent test@example.org" for c in ses.calls)


def test_ticker_table_is_cached_and_a_second_class_of_the_same_fund_reuses_the_filing():
    # two share classes of ONE series (FXAIX's): the table is stored first, as if downloaded a moment ago
    fdb.replace_sec_fund_ids([(819118, "S000006027", "C000100045", "FXAIX"), (819118, "S000006027", "C000999999", "FXAIXB")],
                             fetched_at=NOW.isoformat(timespec="seconds"))
    prov, ses = _provider()
    a, b = prov.fetch("FXAIX"), prov.fetch("FXAIXB")
    assert a.ok and b.ok
    urls = [c[0] for c in ses.calls]
    assert urls.count(sn.TICKERS_MF_URL) == 0                                         # fresh table in sec_fund_ids: no download
    assert sum(u.endswith("primary_doc.xml") for u in urls) == 1                      # the filing is fetched once for both classes
    assert a.holdings == b.holdings


def test_ticker_table_redownloaded_only_after_the_refresh_interval():
    prov, ses = _provider()
    prov.fetch("FXAIX")
    n = sum(c[0] == sn.TICKERS_MF_URL for c in ses.calls)
    prov._now = lambda: NOW + timedelta(days=6)
    prov.fetch("FXAIX")
    assert sum(c[0] == sn.TICKERS_MF_URL for c in ses.calls) == n
    prov._now = lambda: NOW + timedelta(days=8)
    prov.fetch("FXAIX")
    assert sum(c[0] == sn.TICKERS_MF_URL for c in ses.calls) == n + 1


def test_fund_with_no_filing_gets_nport_not_filed():
    prov, _ = _provider()
    assert prov.fetch("SPY").reason_codes == [RC_NPORT_NOT_FILED]                     # not in SEC's fund table (it is a UIT)
    prov2, _ = _provider(FakeSession(feeds={}))                                       # in the table, empty feed
    assert prov2.fetch("FXAIX").reason_codes == [RC_NPORT_NOT_FILED]


def test_failures_become_reason_codes_never_exceptions():
    prov, _ = _provider(FakeSession(tickers=False))
    assert prov.fetch("FXAIX").reason_codes == [RC_FUND_FETCH_FAILED]                 # no table, nothing cached
    for fail in ({"primary_doc.xml": 500}, {"browse-edgar": 403}):
        prov, _ = _provider(FakeSession(fail=fail))
        d = prov.fetch("FXAIX")
        assert d.reason_codes == [RC_FUND_FETCH_FAILED] and not d.ok
    # a failed refresh falls back on the table we already have
    prov, ses = _provider()
    prov.fetch("FXAIX")
    ses.tickers = False
    prov._now = lambda: NOW + timedelta(days=30)
    assert prov.fetch("FXAIX").ok


def test_series_mismatch_is_refused():
    prov, _ = _provider(FakeSession(feeds={"S000006027": "vfifx"}, docs={"752177": "vfifx"}))
    assert prov.fetch("FXAIX").reason_codes == [RC_FUND_FETCH_FAILED]


def test_no_user_agent_means_no_sec_call():
    prov, ses = _provider(ua="")
    assert prov.fetch("FXAIX").reason_codes == [RC_NPORT_NOT_CONFIGURED] and ses.calls == []


def test_blank_download_never_wipes_the_stored_table():
    fdb.replace_sec_fund_ids([(1, "S1", "C1", "AAA")])
    assert fdb.replace_sec_fund_ids([]) == 0
    assert fdb.get_sec_fund_id("aaa")["cik"] == 1


# ── throttle ──────────────────────────────────────────────────────────────────

def test_throttle_spaces_requests_and_never_exceeds_ten_per_second():
    clk = NoSleep()
    th = sn.Throttle(100, clock=clk.clock, sleep=clk.sleep)       # asked for 100/s; capped to SEC's 10/s
    for _ in range(11):
        th.wait()
    assert clk.t == pytest.approx(1.0, abs=1e-6)                  # 11 calls need 10 gaps of 0.1 s
    clk2 = NoSleep()
    th2 = sn.Throttle(2, clock=clk2.clock, sleep=clk2.sleep)
    for _ in range(3):
        th2.wait()
    assert clk2.t == pytest.approx(1.0, abs=1e-6)


def test_threads_share_one_throttle_per_rate():
    assert sn.shared_throttle(5) is sn.shared_throttle(5)
    assert sn.shared_throttle(50) is sn.shared_throttle(10)


# ── storage round trip + settings ─────────────────────────────────────────────

def test_n_port_data_round_trips_through_the_fund_tables():
    from portfolio_agent.tools import holdings_db as repo
    data = sn.report_to_fund_data("FXAIX", sn.parse_nport_xml((FIX / "fxaix_nport.xml").read_bytes()))
    fdb.save_fund_data(7, data)
    back = fdb.get_fund_data(7, "FXAIX")
    assert back.derivative_short_weight == pytest.approx(data.derivative_short_weight)
    assert {(h.name, h.cusip, h.isin) for h in back.holdings} == {(h.name, h.cusip, h.isin) for h in data.holdings}
    assert back.holdings_coverage == pytest.approx(data.holdings_coverage) and back.as_of == "2026-05-31"


def test_fund_source_settings_are_admin_editable_validated_and_capped_at_ten_per_second():
    ctx = local_context("test")
    auth_users_db.invite_user(f"{ctx.actor}@example.com", ctx.actor, "admin")
    s = svc.update_portfolio_settings(ctx, nport_requests_per_second=5, sec_fund_ids_refresh_days=14)
    assert (s["nport_requests_per_second"], s["sec_fund_ids_refresh_days"]) == (5, 14)
    for bad in ({"nport_requests_per_second": 11}, {"nport_requests_per_second": 0}, {"nport_http_timeout_seconds": 1}):
        with pytest.raises(ValueError):
            svc.update_portfolio_settings(ctx, **bad)
    assert svc.reset_portfolio_settings(ctx)["nport_requests_per_second"] == sdb.FUND_SOURCE_SETTINGS_DEFAULTS["nport_requests_per_second"]


def test_migration_adds_the_new_columns_to_an_existing_database_idempotently():
    fdb.save_fund_data(1, FundData("A", profile=FundProfile(), holdings=[FundHolding("X", "x", 0.1)]))
    fdb._schema_ready.clear()
    fdb._ensure_once()
    fdb._schema_ready.clear()
    fdb._ensure_once()
    with sqlite3.connect(db_module.DB_PATH) as c:
        assert "derivative_short_weight" in [r[1] for r in c.execute("PRAGMA table_info(fund_profiles)")]
        assert "isin" in [r[1] for r in c.execute("PRAGMA table_info(fund_holdings)")]
    assert fdb.get_fund_data(1, "A").holdings[0].symbol == "X"                         # old data untouched
