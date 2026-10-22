"""Phase 2: LLM extraction of retirement-plan statements, for any recordkeeper. The model is always a canned fake (conftest blocks live calls):
what is tested is the prompt (provider-neutral, redacted), the tolerance for how different models / companies name things, ticker-less funds surviving,
money-source aggregation into accounts, the ending-balance cross-check, and the upload path."""
from __future__ import annotations

import json

import pytest

from portfolio_agent.tools import llm_holdings_parser as lp
from portfolio_agent.tools import pdf_statement as ps
from portfolio_agent.tools.holdings_import import build_plan, parse_upload
from tests.fixtures.statement_texts import EMPOWER_STYLE, FIDELITY_STYLE, PRINCIPAL_STYLE, VANGUARD_403B_STYLE
from tests.helpers_pdf import make_pdf

PROVIDERS = ("empower", "fidelity", "vanguard", "principal", "voya", "schwab", "t. rowe", "transamerica")


def fake_llm(monkeypatch, *answers):
    """Return the given answers (JSON-able) one per call, recording every prompt."""
    prompts: list[str] = []
    it = iter(answers)

    def _call(prompt):
        prompts.append(prompt)
        a = next(it, "")
        return a if isinstance(a, str) else json.dumps(a)
    monkeypatch.setattr(lp, "_call_llm", _call)
    return prompts


def pdf(pages):
    return make_pdf(pages)


# ── model answers in different styles (what different models / companies produce) ──

