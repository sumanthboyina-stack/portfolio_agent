"""
APEX single-run prediction module.

Architecture:
  1. Deterministic, config-driven model selection: one CHAMPION model plus an
     ordered FALLBACK list, read from engine_settings (apex_champion_model,
     apex_fallback_models) with the code defaults below — no env var needed and
     no randomness, so the same settings always pick the same model and
     validation compares like with like over time.
  2. The champion runs first. Only a timeout (90 s), a failover-class provider
     error (_models._is_failover_error) or an unparseable response moves on to
     the next model in the fallback list, in order. Any other exception
     propagates, exactly as before.
  3. Every model fails → return None; caller skips, never writes a placeholder.
  4. Optional challenger (off by default): for apex_challenger_sample_pct % of
     tickers — chosen deterministically per (ticker, date), see
     challenger_selected — the pipeline ALSO runs apex_challenger_model on the
     same prompt (run_challenger, single attempt, no fallback) and stores it with
     prediction_role='challenger'. Challenger rows are evaluated like any other
     row but never shown to users or fed back into prompts.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any

import litellm

from portfolio_agent.log import get_logger as _get_logger
from portfolio_agent._models import (
    _SONNET, _GPT_4O, _GPT_5, _O3,
    _is_failover_error,
)
from portfolio_agent.tools.prediction_db import ROLE_CHALLENGER, ROLE_CHAMPION, ROLE_FALLBACK

_log = _get_logger("apex")

# ── Model registry ─────────────────────────────────────────────────────────────
# Every model APEX may be configured to use, keyed by display label (the label is
# what engine_settings stores and what predictions.model_name records).
# litellm.drop_params is set True at import time in portfolio_agent._models, so
# o3's temperature != 1 restriction is silently dropped rather than raising.

APEX_MODELS: list[tuple[str, str, str]] = [
    (_SONNET, "anthropic", "Claude-Sonnet"),
    (_GPT_4O, "openai",    "GPT-4o"),
    (_GPT_5,  "openai",    "GPT-5"),
    (_O3,     "openai",    "o3"),
]
_MODELS_BY_LABEL: dict[str, tuple[str, str, str]] = {label: (mid, prov, label) for mid, prov, label in APEX_MODELS}

# Code defaults — engine_settings_db seeds its columns from these.
DEFAULT_CHAMPION: str = "Claude-Sonnet"
DEFAULT_FALLBACKS: tuple[str, ...] = ("GPT-5", "GPT-4o", "o3")

APEX_TIMEOUT: float = 90.0   # seconds before abandoning a model and moving down the fallback list


def known_model_labels() -> tuple[str, ...]:
    return tuple(_MODELS_BY_LABEL)


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
class ModelResult:
    """Prediction result from the model plan (champion, or a fallback after it failed)."""
    merged:        dict[str, Any]
    individuals:   list[SinglePrediction]   # every attempted call, in order
    model_sources: list[str]                # display name of the model that succeeded
    used_fallback: bool                     # True when the champion failed/timed out
    fallback_model: str = ""
    role:          str = ROLE_CHAMPION      # prediction_role for the rows this result produces
    fallback_reason: str = ""               # why the previous model was abandoned ("timeout", ...)


@dataclass(frozen=True)
class ModelPlan:
    """Resolved model selection for one run: (model_id, provider, label) tuples."""
    champion:       tuple[str, str, str]
    fallbacks:      tuple[tuple[str, str, str], ...]
    challenger:     tuple[str, str, str] | None = None
    challenger_pct: float = 0.0

    def describe(self) -> str:
        fb = " → ".join(m[2] for m in self.fallbacks) or "none"
        ch = f"{self.challenger[2]} on {self.challenger_pct:g}% of tickers" if self.challenger else "off"
        return f"champion {self.champion[2]} · fallbacks {fb} · challenger {ch}"


# ── Model plan ─────────────────────────────────────────────────────────────────

def build_model_plan(champion: str | None = None, fallbacks: list[str] | tuple[str, ...] | None = None,
                     challenger: str | None = None, challenger_pct: float | None = 0.0) -> ModelPlan:
    """
    Resolve configured labels against APEX_MODELS. An unknown champion falls
    back to DEFAULT_CHAMPION and unknown fallback entries are dropped — each with
    a warning — so a bad setting never stops the pipeline. The champion is
    removed from its own fallback list; duplicates keep their first position.
    A challenger equal to the champion, or a percentage <= 0, means off.
    """
    champ_label = champion if champion in _MODELS_BY_LABEL else DEFAULT_CHAMPION
    if champion and champion not in _MODELS_BY_LABEL:
        _log.warning(f"  [apex] unknown champion model {champion!r} in engine_settings — using {DEFAULT_CHAMPION}",
                     event_type="warning", model=champion)

    fb_labels: list[str] = []
    for label in (DEFAULT_FALLBACKS if fallbacks is None else fallbacks):
        if label not in _MODELS_BY_LABEL:
            _log.warning(f"  [apex] unknown fallback model {label!r} in engine_settings — skipped",
                         event_type="warning", model=label)
            continue
        if label != champ_label and label not in fb_labels:
            fb_labels.append(label)

    pct = max(0.0, min(100.0, float(challenger_pct or 0.0)))
    chall = None
    if challenger:
        if challenger not in _MODELS_BY_LABEL:
            _log.warning(f"  [apex] unknown challenger model {challenger!r} in engine_settings — challenger off",
                         event_type="warning", model=challenger)
        elif challenger == champ_label:
            _log.warning(f"  [apex] challenger {challenger} is the champion — challenger off",
                         event_type="warning", model=challenger)
        elif pct > 0:
            chall = _MODELS_BY_LABEL[challenger]

    return ModelPlan(
        champion=_MODELS_BY_LABEL[champ_label],
        fallbacks=tuple(_MODELS_BY_LABEL[m] for m in fb_labels),
        challenger=chall,
        challenger_pct=pct if chall else 0.0,
    )


def load_model_plan() -> ModelPlan:
    """The model plan from engine_settings; the code defaults if settings can't be read."""
    try:
        from portfolio_agent.tools.engine_settings_db import get_engine_settings
        s = get_engine_settings()
    except Exception as exc:
        _log.warning(f"  [apex] engine_settings unavailable ({type(exc).__name__}) — using default model plan",
                     event_type="warning")
        return build_model_plan()
    return build_model_plan(
        champion=s.get("apex_champion_model"),
        fallbacks=s.get("apex_fallback_models") or None,
        challenger=s.get("apex_challenger_model"),
        challenger_pct=s.get("apex_challenger_sample_pct"),
    )


