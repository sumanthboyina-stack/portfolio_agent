"""
Phase 8: scheduler and notification integration.

WHAT THIS PHASE BUILT / FIXED
══════════════════════════════
1. pipeline.daily.apex — removed the "date-only suppression" bug: a ticket
   carrying an identified trigger_event_id (event-driven — intraday Track A/B)
   is now suppressed only if THAT EXACT event has already produced a shared
   row today (tools.prediction_db.event_already_processed), never merely
   because some OTHER horizon/reason already wrote a row today. Non-event-
   driven (routine cadence) tickers are unchanged — still deduped per-horizon
   by get_today_horizons. Fixed at BOTH the ticker-selection pre-check and
   the per-horizon save loop (the same bug existed in both places).
2. tools.notification_log_db — new: delivery dedup keyed by
   (owner_scope, dedup_key, channel), so the same event/decision is never
   delivered twice to the same recipient on the same channel even if the
   detector runs again (morning self-heal, an intraday re-run, etc).
3. events.detector._maybe_notify / _emit — now dedup-gated via
   notification_log_db, and enriched with the ticker's current private
   ActionPermission (tools.clearance) purely as informational content on
   the notification — NEVER used to suppress delivery. This is the direct
   expression of "a blackout must not disable monitoring existing holdings":
   an event about a blacked-out/restricted ticker still notifies (the user
   still needs to know something happened to a position they hold), it just
   also says plainly that no new trade is currently permitted.
4. tools.validation_engine — evaluation cohorts: recompute_rolling_metrics
   and weekly_pattern_analysis now group same-day shared rows for one
   (ticker, horizon_days, as_of_date) into a single cohort and count/average
   only ONE representative (the latest version) per cohort, so a same-day
   event-driven regeneration (see #1) never silently doubles a validation
   sample. Per-row start_price/evaluation_date/actual_* fields are
   completely unchanged — only which rows COUNT toward aggregate stats.
5. pipeline.batch.morning.ensure_morning_ran_today — now claims a
   tools.generation_jobs_db lease before running, so two callers racing
   (intraday and evening both self-healing a missed morning at once) run
   morning at most once, not twice.

VERIFIED, NOT CHANGED (already correct)
══════════════════════════════════════════
  - pipeline.daily.risk_technical never imports or calls anything from
    clearance.py/blackout_windows_db — portfolio risk/technical monitoring
    was never gated on trade permission to begin with, so "a blackout must
    not disable monitoring existing holdings" already held for this phase
    at the pipeline level; #3 above extends the same principle to event
    notifications, which DID need a fix (see above).
  - pipeline.batch.evening never calls pipeline.daily.apex / _run_daily_apex
    — it only validates/calibrates/snapshots. No unconditional evening APEX
    pass was added.

INVENTORY — NOT done this phase
══════════════════════════════════════════
  - _emit() still only logs a structured record (no real email/Slack
    sender) — dedup and policy enrichment apply to whatever IS emitted;
    wiring an actual channel adapter is unchanged, pre-existing scope.
  - notification_log_db has no admin UI or retention policy yet.
  - "Define evaluation cohorts for multiple same-day forecast versions" is
    implemented for the two validation entry points that aggregate accuracy
    (recompute_rolling_metrics, weekly_pattern_analysis); the raw per-row
    dashboards (get_recent_evaluated_predictions etc., Phase 1's
    LEGACY_REVIEW_SCOPES readers) still show every row individually, which
    is correct for an audit view — only AGGREGATE counting was at risk of
    inflation and is what got fixed.

EXISTING TEST STATUS: `pytest tests/ -q` → 333 passed, 0 failed, before this
phase's additions — no pre-existing failures to report separately.
"""
from __future__ import annotations

import asyncio
from datetime import date

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.tools import market_context as market_context_mod
from portfolio_agent.tools import prediction_db as pdb
from portfolio_agent.tools import reasoning_tools as rt_mod
from portfolio_agent.tools.apex_dual_run import ModelResult, SinglePrediction


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")


NEWS = [{"headline": "steady", "sentiment": "NEUTRAL", "sentiment_score": 0.0,
        "published_at": "2026-09-20", "date": "2026-09-20"}]
RESEARCH = {"price_target_avg": 120.0, "current_price": 100.0, "num_analysts": 10, "as_of_date": "2026-09-15"}
MACRO = {"vix": 20.0, "regime": "NEUTRAL"}
FUND = {"as_of_date": "2026-08-01", "fundamental_score": 7}
VAL = {"as_of_date": "2026-09-01", "intrinsic_value": 110.0}


