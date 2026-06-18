"""
Daily pipeline orchestration — all --daily-* jobs.

Public entry points (called by main.py):
  run_daily(extra_tickers)
  run_daily_news(extra_tickers)
  run_daily_fundamentals_only(extra_tickers)
  run_daily_research_only(extra_tickers)
"""

from __future__ import annotations

import os as _os
import sys
from datetime import datetime as _datetime
from pathlib import Path

from portfolio_agent.log import get_logger as _get_logger
from portfolio_agent._models import GEMINI_COUNTER
from portfolio_agent.pipeline.watchlist import load_all_tickers

from portfolio_agent.pipeline.daily.fundamentals import _run_daily_fundamentals
from portfolio_agent.pipeline.daily.news import _run_news_phase
from portfolio_agent.pipeline.daily.research import _run_daily_research
from portfolio_agent.pipeline.daily.apex import _run_daily_apex
from portfolio_agent.pipeline.batch import (
    run_batch_morning,
    run_batch_intraday,
    run_batch_evening,
)

# ── Run identity (shared across all daily entry points) ───────────────────────
_PROJECT_ROOT: Path = Path(__file__).resolve().parents[3]
_RUN_ID:   str = _os.environ.get("PIPELINE_RUN_ID") or _datetime.now().strftime("%Y%m%d_%H%M%S")
_LOG_FILE: str = str(_PROJECT_ROOT / "logs" / f"{_RUN_ID}.log")


def _watchlist_path() -> Path:
    return _PROJECT_ROOT / "config" / "watchlist.yaml"


def _portfolio_path() -> Path:
    return _PROJECT_ROOT / "config" / "portfolio.yaml"


# ── Standalone daily entry points ─────────────────────────────────────────────

async def run_daily_fundamentals_only(extra_tickers: list[str] | None = None) -> None:
    """Fundamentals-only daily job (--daily-fundamentals)."""
    log = _get_logger("fundamentals")
    wp = _watchlist_path()
    pp = _portfolio_path()
    if not wp.exists():
        log.error("[error] config/watchlist.yaml not found.", event_type="error")
        sys.exit(1)

    all_tickers, _, portfolio_tickers, _ = load_all_tickers(extra_tickers, wp, pp)
    always_run = set((extra_tickers or []) + portfolio_tickers)
    log.info(f"\n{'━' * 64}", event_type="separator")
    log.info(f"  Fundamentals-Only Job — {len(all_tickers)} tickers", event_type="phase_start")
    log.info(f"{'━' * 64}", event_type="separator")
    from portfolio_agent.tools.progress_tracker import PipelineProgressTracker as _Tracker
    _tracker = _Tracker(_RUN_ID, "fundamentals", _os.getpid(), _LOG_FILE)
    _tracker.set_total(len(all_tickers))
    await _run_daily_fundamentals(all_tickers, always_run, tracker=_tracker)
    _tracker.finish_run("completed")


async def run_daily_research_only(extra_tickers: list[str] | None = None) -> None:
    """Research-only daily job (--daily-research)."""
    log = _get_logger("research")
    wp = _watchlist_path()
    pp = _portfolio_path()
    if not wp.exists():
        log.error("[error] config/watchlist.yaml not found.", event_type="error")
        sys.exit(1)

    all_tickers, _, portfolio_tickers, _ = load_all_tickers(extra_tickers, wp, pp)
    always_run = set((extra_tickers or []) + portfolio_tickers)
    log.info(f"\n{'━' * 64}", event_type="separator")
    log.info(f"  Research-Only Job — {len(all_tickers)} tickers", event_type="phase_start")
    log.info(f"{'━' * 64}", event_type="separator")
    from portfolio_agent.tools.progress_tracker import PipelineProgressTracker as _Tracker
    _tracker = _Tracker(_RUN_ID, "research", _os.getpid(), _LOG_FILE)
    _tracker.set_total(len(all_tickers))
    await _run_daily_research(all_tickers, always_run, tracker=_tracker)
    _tracker.finish_run("completed")


