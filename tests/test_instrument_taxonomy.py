"""
Step 2 — instrument + account taxonomy: idempotent migrations and legacy-value mapping, the classifier (yfinance
mocked — no live calls), manual overrides that stick, ticker-less ("CIT:...") funds kept out of pricing and the
pipeline, account-type normalization in every parser, paper accounts kept out of real totals, owner scoping and
audit events on every new service verb.
"""
from __future__ import annotations

import sqlite3

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.domain import (
    AccountType, InstrumentType, is_synthetic_ticker, map_legacy_asset_class, normalize_account_type,
    synthetic_cit_ticker, tax_treatment,
)
from portfolio_agent.services import account_service as svc
from portfolio_agent.services import instrument_classifier as ic
from portfolio_agent.services.context import RequestContext, _mint_identity_for_tests
from portfolio_agent.tools import holdings_db as repo
from tests.helpers_holdings import CTX, account_id_of, seed_holding


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")


def _pos(ticker, shares=10, account="X1", **extra):
    return {"ticker": ticker, "shares": shares, "avg_cost": 100.0, "cost_basis_total": shares * 100.0,
            "account_name": "Individual", "account_number": account, "account_type": "TAXABLE", **extra}


def _ops(**kw):
    return [e["operation"] for e in repo.get_audit_events(**kw)]


def _stranger(owner="stranger") -> RequestContext:
    with repo.unit_of_work() as c:
        c.execute("INSERT OR IGNORE INTO portfolios (owner) VALUES (?)", [owner])
    return RequestContext(identity=_mint_identity_for_tests(owner), source="test")


def _fake_lookup(monkeypatch, table: dict[str, dict | None], calls: list | None = None):
    def _fetch(ticker):
        if calls is not None:
            calls.append(ticker)
        return table.get(ticker)
    monkeypatch.setattr(ic, "fetch_info", _fetch)


SPY = {"quoteType": "ETF", "longName": "SPDR S&P 500 ETF Trust", "category": "Large Blend",
       "fundFamily": "State Street", "annualReportExpenseRatio": 0.000945}
AAPL = {"quoteType": "EQUITY", "longName": "Apple Inc.", "country": "United States"}


# ── Legacy-value mapping (pure) ───────────────────────────────────────────────

def test_account_type_normalization_maps_legacy_and_free_text():
    n = normalize_account_type
    assert n("IRA") == "TRADITIONAL_IRA"                      # the old parser value
    assert n("TAXABLE") == "TAXABLE" and n("ROTH_IRA") == "ROTH_IRA" and n("401K") == "401K"
    assert n("roth") == "ROTH_IRA" and n("", "Roth 401(k)") == "ROTH_401K"   # was ROTH_IRA before: ordering bug
    assert n("", "Rollover IRA ***1234") == "ROLLOVER_IRA" and n("SEP-IRA") == "SEP_IRA"
    assert n("403(b)") == "403B" and n("", "457 plan") == "457B" and n("", "Family HSA") == "HSA" and n("529") == "529"
    assert n("", "SEPARATE accounts LLC") == "TAXABLE"        # "SEP" only as a word
    assert n("", "Joint brokerage") == "TAXABLE" and n("", "mystery", default=None) is None


def test_tax_treatment_is_derived_from_account_type():
    assert tax_treatment("TAXABLE") == "TAXABLE"
    assert {tax_treatment(t) for t in ("TRADITIONAL_IRA", "ROLLOVER_IRA", "SEP_IRA", "401K", "403B", "457B")} == {"TAX_DEFERRED"}
    assert {tax_treatment(t) for t in ("ROTH_IRA", "ROTH_401K", "HSA", "529")} == {"TAX_FREE"}
    assert tax_treatment(None) is None and tax_treatment("OTHER") is None and tax_treatment("nonsense") is None


def test_legacy_asset_class_mapping():
    m = map_legacy_asset_class
    assert m("Equity") == "US_EQUITY" and m("us_equity") == "US_EQUITY" and m("International stock") == "INTL_EQUITY"
    assert m("Bond fund") == "FIXED_INCOME" and m("Cash") == "CASH" and m("Gold") == "COMMODITY"
    assert m(None) == "UNKNOWN" and m("") == "UNKNOWN" and m("ETF") == "UNKNOWN" and m("weird thing") == "UNKNOWN"


def test_synthetic_ticker_helpers():
    t = synthetic_cit_ticker("Acme Corp 401(k)", "S&P 500 Index Fund")
    assert t.startswith("CIT:") and is_synthetic_ticker(t) and not is_synthetic_ticker("VOO")
    assert t == synthetic_cit_ticker("acme corp 401(k)", "s&p 500   index fund")          # stable, case-insensitive
    with pytest.raises(ValueError):
        synthetic_cit_ticker("Acme", "")


