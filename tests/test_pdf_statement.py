"""Phase 1: PDF -> text safely. Text extraction (scans refused), page selection, redaction, statement metadata, detection, and the upload path."""
from __future__ import annotations

import pytest

from portfolio_agent.tools import pdf_statement as ps
from portfolio_agent.tools.holdings_import import parse_upload
from portfolio_agent.tools.llm_holdings_parser import is_excel, is_pdf
from tests.fixtures.statement_texts import EMPOWER_STYLE
from tests.helpers_pdf import make_pdf


# ── detection ─────────────────────────────────────────────────────────────────

def test_pdf_detection_by_extension_and_signature_and_never_a_spreadsheet():
    pdf = make_pdf(["hello"])
    assert is_pdf(pdf, "x.PDF") and is_pdf(pdf, "") and is_pdf(b"\n  %PDF-1.7 ...", "noext") and not is_pdf(b"a,b\n1,2\n", "x.csv")
    assert not is_excel(pdf, "statement.pdf") and not is_excel(pdf, "") and is_excel(b"PK\x03\x04", "")
    assert is_pdf(b"not really", "report.pdf")                                      # the extension alone says "PDF"; reading it will then fail clearly


# ── text extraction ───────────────────────────────────────────────────────────

def test_extract_pages_returns_each_pages_text_in_order():
    pages = ps.extract_pages(make_pdf(EMPOWER_STYLE))
    assert len(pages) == 6
    assert "Statement Period: January 1, 2026 - March 31, 2026" in pages[0] and "Balance by Investment" in pages[2] and "Acme Stable Value Fund" in pages[2]
    assert "Important Disclosures" in pages[5]


def test_a_scanned_pdf_is_refused_with_a_clear_message_and_nothing_is_guessed():
    scan = make_pdf([None, None])
    with pytest.raises(ps.NoTextLayerError, match="no text layer"):
        ps.extract_pages(scan)
    assert issubclass(ps.NoTextLayerError, ValueError)
    with pytest.raises(ValueError, match="no text layer"):
        parse_upload(scan, "scan.pdf")


def test_unreadable_pdfs_raise_one_friendly_error_type():
    for bad in (b"", b"%PDF-1.4\nthis is not a real pdf body\n%%EOF", b"just text"):
        with pytest.raises(ps.PdfStatementError):
            ps.extract_pages(bad)
    with pytest.raises(ps.PdfStatementError, match="larger than"):
        ps.extract_pages(b"%PDF-" + b"0" * (ps.MAX_PDF_BYTES + 1))
    with pytest.raises(ps.PdfStatementError, match="pages; the limit"):
        ps.extract_pages(make_pdf([f"page {i} with enough text to count" for i in range(ps.MAX_PAGES + 1)]))


def test_a_mixed_pdf_with_some_blank_pages_still_reads():
    pages = ps.extract_pages(make_pdf([EMPOWER_STYLE[0], None, EMPOWER_STYLE[2]]))
    assert len(pages) == 3 and pages[1] == "" and "Balance by Investment" in pages[2]


# ── page selection ────────────────────────────────────────────────────────────

def test_holdings_pages_are_the_cover_the_table_and_its_continuation_only():
    assert ps.select_holdings_pages(EMPOWER_STYLE) == [0, 2, 3]                      # not the summary-by-source, the activity or the disclosures
    got = ps.find_holdings_pages(EMPOWER_STYLE)
    assert len(got) == 3 and "Statement Period" in got[0] and "Acme Stable Value Fund" in got[1] and "Acme Cash Reserve" in got[2]
    assert not any("Transaction Activity" in p or "Important Disclosures" in p for p in got)


def test_page_one_is_always_kept_and_a_text_without_holdings_selects_only_it():
    assert ps.select_holdings_pages(["Welcome to your statement", "Contact us at our website", "Glossary of terms"]) == [0]
    assert ps.select_holdings_pages([]) == []


def test_a_disclosure_page_with_the_keywords_is_not_mistaken_for_holdings():
    prose = ("Important disclosures. This statement is for information. Past performance is not a guarantee. Funds, shares, units and the price "
             "of units can fall. See the glossary. Ending balance definitions are in the privacy policy and terms and conditions.")
    assert ps.select_holdings_pages(["cover", prose]) == [0]


def test_fitting_to_a_budget_drops_whole_pages_never_cuts_one_and_keeps_page_one():
    idx = ps.select_holdings_pages(EMPOWER_STYLE)
    total = sum(len(EMPOWER_STYLE[i]) for i in idx)
    assert ps.fit_pages(EMPOWER_STYLE, idx, total) == idx
    fitted = ps.fit_pages(EMPOWER_STYLE, idx, total - 1)
    assert 0 in fitted and 2 in fitted and fitted == [0, 2]                           # the thinnest page (the continuation) goes first
    assert ps.fit_pages(EMPOWER_STYLE, idx, 1) == [0]
    out = ps.find_holdings_pages(EMPOWER_STYLE, max_chars=total - 1)
    assert out == [EMPOWER_STYLE[0], EMPOWER_STYLE[2]]                                # whole pages, verbatim


# ── redaction ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,gone", [
    ("SSN 123-45-6789 on file", "123-45-6789"), ("SSN: ***-**-6789", "6789"), ("tax id xxx-xx-4321 ok", "4321"), ("123 45 6789", "6789"),
    ("Social Security Number: 987654321", "987654321"),
])
def test_ssns_are_removed_in_every_form(raw, gone):
    out = ps.redact(raw)
    assert gone not in out and "[SSN]" in out