EMPOWER_ANSWER = [
    {"fund_name": "Acme Stable Value Fund", "ticker": "", "instrument_hint": "STABLE_VALUE", "shares": 3120.441, "unit_price": 14.231, "current_value": 44405.62,
     "contribution_source": "", "vested_value": None, "account_name": "Acme Corporation 401(k) Retirement Savings Plan", "account_number": "****9012",
     "account_type": "401K", "as_of_date": "2026-03-31"},
    {"fund_name": "Acme S&P 500 Index CIT", "ticker": "", "instrument_hint": "CIT", "shares": 190.221, "unit_price": 210.33, "current_value": 40010.74,
     "contribution_source": "", "account_name": "Acme Corporation 401(k) Retirement Savings Plan", "account_type": "401K"},
    {"fund_name": "Acme Target Retirement 2050 Fund", "ticker": "", "instrument_hint": "TARGET_DATE_FUND", "shares": 812.5, "unit_price": 21.01, "current_value": 17070.63},
    {"fund_name": "Acme Corporation Common Stock (ACME)", "ticker": "ACME", "instrument_hint": "STOCK", "shares": 104, "unit_price": 87.4, "current_value": 9089.60},
    {"fund_name": "Acme International Equity CIT", "ticker": "", "instrument_hint": "CIT", "shares": 240, "unit_price": 65.72, "current_value": 15772.80},
    {"fund_name": "Acme Cash Reserve", "ticker": "", "instrument_hint": "MONEY_MARKET", "shares": 214.56, "unit_price": 1.0, "current_value": 214.56},
    {"fund_name": "Total ending balance", "ticker": "", "instrument_hint": "OTHER", "shares": None, "unit_price": None, "current_value": 126563.95},
]
# a different model: other key names, strings for numbers, tickers wrapped, a header echoed back
FIDELITY_ANSWER = {"holdings": [
    {"Investment": "Fidelity 500 Index Fund", "Symbol": "FXAIX", "Type": "mutual fund", "Quantity": "120.553", "Price": "$190.40", "Value": "$22,953.26"},
    {"investment_name": "Fidelity International Index Fund", "symbol": "fspsx", "units": 210.1, "price": 50.12, "balance": "10,530.41"},
    {"investment": "Managed Income Portfolio", "symbol": "N/A", "asset_type": "Stable Value", "units": "8,900.220", "price": 1.0, "balance": 8900.22},
    {"fund": "Globex Corp Stock Fund (GLBX)", "ticker": "GLBX", "type": "stock", "shares": 80, "price": 116, "value": 9280},
    {"fund": "Fidelity Government Cash Reserves", "ticker": "", "type": "money market", "shares": 436.2, "price": 1, "value": 436.20},
    {"fund": "Investment", "ticker": "", "shares": None, "value": None},                       # an echoed column header: dropped
]}
# per-source lines (units given for the main fund, dollars only for the stable value lines)
PRINCIPAL_ANSWER = [
    {"fund_name": "Principal LifeTime 2045 Separate Account", "instrument_hint": "TARGET_DATE_FUND", "shares": 120.55, "unit_price": 31.22, "current_value": 3763.55,
     "contribution_source": "Salary deferral", "account_name": "Initech Retirement Savings Plan 401(k)", "account_number": "****4455"},
    {"fund_name": "Principal LifeTime 2045 Separate Account", "instrument_hint": "TARGET_DATE_FUND", "shares": 40.0, "unit_price": 31.22, "current_value": 1248.80,
     "contribution_source": "Roth deferral", "account_name": "Initech Retirement Savings Plan 401(k)", "account_number": "****4455"},
    {"fund_name": "Principal LifeTime 2045 Separate Account", "instrument_hint": "TARGET_DATE_FUND", "shares": 30.1, "unit_price": 31.22, "current_value": 939.76,
     "contribution_source": "Safe harbor match", "vested_value": 939.76, "account_name": "Initech Retirement Savings Plan 401(k)", "account_number": "****4455"},
    {"fund_name": "Initech Stable Value Option", "instrument_hint": "STABLE_VALUE", "shares": None, "unit_price": None, "current_value": 2000.00,
     "contribution_source": "Salary deferral", "account_name": "Initech Retirement Savings Plan 401(k)", "account_number": "****4455"},
    {"fund_name": "Initech Stable Value Option", "instrument_hint": "STABLE_VALUE", "shares": None, "unit_price": None, "current_value": 1099.00,
     "contribution_source": "Safe harbor match", "account_name": "Initech Retirement Savings Plan 401(k)", "account_number": "****4455"},
]
VANGUARD_ANSWER = [
    {"fund_name": "Target Retirement 2040 Fund", "ticker": "VFORX", "instrument_hint": "TARGET_DATE_FUND", "shares": 300, "unit_price": 42.10, "current_value": 12630},
    {"fund_name": "Total Stock Market Index Fund", "ticker": "VTSAX", "instrument_hint": "MUTUAL_FUND", "shares": 150, "unit_price": 121, "current_value": 18150},
    {"fund_name": "Plan Cash Reserve", "ticker": "", "instrument_hint": "MONEY_MARKET", "shares": 100, "unit_price": 1, "current_value": 100},
]


def parse(monkeypatch, pages, answer):
    fake_llm(monkeypatch, answer)
    return lp.parse_statement_with_llm(pdf(pages))


# ── the prompt: provider-neutral and redacted ─────────────────────────────────

@pytest.mark.parametrize("pages,answer", [(EMPOWER_STYLE, EMPOWER_ANSWER), (FIDELITY_STYLE, FIDELITY_ANSWER), (PRINCIPAL_STYLE, PRINCIPAL_ANSWER), (VANGUARD_403B_STYLE, VANGUARD_ANSWER)])
def test_only_redacted_text_reaches_the_model_and_the_instructions_name_no_provider(monkeypatch, pages, answer):
    prompts = fake_llm(monkeypatch, answer)
    lp.parse_statement_with_llm(pdf(pages))
    assert len(prompts) == 1
    p = prompts[0]
    instructions = p.split("Statement text:")[0].lower()
    assert not any(name in instructions for name in PROVIDERS)                    # the rules describe WHAT to extract, never one company's layout
    for pii in ("Jordan", "Maple Grove", "62704", "555-0142", "jordan.sample", "6789", "07/14/1984", "123456789012", "998877665", "Pat Lee", "800-555-0100"):
        assert pii not in p, pii
    assert "Do NOT skip a fund because it has no ticker" in p and "NEVER fabricate" in p


