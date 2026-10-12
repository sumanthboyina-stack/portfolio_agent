"""Step 2.3: issuer holdings files, the provider chain and its merge rules, per-source staleness, atomic holdings replace, the evening /
admin refresh, and a performance check on a ~5,000-holding fund. No network: fake sessions and fake providers throughout. The issuer
fixtures in tests/fixtures/issuer are SYNTHETIC (written from the documented layouts, not downloaded)."""
from __future__ import annotations

import io
import sqlite3
import time
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.services import fund_data_service as fds
from portfolio_agent.services import investor_service as svc
from portfolio_agent.services.context import local_context
from portfolio_agent.tools import auth_users_db
from portfolio_agent.tools import fund_data_db as fdb
from portfolio_agent.tools import holdings_db as repo
from portfolio_agent.tools import investor_settings_db as sdb
from portfolio_agent.tools import issuer_holdings as ih
from portfolio_agent.tools import lookthrough as lt
from portfolio_agent.tools.fund_data_providers import (
    RC_FUND_DATA_STALE, RC_FUND_FETCH_FAILED, RC_FUND_HOLDINGS_MISSING, RC_FUND_HOLDINGS_TOP_N_ONLY, RC_FUND_SECTORS_MISSING,
    RC_HOLDINGS_KEPT_PREVIOUS, RC_ISSUER_NOT_CONFIGURED, RC_NPORT_NOT_FILED, FundData, FundDataProvider, FundHolding, FundProfile,
)
from portfolio_agent.tools.fund_provider_chain import PROVIDES, ChainFundProvider, build_default_chain

FIX = Path(__file__).parent / "fixtures" / "issuer"
TODAY = date(2026, 10, 12)
ME = local_context("test")


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")


def _admin():
    auth_users_db.invite_user(f"{ME.actor}@example.com", ME.actor, "admin")


class Resp:
    def __init__(self, content=b"", status=200): self.content, self.status_code = content, status


class FakeIssuerSession:
    def __init__(self, files=None, status=200): self.files, self.status, self.calls = files or {}, status, []
    def get(self, url, timeout=None, headers=None):
        self.calls.append((url, headers))
        return Resp(self.files.get(url, b""), self.status if url in self.files else 404)


class NoWait:
    def wait(self): pass


# ── issuer files ──────────────────────────────────────────────────────────────

def test_ishares_csv_parses_holdings_sectors_cash_and_derivatives():
    f = ih.parse_issuer_file((FIX / "ishares_ivv_sample.csv").read_bytes())
    assert f.as_of == "2026-10-09" and len(f.rows) == 8
    d = ih.issuer_file_to_fund_data("IVV", f)
    assert [(h.symbol, h.weight) for h in d.holdings][:2] == [("NVDA", pytest.approx(0.074)), ("AAPL", pytest.approx(0.066))]
    assert d.holdings_coverage == pytest.approx(0.176) and d.mapped_coverage == pytest.approx(0.176)    # tickers come with the file
    assert d.asset_class_weights == {"stock": pytest.approx(0.176), "cash": pytest.approx(0.005)}       # money market + cash
    assert d.derivative_short_weight == pytest.approx(0.0005)                                           # the future: not a holding
    assert set(d.sector_weights) == {"Technology", "Financial Services", "Healthcare"}                  # GICS names translated
    assert sum(d.sector_weights.values()) == pytest.approx(1.0)                                         # share of the equity part
    assert d.sector_weights["Technology"] == pytest.approx((0.074 + 0.066) / 0.176)
    assert "XTSLA" not in {h.symbol for h in d.holdings} and d.as_of == "2026-10-09" and d.source == "issuer_csv"


def test_spdr_spreadsheet_parses_and_a_cash_line_is_not_a_stock():
    d = ih.issuer_file_to_fund_data("SPY", ih.parse_issuer_file((FIX / "spdr_spy_sample.xlsx").read_bytes()))
    assert [h.symbol for h in d.holdings] == ["NVDA", "AAPL", "JPM", "LLY"] and d.as_of == "2026-10-09"
    assert d.asset_class_weights["cash"] == pytest.approx(0.002) and "USD" not in {h.symbol for h in d.holdings}


def test_unreadable_files_raise_issuer_file_error_only():
    for raw in (b"", b"just,some,text\n1,2,3\n", b"PK\x03\x04 not really a zip"):
        with pytest.raises(ih.IssuerFileError):
            ih.parse_issuer_file(raw)


