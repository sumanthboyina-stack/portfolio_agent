"""Phase 3: a retirement-plan statement becomes a normal import plan. Funds with no ticker are matched to the ones you already hold (or created in the
commit, never in the preview), a statement keeps what it does not list unless told otherwise, prices come from the statement, and suggestions are only
ever suggestions. The model is a canned fake; nothing here touches the network."""
from __future__ import annotations

import json
import sqlite3

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.domain import synthetic_cit_ticker
from portfolio_agent.services import account_service as accts
from portfolio_agent.services.context import RequestContext, _mint_identity_for_tests
from portfolio_agent.tools import fund_name_lookup as fnl
from portfolio_agent.tools import holdings_db as repo
from portfolio_agent.tools import holdings_import as hi
from portfolio_agent.tools import llm_holdings_parser as lp
from portfolio_agent.tools.holdings_import import ImportOptions, build_plan, commit_plan, parse_upload
from tests.fixtures.statement_texts import EMPOWER_STYLE, PRINCIPAL_STYLE
from tests.helpers_holdings import CTX
from tests.helpers_pdf import make_pdf
from tests.test_statement_extraction import EMPOWER_ANSWER, PRINCIPAL_ANSWER

PLAN = "Acme Corporation 401(k) Retirement Savings Plan"


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")


def parsed(monkeypatch, pages=EMPOWER_STYLE, answer=EMPOWER_ANSWER):
    monkeypatch.setattr(lp, "_call_llm", lambda prompt: json.dumps(answer))
    return parse_upload(make_pdf(pages), "statement.pdf")


def positions(account_id):
    return {p["ticker"]: p for p in repo.list_positions(account_id)}


def key(fund, plan=PLAN):
    return synthetic_cit_ticker(plan, fund)


def import_statement(monkeypatch, **kw):
    p = parsed(monkeypatch, **{k: v for k, v in kw.items() if k in ("pages", "answer")})
    plan = build_plan(p, options=kw.get("options"))
    return plan, commit_plan(plan, fetch_prices=False)


# ── the preview creates nothing ───────────────────────────────────────────────

def test_the_preview_proposes_new_funds_with_no_ticker_but_creates_nothing(monkeypatch):
    plan = build_plan(parsed(monkeypatch))
    assert plan.is_statement and plan.broker == "empower" and plan.statement_date == "2026-03-31"
    assert plan.statement_meta["plan_name"] == PLAN and plan.check["ok"] is True
    [a] = plan.accounts
    assert (a.account_number, a.account_type, a.remove_missing) == ("****9012", "401K", False)
    by = {r["fund_name"]: r for r in a.rows}
    assert by["Acme Stable Value Fund"]["cit_status"] == "new" and by["Acme Stable Value Fund"]["ticker"] == key("Acme Stable Value Fund")
    assert by["Acme Corporation Common Stock (ACME)"]["ticker"] == "ACME" and by["Acme Corporation Common Stock (ACME)"].get("cit_status") is None
    assert len(a.funds_no_ticker) == 5 and sorted(a.added) == sorted(r["ticker"] for r in a.rows)
    assert repo.find_instrument(key("Acme Stable Value Fund")) is None
    with sqlite3.connect(db_module.DB_PATH) as c:
        assert c.execute("SELECT COUNT(*) FROM instruments WHERE ticker LIKE 'CIT:%'").fetchone()[0] == 0     # nothing was created by building the plan


# ── the commit ────────────────────────────────────────────────────────────────

