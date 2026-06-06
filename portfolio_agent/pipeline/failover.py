"""
Model failover helpers.

Centralizes:
  - CreditExhaustedError
  - Error-classification predicates (_is_ticker_only_fallback, _is_context_too_large)
  - Groq RPM / TPD sleep-and-retry helpers (_is_groq_rpm_error, _groq_should_sleep, …)
"""

from __future__ import annotations

import re as _re

from portfolio_agent._models import _is_failover_error  # noqa: F401 (re-exported)


class CreditExhaustedError(RuntimeError):
    """Raised when all models in the failover chain are exhausted."""


def _is_ticker_only_fallback(exc: BaseException) -> bool:
    """Return True for errors that should fall back for THIS ticker only.

    Unlike _is_failover_error, these do NOT advance the session pointer —
    the model may work fine for the next ticker (e.g. intermittent XML tool calls).
    """
    msg = str(exc).lower()
    return any(k in msg for k in [
        "tool_use_failed",           # Groq intermittent XML tool-call format
        "failed to call a function", # Groq companion message
        "failed_generation",         # Groq raw JSON field name
    ])


def _is_context_too_large(exc: BaseException) -> bool:
    """Return True when the model rejected because the prompt exceeds its context window."""
    msg = str(exc).lower()
    return any(k in msg for k in [
        "request too large for",
        "context_length_exceeded",
        "context length exceeded",
        "maximum context length",
        "prompt is too long",
        "reduce the length of your prompt",
        "your input contains too many",
        "exceeds the maximum allowed number of tokens",
    ])


# ── Groq RPM sleep-and-retry ──────────────────────────────────────────────────
# Groq enforces a per-minute (RPM) request quota.  When ALL models in the chain
# are exhausted and the last failure was a Groq rate-limit, we sleep one full
# RPM window and retry the current batch from the Groq slot — instead of
# immediately checkpointing.  If Groq fails again (daily quota, not just RPM)
# we fall through to the normal checkpoint-and-exit path.

_GROQ_RPM_WINDOW_SECS = 65   # slightly longer than Groq's 60-second RPM/TPM window
_GROQ_MAX_SLEEP_SECS  = 300  # >5 min Retry-After → checkpoint instead of sleeping


def _is_groq_tpd_error(exc: BaseException | None) -> bool:
    """True if the Groq error is a daily-quota (TPD) exhaustion rather than per-minute RPM/TPM."""
    if exc is None:
        return False
    msg = str(exc)
    return ("tokens per day" in msg.lower() or "tpd" in msg.lower())


def _is_groq_rpm_error(exc: BaseException | None) -> bool:
    """True if *exc* looks like a Groq per-minute rate limit (resettable after ~60s)."""
    if exc is None:
        return False
    msg = str(exc).lower()
    return ("groq" in msg or "groqexception" in msg) and "rate limit" in msg


def _first_groq_idx(chain: list) -> int | None:
    """Return the index of the first Groq model in *chain*, or None if absent."""
    for idx, (_, provider, _) in enumerate(chain):
        if provider == "groq":
            return idx
    return None


def _groq_retry_secs(exc: BaseException) -> float | None:
    """Extract the suggested retry delay (seconds) from a Groq rate-limit error.

    Handles all formats: "22.37s", "35m51.3s", "2h30m3s".
    Returns None if the wait exceeds _GROQ_MAX_SLEEP_SECS (callers must checkpoint).
    """
    text = str(exc)

    m = _re.search(r"(\d+)h(\d+)m(\d+(?:\.\d+)?)s", text, _re.IGNORECASE)
    if m:
        total = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        wait = total + 2.0
        return None if wait > _GROQ_MAX_SLEEP_SECS else wait

    m = _re.search(r"(\d+)m(\d+(?:\.\d+)?)s", text, _re.IGNORECASE)
    if m:
        total = int(m.group(1)) * 60 + float(m.group(2))
        wait = total + 2.0
        return None if wait > _GROQ_MAX_SLEEP_SECS else wait

    m = _re.search(r"(\d+(?:\.\d+)?)s", text, _re.IGNORECASE)
    if m:
        secs = float(m.group(1))
        if secs < 1:
            secs = 1.0
        wait = secs + 2.0
        return None if wait > _GROQ_MAX_SLEEP_SECS else wait

    return float(_GROQ_RPM_WINDOW_SECS)


def _groq_should_sleep(exc: BaseException) -> tuple[bool, float]:
    """Return (should_sleep, seconds) for a Groq rate-limit error.

    Returns (False, 0) when the wait exceeds _GROQ_MAX_SLEEP_SECS — callers must checkpoint.
    """
    secs = _groq_retry_secs(exc)
    if secs is None:
        return False, 0.0
    return True, secs