def test_issuer_urls_must_be_https_on_the_issuers_own_domain():
    ok = "https://www.ishares.com/us/products/239726/x.ajax?fileType=csv"
    assert ih.validate_issuer_url("ishares", ok) == ok
    assert ih.validate_issuer_url("spdr", "https://www.ssga.com/x.xlsx")
    for issuer, bad in (("ishares", "http://www.ishares.com/x"), ("ishares", "https://evil.com/ishares.com"),
                        ("ishares", "https://ishares.com.evil.com/x"), ("spdr", ok), ("nobody", ok), ("ishares", "file:///etc/passwd")):
        with pytest.raises(ValueError):
            ih.validate_issuer_url(issuer, bad)


# ── the issuer provider and its source table ──────────────────────────────────

def test_sources_are_seeded_disabled_and_nothing_is_downloaded_until_enabled():
    rows = fdb.list_issuer_sources()
    assert {r["ticker"] for r in rows} >= {"IVV", "SPY"} and all(r["enabled"] == 0 for r in rows)
    ses = FakeIssuerSession()
    d = ih.IssuerCSVProvider(session=ses, spacing=NoWait()).fetch("IVV")
    assert d.reason_codes == [RC_ISSUER_NOT_CONFIGURED] and ses.calls == []
    assert ih.IssuerCSVProvider(session=ses, spacing=NoWait()).fetch("ZZZZ").reason_codes == [RC_ISSUER_NOT_CONFIGURED]


def test_enabled_source_is_downloaded_and_parsed_and_failures_are_codes():
    _admin()
    url = fdb.get_issuer_source("IVV", enabled_only=False)["url"]
    svc.upsert_fund_issuer_source(ME, "IVV", "ishares", url, True)
    ses = FakeIssuerSession({url: (FIX / "ishares_ivv_sample.csv").read_bytes()})
    d = ih.IssuerCSVProvider(session=ses, spacing=NoWait()).fetch("ivv")
    assert d.ok and len(d.holdings) == 5 and ses.calls[0][0] == url and "User-Agent" in ses.calls[0][1]
    for bad in (FakeIssuerSession({}, status=500), FakeIssuerSession({url: b"garbage"})):
        assert ih.IssuerCSVProvider(session=bad, spacing=NoWait()).fetch("IVV").reason_codes == [RC_FUND_FETCH_FAILED]
    # a row edited straight in the database to an off-domain URL is still refused at fetch time
    with sqlite3.connect(db_module.DB_PATH) as c:
        c.execute("UPDATE fund_issuer_sources SET url = 'https://evil.example.com/x.csv' WHERE ticker = 'IVV'")
    s2 = FakeIssuerSession({"https://evil.example.com/x.csv": b"x"})
    assert ih.IssuerCSVProvider(session=s2, spacing=NoWait()).fetch("IVV").reason_codes == [RC_FUND_FETCH_FAILED] and s2.calls == []


def test_issuer_source_admin_verb_is_admin_only_validated_audited_and_versioned():
    stranger_ctx = local_context("test")
    with pytest.raises(Exception):
        svc.upsert_fund_issuer_source(stranger_ctx, "IVV", "ishares", "https://www.ishares.com/x", True)       # not an admin yet
    _admin()
    with pytest.raises(ValueError):
        svc.upsert_fund_issuer_source(ME, "IVV", "ishares", "https://evil.com/x", True)
    row = svc.upsert_fund_issuer_source(ME, "IWM", "ishares", "https://www.ishares.com/iwm.csv", True, notes="new")
    assert row["enabled"] == 1 and row["version"] == 1
    row2 = svc.upsert_fund_issuer_source(ME, "IWM", "ishares", "https://www.ishares.com/iwm2.csv", False, expected_version=1)
    assert row2["version"] == 2 and row2["enabled"] == 0
    with pytest.raises(RuntimeError):
        svc.upsert_fund_issuer_source(ME, "IWM", "ishares", "https://www.ishares.com/iwm3.csv", True, expected_version=1)
    assert "upsert_fund_issuer_source" in [e["operation"] for e in repo.get_audit_events(limit=20)]


# ── the chain ─────────────────────────────────────────────────────────────────