def test_the_prompt_carries_the_plan_name_and_period_from_the_header_and_says_when_it_is_a_part(monkeypatch):
    prompts = fake_llm(monkeypatch, [])
    with pytest.raises(ValueError):
        lp.parse_statement_with_llm(pdf(EMPOWER_STYLE))
    assert "plan name: Acme Corporation 401(k) Retirement Savings Plan" in prompts[0] and "2026-01-01 to 2026-03-31" in prompts[0] and "(part" not in prompts[0]
    assert "[SSN]" in prompts[0] and "[PHONE]" in prompts[0]                        # the cover page is sent, with its personal details replaced


# ── ticker-less funds survive (any company) ───────────────────────────────────

def test_empower_style_keeps_cits_stable_value_target_date_and_money_market_and_drops_totals(monkeypatch):
    sp = parse(monkeypatch, EMPOWER_STYLE, EMPOWER_ANSWER)
    by = {r["fund_name"]: r for r in sp.rows}
    assert len(sp.rows) == 6 and "Total ending balance" not in by
    assert {n for n, r in by.items() if r["ticker_less"]} == {"Acme Stable Value Fund", "Acme S&P 500 Index CIT", "Acme Target Retirement 2050 Fund",
                                                              "Acme International Equity CIT", "Acme Cash Reserve"}
    assert by["Acme Stable Value Fund"]["instrument_hint"] == "STABLE_VALUE" and by["Acme Cash Reserve"]["instrument_hint"] == "MONEY_MARKET"
    stock = by["Acme Corporation Common Stock (ACME)"]
    assert stock["ticker"] == "ACME" and not stock["ticker_less"] and stock["instrument_hint"] == "STOCK"
    assert {r["account_type"] for r in sp.rows} == {"401K"}
    assert by["Acme Stable Value Fund"]["as_of_date"] == "2026-03-31" and by["Acme Target Retirement 2050 Fund"]["as_of_date"] == "2026-03-31"   # given, or the header's period end
    assert [r["fund_name"] for r in sp.with_ticker] == ["Acme Corporation Common Stock (ACME)"] and len(sp.ticker_less) == 5


def test_another_company_and_another_models_key_names_work_the_same_way(monkeypatch):
    sp = parse(monkeypatch, FIDELITY_STYLE, FIDELITY_ANSWER)
    by = {r["fund_name"]: r for r in sp.rows}
    assert len(sp.rows) == 5 and "Investment" not in by                                 # the echoed header row is gone
    assert by["Fidelity 500 Index Fund"]["ticker"] == "FXAIX" and by["Fidelity 500 Index Fund"]["instrument_hint"] == "MUTUAL_FUND"
    assert by["Fidelity 500 Index Fund"]["shares"] == 120.553 and by["Fidelity 500 Index Fund"]["current_value"] == 22953.26      # "$22,953.26" parsed
    assert by["Fidelity International Index Fund"]["ticker"] == "FSPSX" and by["Fidelity International Index Fund"]["instrument_hint"] == "MUTUAL_FUND"
    assert by["Managed Income Portfolio"]["ticker_less"] and by["Managed Income Portfolio"]["instrument_hint"] == "STABLE_VALUE"
    assert by["Fidelity Government Cash Reserves"]["ticker_less"] and by["Fidelity Government Cash Reserves"]["instrument_hint"] == "MONEY_MARKET"
    assert sp.meta.provider == "Fidelity" and sp.meta.plan_name == "Globex Corp 401(k) Plan" and sp.meta.period_end == "2026-03-31"
    assert {r["account_name"] for r in sp.rows} == {"Globex Corp 401(k) Plan"}           # no account name in the answer: the plan name from the header


