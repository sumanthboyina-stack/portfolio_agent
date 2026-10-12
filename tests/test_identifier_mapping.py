"""CUSIP / ISIN -> ticker (Step 2.2). No test touches OpenFIGI: a fake session serves canned answers. The answers follow OpenFIGI's
documented v3 mapping format ({"data": [...]} / {"warning": "No identifier found."} / {"error": ...}); they are hand-written, NOT recorded
from the live service."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.services import fund_data_service as fds
from portfolio_agent.services.context import local_context
from portfolio_agent.tools import fund_data_db as fdb
from portfolio_agent.tools import holdings_db as repo
from portfolio_agent.tools import identifier_mapping as idm
from portfolio_agent.tools import investor_settings_db as sdb
from portfolio_agent.tools import lookthrough as lt
from portfolio_agent.tools.fund_data_providers import (
    RC_HOLDINGS_UNMAPPED, FundData, FundDataProvider, FundHolding, FundProfile,
)
from portfolio_agent.tools.lookthrough import FundView, ViewHolding, analyze

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
ME = local_context("test")
AAPL, MSFT, TSLA, VTSAX_C = "037833100", "594918104", "88160R101", "922908728"   # real CUSIP shapes
ISIN_AAPL = "US0378331005"


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")


def listing(ticker, exch="US", name=None, stype="Common Stock"):
    return {"figi": f"BBG{ticker}", "ticker": ticker, "name": name or ticker, "exchCode": exch, "securityType": stype}


class FakeFigi:
    """session.post(url, json=jobs, headers=..., timeout=...) -> a response; `book` maps idValue -> answer."""

    def __init__(self, book, status=200):
        self.book, self.status, self.calls = book, status, []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json, headers))
        outer = self

        class R:
            status_code = outer.status

            def json(self_inner):
                return [outer.book.get(j["idValue"], {"warning": "No identifier found."}) for j in json]
        return R()


class NoWait:
    def wait(self): pass


def client(book=None, *, key="", status=200, session=None):
    ses = session or FakeFigi(book or {}, status)
    return idm.OpenFigiClient(api_key=key, session=ses, spacing=NoWait()), ses


def cfg(**kw):
    c = dict(sdb.FUND_SOURCE_SETTINGS_DEFAULTS)
    c.update(kw)
    return c


BOOK = {AAPL: {"data": [listing("AAPL", "UW"), listing("AAPL", "US")]}, MSFT: {"data": [listing("MSFT")]},
        TSLA: {"warning": "No identifier found."}}


# ── identifiers, listing choice ───────────────────────────────────────────────

def test_normalize_id_accepts_only_well_shaped_identifiers():
    assert idm.normalize_id("CUSIP", " 037833100 ") == "037833100" and idm.normalize_id("CUSIP", "92203c105") == "92203C105"
    assert idm.normalize_id("ISIN", "us0378331005") == "US0378331005"
    for bad in ("N/A", "", None, "123", "0378331005X1"):
        assert idm.normalize_id("CUSIP", bad) is None
    assert idm.normalize_id("ISIN", "037833100") is None


def test_pick_listing_prefers_the_us_composite_and_needs_a_ticker():
    assert idm.pick_listing([listing("AAPL", "UW"), listing("AAPL", "US")])["exchCode"] == "US"
    assert idm.pick_listing([listing("XYZ", "LN")])["ticker"] == "XYZ"
    assert idm.pick_listing([{"ticker": ""}, {"name": "x"}]) is None and idm.pick_listing([]) is None


# ── resolving and caching ─────────────────────────────────────────────────────

def test_mapped_and_not_found_are_both_cached_and_not_asked_again():
    c, ses = client(BOOK)
    out = idm.resolve_identifiers([("CUSIP", AAPL), ("CUSIP", MSFT), ("CUSIP", TSLA)], cfg(), client=c, now=NOW)
    assert out == {("CUSIP", AAPL): "AAPL", ("CUSIP", MSFT): "MSFT", ("CUSIP", TSLA): None}
    assert len(ses.calls) == 1 and [j["idType"] for j in ses.calls[0][1]] == ["ID_CUSIP"] * 3
    rows = fdb.get_security_identifiers([("CUSIP", AAPL), ("CUSIP", TSLA)])
    assert rows[("CUSIP", AAPL)]["status"] == "mapped" and rows[("CUSIP", AAPL)]["exch_code"] == "US"
    assert rows[("CUSIP", TSLA)]["status"] == "not_found" and rows[("CUSIP", TSLA)]["ticker"] is None
    c2, ses2 = client(BOOK)
    again = idm.resolve_identifiers([("CUSIP", AAPL), ("CUSIP", TSLA)], cfg(), client=c2, now=NOW + timedelta(days=5))
    assert again == {("CUSIP", AAPL): "AAPL", ("CUSIP", TSLA): None} and ses2.calls == []


def test_a_mapping_is_looked_up_again_only_after_the_refresh_interval():
    c, ses = client(BOOK)
    idm.resolve_identifiers([("CUSIP", AAPL)], cfg(), client=c, now=NOW)
    idm.resolve_identifiers([("CUSIP", AAPL)], cfg(mapping_refresh_days=30), client=c, now=NOW + timedelta(days=29))
    assert len(ses.calls) == 1
    idm.resolve_identifiers([("CUSIP", AAPL)], cfg(mapping_refresh_days=30), client=c, now=NOW + timedelta(days=31))
    assert len(ses.calls) == 2


def test_disabled_uses_only_stored_mappings_and_sends_nothing():
    c, ses = client(BOOK)
    idm.resolve_identifiers([("CUSIP", AAPL)], cfg(), client=c, now=NOW)
    ses.calls.clear()
    out = idm.resolve_identifiers([("CUSIP", AAPL), ("CUSIP", MSFT)], cfg(openfigi_enabled=0.0), client=c, now=NOW)
    assert out == {("CUSIP", AAPL): "AAPL", ("CUSIP", MSFT): None} and ses.calls == []


def test_transient_failures_are_not_cached_and_stop_the_run():
    c, ses = client(BOOK, status=429)
    out = idm.resolve_identifiers([("CUSIP", AAPL), ("CUSIP", MSFT)], cfg(openfigi_batch_size=1), client=c, now=NOW)
    assert out == {("CUSIP", AAPL): None, ("CUSIP", MSFT): None} and len(ses.calls) == 1        # stopped after the first failure
    assert fdb.get_security_identifiers([("CUSIP", AAPL)]) == {}
    # a per-identifier error inside an otherwise good answer is not cached either
    c2, _ = client({AAPL: {"error": "Invalid idValue format"}, MSFT: {"data": [listing("MSFT")]}})
    out = idm.resolve_identifiers([("CUSIP", AAPL), ("CUSIP", MSFT)], cfg(), client=c2, now=NOW)
    assert out[("CUSIP", MSFT)] == "MSFT" and fdb.get_security_identifiers([("CUSIP", AAPL)]) == {}


def test_malformed_answers_raise_nothing_and_cache_nothing():
    class Bad(FakeFigi):
        def post(self, *a, **k):
            class R:
                status_code = 200
                def json(s): return [{"data": []}]                    # one answer for two jobs
            return R()
    c, _ = client(session=Bad({}))
    assert idm.resolve_identifiers([("CUSIP", AAPL), ("CUSIP", MSFT)], cfg(), client=c, now=NOW) == {("CUSIP", AAPL): None, ("CUSIP", MSFT): None}
    assert fdb.get_security_identifiers([("CUSIP", AAPL)]) == {}


def test_batch_size_follows_the_setting_but_no_key_caps_it_at_ten():
    ids = [(("CUSIP", f"{i:09d}")) for i in range(25)]
    c, ses = client({})
    idm.resolve_identifiers(ids, cfg(openfigi_batch_size=100, openfigi_max_ids_per_refresh=1000), client=c, now=NOW)
    assert [len(call[1]) for call in ses.calls] == [10, 10, 5]
    ckey, ses2 = client({}, key="k")
    idm.resolve_identifiers([("CUSIP", f"{i:09d}") for i in range(100, 125)],
                            cfg(openfigi_batch_size=20, openfigi_max_ids_per_refresh=1000), client=ckey, now=NOW)
    assert [len(call[1]) for call in ses2.calls] == [20, 5]


def test_per_refresh_cap_leaves_the_rest_for_a_later_run():
    c, ses = client({})
    ids = [("CUSIP", f"{i:09d}") for i in range(25)]
    out = idm.resolve_identifiers(ids, cfg(openfigi_max_ids_per_refresh=12), client=c, now=NOW)
    assert sum(len(call[1]) for call in ses.calls) == 12
    assert len(fdb.get_security_identifiers(ids)) == 12 and len(out) == 25          # the other 13 are simply unresolved, not cached


def test_api_key_comes_from_the_environment_and_is_sent_only_as_a_header(monkeypatch):
    monkeypatch.setenv("OPENFIGI_API_KEY", "sekret-key-123")
    ses = FakeFigi(BOOK)
    c = idm.OpenFigiClient(session=ses, spacing=NoWait())
    assert c.has_key and c.max_batch == 100
    idm.resolve_identifiers([("CUSIP", AAPL)], cfg(), client=c, now=NOW)
    url, jobs, headers = ses.calls[0]
    assert headers["X-OPENFIGI-APIKEY"] == "sekret-key-123" and "sekret" not in str(jobs)
    monkeypatch.delenv("OPENFIGI_API_KEY")
    c2 = idm.OpenFigiClient(session=FakeFigi(BOOK), spacing=NoWait())
    c2.map([{"idType": "ID_CUSIP", "idValue": AAPL}])
    assert "X-OPENFIGI-APIKEY" not in c2._session.calls[0][2] and c2.max_batch == 10
    # nothing about the key, and nothing from the settings table, is stored
    with sqlite3.connect(db_module.DB_PATH) as conn:
        dumped = " ".join(str(r) for t in ("security_identifiers",) for r in conn.execute(f"SELECT * FROM {t}"))
    assert "sekret" not in dumped


def test_errors_never_carry_the_key(monkeypatch):
    class Boom:
        def post(self, *a, **k): raise ConnectionError("secret=sekret-key-123")
    c = idm.OpenFigiClient(api_key="sekret-key-123", session=Boom(), spacing=NoWait())
    with pytest.raises(idm.MappingError) as e:
        c.map([{"idType": "ID_CUSIP", "idValue": AAPL}])
    assert "sekret" not in str(e.value)


def test_request_spacing_differs_with_and_without_a_key():
    waits = []
    sp = idm._Spacing(idm.NO_KEY_SPACING, clock=lambda: 0.0, sleep=waits.append)
    for _ in range(3):
        sp.wait()
    assert waits == [pytest.approx(2.4), pytest.approx(4.8)]
    assert idm.KEY_SPACING == pytest.approx(0.24)


# ── mapping a fund's holdings ─────────────────────────────────────────────────

def _fund(*hs, coverage=None):
    d = FundData(ticker="TDF", profile=FundProfile(), holdings=list(hs), source="nport")
    d.holdings_coverage = coverage if coverage is not None else round(sum(h.weight for h in hs), 12)
    return d


def test_holdings_get_tickers_and_coverage_reports_explained_vs_mapped():
    d = _fund(FundHolding(None, "Apple", 0.30, cusip=AAPL), FundHolding(None, "Tesla", 0.20, cusip=TSLA),
              FundHolding(None, "Microsoft", 0.10, cusip=MSFT))
    c, _ = client(BOOK)
    idm.map_fund_holdings(d, cfg(), client=c, now=NOW)
    assert {h.name: h.symbol for h in d.holdings} == {"Apple": "AAPL", "Tesla": None, "Microsoft": "MSFT"}
    assert d.holdings_coverage == pytest.approx(0.60) and d.mapped_coverage == pytest.approx(0.40)     # explained vs mapped
    assert RC_HOLDINGS_UNMAPPED in d.reason_codes
    assert [h.cusip for h in d.holdings if h.symbol is None] == [TSLA]                                # unmapped stay keyed by CUSIP


def test_largest_holdings_are_looked_up_first_when_capped():
    hs = [FundHolding(None, f"H{i}", 0.01 * (i + 1), cusip=f"{i:09d}") for i in range(6)]
    c, ses = client({f"{i:09d}": {"data": [listing(f"T{i}")]} for i in range(6)})
    d = _fund(*hs)
    idm.map_fund_holdings(d, cfg(openfigi_max_ids_per_refresh=2), client=c, now=NOW)
    assert sorted(h.symbol for h in d.holdings if h.symbol) == ["T4", "T5"]                            # the two biggest


def test_a_cusip_not_found_is_retried_as_isin_and_ticker_holdings_are_untouched():
    book = {AAPL: {"warning": "No identifier found."}, ISIN_AAPL: {"data": [listing("AAPL")]}}
    d = _fund(FundHolding(None, "Apple", 0.5, cusip=AAPL, isin=ISIN_AAPL), FundHolding("KO", "Coke", 0.2))
    c, ses = client(book)
    idm.map_fund_holdings(d, cfg(), client=c, now=NOW)
    assert [h.symbol for h in d.holdings] == ["AAPL", "KO"] and d.mapped_coverage == pytest.approx(0.7)
    assert [j["idType"] for call in ses.calls for j in call[1]] == ["ID_CUSIP", "ID_ISIN"]
    assert RC_HOLDINGS_UNMAPPED not in d.reason_codes


def test_provider_data_that_already_has_tickers_needs_no_lookup():
    d = _fund(FundHolding("AAPL", "Apple", 0.06), FundHolding("MSFT", "Microsoft", 0.05))
    c, ses = client(BOOK)
    idm.map_fund_holdings(d, cfg(), client=c, now=NOW)
    assert ses.calls == [] and d.mapped_coverage == pytest.approx(0.11) and d.reason_codes == []


# ── look-through: unmapped holdings stay keyed by CUSIP ───────────────────────

def test_unmapped_holdings_are_exposures_keyed_by_cusip_and_shared_across_funds():
    h = lambda w: ViewHolding(None, "Tesla", w, cusip=TSLA)
    views = {"AAA": FundView("AAA", holdings=[ViewHolding("MSFT", "Microsoft", 0.4), h(0.2)], asset_class_weights={"stock": 1.0}, source=lt.SRC_OWN),
             "BBB": FundView("BBB", holdings=[h(0.5)], asset_class_weights={"stock": 1.0}, source=lt.SRC_OWN)}
    pos = [{"account_id": 1, "tax_treatment": "TAXABLE", "ticker": t, "market_value": 1000.0, "instrument_type": "ETF",
            "leverage_factor": 1.0, "asset_class": "US_EQUITY", "is_paper": False} for t in ("AAA", "BBB")]
    r = analyze(pos, views).household
    assert r.issuers[f"CUSIP:{TSLA}"]["value"] == pytest.approx(700.0)               # 200 via AAA + 500 via BBB: one exposure, not two
    assert set(r.issuers[f"CUSIP:{TSLA}"]["via"]) == {"AAA", "BBB"}
    assert "MSFT" in r.issuers and lt.RC_LOOKTHROUGH_UNMAPPED in r.reason_codes
    assert r.fund_coverage_pct == pytest.approx(55.0)                               # (1100 explained) of 2000: explained ...
    assert r.fund_mapped_coverage_pct == pytest.approx(20.0)                        # ... but only 400 of it has a ticker


def test_a_fully_mapped_fund_has_equal_explained_and_mapped_coverage():
    views = {"VTI": FundView("VTI", holdings=[ViewHolding("AAPL", "Apple", 0.06)], asset_class_weights={"stock": 1.0}, source=lt.SRC_OWN)}
    r = analyze([{"account_id": 1, "tax_treatment": "T", "ticker": "VTI", "market_value": 1000.0, "instrument_type": "ETF",
                  "leverage_factor": 1.0, "asset_class": "US_EQUITY", "is_paper": False}], views).household
    assert r.fund_coverage_pct == r.fund_mapped_coverage_pct == pytest.approx(6.0)
    assert lt.RC_LOOKTHROUGH_UNMAPPED not in r.reason_codes


# ── through the refresh service: map, then store; the fund-of-funds opens ─────

class CusipProvider(FundDataProvider):
    name = "nport"
    def get_profile(self, t): return None
    def get_holdings(self, t): return []
    def get_sector_weights(self, t): return {}
    def get_asset_class_weights(self, t): return {}

    def __init__(self, table): self.table, self.calls = table, []

    def fetch(self, ticker):
        self.calls.append(ticker)
        return self.table.get(ticker) or FundData(ticker=ticker, source="nport", reason_codes=["NPORT_NOT_FILED"])


def test_refresh_maps_identifiers_before_saving_and_opens_the_funds_inside(monkeypatch):
    from datetime import timezone as tz
    for t, it in (("TDF", "TARGET_DATE_FUND"), ("VTSAX", "MUTUAL_FUND")):
        inst = repo.get_or_create_instrument(t)
        repo.update_instrument(inst["instrument_id"], instrument_type=it, classification_source="manual",
                               classified_at=datetime.now(tz.utc).isoformat())
    book = {VTSAX_C: {"data": [listing("VTSAX", "US", stype="Open-End Fund")]}, TSLA: {"warning": "No identifier found."}}
    fig, ses = client(book)
    monkeypatch.setattr(idm, "OpenFigiClient", lambda **k: fig)
    tdf = FundData("TDF", profile=FundProfile(), source="nport", as_of="2026-06-30", asset_class_weights={"stock": 1.0},
                   holdings=[FundHolding(None, "Vanguard Total Stock Market Index Fund", 0.9, cusip=VTSAX_C, holding_is_fund=True),
                             FundHolding(None, "Tesla", 0.05, cusip=TSLA)])
    tdf.holdings_coverage = 0.95
    vt = FundData("VTSAX", profile=FundProfile(), source="nport", holdings=[FundHolding("AAPL", "Apple", 0.06)],
                  asset_class_weights={"stock": 1.0}, holdings_coverage=0.06, as_of="2026-06-30")
    prov = CusipProvider({"TDF": tdf, "VTSAX": vt})
    status = fds.refresh_fund_data(ME, ["TDF"], provider=prov)
    assert status == {"TDF": "fetched", "VTSAX": "fetched"} and prov.calls == ["TDF", "VTSAX"]      # the mapped child was opened
    stored = fdb.get_fund_data(repo.find_instrument("TDF")["instrument_id"], "TDF")
    assert {h.name[:6]: h.symbol for h in stored.holdings} == {"Vangua": "VTSAX", "Tesla": None}
    assert stored.holdings_coverage == pytest.approx(0.95) and stored.mapped_coverage == pytest.approx(0.90)
    assert RC_HOLDINGS_UNMAPPED in stored.reason_codes
    # a second refresh with the cache warm sends nothing to OpenFIGI
    n = len(ses.calls)
    fds.refresh_fund_data(ME, ["TDF"], provider=prov, force=True)
    assert len(ses.calls) == n


def test_a_mapping_failure_never_breaks_the_refresh(monkeypatch):
    inst = repo.get_or_create_instrument("TDF2")
    repo.update_instrument(inst["instrument_id"], instrument_type="ETF", classification_source="manual",
                           classified_at=datetime.now(timezone.utc).isoformat())
    d = FundData("TDF2", profile=FundProfile(), source="nport", holdings=[FundHolding(None, "Apple", 0.5, cusip=AAPL)],
                 asset_class_weights={"stock": 1.0}, holdings_coverage=0.5)
    monkeypatch.setattr(idm, "map_fund_holdings", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert fds.refresh_fund_data(ME, ["TDF2"], provider=CusipProvider({"TDF2": d})) == {"TDF2": "fetched"}


def test_the_settings_are_admin_editable_validated_and_default_sensibly():
    from portfolio_agent.services import investor_service as svc
    from portfolio_agent.tools import auth_users_db
    auth_users_db.invite_user(f"{ME.actor}@example.com", ME.actor, "admin")
    ps = sdb.get_portfolio_settings()
    assert (ps["openfigi_enabled"], ps["openfigi_batch_size"], ps["mapping_refresh_days"]) == (1, 10, 30)
    out = svc.update_portfolio_settings(ME, openfigi_enabled=0, openfigi_batch_size=50, mapping_refresh_days=60, openfigi_max_ids_per_refresh=100)
    assert (out["openfigi_enabled"], out["openfigi_batch_size"], out["mapping_refresh_days"]) == (0, 50, 60)
    for bad in ({"openfigi_batch_size": 0}, {"openfigi_batch_size": 101}, {"openfigi_enabled": 2}, {"mapping_refresh_days": 0}):
        with pytest.raises(ValueError):
            svc.update_portfolio_settings(ME, **bad)


def test_migration_adds_the_cache_table_and_mapped_coverage_idempotently():
    fdb.save_fund_data(1, FundData("A", profile=FundProfile(), holdings=[FundHolding("X", "x", 0.1)], holdings_coverage=0.1))
    fdb._schema_ready.clear(); fdb._ensure_once(); fdb._schema_ready.clear(); fdb._ensure_once()
    with sqlite3.connect(db_module.DB_PATH) as c:
        assert [r[1] for r in c.execute("PRAGMA table_info(security_identifiers)")][:3] == ["id_type", "id_value", "status"]
        assert "mapped_coverage" in [r[1] for r in c.execute("PRAGMA table_info(fund_profiles)")]
    assert fdb.get_fund_data(1, "A").mapped_coverage == pytest.approx(0.1)             # a provider that never set mapped_coverage is not stored as 0
