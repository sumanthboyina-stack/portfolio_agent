"""
LLM model registry — task-aware routing with multi-provider failover.

Hierarchy principle:
  Low-reasoning tasks  (triage, flash, fast):   free Gemini/Groq/Cerebras/OpenRouter
  High-reasoning tasks (analyst, reasoning):     paid quality-first → OR high-reasoning → Groq

Task → model mapping (in priority order):

  triage    Gemini-2.5-Flash-Lite → Gemini-2.5-Flash → Groq-70B → Cerebras-120B
            → OR-Laguna-M.1 → OR-Nemotron-Nano → OR-Ring-1T → OR-Granite-8B
            Bulk news batch classification (no paid models — Lite has higher daily quota).
            ⚠ Cerebras has 8K context cap — batch size may need reducing when it kicks in.

  flash     Gemini-2.5-Flash → Gemini-2.5-Flash-Lite → Groq-70B → Cerebras-120B
            → OR-Laguna-M.1 → OR-Qwen3.6-Flash → OR-Ring-1T
            Fundamentals / research batch LLM (no paid models).

  fast      Gemini-2.5-Flash → Gemini-2.5-Flash-Lite → Groq-70B → Cerebras-120B
            → OR-Laguna-M.1 → OR-Ring-1T
            Clearance, technical. Uses ADK tools.

  analyst   Claude-Sonnet → GPT-4o → Claude-Haiku → Gemini-Flash → GPT-mini → Gemini-Flash-Lite
            → OR-Claude-Opus-4.8 → OR-Qwen3.7-Max → OR-Grok-4.3 → OR-Mistral-Med-3.5 → Groq-70B
            Macro regime, risk synthesis. Uses ADK tools.

  reasoning Claude-Sonnet → GPT-4o → OR-Claude-Opus-4.8 → OR-Qwen3.7-Max → OR-Grok-4.3 → Groq-70B
            Interactive APEX chat (single-ticker). Daily batch APEX uses a separate
            dual-run strategy: Claude-Sonnet + GPT-4o run in parallel, results are
            merged; OR high-reasoning models are mid-tier fallbacks; Groq-70B is last resort.

OpenRouter (OR): gateway to 300+ models via single key (OPENROUTER_API_KEY).
  High-reasoning (analyst/reasoning): OR-Claude-Opus-4.8, OR-Qwen3.7-Max, OR-Grok-4.3
  Free/cheap (triage/flash/fast):     OR-Owl-Alpha (free), OR-Laguna-M.1 (free),
                                       OR-Nemotron-Nano (free), OR-Ring-1T (~$0.1/M),
                                       OR-Granite-8B (~$0.05/M)

ModelSession: tracks the working model index across a batch of same-agent calls.
Once a model fails within a batch, all subsequent tickers in that batch start at
the next model — no re-discovering the same rate limit on every ticker.

Groq (llama-3.3-70b-versatile) supports structured tool
calling via LiteLLM 1.83+ and works correctly with ADK's tool-call JSON format.

Failover triggers: credit exhaustion, 429 rate limit, quota exceeded,
context-length exceeded, 529 overloaded, provider outage, model not found (404).
"""

from __future__ import annotations

import os
import threading
from datetime import date

# Suppress noisy LiteLLM "use Gemini directly" warnings
os.environ.setdefault("ADK_SUPPRESS_GEMINI_LITELLM_WARNINGS", "true")

from google.adk.models.lite_llm import LiteLlm
from portfolio_agent.log import get_logger as _get_logger
_log = _get_logger("models")

# ── Provider model IDs ─────────────────────────────────────────────────────────

_GROQ_70B          = "groq/llama-3.3-70b-versatile"
_GEMINI_FLASH      = "gemini/gemini-2.5-flash"
_GEMINI_FLASH_LITE = "gemini/gemini-2.5-flash-lite"   # primary triage — higher daily quota
_CEREBRAS_120B     = "cerebras/gpt-oss-120b"           # available on free tier; 1M TPD, ⚠ 8K ctx cap
_HAIKU             = "anthropic/claude-haiku-4-5-20251001"
_SONNET            = "anthropic/claude-sonnet-4-6"
_GPT_MINI          = "openai/gpt-4o-mini"   # cheap — same tier as Haiku / Groq-70B
_GPT_4O            = "openai/gpt-4o"        # capable — same tier as Claude Sonnet

