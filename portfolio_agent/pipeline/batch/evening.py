"""
Evening batch pipeline (--batch evening).

Runs validation (Layer 1 + Layer 2) and optionally refreshes slow-moving data.
Does NOT generate new predictions.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from portfolio_agent.log import get_logger as _get_logger

_PROJECT_ROOT = Path(__file__).resolve().parents[3]


async def run_batch_evening(
    extra_tickers: list[str] | None = None,
) -> None:
    """Evening batch: validate matured predictions + recompute rolling metrics."""
    log = _get_logger("batch.evening")

    log.info(f"\n{'━' * 64}", event_type="separator")
    log.info("  Evening Batch — validation + slow-data refresh", event_type="phase_start")
    log.info(f"{'━' * 64}", event_type="separator")

    from portfolio_agent.tools.validation_engine import (
        evaluate_matured_predictions,
        recompute_rolling_metrics,
    )
    from portfolio_agent.tools.prediction_db import is_trading_day
    from datetime import date

    run_id = os.environ.get("PIPELINE_RUN_ID", "")
    log_file = str(_PROJECT_ROOT / "logs" / f"{run_id}.log") if run_id else ""
    from portfolio_agent.tools.progress_tracker import PipelineProgressTracker as _Tracker
    tracker = _Tracker(run_id, "evening", os.getpid(), log_file) if run_id else None

    today_date = date.today()
    if not is_trading_day(today_date):
        log.info(
            f"  [{today_date}] Not a trading day — skipping evening validation.",
            event_type="info",
        )
        if tracker:
            tracker.finish_run("completed")
        return

    log.info("\n── Layer 1: Outcome Assignment ─────────────────────────────────", event_type="phase_start")
    if tracker:
        tracker.start_phase("l1_outcome", total=1)
    r1 = evaluate_matured_predictions(force=True)
    _l1_note = (
        f"{r1.get('evaluated', 0)} evaluated  "
        f"{r1.get('data_missing', 0)} data_missing  "
        f"{r1.get('errors', 0)} errors"
    )
    log.info(f"  L1: {_l1_note}", event_type="summary")
    if tracker:
        tracker.finish_phase("l1_outcome", 1, 0, note=_l1_note)

    log.info("\n── Layer 2: Rolling Metrics ────────────────────────────────────", event_type="phase_start")
    if tracker:
        tracker.start_phase("l2_metrics", total=1)
    r2 = recompute_rolling_metrics()
    _l2_note = f"{r2.get('metrics_written', 0)} metric rows written"
    log.info(f"  L2: {_l2_note}", event_type="summary")
    if tracker:
        tracker.finish_phase("l2_metrics", 1, 0, note=_l2_note)

    log.info("\n── Layer 3: Portfolio Price Snapshot ───────────────────────────", event_type="phase_start")
    if tracker:
        tracker.start_phase("l3_snapshot", total=1)
    try:
        from portfolio_agent.tools.holdings_db import snapshot_portfolio_prices
        r3 = snapshot_portfolio_prices(today_date.isoformat())
        _l3_note = f"{r3.get('snapped', 0)} holdings snapped for {r3.get('date')}"
        log.info(f"  L3: {_l3_note}", event_type="summary")
        if tracker:
            tracker.finish_phase("l3_snapshot", 1, 0, note=_l3_note)
    except Exception as e:
        log.warning(f"  L3: Portfolio snapshot failed — {e}", event_type="warning")
        if tracker:
            tracker.finish_phase("l3_snapshot", 0, 1, note=str(e)[:120])

    log.info("\n── Layer 4: Screener Outcome Scoring ───────────────────────────", event_type="phase_start")
    if tracker:
        tracker.start_phase("l4_screener_outcomes", total=1)
    try:
        from portfolio_agent.tools.screener import evaluate_screener_outcomes
        r4 = evaluate_screener_outcomes(log=log)
        _l4_note = (
            f"{r4.get('evaluated', 0)} evaluated  "
            f"{r4.get('data_missing', 0)} data_missing  "
            f"{r4.get('errors', 0)} errors"
        )
        log.info(f"  L4: {_l4_note}", event_type="summary")
        if tracker:
            tracker.finish_phase("l4_screener_outcomes", 1, 0, note=_l4_note)
    except Exception as e:
        log.warning(f"  L4: Screener outcome scoring failed — {e}", event_type="warning")
        if tracker:
            tracker.finish_phase("l4_screener_outcomes", 0, 1, note=str(e)[:120])

    if tracker:
        tracker.finish_run("completed")
    log.info("\nEvening batch complete.", event_type="phase_end")
