"""Phase 5: 401(k) extras — plan facts on the account (plan, provider, statement date, vested, loan), unvested money kept out of the health checks and a
loan never counted as a holding, the fact-sheet breakdown form for a fund with no ticker, and employer stock as an ordinary holding in the restricted-list
and concentration checks."""
from __future__ import annotations

import json
import sqlite3
from datetime import date

import pytest
from streamlit.testing.v1 import AppTest

import portfolio_agent.tools.db as db_module
from portfolio_agent.domain import LOCAL_OWNER
from portfolio_agent.services import account_service as accts
from portfolio_agent.services import fund_data_service as fds
from portfolio_agent.services import investor_service as inv
from portfolio_agent.services.context import RequestContext, _mint_identity_for_tests, local_context
from portfolio_agent.tools import auth_users_db
from portfolio_agent.tools import holdings_db as repo
from portfolio_agent.tools import llm_holdings_parser as lp
from portfolio_agent.tools import pdf_statement as ps
from portfolio_agent.tools import portfolio_health as ph
from portfolio_agent.tools import portfolio_risk as risk
from portfolio_agent.tools import restricted_list_db
from portfolio_agent.tools.holdings_import import build_plan, commit_plan, parse_upload
from tests.fixtures.statement_texts import EMPOWER_STYLE, PRINCIPAL_STYLE
from tests.helpers_pdf import make_pdf
from tests.test_portfolio_health import NOW, _account, _c, _health, _hold
from tests.test_statement_extraction import EMPOWER_ANSWER, PRINCIPAL_ANSWER

ME = local_context("test")
PLAN = "Acme Corporation 401(k) Retirement Savings Plan"


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(risk, "compute_portfolio_risk_context", lambda *a, **k: {})


def other_ctx():
    with repo.unit_of_work() as c:
        c.execute("INSERT OR IGNORE INTO portfolios (owner) VALUES ('someone-else')")
    return RequestContext(identity=_mint_identity_for_tests("someone-else"), source="test")


# ── account fields ────────────────────────────────────────────────────────────

def test_plan_facts_are_nullable_validated_audited_and_clearable():
    a = accts.create_account(ME, broker="manual", display_name="Work 401k", account_type="401K")
    assert all(a.get(k) is None for k in ("plan_name", "provider", "vested_balance", "loan_balance", "statement_period_end"))
    out = accts.set_retirement_details(ME, a["account_id"], plan_name=" Acme 401(k) ", provider="Empower", vested_balance="12000.456", loan_balance=500,
                                       statement_period_end="2026-03-31", reason="typed in")
    assert (out["plan_name"], out["provider"], out["vested_balance"], out["loan_balance"], out["statement_period_end"]) == ("Acme 401(k)", "Empower", 12000.46, 500.0, "2026-03-31")
    ev = [e for e in repo.get_audit_events(limit=10, account_id=a["account_id"]) if e["operation"] == "set_retirement_details"]
    assert len(ev) == 1 and ev[0]["after"]["plan_name"] == "Acme 401(k)" and ev[0]["reason"] == "typed in"
    accts.set_retirement_details(ME, a["account_id"], loan_balance=500)                       # no change: no second audit event
    assert len([e for e in repo.get_audit_events(limit=10, account_id=a["account_id"]) if e["operation"] == "set_retirement_details"]) == 1
    cleared = accts.set_retirement_details(ME, a["account_id"], loan_balance=None, provider="")
    assert cleared["loan_balance"] is None and cleared["provider"] is None and cleared["plan_name"] == "Acme 401(k)"      # only what was passed changes
    for bad in ({"loan_balance": -1}, {"vested_balance": "x"}, {"statement_period_end": "31/03/2026"}, {"nope": 1}):
        with pytest.raises(ValueError):
            accts.set_retirement_details(ME, a["account_id"], **bad)
    with pytest.raises(accts.VersionConflict):
        accts.set_retirement_details(ME, a["account_id"], expected_version=1, plan_name="x")
    with pytest.raises((accts.NotAuthorized, accts.NotFound)):
        accts.set_retirement_details(other_ctx(), a["account_id"], plan_name="mine now")


def test_the_new_columns_are_added_to_an_existing_database_idempotently():
    a = accts.create_account(ME, broker="manual", display_name="Old", account_type="401K")["account_id"]
    accts.add_position(ME, a, {"ticker": "VTI", "shares": 1, "current_price": 1.0})
    with sqlite3.connect(db_module.DB_PATH) as c:
        for col in ("plan_name", "provider", "vested_balance", "loan_balance", "statement_period_end"):
            c.execute(f"ALTER TABLE accounts DROP COLUMN {col}")
        c.execute("DROP VIEW holdings")
        c.execute("ALTER TABLE positions DROP COLUMN vested_value")
    for _ in range(2):
        repo.get_account(a)
    with sqlite3.connect(db_module.DB_PATH) as c:
        assert {"plan_name", "provider", "vested_balance", "loan_balance", "statement_period_end"} <= {r[1] for r in c.execute("PRAGMA table_info(accounts)")}
        assert "vested_value" in {r[1] for r in c.execute("PRAGMA table_info(positions)")}
    assert repo.get_holdings()[0]["vested_value"] is None and len(repo.get_holdings()) == 1              # existing data untouched, the view works