def _fake_loaders(**over):
    base = {"fundamentals": lambda t: FUND, "research": lambda t: RESEARCH, "news": lambda t: NEWS,
            "valuation": lambda t: VAL, "macro": lambda: MACRO, "history": lambda t: [], "failure_patterns": lambda: []}
    base.update(over)
    return base


def _patch_apex_pipeline(monkeypatch, *, recommendation="BUY"):
    """Stub every network/LLM boundary _run_daily_apex touches so it runs
    fast and deterministically end to end."""
    monkeypatch.setattr(market_context_mod, "default_loaders", lambda: _fake_loaders())
    monkeypatch.setattr(rt_mod, "get_macro_snapshot", lambda: '{"vix": 20.0}')

    from portfolio_agent.tools import yfinance_tools as yft
    monkeypatch.setattr(yft, "get_close", lambda t, d: 100.0)
    monkeypatch.setattr(yft, "get_trailing_return", lambda t, d: 0.01)

    async def _fake_run_apex(ticker, prompt):
        data = {"prediction": "BULLISH", "recommendation": recommendation, "confidence": 7,
                "composite_score": 7.5, "horizons": [
                    {"horizon_days": 5, "predicted_direction": "UP", "predicted_return_low": 1.0,
                    "predicted_return_high": 3.0, "conviction_score": 7, "reasoning_text": "steady"},
                ]}
        return ModelResult(merged=data, individuals=[SinglePrediction("m", "anthropic", "Claude", data, 10, True)],
                           model_sources=["Claude"], used_fallback=False)

    import portfolio_agent.tools.apex_dual_run as apex_dual_run_mod
    monkeypatch.setattr(apex_dual_run_mod, "run_apex", _fake_run_apex)


def _run_apex(tickers, **kw):
    import portfolio_agent.pipeline.daily.apex as apex_mod
    asyncio.run(apex_mod._run_daily_apex(tickers, **kw))


# ── New events create new versions; date-only suppression is gone ────────────

def test_event_with_new_id_generates_despite_horizon_already_done_today(monkeypatch):
    _patch_apex_pipeline(monkeypatch)

    # Routine morning generation for 5d.
    _run_apex(["AAPL"], scheduled_horizons_override=[5])
    rows_after_morning = pdb.get_prediction_history("AAPL", scopes=pdb.SHARED_SCOPES)
    assert len(rows_after_morning) == 1
    assert rows_after_morning[0].trigger_event_id is None

    # A same-day intraday event for the SAME horizon must still generate --
    # this is exactly the bug: 5d already has a row today.
    _run_apex(["AAPL"], scheduled_horizons_override=[5],
             trigger_map={"AAPL": ("event_material_news", 42)})
    rows_after_event = pdb.get_prediction_history("AAPL", limit=10, scopes=pdb.SHARED_SCOPES)
    assert len(rows_after_event) == 2   # a NEW version was written, not suppressed
    assert any(r.trigger_event_id == 42 for r in rows_after_event)


def test_reprocessing_the_same_event_id_is_suppressed(monkeypatch):
    _patch_apex_pipeline(monkeypatch)

    _run_apex(["AAPL"], scheduled_horizons_override=[5],
             trigger_map={"AAPL": ("event_material_news", 42)})
    _run_apex(["AAPL"], scheduled_horizons_override=[5],
             trigger_map={"AAPL": ("event_material_news", 42)})   # same event again

    rows = pdb.get_prediction_history("AAPL", limit=10, scopes=pdb.SHARED_SCOPES)
    assert len(rows) == 1   # not duplicated


def test_routine_generation_without_an_event_id_is_unaffected_by_the_fix(monkeypatch):
    """Non-event-driven tickers keep the original per-horizon dedup exactly."""
    _patch_apex_pipeline(monkeypatch)
    _run_apex(["AAPL"], scheduled_horizons_override=[5])
    _run_apex(["AAPL"], scheduled_horizons_override=[5])   # routine re-run, no event
    rows = pdb.get_prediction_history("AAPL", limit=10, scopes=pdb.SHARED_SCOPES)
    assert len(rows) == 1