def test_a_money_market_ticker_is_a_holding_on_this_path_not_cash(monkeypatch):
    sp = parse(monkeypatch, VANGUARD_403B_STYLE, [{"fund_name": "Federal Money Market Fund", "ticker": "VMFXX", "instrument_hint": "MONEY_MARKET", "shares": 100, "current_value": 100},
                                                  {"fund_name": "Total Stock Market Index Fund", "ticker": "VTSAX", "shares": 150, "current_value": 18150}])
    assert [r["ticker"] for r in sp.rows] == ["VMFXX", "VTSAX"] and all(not r["ticker_less"] for r in sp.rows)


def test_rows_are_dropped_only_when_there_is_nothing_to_hold():
    meta = ps.StatementMeta(plan_name="P", period_end="2026-03-31")
    n = lambda d: lp._normalise_statement_row(d, meta)
    assert n({"fund_name": "", "ticker": ""}) is None                                   # neither ticker nor name
    assert n({"fund_name": "Some CIT", "shares": None, "current_value": None}) is None  # a name but nothing held
    assert n({"fund_name": "Some CIT", "current_value": 10.0})["ticker_less"] is True    # a name and a value: kept
    assert n({"fund_name": "Some CIT", "shares": 5})["current_value"] is None            # shares only: value stays null, nothing computed
    assert n({"fund_name": "Total", "current_value": 100.0}) is None and n({"fund_name": "Ending Balance", "current_value": 1.0}) is None
    assert n({"ticker": "VTSAX", "shares": 1})["fund_name"] == "VTSAX"
    assert n({"fund_name": "X Trust", "ticker": "not a ticker!", "current_value": 5})["ticker"] == ""


def test_a_ticker_on_a_collective_trust_or_stable_value_fund_is_not_kept():
    meta = ps.StatementMeta()
    r = lp._normalise_statement_row({"fund_name": "Acme Index CIT", "ticker": "ACMEX", "instrument_hint": "CIT", "current_value": 5}, meta)
    assert r["ticker"] == "" and r["ticker_less"] and r["dropped_ticker"] == "ACMEX"


def test_the_hint_falls_back_to_the_fund_name_when_the_model_omits_or_garbles_it():
    meta = ps.StatementMeta()
    cases = {"Guaranteed Income Fund": "STABLE_VALUE", "LifePath Index 2045 Fund": "TARGET_DATE_FUND", "Government Cash Reserves": "MONEY_MARKET",
             "Acme Company Stock Fund": "STOCK", "Large Cap Collective Trust": "CIT", "Mystery Option": "OTHER", "Some ETF": "ETF"}
    for name, hint in cases.items():
        r = lp._normalise_statement_row({"fund_name": name, "instrument_hint": "whatever", "current_value": 1}, meta)
        assert (r["instrument_hint"], r["hint_source"]) == (hint, "name"), name
    r = lp._normalise_statement_row({"fund_name": "Index Fund", "ticker": "VTSAX", "current_value": 1}, meta)
    assert r["instrument_hint"] == "MUTUAL_FUND"                                         # a ticker and no other clue


# ── money sources and accounts ────────────────────────────────────────────────

@pytest.mark.parametrize("label,expected", [
    ("Pre-Tax", "PRE_TAX"), ("Salary deferral", "PRE_TAX"), ("Employee elective deferral", "PRE_TAX"), ("Traditional 401(k)", "PRE_TAX"), ("EE Contributions", "PRE_TAX"),
    ("Roth", "ROTH"), ("Roth deferral", "ROTH"), ("Roth 401(k) contributions", "ROTH"), ("Roth rollover", "ROTH"),
    ("Employer match", "EMPLOYER_MATCH"), ("Safe harbor match", "EMPLOYER_MATCH"), ("Profit sharing", "EMPLOYER_MATCH"), ("Employer non-elective", "EMPLOYER_MATCH"),
    ("Company contribution", "EMPLOYER_MATCH"), ("After-tax", "AFTER_TAX"), ("Voluntary after tax", "AFTER_TAX"), ("Rollover", "ROLLOVER"), ("Rollover (pre-tax)", "ROLLOVER"),
    ("Total", ""), ("All sources", ""), ("", ""), (None, ""), ("Something unrecognised", ""),
])
def test_money_source_labels_from_different_companies_map_to_one_vocabulary(label, expected):
    assert lp.normalize_contribution_source(label) == expected