# ── reading loan / vested from a statement ────────────────────────────────────

def test_loan_and_vested_balances_are_read_from_the_text_and_otherwise_none():
    pages = ["Plan 401(k)\nEnding balance $130,000.00\nVested balance $124,000.00\nOutstanding loan balance $4,000.00\nLoan repayments this quarter $300.00"]
    assert ps.extract_vested_balance(pages) == 124000.0 and ps.extract_loan_balance(pages) == 4000.0
    assert ps.extract_vested_balance(EMPOWER_STYLE) == 124000.0 and ps.extract_loan_balance(EMPOWER_STYLE) is None
    assert ps.extract_loan_balance(["You may borrow from your account. Loan information is on our website."]) is None
    assert ps.extract_vested_balance(["nothing here"]) is None and ps.extract_loan_balance([]) is None


def _pages_with_loan(loan=4000.0, ending=130563.95):
    p = list(EMPOWER_STYLE)
    p[0] = p[0].replace("Ending balance $126,563.95", f"Ending balance ${ending:,.2f}\nOutstanding loan balance ${loan:,.2f}")
    return p


def parsed(monkeypatch, pages, answer):
    monkeypatch.setattr(lp, "_call_llm", lambda prompt: json.dumps(answer))
    return parse_upload(make_pdf(pages), "q1.pdf")


def test_a_difference_equal_to_the_loan_is_explained_as_the_loan_not_as_a_missed_fund(monkeypatch):
    p = parsed(monkeypatch, _pages_with_loan(), EMPOWER_ANSWER)
    assert p.statement.loan_balance == 4000.0 and p.statement.check["loan_explains"] is True and p.statement.check["difference"] == 4000.0
    assert any("matches the plan loan balance of 4,000.00, which is not a holding" in w for w in p.warnings) and not any("missed fund" in w for w in p.warnings)


# ── an imported statement records the plan facts and keeps the loan out of the holdings ──

def test_import_records_plan_facts_on_the_main_account_and_the_loan_is_not_a_holding(monkeypatch):
    p = parsed(monkeypatch, _pages_with_loan(), EMPOWER_ANSWER)
    res = commit_plan(build_plan(p), fetch_prices=False)
    [acct] = repo.list_accounts()
    assert (acct["plan_name"], acct["provider"], acct["statement_period_end"], acct["vested_balance"], acct["loan_balance"]) == \
        (PLAN, "Empower", "2026-03-31", 124000.0, 4000.0)
    holdings = repo.get_holdings(portfolio_id=acct["portfolio_id"])
    assert round(sum(h["current_value"] for h in holdings), 2) == 126563.95 and not any("LOAN" in h["ticker"] for h in holdings)     # the loan is nowhere in the holdings
    assert "set_retirement_details" in [e["operation"] for e in repo.get_audit_events(limit=100, account_id=acct["account_id"])]
    # a later statement that does not mention the loan leaves the recorded one alone (nothing is cleared on a guess)
    commit_plan(build_plan(parsed(monkeypatch, EMPOWER_STYLE, EMPOWER_ANSWER)), fetch_prices=False)
    assert repo.get_account(acct["account_id"])["loan_balance"] == 4000.0 and res.is_statement


def test_roth_sibling_gets_the_plan_name_and_period_but_not_the_plan_wide_loan_and_vested_totals(monkeypatch):
    p = parsed(monkeypatch, PRINCIPAL_STYLE, PRINCIPAL_ANSWER)
    commit_plan(build_plan(p), fetch_prices=False)
    by = {a["account_type"]: a for a in repo.list_accounts()}
    assert by["ROTH_401K"]["plan_name"] == "Initech Retirement Savings Plan 401(k)" and by["ROTH_401K"]["provider"] == "Principal"
    assert by["ROTH_401K"]["vested_balance"] is None and by["ROTH_401K"]["loan_balance"] is None


def test_position_level_vesting_comes_from_stated_amounts_and_the_employee_rule_never_from_a_guess(monkeypatch):
    p = parsed(monkeypatch, PRINCIPAL_STYLE, PRINCIPAL_ANSWER)
    commit_plan(build_plan(p), fetch_prices=False)
    main = next(a for a in repo.list_accounts() if a["account_type"] == "401K")
    pos = {x["ticker"]: x for x in repo.list_positions(main["account_id"])}
    tdf = next(v for k, v in pos.items() if "LIFETIME" in k)
    assert (tdf["current_value"], tdf["vested_value"]) == (4703.31, 4703.31)                         # pre-tax (employee) + a stated vested match
    sv = next(v for k, v in pos.items() if "STABLE" in k)
    assert sv["current_value"] == 3099.0 and sv["vested_value"] is None                                # the employer line has no stated vested amount: unknown


