"""
Intraday batch pipeline (--batch intraday).

Runs 2-3x per day (11:00, 13:00, 15:00 CST).  Two parallel tracks:

Track A — Portfolio and watchlist tickers
  Check for severity-3 events in news_filter_log.  If any, generate 5d
  APEX prediction for those tickers (tagged event_material_news).

Track B — Opportunity discovery (capped; trending headlines alone never trigger it)
  A non-portfolio ticker enters the news/research/fundamentals/APEX pipeline only when
    a) it has an unforecast severity-3 event today (every intraday run), OR
    b) the morning math screen promoted it (score >= track_b_min_screen_score) and it
       has not been examined today (first intraday run of the day only).
  Trending symbols must exist in the loaded universe (when one is loaded).  At most
  track_b_max_tickers run per pass, ranked by event severity then screen score.
  These are NOT portfolio positions; they get a 5d forecast only.
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


def _examined_today(tickers: list[str], today: str) -> set[str]:
    """Tickers already looked at today: a news row OR a news-filter-log row (a ticker the filter dropped
    leaves no news row, and must not look new again at the next run)."""
    done = _analyzed_today(tickers, today)
    rest = [t for t in tickers if t not in done]
    if not rest:
        return done
    from portfolio_agent.tools.db import db_conn
    try:
        with db_conn() as c:
            rows = c.execute(
                f"SELECT DISTINCT ticker FROM news_filter_log WHERE as_of_date = ? "
                f"AND ticker IN ({','.join('?' * len(rest))})", [today] + rest).fetchall()
        done |= {r["ticker"] for r in rows}
    except Exception:
        pass
    return done


def _known_universe() -> set[str]:
    """The loaded universe symbols (empty when none is loaded, which disables the symbol check)."""
    try:
        from portfolio_agent.tools.universe_db import get_tickers
        return {t.upper() for t in get_tickers()}
    except Exception:
        return set()


def select_track_b(
    trending_new: list[str],
    event_by_ticker: dict[str, dict],
    screen_scores: dict[str, float],
    examined: set[str],
    forecast_done: set[str],
    *,
    discovery_allowed: bool,
    cap: int,
    min_screen_score: float,
    known_universe: set[str],
) -> dict:
    """Choose the Track B tickers (pure: no I/O).  Returns {"tickers": ranked, capped list, "dropped_for_cap": [...]}.

    Event-triggered trending tickers are eligible on every run unless THIS event already produced a forecast.
    Screen-promoted tickers are eligible only on the day's first run and only if not examined today.
    A trending symbol outside a loaded universe is ignored; a trending ticker with neither signal is never run."""
    trending = [t for t in trending_new if not known_universe or t in known_universe]
    ranked: dict[str, tuple] = {}
    for t in trending:
        ev = event_by_ticker.get(t)
        if ev is not None and t not in forecast_done:
            ranked[t] = (0, -int(ev.get("severity") or 0), -float(screen_scores.get(t, 0)))
    if discovery_allowed:
        for t, score in screen_scores.items():
            if t not in ranked and score >= min_screen_score and t not in examined:
                ranked[t] = (1, 0, -float(score))
    order = sorted(ranked, key=lambda t: (ranked[t], t))
    cap = max(0, int(cap))
    return {"tickers": order[:cap], "dropped_for_cap": order[cap:]}


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

    # Held and watchlist tickers are forecast on an event (Track A); everything else is Track B discovery.
    watchlist_only = {t.upper() for t in current_watchlist if t.upper() not in portfolio_set}
    forecast_pool = list(dict.fromkeys(portfolio_tickers + sorted(watchlist_only)))
    forecast_set = set(forecast_pool)

    log.info(f"\n{'━' * 64}", event_type="separator")
    log.info(
        f"  Intraday Batch — {len(portfolio_tickers)} portfolio | scanning trending…",
        event_type="phase_start",
    )

    trending_tickers = _get_trending_tickers(30)
    # Exclude tickers already in portfolio (they're covered by Track A)
    trending_new = [t for t in trending_tickers if t not in forecast_set]
    log.info(f"  {len(trending_tickers)} trending tickers found, {len(trending_new)} outside portfolio and watchlist", event_type="info")
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
    all_tickers = list(dict.fromkeys(forecast_pool + trending_new))
    run_all_detectors(all_tickers, today, config)

    from portfolio_agent.events.scheduler import select_event_tickers
    threshold = config.get("intraday_severity_threshold", 3)
    wl_threshold = config.get("watchlist_severity_threshold", 3)
    pending = get_pending_events(today, min_severity=min(threshold, wl_threshold))
    event_by_ticker: dict[str, dict] = select_event_tickers(pending, watchlist_only, threshold, wl_threshold)

    # ── Track A: Portfolio tickers with events → APEX 5d ─────────────────────
    portfolio_triggered = [t for t in forecast_pool if t in event_by_ticker]

    if not portfolio_triggered:
        log.info(
            f"  [Track A] No severity-{threshold} events for portfolio or watchlist tickers.",
            event_type="info",
        )
    else:
        log.info(
            f"  [Track A] {len(portfolio_triggered)} portfolio/watchlist ticker(s) with events: "
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

    # ── Track B: event-triggered trending + math-screen promoted → news + research ──
    from portfolio_agent.tools.progress_tracker import has_completed_today
    from portfolio_agent.tools.prediction_db import SHARED_SCOPES, event_already_processed

    _screen_scores: dict[str, float] = {}
    try:
        from portfolio_agent.tools.universe_db import get_signals as _get_signals
        _screen_scores = {s["ticker"]: float(s["score"] or 0) for s in _get_signals(today, min_score=0)
                          if s["ticker"] not in forecast_set}
    except Exception:
        pass
    _candidates = list(dict.fromkeys(trending_new + list(_screen_scores)))
    _forecast_done = {t for t in trending_new if t in event_by_ticker
                      and event_already_processed(t, event_by_ticker[t]["id"], today, scopes=SHARED_SCOPES)}
    _discovery_allowed = not has_completed_today("intraday")   # discovery once a day; events every run
    picked = select_track_b(
        trending_new, event_by_ticker, _screen_scores, _examined_today(_candidates, today), _forecast_done,
        discovery_allowed=_discovery_allowed, cap=config.get("track_b_max_tickers", 5),
        min_screen_score=config.get("track_b_min_screen_score", 2), known_universe=_known_universe(),
    )
    research_tickers = picked["tickers"]
    trending_with_events = [t for t in research_tickers if t in event_by_ticker]
    trending_unanalyzed = [t for t in research_tickers if t not in event_by_ticker]
    if picked["dropped_for_cap"]:
        log.info(f"  [Track B] cap {config.get('track_b_max_tickers', 5)} reached — deferred "
                 f"{picked['dropped_for_cap']}", event_type="info")

    if not research_tickers:
        log.info(
            "  [Track B] All trending tickers already analyzed today, no new events.",
            event_type="info",
        )
    else:
        log.info(
            f"  [Track B] {len(research_tickers)} trending ticker(s) need research "
            f"({len(trending_with_events)} event-triggered, "
            f"{len(trending_unanalyzed)} screen-promoted): {research_tickers}",
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
            always_run=set(trending_with_events),
            tracker=tracker,
        )

        # Fundamentals phase — EDGAR check for trending tickers
        from portfolio_agent.pipeline.daily.fundamentals import _run_daily_fundamentals
        await _run_daily_fundamentals(
            all_tickers=research_tickers,
            always_run=set(trending_with_events),
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
        from portfolio_agent.tools.holdings_prices import backfill_missing_price_snapshots
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