def challenger_selected(ticker: str, as_of_date: str, pct: float) -> bool:
    """
    Deterministic sampling: the same (ticker, date) is always in or out for a
    given percentage, so a re-run never adds or drops challenger rows and tests
    are reproducible. Buckets of 0.01% allow fractional percentages.
    """
    if pct <= 0:
        return False
    if pct >= 100:
        return True
    bucket = int(hashlib.sha1(f"{ticker.upper()}|{as_of_date}".encode()).hexdigest()[:8], 16) % 10_000
    return bucket < pct * 100


def _failure_reason(result: SinglePrediction) -> str:
    if result.error == "timeout":
        return "timeout"
    if result.error == "no_valid_json":
        return "invalid_json"
    return f"failover_error: {result.error[:80]}"


# ── Public entry points ────────────────────────────────────────────────────────

async def run_apex(ticker: str, prompt: str, *, plan: ModelPlan | None = None) -> ModelResult | None:
    """
    Run the champion model for *ticker*; on timeout / failover error / invalid
    JSON, try each fallback in order. Returns None if every model fails —
    caller skips, no placeholder written.
    """
    plan = plan or load_model_plan()
    chain = [plan.champion, *plan.fallbacks]
    attempts: list[SinglePrediction] = []
    reason = ""

    for i, (mid, prov, label) in enumerate(chain):
        role = ROLE_CHAMPION if i == 0 else ROLE_FALLBACK
        why = "champion" if i == 0 else f"fallback {i}/{len(chain) - 1} after {attempts[-1].display_name} {reason}"
        _log.info(
            f"  [apex] {ticker} — {label} ({why})",
            event_type="apex_model_select", ticker=ticker, model=label, role=role, reason=why,
        )
        result = await _call_model_with_timeout(mid, prov, label, prompt, ticker)
        attempts.append(result)

        if result.succeeded:
            _log.info(
                f"  ✓ [apex] {ticker} — {label} ✓ ({result.latency_ms}ms, {role})",
                event_type="apex_complete", ticker=ticker, model=label,
                role=role, used_fallback=i > 0,
            )
            return ModelResult(
                merged=result.data, individuals=attempts,
                model_sources=[label],
                used_fallback=i > 0,
                fallback_model=label if i > 0 else "",
                role=role,
                fallback_reason=reason if i > 0 else "",
            )

        reason = _failure_reason(result)
        nxt = chain[i + 1][2] if i + 1 < len(chain) else None
        if nxt:
            _log.warning(
                f"  [apex] {ticker} — {label} {reason}, retrying with {nxt}",
                event_type="apex_retry", ticker=ticker,
                failed_model=label, retry_model=nxt, reason=result.error,
            )

    _log.error(
        f"  [apex] {ticker} — all {len(chain)} models failed — skipping",
        event_type="apex_exhausted", ticker=ticker,
        models=[a.display_name for a in attempts],
        errors=[a.error[:120] for a in attempts],
    )
    return None


async def run_challenger(ticker: str, prompt: str, plan: ModelPlan) -> ModelResult | None:
    """
    One attempt with the plan's challenger model (no fallback: a challenger
    that fails simply produces no comparison row). None when the plan has no
    challenger or the call fails.
    """
    if plan.challenger is None:
        return None
    mid, prov, label = plan.challenger
    _log.info(
        f"  [apex] {ticker} — challenger {label} (champion {plan.champion[2]})",
        event_type="apex_challenger", ticker=ticker, model=label, role=ROLE_CHALLENGER,
        champion=plan.champion[2],
    )
    result = await _call_model_with_timeout(mid, prov, label, prompt, ticker)
    if not result.succeeded:
        _log.warning(
            f"  [apex] {ticker} — challenger {label} {_failure_reason(result)} — no challenger row",
            event_type="apex_challenger", ticker=ticker, model=label, role=ROLE_CHALLENGER, reason=result.error,
        )
        return None
    return ModelResult(
        merged=result.data, individuals=[result], model_sources=[label],
        used_fallback=False, role=ROLE_CHALLENGER,
    )


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