# ── health: unvested money is left out and shown apart ────────────────────────

def _plan_account():
    return _account(account_type="401K", name="Work 401k")


def _vest(acct, ticker, value, vested, **kw):
    h = _hold(acct, ticker, value, **kw)
    pos = repo.find_position(acct["account_id"], ticker)
    repo.update_position(pos["position_id"], touch=False, vested_value=vested)
    return h


def test_unvested_employer_money_is_excluded_from_the_totals_by_default_and_reported_separately():
    acct = _plan_account()
    _vest(acct, "ACME", 10000.0, 6000.0)                      # 4,000 unvested
    _hold(acct, "VTI", 5000.0)                                # no vesting information: counted in full
    pid = repo.get_portfolio_by_owner(LOCAL_OWNER)
    inp = ph.gather_inputs(LOCAL_OWNER, pid, now=NOW)
    vals = {h["ticker"]: ph._value(h) for h in inp.holdings}
    assert vals == {"ACME": 6000.0, "VTI": 5000.0}
    assert inp.exposures.per_holding_value["ACME"] == 6000.0 and inp.exposures.holdings_count == 2
    assert inp.unvested == {"excluded": True, "total": 4000.0, "accounts": [{"account_id": acct["account_id"], "name": "Work 401k", "amount": 4000.0}]}
    h = _health()
    assert h.unvested["total"] == 4000.0 and h.to_dict()["unvested"]["excluded"] is True
    stored = {x["ticker"]: x for x in repo.get_holdings()}
    assert stored["ACME"]["vested_value"] == 6000.0 and stored["ACME"]["shares"] * stored["ACME"]["current_price"] == 10000.0     # the stored position is untouched


def test_the_setting_turns_the_exclusion_off_and_the_health_cache_notices():
    acct = _plan_account()
    _vest(acct, "ACME", 10000.0, 6000.0)
    _hold(acct, "VTI", 5000.0)
    first = _health()
    assert first.unvested["excluded"] is True
    auth_users_db.invite_user(f"{ME.actor}@example.com", ME.actor, "admin")
    assert inv.update_portfolio_settings(ME, health_exclude_unvested=0.0)["health_exclude_unvested"] == 0
    again = ph.get_portfolio_health_for_owner(LOCAL_OWNER, repo.get_portfolio_by_owner(LOCAL_OWNER), now=NOW)         # no cache clear
    assert again.unvested == {"excluded": False, "total": 4000.0, "accounts": again.unvested["accounts"]} and again.version != first.version
    assert inv.reset_portfolio_settings(ME)["health_exclude_unvested"] == 1


def test_apply_vesting_edge_cases():
    hs = [{"ticker": "A", "account_id": 1, "current_value": 100.0, "vested_value": 100.0}, {"ticker": "B", "account_id": 1, "current_value": 100.0, "vested_value": None},
          {"ticker": "C", "account_id": 1, "current_value": None, "vested_value": 5.0}, {"ticker": "D", "account_id": 1, "current_value": 100.0, "vested_value": 150.0},
          {"ticker": "E", "account_id": 2, "current_value": 100.0, "vested_value": 0.0}]
    out, info = ph.apply_vesting(hs, {1: {"display_name": "One"}, 2: {"account_number": "two"}}, exclude=True)
    assert [h["current_value"] for h in out] == [100.0, 100.0, None, 100.0, 0.0]            # only E is cut (to 0 vested); the rest have nothing unvested to cut
    assert info == {"excluded": True, "total": 100.0, "accounts": [{"account_id": 2, "name": "two", "amount": 100.0}]}
    assert ph.apply_vesting(hs, {}, exclude=False)[0] == hs and ph.apply_vesting([], {}, exclude=True)[1] == {"excluded": False, "total": 0.0, "accounts": []}


# ── employer stock is an ordinary holding ─────────────────────────────────────

