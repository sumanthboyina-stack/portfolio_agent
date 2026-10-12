"""
Step 2 — fund data, look-through and the assessment that uses it.

No live calls: every provider here is a canned fake. Covers the data model (shared fund tables vs the owner-scoped manual
breakdown), the deterministic look-through engine (coverage, `_other`, overlap, leverage / inverse, fund-of-funds
recursion with a cycle guard), priority manual > proxy > UNKNOWN, per-instrument-type forecast horizons, NAV-aware price
staleness, partial-coverage handling in the assessment (never "not breached"), cache invalidation on fresh fund data,
and the boundary: the shared fund_context never sees anything private.
"""
from __future__ import annotations

import dataclasses
import json
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.domain import FUND_INSTRUMENT_TYPES, LOCAL_OWNER, NO_PREDICTION_TYPES, InstrumentType
from portfolio_agent.services import account_service, investor_service as inv
from portfolio_agent.services import fund_data_service as fds
from portfolio_agent.services.context import RequestContext, _mint_identity_for_tests, local_context, system_context
from portfolio_agent.services.instrument_classifier import classify_from_info
from portfolio_agent.tools import auth_users_db, engine_settings_db as esdb
from portfolio_agent.tools import fund_data_db as fdb
from portfolio_agent.tools import holdings_db as repo
from portfolio_agent.tools import investor_settings_db as sdb
from portfolio_agent.tools import lookthrough as lt
from portfolio_agent.tools import portfolio_assessment as pa
from portfolio_agent.tools import portfolio_risk as risk
from portfolio_agent.tools.fund_data_providers import FundData, FundDataProvider, FundHolding, FundProfile
from portfolio_agent.tools.instrument_reference import get_fund_context
from portfolio_agent.tools.lookthrough import FundView, ViewHolding, analyze, concentration_stats
from portfolio_agent.tools.price_freshness import expected_price_date, is_price_stale

ROOT = Path(__file__).resolve().parents[1]
ME = local_context("test")


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(risk, "compute_portfolio_risk_context", lambda *a, **k: {})      # no price history, no network


# ── helpers ───────────────────────────────────────────────────────────────────

def _typed(ticker, itype, leverage=1.0, proxy=None, asset_class=None):
    inst = repo.get_or_create_instrument(ticker)
    fields = dict(instrument_type=itype, leverage_factor=leverage, classification_source="manual",
                  classified_at=datetime.now(timezone.utc).isoformat())
    if proxy:
        fields["proxy_ticker"] = proxy
    if asset_class:
        fields["asset_class"] = asset_class
    repo.update_instrument(inst["instrument_id"], **fields)
    return repo.find_instrument(ticker)


def _hold(ctx, ticker, shares, price, *, itype="STOCK", leverage=1.0, sector="Technology", proxy=None, account_type=None):
    acct = account_service.create_account(ctx, broker="manual", display_name=f"acct-{ticker}", account_type=account_type)
    account_service.add_position(ctx, acct["account_id"], {"ticker": ticker, "shares": shares, "current_price": price,
                                                           "sector": sector})
    _typed(ticker, itype, leverage, proxy=proxy)


def _save(ticker, holdings=(), sectors=None, classes=None, expense=None, source="fake", fetched_at=None, coverage=None):
    inst = repo.find_instrument(ticker)
    hs = [FundHolding(sym, name, w, holding_is_fund=isf) for sym, name, w, isf in holdings]
    data = FundData(ticker=ticker, profile=FundProfile(expense_ratio=expense), holdings=hs, sector_weights=sectors or {},
                    asset_class_weights=classes or {}, holdings_coverage=coverage if coverage is not None else sum(h.weight for h in hs),
                    as_of="2026-10-01", source=source)
    fdb.save_fund_data(inst["instrument_id"], data, fetched_at=fetched_at)


class FakeProvider(FundDataProvider):
    name = "fake"

    def __init__(self, table):
        self.table, self.calls = table, []

    def get_profile(self, t): return None
    def get_holdings(self, t): return []
    def get_sector_weights(self, t): return {}
    def get_asset_class_weights(self, t): return {}

    def fetch(self, ticker):
        self.calls.append(ticker)
        if isinstance(self.table.get(ticker), Exception):
            raise self.table[ticker]
        return self.table.get(ticker) or FundData(ticker=ticker, source="fake", reason_codes=["FUND_PROFILE_MISSING"])


def _data(ticker, holdings=(), sectors=None, classes=None, expense=None):
    hs = [FundHolding(s, s, w) for s, w in holdings]
    return FundData(ticker=ticker, profile=FundProfile(expense_ratio=expense), holdings=hs, sector_weights=sectors or {},
                    asset_class_weights=classes or {"stock": 1.0}, holdings_coverage=sum(w for _, w in holdings),
                    as_of="2026-10-01", source="fake")


def _pos(ticker, value, itype="STOCK", lf=1.0, account=1, tax="TAXABLE", ac="US_EQUITY", paper=False):
    return {"account_id": account, "tax_treatment": tax, "ticker": ticker, "market_value": value, "instrument_type": itype,
            "leverage_factor": lf, "asset_class": ac, "is_paper": paper}