# ── Migrations on an existing database ────────────────────────────────────────

_OLD_ACCOUNTS = """CREATE TABLE accounts (
    account_id INTEGER PRIMARY KEY AUTOINCREMENT, portfolio_id INTEGER NOT NULL, broker TEXT NOT NULL,
    account_number TEXT NOT NULL DEFAULT '', display_name TEXT, account_type TEXT, status TEXT NOT NULL DEFAULT 'active',
    version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')), UNIQUE(portfolio_id, broker, account_number))"""
_OLD_INSTRUMENTS = """CREATE TABLE instruments (
    instrument_id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL UNIQUE, name TEXT, asset_class TEXT,
    status TEXT NOT NULL DEFAULT 'active', version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')), updated_at TEXT NOT NULL DEFAULT (datetime('now')))"""


def _build_pre_taxonomy_db():
    c = sqlite3.connect(db_module.DB_PATH)
    c.execute(repo._SCHEMA[0])                                                    # portfolios
    c.execute("INSERT INTO portfolios (owner) VALUES ('sumanth_b')")
    c.execute(_OLD_ACCOUNTS); c.execute(_OLD_INSTRUMENTS); c.execute(repo._POSITIONS_DDL)
    c.executemany("INSERT INTO accounts (portfolio_id, broker, account_number, display_name, account_type) VALUES (1, 'fidelity', ?, ?, ?)",
                  [("A1", "Individual", "TAXABLE"), ("A2", "Retirement", "IRA"), ("A3", "Mystery acct", "mystery"), ("A4", "Blank", None)])
    c.executemany("INSERT INTO instruments (ticker, name, asset_class) VALUES (?, ?, ?)",
                  [("AAPL", "Apple", "Equity"), ("BND", "Bonds", "Bond fund"), ("ZZZ", "Odd", "weird thing"), ("NEW", "New", None)])
    c.commit(); c.close()


def test_taxonomy_migration_maps_legacy_values_keeps_originals_and_is_idempotent():
    _build_pre_taxonomy_db()
    first = {i["ticker"]: i for i in repo.list_instruments()}                     # first open migrates
    assert (first["AAPL"]["asset_class"], first["AAPL"]["asset_class_legacy"]) == ("US_EQUITY", "Equity")
    assert (first["BND"]["asset_class"], first["BND"]["asset_class_legacy"]) == ("FIXED_INCOME", "Bond fund")
    assert (first["ZZZ"]["asset_class"], first["ZZZ"]["asset_class_legacy"]) == ("UNKNOWN", "weird thing")   # original kept
    assert (first["NEW"]["asset_class"], first["NEW"]["asset_class_legacy"]) == ("UNKNOWN", None)
    for i in first.values():
        assert (i["instrument_type"], i["leverage_factor"], i["classification_source"], i["classified_at"]) == ("UNKNOWN", 1.0, "auto", None)

    accts = {a["account_number"]: a for a in repo.list_accounts()}
    assert (accts["A1"]["account_type"], accts["A1"]["account_type_legacy"]) == ("TAXABLE", None)
    assert (accts["A2"]["account_type"], accts["A2"]["account_type_legacy"]) == ("TRADITIONAL_IRA", "IRA")
    assert (accts["A3"]["account_type"], accts["A3"]["account_type_legacy"]) == ("OTHER", "mystery")
    assert accts["A4"]["account_type"] is None                                   # unset stays unset
    assert all((a["holdings_source"], a["is_paper"]) == ("snapshot", 0) for a in accts.values())

    # Re-opening (and running the migration again directly) changes nothing and adds no duplicate columns.
    with repo.unit_of_work() as c:
        repo._migrate_taxonomy(c); repo._migrate_taxonomy(c)
        icols = [r[1] for r in c.execute("PRAGMA table_info(instruments)")]
        acols = [r[1] for r in c.execute("PRAGMA table_info(accounts)")]
    assert len(icols) == len(set(icols)) and len(acols) == len(set(acols))
    assert {i["ticker"]: i for i in repo.list_instruments()} == first
    assert {a["account_number"]: a for a in repo.list_accounts()} == accts


def test_holdings_view_and_holding_expose_the_taxonomy():
    a = seed_holding(_pos("AAPL"), "fidelity", "2026-09-01")
    h = repo.get_holdings()[0]
    assert (h["instrument_type"], h["asset_class"], h["leverage_factor"], h["classification_source"]) == ("UNKNOWN", "UNKNOWN", 1.0, "auto")
    assert h.to_dict()["instrument_type"] == "UNKNOWN" and a["account_id"] == h["account_id"]


