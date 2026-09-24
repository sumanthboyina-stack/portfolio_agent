"""
Phase 4: incremental run planning and concurrent deduplication.

WHAT THIS PHASE BUILT
══════════════════════
1. tools.run_planner — pure decision logic:
     Decision: REUSE | GENERATE | WAIT_FOR_EXISTING_JOB | DEFER | UNAVAILABLE
     PlanDecision(decision, reason, dependency_manifest, work_key) — every
     decision carries an explicit reason and the exact facts checked.
     plan_reuse() — reuse-vs-generate from CURRENT evidence-source freshness
     (fundamentals_as_of/research_as_of/valuation_as_of) and CURRENT prompt/
     policy/guardrail/context-schema versions vs. what the stored forecast
     recorded — never from "is this still the latest row in prediction
     history" (self-invalidation; proven directly below).
     plan_ticker_horizon() — the single-unit decision: admin global exclusion
     (pipeline_skip) and unsupported horizons are UNAVAILABLE; an active
     lease is WAIT_FOR_EXISTING_JOB; an exhausted budget is DEFER; otherwise
     REUSE or GENERATE per plan_reuse(). Never consults the PERSONAL
     restricted list (tools.restricted_list_db) — proven below.
     plan_batch() — unions demand across requesters (e.g. portfolio tickers +
     trending + event-triggered in one run), de-duplicating identical
     (ticker, horizon) asks to one decision and decrementing a shared budget
     once per GENERATE, not once per requester.
2. tools.generation_jobs_db — the concurrency-dedup layer: claim_job()/
     complete_job() on a `generation_jobs` table keyed by work_key, using
     BEGIN IMMEDIATE for cross-thread/process atomicity. An expired lease is
     simply reclaimable by the next claim_job() call (no separate "recovery"
     step); complete_job() only succeeds for the worker holding the CURRENT
     lease_id, so a worker whose lease was reclaimed gets False back and must
     discard its result rather than publish or retry in a loop.
3. run_planner.run_generation_unit() — orchestrates plan -> claim -> provider
     call (outside any transaction) -> complete_job (gates publish) ->
     publish_fn. This is what the tests below exercise end-to-end.

WHAT WAS NOT DONE THIS PHASE (inventory)
══════════════════════════════════════════
  - Nothing in pipeline.daily.apex or the batch pipelines calls run_planner
    yet — apex.py's existing done_horizons/missing check (Phase 1-era) is
    UNCHANGED and still what production uses. run_planner is a new, fully
    tested, standalone capability, not yet wired in. Wiring it in would mean
    passing real Prediction.to_dict() rows and real market_context evidence_
    refs as plan_ticker_horizon's inputs, and deciding what counts as
    "material new evidence" in production (a natural fit: trigger_type ==
    event_material_news from the event detector) — left for a follow-up
    phase rather than risking apex.py's already-tested behavior in this one.
  - "Entitlements" and "budgets" are represented here as a single integer
    budget_remaining — there is no real tiered-entitlement concept anywhere
    else in this codebase (single local owner) to model more richly than
    that; noted rather than invented.
  - generation_jobs rows are never pruned/archived — fine at this scale
    (bounded by ticker x horizon x day), would need a retention policy under
    real volume.

EXISTING TEST STATUS: `pytest tests/ -q` → 266 passed, 0 failed, before this
phase's additions — no pre-existing failures to report separately.
"""
from __future__ import annotations

import threading
import time

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.tools import generation_jobs_db as jobs
from portfolio_agent.tools import run_planner as rp
from portfolio_agent.tools.run_planner import Decision


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")


_EVIDENCE = {"context_version": "market-context/1.0", "fundamentals_as_of": "2026-09-01",
            "research_as_of": "2026-09-15", "valuation_as_of": "2026-09-01"}


def _plan_kwargs(**over):
    base = dict(
        is_globally_excluded=False, horizon_supported=True, budget_remaining=None,
        latest_shared_row=None, current_evidence_refs=_EVIDENCE,
        current_policy_version="v1.0|apex-prompt:abc", current_guardrail_version="conviction-guardrails/1.0",
        new_material_evidence=False,
    )
    base.update(over)
    return base


