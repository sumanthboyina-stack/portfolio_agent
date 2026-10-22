"""The statement preview in the shared import flow (AppTest): the funds-with-no-ticker section, the remove-missing box (off), the account choice,
and Confirm & import."""
from __future__ import annotations

import pytest
from streamlit.testing.v1 import AppTest

import portfolio_agent.tools.db as db_module
from portfolio_agent.tools import holdings_db as repo


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")


def _flow():
    import json
    import sys
    from pathlib import Path as P
    sys.path.insert(0, str(P.cwd()))
    import streamlit as st
    import web.components.holdings_import as comp
    from portfolio_agent.services.context import local_context
    from portfolio_agent.tools import llm_holdings_parser as lp
    from portfolio_agent.tools.holdings_import import parse_upload
    from tests.fixtures.statement_texts import EMPOWER_STYLE
    from tests.helpers_pdf import make_pdf
    from tests.test_statement_extraction import EMPOWER_ANSWER
    comp.current_context = lambda source: local_context("import")
    if "import_flow_t" not in st.session_state:
        lp._call_llm = lambda prompt: json.dumps(EMPOWER_ANSWER)
        parsed = parse_upload(make_pdf(EMPOWER_STYLE), "q1.pdf")
        st.session_state["import_flow_t"] = {"step": "preview", "labels": {}, "parsed": parsed, "sig": ("q1.pdf", 1)}
    comp.render_import_flow("t")


def _run():
    at = AppTest.from_function(_flow, default_timeout=90).run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def test_the_statement_preview_shows_plan_balance_check_and_the_funds_with_no_ticker():
    at = _run()
    html = " ".join(m.value for m in at.markdown)
    assert "Retirement-plan statement" in html and "Acme Corporation 401(k) Retirement Savings Plan" in html and "Empower" in html
    assert "Funds add up to" in html and "ending balance" in html
    assert "Funds with no ticker" in html and "Acme Stable Value Fund" in html and "Acme S&amp;P 500 Index CIT" in html
    assert "New fund with no ticker" in " ".join(c.value for c in at.caption)
    rm = at.checkbox(key="t_remove_missing")
    assert rm.value is False and "not on this statement" in rm.label
    assert len([t for t in at.text_input if "Proxy ticker" in t.label]) == 5                       # one optional, empty proxy box per fund with no ticker
    assert all(t.value == "" for t in at.text_input if "Proxy ticker" in t.label)


def test_a_proxy_typed_in_the_preview_is_used_and_confirm_imports_the_statement():
    at = _run()
    box = next(t for t in at.text_input if "Proxy ticker" in t.label)
    box.input("voo").run()
    assert not at.exception, [e.value for e in at.exception]
    at.button(key="t_confirm").click().run()
    assert not at.exception, [e.value for e in at.exception]
    flash = at.session_state["holdings_flash"]
    assert "Statement imported" in flash and "Funds with no ticker: 5 new" in flash
    acct = repo.list_accounts()[0]["account_id"]
    assert len(repo.list_positions(acct)) == 6
    with repo.unit_of_work() as c:
        proxies = [r[0] for r in c.execute("SELECT proxy_ticker FROM instruments WHERE ticker LIKE 'CIT:%' AND proxy_ticker IS NOT NULL")]
    assert proxies == ["VOO"]


def test_the_lookup_button_only_appears_for_funds_that_could_be_public_funds():
    at = _run()
    assert not [b for b in at.button if "Look up possible tickers" in b.label]          # the sample's funds are collective trusts / stable value / a plan reserve