# ── OpenRouter model IDs (prefix: openrouter/<provider>/<model>) ──────────────
# High-reasoning — for analyst / reasoning (APEX) chains
_OR_CLAUDE_OPUS48  = "openrouter/anthropic/claude-opus-4.8"          # $5/$25/M; most capable Claude
_OR_QWEN37_MAX     = "openrouter/qwen/qwen3.7-max"                    # $1.25/$3.75/M; strong agentic
_OR_GROK43         = "openrouter/x-ai/grok-4.3"                       # $1.25/$2.5/M; reasoning
_OR_MISTRAL_MED35  = "openrouter/mistralai/mistral-medium-3.5"        # $1.5/$7.5/M; 128B dense

# Free / ultra-cheap — for triage / flash / fast chains
_OR_LAGUNA_M1      = "openrouter/poolside/laguna-m.1:free"            # FREE; 262K ctx; tools
_OR_NEMOTRON_FREE  = "openrouter/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"  # FREE; 256K
_OR_RING_1T        = "openrouter/inclusionai/ring-2.6-1t"             # $0.075/$0.625/M; 262K; tools
_OR_QWEN36_FLASH   = "openrouter/qwen/qwen3.6-flash"                  # $0.19/$1.13/M; fast; tools
_OR_GRANITE_8B     = "openrouter/ibm-granite/granite-4.1-8b"          # $0.05/$0.1/M; 131K; tools

# ── Gemini daily call counter ──────────────────────────────────────────────────

class _GeminiCallCounter:
    """Thread-safe per-day counter for Gemini API calls, keyed by model name."""

    def __init__(self) -> None:
        self._lock   = threading.Lock()
        self._counts: dict[str, int] = {}
        self._date   = date.today().isoformat()

    def record(self, model_id: str) -> None:
        key = model_id.split("/")[-1]   # "gemini-2.5-flash" from "gemini/gemini-2.5-flash"
        with self._lock:
            today = date.today().isoformat()
            if today != self._date:     # new day — reset
                self._counts = {}
                self._date   = today
            self._counts[key] = self._counts.get(key, 0) + 1

    def stats(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)

    def print_stats(self, prefix: str = "") -> None:
        with self._lock:
            if not self._counts:
                _log.info(f"  [gemini/calls]{prefix} no Gemini calls recorded today",
                          event_type="summary", total=0)
                return
            total     = sum(self._counts.values())
            breakdown = "  ".join(f"{k}={v}" for k, v in sorted(self._counts.items()))
            _log.info(f"  [gemini/calls]{prefix} today={total}  {breakdown}",
                      event_type="summary", total=total, breakdown=dict(self._counts))


GEMINI_COUNTER = _GeminiCallCounter()

# ── Failover chains ────────────────────────────────────────────────────────────
# Each entry: (litellm_model_id, provider_label, display_name)

