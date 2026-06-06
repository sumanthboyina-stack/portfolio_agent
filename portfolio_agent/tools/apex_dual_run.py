"""
APEX dual-run prediction module.

Architecture:
  1. Claude-Sonnet + GPT-4o run in parallel per ticker (90s timeout).
  2. Both outputs merged: horizons matched by horizon_days, distribution
     buckets weighted-averaged, direction/conviction via tie-break.
  3. On both-fail → OR high-reasoning chain tried sequentially.
  4. On all-fail → return None; caller must skip, never write a placeholder.

Ensemble merging is always at the horizon level — distribution buckets and
numeric fields are averaged, directions use conviction-score tie-break,
reasoning text is concatenated with per-model attribution.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Any

import litellm

from portfolio_agent.log import get_logger as _get_logger
from portfolio_agent._models import (
    _SONNET, _GPT_4O,
    _OR_CLAUDE_OPUS48, _OR_QWEN37_MAX, _OR_GROK43, _OR_MISTRAL_MED35,
    _is_failover_error, GEMINI_COUNTER,
)

_log = _get_logger("apex")

# ── Model chains ───────────────────────────────────────────────────────────────

DUAL_RUN_PRIMARY: list[tuple[str, str, str]] = [
    (_SONNET, "anthropic", "Claude-Sonnet"),
    (_GPT_4O, "openai",    "GPT-4o"),
]

DUAL_RUN_FALLBACK: list[tuple[str, str, str]] = [
    (_OR_CLAUDE_OPUS48, "openrouter", "OR-Claude-Opus-4.8"),
    (_OR_QWEN37_MAX,    "openrouter", "OR-Qwen3.7-Max"),
    (_OR_GROK43,        "openrouter", "OR-Grok-4.3"),
    (_OR_MISTRAL_MED35, "openrouter", "OR-Mistral-Med-3.5"),
]

DUAL_RUN_TIMEOUT: float = 90.0   # seconds before the parallel pair is abandoned
DEFAULT_WEIGHTS:  dict[str, float] = {"Claude-Sonnet": 0.5, "GPT-4o": 0.5}

# Canonical distribution bucket names (nested inside each horizon)
_BUCKETS = ["strong_down", "moderate_down", "flat", "moderate_up", "strong_up"]
# Horizon-level numeric fields to weighted-average
_H_NUM   = ["predicted_return_low", "predicted_return_high", "conviction_score"]


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class SinglePrediction:
    """One model's parsed prediction before merging."""
    model_id:     str
    provider:     str
    display_name: str
    data:         dict[str, Any]   # full parsed JSON (ticker + horizons + scores)
    latency_ms:   int
    succeeded:    bool
    error:        str = ""


@dataclass
class EnsembleResult:
    """Merged prediction + per-model breakdown for audit/per-model validation."""
    merged:          dict[str, Any]
    individuals:     list[SinglePrediction]
    model_sources:   list[str]
    agreement_score: float     # 0–1; 1 = identical horizon distributions
    is_single_model: bool      # True when only one of the two primaries succeeded
    used_fallback:   bool      # True when OR chain was needed
    fallback_model:  str = ""


# ── Public entry point ─────────────────────────────────────────────────────────

async def run_apex_dual(ticker: str, prompt: str) -> EnsembleResult | None:
    """
    Run Claude-Sonnet + GPT-4o in parallel for *ticker*, then merge.

    Falls back to OR high-reasoning chain if both primaries fail.
    Returns None if ALL models fail — caller skips, no placeholder written.
    """
    # ── Step 1: parallel primary pair ─────────────────────────────────────────
    results = await _run_parallel(prompt, ticker)
    ok      = [r for r in results if r.succeeded]
    fail    = [r for r in results if not r.succeeded]

    if len(ok) == 2:
        merged = _merge(ok[0], ok[1])
        agr    = _agreement(ok[0], ok[1])
        _log.info(
            f"  ✓ [apex/dual] {ticker} — both models succeeded "
            f"(agreement={agr:.2f}, direction={_first_direction(merged)})",
            event_type="dual_run_complete", ticker=ticker,
            agreement_score=agr, model_sources=[r.display_name for r in ok],
        )
        return EnsembleResult(
            merged=merged, individuals=results,
            model_sources=[r.display_name for r in ok],
            agreement_score=agr, is_single_model=False, used_fallback=False,
        )

    if len(ok) == 1:
        _log.warning(
            f"  [apex/dual] {ticker} — {fail[0].display_name} failed "
            f"({fail[0].error[:80]}); using {ok[0].display_name} alone",
            event_type="dual_run_partial", ticker=ticker,
            succeeded=ok[0].display_name, failed=fail[0].display_name,
        )
        return EnsembleResult(
            merged=ok[0].data, individuals=results,
            model_sources=[ok[0].display_name],
            agreement_score=1.0, is_single_model=True, used_fallback=False,
        )

    # ── Step 2: both failed — sequential OR fallback chain ────────────────────
    _log.warning(
        f"  [apex/dual] {ticker} — both primaries failed, trying OR chain "
        f"({len(DUAL_RUN_FALLBACK)} models)",
        event_type="dual_run_fallback", ticker=ticker,
        sonnet_error=results[0].error[:120],
        gpt4o_error=results[1].error[:120],
    )
    for mid, prov, label in DUAL_RUN_FALLBACK:
        fb = await _call_model(mid, prov, label, prompt, ticker)
        if fb.succeeded:
            _log.info(
                f"  [apex/dual] {ticker} — OR fallback succeeded: {label}",
                event_type="fallback_success", ticker=ticker, model=label,
            )
            return EnsembleResult(
                merged=fb.data, individuals=results + [fb],
                model_sources=[label],
                agreement_score=1.0, is_single_model=True, used_fallback=True,
                fallback_model=label,
            )

    _log.error(
        f"  [apex/dual] {ticker} — ALL models failed — skipping",
        event_type="dual_run_exhausted", ticker=ticker,
    )
    return None