class Fake(FundDataProvider):
    def __init__(self, name, data=None): self.name, self.data, self.calls = name, data, []
    def get_profile(self, t): return None
    def get_holdings(self, t): return []
    def get_sector_weights(self, t): return {}
    def get_asset_class_weights(self, t): return {}
    def fetch(self, t):
        self.calls.append(t)
        if isinstance(self.data, Exception):
            raise self.data
        return self.data or FundData(t, source=self.name, reason_codes=["FUND_PROFILE_MISSING"])


def fd(source, *, hold=None, cov=None, sectors=None, classes=None, expense=None, as_of=None, deriv=0.0, codes=(), aum=None, family=None):
    hs = [FundHolding(s, s, w) for s, w in (hold or [])]
    return FundData("F", profile=FundProfile(expense_ratio=expense, aum=aum, fund_family=family) if (expense is not None or aum or family) else None,
                    holdings=hs, sector_weights=sectors or {}, asset_class_weights=classes or {},
                    holdings_coverage=cov if cov is not None else sum(w for _, w in (hold or [])), as_of=as_of, source=source,
                    derivative_short_weight=deriv, reason_codes=list(codes))


CFG = dict(sdb.FUND_SOURCE_SETTINGS_DEFAULTS)


def chain(provs, order=("issuer_csv", "nport", "yfinance"), cfg=None):
    return ChainFundProvider(list(order), provs, cfg or CFG, today=lambda: TODAY)


def test_defaults_order_limits_and_provides_are_what_the_spec_says():
    ps = sdb.get_portfolio_settings()
    assert sdb.parse_fund_provider_order(ps["fund_provider_order_json"]) == ["issuer_csv", "nport", "yfinance"]
    assert (ps["issuer_csv_max_age_days"], ps["nport_max_age_days"], ps["yfinance_max_age_days"]) == (3, 150, 7)
    assert PROVIDES["nport"] == {"holdings", "asset_classes"}


def test_each_piece_comes_from_its_best_source_and_the_merge_says_which():
    issuer = Fake("issuer_csv", fd("issuer_csv", hold=[("AAPL", 0.5), ("MSFT", 0.45)], sectors={"Technology": 1.0},
                                  classes={"stock": 0.95, "cash": 0.05}, as_of="2026-10-10", codes=[RC_FUND_SECTORS_MISSING]))
    nport = Fake("nport", fd("nport", hold=[("X", 0.3)], as_of="2026-06-30"))
    yf = Fake("yfinance", fd("yfinance", hold=[("AAPL", 0.07)], sectors={"Energy": 1.0}, classes={"stock": 1.0}, expense=0.0003,
                              codes=[RC_FUND_HOLDINGS_TOP_N_ONLY, "FUND_HOLDINGS_MISSING"], family="Fam"))
    out = chain({"issuer_csv": issuer, "nport": nport, "yfinance": yf}).fetch("IVV")
    assert out.source == "issuer_csv" and out.holdings_coverage == pytest.approx(0.95) and out.as_of == "2026-10-10"
    assert out.sector_weights == {"Technology": 1.0} and out.profile.expense_ratio == 0.0003 and out.profile.fund_family == "Fam"
    assert out.source_detail == {"holdings": "issuer_csv", "asset_classes": "issuer_csv", "sectors": "issuer_csv", "profile": "yfinance"}
    assert nport.calls == [] and yf.calls == ["IVV"]                           # complete fresh holdings + sectors: N-PORT never asked
    assert RC_FUND_SECTORS_MISSING not in out.reason_codes and RC_FUND_HOLDINGS_TOP_N_ONLY not in out.reason_codes


def test_a_mutual_fund_gets_nport_holdings_and_yfinance_sectors_and_profile():
    issuer = Fake("issuer_csv")                                                  # not configured
    nport = Fake("nport", fd("nport", hold=[("A", 0.6), ("B", 0.39)], classes={"stock": 0.99}, as_of="2026-06-30", deriv=-0.001,
                              aum=1e9, family="Trust"))
    yf = Fake("yfinance", fd("yfinance", sectors={"Technology": 1.0}, classes={"stock": 1.0}, expense=0.0004, hold=[("A", 0.05)], aum=2e9))
    out = chain({"issuer_csv": issuer, "nport": nport, "yfinance": yf}).fetch("VFIAX")
    assert out.source == "nport" and out.derivative_short_weight == pytest.approx(-0.001) and out.as_of == "2026-06-30"
    assert out.asset_class_weights == {"stock": 0.99}                            # the holdings source's own mix wins
    assert out.sector_weights == {"Technology": 1.0} and out.profile.expense_ratio == 0.0004
    assert out.profile.aum == 1e9 and out.profile.fund_family == "Trust"         # AUM / family: first source that has it
    assert out.source_detail["holdings"] == "nport" and out.source_detail["sectors"] == "yfinance"
    assert RC_ISSUER_NOT_CONFIGURED not in out.reason_codes                      # absence codes drop once another source delivered