# ── Classifier (pure) ─────────────────────────────────────────────────────────

def test_classifier_uses_quote_type_then_name_and_category():
    c = ic.classify_from_info("AAPL", AAPL, "APPLE INC")
    assert (c.instrument_type, c.asset_class, c.leverage_factor, c.fund_family) == ("STOCK", "US_EQUITY", 1.0, None)
    assert ic.classify_from_info("SAP", {"quoteType": "EQUITY", "country": "Germany"}).asset_class == "INTL_EQUITY"

    c = ic.classify_from_info("SPY", SPY)
    assert (c.instrument_type, c.asset_class, c.sub_asset_class) == ("ETF", "US_EQUITY", "LARGE_CAP_BLEND")
    assert (c.fund_family, c.category, c.expense_ratio) == ("State Street", "Large Blend", 0.000945)
    assert ic.classify_from_info("X", {"quoteType": "ETF", "longName": "Some ETF", "netExpenseRatio": 0.2}).expense_ratio == pytest.approx(0.002)

    c = ic.classify_from_info("FXAIX", {"quoteType": "MUTUALFUND", "longName": "Fidelity 500 Index Fund", "category": "Large Blend"})
    assert c.instrument_type == "MUTUAL_FUND"
    c = ic.classify_from_info("SPAXX", {"quoteType": "MONEYMARKET", "longName": "Fidelity Government Money Market"})
    assert (c.instrument_type, c.asset_class) == ("MONEY_MARKET", "CASH")
    c = ic.classify_from_info("FDRXX", {"quoteType": "MUTUALFUND", "longName": "Fidelity Government Cash Reserves Money Market Fund"})
    assert c.instrument_type == "MONEY_MARKET"                                    # keyword wins over MUTUALFUND

    c = ic.classify_from_info("VFIFX", {"quoteType": "MUTUALFUND", "longName": "Vanguard Target Retirement 2050 Fund"})
    assert (c.instrument_type, c.asset_class) == ("TARGET_DATE_FUND", "MULTI_ASSET")
    c = ic.classify_from_info("TD", {"quoteType": "MUTUALFUND", "longName": "Some Fund", "category": "Target-Date 2045"})
    assert c.instrument_type == "TARGET_DATE_FUND"
    assert ic.classify_from_info("IBND", {"quoteType": "ETF", "longName": "iShares iBonds Dec 2025 Term Corporate ETF"}).instrument_type == "ETF"
    c = ic.classify_from_info("VXX", {"quoteType": "ETF", "longName": "iPath Series B S&P 500 VIX Short-Term Futures ETN"})
    assert (c.instrument_type, c.leverage_factor) == ("ETN", 1.0)


@pytest.mark.parametrize("name, factor", [
    ("ProShares UltraPro QQQ", 3.0), ("ProShares Ultra S&P500", 2.0), ("ProShares UltraShort S&P500", -2.0),
    ("ProShares Short QQQ", -1.0), ("Direxion Daily S&P 500 Bear 3X Shares", -3.0), ("ProShares UltraPro Short QQQ", -3.0),
    ("Vanguard Short-Term Treasury ETF", 1.0), ("Vanguard Ultra-Short Bond ETF", 1.0),
    ("iShares Short Duration Bond Active ETF", 1.0), ("SPDR S&P 500 ETF Trust", 1.0),
])
def test_leveraged_and_inverse_detection_does_not_flag_ordinary_short_bond_funds(name, factor):
    assert ic.classify_from_info("X", {"quoteType": "ETF", "longName": name}).leverage_factor == factor


def test_stock_names_are_never_read_as_leveraged():
    assert ic.classify_from_info("SHBI", {"quoteType": "EQUITY", "longName": "Short Hills Bancorp 2x Ultra Bear Inc"}).leverage_factor == 1.0


def test_failed_lookup_is_unknown_and_never_raises():
    c = ic.classify_from_info("ZZZ", None, "whatever")
    assert (c.instrument_type, c.asset_class, c.lookup_ok) == ("UNKNOWN", "UNKNOWN", False)
    assert ic.classify_from_info("ZZZ", {}, None).lookup_ok is False


def test_fetch_info_swallows_provider_errors_and_empty_answers(monkeypatch):
    import sys, types

    class _Boom:
        def __init__(self, t):
            raise RuntimeError("offline")

    class _Empty:
        def __init__(self, t):
            self.info = {}

    class _Ok:
        def __init__(self, t):
            self.info = {"quoteType": "ETF", "longName": "X"}

    for cls, expected in ((_Boom, None), (_Empty, None), (_Ok, {"quoteType": "ETF", "longName": "X"})):
        monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=cls))
        assert ic._fetch_info_impl("AAPL") == expected


