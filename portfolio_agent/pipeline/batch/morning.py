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
import sys
from datetime import date
from pathlib import Path

from portfolio_agent.log import get_logger as _get_logger
from portfolio_agent.pipeline.watchlist import load_all_tickers

_PROJECT_ROOT = Path(__file__).resolve().parents[3]


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
    wp = _PROJECT_ROOT / "config" / "watchlist.yaml"
    pp = _PROJECT_ROOT / "config" / "portfolio.yaml"

    if not wp.exists():
        log.error("[error] config/watchlist.yaml not found.", event_type="error")
        sys.exit(1)

    all_tickers, watchlist, portfolio_tickers, trending = load_all_tickers(
        extra_tickers, wp, pp
    )
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
        all_tickers, watchlist, portfolio_tickers, trending,
        extra_tickers, wp, tracker=tracker,
    ):
        log.info("\n  ⛔ News phase exhausted all models — stopping.", event_type="phase_end")
        if tracker:
            tracker.finish_run("error")
        return

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

    if force_all:
        # Legacy behaviour: all portfolio tickers, all scheduled horizons
        log.info("  [force-all] Bypassing event-driven cadence.", event_type="info")
        await _run_daily_apex(portfolio_tickers, tracker=tracker)
    else:
        await _run_morning_apex_event_driven(
            portfolio_tickers, all_tickers, tracker, log,
        )

    GEMINI_COUNTER.print_stats(prefix=" (end of morning batch)")
    if tracker:
        tracker.finish_run("completed")
    log.info("\nMorning batch complete.", event_type="phase_end")


async def _run_morning_apex_event_driven(
    portfolio_tickers: list[str],
    all_tickers: list[str],
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

    if not tickers_to_run:
        log.info(
            "  No predictions needed today (no scheduled horizons + no events).",
            event_type="info",
        )
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