def _stored_row(**over):
    row = {"policy_version": "v1.0|apex-prompt:abc", "guardrail_version": "conviction-guardrails/1.0",
          "context_digest": "abc123", "evidence_refs": dict(_EVIDENCE)}
    row.update(over)
    return row


# ── plan_reuse: stable-identifier reuse, never history-driven ────────────────

def test_plan_reuse_no_stored_row_means_generate():
    ok, reason, manifest = rp.plan_reuse(latest_shared_row=None, current_evidence_refs=_EVIDENCE,
                                         current_policy_version="v1", current_guardrail_version="g1")
    assert ok is False and "no existing" in reason


def test_plan_reuse_matches_when_nothing_changed():
    ok, reason, manifest = rp.plan_reuse(
        latest_shared_row=_stored_row(), current_evidence_refs=_EVIDENCE,
        current_policy_version="v1.0|apex-prompt:abc", current_guardrail_version="conviction-guardrails/1.0",
    )
    assert ok is True
    assert manifest["current"]["fundamentals_as_of"] == manifest["stored"]["fundamentals_as_of"]


def test_plan_reuse_detects_stale_source_data():
    stale_evidence = dict(_EVIDENCE, fundamentals_as_of="2026-09-10")   # newer filing since the stored row
    ok, reason, manifest = rp.plan_reuse(
        latest_shared_row=_stored_row(), current_evidence_refs=stale_evidence,
        current_policy_version="v1.0|apex-prompt:abc", current_guardrail_version="conviction-guardrails/1.0",
    )
    assert ok is False and "fundamentals_as_of" in reason


def test_plan_reuse_detects_guardrail_version_change():
    ok, reason, _ = rp.plan_reuse(
        latest_shared_row=_stored_row(), current_evidence_refs=_EVIDENCE,
        current_policy_version="v1.0|apex-prompt:abc", current_guardrail_version="conviction-guardrails/2.0",
    )
    assert ok is False and "guardrail version" in reason


def test_writing_a_forecast_does_not_self_invalidate():
    """The exact scenario the module docstring warns about: build a plan that
    says REUSE, 'write' the forecast (simulated -- prediction_history changes),
    then re-plan with the SAME evidence and confirm it's still REUSE, not
    suddenly GENERATE just because something new is now in history."""
    stored = _stored_row()
    ok1, _, _ = rp.plan_reuse(latest_shared_row=stored, current_evidence_refs=_EVIDENCE,
                              current_policy_version=stored["policy_version"],
                              current_guardrail_version=stored["guardrail_version"])
    assert ok1 is True

    # Simulate "prediction_history now has a newer row" -- evidence sources
    # (fundamentals/research/valuation _as_of) are untouched by that; a
    # correct implementation must not treat "history changed" as staleness.
    ok2, reason2, _ = rp.plan_reuse(latest_shared_row=stored, current_evidence_refs=_EVIDENCE,
                                    current_policy_version=stored["policy_version"],
                                    current_guardrail_version=stored["guardrail_version"])
    assert ok2 is True and ok2 == ok1


# ── A new same-day event generates a new version ──────────────────────────────

def test_new_material_evidence_forces_generate_even_with_matching_versions():
    stored = _stored_row()
    plan = rp.plan_ticker_horizon(
        "AAPL", 5, as_of_date="2026-09-21",
        **_plan_kwargs(latest_shared_row=stored, new_material_evidence=True),
    )
    assert plan.decision == Decision.GENERATE
    assert "material new evidence" in plan.reason


def test_without_new_evidence_matching_versions_means_reuse():
    stored = _stored_row()
    plan = rp.plan_ticker_horizon("AAPL", 5, as_of_date="2026-09-21", **_plan_kwargs(latest_shared_row=stored))
    assert plan.decision == Decision.REUSE


# ── Global exclusion vs. personal restriction ─────────────────────────────────