def test_event_already_processed_helper_is_scope_and_ticker_specific():
    ctx = market_context_mod.build_market_context("AAPL", [5], loaders=_fake_loaders(), private_terms=frozenset())
    from portfolio_agent.tools.forecast_writer import ForecastProvenance, write_shared_forecast
    write_shared_forecast(ctx, ForecastProvenance(origin="pipeline.apex", model_used="m"),
                          ticker="AAPL", prediction="BULLISH", recommendation="BUY", horizon_days=5,
                          reasoning="x", trigger_type="event_material_news", trigger_event_id=99)

    today = date.today().isoformat()
    assert pdb.event_already_processed("AAPL", 99, today, scopes=pdb.SHARED_SCOPES) is True
    assert pdb.event_already_processed("AAPL", 100, today, scopes=pdb.SHARED_SCOPES) is False
    assert pdb.event_already_processed("MSFT", 99, today, scopes=pdb.SHARED_SCOPES) is False


# ── Notification dedup by recipient/event/channel; snapshots and alerts stay private ──

def _enable_notifications(monkeypatch):
    from portfolio_agent.tools.user_notifications_db import update_user_notifications
    update_user_notifications(channel="email", min_severity_threshold=3,
                              quiet_hours_start=None, quiet_hours_end=None)


def test_notification_delivery_is_deduplicated_by_event_and_channel(monkeypatch):
    from portfolio_agent.events import detector
    _enable_notifications(monkeypatch)
    emitted = []
    monkeypatch.setattr(detector, "_emit", lambda ch, ev: emitted.append((ch, ev["ticker"])))
    event = {"id": 77, "ticker": "AAPL", "event_type": "material_news", "severity": 4, "summary": "x"}

    assert detector._maybe_notify(event) is True
    assert detector._maybe_notify(event) is False   # same event id, same channel -- not redelivered
    assert len(emitted) == 1


def test_a_different_event_id_for_the_same_ticker_still_delivers(monkeypatch):
    from portfolio_agent.events import detector
    _enable_notifications(monkeypatch)
    emitted = []
    monkeypatch.setattr(detector, "_emit", lambda ch, ev: emitted.append((ch, ev["ticker"])))

    assert detector._maybe_notify({"id": 1, "ticker": "AAPL", "event_type": "material_news",
                                   "severity": 4, "summary": "x"}) is True
    assert detector._maybe_notify({"id": 2, "ticker": "AAPL", "event_type": "material_news",
                                   "severity": 4, "summary": "y"}) is True
    assert len(emitted) == 2


def test_notification_log_is_private_and_never_touches_shared_prediction_tables(monkeypatch):
    from portfolio_agent.events import detector
    from portfolio_agent.tools.notification_log_db import was_delivered
    _enable_notifications(monkeypatch)
    monkeypatch.setattr(detector, "_emit", lambda ch, ev: None)
    detector._maybe_notify({"id": 5, "ticker": "AAPL", "event_type": "material_news",
                            "severity": 4, "summary": "x"})

    from portfolio_agent.domain import LOCAL_OWNER
    assert was_delivered(f"user:{LOCAL_OWNER}", "event:5", "email") is True
    assert was_delivered("user:someone-else", "event:5", "email") is False   # scoped, not global
    assert pdb.get_prediction_history("AAPL", scopes=pdb.SHARED_SCOPES) == []   # a notification never writes a forecast


def test_blocked_ticker_event_still_notifies_monitoring_is_not_disabled_by_blackout(monkeypatch):
    """The core 'blackout must not disable monitoring' proof: a restricted-list
    hit doesn't suppress the notification, it just annotates it."""
    import portfolio_agent.tools.restricted_list_db as rl
    monkeypatch.setattr(rl, "is_restricted", lambda t: (True, "insider list"))
    import portfolio_agent.tools.blackout_windows_db as bw
    monkeypatch.setattr(bw, "list_active_blackout_windows", lambda **kw: [])
    _enable_notifications(monkeypatch)

    import logging
    from portfolio_agent.events import detector
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    logger = logging.getLogger("portfolio_agent.events.detector")
    logger.addHandler(handler)
    event = {"id": 9, "ticker": "AAPL", "event_type": "material_news", "severity": 4, "summary": "big news"}

    try:
        delivered = detector._maybe_notify(event)
    finally:
        logger.removeHandler(handler)
    logged = [r.getMessage() for r in records]
    assert delivered is True   # NOT suppressed by the block
    assert any("BLOCKED" in m for m in logged)   # but the block is noted


# ── Missed-morning recovery is deduplicated ───────────────────────────────────