def test_per_source_lines_become_one_row_per_fund_per_account_with_roth_in_its_own_account(monkeypatch):
    sp = parse(monkeypatch, PRINCIPAL_STYLE, PRINCIPAL_ANSWER)
    key = {(r["fund_name"], r["account_type"]): r for r in sp.rows}
    assert set(key) == {("Principal LifeTime 2045 Separate Account", "401K"), ("Principal LifeTime 2045 Separate Account", "ROTH_401K"),
                        ("Initech Stable Value Option", "401K")}
    main = key[("Principal LifeTime 2045 Separate Account", "401K")]
    roth = key[("Principal LifeTime 2045 Separate Account", "ROTH_401K")]
    assert (main["shares"], main["current_value"]) == (150.65, 4703.31)                    # pre-tax + match, summed
    assert (roth["shares"], roth["current_value"]) == (40.0, 1248.8)
    assert main["account_number"] == "****4455" and roth["account_number"] == "****4455-ROTH" and roth["account_name"].endswith("(Roth)")
    assert [s["source"] for s in main["sources"]] == ["PRE_TAX", "EMPLOYER_MATCH"] and main["sources"][1]["vested_value"] == 939.76
    assert main["vested_value"] == 4703.31                                                 # pre-tax is the employee's own money (vested in full) + the stated vested match
    assert [s["vested_inferred"] for s in main["sources"]] == [True, False]                  # the pre-tax line's vesting is a rule, the match line's is as printed
    unstated = [{**PRINCIPAL_ANSWER[0]}, {**PRINCIPAL_ANSWER[2], "vested_value": None}]      # employer money with NO stated vested amount
    assert lp.aggregate_statement_rows([lp._normalise_statement_row(r, ps.StatementMeta()) for r in unstated])[0]["vested_value"] is None
    assert main["current_price"] == 31.22


def test_dollars_only_lines_never_get_invented_shares_and_a_partial_total_is_left_blank(monkeypatch):
    sp = parse(monkeypatch, PRINCIPAL_STYLE, PRINCIPAL_ANSWER)
    sv = next(r for r in sp.rows if r["fund_name"] == "Initech Stable Value Option")
    assert sv["shares"] is None and sv["current_value"] == 3099.0 and sv["partial"] == []  # no shares anywhere: simply no shares
    mixed = [
        {"fund_name": "Mixed Fund", "shares": 10, "unit_price": 5, "current_value": 50, "contribution_source": "Pre-tax"},
        {"fund_name": "Mixed Fund", "shares": None, "unit_price": None, "current_value": 20, "contribution_source": "Employer match"}]
    sp2 = parse(monkeypatch, PRINCIPAL_STYLE, mixed)
    r = sp2.rows[0]
    assert r["shares"] is None and r["current_value"] == 70.0 and r["partial"] == ["shares"]       # shares NOT derived from the 20 dollars
    assert any("Mixed Fund: shares not stated on every line" in w for w in sp2.warnings)
    no_val = parse(monkeypatch, PRINCIPAL_STYLE, [{"fund_name": "F", "shares": 5, "contribution_source": "Pre-tax"},
                                                  {"fund_name": "F", "shares": 2, "current_value": 9, "contribution_source": "Roth"}])
    assert next(x for x in no_val.rows if x["account_type"] == "401K")["current_value"] is None


