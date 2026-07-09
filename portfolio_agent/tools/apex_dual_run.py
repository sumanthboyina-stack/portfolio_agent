"""
APEX single-run prediction module.

Architecture:
  1. Randomly select Claude-Sonnet or GPT-4o for the prediction.
  2. If the selected model times out (90 s) or errors, retry with the other model.
  3. On both-fail → return None; caller skips, never writes a placeholder.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
from dataclasses import dataclass
from typing import Any

import litellm

from portfolio_agent.log import get_logger as _get_logger
from portfolio_agent._models import (
    _SONNET, _GPT_4O,
    _is_failover_error,
)

_log = _get_logger("apex")

# ── Model list ─────────────────────────────────────────────────────────────────

APEX_MODELS: list[tuple[str, str, str]] = [
    (_SONNET, "anthropic", "Claude-Sonnet"),
    (_GPT_4O, "openai",    "GPT-4o"),
]

APEX_TIMEOUT: float = 90.0   # seconds before abandoning and retrying with the other model


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class SinglePrediction:
    """One model's parsed prediction."""
    model_id:     str
    provider:     str
    display_name: str
    data:         dict[str, Any]
    latency_ms:   int
    succeeded:    bool
    error:        str = ""


@dataclass
class EnsembleResult:
    """Prediction result — always single-model."""
    merged:          dict[str, Any]
    individuals:     list[SinglePrediction]
    model_sources:   list[str]
    agreement_score: float      # always 1.0 (single model)
    is_single_model: bool       # always True
    used_fallback:   bool       # True when the first-choice model failed/timed out
    fallback_model:  str = ""


# ── Public entry point ─────────────────────────────────────────────────────────

async def run_apex_dual(ticker: str, prompt: str) -> EnsembleResult | None:
    """
    Run a single randomly-selected model (Sonnet or GPT-4o) for *ticker*.

    If the chosen model times out or fails, retries with the other model.
    Returns None if both models fail — caller skips, no placeholder written.
    """
    models = list(APEX_MODELS)
    random.shuffle(models)
    primary_mid, primary_prov, primary_label = models[0]
    fallback_mid, fallback_prov, fallback_label = models[1]

    _log.info(
        f"  [apex] {ticker} — selected {primary_label} (random)",
        event_type="apex_model_select", ticker=ticker, model=primary_label,
    )

    # ── Step 1: try the randomly selected model ────────────────────────────────
    result = await _call_model_with_timeout(
        primary_mid, primary_prov, primary_label, prompt, ticker
    )

    if result.succeeded:
        _log.info(
            f"  ✓ [apex] {ticker} — {result.display_name} ✓ ({result.latency_ms}ms)",
            event_type="apex_complete", ticker=ticker, model=result.display_name,
        )
        return EnsembleResult(
            merged=result.data, individuals=[result],
            model_sources=[result.display_name],
            agreement_score=1.0, is_single_model=True, used_fallback=False,
        )

    # ── Step 2: retry with the other model ────────────────────────────────────
    reason = "timed out" if result.error == "timeout" else f"failed ({result.error[:80]})"
    _log.warning(
        f"  [apex] {ticker} — {primary_label} {reason}, retrying with {fallback_label}",
        event_type="apex_retry", ticker=ticker,
        failed_model=primary_label, retry_model=fallback_label, reason=result.error,
    )

    fb = await _call_model_with_timeout(
        fallback_mid, fallback_prov, fallback_label, prompt, ticker
    )

    if fb.succeeded:
        _log.info(
            f"  ✓ [apex] {ticker} — {fb.display_name} ✓ ({fb.latency_ms}ms, fallback)",
            event_type="apex_complete", ticker=ticker,
            model=fb.display_name, used_fallback=True,
        )
        return EnsembleResult(
            merged=fb.data, individuals=[result, fb],
            model_sources=[fb.display_name],
            agreement_score=1.0, is_single_model=True, used_fallback=True,
            fallback_model=fb.display_name,
        )

    _log.error(
        f"  [apex] {ticker} — both models failed — skipping",
        event_type="apex_exhausted", ticker=ticker,
        primary_error=result.error[:120], fallback_error=fb.error[:120],
    )
    return None


# ── Internal helpers ───────────────────────────────────────────────────────────

async def _call_model_with_timeout(
    model_id: str, provider: str, display_name: str,
    prompt: str, ticker: str,
) -> SinglePrediction:
    """Wrap _call_model with a hard timeout; returns a failed result on expiry."""
    try:
        return await asyncio.wait_for(
            _call_model(model_id, provider, display_name, prompt, ticker),
            timeout=APEX_TIMEOUT,
        )
    except asyncio.TimeoutError:
        _log.warning(
            f"  [apex] {ticker} — {display_name} timed out after {APEX_TIMEOUT}s",
            event_type="apex_timeout", ticker=ticker, model=display_name,
        )
        return SinglePrediction(model_id, provider, display_name, {}, 0, False, "timeout")


async def _call_model(
    model_id: str, provider: str, display_name: str,
    prompt: str, ticker: str,
) -> SinglePrediction:
    """Single LiteLLM call with structured error handling."""
    t0 = time.time()
    try:
        resp = await litellm.acompletion(
            model=model_id,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=4000,
            response_format={"type": "json_object"},
        )
        latency = int((time.time() - t0) * 1000)
        raw     = resp.choices[0].message.content or ""
        parsed  = _parse_apex_json(raw)
        if parsed is None:
            _log.warning(
                f"  ⚠ [apex] {ticker} — {display_name}: no valid JSON in response",
                event_type="llm_call_error", model=display_name, ticker=ticker,
            )
            return SinglePrediction(model_id, provider, display_name,
                                    {}, latency, False, "no_valid_json")
        _log.info(
            f"  ⟳ [apex] {ticker} — {display_name} ✓ ({latency}ms)",
            event_type="llm_call_success", model=display_name,
            provider=provider, ticker=ticker, latency_ms=latency,
        )
        return SinglePrediction(model_id, provider, display_name, parsed, latency, True)

    except Exception as exc:
        latency = int((time.time() - t0) * 1000)
        if _is_failover_error(exc):
            _log.warning(
                f"  ⚠ [apex] {ticker} — {display_name} failed "
                f"({type(exc).__name__}: {str(exc)[:200]})",
                event_type="llm_call_error", model=display_name, provider=provider,
                error_type=type(exc).__name__, error=str(exc)[:200],
            )
            return SinglePrediction(model_id, provider, display_name,
                                    {}, latency, False,
                                    f"{type(exc).__name__}: {str(exc)[:200]}")
        raise


def _parse_apex_json(text: str) -> dict | None:
    """Extract the APEX JSON object from a model response, handling markdown fences.

    Scans all JSON objects in the text and returns the last valid one with a
    'recommendation' key — handles models that emit reasoning blobs before the
    final prediction object.
    """
    if not text:
        return None
    text = re.sub(r"```(?:json)?\s*|\s*```", "", text.strip())
    decoder    = json.JSONDecoder()
    last_valid: dict | None = None
    i = 0
    while i < len(text):
        idx = text.find("{", i)
        if idx == -1:
            break
        try:
            obj, end = decoder.raw_decode(text, idx)
            if isinstance(obj, dict) and "recommendation" in obj:
                horizons = obj.get("horizons")
                if horizons is None or (isinstance(horizons, list) and len(horizons) > 0):
                    last_valid = obj
            i = end
        except json.JSONDecodeError:
            i = idx + 1
    if last_valid is None:
        _log.warning(
            f"  [apex/json] parse failed — snippet: {text[:300]!r}",
            event_type="llm_call_error",
        )
    return last_valid