def _view(ticker, holdings, *, classes=None, sectors=None, source=lt.SRC_OWN, via=None, ac="US_EQUITY"):
    return FundView(ticker=ticker, source=source, via=via, holdings=[ViewHolding(s, s, w, isf) for s, w, isf in holdings],
                    asset_class_weights=classes or {"stock": 1.0}, sector_weights=sectors or {}, asset_class=ac)


# ── Engine: coverage, _other, overlap, leverage ───────────────────────────────

def test_named_holdings_become_issuer_exposure_and_the_rest_stays_in_a_fund_other_bucket():
    views = {"VTI": _view("VTI", [("AAPL", 0.06, False), ("MSFT", 0.05, False)])}
    r = analyze([_pos("VTI", 1000.0, "ETF")], views).household
    assert r.issuers["AAPL"]["value"] == pytest.approx(60) and r.issuers["MSFT"]["value"] == pytest.approx(50)
    assert r.issuers["VTI_other"]["value"] == pytest.approx(890) and r.issuers["VTI_other"]["bucket"] is True
    assert r.coverage_pct == pytest.approx(11.0) and r.fund_coverage_pct == pytest.approx(11.0)
    assert set(r.named_issuers()) == {"AAPL", "MSFT"}                 # a bucket is never an issuer
    assert lt.RC_LOOKTHROUGH_TOP_N_ONLY in r.reason_codes
    assert r.issuers["AAPL"]["via"] == {"VTI": pytest.approx(60)}


def test_a_fund_with_no_data_is_one_unknown_bucket_and_never_a_guess():
    r = analyze([_pos("MYSTERY", 500.0, "ETF"), _pos("AAPL", 500.0)], {"MYSTERY": FundView("MYSTERY")}).household
    assert r.issuers["MYSTERY_unknown"]["bucket"] and lt.RC_LOOKTHROUGH_NO_DATA in r.reason_codes
    assert r.coverage_pct == pytest.approx(50.0)                        # only the stock is explained


def test_overlap_report_lists_every_way_an_issuer_is_reached():
    views = {"VTI": _view("VTI", [("AAPL", 0.10, False)]), "QQQ": _view("QQQ", [("AAPL", 0.20, False)])}
    r = analyze([_pos("AAPL", 100.0), _pos("VTI", 1000.0, "ETF"), _pos("QQQ", 500.0, "ETF")], views).household
    [o] = r.overlap()
    assert o["issuer"] == "AAPL" and o["value"] == pytest.approx(300.0)
    assert {s["via"]: s["value"] for s in o["sources"]} == {"VTI": pytest.approx(100.0), "QQQ": pytest.approx(100.0), "AAPL": pytest.approx(100.0)}


def test_leveraged_and_inverse_funds_are_counted_and_scale_issuer_exposure():
    views = {"TQQQ": _view("TQQQ", [("AAPL", 0.10, False)]), "SQQQ": _view("SQQQ", [("AAPL", 0.10, False)])}
    r = analyze([_pos("TQQQ", 100.0, "ETF", lf=3.0), _pos("SQQQ", 100.0, "ETF", lf=-3.0), _pos("AAPL", 800.0)], views).household
    assert r.leveraged_inverse_value == pytest.approx(200.0) and r.leveraged_inverse_pct() == pytest.approx(20.0)
    assert r.leverage_adjustment == pytest.approx(100 * 2 + 100 * -4)
    aapl = r.issuers["AAPL"]
    assert aapl["value"] == pytest.approx(820.0)                                    # market value
    assert aapl["exposure"] == pytest.approx(800 + 10 * 3 + 10 * -3)                # economic exposure × leverage_factor


def test_target_date_fund_opens_its_index_funds_and_a_cycle_cannot_loop():
    views = {
        "TGT2050": _view("TGT2050", [("IDX1", 0.6, True), ("IDX2", 0.4, True)], classes={"stock": 0.9, "bond": 0.1}),
        "IDX1": _view("IDX1", [("AAPL", 0.5, False), ("TGT2050", 0.5, True)]),            # holds its own parent: a cycle
        "IDX2": _view("IDX2", [("MSFT", 1.0, False)]),
    }
    r = analyze([_pos("TGT2050", 1000.0, "TARGET_DATE_FUND")], views, max_depth=3).household
    assert r.issuers["AAPL"]["value"] == pytest.approx(300.0) and r.issuers["MSFT"]["value"] == pytest.approx(400.0)
    assert r.issuers["IDX1_other"]["value"] == pytest.approx(300.0)                   # the cyclic half is not expanded
    assert lt.RC_LOOKTHROUGH_CYCLE in r.reason_codes
    assert r.coverage_pct == pytest.approx(70.0)