def test_commit_creates_funds_with_the_right_types_prices_them_from_the_statement_and_leaves_cash_alone(monkeypatch):
    plan = build_plan(parsed(monkeypatch))
    acct = accts.create_account(CTX, broker="empower", account_number="****9012", display_name=PLAN, account_type="401K")["account_id"]
    accts.record_account_cash(CTX, acct, 321.0, as_of_date="2026-03-01")                 # a cash line the person recorded: a statement must not touch it
    res = commit_plan(build_plan(parsed(monkeypatch)), fetch_prices=False)
    assert res.is_statement and (res.added, res.updated, res.removed) == (6, 0, 0)
    assert sorted(res.funds_created) == sorted(key(n) for n in ("Acme Stable Value Fund", "Acme S&P 500 Index CIT", "Acme Target Retirement 2050 Fund",
                                                                "Acme International Equity CIT", "Acme Cash Reserve")) and res.funds_matched == []
    assert "Funds with no ticker: 5 new, 0 matched" in res.message and "Statement imported" in res.message
    pos = positions(acct)
    sv = repo.find_instrument(key("Acme Stable Value Fund"))
    assert (sv["instrument_type"], sv["asset_class"], sv["classification_source"]) == ("STABLE_VALUE", "FIXED_INCOME", "manual")
    assert repo.find_instrument(key("Acme Cash Reserve"))["instrument_type"] == "MONEY_MARKET" and repo.find_instrument(key("Acme Cash Reserve"))["asset_class"] == "CASH"
    tdf = repo.find_instrument(key("Acme Target Retirement 2050 Fund"))
    assert (tdf["instrument_type"], tdf["sub_asset_class"]) == ("CIT", "TARGET_DATE")
    assert repo.find_instrument(key("Acme S&P 500 Index CIT"))["instrument_type"] == "CIT"
    p = pos[key("Acme S&P 500 Index CIT")]
    assert (p["shares"], p["current_price"], p["current_value"], p["price_as_of"]) == (190.221, 210.33, 40010.74, "2026-03-31")   # the balance as printed
    assert pos["ACME"]["shares"] == 104 and pos["ACME"]["price_as_of"] == "2026-03-31"
    assert repo.get_account_cash(acct)["amount"] == 321.0                                   # untouched
    ev = [e for e in repo.get_audit_events(limit=100, account_id=acct) if e["operation"] == "set_position_price"]
    assert ev and all(e["reason"] == "quarterly statement 2026-01-01 – 2026-03-31" for e in ev) and len(ev) == 5
    run = repo.get_import_runs(limit=1)[0]
    assert run["broker"] == "empower" and run["added"] == 6


def test_a_balance_only_fund_keeps_its_balance_and_gets_the_statements_date_without_a_computed_price(monkeypatch):
    answer = [{"fund_name": "Guaranteed Income Fund", "instrument_hint": "STABLE_VALUE", "shares": None, "unit_price": None, "current_value": 5000.0,
               "account_name": PLAN, "account_number": "****9012"}]
    plan, res = import_statement(monkeypatch, answer=answer)
    acct = repo.list_accounts()[0]["account_id"]
    p = positions(acct)[key("Guaranteed Income Fund")]
    assert (p["shares"], p["current_price"], p["current_value"], p["price_as_of"]) == (None, None, 5000.0, "2026-03-31")


def test_reimporting_the_same_statement_updates_in_place_with_no_duplicates_and_no_deletions(monkeypatch):
    import_statement(monkeypatch)
    acct = repo.list_accounts()[0]["account_id"]
    before = {t: p["position_id"] for t, p in positions(acct).items()}
    plan = build_plan(parsed(monkeypatch))
    [a] = plan.accounts
    assert a.added == [] and a.removed == [] and a.matched_existing is False and a.account_id == acct
    assert {r["ticker"]: r["cit_status"] for r in a.funds_no_ticker} == {key(n): "matched" for n in (
        "Acme Stable Value Fund", "Acme S&P 500 Index CIT", "Acme Target Retirement 2050 Fund", "Acme International Equity CIT", "Acme Cash Reserve")}
    res = commit_plan(plan, fetch_prices=False)
    assert (res.added, res.removed) == (0, 0) and res.funds_created == [] and len(res.funds_matched) == 5
    assert {t: p["position_id"] for t, p in positions(acct).items()} == before                 # the very same rows
    # a changed price on a later statement is an UPDATE even though shares and cost did not change
    newer = [dict(r) for r in EMPOWER_ANSWER]
    newer[1] = {**newer[1], "unit_price": 220.0, "current_value": round(190.221 * 220.0, 2)}
    plan2 = build_plan(parsed(monkeypatch, answer=newer))
    assert [u["ticker"] for u in plan2.accounts[0].updated] == [key("Acme S&P 500 Index CIT")]