# ── Internal helpers ───────────────────────────────────────────────────────────

async def _run_parallel(prompt: str, ticker: str) -> list[SinglePrediction]:
    """Launch both primary models concurrently with a shared timeout."""
    tasks = [
        _call_model(mid, prov, label, prompt, ticker)
        for mid, prov, label in DUAL_RUN_PRIMARY
    ]
    try:
        return list(await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=False),
            timeout=DUAL_RUN_TIMEOUT,
        ))
    except asyncio.TimeoutError:
        _log.error(
            f"  [apex/dual] {ticker} — parallel pair timed out after {DUAL_RUN_TIMEOUT}s",
            event_type="dual_run_timeout", ticker=ticker,
        )
        return [
            SinglePrediction(mid, prov, label, {}, 0, False, "timeout")
            for mid, prov, label in DUAL_RUN_PRIMARY
        ]


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
            max_tokens=2000,
        )
        latency = int((time.time() - t0) * 1000)
        raw = resp.choices[0].message.content or ""
        if "gemini" in model_id:
            GEMINI_COUNTER.record(model_id)
        parsed = _parse_apex_json(raw)
        if parsed is None:
            _log.warning(
                f"  ⚠ [apex/dual] {ticker} — {display_name}: no valid JSON in response",
                event_type="llm_call_error", model=display_name, ticker=ticker,
            )
            return SinglePrediction(model_id, provider, display_name,
                                    {}, latency, False, "no_valid_json")
        _log.info(
            f"  ⟳ [apex/dual] {ticker} — {display_name} ✓ ({latency}ms)",
            event_type="llm_call_success", model=display_name,
            provider=provider, ticker=ticker, latency_ms=latency,
        )
        return SinglePrediction(model_id, provider, display_name,
                                parsed, latency, True)

    except Exception as exc:
        latency = int((time.time() - t0) * 1000)
        if _is_failover_error(exc):
            _log.warning(
                f"  ⚠ [apex/dual] {ticker} — {display_name} failed "
                f"({type(exc).__name__}: {str(exc)[:200]})",
                event_type="llm_call_error", model=display_name, provider=provider,
                error_type=type(exc).__name__, error=str(exc)[:200],
            )
            return SinglePrediction(model_id, provider, display_name,
                                    {}, latency, False,
                                    f"{type(exc).__name__}: {str(exc)[:200]}")
        raise   # non-failover: propagate so the caller sees it


def _parse_apex_json(text: str) -> dict | None:
    """Extract the APEX JSON object from a model response, handling markdown fences."""
    if not text:
        return None
    text = re.sub(r"```(?:json)?\s*|\s*```", "", text.strip())
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or "recommendation" not in obj:
        return None
    horizons = obj.get("horizons")
    if horizons is not None and (not isinstance(horizons, list) or len(horizons) == 0):
        return None
    return obj


