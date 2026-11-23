"""The chat turns a model's prediction JSON into a card; a raw-JSON answer must be parsed, never printed."""
from __future__ import annotations

from web.chat.orchestration import _extract_json

_NESTED = ('{ "ticker": "XYZ", "recommendation": "HOLD", "confidence": 5, "weights_used": { "macro": 0.41, "news": 0.1 }, '
           '"panel_summary": { "key_debate": "split" }, "changed_from_previous": false, "reasoning": "text with } and { braces" }')


def test_bare_json_with_nested_objects_is_extracted_after_leading_prose():
    out = _extract_json("Note: examples are for illustration.\n\n" + _NESTED + "\n\ntrailing prose")
    assert out["recommendation"] == "HOLD" and out["weights_used"]["macro"] == 0.41 and out["reasoning"].startswith("text with")


def test_fenced_and_flat_json_still_parse_as_before():
    assert _extract_json("```json\n" + _NESTED + "\n```")["recommendation"] == "HOLD"
    assert _extract_json('{"recommendation": "BUY", "confidence": 7}') == {"recommendation": "BUY", "confidence": 7}


def test_text_without_a_prediction_gives_nothing():
    assert _extract_json("Just prose, no JSON at all.") == {}
    assert _extract_json('{"ticker": "XYZ", "note": "no recommendation key here"}') == {}
    assert _extract_json('{ "recommendation": "HOLD", broken') == {}
