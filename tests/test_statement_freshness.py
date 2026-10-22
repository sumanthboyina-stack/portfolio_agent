"""Phase 4: a fund priced from a statement is as fresh as its statement. The rule (pure), the admin setting, the Portfolio Health data-quality check, and
that market-priced holdings in the same statement still refresh from the market while the statement-priced ones are left alone."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.services import account_service as accts
from portfolio_agent.services import investor_service as inv
from portfolio_agent.services.context import local_context
from portfolio_agent.tools import auth_users_db
from portfolio_agent.tools import holdings_db as repo
from portfolio_agent.tools import holdings_prices
from portfolio_agent.tools import investor_settings_db as sdb
from portfolio_agent.tools import llm_holdings_parser as lp
from portfolio_agent.tools import portfolio_health as ph
from portfolio_agent.tools import portfolio_risk as risk
from portfolio_agent.tools import price_freshness as pf
from portfolio_agent.tools.holdings_import import build_plan, commit_plan, parse_upload
from tests.fixtures.statement_texts import EMPOWER_STYLE
from tests.helpers_pdf import make_pdf
from tests.test_portfolio_health import NOW, _account, _c, _health, _hold
from tests.test_statement_extraction import EMPOWER_ANSWER

ME = local_context("test")
D = lambda n: (NOW - timedelta(days=n)).date().isoformat()          # n days before the health tests' "now" (2026-10-09)


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(risk, "compute_portfolio_risk_context", lambda *a, **k: {})


# ── the pure rule ─────────────────────────────────────────────────────────────

def test_which_holdings_are_statement_priced():
    assert pf.is_statement_priced("CIT") and pf.is_statement_priced("STABLE_VALUE") and pf.is_statement_priced("MONEY_MARKET", "CIT:P-CASH")
    assert pf.is_statement_priced(None, "CIT:P-F") and not pf.is_statement_priced("MONEY_MARKET", "VMFXX")
    assert not any(pf.is_statement_priced(t, tk) for t, tk in (("STOCK", "AAPL"), ("ETF", "VOO"), ("MUTUAL_FUND", "VFIAX"), ("TARGET_DATE_FUND", "VFIFX"), (None, None)))


def test_a_statement_price_is_fresh_for_exactly_the_allowed_number_of_days():
    now = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc)
    on = lambda days, **kw: pf.is_price_stale(D(days), "CIT", now, ticker="CIT:P-F", **kw)
    assert on(0) is False and on(100) is False and on(101) is True                         # the default window: 100 days
    assert on(120, statement_max_age_days=130) is False and on(131, statement_max_age_days=130) is True
    assert on(40, statement_max_age_days=30) is True
    assert pf.is_price_stale(None, "CIT", now, ticker="CIT:P-F") is None and pf.is_price_stale("", "STABLE_VALUE", now) is None and pf.is_price_stale("garbage", "CIT", now) is None
    assert pf.statement_price_age_days(D(7), now) == 7 and pf.statement_price_age_days(None, now) is None
    assert pf.statement_price_due_date("2026-03-31") == date(2026, 7, 9) and pf.statement_price_due_date("2026-03-31", 90) == date(2026, 6, 29)
    assert pf.statement_price_due_date(None) is None


def test_a_statement_price_ignores_the_market_clock_and_market_priced_holdings_are_unchanged():
    saturday_noon = datetime(2026, 10, 10, 16, 0, tzinfo=timezone.utc)
    assert pf.is_price_stale("2026-10-02", "CIT", saturday_noon, ticker="CIT:P-F") is False       # 8 days old: fine for a statement price
    assert pf.is_price_stale("2026-10-02", "STOCK", saturday_noon, ticker="AAPL") is True          # but a week-old stock price is stale
    assert pf.is_price_stale("2026-10-08", "MUTUAL_FUND", datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc), ticker="VFIAX") is False      # NAV rule as before


# ── the setting ───────────────────────────────────────────────────────────────

def test_the_window_is_a_database_setting_with_a_default_validated_and_resettable():
    assert sdb.get_portfolio_settings()["statement_price_max_age_days"] == 100
    auth_users_db.invite_user(f"{ME.actor}@example.com", ME.actor, "admin")
    assert inv.update_portfolio_settings(ME, statement_price_max_age_days=120)["statement_price_max_age_days"] == 120
    for bad in (0, -5, 731, "x"):
        with pytest.raises(ValueError):
            inv.update_portfolio_settings(ME, statement_price_max_age_days=bad)
    assert inv.reset_portfolio_settings(ME)["statement_price_max_age_days"] == 100


# ── Portfolio Health ──────────────────────────────────────────────────────────

def _cit(acct, name, price_as_of, itype="CIT", value=1000.0):
    inst = accts.create_synthetic_instrument(ME, plan_name="Acme Plan", fund_name=name, instrument_type=itype if itype in ("CIT", "STABLE_VALUE") else "CIT")
    accts.add_position(ME, acct["account_id"], {"ticker": inst["ticker"], "shares": 10, "current_price": value / 10, "price_as_of": price_as_of})
    return inst["ticker"]


def test_a_statement_price_inside_the_window_is_not_flagged_and_one_outside_says_it_needs_the_next_statement():
    acct = _account(account_type="401K")
    _hold(acct, "AAA", 1000.0)
    young, old = _cit(acct, "Young Fund", D(90)), _cit(acct, "Old Stable Value", D(120), itype="STABLE_VALUE")
    c = _c(_health(), "data_quality")
    assert ph.RC_STATEMENT_PRICE_STALE in c.reason_codes and ph.RC_PRICE_STALE not in c.reason_codes
    stale = next(i for i in c.metrics["issues"] if i["code"] == ph.RC_STATEMENT_PRICE_STALE)
    assert stale["tickers"] == [old] and young not in stale["tickers"]
    assert "needs next statement (price older than 100 days) (" + old + ")" in c.summary


def test_the_window_setting_changes_the_verdict_without_waiting_for_a_cache_to_expire():
    acct = _account(account_type="401K")
    _hold(acct, "AAA", 1000.0)
    t = _cit(acct, "Fund", D(120))
    assert ph.RC_STATEMENT_PRICE_STALE in _c(_health(), "data_quality").reason_codes
    auth_users_db.invite_user(f"{ME.actor}@example.com", ME.actor, "admin")
    inv.update_portfolio_settings(ME, statement_price_max_age_days=130)
    after = ph.get_portfolio_health_for_owner(ME.actor, repo.get_portfolio_by_owner(ME.actor), now=NOW)          # NO cache clear in between
    assert ph.RC_STATEMENT_PRICE_STALE not in _c(after, "data_quality").reason_codes and t


def test_a_statement_priced_fund_with_no_price_date_is_unknown_not_fresh_and_market_prices_keep_their_own_rule():
    acct = _account(account_type="401K")
    _hold(acct, "AAA", 1000.0)
    inst = accts.create_synthetic_instrument(ME, plan_name="P", fund_name="Dateless")
    pos = accts.add_position(ME, acct["account_id"], {"ticker": inst["ticker"], "shares": 10, "current_price": 5.0})
    repo.update_position(pos["position_id"], touch=False, price_as_of=None)                                           # a price with no date
    _hold(acct, "OLDPX", 1000.0, price_as_of="2026-09-01")
    c = _c(_health(), "data_quality")
    assert ph.RC_PRICE_DATE_UNKNOWN in c.reason_codes and ph.RC_PRICE_STALE in c.reason_codes and ph.RC_STATEMENT_PRICE_STALE not in c.reason_codes
    assert "price date unknown (CIT:P-DATELESS)" in c.summary and "price out of date (OLDPX)" in c.summary


# ── an imported statement end to end ──────────────────────────────────────────

def _import_statement(monkeypatch):
    monkeypatch.setattr(lp, "_call_llm", lambda prompt: json.dumps(EMPOWER_ANSWER))
    commit_plan(build_plan(parse_upload(make_pdf(EMPOWER_STYLE), "q1.pdf")), fetch_prices=False)
    return repo.list_accounts()[0]["account_id"]


def test_statement_prices_are_dated_by_the_statement_and_age_out_after_the_window(monkeypatch):
    acct = _import_statement(monkeypatch)
    pos = {p["ticker"]: p for p in repo.list_positions(acct)}
    cits = [t for t in pos if t.startswith("CIT:")]
    assert len(cits) == 5 and {pos[t]["price_as_of"] for t in cits} == {"2026-03-31"}
    inside = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)                                # 100 days after 2026-03-31
    outside = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
    for t in cits:
        assert pf.is_price_stale(pos[t]["price_as_of"], repo.find_instrument(t)["instrument_type"], inside, ticker=t) is False
        assert pf.is_price_stale(pos[t]["price_as_of"], repo.find_instrument(t)["instrument_type"], outside, ticker=t) is True


def test_market_priced_holdings_in_the_same_statement_still_refresh_and_statement_priced_ones_are_left_alone(monkeypatch):
    acct = _import_statement(monkeypatch)
    before = {p["ticker"]: dict(p) for p in repo.list_positions(acct)}
    monkeypatch.setattr(holdings_prices, "fetch_last_closes", lambda tickers: ({t: 95.0 for t in tickers}, "2026-10-13"))
    out = holdings_prices.refresh_holding_prices()
    assert out["updated"] == 1 and out["price_as_of"] == "2026-10-13"
    after = {p["ticker"]: p for p in repo.list_positions(acct)}
    assert (after["ACME"]["current_price"], after["ACME"]["price_as_of"]) == (95.0, "2026-10-13")              # the company stock follows the market
    for t, b in before.items():
        if t.startswith("CIT:"):
            assert (after[t]["current_price"], after[t]["current_value"], after[t]["price_as_of"]) == (b["current_price"], b["current_value"], "2026-03-31")


def test_the_accounts_page_note_says_when_the_next_statement_is_due_or_overdue():
    import ast
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "web" / "components" / "accounts_panel.py").read_text()
    ns: dict = {}
    fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "_statement_price_note")
    exec(compile(ast.Module([fn], []), "accounts_note", "exec"), ns)
    note = ns["_statement_price_note"]
    today = datetime.now(timezone.utc).date()
    cit = lambda d: {"ticker": "CIT:P-F", "instrument_type": "CIT", "price_as_of": d.isoformat() if d else None}
    assert note([{"ticker": "AAPL", "instrument_type": "STOCK", "price_as_of": today.isoformat()}]) is None
    fresh = note([cit(today - timedelta(days=10))])
    assert "next one due by" in fresh["text"] and fresh["color"] == "#64748B"
    old = note([cit(today - timedelta(days=150)), cit(today - timedelta(days=20))])
    assert old["text"].startswith("Needs next statement — 1 fund price") and "older than 100 days" in old["text"] and old["color"] == "#D97706"
    assert "price date unknown" in note([cit(None)])["text"]