def test_depth_limit_and_missing_level_are_reason_codes_not_guesses():
    views = {"TGT": _view("TGT", [("IDX1", 1.0, True)]), "IDX1": _view("IDX1", [("AAPL", 1.0, False)])}
    shallow = analyze([_pos("TGT", 1000.0, "TARGET_DATE_FUND")], views, max_depth=1).household
    assert lt.RC_LOOKTHROUGH_DEPTH_LIMIT in shallow.reason_codes and "AAPL" not in shallow.issuers
    missing = analyze([_pos("TGT", 1000.0, "TARGET_DATE_FUND")], {"TGT": views["TGT"]}, max_depth=3).household
    assert lt.RC_LOOKTHROUGH_MISSING_LEVEL in missing.reason_codes and missing.issuers["IDX1_unknown"]["bucket"]


def test_scopes_account_tax_treatment_and_paper_exclusion():
    pos = [_pos("AAPL", 100.0, account=1, tax="TAXABLE"), _pos("AAPL", 300.0, account=2, tax="TAX_DEFERRED"),
           _pos("AAPL", 999.0, account=3, tax="TAXABLE", paper=True)]
    res = analyze(pos, {})
    assert res.household.total_value == pytest.approx(400.0)
    assert set(res.by_account) == {"1", "2"} and res.by_account["2"].total_value == pytest.approx(300.0)
    assert res.by_tax_treatment["TAXABLE"].total_value == pytest.approx(100.0)


def test_sector_asset_class_and_international_mix_come_from_the_fund_not_a_guess():
    views = {"VXUS": _view("VXUS", [("NESN", 0.02, False)], classes={"stock": 0.95, "cash": 0.05},
                           sectors={"Technology": 0.5, "Healthcare": 0.5}, ac="INTL_EQUITY")}
    r = analyze([_pos("VXUS", 1000.0, "ETF", ac="INTL_EQUITY"), _pos("AAPL", 1000.0)], views).household
    assert r.asset_class_pct() == {"stock": pytest.approx(97.5), "cash": pytest.approx(2.5)}
    assert r.sector_pct()["Technology"] == pytest.approx(23.75)
    assert r.intl_share_pct() == pytest.approx(100 * 950 / 1950, abs=0.01)


def test_concentration_helpers_are_normalized_by_what_is_explained():
    c = concentration_stats([50.0, 30.0, 20.0])
    assert (c["top1_pct"], c["top5_pct"], c["hhi"], c["effective_n"]) == (50.0, 100.0, 0.38, 2.63)
    half = concentration_stats([5.0, 3.0, 2.0], explained_value=10.0)                 # a 10-of-100 slice, normalized by itself
    assert half["top1_pct"] == 50.0
    assert concentration_stats([]) ["hhi"] is None


# ── Providers, storage, refresh ───────────────────────────────────────────────

def test_refresh_stores_shared_fund_data_and_is_ttl_guarded():
    _typed("VTI", "ETF")
    prov = FakeProvider({"VTI": _data("VTI", [("AAPL", 0.06)], sectors={"Technology": 0.3}, expense=0.0003)})
    assert fds.refresh_fund_data(ME, ["VTI"], provider=prov) == {"VTI": "fetched"}
    stored = fdb.get_fund_data(repo.find_instrument("VTI")["instrument_id"], "VTI")
    assert stored.holdings[0].symbol == "AAPL" and stored.profile.expense_ratio == 0.0003 and stored.holdings_coverage == pytest.approx(0.06)
    assert fds.refresh_fund_data(ME, ["VTI"], provider=prov) == {"VTI": "fresh"} and prov.calls == ["VTI"]       # not re-fetched
    assert fds.refresh_fund_data(ME, ["VTI"], provider=prov, force=True) == {"VTI": "fetched"} and prov.calls == ["VTI", "VTI"]
    assert "refresh_fund_data" in [e["operation"] for e in repo.get_audit_events(limit=20)]


def test_etf_and_mutual_fund_use_their_own_ttl_settings():
    _typed("ETFX", "ETF"); _typed("MFX", "MUTUAL_FUND")
    prov = FakeProvider({"ETFX": _data("ETFX", [("AAPL", 0.1)]), "MFX": _data("MFX", [("AAPL", 0.1)])})
    fds.refresh_fund_data(ME, ["ETFX", "MFX"], provider=prov)
    later = datetime.now(timezone.utc).replace(microsecond=0) + __import__("datetime").timedelta(hours=30)   # past 24h, inside 168h
    out = fds.refresh_fund_data(ME, ["ETFX", "MFX"], provider=prov, now=later)
    assert out == {"ETFX": "fetched", "MFX": "fresh"}


def test_provider_failures_become_reason_codes_and_never_raise():
    _typed("BOOM", "ETF"); _typed("EMPTY", "ETF")
    prov = FakeProvider({"BOOM": RuntimeError("network down")})
    out = fds.refresh_fund_data(ME, ["BOOM", "EMPTY"], provider=prov)
    assert out == {"BOOM": "no_data", "EMPTY": "no_data"}
    assert fdb.get_fund_profile_row(repo.find_instrument("BOOM")["instrument_id"]) is None              # nothing half-stored
    views = fds.load_fund_views(LOCAL_OWNER, ["BOOM"])
    assert views["BOOM"].source == lt.SRC_NONE                                                           # UNKNOWN, not a default