# ── matching the funds and accounts you already have ──────────────────────────

def manual_cit(fund, account_id, plan=PLAN, shares=10.0, price=5.0):
    inst = accts.create_synthetic_instrument(CTX, plan_name=plan, fund_name=fund, asset_class="US_EQUITY", proxy_ticker="VOO")
    accts.add_position(CTX, account_id, {"ticker": inst["ticker"], "description": inst["name"], "shares": shares, "current_price": price,
                                         "current_value": shares * price}, as_of_date="2026-01-01")
    return inst


def test_a_fund_you_added_by_hand_is_matched_by_its_key_or_its_normalised_name_and_updated_not_duplicated(monkeypatch):
    acct = accts.create_account(CTX, broker="manual", account_number=PLAN, display_name=PLAN, account_type="401K")["account_id"]
    exact = manual_cit("Acme S&P 500 Index CIT", acct)
    fuzzy = manual_cit("Acme International Equity Fund (CIT)", acct)               # a slightly different name for the same fund
    plan = build_plan(parsed(monkeypatch))
    [a] = plan.accounts
    assert a.matched_existing and a.account_id == acct and (a.broker, a.account_number) == ("manual", PLAN)      # the plan's account is the one you already have
    by = {r["fund_name"]: r for r in a.rows}
    assert by["Acme S&P 500 Index CIT"]["cit_status"] == "matched" and by["Acme S&P 500 Index CIT"]["ticker"] == exact["ticker"]
    assert by["Acme International Equity CIT"]["cit_status"] == "matched" and by["Acme International Equity CIT"]["ticker"] == fuzzy["ticker"]
    assert by["Acme Stable Value Fund"]["cit_status"] == "new"
    assert sorted(a.updated and [u["ticker"] for u in a.updated]) == sorted([exact["ticker"], fuzzy["ticker"]]) and exact["ticker"] not in a.added and exact["ticker"] not in a.removed
    res = commit_plan(plan, fetch_prices=False)
    assert len(repo.list_accounts()) == 1 and sorted(res.funds_matched) == sorted([exact["ticker"], fuzzy["ticker"]]) and len(res.funds_created) == 3
    assert positions(acct)[exact["ticker"]]["shares"] == 190.221 and positions(acct)[exact["ticker"]]["price_as_of"] == "2026-03-31"
    assert repo.find_instrument(exact["ticker"])["proxy_ticker"] == "VOO"                  # your proxy survives a re-import


def test_two_funds_that_look_alike_are_never_guessed_between(monkeypatch):
    acct = accts.create_account(CTX, broker="manual", account_number="x", display_name=PLAN, account_type="401K")["account_id"]
    manual_cit("Acme International Equity Fund (CIT)", acct)
    manual_cit("Acme International Equity Trust (CIT)", acct)
    plan = build_plan(parsed(monkeypatch))
    r = next(r for r in plan.accounts[0].rows if r["fund_name"] == "Acme International Equity CIT")
    assert r["cit_status"] == "new" and r["ambiguous"] is True


def test_a_choice_in_the_preview_overrides_the_automatic_match(monkeypatch):
    acct = accts.create_account(CTX, broker="manual", account_number="x", display_name=PLAN, account_type="401K")["account_id"]
    mine = manual_cit("My Old Name For The Index Fund", acct)
    fk = key("Acme S&P 500 Index CIT")
    plan = build_plan(parsed(monkeypatch), options=ImportOptions(funds={fk: {"map_to": mine["ticker"]}}))
    r = next(r for r in plan.accounts[0].rows if r["fund_name"] == "Acme S&P 500 Index CIT")
    assert (r["cit_status"], r["ticker"]) == ("matched", mine["ticker"])
    forced = build_plan(parsed(monkeypatch), options=ImportOptions(funds={fk: {"map_to": "__new__"}}))
    assert next(r for r in forced.accounts[0].rows if r["fund_name"] == "Acme S&P 500 Index CIT")["cit_status"] == "new"


