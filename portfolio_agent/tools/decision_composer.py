"""
Common decision composition — the one place that combines the three result
types built in earlier phases into a single, consistently-shaped answer:

  MarketForecast      (tools.market_context / stored SHARED predictions)
                       — shared, ticker-only, no portfolio or identity.
  PortfolioAssessment  (tools.portfolio_assessment)
                       — private, owner-scoped: exposures, limits, sizing.
  PolicyDecision       (tools.clearance)
                       — private, action-specific: ALLOWED_BY_RULES/BLOCKED/
                        PENDING_APPROVAL/UNKNOWN, re-evaluated fresh on every
                        call (cheap and deterministic — no LLM, no network),
                        so an actionable proposal is never built from a stale
                        cached permission.

compose_decision() makes ZERO specialist/provider calls by default — it only
reads what's already stored/computable. Reuse rule: the market-outlook piece
reports whether the stored SHARED forecast is still valid evidence (tools.
run_planner.plan_reuse, same reuse logic Phase 4 built for batch generation)
instead of ever regenerating it itself; a caller that finds source="stale"
or "unavailable" decides whether to trigger the EXISTING generation path
(pipeline.daily.apex / web.chat.orchestration) and call compose_decision
again — composition and generation stay separate responsibilities.

The private question that prompted a composition (if any) is NEVER passed
into anything that touches shared generation — _market_outlook() doesn't
even accept it as a parameter. It may only ever reach the optional, private,
explicitly opt-in explanation step, which is the one place an LLM call is
allowed here and which never changes market_outlook/portfolio_fit/
action_permission after the fact (see ComposedDecision.actionable_recommendation,
computed BEFORE the explanation step ever runs).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class MarketOutlook:
    ticker: str
    horizon_days: int | None
    prediction: str | None
    recommendation: str | None
    composite_score: float | None
    as_of: str | None                # created_at of the stored shared forecast, if any
    context_digest: str | None
    source: str                       # "reused" | "stale" | "unavailable"
    reason: str

    def to_dict(self) -> dict:
        return {"ticker": self.ticker, "horizon_days": self.horizon_days, "prediction": self.prediction,
                "recommendation": self.recommendation, "composite_score": self.composite_score,
                "as_of": self.as_of, "context_digest": self.context_digest, "source": self.source,
                "reason": self.reason}


@dataclass(frozen=True)
class PortfolioFit:
    status: str
    current_weight_pct: object
    sector_concentration_pct: object
    issuer_concentration_pct: object
    fit_score: float | None
    limits: list
    sizing: dict
    as_of: str

    def to_dict(self) -> dict:
        return {"status": self.status, "current_weight_pct": self.current_weight_pct,
                "sector_concentration_pct": self.sector_concentration_pct,
                "issuer_concentration_pct": self.issuer_concentration_pct, "fit_score": self.fit_score,
                "limits": [c.to_dict() if hasattr(c, "to_dict") else c for c in self.limits],
                "sizing": self.sizing, "as_of": self.as_of}


@dataclass(frozen=True)
class ActionPermission:
    decision: str
    reason_codes: list
    memo: str
    evaluated_at: str
    next_transition_at: str | None
    valid_until: str | None

    def to_dict(self) -> dict:
        return {"decision": self.decision, "reason_codes": self.reason_codes, "memo": self.memo,
                "evaluated_at": self.evaluated_at, "next_transition_at": self.next_transition_at,
                "valid_until": self.valid_until}


@dataclass(frozen=True)
class ComposedDecision:
    ticker: str
    action: str
    market_outlook: MarketOutlook
    portfolio_fit: PortfolioFit
    action_permission: ActionPermission
    actionable_recommendation: str | None   # the ONLY field a UI may render as "what to do" — see compose_decision
    explanation: str | None
    composed_at: str

    def to_dict(self) -> dict:
        return {"ticker": self.ticker, "action": self.action,
                "market_outlook": self.market_outlook.to_dict(), "portfolio_fit": self.portfolio_fit.to_dict(),
                "action_permission": self.action_permission.to_dict(),
                "actionable_recommendation": self.actionable_recommendation, "explanation": self.explanation,
                "composed_at": self.composed_at}


ALLOWED_DECISION = "ALLOWED_BY_RULES"   # local mirror of clearance.DECISION_ALLOWED, avoids an import cycle risk


def _current_evidence_refs(ticker: str) -> dict:
    """Same freshness signals market_context.py's evidence_refs records —
    pure DB reads, no network, no LLM."""
    from portfolio_agent.tools.market_context import MARKET_CONTEXT_VERSION, _get
    from portfolio_agent.tools.fundamentals_db import get_stored_fundamentals
    from portfolio_agent.tools.research_db import get_stored_research
    from portfolio_agent.tools.technical_features import get_latest_technical_features
    from portfolio_agent.tools.valuation_db import get_stored_valuation

    fundamentals = get_stored_fundamentals(ticker)
    research = get_stored_research(ticker)
    valuation = get_stored_valuation(ticker)
    try:
        technical = get_latest_technical_features(ticker)
    except Exception:
        technical = None
    return {
        "context_version": MARKET_CONTEXT_VERSION,
        "fundamentals_as_of": _get(fundamentals, "as_of_date", "filing_date"),
        "research_as_of": _get(research, "as_of_date", "fetched_at", "updated_at"),
        "valuation_as_of": _get(valuation, "as_of_date", "computed_at"),
        "technical_as_of": _get(technical, "data_snapshot"),
    }


def _market_outlook(ticker: str, horizon_days: int) -> MarketOutlook:
    """Retrieval only — never generates, never receives a private question."""
    from portfolio_agent.tools.prediction_db import SHARED_SCOPES, get_latest_prediction
    from portfolio_agent.tools.forecast_writer import prompt_policy_version, GUARDRAIL_VERSION
    from portfolio_agent.tools import run_planner

    latest = get_latest_prediction(ticker, horizon_days=horizon_days, scopes=SHARED_SCOPES)
    if latest is None:
        return MarketOutlook(ticker=ticker, horizon_days=horizon_days, prediction=None, recommendation=None,
                             composite_score=None, as_of=None, context_digest=None, source="unavailable",
                             reason="no shared forecast on file for this ticker/horizon")

    reuse_ok, reason, _manifest = run_planner.plan_reuse(
        latest_shared_row=latest.to_dict(), current_evidence_refs=_current_evidence_refs(ticker),
        current_policy_version=prompt_policy_version(), current_guardrail_version=GUARDRAIL_VERSION,
    )
    return MarketOutlook(
        ticker=ticker, horizon_days=horizon_days, prediction=latest.prediction, recommendation=latest.recommendation,
        composite_score=latest.composite_score, as_of=latest.created_at, context_digest=latest.context_digest,
        source="reused" if reuse_ok else "stale", reason=reason,
    )


def _portfolio_fit(ctx, ticker: str) -> PortfolioFit:
    from portfolio_agent.tools.portfolio_assessment import build_portfolio_assessment

    pa = build_portfolio_assessment(ctx, ticker)
    return PortfolioFit(
        status=pa.fit.get("status", "OK"), current_weight_pct=pa.exposures.get("current_weight_pct"),
        sector_concentration_pct=pa.exposures.get("sector_concentration_pct"),
        issuer_concentration_pct=pa.exposures.get("issuer_concentration_pct"), fit_score=pa.fit.get("fit_score"),
        limits=pa.limits, sizing=pa.sizing, as_of=pa.built_at,
    )


def _action_permission(ctx, ticker: str, action: str) -> ActionPermission:
    """Re-evaluated fresh on every call (see module docstring) — cheap and
    deterministic, so an actionable proposal never relies on a stale cached
    permission from an earlier composition or an earlier day."""
    from portfolio_agent.clearance import evaluate_and_record_clearance
    from portfolio_agent.tools.user_profile_db import get_user_profile_for

    profile = get_user_profile_for(ctx)
    decision = evaluate_and_record_clearance(ticker, profile, owner_scope=f"user:{ctx.actor}", action=action)
    return ActionPermission(
        decision=decision.decision, reason_codes=list(decision.evidence_refs.get("reason_codes") or []),
        memo=decision.memo, evaluated_at=decision.evidence_refs.get("evaluated_at", ""),
        next_transition_at=decision.next_transition_at, valid_until=decision.valid_until,
    )


def compose_decision(ctx, ticker: str, *, action: str = "trade", horizon_days: int = 5,
                     private_question: str | None = None, allow_llm_explanation: bool = False) -> ComposedDecision:
    """
    ctx must resolve to an authorized portfolio (checked by _portfolio_fit's
    and _action_permission's own authorization calls — this function touches
    no private data before either of those). private_question is accepted
    ONLY for the optional explanation step below; nothing else in this
    function ever sees it.
    """
    ticker = ticker.upper().strip()
    market_outlook = _market_outlook(ticker, horizon_days)
    portfolio_fit = _portfolio_fit(ctx, ticker)
    action_permission = _action_permission(ctx, ticker, action)

    # The ONLY field a caller should render as "what to do" — already
    # policy-qualified. A BLOCKED/PENDING_APPROVAL/UNKNOWN permission means
    # this is None regardless of what the raw market recommendation says.
    actionable_recommendation = (
        market_outlook.recommendation if action_permission.decision == ALLOWED_DECISION else None
    )

    explanation = None
    if allow_llm_explanation:
        explanation = _generate_private_explanation(
            ticker, market_outlook, portfolio_fit, action_permission, private_question)

    return ComposedDecision(
        ticker=ticker, action=action, market_outlook=market_outlook, portfolio_fit=portfolio_fit,
        action_permission=action_permission, actionable_recommendation=actionable_recommendation,
        explanation=explanation, composed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def _generate_private_explanation(ticker: str, market_outlook: MarketOutlook, portfolio_fit: PortfolioFit,
                                  action_permission: ActionPermission, private_question: str | None) -> str:
    """
    The only place in this module an LLM may be called — private, optional,
    explicitly opt-in, and explanatory only: it narrates the three sections
    already computed above and NEVER changes them (they're already frozen on
    the ComposedDecision by the time this runs). private_question may be
    folded into the prompt here — this is a PRIVATE call, never shared
    generation, so that's safe; it is not forwarded anywhere else.

    Not implemented yet in this phase — composition and its structured
    output are the deliverable; wiring a real LLM call here (and choosing
    which model/failover chain) is left to whichever caller first needs it.
    Raising rather than silently returning a fabricated-looking string keeps
    "explanation is optional" honest: a caller that doesn't ask for one
    never hits this at all (allow_llm_explanation defaults to False).
    """
    raise NotImplementedError(
        "private explanation generation is not wired to a model in this phase — "
        "call compose_decision with allow_llm_explanation=False (the default), "
        "or provide your own explanation step downstream of ComposedDecision."
    )