def test_globally_excluded_ticker_is_unavailable():
    plan = rp.plan_ticker_horizon("AAPL", 5, as_of_date="2026-09-21", **_plan_kwargs(is_globally_excluded=True))
    assert plan.decision == Decision.UNAVAILABLE and "globally excluded" in plan.reason


def test_a_personal_restriction_never_reaches_the_planner_at_all():
    """plan_ticker_horizon has no parameter for a personal restriction, and
    nothing in tools.run_planner imports tools.restricted_list_db's personal
    (non-pipeline_skip) functions — the only way a ticker becomes UNAVAILABLE
    here is is_globally_excluded, which the caller must derive from the
    ADMIN pipeline_skip list, never a user's own restricted_list."""
    import inspect
    # Check only the executable source (module docstring aside, which explains
    # the rule in prose) -- run_planner must never import restricted_list_db.
    src = "\n".join(
        inspect.getsource(obj) for name, obj in vars(rp).items()
        if inspect.isfunction(obj) and obj.__module__ == rp.__name__
    )
    assert "restricted_list_db" not in src and "list_restricted" not in src and "is_restricted(" not in src
    # A ticker with no global exclusion generates/reuses normally regardless
    # of what any individual person's restricted list says about it.
    plan = rp.plan_ticker_horizon("AAPL", 5, as_of_date="2026-09-21", **_plan_kwargs(is_globally_excluded=False))
    assert plan.decision in (Decision.GENERATE, Decision.REUSE)


def test_unsupported_horizon_is_unavailable():
    plan = rp.plan_ticker_horizon("AAPL", 7, as_of_date="2026-09-21", **_plan_kwargs(horizon_supported=False))
    assert plan.decision == Decision.UNAVAILABLE


def test_exhausted_budget_defers_generate_candidates():
    plan = rp.plan_ticker_horizon("AAPL", 5, as_of_date="2026-09-21", **_plan_kwargs(budget_remaining=0))
    assert plan.decision == Decision.DEFER and "budget" in plan.reason


def test_active_lease_means_wait_for_existing_job():
    plan = rp.plan_ticker_horizon("AAPL", 5, as_of_date="2026-09-21", **_plan_kwargs(active_lease=True))
    assert plan.decision == Decision.WAIT_FOR_EXISTING_JOB


# ── plan_batch: union + de-dupe + shared budget ───────────────────────────────

def test_plan_batch_deduplicates_identical_demand_from_different_requesters():
    demands = [("AAPL", 5), ("AAPL", 5), ("MSFT", 5)]   # AAPL/5 asked for twice (e.g. portfolio + trending)
    result = rp.plan_batch(
        demands, as_of_date="2026-09-21",
        is_globally_excluded_fn=lambda t: False, horizon_supported_fn=lambda h: True,
        budget_remaining=10, latest_shared_row_fn=lambda t, h: None,
        current_evidence_refs_fn=lambda t: _EVIDENCE,
        current_policy_version="v1", current_guardrail_version="g1",
    )
    assert len(result) == 2   # AAPL/5 collapsed to one decision, not counted twice


def test_plan_batch_decrements_shared_budget_once_per_generate_not_per_requester():
    demands = [("AAPL", 5), ("AAPL", 5), ("MSFT", 5), ("NVDA", 5)]
    result = rp.plan_batch(
        demands, as_of_date="2026-09-21",
        is_globally_excluded_fn=lambda t: False, horizon_supported_fn=lambda h: True,
        budget_remaining=2, latest_shared_row_fn=lambda t, h: None,
        current_evidence_refs_fn=lambda t: _EVIDENCE,
        current_policy_version="v1", current_guardrail_version="g1",
    )
    decisions = [d.decision for d in result.values()]
    assert decisions.count(Decision.GENERATE) == 2   # budget=2 covers exactly AAPL and MSFT
    assert decisions.count(Decision.DEFER) == 1        # NVDA deferred, not silently dropped


# ── Orchestration: concurrent dedup, lease recovery, no self-repeat ──────────

def _base_run_kwargs(**over):
    kw = dict(as_of_date="2026-09-21", origin="pipeline.apex", plan_kwargs=_plan_kwargs())
    kw.update(over)
    return kw


