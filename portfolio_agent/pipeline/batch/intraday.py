"""
Intraday batch pipeline (--batch intraday).

Runs 2-3x per day (11:00, 13:00, 15:00 CST).  Two parallel tracks:

Track A — Portfolio tickers
  Check for severity-3 events in news_filter_log.  If any, generate 5d
  APEX prediction for those tickers (tagged event_material_news).

Track B — Trending tickers (new opportunity discovery)
  Scan market headlines for trending tickers.  For any that:
    a) have not been analyzed at all today (new to the system), OR
    b) scored severity-3 in today's news_filter_log
  → run news + research pipeline so they appear in the DB for manual review.
  These are NOT portfolio positions, so no APEX prediction is generated.
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

from portfolio_agent.log import get_logger as _get_logger

_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _load_portfolio_tickers(extra: list[str] | None) -> list[str]:
    """Portfolio tickers from the holdings DB, without triggering a trending scan."""
    from portfolio_agent.tools.holdings_db import get_portfolio_tickers
    tickers = list(dict.fromkeys(get_portfolio_tickers() + (extra or [])))
    return tickers


def _get_trending_tickers(limit: int = 30) -> list[str]:
    """Return market-trending tickers (pure scan, no LLM)."""
    try:
        from portfolio_agent.tools.news_sources import get_trending_tickers
        raw = json.loads(get_trending_tickers(limit))
        return [t.upper() for t in raw.get("tickers", [])]
    except Exception:
        return []


def _analyzed_today(tickers: list[str], today: str) -> set[str]:
    """Return the subset of tickers that already have a news row in news_daily_update today."""
    if not tickers:
        return set()
    from portfolio_agent.tools.db import db_conn
    placeholders = ",".join("?" * len(tickers))
    try:
        with db_conn() as c:
            rows = c.execute(
                f"""SELECT DISTINCT ticker FROM news_daily_update
                    WHERE as_of_date = ? AND ticker IN ({placeholders})
                    AND row_type = 'ticker'""",
                [today] + tickers,
            ).fetchall()
        return {r["ticker"] for r in rows}
    except Exception:
        return set()


async def run_batch_intraday(
    extra_tickers: list[str] | None = None,
) -> None:
    """
    Intraday event check + trending opportunity discovery.

    Track A: portfolio tickers with severity-3 events → APEX 5d prediction.
    Track B: new or event-triggered trending tickers → news + research.
    """
    log = _get_logger("batch.intraday")

    # ── Load tickers ──────────────────────────────────────────────────────────
    portfolio_tickers = _load_portfolio_tickers(extra_tickers)
    portfolio_set = {t.upper() for t in portfolio_tickers}

    # Load current watchlist (needed to avoid double-adding tickers)
    from portfolio_agent.tools.watchlist_db import load_watchlist_tickers
    try:
        current_watchlist: list[str] = load_watchlist_tickers()
    except Exception:
        current_watchlist = []

    log.info(f"\n{'━' * 64}", event_type="separator")
    log.info(
        f"  Intraday Batch — {len(portfolio_tickers)} portfolio | scanning trending…",
        event_type="phase_start",
    )

    trending_tickers = _get_trending_tickers(30)
    # Exclude tickers already in portfolio (they're covered by Track A)
    trending_new = [t for t in trending_tickers if t not in portfolio_set]
    log.info(f"  {len(trending_tickers)} trending tickers found, {len(trending_new)} outside portfolio", event_type="info")
    log.info(f"{'━' * 64}", event_type="separator")

    run_id = os.environ.get("PIPELINE_RUN_ID", "")
    log_file = str(_PROJECT_ROOT / "logs" / f"{run_id}.log") if run_id else ""
    from portfolio_agent.tools.progress_tracker import PipelineProgressTracker as _Tracker
    tracker = _Tracker(run_id, "intraday", os.getpid(), log_file) if run_id else None
    if tracker:
        tracker.set_total(len(portfolio_tickers) + len(trending_new))

    from portfolio_agent.config import get_event_driven_config
    from portfolio_agent.events.detector import run_all_detectors
    from portfolio_agent.events.db import get_pending_events
    from portfolio_agent.tools.prediction_db import is_trading_day
    from portfolio_agent.pipeline.daily.apex import _run_daily_apex
    from portfolio_agent.pipeline.daily.news import _run_news_phase
    from portfolio_agent.pipeline.daily.research import _run_daily_research

    today_date = date.today()
    today = today_date.isoformat()
    config = get_event_driven_config()

    if not is_trading_day(today_date):
        log.info(f"  [{today}] Not a trading day — skipping intraday check.", event_type="info")
        if tracker:
            tracker.finish_run("completed")
        return

    from portfolio_agent.pipeline.batch.morning import ensure_morning_ran_today
    await ensure_morning_ran_today(extra_tickers=extra_tickers)

    if not config.get("enabled", True):
        log.info("  event_driven disabled in config — skipping.", event_type="info")
        if tracker:
            tracker.finish_run("completed")
        return

    # ── Event detection on all tickers ───────────────────────────────────────
    all_tickers = list(dict.fromkeys(portfolio_tickers + trending_new))
    run_all_detectors(all_tickers, today, config)

    threshold = config.get("intraday_severity_threshold", 3)
    pending = get_pending_events(today, min_severity=threshold)

    event_by_ticker: dict[str, dict] = {}
    for e in pending:
        if e["ticker"] not in event_by_ticker:
            event_by_ticker[e["ticker"]] = e

    # ── Track A: Portfolio tickers with events → APEX 5d ─────────────────────
    portfolio_triggered = [t for t in portfolio_tickers if t in event_by_ticker]

    if not portfolio_triggered:
        log.info(
            f"  [Track A] No severity-{threshold} events for portfolio tickers.",
            event_type="info",
        )
    else:
        log.info(
            f"  [Track A] {len(portfolio_triggered)} portfolio ticker(s) with events: "
            f"{portfolio_triggered}",
            event_type="summary",
        )
        trigger_map = {
            t: ("event_material_news", event_by_ticker[t]["id"])
            for t in portfolio_triggered
        }
        await _run_daily_apex(
            portfolio_triggered,
            tracker=tracker,
            trigger_map=trigger_map,
            scheduled_horizons_override=[5],
        )

    # ── Track B: Trending tickers + math-screen promoted → news + research ──────
    # Include a ticker if:
    #   (a) it has a severity-3 event today (trending), OR
    #   (b) it has not been analyzed yet today (new trending discovery), OR
    #   (c) it was promoted from the morning batch math screen (universe_db signals)
    already_done = _analyzed_today(trending_new, today)
    trending_with_events = [t for t in trending_new if t in event_by_ticker]
    trending_unanalyzed  = [t for t in trending_new if t not in already_done]

    # Math-screen promoted candidates (from Phase 5 of morning batch)
    _screen_promoted: list[str] = []
    try:
        from portfolio_agent.tools.universe_db import get_signals as _get_signals
        _screen_today = [
            s["ticker"] for s in _get_signals(today, min_score=2)
            if s["ticker"] not in portfolio_set
        ]
        if _screen_today:
            _screen_done = _analyzed_today(_screen_today, today)
            _screen_promoted = [t for t in _screen_today if t not in _screen_done]
            if _screen_promoted:
                log.info(
                    f"  [Track B] +{len(_screen_promoted)} from morning math screen",
                    event_type="info",
                )
    except Exception:
        pass

    research_tickers = list(dict.fromkeys(
        trending_with_events + trending_unanalyzed + _screen_promoted
    ))

    if not research_tickers:
        log.info(
            "  [Track B] All trending tickers already analyzed today, no new events.",
            event_type="info",
        )
    else:
        log.info(
            f"  [Track B] {len(research_tickers)} trending ticker(s) need research "
            f"({len(trending_with_events)} event-triggered, "
            f"{len(trending_unanalyzed)} new today): {research_tickers}",
            event_type="summary",
        )

        # News phase for trending/event-triggered tickers. The watchlist itself is
        # manual-only (Watchlist Manager screen / Opportunity Engine "Add to
        # Watchlist") — trending tickers get same-day News/Research/Fundamentals/
        # APEX coverage here but are never auto-promoted onto the watchlist.
        await _run_news_phase(
            all_tickers=research_tickers,
            watchlist=current_watchlist,
            portfolio_tickers=[],
            extra_tickers=None,
            tracker=tracker,
        )

        # Research phase — always_run forces Track B tickers through even if fresh
        await _run_daily_research(
            all_tickers=research_tickers,
            always_run=set(research_tickers),
            tracker=tracker,
        )

        # Fundamentals phase — EDGAR check for trending tickers
        from portfolio_agent.pipeline.daily.fundamentals import _run_daily_fundamentals
        await _run_daily_fundamentals(
            all_tickers=research_tickers,
            always_run=set(research_tickers),
            tracker=tracker,
        )

        # APEX 5d predictions — opportunity discovery for trending tickers
        trending_trigger_map = {
            t: ("event_material_news", event_by_ticker[t]["id"])
            if t in event_by_ticker
            else ("trending_opportunity", None)
            for t in research_tickers
        }
        await _run_daily_apex(
            research_tickers,
            tracker=tracker,
            trigger_map=trending_trigger_map,
            scheduled_horizons_override=[5],
        )

    # ── Price History Backfill (fill any gaps from missed evening runs) ──────
    if tracker:
        tracker.start_phase("price_backfill", total=1)
    try:
        from portfolio_agent.tools.holdings_db import backfill_missing_price_snapshots
        rb = backfill_missing_price_snapshots()
        if rb.get("backfilled", 0) > 0:
            _pb_note = f"{rb['backfilled']} rows inserted for {rb['missing_days']} missing day(s)"
            log.info(f"  [Price Backfill] {_pb_note}", event_type="summary")
        else:
            _pb_note = "Nothing to backfill"
        if tracker:
            tracker.finish_phase("price_backfill", 1, 0, note=_pb_note)
    except Exception as e:
        log.warning(f"  [Price Backfill] failed — {e}", event_type="warning")
        if tracker:
            tracker.finish_phase("price_backfill", 0, 1, note=str(e)[:120])

    if tracker:
        tracker.finish_run("completed")

    log.info("\nIntraday batch complete.", event_type="phase_end")