def test_account_numbers_keep_only_the_last_four_and_amounts_are_left_alone():
    out = ps.redact("Account Number: 123456789012 and acct no. 8841-2207-5531, contract # AB-12345678\nplain 98765432101\nmasked ****1234")
    assert "123456789012" not in out and "****9012" in out and "****5531" in out and "****5678" in out and "****2101" in out and "****1234" in out
    assert "8841" not in out and "AB-12" not in out
    keep = "Ending balance $126,563.95; 3,120.4410 units at $14.2310 = 44,405.62; shares 190.2210; 20260331; 2026-03-31; 401(k)"
    assert ps.redact(keep) == keep


def test_street_addresses_phones_emails_and_dates_of_birth_are_removed():
    raw = ("1428 Maple Grove Lane, Apt 4B\nSpringfield, IL 62704\nPO Box 4417\n350 N. Main Street\nCall (217) 555-0142 or 800-555-0199 or +1 312.555.0100\n"
           "jordan.sample@example.com\nDate of Birth: 07/14/1984   DOB: March 3, 1990\nBorn 1984-07-14")
    out = ps.redact(raw)
    for gone in ("Maple", "62704", "4417", "Main Street", "555-0142", "555-0199", "555.0100", "example.com", "1984", "1990"):
        assert gone not in out, gone
    assert out.count("[ADDRESS]") >= 4 and out.count("[PHONE]") == 3 and "[EMAIL]" in out and out.count("[DOB]") == 3
    assert "Date of Birth: [DOB]" in out


def test_the_participants_name_after_a_label_goes_but_plan_and_fund_names_stay():
    out = ps.redact("Prepared for\nJordan Q Sample\nPlan Name: Acme Corporation 401(k) Retirement Savings Plan\nAcme S&P 500 Index CIT")
    assert "Jordan" not in out and "[NAME]" in out
    assert "Acme Corporation 401(k) Retirement Savings Plan" in out and "Acme S&P 500 Index CIT" in out
    assert ps.redact("Name: Plan Acme") == "Name: Plan Acme"                           # a "Plan ..." value after a Name label is not a person


def test_the_whole_sample_statement_is_clean_after_redaction_and_still_extractable():
    red = "\n".join(ps.redact(p) for p in EMPOWER_STYLE)
    for pii in ("Jordan", "Maple Grove", "62704", "555-0142", "jordan.sample", "6789", "07/14/1984", "123456789012", "8841-2207-5531"):
        assert pii not in red, pii
    for kept in ("Statement Period: January 1, 2026 - March 31, 2026", "Acme Stable Value Fund", "3,120.4410", "$44,405.62", "Acme S&P 500 Index CIT",
                 "Acme Corporation Common Stock (ACME)", "Ending balance $126,563.95", "Pre-tax"):
        assert kept in red, kept
    assert ps.redact("") == "" and ps.redact(None) == ""
    assert ps.redact(ps.redact(red)) == ps.redact(red)                                  # idempotent


# ── statement metadata ────────────────────────────────────────────────────────

def test_statement_meta_from_the_first_page():
    m = ps.extract_statement_meta(EMPOWER_STYLE)
    assert (m.period_start, m.period_end, m.provider) == ("2026-01-01", "2026-03-31", "Empower")
    assert m.plan_name == "Acme Corporation 401(k) Retirement Savings Plan" and m.notes == []


def test_statement_meta_variants_and_missing_values_stay_none():
    assert ps.extract_statement_meta(["Fidelity NetBenefits\nFor the period 1/1/26 to 3/31/26\nACME PROFIT SHARING PLAN"]).as_dict() == \
        {"period_start": "2026-01-01", "period_end": "2026-03-31", "plan_name": "ACME PROFIT SHARING PLAN", "provider": "Fidelity"}
    q = ps.extract_statement_meta(["Quarter ending March 31, 2026\nVoya 403(b) Plan"])
    assert (q.period_start, q.period_end, q.provider) == (None, "2026-03-31", "Voya")                  # an end alone: the start is never guessed
    none = ps.extract_statement_meta(["Some statement with nothing recognisable"])
    assert none.as_dict() == {"period_start": None, "period_end": None, "plan_name": None, "provider": None} and len(none.notes) == 3
    assert ps.extract_statement_meta([]).notes == ["no text on the first pages"]
    assert ps.extract_statement_meta(["Period 13/45/2026 - 99/99/2026"]).period_end is None


# ── the upload path ───────────────────────────────────────────────────────────

def test_csv_and_excel_uploads_are_untouched_by_the_pdf_branch():
    csv = b"Account Number,Account Name,Symbol,Description,Quantity,Last Price,Current Value,Cost Basis Total,Average Cost Basis\nX1,Ind,AAPL,APPLE,1,$1.00,$1.00,$1.00,$1.00\n\n"
    assert parse_upload(csv, "p.csv").securities[0]["ticker"] == "AAPL"


def test_the_uploader_accepts_pdf_and_requirements_list_pdfplumber():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    assert '"pdf"' in (root / "web" / "components" / "holdings_import.py").read_text()
    assert "pdfplumber" in (root / "requirements.txt").read_text()