def test_fetch_happens_outside_any_transaction():
    _typed("VTI", "ETF")
    seen = {}

    class Probe(FakeProvider):
        def fetch(self, ticker):
            c = sqlite3.connect(str(db_module.DB_PATH), timeout=0.2)
            try:
                c.execute("CREATE TABLE IF NOT EXISTS probe (x)")                # needs the write lock: fails if one is held
                c.execute("INSERT INTO probe VALUES (1)"); c.commit()
                seen["free"] = True
            finally:
                c.close()
            return _data(ticker, [("AAPL", 0.1)])
    fds.refresh_fund_data(ME, ["VTI"], provider=Probe({}))
    assert seen == {"free": True}


def test_refresh_recurses_through_a_target_date_fund_with_a_cycle_guard_and_the_depth_setting():
    _typed("TGT2050", "TARGET_DATE_FUND"); _typed("IDX1", "MUTUAL_FUND"); _typed("IDX2", "MUTUAL_FUND")
    table = {"TGT2050": _data("TGT2050", [("IDX1", 0.6), ("IDX2", 0.4)]),
             "IDX1": _data("IDX1", [("AAPL", 0.5), ("TGT2050", 0.5)]),                # points back at its parent
             "IDX2": _data("IDX2", [("MSFT", 1.0)])}
    prov = FakeProvider(table)
    out = fds.refresh_fund_data(ME, ["TGT2050"], provider=prov)
    assert out["TGT2050"] == out["IDX1"] == out["IDX2"] == "fetched" and sorted(prov.calls) == ["IDX1", "IDX2", "TGT2050"]
    stored = fdb.get_fund_data(repo.find_instrument("TGT2050")["instrument_id"], "TGT2050")
    assert [h.holding_is_fund for h in stored.holdings] == [True, True]               # holdings classified as funds

    sdb.update_portfolio_settings(updated_by="admin", lookthrough_max_depth=1)
    prov2 = FakeProvider(table)
    fds.refresh_fund_data(ME, ["TGT2050"], provider=prov2, force=True)
    assert prov2.calls == ["TGT2050"]                                                 # depth 1: the sub-funds are not fetched


# ── Priority: own data > manual > proxy > UNKNOWN ─────────────────────────────

def test_manual_breakdown_beats_proxy_and_proxy_beats_unknown_and_is_labeled_via():
    _hold(ME, "CIT:ACME-2045", 10, 100.0, itype="CIT", proxy="VTI")
    _typed("VTI", "ETF"); _save("VTI", [("AAPL", "Apple", 0.10, False)], classes={"stock": 1.0})
    v = fds.load_fund_views(LOCAL_OWNER, ["CIT:ACME-2045"])["CIT:ACME-2045"]
    assert (v.source, v.via) == (lt.SRC_PROXY, "VTI") and v.holdings[0].symbol == "AAPL"            # "via proxy VTI"

    fds.set_manual_breakdown(ME, "CIT:ACME-2045", {"stock": 60, "bond": 40})
    v = fds.load_fund_views(LOCAL_OWNER, ["CIT:ACME-2045"])["CIT:ACME-2045"]
    assert v.source == lt.SRC_MANUAL and v.via is None and v.asset_class_weights == {"stock": 0.6, "bond": 0.4} and v.holdings == []

    fds.clear_manual_breakdown(ME, "CIT:ACME-2045")
    assert fds.load_fund_views(LOCAL_OWNER, ["CIT:ACME-2045"])["CIT:ACME-2045"].source == lt.SRC_PROXY
    inst = repo.find_instrument("CIT:ACME-2045")
    repo.update_instrument(inst["instrument_id"], proxy_ticker="")
    assert fds.load_fund_views(LOCAL_OWNER, ["CIT:ACME-2045"])["CIT:ACME-2045"].source == lt.SRC_NONE


def test_a_funds_own_data_wins_over_a_manual_breakdown():
    _hold(ME, "VFIAX", 10, 100.0, itype="MUTUAL_FUND")
    _save("VFIAX", [("AAPL", "Apple", 0.07, False)])
    fds.set_manual_breakdown(ME, "VFIAX", {"stock": 100})
    assert fds.load_fund_views(LOCAL_OWNER, ["VFIAX"])["VFIAX"].source == lt.SRC_OWN


def test_manual_breakdown_is_owner_scoped_validated_and_audited():
    other = RequestContext(identity=_mint_identity_for_tests("someone-else"), source="test")
    with repo.unit_of_work() as c:
        c.execute("INSERT INTO portfolios (owner) VALUES (?)", ("someone-else",))
    _hold(ME, "CIT:ACME-2045", 10, 100.0, itype="CIT")
    fds.set_manual_breakdown(ME, "CIT:ACME-2045", {"stock": 70, "bond": 30}, {"Technology": 100}, as_of="2026-09-30")
    assert fds.load_fund_views("someone-else", ["CIT:ACME-2045"])["CIT:ACME-2045"].source == lt.SRC_NONE   # not theirs to see
    with pytest.raises(fds.NotYourFund):
        fds.set_manual_breakdown(other, "CIT:ACME-2045", {"stock": 100})                                   # nor to write
    for bad in ({"stock": 60, "bond": 30}, {"stock": 120, "bond": -20}, {"equities": 100}, {}):
        with pytest.raises(ValueError):
            fds.set_manual_breakdown(ME, "CIT:ACME-2045", bad)
    assert "set_fund_manual_breakdown" in [e["operation"] for e in repo.get_audit_events(limit=20)]
    assert fds.load_fund_views(LOCAL_OWNER, ["CIT:ACME-2045"])["CIT:ACME-2045"].asset_class_weights["stock"] == 0.7   # failures left it alone