def test_stale_source_is_passed_over_and_if_all_are_stale_the_best_is_used_with_a_code():
    old_issuer = fd("issuer_csv", hold=[("A", 0.97)], as_of="2026-10-01", sectors={"Technology": 1.0}, classes={"stock": 1.0})   # 11 days > 3
    nport = fd("nport", hold=[("A", 0.9), ("B", 0.09)], as_of="2026-07-01", classes={"stock": 1.0})                               # 103 days < 150
    out = chain({"issuer_csv": Fake("i", old_issuer), "nport": Fake("n", nport), "yfinance": Fake("y")}).fetch("X")
    assert out.source == "nport" and out.holdings[0].weight == 0.9                       # the fresh holdings win over the better-looking stale ones
    assert out.source_detail["sectors"] == "issuer_csv" and RC_FUND_DATA_STALE in out.reason_codes   # only a stale file has sectors: used, and flagged
    stale_n = fd("nport", hold=[("A", 0.99)], as_of="2026-01-01", classes={"stock": 1.0})                                         # 284 days > 150
    out2 = chain({"issuer_csv": Fake("i", old_issuer), "nport": Fake("n", stale_n), "yfinance": Fake("y")}).fetch("X")
    assert out2.source == "nport" and RC_FUND_DATA_STALE in out2.reason_codes            # all stale: best coverage, flagged, not hidden
    # limits are settings: a longer issuer limit makes the same file fresh again
    out3 = chain({"issuer_csv": Fake("i", old_issuer), "nport": Fake("n", nport), "yfinance": Fake("y")},
                 cfg={**CFG, "issuer_csv_max_age_days": 30}).fetch("X")
    assert out3.source == "issuer_csv"


def test_order_is_respected_and_a_failing_source_never_stops_the_chain():
    a = Fake("issuer_csv", fd("issuer_csv", hold=[("A", 0.5)], cov=0.5, as_of="2026-10-11", classes={"stock": 1.0}))
    b = Fake("nport", fd("nport", hold=[("B", 0.5)], cov=0.5, as_of="2026-09-30", classes={"stock": 1.0}))
    out = chain({"issuer_csv": a, "nport": b, "yfinance": Fake("yfinance", RuntimeError("boom"))}, order=("nport", "issuer_csv", "yfinance")).fetch("X")
    assert out.holdings[0].symbol == "B" and RC_FUND_FETCH_FAILED in out.reason_codes    # tie on coverage: the earlier source in the ORDER wins
    out2 = chain({"issuer_csv": a, "nport": b}, order=("issuer_csv", "nport", "unknown")).fetch("X")
    assert out2.holdings[0].symbol == "A"                                                # an unknown name in the order is ignored


def test_nothing_usable_gives_the_union_of_reason_codes_and_no_data():
    out = chain({"issuer_csv": Fake("i", FundData("X", reason_codes=[RC_ISSUER_NOT_CONFIGURED])),
                 "nport": Fake("n", FundData("X", reason_codes=[RC_NPORT_NOT_FILED])),
                 "yfinance": Fake("y", FundData("X", reason_codes=[RC_FUND_FETCH_FAILED]))}).fetch("X")
    assert not out.ok and out.reason_codes == [RC_ISSUER_NOT_CONFIGURED, RC_NPORT_NOT_FILED, RC_FUND_FETCH_FAILED]


def test_default_chain_is_built_from_the_database_settings_and_can_be_reordered():
    _admin()
    svc.update_portfolio_settings(ME, fund_provider_order=["yfinance", "nport"], nport_max_age_days=200)
    c = build_default_chain(providers={"issuer_csv": Fake("i"), "nport": Fake("n"), "yfinance": Fake("y")})
    assert c._order == ["yfinance", "nport"] and float(c._cfg["nport_max_age_days"]) == 200
    for bad in ([], ["nport", "nport"], ["nope"], "nport"):
        with pytest.raises(ValueError):
            svc.update_portfolio_settings(ME, fund_provider_order=bad)
    assert svc.reset_portfolio_settings(ME)["fund_provider_order_json"] == '["issuer_csv", "nport", "yfinance"]'