def test_employer_stock_in_a_401k_is_in_the_restricted_list_and_concentration_checks():
    acct = _plan_account()
    _hold(acct, "ACME", 40000.0, itype="STOCK")                                  # the employer's stock, held inside the 401(k)
    other = _account(name="Brokerage")
    for i in range(10):
        _hold(other, f"S{i:02d}", 6000.0)
    h = _health()
    conc = _c(h, "concentration")
    assert conc.metrics["top_position"]["ticker"] == "ACME" and conc.metrics["top_position"]["pct"] == pytest.approx(40.0) and conc.status == "ACTION"
    assert _c(h, "compliance").status != "ACTION"                              # not restricted yet
    restricted_list_db.add_restricted("ACME", "employer insider window", LOCAL_OWNER)
    c = _c(_health(), "compliance")
    assert c.status == "ACTION" and "ACME" in c.summary and "employer insider window" in c.summary
    # and it follows the 401(k) account's own slice too
    h2 = _health()
    acct_scope = next(v for k, v in h2.by_account.items() if str(k) == str(acct["account_id"]))
    assert acct_scope.check("concentration").metrics["top_position"]["ticker"] == "ACME"


def test_a_statements_company_stock_line_becomes_a_normal_stock_holding(monkeypatch):
    commit_plan(build_plan(parsed(monkeypatch, EMPOWER_STYLE, EMPOWER_ANSWER)), fetch_prices=False)
    acct = repo.list_accounts()[0]["account_id"]
    stock = next(p for p in repo.list_positions(acct) if p["ticker"] == "ACME")
    assert stock["shares"] == 104 and not stock["ticker"].startswith("CIT:") and stock["account_id"] == acct
    h = next(h for h in repo.get_holdings() if h["ticker"] == "ACME")
    assert h["account_type"] == "401K"


# ── the fact-sheet breakdown form ─────────────────────────────────────────────

def _cit_account():
    a = accts.create_account(ME, broker="manual", display_name="Work 401k", account_type="401K")["account_id"]
    inst = accts.create_synthetic_instrument(ME, plan_name="Acme", fund_name="S&P 500 CIT", asset_class="US_EQUITY")
    accts.add_position(ME, a, {"ticker": inst["ticker"], "shares": 10, "current_price": 100.0, "price_as_of": "2026-03-31"})
    return inst["ticker"]


def _form():
    import sys
    from pathlib import Path as P
    sys.path.insert(0, str(P.cwd()))
    import web.components.holdings_import as comp
    from portfolio_agent.services.context import local_context as lc
    comp.current_context = lambda s: lc("web")
    comp.render_manual_breakdown_form("t")


def _run():
    at = AppTest.from_function(_form, default_timeout=60).run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def test_the_breakdown_form_only_saves_a_total_of_100_and_stores_it_privately():
    t = _cit_account()
    at = _run()
    assert [n.label for n in at.number_input][:4] == ["Stocks (%)", "Bonds (%)", "Cash (%)", "Other (%)"]
    save = next(b for b in at.button if b.label == "Save breakdown")
    assert save.disabled is True                                              # nothing entered: 0%, not 100%
    at.number_input(key=f"t_ac_{t}_stock").set_value(70.0).run()
    at.number_input(key=f"t_ac_{t}_bond").set_value(20.0).run()
    assert next(b for b in at.button if b.label == "Save breakdown").disabled is True
    assert any("must add up to 100%" in w.value for w in at.warning)
    at.number_input(key=f"t_ac_{t}_cash").set_value(10.0).run()
    assert next(b for b in at.button if b.label == "Save breakdown").disabled is False
    at.number_input(key=f"t_sc_{t}_Technology").set_value(60.0).run()
    at.number_input(key=f"t_sc_{t}_Healthcare").set_value(40.0).run()
    at.button(key="t_save").click().run()
    assert not at.exception, [e.value for e in at.exception]
    saved = fds.get_manual_breakdown_for(ME, t)
    assert saved["asset_class"] == {"stock": 70.0, "bond": 20.0, "cash": 10.0} and saved["sector"] == {"Technology": 60.0, "Healthcare": 40.0}
    assert saved["as_of"] == date.today().isoformat()
    with pytest.raises(fds.NotYourFund):
        fds.get_manual_breakdown_for(other_ctx(), t)                                          # another owner cannot read it
    again = _run()
    assert any("Your current breakdown: stock 70%" in c.value for c in again.caption)
    again.button(key="t_clear").click().run()
    assert fds.get_manual_breakdown_for(ME, t) is None


def test_the_form_says_so_when_you_hold_no_fund_with_no_ticker():
    at = _run()
    assert any("will appear here" in c.value for c in at.caption) and not at.number_input


def test_a_server_side_sector_split_that_does_not_add_up_is_refused_whatever_the_form_did():
    t = _cit_account()
    with pytest.raises(ValueError, match="100"):
        fds.set_manual_breakdown(ME, t, {"stock": 100.0}, {"Technology": 50.0})
    assert fds.get_manual_breakdown_for(ME, t) is None


def test_the_accounts_page_wires_the_plan_line_the_edit_fields_and_the_breakdown_form():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "web" / "components" / "accounts_panel.py").read_text()
    assert "_plan_line" in src and "set_retirement_details" in src and "render_manual_breakdown_form" in src and "not a holding" in src