def test_other_owners_funds_are_never_matched_or_shown(monkeypatch):
    other = RequestContext(identity=_mint_identity_for_tests("someone-else"), source="test")
    with repo.unit_of_work() as c:
        c.execute("INSERT OR IGNORE INTO portfolios (owner) VALUES ('someone-else')")
    theirs = accts.create_account(other, broker="manual", account_number="t", display_name=PLAN, account_type="401K")["account_id"]
    inst = accts.create_synthetic_instrument(other, plan_name=PLAN, fund_name="Acme S&P 500 Index CIT")
    accts.add_position(other, theirs, {"ticker": inst["ticker"], "shares": 1, "current_price": 1.0}, as_of_date="2026-01-01")
    assert accts.list_cit_instruments_for(CTX) == [] and [c["ticker"] for c in accts.list_cit_instruments_for(other)] == [inst["ticker"]]
    r = next(r for r in build_plan(parsed(monkeypatch)).accounts[0].rows if r["fund_name"] == "Acme S&P 500 Index CIT")
    assert r["cit_status"] == "new" and not r.get("cit_name")                          # not matched to theirs, and nothing of theirs is shown


# ── what a statement keeps ────────────────────────────────────────────────────

def test_a_statement_without_a_cit_row_does_not_remove_a_fund_you_added_by_hand_unless_you_tick_the_box(monkeypatch):
    acct = accts.create_account(CTX, broker="manual", account_number="x", display_name=PLAN, account_type="401K")["account_id"]
    keep = manual_cit("Some Plan Fund Not On This Statement", acct)
    only_stock = [r for r in EMPOWER_ANSWER if r["fund_name"].endswith("(ACME)")]
    plan = build_plan(parsed(monkeypatch, answer=only_stock))
    [a] = plan.accounts
    assert a.remove_missing is False and a.removed == [] and a.kept_missing == [keep["ticker"]]
    res = commit_plan(plan, fetch_prices=False)
    assert keep["ticker"] in positions(acct) and res.removed == 0 and res.kept == [keep["ticker"]] and "Kept (not on this statement)" in res.message
    removing = build_plan(parsed(monkeypatch, answer=only_stock), options=ImportOptions(remove_missing=True))
    assert removing.accounts[0].removed == [keep["ticker"]]
    commit_plan(removing, fetch_prices=False)
    assert keep["ticker"] not in positions(acct) and "ACME" in positions(acct)


def test_a_broker_csv_still_replaces_the_whole_account_by_default(monkeypatch):
    from tests.test_holdings_import import FIDELITY_CSV
    acct = accts.create_account(CTX, broker="fidelity", account_number="X12345678", display_name="Individual - TOD")["account_id"]
    keep = manual_cit("Manual Fund", acct)
    plan = build_plan(parse_upload(FIDELITY_CSV, "p.csv"))
    assert plan.accounts[0].remove_missing is True and keep["ticker"] in plan.accounts[0].removed and not plan.is_statement
    commit_plan(plan, fetch_prices=False)
    assert keep["ticker"] not in positions(acct)


def test_the_diff_keys_a_fund_with_no_ticker_by_its_synthetic_key():
    existing = [{"ticker": "CIT:P-FUND", "shares": 5, "cost_basis_total": None, "current_value": 50.0, "current_price": 10.0},
                {"ticker": "CIT:P-OTHER", "shares": 1, "cost_basis_total": None}]
    new = [{"ticker": "CIT:P-FUND", "shares": 5, "cost_basis_total": None, "current_value": 55.0, "current_price": 11.0}]
    assert hi._diff(existing, new) == ([], [], ["CIT:P-OTHER"], ["CIT:P-FUND"])                          # broker files: shares / cost only, absent = removed
    added, updated, removed, unchanged = hi._diff(existing, new, remove_missing=False, compare_values=True)
    assert (added, removed, unchanged) == ([], [], []) and [u["ticker"] for u in updated] == ["CIT:P-FUND"]