async def run_daily_news(extra_tickers: list[str] | None = None) -> None:
    """News-only daily job (--daily-news)."""
    log = _get_logger("news")
    wp = _watchlist_path()
    pp = _portfolio_path()
    if not wp.exists():
        log.error("[error] config/watchlist.yaml not found.", event_type="error")
        sys.exit(1)

    all_tickers, watchlist, portfolio_tickers, trending = load_all_tickers(extra_tickers, wp, pp)
    from portfolio_agent.tools.progress_tracker import PipelineProgressTracker as _Tracker
    _tracker = _Tracker(_RUN_ID, "news", _os.getpid(), _LOG_FILE)
    _tracker.set_total(len(all_tickers))
    await _run_news_phase(
        all_tickers, watchlist, portfolio_tickers, trending,
        extra_tickers, wp, tracker=_tracker,
    )
    _tracker.finish_run("completed")


async def run_daily(extra_tickers: list[str] | None = None) -> None:
    """Full daily job (--daily): news → research → fundamentals → APEX."""
    log = _get_logger("daily")
    wp = _watchlist_path()
    pp = _portfolio_path()
    if not wp.exists():
        log.error("[error] config/watchlist.yaml not found.", event_type="error")
        sys.exit(1)

    all_tickers, watchlist, portfolio_tickers, trending = load_all_tickers(
        extra_tickers, wp, pp
    )
    always_run = set((extra_tickers or []) + portfolio_tickers)

    wl_count  = len(watchlist)
    port_new  = len([t for t in portfolio_tickers if t not in set(watchlist)])
    trend_new = len([t for t in trending if t not in set(watchlist)])

    log.info(f"\n{'━' * 64}", event_type="separator")
    log.info(f"  Daily Job — {len(all_tickers)} tickers total", event_type="phase_start")
    log.info(
        f"  {wl_count} watchlist  |  {port_new} portfolio-only  |  {trend_new} market-trending",
        event_type="summary",
    )

    from portfolio_agent.tools.pipeline_skip import is_all_phases_skipped as _all_skip
    _all_phase_skip = [t for t in all_tickers if _all_skip(t)]
    if _all_phase_skip:
        log.info(
            f"  [restricted/all] {len(_all_phase_skip)} tickers skip all phases (complete): "
            f"{', '.join(_all_phase_skip)}",
            event_type="restricted_skip",
        )

    log.info(f"{'━' * 64}", event_type="separator")

    from portfolio_agent.tools.progress_tracker import PipelineProgressTracker as _Tracker
    _tracker = _Tracker(_RUN_ID, "daily", _os.getpid(), _LOG_FILE)
    _tracker.set_total(len(all_tickers))

    log.info("\n── Phase 1: News ───────────────────────────────────────────────", event_type="phase_start")
    if not await _run_news_phase(
        all_tickers, watchlist, portfolio_tickers, trending,
        extra_tickers, wp, tracker=_tracker,
    ):
        log.info("\n  ⛔ News phase exhausted all models — stopping pipeline.", event_type="phase_end")
        _tracker.finish_run("error")
        return

    log.info("\n── Phase 2: Research ───────────────────────────────────────────", event_type="phase_start")
    if not await _run_daily_research(all_tickers, always_run, tracker=_tracker):
        log.info("\n  ⛔ Research phase exhausted all models — stopping pipeline.", event_type="phase_end")
        _tracker.finish_run("error")
        return

    log.info("\n── Phase 3: Fundamentals ───────────────────────────────────────", event_type="phase_start")
    if not await _run_daily_fundamentals(all_tickers, always_run, tracker=_tracker):
        log.info("\n  ⛔ Fundamentals phase exhausted all models — stopping pipeline.", event_type="phase_end")
        _tracker.finish_run("error")
        return

    log.info("\n── Phase 4: APEX Predictions ───────────────────────────────────", event_type="phase_start")
    await _run_daily_apex(portfolio_tickers, tracker=_tracker)

    GEMINI_COUNTER.print_stats(prefix=" (end of daily run)")
    _tracker.finish_run("completed")
    log.info("\nDaily pipeline complete.", event_type="phase_end")