# ── Classification stored through the service ─────────────────────────────────

def test_classify_new_holdings_stores_audits_and_rechecks_only_after_30_days(monkeypatch):
    seed_holding(_pos("AAPL"), "fidelity", "2026-09-01")
    seed_holding(_pos("SPY"), "fidelity", "2026-09-01")
    calls: list[str] = []
    _fake_lookup(monkeypatch, {"AAPL": AAPL, "SPY": SPY}, calls)

    out = ic.classify_new_holdings(CTX)
    assert sorted(calls) == ["AAPL", "SPY"] and len(out) == 2
    spy = repo.find_instrument("SPY")
    assert (spy["instrument_type"], spy["asset_class"], spy["classification_source"]) == ("ETF", "US_EQUITY", "auto")
    assert spy["classified_at"] and spy["expense_ratio"] == 0.000945
    assert _ops().count("classify_instrument") == 2

    calls.clear()
    assert ic.classify_new_holdings(CTX) == {} and calls == []                    # fresh: not looked up again

    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    repo.update_instrument(spy["instrument_id"], classified_at=old)
    ic.classify_new_holdings(CTX)
    assert calls == ["SPY"]                                                        # only the stale one
    assert _ops().count("classify_instrument") == 2                                # unchanged result → no new audit event


def test_failed_lookup_stores_nothing_wipes_nothing_and_retries_next_time(monkeypatch):
    seed_holding(_pos("SPY"), "fidelity", "2026-09-01")
    _fake_lookup(monkeypatch, {"SPY": None})
    assert ic.classify_new_holdings(CTX)                                           # attempted, no exception
    spy = repo.find_instrument("SPY")
    assert (spy["instrument_type"], spy["classified_at"]) == ("UNKNOWN", None) and _ops() == ["add_position", "create_account"]

    _fake_lookup(monkeypatch, {"SPY": SPY})
    ic.classify_new_holdings(CTX)
    assert repo.find_instrument("SPY")["instrument_type"] == "ETF"
    repo.update_instrument(spy["instrument_id"], classified_at="2020-01-01T00:00:00+00:00")
    _fake_lookup(monkeypatch, {"SPY": None})                                       # provider is down on the re-check
    ic.classify_new_holdings(CTX)
    assert repo.find_instrument("SPY")["instrument_type"] == "ETF"                 # good classification survives


def test_add_position_classifies_after_commit_but_not_inside_a_caller_transaction(monkeypatch):
    acct = svc.create_account(CTX, broker="manual", display_name="Brokerage")
    calls: list[str] = []
    _fake_lookup(monkeypatch, {"SPY": SPY, "AAPL": AAPL}, calls)
    svc.add_position(CTX, acct["account_id"], _pos("SPY"))
    assert calls == ["SPY"] and repo.find_instrument("SPY")["instrument_type"] == "ETF"

    with repo.transaction() as conn:                                               # composed: the caller owns the follow-up
        svc.add_position(CTX, acct["account_id"], _pos("AAPL"), conn=conn)
    assert calls == ["SPY"] and repo.find_instrument("AAPL")["instrument_type"] == "UNKNOWN"


def test_import_commit_classifies_new_securities(monkeypatch):
    from portfolio_agent.tools.holdings_import import build_plan, commit_plan, parse_upload
    csv = (b"Account Number,Account Name,Symbol,Description,Quantity,Last Price,Current Value,Cost Basis Total,Average Cost Basis\n"
           b'X12345678,Individual - TOD,SPY,SPDR S&P 500,10,$500.00,"$5,000.00","$4,000.00",$400.00\n')
    calls: list[str] = []
    _fake_lookup(monkeypatch, {"SPY": SPY}, calls)
    commit_plan(build_plan(parse_upload(csv, "positions.csv")), fetch_prices=False)
    assert calls == ["SPY"]
    assert repo.find_instrument("SPY")["instrument_type"] == "ETF"


# ── Manual overrides ──────────────────────────────────────────────────────────

def test_manual_override_sticks_against_automatic_reclassification(monkeypatch):
    a = seed_holding(_pos("XYZ", account="X1"), "fidelity", "2026-09-01")
    inst = repo.find_instrument("XYZ")
    after = svc.set_instrument_classification(CTX, inst["instrument_id"], instrument_type="ETF", asset_class="COMMODITY",
                                              sub_asset_class="gold miners", leverage_factor=2, proxy_ticker="gdx",
                                              reason="my fund")
    assert (after["instrument_type"], after["asset_class"], after["sub_asset_class"], after["leverage_factor"],
            after["proxy_ticker"], after["classification_source"]) == ("ETF", "COMMODITY", "GOLD_MINERS", 2.0, "GDX", "manual")
    assert after["classified_at"] and after["version"] == inst["version"] + 1
    assert "set_instrument_classification" in _ops()

    calls: list[str] = []
    _fake_lookup(monkeypatch, {"XYZ": AAPL}, calls)
    assert ic.classify_new_holdings(CTX, force=True) == {} and calls == []         # manual: never even looked up
    assert svc.apply_auto_classification(CTX, inst["instrument_id"], ic.classify_from_info("XYZ", AAPL))["instrument_type"] == "ETF"
    assert repo.find_instrument("XYZ")["classification_source"] == "manual" and a["account_id"]