# ── accounts: Roth, matching, proxies ─────────────────────────────────────────

def test_roth_money_lands_in_its_own_account_and_both_are_created(monkeypatch):
    plan, res = import_statement(monkeypatch, pages=PRINCIPAL_STYLE, answer=PRINCIPAL_ANSWER)
    by_type = {a["account_type"]: a for a in repo.list_accounts()}
    assert set(by_type) == {"401K", "ROTH_401K"} and by_type["ROTH_401K"]["account_number"] == "****4455-ROTH" and by_type["ROTH_401K"]["display_name"].endswith("(Roth)")
    k = key("Principal LifeTime 2045 Separate Account", "Initech Retirement Savings Plan 401(k)")
    assert positions(by_type["401K"]["account_id"])[k]["shares"] == 150.65 and positions(by_type["ROTH_401K"]["account_id"])[k]["shares"] == 40.0
    assert plan.broker == "principal" and len(res.funds_created) == 2 and res.funds_matched == []     # one fund shared by both accounts is created once
    with sqlite3.connect(db_module.DB_PATH) as c:
        assert c.execute("SELECT COUNT(*) FROM instruments WHERE ticker = ?", [k]).fetchone()[0] == 1


def test_an_account_you_chose_in_the_preview_is_used(monkeypatch):
    mine = accts.create_account(CTX, broker="manual", account_number="my-401k", display_name="Work retirement", account_type="401K")["account_id"]
    plan = build_plan(parsed(monkeypatch), options=ImportOptions(account_map={"****9012": mine}))
    assert plan.accounts[0].matched_existing and plan.accounts[0].account_id == mine
    commit_plan(plan, fetch_prices=False)
    assert len(repo.list_accounts()) == 1 and len(positions(mine)) == 6


def test_a_proxy_is_set_on_a_new_fund_and_on_a_matched_fund_only_if_it_has_none(monkeypatch):
    fk = key("Acme S&P 500 Index CIT")
    commit_plan(build_plan(parsed(monkeypatch), options=ImportOptions(funds={fk: {"proxy": "VOO"}})), fetch_prices=False)
    assert repo.find_instrument(fk)["proxy_ticker"] == "VOO"
    fk2 = key("Acme International Equity CIT")
    commit_plan(build_plan(parsed(monkeypatch), options=ImportOptions(funds={fk2: {"proxy": "VXUS"}, fk: {"proxy": "SPY"}})), fetch_prices=False)
    assert repo.find_instrument(fk2)["proxy_ticker"] == "VXUS" and repo.find_instrument(fk)["proxy_ticker"] == "VOO"   # an existing proxy is never overwritten


# ── name -> ticker suggestions ────────────────────────────────────────────────

class FakeSearch:
    def __init__(self, quotes): self.quotes, self.queries = quotes, []
    def search(self, q):
        self.queries.append(q)
        return self.quotes


QUOTES = [{"symbol": "VTSAX", "longname": "Vanguard Total Stock Market Index Fund Admiral Shares", "quoteType": "MUTUALFUND"},
          {"symbol": "VTI", "longname": "Vanguard Total Stock Market ETF", "quoteType": "ETF"},
          {"symbol": "VTSAX2", "longname": "Totally Unrelated Thing", "quoteType": "MUTUALFUND"},
          {"symbol": "VTSMX", "longname": "Vanguard Total Stock Market Index Fund Investor Shares", "quoteType": "EQUITY"}]


