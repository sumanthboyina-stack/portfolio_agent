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

    # Short interest refresh — cheap no-op when settlement date hasn't changed;
    # fires one paginated FINRA pull (~twice per month) when new data is due.
    log.info("\n── Short Interest Check ────────────────────────────────────────", event_type="phase_start")
    try:
        from portfolio_agent.tools.finra import maybe_refresh_short_interest
        maybe_refresh_short_interest(all_tickers, log=log)
    except Exception as _si_exc:
        log.warning(
            f"  [short_interest] Refresh skipped — {_si_exc}",
            event_type="warning",
        )

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
            log.info(
                f"  [universe] {total} candidate(s) flagged "
                f"({len(promoted_ext)} extended, {len(promoted_broad)} broad) "
                f"— intraday checks will triage these.",
                event_type="summary",
            )
        else:
            log.info("  [universe] No signals today.", event_type="info")
    except Exception as _univ_exc:
        log.warning(f"  [universe] Screen skipped — {_univ_exc}", event_type="warning")

    log.info("\n── Price History Backfill ──────────────────────────────────────", event_type="phase_start")
    try:
        from portfolio_agent.tools.holdings_db import backfill_missing_price_snapshots
        rb = backfill_missing_price_snapshots()
        if rb.get("backfilled", 0) > 0:
            log.info(
                f"  Backfilled {rb['backfilled']} price rows across "
                f"{rb['missing_days']} missing day(s)",
                event_type="summary",
            )
        else:
            log.info("  Price history up to date — nothing to backfill.", event_type="info")
    except Exception as e:
        log.warning(f"  Price backfill failed — {e}", event_type="warning")

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
        for ticker in trending_to_add:
            tickers_to_run.append(ticker)
            needed_horizons.update(scheduled_horizons)
            trigger_map[ticker] = ("trending_opportunity", None)
        if trending_to_add:
            log.info(
                f"  {len(trending_to_add)} trending ticker(s) added for opportunity discovery",
                event_type="info",
            )

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