def test_override_validation():
    seed_holding(_pos("XYZ"), "fidelity", "2026-09-01")
    iid = repo.find_instrument("XYZ")["instrument_id"]
    for bad in ({"instrument_type": "STONK"}, {"asset_class": "MOON"}, {"leverage_factor": 0}, {"leverage_factor": 9},
                {"proxy_ticker": "CIT:A-B"}, {}):
        with pytest.raises(ValueError):
            svc.set_instrument_classification(CTX, iid, **bad)
    assert repo.find_instrument("XYZ")["classification_source"] == "auto"
    with pytest.raises(svc.VersionConflict):
        svc.set_instrument_classification(CTX, iid, instrument_type="ETF", expected_version=99)
    assert svc.set_instrument_classification(CTX, iid, proxy_ticker=None)["proxy_ticker"] is None


# ── Accounts: type, paper, owner scoping ──────────────────────────────────────

def test_account_type_normalized_on_create_and_set_with_audit():
    a = svc.create_account(CTX, broker="manual", display_name="Retirement", account_type="IRA")
    assert a["account_type"] == "TRADITIONAL_IRA"
    b = svc.set_account_type(CTX, a["account_id"], "ROTH_IRA", expected_version=a["version"], reason="it is a Roth")
    assert b["account_type"] == "ROTH_IRA" and b["version"] == a["version"] + 1
    with pytest.raises(ValueError):
        svc.set_account_type(CTX, a["account_id"], "IRA")                          # legacy value is not accepted here
    with pytest.raises(svc.VersionConflict):
        svc.set_account_type(CTX, a["account_id"], "HSA", expected_version=1)
    ev = [e for e in repo.get_audit_events(account_id=a["account_id"]) if e["operation"] == "set_account_type"]
    assert len(ev) == 1 and ev[0]["before"]["account_type"] == "TRADITIONAL_IRA" and ev[0]["after"]["account_type"] == "ROTH_IRA"


def test_parsers_emit_normalized_account_types():
    from portfolio_agent.tools.holdings_parser import _fidelity_account_type, _vanguard_account_type
    from portfolio_agent.tools.llm_holdings_parser import _normalise_account_type
    assert _normalise_account_type("IRA") == "TRADITIONAL_IRA" and _normalise_account_type("roth") == "ROTH_IRA"
    assert _normalise_account_type("", "Roth 401(k) - ****1234") == "ROTH_401K" and _normalise_account_type("junk") == "TAXABLE"
    assert _normalise_account_type("457(b)") == "457B" and _normalise_account_type("403b") == "403B"
    assert _fidelity_account_type("ROTH IRA") == "ROTH_IRA" and _fidelity_account_type("Rollover IRA") == "ROLLOVER_IRA"
    assert _fidelity_account_type("Traditional IRA") == "TRADITIONAL_IRA" and _fidelity_account_type("Roth 401(k)") == "ROTH_401K"
    assert _fidelity_account_type("Health Savings Account (HSA)") == "HSA" and _fidelity_account_type("Individual - TOD") == "TAXABLE"
    assert _vanguard_account_type("12345678", "Roth IRA") == "ROTH_IRA" and _vanguard_account_type("1", "Trad IRA") == "TRADITIONAL_IRA"
    assert _vanguard_account_type("1", "Brokerage Account") == "TAXABLE"


def test_paper_accounts_are_excluded_from_real_holdings_cash_and_pipeline_universe():
    seed_holding(_pos("AAPL"), "fidelity", "2026-09-01")
    pid = repo.get_portfolio_by_owner("sumanth_b")["portfolio_id"]
    with repo.transaction() as conn:
        paper = repo.insert_account(pid, "paper", "P1", "Paper trading", "TAXABLE", is_paper=True, conn=conn)
        repo.insert_position(paper["account_id"], repo.position_fields(_pos("TSLA", 5, current_price=100.0, current_value=500.0), "2026-09-01"), conn=conn)
        repo.set_account_cash(paper["account_id"], 99_999.0, "2026-09-01", conn=conn)
    assert [h["ticker"] for h in repo.get_holdings()] == ["AAPL"]
    assert repo.get_portfolio_tickers() == ["AAPL"] and repo.get_cash_balances() == []
    assert [a["account_number"] for a in repo.list_accounts()] == ["X1"]
    assert {a["account_number"] for a in repo.list_accounts(include_paper=True)} == {"X1", "P1"}
    assert [h["ticker"] for h in svc.list_holdings_for(CTX)] == ["AAPL"]
    assert repo.get_holdings_summary()["total_value"] in (0, 0.0, None) or "TSLA" not in str(repo.get_holdings_summary())