def test_only_funds_that_look_public_get_a_lookup_and_only_close_fund_matches_are_suggested():
    ll = lambda name, hint="MUTUAL_FUND": {"ticker_less": True, "fund_name": name, "instrument_hint": hint, "fund_key": "k:" + name}
    rows = [ll("Vanguard Total Stock Market Index Fund Admiral Shares"), ll("Acme S&P 500 Index CIT", "CIT"), ll("Acme Stable Value Fund", "STABLE_VALUE"),
            ll("Fidelity Contrafund Collective Trust"), ll("Acme Cash Reserve", "MONEY_MARKET"), {"ticker_less": False, "fund_name": "Vanguard Growth Index Fund", "fund_key": "x"}]
    s = FakeSearch(QUOTES)
    out = fnl.suggest_tickers(rows, s)
    assert list(out) == ["k:Vanguard Total Stock Market Index Fund Admiral Shares"] and s.queries == ["Vanguard Total Stock Market Index Fund Admiral Shares"]
    cands = out["k:Vanguard Total Stock Market Index Fund Admiral Shares"]
    assert [c["symbol"] for c in cands][0] == "VTSAX" and all(c["score"] >= fnl.MIN_SCORE for c in cands)
    assert "VTSAX2" not in [c["symbol"] for c in cands] and "VTSMX" not in [c["symbol"] for c in cands]       # unrelated name; an EQUITY quote
    assert fnl.suggest_tickers(rows, FakeSearch([])) == {} and fnl.suggest_tickers(rows, type("B", (), {"search": lambda self, q: 1 / 0})()) == {}


def test_a_suggestion_changes_nothing_until_it_is_confirmed(monkeypatch):
    answer = [{"fund_name": "Vanguard Total Stock Market Index Fund Admiral Shares", "ticker": "", "instrument_hint": "MUTUAL_FUND", "shares": 10,
               "unit_price": 100.0, "current_value": 1000.0, "account_name": PLAN, "account_number": "****9012"}]
    fk = key("Vanguard Total Stock Market Index Fund Admiral Shares")
    plan = build_plan(parsed(monkeypatch, answer=answer))
    assert plan.accounts[0].rows[0]["ticker"] == fk and plan.accounts[0].rows[0]["cit_status"] == "new"      # the default: still a fund with no ticker
    confirmed = build_plan(parsed(monkeypatch, answer=answer), options=ImportOptions(funds={fk: {"ticker": "vtsax"}}))
    r = confirmed.accounts[0].rows[0]
    assert (r["ticker"], r["ticker_less"], r["resolved_by"]) == ("VTSAX", False, "confirmed lookup") and confirmed.accounts[0].funds_no_ticker[0]["fund_key"] == fk
    commit_plan(confirmed, fetch_prices=False)
    acct = repo.list_accounts()[0]["account_id"]
    assert list(positions(acct)) == ["VTSAX"] and repo.find_instrument(fk) is None                          # an ordinary fund; no fund-with-no-ticker was created


# ── nothing leaks into brokerage imports ──────────────────────────────────────

def test_create_synthetic_instrument_rejects_a_type_it_does_not_support_and_never_changes_an_existing_one():
    with pytest.raises(ValueError, match="instrument_type"):
        accts.create_synthetic_instrument(CTX, plan_name="P", fund_name="F", instrument_type="STOCK")
    a = accts.create_synthetic_instrument(CTX, plan_name="P", fund_name="F", instrument_type="STABLE_VALUE")
    b = accts.create_synthetic_instrument(CTX, plan_name="P", fund_name="F", instrument_type="CIT")
    assert a["instrument_type"] == b["instrument_type"] == "STABLE_VALUE"


def test_enrich_prices_never_asks_the_market_about_a_fund_with_no_ticker(monkeypatch):
    asked = []
    monkeypatch.setattr(hi, "fetch_last_closes", lambda tickers: asked.append(list(tickers)) or ({}, None))
    rows = [{"ticker": "CIT:P-F", "shares": 1, "current_price": None}, {"ticker": "AAPL", "shares": 1, "current_price": None}]
    hi.enrich_prices(rows)
    assert asked == [["AAPL"]]
