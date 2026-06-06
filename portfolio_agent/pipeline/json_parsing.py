"""JSON extraction helpers for parsing structured data out of LLM responses."""

from __future__ import annotations

import json


def _extract_json_array(text: str) -> list[dict]:
    """Extract a JSON array from LLM response that may include prose or markdown fences."""
    import re as _re
    for pattern in [
        r'```json\s*(\[.*?\])\s*```',
        r'```\s*(\[.*?\])\s*```',
        r'(\[[\s\S]*\])',
    ]:
        m = _re.search(pattern, text, _re.DOTALL)
        if m:
            try:
                result = json.loads(m.group(1))
                if isinstance(result, list):
                    return result
            except Exception:
                continue
    return []


def _extract_json_block(text: str) -> dict | None:
    """Extract first JSON object from text that may contain narrative prose + JSON block."""
    import re as _re
    for pattern in [
        r'```json\s*(\{.*?\})\s*```',
        r'```\s*(\{.*?\})\s*```',
        r'(\{[\s\S]*\})',
    ]:
        m = _re.search(pattern, text, _re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                continue
    return None


def _extract_apex_json(text: str) -> dict | None:
    """Pull the structured JSON block out of an APEX response."""
    import re as _re
    for pattern in [
        r'```json\s*(\{.*?\})\s*```',
        r'```\s*(\{.*?\})\s*```',
        r'(\{[^{}]*"recommendation"[^{}]*\})',
    ]:
        m = _re.search(pattern, text, _re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                continue
    return None