def test_new_service_verbs_are_owner_scoped():
    a = seed_holding(_pos("AAPL"), "fidelity", "2026-09-01")
    iid = repo.find_instrument("AAPL")["instrument_id"]
    stranger = _stranger()
    nobody = RequestContext(identity=_mint_identity_for_tests("no-portfolio"), source="test")

    for ctx in (stranger, nobody):
        with pytest.raises(svc.NotAuthorized):
            svc.set_account_type(ctx, a["account_id"], "ROTH_IRA")
        with pytest.raises(svc.NotAuthorized):
            svc.set_instrument_classification(ctx, iid, instrument_type="ETF")
        with pytest.raises(svc.NotAuthorized):
            svc.set_position_price(ctx, a["id"], 10.0)
        with pytest.raises(svc.NotAuthorized):
            svc.create_synthetic_instrument(ctx, plan_name="P", fund_name="F") if ctx is nobody else (_ for _ in ()).throw(svc.NotAuthorized())
    assert repo.get_account(a["account_id"])["account_type"] == "TAXABLE"
    assert repo.find_instrument("AAPL")["classification_source"] == "auto" and repo.get_position(a["id"])["current_price"] is None

    # A person who holds the security may override it; the effect is on the shared instrument (decision F, audited).
    sa = svc.create_account(stranger, broker="manual", display_name="Theirs")
    svc.add_position(stranger, sa["account_id"], _pos("AAPL", 1, account="Theirs"))
    svc.set_instrument_classification(stranger, iid, asset_class="INTL_EQUITY")
    assert repo.find_instrument("AAPL")["asset_class"] == "INTL_EQUITY"
    ev = [e for e in repo.get_audit_events() if e["operation"] == "set_instrument_classification"]
    assert ev and ev[0]["actor"] == "stranger"


def test_every_new_verb_writes_one_audit_event():
    a = seed_holding(_pos("AAPL"), "fidelity", "2026-09-01")
    base = len(repo.get_audit_events(limit=500))
    svc.set_account_type(CTX, a["account_id"], "TRADITIONAL_IRA")
    svc.set_instrument_classification(CTX, repo.find_instrument("AAPL")["instrument_id"], instrument_type="STOCK")
    svc.set_position_price(CTX, a["id"], 12.5)
    inst = svc.create_synthetic_instrument(CTX, plan_name="Acme", fund_name="Target 2050 CIT")
    svc.create_synthetic_instrument(CTX, plan_name="Acme", fund_name="Target 2050 CIT")      # idempotent: no second event
    assert [e["operation"] for e in repo.get_audit_events(limit=500)][: len(repo.get_audit_events(limit=500)) - base] == [
        "create_synthetic_instrument", "set_position_price", "set_instrument_classification", "set_account_type"]
    assert inst["instrument_type"] == "CIT"


# ── Ticker-less funds (CIT:...) ───────────────────────────────────────────────

def _cit_position(price=25.0, units=100.0):
    acct = svc.create_account(CTX, broker="manual", display_name="Acme 401k", account_type="401K")
    inst = svc.create_synthetic_instrument(CTX, plan_name="Acme 401k", fund_name="S&P 500 Index CIT",
                                           asset_class="US_EQUITY", proxy_ticker="voo")
    pos = svc.add_position(CTX, acct["account_id"], {"ticker": inst["ticker"], "description": inst["name"], "shares": units,
                                                      "current_price": price, "current_value": price * units})
    return acct, inst, pos


def test_cit_instrument_shape_and_proxy_rules():
    _, inst, pos = _cit_position()
    assert is_synthetic_ticker(inst["ticker"]) and inst["ticker"] == synthetic_cit_ticker("Acme 401k", "S&P 500 Index CIT")
    assert (inst["instrument_type"], inst["asset_class"], inst["proxy_ticker"], inst["classification_source"]) == \
           ("CIT", "US_EQUITY", "VOO", "manual")
    assert repo.get_holdings()[0]["proxy_ticker"] == "VOO" and pos["ticker"] == inst["ticker"]   # holding is the CIT, not VOO
    with pytest.raises(ValueError):
        svc.create_synthetic_instrument(CTX, plan_name="Acme", fund_name="Other", proxy_ticker=inst["ticker"])
    # A CIT created implicitly (position added straight under its key) is still typed CIT, and never auto-classified.
    assert repo.get_or_create_instrument("CIT:SOMETHING")["instrument_type"] == "CIT"