def test_a_403b_plan_keeps_its_roth_money_in_its_own_account_type(monkeypatch):
    rows = [{"fund_name": "Fund A", "ticker": "VTSAX", "shares": 10, "current_value": 100, "contribution_source": "Roth", "account_name": "Springfield School District 403(b) Plan"},
            {"fund_name": "Fund A", "ticker": "VTSAX", "shares": 5, "current_value": 50, "contribution_source": "Pre-tax", "account_name": "Springfield School District 403(b) Plan"}]
    sp = parse(monkeypatch, VANGUARD_403B_STYLE, rows)
    assert [(r["account_type"], r["shares"]) for r in sp.rows] == [("403B", 15.0)]


def test_a_457b_plan_name_sets_the_type_when_the_answer_gives_none(monkeypatch):
    sp = parse(monkeypatch, VANGUARD_403B_STYLE, [{"fund_name": "Fund", "ticker": "VTSAX", "shares": 1, "current_value": 1, "account_name": "County 457(b) Deferred Comp Plan"}])
    assert sp.rows[0]["account_type"] == "457B"


# ── cross-check ───────────────────────────────────────────────────────────────

def test_the_ending_balance_is_read_from_the_statement_and_compared_with_the_rows(monkeypatch):
    for pages, expected in ((EMPOWER_STYLE, 126563.95), (FIDELITY_STYLE, 52100.10), (PRINCIPAL_STYLE, 9051.07), (VANGUARD_403B_STYLE, 30880.0)):
        assert ps.extract_ending_balance(pages) == expected
    sp = parse(monkeypatch, VANGUARD_403B_STYLE, VANGUARD_ANSWER)
    assert sp.check == {"ending_balance": 30880.0, "sum_of_rows": 30880.0, "difference": 0.0, "ok": True, "missing_values": 0}
    assert not any("ending balance" in w for w in sp.warnings)


def test_a_difference_is_a_warning_with_the_numbers_and_never_an_error(monkeypatch):
    sp = parse(monkeypatch, EMPOWER_STYLE, EMPOWER_ANSWER)                 # the funds in the sample answer add up to the printed balance
    assert sp.check["ok"] is True and sp.check["sum_of_rows"] == 126563.95
    short = [r for r in EMPOWER_ANSWER if r["fund_name"] != "Acme International Equity CIT"]
    sp = parse(monkeypatch, EMPOWER_STYLE, short)                          # a missed fund
    assert sp.check["ok"] is False and sp.check["difference"] == 15772.80 and sp.check["ending_balance"] == 126563.95
    assert any("ending balance is 126,563.95" in w and "difference 15,772.80" in w for w in sp.warnings)
    assert len(sp.rows) == 5                                                # still returned: the import is never blocked by it


def test_no_stated_balance_means_no_cross_check_and_a_note():
    pages = ["Some Plan 401(k)\nStatement period 01/01/2026 - 03/31/2026", "Investment  Shares  Price  Balance\nFund A  1.0  $1.00  $1.00 $2.00 $3.00 $4.00"]
    assert ps.extract_ending_balance(pages) is None
    c = lp.check_against_ending_balance([{"current_value": 5.0}], None)
    assert c["ok"] is None and c["difference"] is None
    assert lp.check_against_ending_balance([{"current_value": None}], 100.0)["ok"] is None
    assert lp.check_against_ending_balance([{"current_value": 99.5}, {"current_value": None}], 100.0)["missing_values"] == 1


def test_units_times_price_that_do_not_match_the_balance_are_flagged(monkeypatch):
    sp = parse(monkeypatch, VANGUARD_403B_STYLE, [{"fund_name": "Odd Fund", "ticker": "VTSAX", "shares": 100, "unit_price": 10, "current_value": 2000}])
    assert any("Odd Fund: units × price does not equal the balance" in w for w in sp.warnings)


# ── failures and big statements ───────────────────────────────────────────────

