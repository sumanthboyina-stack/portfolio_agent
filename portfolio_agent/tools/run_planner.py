"""
Incremental run planning for shared (market-only) forecast generation.

Every unit of demand is one (ticker, horizon_days) for one as_of_date. Two
separate questions are answered, in order:

  1. REUSE vs. GENERATE — plan_reuse(): does the latest SHARED prediction row
     already satisfy this demand, or is fresh generation needed? Decided from
     STABLE identifiers only — the CURRENT freshness of each evidence source
     (fundamentals_as_of, research_as_of, valuation_as_of) and the CURRENT
     prompt/policy/guardrail/context-schema versions, compared against what
     the stored row recorded when it was built. This NEVER looks at "is this
     still the latest row in prediction history" — that would make writing a
     forecast immediately invalidate itself the instant anything else gets
     appended to history (self-invalidation; see the test file for a direct
     proof this doesn't happen).
  2. if generation is needed, CLAIM vs. WAIT — tools.generation_jobs_db: is
     another worker already generating this exact work key right now?
     Concurrent dedup, not reuse — a claim only prevents two simultaneous
     provider calls for the identical unit of work.

Five decisions, always with an explicit reason and dependency manifest:
  REUSE                  an existing row satisfies this demand as-is
  GENERATE               this worker should call the provider itself
  WAIT_FOR_EXISTING_JOB  another worker holds an active lease on this work
  DEFER                  eligible in principle, not right now (budget/cadence)
  UNAVAILABLE            not eligible at all (globally excluded / unsupported)

Global exclusions only. A PERSONAL restricted-list entry (tools.
restricted_list_db.list_restricted*, Phase 2 — a compliance blocklist for one
person) is NEVER consulted here: this plans SHARED, market-only generation
for everyone, and one person's restriction must not suppress another
person's research. Only pipeline_skip (admin, global, deliberately unowned —
see restricted_list_db's module docstring) can make a ticker UNAVAILABLE.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Decision(str, Enum):
    REUSE = "REUSE"
    GENERATE = "GENERATE"
    WAIT_FOR_EXISTING_JOB = "WAIT_FOR_EXISTING_JOB"
    DEFER = "DEFER"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class PlanDecision:
    decision: Decision
    reason: str
    dependency_manifest: dict = field(default_factory=dict)
    work_key: str = ""


def work_key(ticker: str, horizon_days: int, as_of_date: str, *, origin: str = "pipeline.apex") -> str:
    """Instrument + horizon + forecast origin/session + as_of_date — the unit
    two concurrent callers must agree is "the same work" to dedupe correctly."""
    return f"{origin}|{ticker.upper()}|{horizon_days}|{as_of_date}"


_VERSION_FIELDS = ("context_version", "fundamentals_as_of", "research_as_of", "valuation_as_of", "technical_as_of")


def plan_reuse(*, latest_shared_row: dict | None, current_evidence_refs: dict,
               current_policy_version: str, current_guardrail_version: str,
               new_material_evidence: bool = False) -> tuple[bool, str, dict]:
    """
    Returns (reuse_ok, reason, manifest). Pure — no DB, no network, no clock
    read beyond what's already in the arguments.
    """
    current = {
        "policy_version": current_policy_version,
        "guardrail_version": current_guardrail_version,
        **{f: current_evidence_refs.get(f) for f in _VERSION_FIELDS},
    }
    manifest = {"current": current}

    if latest_shared_row is None:
        return False, "no existing shared forecast", manifest

    stored_evidence = latest_shared_row.get("evidence_refs") or {}
    stored = {
        "policy_version": latest_shared_row.get("policy_version"),
        "guardrail_version": latest_shared_row.get("guardrail_version"),
        **{f: stored_evidence.get(f) for f in _VERSION_FIELDS},
    }
    manifest["stored"] = stored
    manifest["stored_context_digest"] = latest_shared_row.get("context_digest")

    if new_material_evidence:
        return False, "material new evidence since the stored forecast", manifest

    for key, label in (
        ("policy_version", "prompt/policy version"),
        ("guardrail_version", "guardrail version"),
        ("context_version", "market-context schema version"),
    ):
        if current[key] != stored[key]:
            return False, f"{label} changed ({stored[key]!r} -> {current[key]!r})", manifest

    for src in ("fundamentals_as_of", "research_as_of", "valuation_as_of", "technical_as_of"):
        if current[src] != stored[src]:
            return False, f"{src} source data changed ({stored[src]!r} -> {current[src]!r})", manifest

    return True, "stored forecast's versions and source freshness are unchanged", manifest


def plan_ticker_horizon(
    ticker: str, horizon_days: int, *, as_of_date: str, origin: str = "pipeline.apex",
    is_globally_excluded: bool, horizon_supported: bool, budget_remaining: int | None,
    latest_shared_row: dict | None, current_evidence_refs: dict,
    current_policy_version: str, current_guardrail_version: str,
    new_material_evidence: bool = False, active_lease: bool = False,
) -> PlanDecision:
    """
    The single-unit top-level decision, combining eligibility (global
    exclusion, supported horizon, budget) with the reuse check above and the
    caller's own concurrency check (active_lease — see generation_jobs_db;
    this function does not call the DB itself so it stays a pure decision
    that's easy to test with fixed inputs).
    """
    key = work_key(ticker, horizon_days, as_of_date, origin=origin)

    if is_globally_excluded:
        return PlanDecision(Decision.UNAVAILABLE, "ticker is globally excluded (admin pipeline_skip)",
                            {"ticker": ticker, "horizon_days": horizon_days}, key)
    if not horizon_supported:
        return PlanDecision(Decision.UNAVAILABLE, f"horizon {horizon_days} is not a supported horizon",
                            {"ticker": ticker, "horizon_days": horizon_days}, key)

    reuse_ok, reuse_reason, manifest = plan_reuse(
        latest_shared_row=latest_shared_row, current_evidence_refs=current_evidence_refs,
        current_policy_version=current_policy_version, current_guardrail_version=current_guardrail_version,
        new_material_evidence=new_material_evidence,
    )
    manifest = {"ticker": ticker, "horizon_days": horizon_days, "as_of_date": as_of_date, **manifest}
    if reuse_ok:
        return PlanDecision(Decision.REUSE, reuse_reason, manifest, key)

    if active_lease:
        return PlanDecision(Decision.WAIT_FOR_EXISTING_JOB, "another worker holds an active lease on this work",
                            manifest, key)
    if budget_remaining is not None and budget_remaining <= 0:
        return PlanDecision(Decision.DEFER, "daily generation budget exhausted", manifest, key)

    return PlanDecision(Decision.GENERATE, reuse_reason, manifest, key)


def plan_batch(demands: list[tuple[str, int]], *, as_of_date: str, origin: str = "pipeline.apex",
              is_globally_excluded_fn, horizon_supported_fn, budget_remaining: int | None,
              latest_shared_row_fn, current_evidence_refs_fn,
              current_policy_version: str, current_guardrail_version: str,
              new_material_evidence_fn=lambda ticker, horizon_days: False,
              active_lease_fn=lambda key: False) -> dict[tuple[str, int], PlanDecision]:
    """
    Plan the UNION of eligible (ticker, horizon) demand from possibly several
    requesters (e.g. portfolio tickers + trending discovery + event-triggered
    tickers in one batch run) — duplicate (ticker, horizon) pairs across
    requesters collapse to exactly one decision each, and one GENERATE
    decrements the shared budget_remaining exactly once, never once per
    requester that happened to ask for the same pair.
    """
    seen: dict[tuple[str, int], PlanDecision] = {}
    remaining = budget_remaining
    for ticker, horizon_days in dict.fromkeys(demands):   # de-dupe while preserving first-seen order
        pair = (ticker.upper(), horizon_days)
        if pair in seen:
            continue
        key = work_key(pair[0], horizon_days, as_of_date, origin=origin)
        decision = plan_ticker_horizon(
            pair[0], horizon_days, as_of_date=as_of_date, origin=origin,
            is_globally_excluded=is_globally_excluded_fn(pair[0]),
            horizon_supported=horizon_supported_fn(horizon_days),
            budget_remaining=remaining,
            latest_shared_row=latest_shared_row_fn(pair[0], horizon_days),
            current_evidence_refs=current_evidence_refs_fn(pair[0]),
            current_policy_version=current_policy_version,
            current_guardrail_version=current_guardrail_version,
            new_material_evidence=new_material_evidence_fn(pair[0], horizon_days),
            active_lease=active_lease_fn(key),
        )
        if decision.decision == Decision.GENERATE and remaining is not None:
            remaining -= 1
        seen[pair] = decision
    return seen


# ── Orchestration: plan + claim + provider call + publish ────────────────────
#
# The only impure function in this module — everything above is a pure
# decision given already-known facts. This is what actually calls
# generation_jobs_db (concurrency) and the caller's provider_fn/publish_fn.

def run_generation_unit(ticker: str, horizon_days: int, *, as_of_date: str, origin: str,
                        worker_id: str, provider_fn, publish_fn, plan_kwargs: dict,
                        lease_seconds: float = None) -> dict:
    """
    provider_fn: zero-arg callable performing the actual (network/billed)
    generation, called AT MOST ONCE per successful claim and always OUTSIDE
    any DB transaction this module holds.
    publish_fn: zero-or-one-arg callable (receives provider_fn's result) that
    writes the forecast — called ONLY after this worker's lease is confirmed
    still current, so a worker whose lease was reclaimed by someone else
    never publishes (no double-write for one work key).
    plan_kwargs: everything plan_ticker_horizon needs besides ticker/horizon/
    as_of_date/origin/active_lease (which this function determines itself).
    """
    from portfolio_agent.tools import generation_jobs_db as jobs

    kwargs = dict(lease_seconds=lease_seconds) if lease_seconds is not None else {}
    key = work_key(ticker, horizon_days, as_of_date, origin=origin)
    existing = jobs.get_job(key)
    active_lease = bool(existing and existing["status"] == jobs.STATUS_CLAIMED
                        and existing["leased_until"] > jobs._now_iso())

    plan = plan_ticker_horizon(ticker, horizon_days, as_of_date=as_of_date, origin=origin,
                               active_lease=active_lease, **plan_kwargs)

    if plan.decision != Decision.GENERATE:
        return {"decision": plan.decision, "reason": plan.reason, "manifest": plan.dependency_manifest,
               "published": False}

    lease_id = jobs.claim_job(key, worker_id, **kwargs)
    if lease_id is None:
        return {"decision": Decision.WAIT_FOR_EXISTING_JOB, "reason": "lost the claim race to another worker",
               "manifest": plan.dependency_manifest, "published": False}

    try:
        result = provider_fn()   # network/billed call — outside any transaction
    except Exception as exc:
        jobs.complete_job(key, worker_id, lease_id, status=jobs.STATUS_FAILED)
        return {"decision": Decision.GENERATE, "published": False, "error": str(exc),
               "manifest": plan.dependency_manifest}

    published = jobs.complete_job(key, worker_id, lease_id, status=jobs.STATUS_COMPLETED)
    if not published:
        return {"decision": Decision.GENERATE, "published": False,
               "reason": "lease expired before publish — result discarded, not written",
               "manifest": plan.dependency_manifest}

    publish_fn(result)
    return {"decision": Decision.GENERATE, "published": True, "result": result,
           "manifest": plan.dependency_manifest}