# ── storing: atomic replace, never downgrade ──────────────────────────────────

def _typed(t, it="ETF"):
    inst = repo.get_or_create_instrument(t)
    repo.update_instrument(inst["instrument_id"], instrument_type=it, classification_source="manual",
                           classified_at=datetime.now(timezone.utc).isoformat())
    return repo.find_instrument(t)["instrument_id"]


def test_holdings_replace_is_all_or_nothing():
    iid = _typed("ATOM")
    fdb.save_fund_data(iid, fd("a", hold=[("A", 0.5), ("B", 0.4)], as_of="2026-10-01", classes={"stock": 1.0}))
    bad = fd("b", hold=[("C", 0.9)], as_of="2026-10-11", classes={"stock": 1.0})
    bad.holdings.append(FundHolding("D", "D", None))                          # NOT NULL weight: the insert fails half way
    with pytest.raises(sqlite3.IntegrityError):
        fdb.save_fund_data(iid, bad)
    back = fdb.get_fund_data(iid, "ATOM")
    assert [h.symbol for h in back.holdings] == ["A", "B"] and back.as_of == "2026-10-01" and back.source == "a"      # the old fund, intact


def test_a_refresh_without_holdings_keeps_the_stored_holdings_and_says_so():
    iid = _typed("KEEP")
    fdb.save_fund_data(iid, fd("nport", hold=[("A", 0.7), ("B", 0.2)], as_of="2026-06-30", classes={"stock": 1.0}))
    only_profile = fd("yfinance", expense=0.001, classes={"stock": 1.0})
    only_profile.holdings_coverage = 0.0
    assert fds.refresh_fund_data(ME, ["KEEP"], provider=Fake("x", only_profile), force=True) == {"KEEP": "fetched"}
    back = fdb.get_fund_data(iid, "KEEP")
    assert [h.symbol for h in back.holdings] == ["A", "B"] and back.as_of == "2026-06-30" and back.source == "nport"
    assert RC_HOLDINGS_KEPT_PREVIOUS in back.reason_codes and back.profile.expense_ratio == 0.001 and back.holdings_coverage == pytest.approx(0.9)


def test_source_detail_and_new_columns_round_trip_and_migrate_idempotently():
    iid = _typed("DET")
    d = fd("issuer_csv", hold=[("A", 0.5)], as_of="2026-10-10")
    d.source_detail = {"holdings": "issuer_csv", "profile": "yfinance"}
    fdb.save_fund_data(iid, d)
    assert fdb.get_fund_data(iid, "DET").source_detail == {"holdings": "issuer_csv", "profile": "yfinance"}
    fdb._schema_ready.clear(); fdb._ensure_once(); fdb._schema_ready.clear(); fdb._ensure_once()
    with sqlite3.connect(db_module.DB_PATH) as c:
        assert "source_detail_json" in [r[1] for r in c.execute("PRAGMA table_info(fund_profiles)")]
        assert c.execute("SELECT COUNT(*) FROM fund_issuer_sources WHERE enabled = 0").fetchone()[0] >= 2     # re-seeding never duplicates / enables
    assert [r["ticker"] for r in fdb.list_fund_staleness()] == ["DET"]


# ── evening / admin refresh ───────────────────────────────────────────────────

def test_refresh_all_covers_every_held_fund_in_any_portfolio_and_admin_refresh_ignores_the_ttl():
    from portfolio_agent.services import account_service
    _admin()
    for t, it in (("ETFA", "ETF"), ("STK", "STOCK"), ("MFA", "MUTUAL_FUND")):
        acct = account_service.create_account(ME, broker="manual", display_name=t)
        account_service.add_position(ME, acct["account_id"], {"ticker": t, "shares": 1, "current_price": 10, "sector": "Technology"})
        _typed(t, it)
    prov = Fake("p", fd("p", hold=[("A", 0.95)], classes={"stock": 1.0}, as_of="2026-10-11"))
    assert fds.held_fund_tickers() == ["ETFA", "MFA"]                                     # the stock is not a fund
    out = fds.refresh_all_fund_data(ME, provider=prov)
    assert out["funds"] == 2 and out["counts"] == {"fetched": 2} and prov.calls == ["ETFA", "MFA"]
    assert fds.refresh_all_fund_data(ME, provider=prov)["counts"] == {"fresh": 2} and len(prov.calls) == 2     # TTL-guarded
    import portfolio_agent.tools.fund_provider_chain as fpc
    fpc_default = fpc.build_default_chain
    try:
        fpc.build_default_chain = lambda *a, **k: prov                                  # the admin verb uses default_provider()
        out = svc.refresh_fund_data_now(ME)
    finally:
        fpc.build_default_chain = fpc_default
    assert out["counts"] == {"fetched": 2} and len(prov.calls) == 4                      # force: refetched despite the TTL
    assert "refresh_fund_data_now" in [e["operation"] for e in repo.get_audit_events(limit=30)]


