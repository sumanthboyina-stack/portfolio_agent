"""
Morning batch pipeline (--batch morning).

Flow:
  1. News phase  — triage headlines for all watchlist+portfolio tickers
  2. Research phase — refresh broker consensus
  3. Fundamentals phase — EDGAR check
  4. Event detection — read news_filter_log for severity-3 events
  5. APEX predictions — only for tickers/horizons that pass should_generate_prediction()
     or have severity-threshold events
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from portfolio_agent.log import get_logger as _get_logger
from portfolio_agent.pipeline.watchlist import load_all_tickers

_PROJECT_ROOT = Path(__file__).resolve().parents[3]

# Top-N quant-screener candidates (by conviction) added to the daily
# 'trending_opportunity' APEX run alongside news-trending tickers, so the
# Opportunity Engine has a real fundamentals+research+macro+news synthesis
# behind screener-discovered names too, not just a momentum signal. Each
# addition costs one more per-ticker dual-run APEX call (~2 LLM calls).
SCREENER_OPPORTUNITY_CAP = 10


async def ensure_morning_ran_today(extra_tickers: list[str] | None = None) -> bool:
    """Run the Morning batch first if it hasn't completed yet today.

    Intraday and Evening both depend on data Morning produces same-day
    (universe screen signals, fresh predictions). Called at the top of each
    so a missed/failed Morning run (cron hiccup, laptop asleep) self-heals
    instead of silently operating on stale or absent data.

    Returns True if Morning was run here.
    """
    from portfolio_agent.tools.progress_tracker import has_completed_today
    if has_completed_today("morning"):
        return False

    log = _get_logger("batch.morning")
    log.info(
        "  Morning batch hasn't completed today — running it first…",
        event_type="info",
    )

    # Run under its own run_id so it tracks as a separate "morning" row in
    # pipeline_runs instead of colliding with (and overwriting) the caller's
    # own in-progress run_id/job_type.
    from datetime import datetime as _dt
    _orig_run_id = os.environ.get("PIPELINE_RUN_ID", "")
    os.environ["PIPELINE_RUN_ID"] = f"{_dt.now():%Y%m%d_%H%M%S}_morning"
    try:
        await run_batch_morning(extra_tickers=extra_tickers)
    finally:
        if _orig_run_id:
            os.environ["PIPELINE_RUN_ID"] = _orig_run_id
        else:
            os.environ.pop("PIPELINE_RUN_ID", None)
    return True


async def run_batch_morning(
    extra_tickers: list[str] | None = None,
    force_all: bool = False,
) -> None:
    """
    Morning batch: news → research → fundamentals → event-detection → APEX.

    force_all=True bypasses the event-driven cadence and generates predictions
    for every portfolio ticker using the legacy get_scheduled_horizons() logic.
    """
    log = _get_logger("batch.morning")

    all_tickers, watchlist, portfolio_tickers, trending = load_all_tickers(extra_tickers)
    always_run = set((extra_tickers or []) + portfolio_tickers)

    run_id = os.environ.get("PIPELINE_RUN_ID", "")
    log_file = str(_PROJECT_ROOT / "logs" / f"{run_id}.log") if run_id else ""

    log.info(f"\n{'━' * 64}", event_type="separator")
    log.info(
        f"  Morning Batch — {len(all_tickers)} tickers | force_all={force_all}",
        event_type="phase_start",
    )
    log.info(f"{'━' * 64}", event_type="separator")

    from portfolio_agent.tools.progress_tracker import PipelineProgressTracker as _Tracker
    tracker = _Tracker(run_id, "morning", os.getpid(), log_file) if run_id else None
    if tracker:
        tracker.set_total(len(all_tickers))

    from portfolio_agent.pipeline.daily.news import _run_news_phase
    from portfolio_agent.pipeline.daily.research import _run_daily_research
    from portfolio_agent.pipeline.daily.fundamentals import _run_daily_fundamentals
    from portfolio_agent.pipeline.daily.apex import _run_daily_apex
    from portfolio_agent._models import GEMINI_COUNTER

    log.info("\n── Phase 1: News ───────────────────────────────────────────────", event_type="phase_start")
    if not await _run_news_phase(
        all_tickers, watchlist, portfolio_tickers,
        extra_tickers, tracker=tracker,
    ):
        log.info("\n  ⛔ News phase exhausted all models — stopping.", event_type="phase_end")
        if tracker:
            tracker.finish_run("error")
        return

    # Short interest refresh — cheap no-op when settlement date hasn't changed;
    # fires one paginated FINRA pull (~twice per month) when new data is due.
    log.info("\n── Short Interest Check ────────────────────────────────────────", event_type="phase_start")
    if tracker:
        tracker.start_phase("short_interest", total=1)
    try:
        from portfolio_agent.tools.finra import maybe_refresh_short_interest
        maybe_refresh_short_interest(all_tickers, log=log)
        if tracker:
            tracker.finish_phase("short_interest", 1, 0)
    except Exception as _si_exc:
        log.warning(
            f"  [short_interest] Refresh skipped — {_si_exc}",
            event_type="warning",
        )
        if tracker:
            tracker.finish_phase("short_interest", 0, 1, note=str(_si_exc)[:120])

    log.info("\n── Phase 2: Research ───────────────────────────────────────────", event_type="phase_start")
    if not await _run_daily_research(all_tickers, always_run, tracker=tracker):
        log.info("\n  ⛔ Research phase exhausted all models — stopping.", event_type="phase_end")
        if tracker:
            tracker.finish_run("error")
        return

    log.info("\n── Phase 3: Fundamentals ───────────────────────────────────────", event_type="phase_start")
    if not await _run_daily_fundamentals(all_tickers, always_run, tracker=tracker):
        log.info("\n  ⛔ Fundamentals phase exhausted all models — stopping.", event_type="phase_end")
        if tracker:
            tracker.finish_run("error")
        return

    log.info("\n── Phase 4: APEX Predictions ───────────────────────────────────", event_type="phase_start")

    # restricted_list.yaml's pipeline_skip "phases: [all]" only ever covered
    # {news, research, fundamentals} in pipeline_skip.py — APEX and the
    # Risk/Technical phase never checked it, so tickers marked "removed from
    # daily run" (e.g. GRFXY, RIVN) kept getting full daily analysis anyway.
    from portfolio_agent.tools.pipeline_skip import is_all_phases_skipped
    _apex_skip = [t for t in portfolio_tickers if is_all_phases_skipped(t)]
    apex_tickers = [t for t in portfolio_tickers if t not in _apex_skip]
    if _apex_skip:
        log.info(
            f"  [restricted/apex] skipping {len(_apex_skip)}: {', '.join(sorted(_apex_skip))}",
            event_type="restricted_skip",
        )

    if force_all:
        # Legacy behaviour: all portfolio tickers + trending, all scheduled horizons
        log.info("  [force-all] Bypassing event-driven cadence.", event_type="info")
        all_apex = list(dict.fromkeys(apex_tickers + trending))
        await _run_daily_apex(all_apex, tracker=tracker)
    else:
        await _run_morning_apex_event_driven(
            apex_tickers, all_tickers, trending, tracker, log,
        )

    GEMINI_COUNTER.print_stats(prefix=" (end of morning batch)")

    # Valuation — deliberately runs AFTER APEX, not before: it needs to see
    # today's trigger_type='trending_opportunity' predictions (screener/news
    # candidates APEX just decided on above) in addition to the static
    # watchlist+portfolio list, or Opportunity Engine candidates would never
    # get a DCF (see _run_daily_valuation's own ticker-list merge).
    log.info("\n── Phase 4.2: Valuation (DCF + relative, no LLM) ────────────────", event_type="phase_start")
    try:
        from portfolio_agent.pipeline.daily.valuation import _run_daily_valuation
        await _run_daily_valuation(all_tickers, tracker=tracker)
    except Exception as _val_exc:
        log.warning(f"  [valuation] Phase skipped — {_val_exc}", event_type="warning")

    # Risk + Technical flags — decoupled from APEX/weight engine (Approach B).
    # Portfolio holdings only; writes to risk_flags, feeds the dashboard's
    # Attention Queue / risk strip directly.
    log.info("\n── Phase 4.5: Risk & Technical Flags ────────────────────────────", event_type="phase_start")
    try:
        from portfolio_agent.pipeline.daily.risk_technical import _run_daily_risk_technical
        await _run_daily_risk_technical(apex_tickers, tracker=tracker)
    except Exception as _rt_exc:
        log.warning(f"  [risk_technical] Phase skipped — {_rt_exc}", event_type="warning")

    # Universe screen — pure math (no LLM). Quarterly refresh of index membership
    # fires automatically when due; daily screen finds breakout candidates in
    # the extended/broad tiers and writes them to screening_signals so the
    # intraday Track B can triage them with news+research later in the day.
    log.info("\n── Phase 5: Universe Screen (math only) ────────────────────────", event_type="phase_start")
    if tracker:
        tracker.start_phase("universe_screen", total=1)
    try:
        from portfolio_agent.tools.universe_db import needs_refresh as _universe_needs_refresh
        from portfolio_agent.tools.screener import screen_universe as _screen_universe
        from portfolio_agent.tools.universe import refresh_universe as _refresh_universe

        # Quarterly refresh of index membership (cheap no-op when fresh)
        if _universe_needs_refresh("sp500") or _universe_needs_refresh("nasdaq_listed"):
            log.info("  [universe] Quarterly refresh due — fetching S&P + Nasdaq constituents…", event_type="fetch_start")
            _refresh_universe(log=log)

        core_set = set(all_tickers)

        promoted_ext = _screen_universe(tier="extended", exclude=core_set, log=log)
        promoted_broad = _screen_universe(
            tier="broad", exclude=core_set | set(promoted_ext), log=log
        )
        total = len(promoted_ext) + len(promoted_broad)
        if total:
            _univ_note = (
                f"{total} candidate(s) flagged "
                f"({len(promoted_ext)} extended, {len(promoted_broad)} broad)"
            )
            log.info(
                f"  [universe] {_univ_note} — intraday checks will triage these.",
                event_type="summary",
            )
        else:
            _univ_note = "No signals today"
            log.info(f"  [universe] {_univ_note}.", event_type="info")
        if tracker:
            tracker.finish_phase("universe_screen", 1, 0, note=_univ_note)
    except Exception as _univ_exc:
        log.warning(f"  [universe] Screen skipped — {_univ_exc}", event_type="warning")
        if tracker:
            tracker.finish_phase("universe_screen", 0, 1, note=str(_univ_exc)[:120])

    log.info("\n── Phase 5.5: Score Calibration Snapshot ────────────────────────", event_type="phase_start")
    try:
        from portfolio_agent.pipeline.daily.scoring_snapshot import _run_daily_scoring_snapshot
        await _run_daily_scoring_snapshot(tracker=tracker)
    except Exception as _snap_exc:
        log.warning(f"  [scoring_snapshot] Phase skipped — {_snap_exc}", event_type="warning")

    log.info("\n── Price History Backfill ──────────────────────────────────────", event_type="phase_start")
    if tracker:
        tracker.start_phase("price_backfill", total=1)
    try:
        from portfolio_agent.tools.holdings_db import backfill_missing_price_snapshots
        rb = backfill_missing_price_snapshots()
        if rb.get("backfilled", 0) > 0:
            _pb_note = f"Backfilled {rb['backfilled']} price rows across {rb['missing_days']} missing day(s)"
            log.info(f"  {_pb_note}", event_type="summary")
        else:
            _pb_note = "Price history up to date — nothing to backfill"
            log.info(f"  {_pb_note}.", event_type="info")
        if tracker:
            tracker.finish_phase("price_backfill", 1, 0, note=_pb_note)
    except Exception as e:
        log.warning(f"  Price backfill failed — {e}", event_type="warning")
        if tracker:
            tracker.finish_phase("price_backfill", 0, 1, note=str(e)[:120])

    if tracker:
        tracker.finish_run("completed")
    log.info("\nMorning batch complete.", event_type="phase_end")


async def _run_morning_apex_event_driven(
    portfolio_tickers: list[str],
    all_tickers: list[str],
    trending_tickers: list[str],
    tracker,
    log,
) -> None:
    """Determine which tickers/horizons to predict today and run APEX."""
    from datetime import date as _date

    from portfolio_agent.config import get_event_driven_config
    from portfolio_agent.events.detector import run_all_detectors
    from portfolio_agent.events.db import get_pending_events
    from portfolio_agent.events.scheduler import (
        get_scheduled_horizons_v2,
        should_generate_prediction,
        get_trigger_type_for_scheduled,
    )
    from portfolio_agent.tools.prediction_db import is_trading_day
    from portfolio_agent.pipeline.daily.apex import _run_daily_apex

    today_date = _date.today()
    today = today_date.isoformat()
    config = get_event_driven_config()

    if not is_trading_day(today_date):
        log.info(f"  [{today}] Not a trading day — skipping APEX.", event_type="info")
        if tracker:
            tracker.start_phase("apex", total=0)
            tracker.finish_phase("apex", 0, 0, note="not a trading day")
        return

    # Detect events (writes to trigger_events table)
    log.info("  Detecting events from news_filter_log…", event_type="info")
    run_all_detectors(all_tickers, today, config)

    # Read back pending events for portfolio tickers
    threshold = config.get("intraday_severity_threshold", 3)
    pending = get_pending_events(today, min_severity=threshold)
    event_tickers: set[str] = {e["ticker"] for e in pending}
    event_id_map: dict[str, int] = {e["ticker"]: e["id"] for e in pending}

    if event_tickers:
        log.info(
            f"  Events (severity≥{threshold}): {sorted(event_tickers)}",
            event_type="info",
        )

    # Determine scheduled horizons for today
    scheduled_horizons = get_scheduled_horizons_v2(today_date, config)
    log.info(f"  Scheduled horizons (cadence): {scheduled_horizons}", event_type="info")

    # Build per-ticker work: {ticker → (horizons_needed, trigger_type, event_id)}
    all_possible = [5, 21, 63, 250]
    trigger_map: dict[str, tuple[str, int | None]] = {}
    tickers_to_run: list[str] = []
    needed_horizons: set[int] = set()

    for ticker in dict.fromkeys(portfolio_tickers):  # deduplicate, preserve order
        ticker_horizons: list[int] = []
        ticker_trigger: str | None = None
        ticker_ev_id: int | None = None

        for h in all_possible:
            should, tt = should_generate_prediction(
                ticker, h, today_date, config, event_tickers
            )
            if should:
                ticker_horizons.append(h)
                if ticker_trigger is None:
                    ticker_trigger = tt
                    if "event" in (tt or ""):
                        ticker_ev_id = event_id_map.get(ticker)

        if ticker_horizons:
            tickers_to_run.append(ticker)
            needed_horizons.update(ticker_horizons)
            trigger_map[ticker] = (ticker_trigger, ticker_ev_id)

    # Trending tickers — opportunity discovery on scheduled days
    if scheduled_horizons:
        existing_run_set = set(tickers_to_run)
        portfolio_set = set(portfolio_tickers)
        trending_to_add = [
            t for t in dict.fromkeys(trending_tickers)
            if t not in portfolio_set and t not in existing_run_set
        ]

        # Top quant-screener candidates (by conviction) join the same
        # opportunity-discovery run — see SCREENER_OPPORTUNITY_CAP above.
        screener_to_add: list[str] = []
        try:
            from portfolio_agent.tools.universe_db import (
                get_latest_signal_date, get_signals, get_conviction_data, rank_by_conviction,
            )
            screen_date = get_latest_signal_date()
            if screen_date:
                exclude = set(all_tickers) | existing_run_set | set(trending_to_add)
                sig_signals = get_signals(screen_date, min_score=2)
                sig_conviction = get_conviction_data(screen_date, lookback_days=30, min_score=2)
                ranked = rank_by_conviction(sig_signals, sig_conviction, exclude=exclude)
                screener_to_add = [s["ticker"] for s in ranked[:SCREENER_OPPORTUNITY_CAP]]
        except Exception as _screen_exc:
            log.warning(
                f"  [warn] Could not pull screener candidates for opportunity discovery: {_screen_exc}",
                event_type="warning",
            )

        trending_to_add = trending_to_add + screener_to_add
        for ticker in trending_to_add:
            tickers_to_run.append(ticker)
            needed_horizons.update(scheduled_horizons)
            trigger_map[ticker] = ("trending_opportunity", None)
        if trending_to_add:
            log.info(
                f"  {len(trending_to_add)} trending/screener ticker(s) added for opportunity discovery "
                f"({len(trending_to_add) - len(screener_to_add)} news-trending, {len(screener_to_add)} screener)",
                event_type="info",
            )

    if not tickers_to_run:
        log.info(
            "  No predictions needed today (no scheduled horizons + no events).",
            event_type="info",
        )
        if tracker:
            tracker.start_phase("apex", total=0)
            tracker.finish_phase("apex", 0, 0, note="no predictions needed today")
        return

    effective_horizons = sorted(needed_horizons)
    log.info(
        f"  {len(tickers_to_run)} tickers to predict | horizons={effective_horizons}",
        event_type="summary",
    )

    await _run_daily_apex(
        tickers_to_run,
        tracker=tracker,
        trigger_map=trigger_map,
        scheduled_horizons_override=effective_horizons,
    )