# ── Instrument types: STABLE_VALUE, per-type horizons ─────────────────────────

def test_stable_value_is_classified_cash_like_and_never_predicted():
    c = classify_from_info("STBL", {"longName": "Principal Stable Value Fund", "quoteType": "MUTUALFUND"})
    assert (c.instrument_type, c.asset_class) == (InstrumentType.STABLE_VALUE, "FIXED_INCOME")
    assert InstrumentType.STABLE_VALUE in NO_PREDICTION_TYPES and InstrumentType.STABLE_VALUE not in FUND_INSTRUMENT_TYPES
    assert classify_from_info("X", {"longName": "Voya Stable Income CIT"}).instrument_type == InstrumentType.STABLE_VALUE


def test_horizons_per_instrument_type_are_seeded_and_gate_the_pipeline(monkeypatch):
    from tests.test_etf_routing import _run_apex
    h = esdb.get_engine_settings()["instrument_type_horizons"]
    assert h["STOCK"] == [5, 21, 63, 250] and h["MUTUAL_FUND"] == h["TARGET_DATE_FUND"] == [63, 250]
    assert h["CIT"] == h["STABLE_VALUE"] == h["CASH"] == h["MONEY_MARKET"] == []
    assert esdb.horizons_for_instrument_type(None) == h["UNKNOWN"] == [5, 21, 63, 250]

    _typed("AAPL", "STOCK"); _typed("SPY", "ETF"); _typed("VFIAX", "MUTUAL_FUND"); _typed("STBL", "STABLE_VALUE")
    called = _run_apex(monkeypatch, ["AAPL", "SPY", "VFIAX", "STBL"], horizons=(5, 63))
    assert sorted(called) == ["AAPL", "SPY", "VFIAX"]                                  # a stable value fund is never predicted
    with sqlite3.connect(str(db_module.DB_PATH)) as c:
        rows = {(t, d) for t, d in c.execute("SELECT ticker, horizon_days FROM predictions")}
    assert rows == {("AAPL", 5), ("AAPL", 63), ("SPY", 5), ("SPY", 63), ("VFIAX", 63)}   # the mutual fund gets 63d only


def test_a_type_with_no_enabled_scheduled_horizon_is_skipped_and_the_setting_is_editable(monkeypatch, caplog):
    from tests.test_etf_routing import _run_apex
    esdb.update_engine_settings(updated_by="admin", instrument_type_horizons={"ETF": []})
    _typed("SPY", "ETF"); _typed("AAPL", "STOCK")
    called = _run_apex(monkeypatch, ["SPY", "AAPL"], horizons=(5,))
    assert called == ["AAPL"]
    esdb.update_engine_settings(updated_by="admin", instrument_type_horizons={"MUTUAL_FUND": [63]})
    _typed("VFIAX", "MUTUAL_FUND")
    assert _run_apex(monkeypatch, ["VFIAX"], horizons=(5,)) == []                                                                # 5d is not enabled for mutual funds
    with pytest.raises(ValueError):
        esdb.update_engine_settings(updated_by="admin", instrument_type_horizons={"ETF": [7]})
    with pytest.raises(ValueError):
        esdb.update_engine_settings(updated_by="admin", instrument_type_horizons={"WIDGET": [5]})


def test_horizons_and_portfolio_settings_are_admin_only_audited_and_validated():
    with pytest.raises(inv.NotAdmin):
        inv.update_instrument_type_horizons(ME, {"ETF": [5]})
    with pytest.raises(inv.NotAdmin):
        inv.update_portfolio_settings(ME, etf_data_ttl_hours=12)
    auth_users_db.invite_user("me@example.com", LOCAL_OWNER, "admin")
    after = inv.update_instrument_type_horizons(ME, {"ETF": [21]}, reason="test")
    assert after["ETF"] == [21]
    ev = [e for e in repo.get_audit_events(limit=20) if e["operation"] == "update_instrument_type_horizons"][0]
    assert ev["before"]["ETF"] == [5, 21, 63, 250] and ev["after"]["ETF"] == [21]
    assert inv.reset_instrument_type_horizons(ME)["ETF"] == [5, 21, 63, 250]
    out = inv.update_portfolio_settings(ME, etf_data_ttl_hours=12, lookthrough_min_coverage_pct=70, high_expense_ratio_pct=0.75)
    assert (out["etf_data_ttl_hours"], out["lookthrough_min_coverage_pct"], out["high_expense_ratio_pct"]) == (12, 70, 0.75)
    for bad in ({"lookthrough_min_coverage_pct": 120}, {"lookthrough_max_depth": 0}, {"etf_data_ttl_hours": 0}):
        with pytest.raises(ValueError):
            inv.update_portfolio_settings(ME, **bad)