def test_refresh_now_is_admin_only():
    with pytest.raises(Exception):
        svc.refresh_fund_data_now(ME)


def test_evening_batch_calls_the_fund_refresh():
    from pathlib import Path as P
    src = (P(__file__).resolve().parents[1] / "portfolio_agent" / "pipeline" / "batch" / "evening.py").read_text()
    assert "refresh_all_fund_data" in src and "system_context(\"evening\")" in src


def test_exposures_version_was_bumped_for_the_new_fund_data_shape():
    from portfolio_agent.tools import portfolio_assessment as pa
    assert pa.EXPOSURES_VERSION.startswith("portfolio-exposures/2.1")


# ── performance: a ~5,000-holding fund ────────────────────────────────────────

def _big_fund(n=5000):
    w = [1.0 / (i + 20) for i in range(n)]
    tot = sum(w)
    hs = [FundHolding(f"T{i}" if i % 2 == 0 else None, f"Holding {i}", w[i] / tot * 0.99,
                      cusip=None if i % 2 == 0 else f"{i:09d}", isin=None) for i in range(n)]
    d = FundData("BIG", profile=FundProfile(expense_ratio=0.0003), holdings=hs, asset_class_weights={"stock": 1.0},
                 holdings_coverage=0.99, as_of="2026-10-10", source="issuer_csv", sector_weights={"Technology": 1.0})
    return d


def test_five_thousand_holdings_store_load_map_and_look_through_in_reasonable_time():
    from portfolio_agent.tools import identifier_mapping as idm
    iid = _typed("BIG")
    d = _big_fund()
    t0 = time.perf_counter(); fdb.save_fund_data(iid, d); t_save = time.perf_counter() - t0
    t0 = time.perf_counter(); back = fdb.get_fund_data(iid, "BIG"); t_load = time.perf_counter() - t0
    assert len(back.holdings) == 5000 and back.holdings_coverage == pytest.approx(0.99)
    # identifiers all cached already: mapping the 2,500 CUSIP-only holdings needs no network at all
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    fdb.save_security_identifiers([{"id_type": "CUSIP", "id_value": f"{i:09d}", "status": "mapped", "ticker": f"M{i}", "resolved_at": stamp}
                                   for i in range(5000) if i % 2 == 1])
    class Boom:
        def post(self, *a, **k): raise AssertionError("network used")
    c = idm.OpenFigiClient(session=Boom(), spacing=NoWait())
    t0 = time.perf_counter(); idm.map_fund_holdings(back, {**CFG, "openfigi_max_ids_per_refresh": 5000}, client=c); t_map = time.perf_counter() - t0
    assert back.mapped_coverage == pytest.approx(back.holdings_coverage)
    view = fds._view_from_data({"ticker": "BIG", "name": "Big"}, back, lt.SRC_OWN)
    pos = [{"account_id": 1, "tax_treatment": "TAXABLE", "ticker": "BIG", "market_value": 100000.0, "instrument_type": "ETF",
            "leverage_factor": 1.0, "asset_class": "US_EQUITY", "is_paper": False}]
    t0 = time.perf_counter(); r = lt.analyze(pos, {"BIG": view}).household; t_lt = time.perf_counter() - t0
    assert len(r.named_issuers()) == 5000 and r.fund_coverage_pct == pytest.approx(99.0, abs=0.01)
    # generous ceilings: they catch an accidental O(n^2), not machine noise
    print(f"PERF save={t_save:.3f}s load={t_load:.3f}s map={t_map:.3f}s lookthrough={t_lt:.3f}s")
    assert t_save < 3.0 and t_load < 2.0 and t_map < 3.0 and t_lt < 2.0, (t_save, t_load, t_map, t_lt)