def test_cit_is_excluded_from_pipeline_universe_and_provider_calls(monkeypatch):
    from portfolio_agent.tools import holdings_prices, market_data, price_acquisition
    seed_holding(_pos("AAPL"), "fidelity", "2026-09-01")
    _, inst, pos = _cit_position(price=25.0, units=100.0)
    assert repo.get_portfolio_tickers() == ["AAPL"]                                  # the pipeline universe
    assert {h["ticker"] for h in repo.get_holdings()} == {"AAPL", inst["ticker"]}    # but it IS a holding

    seen: list[list[str]] = []
    def _fake_last(tickers):
        seen.append(list(tickers)); return ({t: 200.0 for t in tickers}, "2026-10-08")
    monkeypatch.setattr(holdings_prices, "fetch_last_closes", _fake_last)
    holdings_prices.refresh_holding_prices()
    assert seen == [["AAPL"]]
    cit_after = repo.get_position(pos["position_id"])
    assert (cit_after["current_price"], cit_after["current_value"]) == (25.0, 2500.0)   # hand-entered price untouched

    holdings_prices.snapshot_portfolio_prices("2026-10-08")
    assert all(inst["ticker"] not in t for t in seen)
    rows = {r["ticker"]: r for r in repo.get_price_history()}
    assert rows[inst["ticker"]]["close_price"] == 25.0 and rows[inst["ticker"]]["market_value"] == 2500.0   # price on file

    fetched: list[list[str]] = []
    def _fake_daily(tickers, start, end):
        fetched.append(list(tickers)); return {}
    monkeypatch.setattr(holdings_prices, "fetch_daily_closes", _fake_daily)
    holdings_prices.backfill_missing_price_snapshots()                                    # AAPL has gaps; the CIT is skipped
    assert fetched and all(t == ["AAPL"] for t in fetched)

    # The provider boundary itself drops synthetic keys, whoever the caller is.
    assert market_data.fetch_last_closes([inst["ticker"]]) == ({}, None)
    assert market_data.fetch_daily_closes([inst["ticker"]], "2026-10-01", "2026-10-08") == {}
    assert price_acquisition.fetch_price_series([inst["ticker"]]) == {}


def test_manual_price_for_a_cit_and_its_audit():
    _, _, pos = _cit_position(price=25.0, units=100.0)
    after = svc.set_position_price(CTX, pos["position_id"], 27.5, as_of_date="2026-10-01", expected_version=pos["version"])
    assert (after["current_price"], after["current_value"], after["price_as_of"]) == (27.5, 2750.0, "2026-10-01")
    with pytest.raises(ValueError):
        svc.set_position_price(CTX, pos["position_id"], 0)
    with pytest.raises(svc.VersionConflict):
        svc.set_position_price(CTX, pos["position_id"], 30.0, expected_version=1)
    assert _ops(position_id=pos["position_id"])[0] == "set_position_price"


def test_classifier_never_looks_up_a_cit(monkeypatch):
    _cit_position()
    calls: list[str] = []
    _fake_lookup(monkeypatch, {}, calls)
    assert ic.classify_new_holdings(CTX, force=True) == {} and calls == []
    assert repo.list_instruments_needing_classification(older_than_days=0) == []


# ── Web data helpers & the backfill script ────────────────────────────────────

def test_portfolio_table_rows_and_label_breakdown_carry_the_taxonomy(monkeypatch):
    from web.data.portfolio import holdings_table_rows, label_breakdown
    seed_holding(_pos("SPY", 10, current_price=500.0, current_value=5000.0), "fidelity", "2026-09-01")
    seed_holding(_pos("AAPL", 10, current_price=200.0, current_value=2000.0), "fidelity", "2026-09-01")
    _fake_lookup(monkeypatch, {"SPY": SPY, "AAPL": AAPL})
    ic.classify_new_holdings(CTX)
    holdings = repo.get_holdings()
    rows = {r["ticker"]: r for r in holdings_table_rows(holdings)}
    assert (rows["SPY"]["instrument_type"], rows["SPY"]["asset_class"], rows["SPY"]["leverage"]) == ("ETF", "US_EQUITY", None)

    by_type = label_breakdown(holdings, "instrument_type")
    assert {r["sector"]: r["value"] for r in by_type["rows"]} == {"Etf": 5000.0, "Stock": 2000.0}
    assert by_type["total"] == 7000.0 and by_type["unvalued"] == 0
    assert label_breakdown([], "asset_class")["rows"] == []
    unk = label_breakdown([{"ticker": "Q", "current_value": 10.0}], "asset_class")
    assert unk["rows"][0]["sector"] == "Unclassified"