def test_a_model_that_returns_nothing_or_junk_gives_a_clear_error_and_no_rows(monkeypatch):
    for answer in ("", "I could not find any holdings.", "[]", "{not json"):
        fake_llm(monkeypatch, answer)
        with pytest.raises(ValueError, match="No investments could be read"):
            lp.parse_statement_with_llm(pdf(EMPOWER_STYLE))
    fake_llm(monkeypatch, "")
    with pytest.raises(ValueError, match="The model gave no answer"):
        lp.parse_statement_with_llm(pdf(EMPOWER_STYLE))


def test_a_scanned_pdf_never_reaches_the_model(monkeypatch):
    prompts = fake_llm(monkeypatch, EMPOWER_ANSWER)
    with pytest.raises(ps.NoTextLayerError):
        lp.parse_statement_with_llm(pdf([None]))
    assert prompts == []


def test_a_long_statement_is_read_in_page_sized_chunks_and_a_failed_part_is_reported(monkeypatch):
    monkeypatch.setattr(lp, "STATEMENT_CHUNK_CHARS", 900)
    many = [EMPOWER_STYLE[0]] + [f"Balance by Investment\nInvestment Option  Shares  Unit Value  Ending Balance\n" + "\n".join(
        f"Fund {p}-{i}  {i + 1}.0  $1.00  ${i + 1}.00  ${i + 1}.00" for i in range(14)) for p in range(3)]
    part = lambda p: [{"fund_name": f"Fund {p}-{i}", "ticker": "", "shares": i + 1, "unit_price": 1, "current_value": i + 1} for i in range(14)]
    prompts = fake_llm(monkeypatch, part(0), "", part(2))
    sp = lp.parse_statement_with_llm(pdf(many))
    assert len(prompts) == 4 and "(part 1 of 4" in prompts[0] and "(part 4 of 4" in prompts[3]
    assert len(sp.rows) == 28 and any("part 2 of 4" in w and "could not be read" in w for w in sp.warnings)


def test_pages_left_out_for_length_are_reported(monkeypatch):
    monkeypatch.setattr(lp, "STATEMENT_MAX_CHARS", 600)
    fake_llm(monkeypatch, VANGUARD_ANSWER)
    sp = lp.parse_statement_with_llm(pdf(EMPOWER_STYLE))
    assert any("left out because the statement is very long" in w for w in sp.warnings) and sp.pages_read < sp.pages


# ── the upload path ───────────────────────────────────────────────────────────

def test_a_pdf_upload_returns_the_parse_with_ticker_less_funds_kept_apart(monkeypatch):
    fake_llm(monkeypatch, EMPOWER_ANSWER)
    parsed = parse_upload(pdf(EMPOWER_STYLE), "Q1-statement.pdf")
    assert parsed.statement is not None and parsed.filename == "Q1-statement.pdf" and parsed.broker == "other"
    assert [r["ticker"] for r in parsed.securities] == ["ACME"] and len(parsed.ticker_less) == 5 and parsed.cash_rows == []
    assert parsed.statement_date == "2026-03-31" and parsed.warnings == parsed.statement.warnings
    plan = build_plan(parsed)                                                           # since Phase 3 a statement becomes a real plan (see test_statement_import.py)
    assert plan.is_statement and len(plan.accounts) == 1 and len(plan.accounts[0].rows) == 6


def test_a_pdf_the_model_cannot_read_is_a_plain_value_error_for_the_page(monkeypatch):
    fake_llm(monkeypatch, "")
    with pytest.raises(ValueError, match="No investments could be read"):
        parse_upload(pdf(EMPOWER_STYLE), "s.pdf")
    with pytest.raises(ValueError, match="no text layer"):
        parse_upload(pdf([None]), "scan.pdf")


def test_the_brokerage_csv_and_excel_prompt_is_unchanged():
    p = lp._build_prompt("a,b\n1,2", "x.csv")
    assert "Skip cash, money-market, sweep" in p and "retirement" not in p.lower().split("source:")[0].replace("401(k)", "")  # the statement prompt did not leak into it
    assert '"fund_name"' not in p and '"contribution_source"' not in p
