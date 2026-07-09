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

    today_date = date.today()
    if not is_trading_day(today_date):
        log.info(
            f"  [{today_date}] Not a trading day — skipping evening validation.",
            event_type="info",
        )
        return

    log.info("\n── Layer 1: Outcome Assignment ─────────────────────────────────", event_type="phase_start")
    r1 = evaluate_matured_predictions(force=True)
    log.info(
        f"  L1: {r1.get('evaluated', 0)} evaluated  "
        f"{r1.get('data_missing', 0)} data_missing  "
        f"{r1.get('errors', 0)} errors",
        event_type="summary",
    )

    log.info("\n── Layer 2: Rolling Metrics ────────────────────────────────────", event_type="phase_start")
    r2 = recompute_rolling_metrics()
    log.info(f"  L2: {r2.get('metrics_written', 0)} metric rows written", event_type="summary")

    log.info("\n── Layer 3: Portfolio Price Snapshot ───────────────────────────", event_type="phase_start")
    try:
        from portfolio_agent.tools.holdings_db import snapshot_portfolio_prices
        r3 = snapshot_portfolio_prices(today_date.isoformat())
        log.info(
            f"  L3: {r3.get('snapped', 0)} holdings snapped for {r3.get('date')}",
            event_type="summary",
        )
    except Exception as e:
        log.warning(f"  L3: Portfolio snapshot failed — {e}", event_type="warning")

    log.info("\nEvening batch complete.", event_type="phase_end")