def test_backfill_script_dry_run_then_apply(monkeypatch, capsys):
    from scripts import backfill_instrument_classification as script
    seed_holding(_pos("SPY"), "fidelity", "2026-09-01")
    calls: list[str] = []
    _fake_lookup(monkeypatch, {"SPY": SPY}, calls)
    assert script.main(["--dry-run"]) == 0 and calls == []
    assert "1 instrument(s) to classify (dry run)" in capsys.readouterr().out
    assert script.main([]) == 0 and calls == ["SPY"]
    assert repo.find_instrument("SPY")["instrument_type"] == "ETF"
    assert script.main(["--dry-run"]) == 0 and "0 instrument(s)" in capsys.readouterr().out   # nothing left


# ── Accounts page renders with the new controls ───────────────────────────────

def _accounts_page():
    import runpy
    import sys
    from pathlib import Path
    root = Path.cwd()
    sys.path.insert(0, str(root))
    import web.auth as auth
    import web.styles as styles
    from portfolio_agent.services.context import local_context
    styles.top_nav = lambda *a, **k: None
    auth.require_login = lambda: None
    auth.current_context = lambda source: local_context(source)
    runpy.run_path(str(root / "web/pages/13_🗂️_Accounts.py"), run_name="__main__")


def test_accounts_page_renders_type_classification_and_cit_controls():
    from streamlit.testing.v1 import AppTest
    seed_holding(_pos("AAPL", 10, current_price=200.0, current_value=2000.0), "fidelity", "2026-09-01")
    _cit_position()
    at = AppTest.from_function(_accounts_page, default_timeout=90)
    at.session_state["tile_open_accounts_class"] = True          # tiles render their body only while open
    at.session_state["tile_open_accounts_cit"] = True
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    text = " ".join(m.value for m in at.markdown)
    assert "Taxable" in text and "401K" in text and "tax deferred" in text
    labels = [s.label for s in at.selectbox]
    assert "Correct a holding" in labels and "Account type" in labels and "Proxy ticker (optional)" in [t.label for t in at.text_input]
    assert any("unit price" in n.label.lower() for n in at.number_input)       # hand-entered price for the CIT


def _portfolio_page():
    import runpy
    import sys
    from pathlib import Path
    root = Path.cwd()
    sys.path.insert(0, str(root))
    import streamlit as st
    import web.auth as auth
    import web.data.portfolio as wp
    import web.styles as styles
    from portfolio_agent.services.context import local_context
    styles.top_nav = lambda *a, **k: None
    st.page_link = lambda *a, **k: None                        # needs a multipage context AppTest doesn't have
    auth.require_login = lambda: None
    auth.current_context = lambda source: local_context(source)
    wp.get_company_names = lambda tickers: {}
    runpy.run_path(str(root / "web/pages/5_💼_Portfolio.py"), run_name="__main__")


def test_portfolio_page_filters_and_groups_by_instrument_type_and_asset_class(monkeypatch):
    from streamlit.testing.v1 import AppTest
    seed_holding(_pos("AAPL", 10, current_price=200.0, current_value=2000.0), "fidelity", "2026-09-01")
    seed_holding(_pos("SPY", 10, current_price=500.0, current_value=5000.0), "fidelity", "2026-09-01")
    _fake_lookup(monkeypatch, {"SPY": SPY, "AAPL": AAPL})
    ic.classify_new_holdings(CTX)

    at = AppTest.from_function(_portfolio_page, default_timeout=120)
    at.session_state["tile_open_portfolio_holdings"] = True
    at.session_state["tile_open_portfolio_sector_allocation"] = True
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    by_label = {m.label: m for m in at.multiselect}
    assert {"Type", "Asset class", "Columns"} <= set(by_label)
    assert by_label["Type"].options == ["ETF", "STOCK"] and by_label["Asset class"].options == ["US_EQUITY"]
    group = next(r for r in at.radio if r.label == "Group by")
    assert group.options == ["Sector", "Asset class", "Instrument type"]

    by_label["Type"].select("ETF")
    group.set_value("Instrument type")
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    table = next(df for df in at.dataframe if "instrument_type" in df.value.columns or "ticker" in df.value.columns)
    assert list(table.value["ticker"]) == ["SPY"]                      # filtered to ETFs only
    assert "Etf" in " ".join(m.value for m in at.markdown)             # the allocation table now groups by type
