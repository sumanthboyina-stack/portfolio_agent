"""
The only two doors into the predictions table.

    write_shared_forecast(context, provenance, **row)   scheduled, market-only generation
    write_private_forecast(owner, provenance, **row)    chat questions, personalized results

A shared forecast needs a MarketContext (the proof it was built from
market-only inputs), an origin on the shared allow-list, and text free of
private terms. Anything else is private and stored with its owner. Nothing
here ever logs forecast text.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field

from portfolio_agent.log import get_logger
from portfolio_agent.privacy import PrivateDataLeak, assert_market_only, collect_private_terms
from portfolio_agent.tools.market_context import MarketContext
from portfolio_agent.tools.prediction_db import (
    CURRENT_SYSTEM_VERSION, SCOPE_PRIVATE, SCOPE_SHARED, insert_prediction,
)

GUARDRAIL_VERSION = "conviction-guardrails/1.0"       # apex._apply_conviction_guardrails
SHARED_ORIGINS = frozenset({"pipeline.apex"})         # scheduled market-only generation only
_log = get_logger("forecast_writer")


class ForecastScopeError(PermissionError):
    """A writer tried to store something as shared that is not entitled to be."""


def prompt_policy_version() -> str:
    """System version plus a hash of the APEX prompt template actually in use."""
    try:
        from portfolio_agent.pipeline.prompt_templates import _APEX_PROMPT_TEMPLATE
        h = hashlib.sha1(_APEX_PROMPT_TEMPLATE.encode()).hexdigest()[:10]
    except Exception:
        h = "unknown"
    return f"{CURRENT_SYSTEM_VERSION}|apex-prompt:{h}"


@dataclass(frozen=True)
class ForecastProvenance:
    origin: str                                  # "pipeline.apex", "chat.ticker_prediction", ...
    model_used: str                              # "provider:model-label" actually used
    policy_version: str = field(default_factory=prompt_policy_version)
    guardrail_version: str | None = None
    origin_run_id: str | None = field(default_factory=lambda: os.environ.get("PIPELINE_RUN_ID"))
    extra_evidence: dict = field(default_factory=dict)


_TEXT_FIELDS = ("prediction", "recommendation", "reasoning", "reasoning_text")


def _shared_text(row: dict) -> str:
    parts = [str(row.get(k) or "") for k in _TEXT_FIELDS]
    ps = row.get("panel_summary")
    if ps:
        parts.append(str(ps))
    return "\n".join(parts)


def write_shared_forecast(context: MarketContext, provenance: ForecastProvenance, **row) -> dict:
    if not isinstance(context, MarketContext) or not context.market_only:
        raise ForecastScopeError("a shared forecast requires a market-only MarketContext")
    if provenance.origin not in SHARED_ORIGINS:
        raise ForecastScopeError(f"origin {provenance.origin!r} may not write shared forecasts")
    ticker = str(row.get("ticker") or context.ticker).upper()
    if ticker != context.ticker:
        raise ForecastScopeError("forecast ticker does not match its market context")
    hz = row.get("horizon_days")
    if hz is not None and int(hz) not in context.horizons:
        raise ForecastScopeError(f"horizon {hz} was not part of the market context {list(context.horizons)}")
    try:
        assert_market_only(_shared_text(row), collect_private_terms(), what=f"shared forecast for {ticker}")
    except PrivateDataLeak as exc:
        _log.warning(f"  [scope] {ticker} {hz}d — shared write refused: private text detected",
                     event_type="warning", ticker=ticker)
        return {"saved": False, "ticker": ticker, "error": f"private_text_rejected: {exc}"}
    res = insert_prediction(
        **row, scope=SCOPE_SHARED, origin=provenance.origin, origin_run_id=provenance.origin_run_id,
        owner_scope=None, evidence_refs={**context.evidence_refs, **provenance.extra_evidence},
        policy_version=provenance.policy_version, model_used=provenance.model_used,
        guardrail_version=provenance.guardrail_version, history_lineage=list(context.history_lineage),
        context_digest=context.digest,
    )
    if res.get("saved"):
        _log.info(f"  [shared] {ticker} {hz}d saved (origin={provenance.origin}, ctx={context.digest})",
                  event_type="db_write", ticker=ticker)
    return res


def write_private_forecast(owner: str, provenance: ForecastProvenance, **row) -> dict:
    ticker = str(row.get("ticker") or "").upper()
    res = insert_prediction(
        **row, scope=SCOPE_PRIVATE, origin=provenance.origin, origin_run_id=provenance.origin_run_id,
        owner_scope=f"user:{owner}", evidence_refs=provenance.extra_evidence or None,
        policy_version=provenance.policy_version, model_used=provenance.model_used,
        guardrail_version=provenance.guardrail_version, history_lineage=None, context_digest=None,
    )
    if res.get("saved"):
        _log.info(f"  [private] {ticker} {row.get('horizon_days')}d saved (origin={provenance.origin})",
                  event_type="db_write", ticker=ticker)
    return res