def _merge(
    a: SinglePrediction,
    b: SinglePrediction,
    weights: dict[str, float] | None = None,
) -> dict[str, Any]:
    """
    Merge two per-ticker APEX predictions.

    Top-level scores are weighted-averaged. Recommendation and direction use
    composite-score tie-break. Each horizon is merged independently:
    distribution buckets and numeric fields are weighted-averaged;
    direction uses conviction-score tie-break; reasoning is concatenated.
    """
    w  = weights or DEFAULT_WEIGHTS
    wa = w.get(a.display_name, 0.5)
    wb = w.get(b.display_name, 0.5)
    total = wa + wb
    wa, wb = wa / total, wb / total

    pa, pb = a.data, b.data
    out: dict[str, Any] = dict(pa)

    # Top-level numeric scores
    for k in ("confidence", "fundamental_score", "research_score",
              "macro_score", "news_score", "composite_score"):
        va, vb = pa.get(k), pb.get(k)
        if va is not None and vb is not None:
            out[k] = round(float(va) * wa + float(vb) * wb, 2)
        elif vb is not None:
            out[k] = vb

    # Recommendation / prediction: consensus or higher composite wins
    comp_a = float(pa.get("composite_score") or 0)
    comp_b = float(pb.get("composite_score") or 0)
    rec_a, rec_b = pa.get("recommendation", "HOLD"), pb.get("recommendation", "HOLD")
    out["recommendation"] = rec_a if rec_a == rec_b or comp_a >= comp_b else rec_b
    prd_a, prd_b = pa.get("prediction", "NEUTRAL"), pb.get("prediction", "NEUTRAL")
    out["prediction"]     = prd_a if prd_a == prd_b or comp_a >= comp_b else prd_b

    # Merge nested horizons
    hors_a = {h["horizon_days"]: h
              for h in (pa.get("horizons") or [])
              if isinstance(h, dict) and h.get("horizon_days")}
    hors_b = {h["horizon_days"]: h
              for h in (pb.get("horizons") or [])
              if isinstance(h, dict) and h.get("horizon_days")}

    merged_h: list[dict] = []
    for hd in sorted(set(hors_a) | set(hors_b)):
        ha, hb = hors_a.get(hd), hors_b.get(hd)
        if ha is None:
            merged_h.append(hb)
            continue
        if hb is None:
            merged_h.append(ha)
            continue

        mh: dict[str, Any] = {"horizon_days": hd}

        # Numeric fields
        for k in _H_NUM:
            va, vb = ha.get(k), hb.get(k)
            if va is not None and vb is not None:
                mh[k] = round(float(va) * wa + float(vb) * wb, 2)
            elif va is not None:
                mh[k] = va
            elif vb is not None:
                mh[k] = vb

        # Direction: conviction tie-break
        da  = ha.get("predicted_direction")
        db  = hb.get("predicted_direction")
        ca  = float(ha.get("conviction_score") or 0)
        cb  = float(hb.get("conviction_score") or 0)
        mh["predicted_direction"] = da if da == db or ca >= cb else db

        # Distribution buckets
        d_a = ha.get("distribution") or {}
        d_b = hb.get("distribution") or {}
        if d_a or d_b:
            raw = {k: float(d_a.get(k, 0)) * wa + float(d_b.get(k, 0)) * wb
                   for k in _BUCKETS}
            s = sum(raw.values())
            if s > 0 and abs(s - 100) > 0.5:
                raw = {k: v * 100 / s for k, v in raw.items()}
            mh["distribution"] = {k: round(v, 1) for k, v in raw.items()}

        # Reasoning: concatenate with attribution
        r_a = (ha.get("reasoning_text") or "").strip()
        r_b = (hb.get("reasoning_text") or "").strip()
        if r_a and r_b:
            mh["reasoning_text"] = (
                f"[{a.display_name}] {r_a}  [{b.display_name}] {r_b}"
            )
        elif r_a or r_b:
            mh["reasoning_text"] = r_a or r_b

        merged_h.append(mh)

    out["horizons"]  = merged_h
    out["_ensemble"] = f"{a.display_name}+{b.display_name}"
    return out


def _agreement(a: SinglePrediction, b: SinglePrediction) -> float:
    """
    Average L1 distribution similarity across all shared horizons.
    Returns 1.0 when no distributions are available for comparison.
    1 = identical distributions, 0 = maximally different.
    """
    hors_a = {h["horizon_days"]: h
              for h in (a.data.get("horizons") or [])
              if isinstance(h, dict) and h.get("horizon_days")}
    hors_b = {h["horizon_days"]: h
              for h in (b.data.get("horizons") or [])
              if isinstance(h, dict) and h.get("horizon_days")}
    common = set(hors_a) & set(hors_b)
    if not common:
        return 1.0
    scores = []
    for hd in common:
        d_a = hors_a[hd].get("distribution") or {}
        d_b = hors_b[hd].get("distribution") or {}
        l1  = sum(abs(float(d_a.get(k, 0)) - float(d_b.get(k, 0))) for k in _BUCKETS)
        scores.append(1.0 - min(l1 / 200.0, 1.0))
    return round(sum(scores) / len(scores), 3)


def _first_direction(merged: dict) -> str:
    """Extract predicted_direction from the first horizon for log messages."""
    horizons = merged.get("horizons") or []
    if horizons and isinstance(horizons[0], dict):
        return horizons[0].get("predicted_direction", "?")
    return merged.get("predicted_direction", "?")
