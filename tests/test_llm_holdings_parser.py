"""Tests for the generic LLM-based holdings extractor (litellm is mocked)."""
from __future__ import annotations

import io
import json
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from portfolio_agent.tools import llm_holdings_parser as m


# ── helpers ────────────────────────────────────────────────────────────────────

def _fake_response(text: str):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = text
    return resp


def _xlsx_bytes(sheets: dict[str, pd.DataFrame]) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for name, df in sheets.items():
            df.to_excel(xw, sheet_name=name, index=False)
    return buf.getvalue()


SCHWAB_CSV = b"""Positions for account Individual ...123 as of 09/20/2026
Symbol,Description,Qty,Price,Market Value,Cost Basis
AAPL,APPLE INC,10,"$200.00","$2,000.00","$1,500.00"
Cash & Cash Investments,--,--,--,"$500.00",--
Account Total,,,,"$2,500.00",
"""

LLM_ROWS = [
    {"ticker": "aapl", "description": "APPLE INC", "shares": 10, "avg_cost": None,
     "cost_basis_total": "$1,500.00", "current_price": 200.0, "current_value": 2000,
     "account_name": "Individual", "account_number": "12345678", "account_type": "taxable"},
    {"ticker": "SPAXX", "description": "money market", "shares": 500, "account_type": "TAXABLE"},
    {"ticker": "Account Total", "current_value": 2500},
    {"ticker": "VTI", "description": "Vanguard Total Stock", "shares": "5.5", "account_name": "Roth IRA",
     "account_type": "roth"},
]


# ── detection ──────────────────────────────────────────────────────────────────

def test_is_excel_by_extension_and_signature():
    assert m.is_excel(b"garbage", "Holdings.XLSX")
    assert m.is_excel(b"PK\x03\x04rest-of-zip")            # no filename
    assert not m.is_excel(SCHWAB_CSV, "positions.csv")
    assert not m.is_excel(SCHWAB_CSV)


# ── source → text ──────────────────────────────────────────────────────────────

def test_excel_all_sheets_flattened_and_labelled():
    content = _xlsx_bytes({
        "Summary": pd.DataFrame({"Account": ["Roth IRA"], "Value": [1000]}),
        "Holdings": pd.DataFrame({"Symbol": ["VTI"], "Shares": [5.5]}),
    })
    text = m._source_to_text(content, "export.xlsx")
    assert "--- Sheet: Summary ---" in text
    assert "--- Sheet: Holdings ---" in text
    assert "VTI" in text and "5.5" in text


def test_csv_decoded_via_existing_helper():
    text = m._source_to_text("Symbol,Qty\nAAPL,10\n".encode("utf-8-sig"), "x.csv")
    assert text.startswith("Symbol,Qty")   # BOM stripped by _decode


# ── JSON parsing ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    '[{"ticker": "AAPL"}]',
    '```json\n[{"ticker": "AAPL"}]\n```',
    'Here you go:\n[{"ticker": "AAPL"}]\nHope that helps.',
    '{"holdings": [{"ticker": "AAPL"}]}',
])
def test_parse_json_array_tolerates_wrapping(raw):
    assert m._parse_json_array(raw) == [{"ticker": "AAPL"}]


def test_parse_json_array_returns_empty_on_garbage():
    assert m._parse_json_array("not json at all") == []
    assert m._parse_json_array("") == []


# ── end-to-end with mocked litellm ─────────────────────────────────────────────

def test_parse_holdings_with_llm_normalises_rows():
    with patch("litellm.completion", return_value=_fake_response(json.dumps(LLM_ROWS))) as comp:
        rows = m.parse_holdings_with_llm(SCHWAB_CSV, "schwab.csv")

    assert comp.call_args.kwargs["temperature"] == 0.0
    assert "Source:" in comp.call_args.kwargs["messages"][0]["content"]

    # SPAXX (money market) and "Account Total" dropped by _is_invalid_ticker
    assert [r["ticker"] for r in rows] == ["AAPL", "VTI"]

    aapl = rows[0]
    assert set(aapl) == {
        "ticker", "description", "shares", "avg_cost", "cost_basis_total",
        "current_price", "current_value", "account_name", "account_number",
        "account_type", "sector",
    }
    assert aapl["shares"] == 10.0
    assert aapl["avg_cost"] is None
    assert aapl["cost_basis_total"] == 1500.0
    assert aapl["current_value"] == 2000.0
    assert aapl["account_number"] == "****5678"
    assert aapl["account_type"] == "TAXABLE"
    assert aapl["sector"] == ""

    vti = rows[1]
    assert vti["shares"] == 5.5
    assert vti["account_type"] == "ROTH_IRA"
    assert vti["account_number"] == ""


def test_failover_skips_failing_and_empty_models():
    responses = [Exception("429 rate limit"), _fake_response(""), _fake_response('[{"ticker":"MSFT"}]')]
    with patch("litellm.completion", side_effect=responses) as comp:
        rows = m.parse_holdings_with_llm(SCHWAB_CSV, "x.csv")
    assert comp.call_count == 3
    assert [r["ticker"] for r in rows] == ["MSFT"]


def test_returns_empty_when_every_model_fails():
    with patch("litellm.completion", side_effect=Exception("boom")):
        assert m.parse_holdings_with_llm(SCHWAB_CSV, "x.csv") == []


def test_returns_empty_on_unparseable_response():
    with patch("litellm.completion", return_value=_fake_response("I cannot help with that.")):
        assert m.parse_holdings_with_llm(SCHWAB_CSV, "x.csv") == []


def test_returns_empty_on_corrupt_excel():
    assert m.parse_holdings_with_llm(b"PK\x03\x04not-really-a-workbook", "bad.xlsx") == []


def test_prompt_is_truncated():
    huge = ("Symbol,Qty\n" + "AAPL,1\n" * 10_000).encode()
    with patch("litellm.completion", return_value=_fake_response("[]")) as comp:
        m.parse_holdings_with_llm(huge, "big.csv")
    prompt = comp.call_args.kwargs["messages"][0]["content"]
    assert len(prompt) < m.MAX_PROMPT_CHARS + 3_000
    assert "[truncated]" in prompt