FAILOVER_CHAINS: dict[str, list[tuple[str, str, str]]] = {
    # Low-reasoning: free Gemini/Groq/Cerebras first, then OpenRouter free/cheap fallbacks.
    # Lite model is primary for triage: higher daily quota for bulk batches.
    # Cerebras last among direct providers: 1M TPD free but 8K context cap.
    # OpenRouter free tier (Owl-Alpha, Laguna, Nemotron) added as final safety net.
    # OR-Owl-Alpha and Groq-8B removed from all chains:
    # OR-Owl-Alpha produced 20% directional accuracy (n=9) in validation — below random.
    # Groq-8B produced 0% directional accuracy (n=1) — removed on principle.
    # Neither should appear in model_name of any pipeline output.
    "triage": [
        (_GEMINI_FLASH_LITE, "google",      "Gemini-2.5-Flash-Lite"),
        (_GEMINI_FLASH,      "google",      "Gemini-2.5-Flash"),
        (_GROQ_70B,          "groq",        "Groq Llama-3.3-70B"),
        (_CEREBRAS_120B,     "cerebras",    "Cerebras GPT-OSS-120B"),
        (_OR_LAGUNA_M1,      "openrouter",  "OR-Laguna-M.1"),
        (_OR_NEMOTRON_FREE,  "openrouter",  "OR-Nemotron-Nano"),
        (_OR_RING_1T,        "openrouter",  "OR-Ring-2.6-1T"),
        (_OR_GRANITE_8B,     "openrouter",  "OR-Granite-4.1-8B"),
    ],
    "flash": [
        (_GEMINI_FLASH,      "google",      "Gemini-2.5-Flash"),
        (_GEMINI_FLASH_LITE, "google",      "Gemini-2.5-Flash-Lite"),
        (_GROQ_70B,          "groq",        "Groq Llama-3.3-70B"),
        (_CEREBRAS_120B,     "cerebras",    "Cerebras GPT-OSS-120B"),
        (_OR_LAGUNA_M1,      "openrouter",  "OR-Laguna-M.1"),
        (_OR_QWEN36_FLASH,   "openrouter",  "OR-Qwen3.6-Flash"),
        (_OR_RING_1T,        "openrouter",  "OR-Ring-2.6-1T"),
    ],
    "fast": [
        (_GEMINI_FLASH,      "google",      "Gemini-2.5-Flash"),
        (_GEMINI_FLASH_LITE, "google",      "Gemini-2.5-Flash-Lite"),
        (_GROQ_70B,          "groq",        "Groq Llama-3.3-70B"),
        (_CEREBRAS_120B,     "cerebras",    "Cerebras GPT-OSS-120B"),
        (_OR_LAGUNA_M1,      "openrouter",  "OR-Laguna-M.1"),
        (_OR_RING_1T,        "openrouter",  "OR-Ring-2.6-1T"),
    ],
    # High-reasoning: paid direct providers first, then OR high-reasoning fallbacks only.
    # Haiku, Flash, Flash-Lite, GPT-4o-mini, Groq-70B intentionally excluded —
    # APEX predictions must never fall back to lower-end models.
    "analyst": [
        (_SONNET,            "anthropic",   "Claude-Sonnet"),
        (_GPT_4O,            "openai",      "GPT-4o"),
        (_OR_CLAUDE_OPUS48,  "openrouter",  "OR-Claude-Opus-4.8"),
        (_OR_QWEN37_MAX,     "openrouter",  "OR-Qwen3.7-Max"),
        (_OR_GROK43,         "openrouter",  "OR-Grok-4.3"),
        (_OR_MISTRAL_MED35,  "openrouter",  "OR-Mistral-Med-3.5"),
    ],
    "reasoning": [
        # Used by the interactive APEX chat path (single-ticker linear failover).
        # Daily batch APEX uses a dual-run strategy (see _run_daily_apex in main.py):
        #   Claude-Sonnet + GPT-4o run in parallel → results merged;
        #   OR high-reasoning models are mid-tier fallbacks.
        # Lower-end models (Haiku, Flash, Groq) intentionally excluded.
        (_SONNET,           "anthropic",   "Claude-Sonnet"),
        (_GPT_4O,           "openai",      "GPT-4o"),
        (_OR_CLAUDE_OPUS48, "openrouter",  "OR-Claude-Opus-4.8"),
        (_OR_QWEN37_MAX,    "openrouter",  "OR-Qwen3.7-Max"),
        (_OR_GROK43,        "openrouter",  "OR-Grok-4.3"),
        (_OR_MISTRAL_MED35, "openrouter",  "OR-Mistral-Med-3.5"),
    ],
}

# Specialist name → task tier
AGENT_TASK_TYPE: dict[str, str] = {
    "news":         "triage",
    "fundamentals": "flash",
    "research":     "flash",
    "macro":        "analyst",
    "risk":         "analyst",
    "reasoning":    "reasoning",
    "synthesis":    "reasoning",
    "clearance":    "fast",
    "technical":    "fast",
}

# ── Failover error detection ───────────────────────────────────────────────────

_PROVIDER_DOMAINS = (
    "api.anthropic.com",
    "generativelanguage.googleapis.com",
    "api.groq.com",
    "api.openai.com",
    "openai.azure.com",
    "api.cerebras.ai",
    "openrouter.ai",
)


def _is_failover_error(exc: BaseException) -> bool:
    """Return True for any transient error that warrants trying the next model.

    Explicitly excludes requests-library HTTP errors against external data APIs
    (SEC.gov, FRED, etc.). Those are tool-execution failures — switching to a
    different model won't help and shouldn't burn the session's model index.
    """
    msg = str(exc).lower()

    # requests.HTTPError format: "NNN Client/Server Error: ... for url: https://..."
    # Only treat as failover if the URL is a model-provider endpoint.
    if "client error" in msg and "for url:" in msg:
        if not any(d in msg for d in _PROVIDER_DOMAINS):
            return False
    if "server error" in msg and "for url:" in msg:
        if not any(d in msg for d in _PROVIDER_DOMAINS):
            return False

    # Anthropic wraps credit-exhaustion and model-not-found under the generic
    # "invalid_request_error" type (HTTP 400). It is a provider-level block, not a
    # malformed-request bug, so we failover to the next provider rather than crash.
    # The actual reason (credits, model not found, etc.) is in the "message" field of
    # the JSON body — visible in logs now that error truncation is 300 chars.
    if "anthropicexception" in msg and "invalid_request_error" in msg:
        return True

    return any(k in msg for k in [
        "credit balance is too low",    # Anthropic credits
        "insufficient_quota",           # Google quota
        "resource_exhausted",           # Google 429
        "rate_limit_exceeded",          # Groq / generic
        "rate limit",                   # generic
        "429",                          # HTTP Too Many Requests
        "503",                          # Service Unavailable — Gemini high demand, retry on next model
        "529",                          # Anthropic overloaded
        "overloaded_error",
        "service_unavailable",          # Gemini status=UNAVAILABLE
        "unavailable",                  # Gemini "currently experiencing high demand"
        "high demand",                  # Gemini demand spike message
        "context_length_exceeded",      # token limit
        "maximum context length",
        "tokens exceed",
        "token limit",
        "quota exceeded",
        "payment required",             # 402
        "insufficient credits",
        "billing",
        "not_found",                    # 404 — model deprecated / not available
        "notfounderror",
        "is not found for api",         # Gemini model not found message
        "model not found",
    ])