def test_new_portfolio_settings_columns_migrate_onto_the_step1_row_untouched():
    p = Path(db_module.DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(p) as c:                                                       # the Step 1 shape: no fund-data columns
        c.execute("""CREATE TABLE portfolio_settings (id INTEGER PRIMARY KEY CHECK (id = 1),
                     default_emergency_reserve_amount REAL NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                     created_at TEXT NOT NULL, updated_at TEXT NOT NULL, updated_by TEXT)""")
        c.execute("INSERT INTO portfolio_settings VALUES (1, 4500.0, 7, '2026-09-22T00:00:00', '2026-09-23T00:00:00', 'admin')")
    s = sdb.get_portfolio_settings()
    assert (s["default_emergency_reserve_amount"], s["version"], s["updated_by"]) == (4500.0, 7, "admin")
    assert (s["etf_data_ttl_hours"], s["mutual_fund_data_ttl_hours"], s["lookthrough_min_coverage_pct"],
            s["lookthrough_max_depth"], s["high_expense_ratio_pct"], s["health_snapshot_enabled"]) == (24, 168, 80.0, 3, 0.5, 1)
    sdb.get_portfolio_settings()                                                        # idempotent


# ── NAV-aware staleness ───────────────────────────────────────────────────────

_TRADING = lambda d: d.weekday() < 5
_NY = __import__("zoneinfo").ZoneInfo("America/New_York")


def _at(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=_NY)


def test_a_mutual_fund_nav_is_not_stale_until_todays_nav_has_posted():
    tue_morning, tue_5pm, tue_10pm = _at(2026, 10, 6, 10), _at(2026, 10, 6, 17), _at(2026, 10, 6, 22)
    mon, tue = date(2026, 10, 5), date(2026, 10, 6)
    for now in (tue_morning, tue_5pm):                                                  # prior-close NAV is current before ~21:00
        assert is_price_stale(mon, "MUTUAL_FUND", now, is_trading_day=_TRADING) is False
        assert is_price_stale(mon, "TARGET_DATE_FUND", now, is_trading_day=_TRADING) is False
    assert is_price_stale(mon, "MUTUAL_FUND", tue_10pm, is_trading_day=_TRADING) is True    # once it should have posted
    assert is_price_stale(tue, "MUTUAL_FUND", tue_10pm, is_trading_day=_TRADING) is False


def test_stocks_are_judged_against_the_4pm_close_and_weekends_roll_back():
    mon = date(2026, 10, 5)
    assert is_price_stale(mon, "STOCK", _at(2026, 10, 6, 10), is_trading_day=_TRADING) is False
    assert is_price_stale(mon, "ETF", _at(2026, 10, 6, 17), is_trading_day=_TRADING) is True
    assert expected_price_date("MUTUAL_FUND", _at(2026, 10, 10, 12), is_trading_day=_TRADING) == date(2026, 10, 9)   # Saturday
    assert is_price_stale(None, "STOCK", _at(2026, 10, 6, 10), is_trading_day=_TRADING) is None                       # unknown, not fresh
    assert is_price_stale("not-a-date", "STOCK", _at(2026, 10, 6, 10), is_trading_day=_TRADING) is None


# ── Assessment wiring ─────────────────────────────────────────────────────────

def _checks(a):
    return {c.name: c for c in a.limits}


def _fund_portfolio(*, fund_holdings, direct=3000.0, classes=None):
    _hold(ME, "AAPL", 30, direct / 30)
    _hold(ME, "VTI", 10, 100.0, itype="ETF", sector=None)
    _save("VTI", fund_holdings, sectors={"Technology": 1.0}, classes=classes or {"stock": 1.0})


def test_stock_only_portfolio_is_unchanged_by_look_through():
    _hold(ME, "AAPL", 10, 100.0); _hold(ME, "MSFT", 10, 100.0, sector="Technology")
    a = pa.build_portfolio_assessment(ME, "AAPL")
    ex = pa.get_portfolio_exposures(ME)
    assert (ex.lookthrough_applicable, ex.lookthrough_used, ex.lookthrough_issuer_pct) == (False, False, None)
    assert "lookthrough_used" not in a.exposures and pa.RC_LOOKTHROUGH_PARTIAL not in a.evidence_refs["reason_codes"]
    c = _checks(a)
    assert c["issuer_concentration"].detail is None and c["sector_concentration"].detail is None
    assert (c["issuer_concentration"].actual_pct, c["sector_concentration"].actual_pct) == (50.0, 100.0)
    assert [k for k in c] == ["position_concentration", "sector_concentration", "issuer_concentration",
                              "cash_reserve_for_candidate", "leveraged_inverse_exposure", "instrument_type_allowed"]
    assert c["leveraged_inverse_exposure"].actual_pct == 0.0 and c["leveraged_inverse_exposure"].breached is False
    assert (pa.EXPOSURES_VERSION, pa.ASSESSMENT_VERSION) == ("portfolio-exposures/2.1", "portfolio-assessment/3.0")


def test_issuer_and_sector_checks_use_look_through_with_the_split_in_plain_words():
    _fund_portfolio(fund_holdings=[("AAPL", "Apple", 0.5, False), ("MSFT", "Microsoft", 0.5, False)])
    ex = pa.get_portfolio_exposures(ME)
    assert ex.lookthrough_applicable and ex.lookthrough_used and ex.lookthrough_coverage_pct == pytest.approx(100.0)
    k = ex.issuer_of["AAPL"]                                                              # issuers are keyed like the direct check (CIK when known)
    assert ex.lookthrough_issuer_pct[k] == pytest.approx(87.5) and ex.lookthrough_issuer_via_pct[k] == pytest.approx(12.5)
    c = _checks(pa.build_portfolio_assessment(ME, "AAPL"))
    assert c["issuer_concentration"].actual_pct == pytest.approx(87.5) and c["issuer_concentration"].breached is True
    assert c["issuer_concentration"].detail.startswith("87.50% = 75.00% direct + 12.50% via funds")
    assert "look-through" in c["issuer_concentration"].detail and c["issuer_concentration"].reason_code is None
    assert c["sector_concentration"].detail.startswith("100.00% = 75.00% direct + 25.00% via funds")


def test_a_candidate_already_held_through_a_fund_is_not_reported_as_brand_new():
    _fund_portfolio(fund_holdings=[("NVDA", "Nvidia", 1.0, False)])
    c = _checks(pa.build_portfolio_assessment(ME, "NVDA"))
    assert c["issuer_concentration"].actual_pct == pytest.approx(25.0) and "25.00% via funds" in c["issuer_concentration"].detail


def test_partial_look_through_falls_back_to_direct_and_is_never_reported_as_not_breached():
    _fund_portfolio(fund_holdings=[("AAPL", "Apple", 0.05, False)], direct=3000.0)         # top holding only: 5% of the fund known
    ex = pa.get_portfolio_exposures(ME)
    assert ex.lookthrough_applicable and not ex.lookthrough_used and ex.lookthrough_coverage_pct < 80
    assert pa.RC_LOOKTHROUGH_PARTIAL in ex.reason_codes
    a = pa.build_portfolio_assessment(ME, "MSFT")                                        # a candidate nobody holds: 0.0% direct
    c = _checks(a)
    assert c["issuer_concentration"].breached == pa.UNKNOWN                              # 0.0% direct: cannot be called "not breached"
    assert c["sector_concentration"].breached is True                                    # tech is already over the limit directly: still over
    for name in ("issuer_concentration", "sector_concentration"):
        assert c[name].breached is not False and c[name].reason_code == pa.RC_LOOKTHROUGH_PARTIAL
        assert "direct holdings only" in c[name].detail and "may hold more" in c[name].detail
    assert pa.RC_LOOKTHROUGH_PARTIAL in a.evidence_refs["reason_codes"]
    assert a.sizing["status"] == pa.INSUFFICIENT_DATA                                      # no permissive sizing on partial evidence
    held = _checks(pa.build_portfolio_assessment(ME, "AAPL"))["issuer_concentration"]    # already over the limit directly: still a breach
    assert held.breached is True and held.reason_code == pa.RC_LOOKTHROUGH_PARTIAL


def test_the_minimum_coverage_is_a_setting_not_a_constant():
    _fund_portfolio(fund_holdings=[("AAPL", "Apple", 0.05, False)])
    assert not pa.get_portfolio_exposures(ME).lookthrough_used
    sdb.update_portfolio_settings(updated_by="admin", lookthrough_min_coverage_pct=70)
    assert pa.get_portfolio_exposures(ME).lookthrough_used                                 # 77.5% explained now clears the bar


def test_leveraged_inverse_and_allowed_type_checks_are_advisory():
    _hold(ME, "AAPL", 90, 100.0); _hold(ME, "TQQQ", 10, 100.0, itype="ETF", leverage=3.0, sector=None)
    _save("TQQQ", [("AAPL", "Apple", 1.0, False)])
    a = pa.build_portfolio_assessment(ME, "AAPL")
    lev = _checks(a)["leveraged_inverse_exposure"]
    assert (lev.severity, lev.actual_pct, lev.limit_pct, lev.breached) == (pa.SEVERITY_ADVISORY, pytest.approx(10.0), 5.0, True)
    policy = dataclasses.replace(pa.get_effective_policy(ME), allowed_instrument_types=("STOCK",))
    _typed("SPY", "ETF")
    t = _checks(pa.build_portfolio_assessment(ME, "SPY", policy=policy))["instrument_type_allowed"]
    assert (t.severity, t.breached, t.actual_pct) == (pa.SEVERITY_ADVISORY, True, "ETF") and "not allowed" in t.detail
    unknown = _checks(pa.build_portfolio_assessment(ME, "NEWCO"))["instrument_type_allowed"]
    assert unknown.breached == pa.UNKNOWN and unknown.reason_code == pa.RC_INSTRUMENT_TYPE_UNKNOWN
    _typed("SQQQ", "ETF", leverage=-3.0)
    cand = _checks(pa.build_portfolio_assessment(ME, "SQQQ", policy=policy))
    assert cand["leveraged_inverse_exposure"].actual_pct == pytest.approx(10.0 + 2.0)      # the hypothetical 2% add counts


def test_exposures_cache_is_invalidated_by_fresh_fund_data_and_by_a_manual_breakdown():
    _fund_portfolio(fund_holdings=[("AAPL", "Apple", 0.5, False), ("MSFT", "Microsoft", 0.5, False)])
    first = pa.get_portfolio_exposures(ME)
    assert pa.get_portfolio_exposures(ME) is first                                         # same state: cached
    _save("VTI", [("AAPL", "Apple", 1.0, False)], sectors={"Technology": 1.0}, classes={"stock": 1.0},
          fetched_at="2026-10-09T12:00:00+00:00")
    second = pa.get_portfolio_exposures(ME)
    assert second is not first and second.fund_data_version != first.fund_data_version
    assert second.version != first.version
    _hold(ME, "CIT:ACME-2045", 10, 100.0, itype="CIT")
    third = pa.get_portfolio_exposures(ME)
    fds.set_manual_breakdown(ME, "CIT:ACME-2045", {"stock": 100})
    assert pa.get_portfolio_exposures(ME).fund_data_version != third.fund_data_version


# ── Shared fund_context: market data only ─────────────────────────────────────

def test_fund_context_summarises_shared_fund_data_deterministically():
    _typed("VTI", "ETF")
    _save("VTI", [("AAPL", "Apple", 0.06, False), ("MSFT", "Microsoft", 0.05, False)], sectors={"Technology": 0.4, "Energy": 0.1},
          classes={"stock": 0.99, "cash": 0.01}, expense=0.0003)
    ctx = get_fund_context("VTI")
    assert ctx["status"] == "OK" and ctx["expense_ratio_pct"] == 0.03 and ctx["holdings_coverage_pct"] == 11.0
    assert ctx["top_holdings"][0] == {"symbol": "AAPL", "name": "Apple", "weight_pct": 6.0}
    assert ctx["sector_mix_pct"] == {"Technology": 40.0, "Energy": 10.0} and ctx["asset_class_mix_pct"]["stock"] == 99.0
    assert ctx["concentration_of_known_holdings"]["top10_pct"] == 100.0 and ctx["concentration_of_known_holdings"]["effective_n"] == pytest.approx(1.98, abs=0.01)
    assert get_fund_context("VTI") == ctx and get_fund_context("NOPE") == {"status": "UNKNOWN", "reason_code": "FUND_DATA_UNAVAILABLE"}


def test_fund_context_never_carries_anything_private_proxy_manual_or_holdings():
    _hold(ME, "CIT:ACME-2045", 1234, 100.0, itype="CIT", proxy="VTI")
    _typed("VTI", "ETF"); _save("VTI", [("AAPL", "Apple", 0.06, False)], classes={"stock": 1.0})
    fds.set_manual_breakdown(ME, "CIT:ACME-2045", {"stock": 61, "bond": 39})
    assert get_fund_context("CIT:ACME-2045") == {"status": "UNKNOWN", "reason_code": "FUND_DATA_UNAVAILABLE"}   # proxy and manual stay private
    text = json.dumps(get_fund_context("VTI"))
    assert "1234" not in text and "123400" not in text and "ACME" not in text and "61" not in text

    from portfolio_agent.tools.market_context import build_market_context
    loaders = {"instrument": lambda t: {"instrument_type": "ETF", "asset_class": "US_EQUITY", "leverage_factor": 1.0},
               "fund_context": get_fund_context, "history": lambda t: [], "news": lambda t: [], "macro": lambda: {},
               "fundamentals": lambda t: None, "research": lambda t: None, "valuation": lambda t: None,
               "technical": lambda t: None, "failure_patterns": lambda: []}
    mc = build_market_context("VTI", [5], loaders=loaders, macro_snapshot={})
    assert mc.payload["fund_context"]["top_holdings"][0]["symbol"] == "AAPL"


def test_the_shared_forecast_path_does_not_import_private_modules():
    for mod in ("instrument_reference.py", "market_context.py", "fund_data_db.py"):
        src = (ROOT / "portfolio_agent" / "tools" / mod).read_text()
        for private in ("fund_manual_db", "fund_data_service", "investor_profile_db", "investor_service", "user_profile_db"):
            assert f"import {private}" not in src and f"{private} import" not in src, f"{mod} reaches {private}"
    code = ("import sys\n"
            "from portfolio_agent.tools.instrument_reference import get_fund_context\n"
            "import portfolio_agent.tools.market_context\n"
            "bad = [m for m in ('fund_manual_db','fund_data_service','investor_profile_db','investor_service','user_profile_db') "
            "if 'portfolio_agent.tools.'+m in sys.modules or 'portfolio_agent.services.'+m in sys.modules]\n"
            "print('LEAK', bad) if bad else print('CLEAN')\n")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert out.stdout.strip().endswith("CLEAN"), out.stdout + out.stderr
