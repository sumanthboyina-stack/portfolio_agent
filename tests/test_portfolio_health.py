"""
Step 3 — Portfolio Health.

One status per check (GOOD / WATCH / ACTION / UNKNOWN, UNKNOWN never GOOD), no single score, owner-scoped, judged only
against the person's effective policy and the global health thresholds (database settings). No network: price history is
patched out and every fund comes from stored (fake) fund data.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.domain import LOCAL_OWNER, HealthStatus
from portfolio_agent.services import account_service, fund_data_service as fds, health_service as hs, investor_service as inv
from portfolio_agent.services.context import RequestContext, _mint_identity_for_tests, local_context
from portfolio_agent.tools import auth_users_db, notification_log_db
from portfolio_agent.tools import fund_data_db as fdb
from portfolio_agent.tools import health_snapshots_db as snapdb
from portfolio_agent.tools import holdings_db as repo
from portfolio_agent.tools import investor_settings_db as sdb
from portfolio_agent.tools import portfolio_health as ph
from portfolio_agent.tools import portfolio_risk as risk
from portfolio_agent.tools import restricted_list_db
from portfolio_agent.tools import user_notifications_db as notif
from portfolio_agent.tools.fund_data_providers import FundData, FundHolding, FundProfile

ROOT = Path(__file__).resolve().parents[1]
ME = local_context("test")
NOW = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc)          # a Friday morning in New York: Thursday's close is current
A, W, U, G = "ACTION", "WATCH", "UNKNOWN", "GOOD"
SECTORS = ["Technology", "Healthcare", "Financial Services", "Energy", "Industrials", "Utilities", "Real Estate",
           "Consumer Defensive", "Consumer Cyclical", "Communication Services", "Basic Materials", "Technology"]


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(risk, "compute_portfolio_risk_context", lambda *a, **k: {})        # no price history by default


# ── helpers ───────────────────────────────────────────────────────────────────

def _typed(ticker, itype="STOCK", leverage=1.0, asset_class="US_EQUITY", proxy=None, expense=None):
    inst = repo.get_or_create_instrument(ticker)
    fields = dict(instrument_type=itype, leverage_factor=leverage, asset_class=asset_class, classification_source="manual",
                  classified_at=datetime.now(timezone.utc).isoformat())
    if proxy:
        fields["proxy_ticker"] = proxy
    if expense is not None:
        fields["expense_ratio"] = expense
    repo.update_instrument(inst["instrument_id"], **fields)
    return repo.find_instrument(ticker)


def _account(ctx=ME, name="Brokerage", account_type="TAXABLE"):
    return account_service.create_account(ctx, broker="manual", display_name=name, account_type=account_type)


def _hold(acct, ticker, value, *, itype="STOCK", asset_class="US_EQUITY", sector="Technology", leverage=1.0, ctx=ME,
          price_as_of="2026-10-08", expense=None, proxy=None, shares=None):
    price = 100.0 if shares is None else value / shares
    shares = value / 100.0 if shares is None else shares
    account_service.add_position(ctx, acct["account_id"], {"ticker": ticker, "shares": shares, "current_price": price,
                                                           "sector": sector, "price_as_of": price_as_of})
    return _typed(ticker, itype, leverage, asset_class, proxy=proxy, expense=expense)


def _cash(acct, amount, ctx=ME):
    account_service.record_account_cash(ctx, acct["account_id"], amount, as_of_date="2026-10-08")


def _diversified(acct, n=12, each=1000.0):
    for i in range(n):
        _hold(acct, f"S{i:02d}", each, sector=SECTORS[i % len(SECTORS)])


def _fund(ticker, holdings=(), *, expense=None, classes=None, sectors=None, fetched_at=None, group=None, family="Acme"):
    inst = repo.find_instrument(ticker)
    hs_ = [FundHolding(s, s, w) for s, w in holdings]
    prof = FundProfile(expense_ratio=expense, fund_family=family, share_class_group=group)
    fdb.save_fund_data(inst["instrument_id"], FundData(
        ticker=ticker, profile=prof, holdings=hs_, sector_weights=sectors or {}, asset_class_weights=classes or {"stock": 1.0},
        holdings_coverage=sum(w for _, w in holdings), as_of="2026-10-08", source="fake"), fetched_at=fetched_at or NOW.isoformat())


def _health(owner=LOCAL_OWNER, **kw):
    ph.clear_health_cache()
    return ph.get_portfolio_health_for_owner(owner, repo.get_portfolio_by_owner(owner), now=kw.pop("now", NOW), **kw)


def _c(health, key, scope=None):
    sc = health.household if scope is None else scope
    return sc.check(key)


def _good_correlation(monkeypatch):
    monkeypatch.setattr(risk, "compute_portfolio_risk_context",
                        lambda holdings, *a, **k: {h["ticker"]: {"correlation_with_portfolio": 0.3} for h in holdings})


# ── Statuses are the four, UNKNOWN is never GOOD ──────────────────────────────

def test_status_ordering_puts_unknown_above_good_and_an_empty_set_is_unknown():
    assert ph.worst([G, G]) == G and ph.worst([G, U]) == U and ph.worst([U, W]) == W and ph.worst([W, A, G]) == A
    assert ph.worst([]) == U
    assert {s.value for s in HealthStatus} == {A, W, U, G}


def test_an_empty_portfolio_is_unknown_not_good_with_a_reason_code():
    h = _health()
    for key in ("allocation", "fixed_income_international", "excess_cash", "emergency_reserve", "concentration", "diversification",
                "leveraged_inverse", "fees", "data_quality"):
        c = _c(h, key)
        assert c.status == U and ph.RC_NO_HOLDINGS in c.reason_codes, key
    assert _c(h, "compliance").status == G                                  # nothing held, nothing restricted
    assert h.household.counts[G] == 1 and h.household.counts[U] == 10 and h.household.counts[A] == 0
    assert not hasattr(h, "score") and "score" not in h.to_dict()           # no single overall score


# ── Emergency reserve ─────────────────────────────────────────────────────────

def test_reserve_is_action_with_the_dollar_shortfall_below_the_default_and_good_at_or_above_it():
    acct = _account()
    _hold(acct, "AAA", 5000.0)
    _cash(acct, 1000.0)
    c = _c(_health(), "emergency_reserve")
    assert c.status == A and c.metrics["shortfall"] == 2000.0 and c.metrics["reserve_source"] == "DEFAULT"
    assert "$2,000" in c.summary and "the default reserve" in c.summary
    _cash(acct, 3000.0)
    c = _c(_health(), "emergency_reserve")
    assert c.status == G and c.metrics["surplus"] == 0.0


def test_reserve_uses_the_users_own_amount_and_says_so():
    acct = _account()
    _cash(acct, 5000.0)
    inv.set_emergency_reserve(ME, 8000.0)
    c = _c(_health(), "emergency_reserve")
    assert c.status == A and c.metrics["reserve_amount"] == 8000.0 and c.metrics["reserve_source"] == "USER"
    assert "your own reserve" in c.summary and c.metrics["shortfall"] == 3000.0


def test_reserve_counts_only_cash_and_money_market_in_taxable_or_cash_accounts_never_retirement_or_paper():
    taxable, ira, cash_acct, k401 = _account(name="T"), _account(name="IRA", account_type="ROTH_IRA"), _account(name="C", account_type="CASH"), _account(name="K", account_type="401K")
    _cash(taxable, 1000.0); _cash(ira, 50000.0); _cash(cash_acct, 500.0); _cash(k401, 9000.0)
    _hold(taxable, "SPAXX", 700.0, itype="MONEY_MARKET", asset_class="CASH")
    _hold(ira, "FZFXX", 20000.0, itype="MONEY_MARKET", asset_class="CASH")                 # a money market in an IRA does not count
    _hold(taxable, "VOO", 90000.0, itype="ETF")                                              # nor does a stock fund
    paper = repo.insert_account(repo.get_portfolio_by_owner(LOCAL_OWNER)["portfolio_id"], "paper", "P1", "Paper", "TAXABLE", is_paper=True)
    repo.set_account_cash(paper["account_id"], 99999.0, "2026-10-08", None)
    c = _c(_health(), "emergency_reserve")
    assert c.metrics["counted_cash"] == pytest.approx(1000 + 500 + 700) and c.status == A and c.metrics["shortfall"] == pytest.approx(800)


def test_reserve_is_unknown_without_a_known_cash_balance_and_when_only_unknown_type_accounts_have_cash():
    acct = _account()
    _hold(acct, "AAA", 5000.0)
    c = _c(_health(), "emergency_reserve")
    assert c.status == U and ph.RC_NO_CASH_DATA in c.reason_codes                           # never "zero cash, ACTION" nor GOOD
    other = account_service.create_account(ME, broker="manual", display_name="Mystery", account_type="OTHER")
    _cash(other, 10000.0)
    c = _c(_health(), "emergency_reserve")
    assert c.status == U and ph.RC_ACCOUNT_TYPE_UNKNOWN in c.reason_codes and c.metrics["excluded_unknown_type"] == 10000.0
    _cash(acct, 0.0)                                                                         # a recorded zero IS known
    assert _c(_health(), "emergency_reserve").status == A


# ── Allocation bands ──────────────────────────────────────────────────────────

BAND = {"target": 45.0, "min": 40.0, "max": 50.0}


@pytest.mark.parametrize("actual,expected", [(45, G), (40, G), (50, G), (39, W), (36, W), (35, W), (34.9, A), (51, W), (55, W), (55.1, A), (80, A)])
def test_band_status_watch_outside_the_band_action_beyond_twice_the_band(actual, expected):
    assert ph._band_status(actual, BAND, 2.0) == expected


def test_allocation_statuses_against_the_profile_bands():
    acct = _account()
    _hold(acct, "VTI", 4500.0, itype="ETF"); _fund("VTI", [("AAPL", 0.05)], classes={"stock": 1.0})
    _hold(acct, "VXUS", 1500.0, itype="ETF", asset_class="INTL_EQUITY"); _fund("VXUS", [("NESN", 0.02)], classes={"stock": 1.0})
    _hold(acct, "BND", 3300.0, itype="ETF", asset_class="FIXED_INCOME"); _fund("BND", [("T1", 0.1)], classes={"bond": 1.0})
    _cash(acct, 700.0)
    h = _health()
    assert _c(h, "allocation").status != A and _c(h, "excess_cash").status == G
    fi = _c(h, "fixed_income_international")
    assert fi.metrics["FIXED_INCOME"]["actual_pct"] == pytest.approx(33.0) and fi.metrics["INTL_EQUITY"]["actual_pct"] == pytest.approx(15.0)
    assert h.household.mix["CASH"] == pytest.approx(7.0)
    # too little in bonds and far too much cash
    h2 = None
    acct2 = _account(name="Second")
    _cash(acct2, 20000.0)
    h2 = _health()
    assert _c(h2, "excess_cash").status == A and _c(h2, "fixed_income_international").status in (W, A)


def test_unclassified_holdings_make_an_under_the_band_verdict_unknown_not_a_false_alarm():
    acct = _account()
    for i in range(5):
        _hold(acct, f"U{i}", 2000.0, itype="UNKNOWN", asset_class="UNKNOWN")
    h = _health()
    alloc = _c(h, "allocation")
    assert alloc.status == U and ph.RC_ALLOCATION_UNKNOWN_SHARE in alloc.reason_codes
    assert _c(h, "fixed_income_international").status == U
    assert alloc.metrics["unclassified_pct"] == pytest.approx(100.0)


# ── Concentration ─────────────────────────────────────────────────────────────

def test_concentration_action_over_the_limit_watch_near_it_good_below(monkeypatch):
    _good_correlation(monkeypatch)
    acct = _account()
    _hold(acct, "BIG", 5000.0, sector="Technology")
    _diversified(acct, 10, 500.0)
    c = _c(_health(), "concentration")
    assert c.status == A and "largest single security BIG" in c.summary                        # ~50% vs a 12% limit
    account_service.archive_account(ME, acct["account_id"])                                  # start over with 10 equal holdings
    ten = _account(name="Ten")
    for i in range(10):
        _hold(ten, f"T{i:02d}", 1000.0, sector=SECTORS[i])
    c = _c(_health(), "concentration")                                                        # 10% each: 83% of the 12% limit, under it
    assert c.metrics["top_position"]["pct"] == pytest.approx(10.0) and c.status == W


def test_a_threshold_change_in_the_settings_table_changes_the_status():
    acct = _account()
    _diversified(acct, 12, 1000.0)                                      # 8.3% each, limit 12% -> 69% of it
    assert _c(_health(), "concentration").metrics["top_position"]["pct"] == pytest.approx(8.33, abs=0.01)
    base = _c(_health(), "concentration").status
    sdb.update_portfolio_settings(updated_by="admin", health_watch_limit_fraction=0.5)
    assert base != W and _c(_health(), "concentration").status in (W, A)                          # same book, lower watch line
    ph.clear_health_cache()


def test_concentration_looks_through_funds_for_issuers_and_flags_a_partial_look_through_unknown():
    acct = _account()
    _hold(acct, "AAPL", 800.0, sector="Technology")
    _hold(acct, "VTI", 9200.0, itype="ETF")
    _fund("VTI", [("AAPL", 0.5), ("MSFT", 0.5)], sectors={"Technology": 1.0})
    c = _c(_health(), "concentration")
    assert c.status == A and c.metrics["top_issuer"]["issuer"] in ("AAPL", "MSFT") and c.metrics["top_issuer"]["pct"] > 40
    assert "counting what funds hold" in c.summary
    # top-10-only data: the fund's contents are mostly unknown, so under-the-limit cannot be GOOD
    _fund("VTI", [("AAPL", 0.01)], sectors={"Technology": 1.0})
    acct2 = _account(name="Two")
    for i in range(10):
        _hold(acct2, f"Z{i}", 1000.0, sector=SECTORS[i])
    h = _health()
    cc = _c(h, "concentration")
    assert cc.status != G and ph.RC_LOOKTHROUGH_PARTIAL in cc.reason_codes + tuple(_c(h, "data_quality").reason_codes)
    assert any("only partly known" in s or "understate" in s for s in [cc.summary]) or cc.status == A


def test_position_check_judges_single_securities_a_big_index_fund_is_judged_through_what_it_holds():
    acct = _account()
    _hold(acct, "VTI", 8000.0, itype="ETF")
    _fund("VTI", [(f"H{i}", 0.01) for i in range(100)], sectors={"Technology": 0.2, "Healthcare": 0.2, "Energy": 0.2, "Financial Services": 0.2, "Utilities": 0.2})
    _hold(acct, "AAPL", 500.0); _hold(acct, "MSFT", 500.0)
    c = _c(_health(), "concentration")
    assert c.metrics["top_position"]["ticker"] in ("AAPL", "MSFT") and c.metrics["top_position"]["pct"] == pytest.approx(5.56, abs=0.01)


# ── Diversification ───────────────────────────────────────────────────────────

def test_diversification_effective_n_thresholds_and_correlation(monkeypatch):
    acct = _account()
    _diversified(acct, 12, 1000.0)
    c = _c(_health(), "diversification")
    assert c.metrics["effective_n"] == pytest.approx(12.0, abs=0.1)
    assert c.status == U and ph.RC_CORRELATION_UNAVAILABLE in c.reason_codes                        # correlation unknown: not GOOD
    _good_correlation(monkeypatch)
    assert _c(_health(), "diversification").status == G
    monkeypatch.setattr(risk, "compute_portfolio_risk_context",
                        lambda holdings, *a, **k: {h["ticker"]: {"correlation_with_portfolio": 0.95} for h in holdings})
    c = _c(_health(), "diversification")
    assert c.status == A and c.metrics["avg_correlation"] == pytest.approx(0.95)
    sdb.update_portfolio_settings(updated_by="admin", health_max_avg_correlation_action=1.0)
    assert _c(_health(), "diversification").status == W                                             # 0.95 > the 0.70 watch line only
    sdb.update_portfolio_settings(updated_by="admin", health_min_effective_n_watch=20.0, health_min_effective_n_action=15.0)
    monkeypatch.setattr(risk, "compute_portfolio_risk_context",
                        lambda holdings, *a, **k: {h["ticker"]: {"correlation_with_portfolio": 0.1} for h in holdings})
    assert _c(_health(), "diversification").status == A                                             # 12 effective < 15


def test_a_single_holding_is_not_diversified(monkeypatch):
    _good_correlation(monkeypatch)
    _hold(_account(), "ONLY", 5000.0)
    c = _c(_health(), "diversification")
    assert c.status == A and c.metrics["effective_n"] == pytest.approx(1.0)


# ── Leveraged / inverse ───────────────────────────────────────────────────────

def test_leveraged_inverse_share_against_the_profile_limit():
    acct = _account()
    _diversified(acct, 10, 1000.0)
    _hold(acct, "TQQQ", 450.0, itype="ETF", leverage=3.0); _fund("TQQQ", [("AAPL", 0.5)])
    c = _c(_health(), "leveraged_inverse")
    assert c.metrics["pct"] == pytest.approx(4.31, abs=0.05) and c.status == W                       # 4.3% of a 5% limit: past 80% of it
    _hold(acct, "SQQQ", 500.0, itype="ETF", leverage=-3.0); _fund("SQQQ", [("AAPL", 0.5)])
    assert _c(_health(), "leveraged_inverse").status == A


def test_leveraged_inverse_is_unknown_when_much_of_the_book_is_unclassified():
    acct = _account()
    for i in range(4):
        _hold(acct, f"U{i}", 1000.0, itype="UNKNOWN", asset_class="UNKNOWN")
    c = _c(_health(), "leveraged_inverse")
    assert c.status == U and ph.RC_TYPE_UNKNOWN in c.reason_codes


# ── Fees ──────────────────────────────────────────────────────────────────────

def test_fees_weighted_expense_ratio_cost_high_list_and_unknown_never_good():
    acct = _account()
    _hold(acct, "CHEAP", 8000.0, itype="ETF"); _fund("CHEAP", [("AAPL", 0.05)], expense=0.0004)
    c = _c(_health(), "fees")
    assert c.status == G and c.metrics["weighted_expense_ratio_pct"] == pytest.approx(0.04) and c.metrics["estimated_annual_fund_cost"] == pytest.approx(3.2)
    assert "Estimated fund expenses" in c.notes[0]
    _hold(acct, "PRICEY", 2000.0, itype="MUTUAL_FUND"); _fund("PRICEY", [("MSFT", 0.05)], expense=0.0125)
    c = _c(_health(), "fees")
    assert c.status == W and [x["ticker"] for x in c.metrics["above_threshold"]] == ["PRICEY"] and "PRICEY 1.25%" in c.summary
    sdb.update_portfolio_settings(updated_by="admin", high_expense_ratio_pct=2.0)
    assert _c(_health(), "fees").status == G
    _hold(acct, "NOER", 6000.0, itype="ETF"); _fund("NOER", [("AAPL", 0.05)])                      # expense ratio unknown for a big slice
    c = _c(_health(), "fees")
    assert c.status == U and ph.RC_EXPENSE_RATIO_UNKNOWN in c.reason_codes


def test_no_funds_means_no_fund_expenses_known():
    acct = _account()
    _hold(acct, "AAA", 1000.0)
    c = _c(_health(), "fees")
    assert c.status == G and c.metrics["fund_value"] == 0.0


def test_cheaper_share_class_note_only_within_the_same_share_class_group():
    acct = _account()
    _hold(acct, "VFIAX", 5000.0, itype="MUTUAL_FUND"); _fund("VFIAX", [("AAPL", 0.05)], expense=0.0004, group="vanguard-500")
    _typed("VOO", "ETF"); _fund("VOO", [("AAPL", 0.05)], expense=0.0003, group="vanguard-500")        # same fund, cheaper class
    _typed("SPY", "ETF"); _fund("SPY", [("AAPL", 0.05)], expense=0.0001, group="spdr-500")             # a different product: never mentioned
    h = _health()
    notes = " ".join(_c(h, "fees").notes)
    assert "VOO" in notes and "same fund, different share class" in notes and "not a recommendation" in notes
    assert "SPY" not in notes


# ── Profile and compliance ────────────────────────────────────────────────────

def test_profile_default_nudges_to_the_quiz_and_is_unknown_chosen_is_good_above_cap_is_watch():
    c = _c(_health(), "profile")
    assert c.status == U and ph.RC_PROFILE_DEFAULT in c.reason_codes and "quiz" in c.summary.lower()
    inv.pick_profile(ME, "MODERATE")
    assert _c(_health(), "profile").status == G
    inv.take_quiz(ME, {"horizon": "lt3", "drop": None, "priority": None})                         # a short horizon caps at Conservative
    inv.pick_profile(ME, "AGGRESSIVE", acknowledge_above_cap=True)
    c = _c(_health(), "profile")
    assert c.status == W and ph.RC_ABOVE_CAP in c.reason_codes


def test_restricted_holdings_are_action_and_an_unreadable_list_is_unknown(monkeypatch):
    acct = _account()
    _hold(acct, "BADCO", 1000.0); _hold(acct, "OKCO", 1000.0)
    assert _c(_health(), "compliance").status == G
    restricted_list_db.add_restricted("BADCO", "insider info", LOCAL_OWNER)
    c = _c(_health(), "compliance")
    assert c.status == A and "BADCO (insider info)" in c.summary and c.metrics["held_restricted"] == ["BADCO"]
    monkeypatch.setattr(restricted_list_db, "list_restricted_by_owner", lambda owner: (_ for _ in ()).throw(RuntimeError("db down")))
    c = _c(_health(), "compliance")
    assert c.status == U and ph.RC_RESTRICTED_UNREADABLE in c.reason_codes


# ── Data quality ──────────────────────────────────────────────────────────────

def test_data_quality_lists_each_kind_of_gap():
    acct = _account()
    _hold(acct, "GOOD1", 1000.0)                                                                  # priced and typed
    _hold(acct, "OLDPX", 1000.0, price_as_of="2026-09-01")                                         # price from weeks ago
    _hold(acct, "WHAT", 1000.0, itype="UNKNOWN", asset_class="UNKNOWN")
    account_service.add_position(ME, acct["account_id"], {"ticker": "NOPX", "shares": 10, "current_price": None, "avg_cost": 5.0})
    _typed("NOPX")
    _hold(acct, "CIT:ACME-2045", 500.0, itype="CIT", asset_class="MULTI_ASSET")
    _hold(acct, "OLDF", 1000.0, itype="ETF"); _fund("OLDF", [("AAPL", 0.05)], fetched_at=(NOW - timedelta(days=10)).isoformat())
    c = _c(_health(), "data_quality")
    codes = set(c.reason_codes)
    assert {ph.RC_UNPRICED, ph.RC_TYPE_UNKNOWN, ph.RC_PRICE_STALE, ph.RC_NO_BREAKDOWN, ph.RC_FUND_DATA_STALE} <= codes
    assert c.status in (W, A) and "CIT:ACME-2045" in c.summary


def test_a_mutual_fund_nav_from_the_prior_close_is_not_flagged_stale_on_a_morning():
    acct = _account()
    _hold(acct, "VFIAX", 1000.0, itype="MUTUAL_FUND", price_as_of="2026-10-08"); _fund("VFIAX", [("AAPL", 0.05)])
    _hold(acct, "AAA", 1000.0, price_as_of="2026-10-08")
    assert ph.RC_PRICE_STALE not in _c(_health(), "data_quality").reason_codes
    evening = datetime(2026, 10, 10, 2, 30, tzinfo=timezone.utc)                                    # Friday 22:30 New York: Friday's NAV should be out
    h = _health(now=evening)
    assert ph.RC_PRICE_STALE in _c(h, "data_quality").reason_codes


def test_a_manual_breakdown_or_proxy_clears_the_cit_gap():
    acct = _account()
    _hold(acct, "CIT:ACME-2045", 5000.0, itype="CIT", asset_class="MULTI_ASSET")
    assert ph.RC_NO_BREAKDOWN in _c(_health(), "data_quality").reason_codes
    fds.set_manual_breakdown(ME, "CIT:ACME-2045", {"stock": 60, "bond": 40})
    assert ph.RC_NO_BREAKDOWN not in _c(_health(), "data_quality").reason_codes


def test_unpriced_share_above_the_threshold_is_action():
    acct = _account()
    account_service.add_position(ME, acct["account_id"], {"ticker": "GHOST", "shares": 1000, "current_price": None, "avg_cost": 10.0})
    _typed("GHOST")
    _hold(acct, "AAA", 1000.0)
    assert _c(_health(), "data_quality").status == A                                                # ~91% of the book has no price


# ── Scopes ────────────────────────────────────────────────────────────────────

def test_scopes_household_accounts_tax_buckets_and_paper_is_excluded():
    taxable, ira = _account(name="Taxable"), _account(name="Roth", account_type="ROTH_IRA")
    _hold(taxable, "AAA", 3000.0); _hold(ira, "BBB", 1000.0)
    paper = repo.insert_account(repo.get_portfolio_by_owner(LOCAL_OWNER)["portfolio_id"], "paper", "P1", "Paper", "TAXABLE", is_paper=True)
    account_service.add_position(ME, paper["account_id"], {"ticker": "PPP", "shares": 10, "current_price": 100.0})
    h = _health()
    assert h.household.total_value == pytest.approx(4000.0)
    assert {s.label for s in h.by_account.values()} == {"Taxable", "Roth"}
    assert set(h.by_tax_treatment) == {"TAXABLE", "TAX_FREE"} and h.by_tax_treatment["TAX_FREE"].total_value == pytest.approx(1000.0)
    assert not any("Paper" in s.label for s in h.scopes())
    sl = h.by_account[str(taxable["account_id"])]
    assert [c.key for c in sl.checks] == ["concentration", "diversification", "leveraged_inverse", "fees", "data_quality"]
    assert [c.key for c in h.household.checks] == ["allocation", "fixed_income_international", "excess_cash", "emergency_reserve",
                                                   "concentration", "diversification", "leveraged_inverse", "fees", "profile",
                                                   "compliance", "data_quality"]
    assert h.household.counts == {s: sum(1 for c in h.household.checks if c.status == s) for s in (A, W, U, G)}


# ── Owner scoping and caching ─────────────────────────────────────────────────

def _second_owner(name="someone-else"):
    with repo.unit_of_work() as c:
        c.execute("INSERT INTO portfolios (owner) VALUES (?)", (name,))
    return RequestContext(identity=_mint_identity_for_tests(name), source="test")


def test_health_is_owner_scoped_one_person_never_sees_anothers_holdings():
    other = _second_owner()
    mine, theirs = _account(), account_service.create_account(other, broker="manual", display_name="Theirs", account_type="TAXABLE")
    _hold(mine, "MINE1", 1000.0)
    _hold(theirs, "THEIRS1", 90000.0, ctx=other)
    h_me, h_them = _health(LOCAL_OWNER), _health("someone-else")
    assert h_me.household.total_value == pytest.approx(1000.0) and h_them.household.total_value == pytest.approx(90000.0)
    assert "THEIRS1" not in json.dumps(h_me.to_dict()) and "MINE1" not in json.dumps(h_them.to_dict())
    stranger = RequestContext(identity=_mint_identity_for_tests("nobody"), source="test")
    with pytest.raises(account_service.NotAuthorized):
        ph.get_portfolio_health(stranger)


def test_health_is_cached_and_every_input_that_feeds_it_invalidates_the_cache():
    acct = _account()
    _hold(acct, "AAA", 1000.0)
    ph.clear_health_cache()
    p = repo.get_portfolio_by_owner(LOCAL_OWNER)
    first = ph.get_portfolio_health_for_owner(LOCAL_OWNER, p, now=NOW)
    assert ph.get_portfolio_health_for_owner(LOCAL_OWNER, p, now=NOW) is first
    _cash(acct, 100.0)
    second = ph.get_portfolio_health_for_owner(LOCAL_OWNER, p, now=NOW)
    assert second is not first and second.version != first.version
    sdb.update_portfolio_settings(updated_by="admin", health_action_band_multiple=3.0)
    third = ph.get_portfolio_health_for_owner(LOCAL_OWNER, p, now=NOW)
    assert third.version != second.version
    restricted_list_db.add_restricted("ZZZ", None, LOCAL_OWNER)
    assert ph.get_portfolio_health_for_owner(LOCAL_OWNER, p, now=NOW).version != third.version
    assert ph.get_portfolio_health_for_owner(LOCAL_OWNER, p, now=NOW + timedelta(days=1)).as_of == "2026-10-10"


# ── Snapshots and diffs ───────────────────────────────────────────────────────

def _snap(day: date, now=None):
    p = repo.get_portfolio_by_owner(LOCAL_OWNER)
    h = ph.get_portfolio_health_for_owner(LOCAL_OWNER, p, now=now or datetime.combine(day, NOW.timetz()), use_cache=False)
    return hs._snapshot_for_owner(LOCAL_OWNER, p, actor=LOCAL_OWNER, source="test", request_id="r1", health=h, today=day)


def test_a_snapshot_is_one_row_a_day_audited_once_and_refreshed_not_duplicated():
    _hold(_account(), "AAA", 1000.0)
    d = date(2026, 10, 9)
    assert _snap(d)["status"] == "created" and _snap(d)["status"] == "updated"
    rows = snapdb.list_recent(LOCAL_OWNER)
    assert [r["snapshot_date"] for r in rows] == ["2026-10-09"]
    assert [e["operation"] for e in repo.get_audit_events(limit=50)].count("save_health_snapshot") == 1
    assert rows[0]["payload"]["scopes"]["household"]["checks"]["emergency_reserve"]["status"] in (A, U, G)
    assert "metrics" in rows[0]["payload"]["scopes"]["household"]["checks"]["emergency_reserve"]


def test_snapshots_can_be_turned_off_in_settings():
    _hold(_account(), "AAA", 1000.0)
    sdb.update_portfolio_settings(updated_by="admin", health_snapshot_enabled=False)
    assert _snap(date(2026, 10, 9))["status"] == "disabled" and snapdb.list_recent(LOCAL_OWNER) == []


def test_diff_against_the_last_snapshot_and_thirty_days_ago():
    acct = _account()
    _hold(acct, "AAA", 5000.0)
    _snap(date(2026, 9, 8))                                                             # no cash known -> reserve UNKNOWN
    _cash(acct, 5000.0)
    _snap(date(2026, 10, 8))                                                            # reserve now GOOD
    _cash(acct, 100.0)                                                                  # and then it drops
    p = repo.get_portfolio_by_owner(LOCAL_OWNER)
    ph.clear_health_cache()
    h = ph.get_portfolio_health_for_owner(LOCAL_OWNER, p, now=NOW)
    view = hs._view(LOCAL_OWNER, h, {"status": "skipped", "snapshot_date": h.as_of, "notified": []})
    last = [c for c in view["vs_last"]["changes"] if c["check"] == "emergency_reserve"][0]
    assert (last["from"], last["to"], last["direction"]) == (G, A, "worse") and view["last_date"] == "2026-10-08"
    old = [c for c in view["vs_30d"]["changes"] if c["check"] == "emergency_reserve"][0]
    assert (old["from"], old["to"]) == (U, A) and view["thirty_days_date"] == "2026-09-08"
    assert view["vs_last"]["counts_from"] != view["vs_last"]["counts_to"]


def test_first_view_has_no_previous_and_a_missing_thirty_day_snapshot_is_none():
    _hold(_account(), "AAA", 1000.0)
    view = hs.get_health_view(ME)
    assert view["vs_last"]["has_previous"] is False and view["vs_30d"] is None and view["snapshot"]["status"] == "created"
    assert len(snapdb.list_recent(LOCAL_OWNER)) == 1                                         # lazily taken on first view
    assert hs.get_health_view(ME)["snapshot"]["status"] == "updated"


def test_snapshots_are_owner_scoped():
    other = _second_owner()
    _hold(_account(), "AAA", 1000.0)
    account_service.create_account(other, broker="manual", display_name="T", account_type="TAXABLE")
    hs.get_health_view(ME)
    assert snapdb.list_recent("someone-else") == [] and len(snapdb.list_recent(LOCAL_OWNER)) == 1
    hs.get_health_view(other)
    assert len(snapdb.list_recent("someone-else")) == 1


def test_evening_batch_snapshots_every_owner_and_one_failure_does_not_stop_the_rest(monkeypatch):
    _second_owner("second")
    _hold(_account(), "AAA", 1000.0)
    real = ph.get_portfolio_health_for_owner

    def flaky(owner, portfolio, **kw):
        if owner == "second":
            raise RuntimeError("boom")
        return real(owner, portfolio, **kw)
    monkeypatch.setattr(ph, "get_portfolio_health_for_owner", flaky)
    out = hs.snapshot_all_owners(today=date(2026, 10, 9))
    assert out["created"] == 1 and out["failed"] == 1
    assert len(snapdb.list_recent(LOCAL_OWNER)) == 1


# ── Notifications ─────────────────────────────────────────────────────────────

# (Rewritten in the notifications task: health alerts now go through notification_service.notify into the outbox, judged by
#  the person's routing / contacts / quiet hours instead of the legacy single channel. The intent of each test is unchanged.)

def _notif_world():
    """LOCAL_OWNER has a verified login-email contact and email is on globally (no rate limit in the way)."""
    from portfolio_agent.services import notification_service as nsvc
    from portfolio_agent.tools import notification_settings_db as nsdb
    auth_users_db.invite_user("owner@example.com", LOCAL_OWNER, "member")
    nsdb.update_notification_settings(updated_by="t", email_from_address="apex@example.com", max_messages_per_user_per_hour=1000)
    nsvc.ensure_contacts(ME)


def _prefs(**kw):
    from portfolio_agent.services import notification_service as nsvc
    nsvc.update_preferences(ME, **{"min_severity_threshold": 3, **kw})


def _outbox_rows(check="emergency_reserve"):
    with sqlite3.connect(str(db_module.DB_PATH)) as c:
        c.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in c.execute("SELECT * FROM notification_outbox WHERE dedup_key LIKE ? ORDER BY id", [f"health:{check}:%"])]
        except sqlite3.OperationalError:                                                    # nothing has ever been queued
            return []


def _delivered(check="emergency_reserve"):
    """Dedup keys of health alerts actually queued to SEND (suppressed ones are recorded but never sent)."""
    return [r["dedup_key"] for r in _outbox_rows(check) if r["status"] in ("PENDING", "DEFERRED", "SENDING", "SENT")]


def test_a_new_action_notifies_once_per_run_and_again_after_it_clears_and_returns():
    _notif_world()
    acct = _account()
    _hold(acct, "AAA", 5000.0)
    _cash(acct, 100.0)
    _prefs()
    r = _snap(date(2026, 10, 7))
    assert "health:emergency_reserve:2026-10-07" in r["notified"]
    assert "health:emergency_reserve:2026-10-07" not in _snap(date(2026, 10, 8))["notified"]
    assert _delivered() == ["health:emergency_reserve:2026-10-07"]                          # same run: no second notification
    _cash(acct, 5000.0)
    _snap(date(2026, 10, 9))                                                                # cleared (GOOD)
    _cash(acct, 50.0)
    r = _snap(date(2026, 10, 10))
    assert "health:emergency_reserve:2026-10-10" in r["notified"]
    assert sorted(_delivered()) == ["health:emergency_reserve:2026-10-07", "health:emergency_reserve:2026-10-10"]
    row = _outbox_rows()[0]
    assert row["category"] == "PORTFOLIO_HEALTH" and row["severity"] == 4 and row["channel"] == "EMAIL"
    assert "$" not in row["body_brief"] and "AAA" not in row["body_brief"]                  # the brief text carries no amounts


def test_watch_never_notifies_and_the_users_preferences_are_respected():
    from portfolio_agent.services import notification_service as nsvc
    _notif_world()
    acct = _account()
    _hold(acct, "AAA", 5000.0); _cash(acct, 100.0)
    nsvc.set_routing(ME, "PORTFOLIO_HEALTH", "EMAIL", False)                                 # routing off -> nothing at all
    assert _snap(date(2026, 10, 7))["notified"] == [] and _outbox_rows() == []
    nsvc.set_routing(ME, "PORTFOLIO_HEALTH", "EMAIL", True)
    _prefs(min_severity_threshold=5)                                                        # ACTION is severity 4: below their bar
    assert _snap(date(2026, 10, 8))["notified"] == [] and _outbox_rows() == []
    _prefs(min_severity_threshold=3, quiet_hours_start="00:00", quiet_hours_end="23:59", quiet_hours_mode="SUPPRESS")
    assert _snap(date(2026, 10, 9))["notified"]                                              # recorded...
    assert _delivered() == [] and {r["status"] for r in _outbox_rows()} == {"SUPPRESSED"}   # ...but suppressed by quiet hours, not sent
    assert {r["reason_code"] for r in _outbox_rows()} == {"QUIET_HOURS"}
    keys = _delivered("profile") + _delivered("data_quality") + _delivered("compliance")
    assert keys == []                                                                       # UNKNOWN / WATCH / GOOD checks never notified
    assert _outbox_rows("diversification")                                                  # one holding is ACTION on diversification, so it did


def test_quiet_hours_defer_holds_a_health_alert_until_they_end():
    _notif_world()
    acct = _account()
    _hold(acct, "AAA", 5000.0); _cash(acct, 100.0)
    _prefs(quiet_hours_start="00:00", quiet_hours_end="23:59", quiet_hours_mode="DEFER")
    assert _snap(date(2026, 10, 9))["notified"]
    rows = _outbox_rows()
    assert {r["status"] for r in rows} == {"DEFERRED"} and all(r["next_attempt_at"] for r in rows)


def test_action_streak_start_walks_back_over_consecutive_action_snapshots_only():
    def snap(d, status):
        return {"snapshot_date": d, "payload": {"scopes": {"household": {"checks": {"x": {"status": status}}}}}}
    hist = [snap("2026-10-09", A), snap("2026-10-08", A), snap("2026-10-07", G), snap("2026-10-06", A)]
    assert snapdb.action_streak_start(hist, "x", "2026-10-10", A) == "2026-10-08"
    assert snapdb.action_streak_start(hist, "x", "2026-10-10", W) == "2026-10-10"
    assert snapdb.action_streak_start([], "x", "2026-10-10", A) == "2026-10-10"


# ── Settings (admin only, validated) ──────────────────────────────────────────

def test_health_thresholds_are_admin_only_validated_and_reset_to_defaults():
    with pytest.raises(inv.NotAdmin):
        inv.update_portfolio_settings(ME, health_watch_limit_fraction=0.5)
    auth_users_db.invite_user("me@example.com", LOCAL_OWNER, "admin")
    out = inv.update_portfolio_settings(ME, health_watch_limit_fraction=0.5, health_unpriced_action_pct=10)
    assert out["health_watch_limit_fraction"] == 0.5 and out["health_unpriced_action_pct"] == 10
    for bad in ({"health_watch_limit_fraction": 1.5}, {"health_action_band_multiple": 0.5}, {"health_min_effective_n_action": 99},
                {"health_max_avg_correlation_action": 0.1}, {"health_nonexistent": 1}):
        with pytest.raises(ValueError):
            inv.update_portfolio_settings(ME, **bad)
    reset = inv.reset_portfolio_settings(ME)
    assert reset["health_watch_limit_fraction"] == 0.8 and reset["health_action_band_multiple"] == 2.0
    assert [e["operation"] for e in repo.get_audit_events(limit=50)].count("update_portfolio_settings") >= 1


def test_new_health_columns_migrate_onto_an_existing_portfolio_settings_row():
    p = Path(db_module.DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(p) as c:
        c.execute("""CREATE TABLE portfolio_settings (id INTEGER PRIMARY KEY CHECK (id = 1), default_emergency_reserve_amount REAL NOT NULL,
                     version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, updated_by TEXT,
                     etf_data_ttl_hours REAL NOT NULL DEFAULT 24, mutual_fund_data_ttl_hours REAL NOT NULL DEFAULT 168,
                     lookthrough_min_coverage_pct REAL NOT NULL DEFAULT 80, lookthrough_max_depth INTEGER NOT NULL DEFAULT 3,
                     high_expense_ratio_pct REAL NOT NULL DEFAULT 0.5, health_snapshot_enabled INTEGER NOT NULL DEFAULT 1)""")
        c.execute("INSERT INTO portfolio_settings (id, default_emergency_reserve_amount, version, created_at, updated_at, updated_by, "
                  "etf_data_ttl_hours) VALUES (1, 4500.0, 3, 'x', 'y', 'admin', 6)")
    s = sdb.get_portfolio_settings()
    assert (s["default_emergency_reserve_amount"], s["version"], s["etf_data_ttl_hours"]) == (4500.0, 3, 6.0)       # untouched
    assert (s["health_action_band_multiple"], s["health_watch_limit_fraction"], s["health_unpriced_action_pct"]) == (2.0, 0.8, 25.0)


# ── Chat tool ─────────────────────────────────────────────────────────────────

def test_chat_tool_is_owner_scoped_read_only_and_has_no_overall_score():
    from web.chat.tools import _CHAT_TOOLS, _execute_chat_tool
    assert "get_portfolio_health" in [t["function"]["name"] for t in _CHAT_TOOLS]
    other = _second_owner()
    mine, theirs = _account(), account_service.create_account(other, broker="manual", display_name="Theirs", account_type="TAXABLE")
    _hold(mine, "MINE1", 1000.0); _hold(theirs, "THEIRS1", 70000.0, ctx=other)
    out = json.loads(_execute_chat_tool("get_portfolio_health", {}, owner=LOCAL_OWNER))
    assert out["total_value"] == pytest.approx(1000.0) and "THEIRS1" not in json.dumps(out)
    assert set(out["counts"]) == {A, W, U, G} and "score" not in out
    assert {c["status"] for c in out["checks"]} <= {A, W, U, G}
    assert snapdb.list_recent(LOCAL_OWNER) == [] and _delivered() == []                     # read-only: no snapshot, no notification
    assert "No signed-in account" in json.loads(_execute_chat_tool("get_portfolio_health", {}, owner=None))["note"]
    assert "No portfolio found" in json.loads(_execute_chat_tool("get_portfolio_health", {}, owner="ghost"))["note"]
    out2 = json.loads(_execute_chat_tool("get_portfolio_health", {}, owner="someone-else"))
    assert out2["total_value"] == pytest.approx(70000.0) and "MINE1" not in json.dumps(out2)


# ── UI ────────────────────────────────────────────────────────────────────────

def test_html_helpers_escape_and_use_the_four_statuses():
    from web.components import portfolio_health as ui
    c = ph.HealthCheck("k", "A <b>title</b>", A, "x & y", {}, ())
    html_ = ui.check_row_html(c)
    assert "&lt;b&gt;" in html_ and "x &amp; y" in html_ and "Action" in html_
    chips = ui.counts_chips_html({A: 2, W: 0, U: 1, G: 5})
    assert "2 Action" in chips and "0 Watch" in chips and "1 Unknown" in chips and "5 Good" in chips and "cursor:help" in chips
    order = [c.status for c in ui.sorted_checks([ph.HealthCheck("a", "a", G, "", {}, ()), ph.HealthCheck("b", "b", A, "", {}, ()),
                                                  ph.HealthCheck("c", "c", U, "", {}, ()), ph.HealthCheck("d", "d", W, "", {}, ())])]
    assert order == [A, W, U, G]
    line = ui.change_line_html({"scope": "household", "label": "Household", "title": "Emergency reserve", "from": G, "to": A, "direction": "worse"})
    assert "Good → Action" in line


def _health_page():
    import runpy
    import sys
    from pathlib import Path as P
    import streamlit as st
    root = P.cwd()
    sys.path.insert(0, str(root))
    import web.auth as auth
    import web.styles as styles
    from portfolio_agent.services.context import RequestContext, _mint_identity_for_tests
    styles.top_nav = lambda *a, **k: None
    auth.require_login = lambda: None
    from portfolio_agent.domain import LOCAL_OWNER
    who = st.session_state.get("_owner", LOCAL_OWNER)
    auth.current_context = lambda source: RequestContext(identity=_mint_identity_for_tests(who), source=source)
    runpy.run_path(str(root / "web/pages/3_🩺_Portfolio_Health.py"), run_name="__main__")


def _home_card():
    import sys
    from pathlib import Path as P
    import streamlit as st
    sys.path.insert(0, str(P.cwd()))
    import web.auth as auth
    from portfolio_agent.services.context import local_context
    auth.require_login = lambda: None
    auth.current_context = lambda source: local_context(source)
    from web.components.portfolio_health import render_health_home_card
    render_health_home_card(auth.current_context("dashboard"))
    st.write("done")


def test_the_health_page_renders_and_is_in_the_nav():
    from streamlit.testing.v1 import AppTest
    acct = _account()
    _diversified(acct, 6, 1000.0)
    _cash(acct, 200.0)
    at = AppTest.from_function(_health_page, default_timeout=60).run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("Action" in m.value for m in at.markdown)
    assert any("no single overall score" in c.value.lower() for c in at.caption)
    scope = at.selectbox(key="health_scope")
    assert scope.options[0].startswith("Household") and len(scope.options) >= 3                        # household + account + tax bucket
    scope.select_index(1).run()
    assert not at.exception, [e.value for e in at.exception]
    from web.styles import _NAV_SECONDARY
    assert any(k == "health" and p.endswith("3_🩺_Portfolio_Health.py") for k, _, _, p in _NAV_SECONDARY)


def test_the_health_page_for_a_user_with_no_portfolio_says_so_instead_of_failing():
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_function(_health_page, default_timeout=60)
    at.session_state["_owner"] = "nobody"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("portfolio" in i.value.lower() for i in at.info)


def test_home_card_never_breaks_the_home_page_and_shows_action_items(monkeypatch):
    from streamlit.testing.v1 import AppTest
    acct = _account()
    _hold(acct, "AAA", 5000.0); _cash(acct, 10.0)
    at = AppTest.from_function(_home_card, default_timeout=60).run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("Emergency reserve" in m.value for m in at.markdown) and any("Full checkup" in m.value for m in at.markdown)
    monkeypatch.setattr(hs, "get_health_view", lambda ctx: (_ for _ in ()).throw(RuntimeError("down")))
    at2 = AppTest.from_function(_home_card, default_timeout=60).run()
    assert not at2.exception and at2.markdown[-1].value == "done"


def test_admin_investor_settings_has_the_health_thresholds_in_a_tab():
    src = (ROOT / "web" / "components" / "investor_profile.py").read_text()
    assert "Health thresholds" in src


# ── Boundaries ────────────────────────────────────────────────────────────────

def test_no_model_decides_anything_and_the_shared_forecast_path_does_not_reach_health():
    src = (ROOT / "portfolio_agent" / "tools" / "portfolio_health.py").read_text()
    for banned in ("litellm", "anthropic", "openai", "import requests", "yfinance"):
        assert banned not in src, banned
    for mod in ("instrument_reference.py", "market_context.py", "fund_data_db.py"):
        text = (ROOT / "portfolio_agent" / "tools" / mod).read_text()
        for private in ("portfolio_health", "health_service", "health_snapshots_db"):
            assert private not in text, f"{mod} reaches {private}"
    code = ("import sys\n"
            "from portfolio_agent.tools.instrument_reference import get_fund_context\n"
            "import portfolio_agent.tools.market_context\n"
            "bad = [m for m in ('portfolio_health','health_snapshots_db','health_service') "
            "if 'portfolio_agent.tools.'+m in sys.modules or 'portfolio_agent.services.'+m in sys.modules]\n"
            "print('LEAK', bad) if bad else print('CLEAN')\n")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert out.stdout.strip().endswith("CLEAN"), out.stdout + out.stderr