# ── Session-level sticky model selection ──────────────────────────────────────

class ModelSession:
    """
    Tracks the working model index across a batch of same-agent calls.

    Once a model fails within a batch, all subsequent tickers start at the
    next model in the chain instead of re-discovering the same rate limit.

    Usage:
        session = ModelSession("fundamentals")
        for ticker in tickers:
            state = await run_analysis_with_failover(ticker, session=session)
    """

    def __init__(self, agent_name: str) -> None:
        task = AGENT_TASK_TYPE.get(agent_name, "fast")
        self._chain = FAILOVER_CHAINS.get(task, FAILOVER_CHAINS["fast"])
        self._agent_name = agent_name
        self._idx = 0   # next call starts here

    @property
    def agent_name(self) -> str:
        return self._agent_name

    @property
    def start_idx(self) -> int:
        return self._idx

    @property
    def chain(self) -> list[tuple[str, str, str]]:
        return self._chain

    @property
    def exhausted(self) -> bool:
        return self._idx >= len(self._chain)

    def on_failure(self, failed_idx: int) -> None:
        """Advance the session pointer past the given chain index."""
        if failed_idx >= self._idx:
            self._idx = failed_idx + 1

    def print_remaining(self) -> None:
        """Log the remaining models this session will try."""
        remaining = self._chain[self._idx:]
        labels = " → ".join(f"{label} ({prov})" for _, prov, label in remaining)
        prefix = f"  [chain/{self._agent_name}]"
        if self._idx > 0:
            skipped = self._chain[:self._idx]
            skip_labels = ", ".join(label for _, _, label in skipped)
            _log.info(f"{prefix} (skipping {skip_labels}) → {labels}",
                      event_type="info", agent=self._agent_name,
                      skipped=[label for _, _, label in skipped],
                      remaining=[label for _, _, label in remaining])
        else:
            _log.info(f"{prefix} {labels}",
                      event_type="info", agent=self._agent_name,
                      remaining=[label for _, _, label in remaining])


# ── Convenience model objects (primary for each tier) ─────────────────────────

def _lite(model_id: str) -> LiteLlm:
    return LiteLlm(model=model_id)


TRIAGE_MODEL    = _lite(_GEMINI_FLASH_LITE)   # news bulk triage — Lite has higher daily quota
FLASH_MODEL     = _lite(_GEMINI_FLASH)        # fundamentals, research
ANALYST_MODEL   = _lite(_SONNET)              # macro, risk, APEX reasoning
FAST_MODEL      = _lite(_GEMINI_FLASH)        # clearance, technical (free first)
SYNTHESIS_MODEL = ANALYST_MODEL               # backward-compat alias


def get_primary_model(task_type: str) -> LiteLlm:
    """Return the primary LiteLlm object for a given task tier."""
    chain = FAILOVER_CHAINS.get(task_type, FAILOVER_CHAINS["fast"])
    return _lite(chain[0][0])


def get_model_for_agent(agent_name: str) -> LiteLlm:
    """Return the primary model for a named specialist agent."""
    task = AGENT_TASK_TYPE.get(agent_name, "fast")
    return get_primary_model(task)


def print_chain(agent_name: str) -> None:
    """Log the full failover chain for an agent (for log transparency)."""
    task  = AGENT_TASK_TYPE.get(agent_name, "fast")
    chain = FAILOVER_CHAINS.get(task, FAILOVER_CHAINS["fast"])
    labels = " → ".join(f"{label} ({prov})" for _, prov, label in chain)
    _log.info(f"  [chain/{agent_name}] {labels}",
              event_type="info", agent=agent_name, task=task,
              chain=[label for _, _, label in chain])