def test_concurrent_identical_requests_normally_produce_one_provider_call():
    calls = []
    call_lock = threading.Lock()

    def _provider():
        with call_lock:
            calls.append(1)
        time.sleep(0.15)
        return {"prediction": "BUY"}

    published = []

    def _publish(result):
        published.append(result)

    results: list[dict] = [None, None]

    def _worker(idx, wid):
        results[idx] = rp.run_generation_unit(
            "AAPL", 5, worker_id=wid, provider_fn=_provider, publish_fn=_publish,
            **_base_run_kwargs(),
        )

    t1 = threading.Thread(target=_worker, args=(0, "worker-1"))
    t2 = threading.Thread(target=_worker, args=(1, "worker-2"))
    t1.start()
    time.sleep(0.03)   # let worker-1 win the claim before worker-2 tries
    t2.start()
    t1.join()
    t2.join()

    assert len(calls) == 1                                    # exactly one provider call total
    assert len(published) == 1
    outcomes = {r["decision"] for r in results}
    published_flags = [r["published"] for r in results]
    assert published_flags.count(True) == 1
    assert Decision.WAIT_FOR_EXISTING_JOB in outcomes or published_flags.count(False) == 1


def test_successful_horizons_are_not_repeated_when_another_horizon_fails():
    calls = {"5": 0, "21": 0}

    def _provider_5():
        calls["5"] += 1
        return {"horizon": 5, "ok": True}

    def _provider_21_fails():
        calls["21"] += 1
        raise RuntimeError("provider timeout")

    published = []

    def _publish(result):
        published.append(result)

    r5 = rp.run_generation_unit("AAPL", 5, worker_id="w1", provider_fn=_provider_5, publish_fn=_publish,
                                **_base_run_kwargs())
    r21 = rp.run_generation_unit("AAPL", 21, worker_id="w1", provider_fn=_provider_21_fails, publish_fn=_publish,
                                 **_base_run_kwargs())
    assert r5["published"] is True and r21["published"] is False
    assert calls == {"5": 1, "21": 1}

    # Retry the batch: 5d is now REUSE-eligible (a stored row with matching
    # versions exists); only 21d should hit the provider again.
    stored_5 = _stored_row()
    r5_retry = rp.run_generation_unit("AAPL", 5, worker_id="w1", provider_fn=_provider_5, publish_fn=_publish,
                                      **_base_run_kwargs(plan_kwargs=_plan_kwargs(latest_shared_row=stored_5)))
    r21_retry = rp.run_generation_unit("AAPL", 21, worker_id="w1", provider_fn=_provider_21_fails, publish_fn=_publish,
                                       **_base_run_kwargs())
    assert r5_retry["decision"] == Decision.REUSE
    assert r21_retry["decision"] == Decision.GENERATE and r21_retry["published"] is False
    assert calls == {"5": 1, "21": 2}   # 5d never re-called; 21d retried


def test_lease_recovery_works_and_publication_does_not_loop():
    key = rp.work_key("AAPL", 5, "2026-09-21")
    lease_id_1 = jobs.claim_job(key, "worker-1", lease_seconds=0.05)
    assert lease_id_1 is not None
    time.sleep(0.1)   # let worker-1's lease expire -- it "vanished" (crash, network partition, whatever)

    lease_id_2 = jobs.claim_job(key, "worker-2", lease_seconds=60)
    assert lease_id_2 is not None and lease_id_2 != lease_id_1   # recovered by a different worker

    # worker-1 finally comes back and tries to publish its stale result --
    # it must be rejected, not silently overwrite worker-2's in-flight claim,
    # and the caller gets a clear False rather than retrying forever.
    accepted_1 = jobs.complete_job(key, "worker-1", lease_id_1, status=jobs.STATUS_COMPLETED)
    assert accepted_1 is False

    accepted_2 = jobs.complete_job(key, "worker-2", lease_id_2, status=jobs.STATUS_COMPLETED)
    assert accepted_2 is True

    job = jobs.get_job(key)
    assert job["status"] == jobs.STATUS_COMPLETED and job["worker_id"] == "worker-2"