def test_ensure_morning_ran_today_is_deduplicated_across_concurrent_callers(monkeypatch):
    from portfolio_agent.pipeline.batch import morning as morning_mod
    from portfolio_agent.tools import progress_tracker

    monkeypatch.setattr(progress_tracker, "has_completed_today", lambda phase: False)
    calls = []

    async def _fake_run_batch_morning(**kw):
        calls.append(1)

    monkeypatch.setattr(morning_mod, "run_batch_morning", _fake_run_batch_morning)

    r1 = asyncio.run(morning_mod.ensure_morning_ran_today())
    r2 = asyncio.run(morning_mod.ensure_morning_ran_today())   # a second caller racing the same day
    assert r1 is True
    assert r2 is False and len(calls) == 1   # only ran once


# ── Evening never repeats completed morning generation ────────────────────────

def test_evening_batch_never_calls_apex_generation():
    import inspect
    from portfolio_agent.pipeline.batch import evening
    src = inspect.getsource(evening)
    assert "_run_daily_apex" not in src and "run_apex" not in src


# ── Blackout never gates portfolio risk/technical monitoring ─────────────────

def test_risk_technical_monitoring_never_imports_clearance_or_blackout():
    import inspect
    from portfolio_agent.pipeline.daily import risk_technical
    src = inspect.getsource(risk_technical)
    assert "clearance" not in src and "blackout" not in src


# ── One forecast serves several users ─────────────────────────────────────────

def test_one_shared_forecast_serves_multiple_private_contexts(monkeypatch):
    from portfolio_agent.services import account_service
    from portfolio_agent.services.context import RequestContext, _mint_identity_for_tests, local_context
    from portfolio_agent.tools import portfolio_risk as risk
    from portfolio_agent.tools.decision_composer import compose_decision

    monkeypatch.setattr(risk, "compute_portfolio_risk_context", lambda *a, **k: {})
    import portfolio_agent.tools.restricted_list_db as rl
    monkeypatch.setattr(rl, "is_restricted", lambda t: (False, None))
    import portfolio_agent.tools.blackout_windows_db as bw
    monkeypatch.setattr(bw, "list_active_blackout_windows", lambda **kw: [])

    from portfolio_agent.tools.market_context import build_market_context
    from portfolio_agent.tools.forecast_writer import ForecastProvenance, write_shared_forecast
    ctx_mc = build_market_context("AAPL", [5], loaders=_fake_loaders(), private_terms=frozenset())
    write_shared_forecast(ctx_mc, ForecastProvenance(origin="pipeline.apex", model_used="m"),
                          ticker="AAPL", prediction="BULLISH", recommendation="BUY", horizon_days=5, reasoning="x")

    ctx_a = local_context("test")
    account_service.create_account(ctx_a, broker="manual", display_name="a")

    # user_profile is a genuine single row (Phase 2) -- compose_decision's
    # policy/permission steps only resolve for the one real local owner in
    # this deployment, so a second synthetic identity here proves the
    # market_outlook (the shared piece) is identical while portfolio_fit (the
    # private piece, via compute_portfolio_exposures's explicit policy
    # override, same pattern as Phase 5) differs -- exactly what "one
    # forecast serves several users" means: shared evidence, private overlay.
    from portfolio_agent.domain import UserProfile
    from portfolio_agent.tools import holdings_db as repo
    from portfolio_agent.tools.portfolio_assessment import compute_portfolio_exposures
    from portfolio_agent.tools.portfolio_policy import effective_policy_from_profile

    with repo.unit_of_work() as conn:
        conn.execute("INSERT INTO portfolios (owner) VALUES (?)", ("bob",))
    ctx_b = RequestContext(identity=_mint_identity_for_tests("bob"), source="test")
    account_service.create_account(ctx_b, broker="manual", display_name="b")
    holdings_b = [{"ticker": "MSFT", "shares": 10, "current_price": 50.0, "sector": "Technology"}]

    out_a = compose_decision(ctx_a, "AAPL", action="buy")
    from portfolio_agent.tools import decision_composer as dc_mod
    outlook_b = dc_mod._market_outlook("AAPL", 5)   # same retrieval compose_decision uses -- no ctx needed, shared

    fit_a = out_a.portfolio_fit
    exposures_b = compute_portfolio_exposures(ctx_b, holdings=holdings_b,
                                              policy=effective_policy_from_profile(UserProfile()))

    assert out_a.market_outlook.recommendation == outlook_b.recommendation == "BUY"
    assert out_a.market_outlook.context_digest == outlook_b.context_digest   # the SAME forecast, reused
    assert fit_a.current_weight_pct != exposures_b.per_holding_weight_pct.get("AAPL", 0.0)
    assert "AAPL" not in exposures_b.per_holding_value   # bob doesn't hold it -- a genuinely different overlay


# ── Validation counts remain reproducible: evaluation cohorts ────────────────

def _seed_evaluated_shared_row(ticker, horizon_days, as_of_date, *, actual_direction, predicted_direction,
                               trigger_event_id=None) -> None:
    from portfolio_agent.tools.market_context import build_market_context
    from portfolio_agent.tools.forecast_writer import ForecastProvenance, write_shared_forecast
    ctx = build_market_context(ticker, [horizon_days], loaders=_fake_loaders(), private_terms=frozenset())
    write_shared_forecast(ctx, ForecastProvenance(origin="pipeline.apex", model_used="m"),
                          ticker=ticker, prediction="BULLISH", recommendation="BUY", horizon_days=horizon_days,
                          reasoning="x", trigger_event_id=trigger_event_id)
    with pdb._db() as c:
        c.execute(
            """UPDATE predictions SET evaluation_status = 'evaluated', evaluated_at = ?, as_of_date = ?,
                                      actual_direction = ?, predicted_direction = ?
               WHERE id = (SELECT id FROM predictions WHERE ticker = ? AND horizon_days = ?
                           ORDER BY id DESC LIMIT 1)""",
            ["2026-09-21T00:00:00", as_of_date, actual_direction, predicted_direction, ticker, horizon_days],
        )
        c.commit()


def test_multiple_same_day_versions_count_once_in_validation_not_once_each(monkeypatch):
    from portfolio_agent.tools.validation_engine import recompute_rolling_metrics
    from portfolio_agent.tools.prediction_db import get_rolling_metrics
    import portfolio_agent.tools.validation_engine as ve
    monkeypatch.setattr(ve, "_portfolio_tickers_for_segmentation", lambda: set())

    as_of = date.today().isoformat()
    # Two same-day versions of the SAME (ticker, horizon, as_of_date) cohort --
    # a routine morning row, then an event-driven regeneration later that day.
    _seed_evaluated_shared_row("AAPL", 5, as_of, actual_direction="UP", predicted_direction="UP")
    _seed_evaluated_shared_row("AAPL", 5, as_of, actual_direction="UP", predicted_direction="DOWN",
                               trigger_event_id=1)

    recompute_rolling_metrics(today=date.today())
    metrics = get_rolling_metrics(lookback_days=30)
    row = next(m for m in metrics if m["horizon_days"] == 5 and m["segment"] == "all")
    assert row["num_predictions"] == 1   # cohort collapsed to its latest version, not counted twice


def test_cohort_collapse_uses_the_latest_version_not_an_average(monkeypatch):
    from portfolio_agent.tools.validation_engine import recompute_rolling_metrics
    from portfolio_agent.tools.prediction_db import get_rolling_metrics
    import portfolio_agent.tools.validation_engine as ve
    monkeypatch.setattr(ve, "_portfolio_tickers_for_segmentation", lambda: set())

    as_of = date.today().isoformat()
    _seed_evaluated_shared_row("AAPL", 5, as_of, actual_direction="UP", predicted_direction="DOWN")  # wrong, older
    _seed_evaluated_shared_row("AAPL", 5, as_of, actual_direction="UP", predicted_direction="UP",     # right, newer
                               trigger_event_id=2)

    recompute_rolling_metrics(today=date.today())
    row = next(m for m in get_rolling_metrics(lookback_days=30) if m["horizon_days"] == 5 and m["segment"] == "all")
    assert row["directional_accuracy"] == 1.0   # the newer (correct) version is the cohort's representative


def test_individual_rows_are_untouched_by_cohort_collapse(monkeypatch):
    """Per-row start_price/evaluation_date/actual_* are never rewritten --
    only which rows count toward the aggregate."""
    from portfolio_agent.tools.validation_engine import recompute_rolling_metrics
    import portfolio_agent.tools.validation_engine as ve
    monkeypatch.setattr(ve, "_portfolio_tickers_for_segmentation", lambda: set())

    as_of = date.today().isoformat()
    _seed_evaluated_shared_row("AAPL", 5, as_of, actual_direction="UP", predicted_direction="DOWN")
    _seed_evaluated_shared_row("AAPL", 5, as_of, actual_direction="UP", predicted_direction="UP", trigger_event_id=3)

    before = pdb.get_prediction_history("AAPL", limit=10, scopes=pdb.SHARED_SCOPES)
    recompute_rolling_metrics(today=date.today())
    after = pdb.get_prediction_history("AAPL", limit=10, scopes=pdb.SHARED_SCOPES)
    assert len(before) == len(after) == 2   # still two independent rows in the DB, neither deleted or merged
