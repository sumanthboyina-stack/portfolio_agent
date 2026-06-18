"""
Intraday batch pipeline (--batch intraday).

Checks for severity-3 events on portfolio tickers since the morning run.
If any are found, generates 5d predictions for those tickers only,
tagged with trigger_type='event_material_news'.

This runs 2-3 times per day (11:00, 13:00, 15:00 CST) and is intentionally
cheap — it only predicts when genuinely new material news exists.
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

from portfolio_agent.log import get_logger as _get_logger

_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _load_portfolio_tickers(pp: Path, extra: list[str] | None) -> list[str]:
    """Read portfolio tickers directly from portfolio.yaml — no trending scan."""
    import yaml
    try:
        data = yaml.safe_load(pp.read_text()) or {}
    except Exception:
        data = {}
    holdings = data.get("holdings", [])
    tickers = list(dict.fromkeys(
        [h["ticker"].upper() for h in holdings if h.get("ticker")]
        + (extra or [])
    ))
    return tickers


async def run_batch_intraday(
    extra_tickers: list[str] | None = None,
) -> None:
    """Intraday event check: detect severity-3 events, predict only if triggered."""
    log = _get_logger("batch.intraday")
    pp = _PROJECT_ROOT / "config" / "portfolio.yaml"

    if not pp.exists():
        log.error("[error] config/portfolio.yaml not found.", event_type="error")
        sys.exit(1)

    portfolio_tickers = _load_portfolio_tickers(pp, extra_tickers)
    all_tickers = portfolio_tickers  # intraday checks portfolio only

    log.info(f"\n{'━' * 64}", event_type="separator")
    log.info(
        f"  Intraday Batch — checking {len(portfolio_tickers)} portfolio tickers for events",
        event_type="phase_start",
    )
    log.info(f"{'━' * 64}", event_type="separator")

    from portfolio_agent.config import get_event_driven_config
    from portfolio_agent.events.detector import run_all_detectors
    from portfolio_agent.events.db import get_pending_events
    from portfolio_agent.tools.prediction_db import is_trading_day
    from portfolio_agent.pipeline.daily.apex import _run_daily_apex

    today_date = date.today()
    today = today_date.isoformat()
    config = get_event_driven_config()

    if not is_trading_day(today_date):
        log.info(f"  [{today}] Not a trading day — skipping intraday check.", event_type="info")
        return

    if not config.get("enabled", True):
        log.info("  event_driven disabled in config — skipping.", event_type="info")
        return

    # Detect events (idempotent — insert_event skips duplicates from morning run)
    run_all_detectors(all_tickers, today, config)

    threshold = config.get("intraday_severity_threshold", 3)
    pending = get_pending_events(today, min_severity=threshold)

    # Filter to portfolio tickers only
    portfolio_set = {t.upper() for t in portfolio_tickers}
    triggered = [e for e in pending if e["ticker"] in portfolio_set]

    if not triggered:
        log.info(
            f"  No severity-{threshold} events for portfolio tickers — nothing to predict.",
            event_type="info",
        )
        return

    triggered_tickers = list(dict.fromkeys(e["ticker"] for e in triggered))
    event_id_map = {e["ticker"]: e["id"] for e in triggered}

    log.info(
        f"  {len(triggered_tickers)} portfolio tickers with intraday events: "
        f"{triggered_tickers}",
        event_type="summary",
    )

    trigger_map = {t: ("event_material_news", event_id_map[t]) for t in triggered_tickers}

    await _run_daily_apex(
        triggered_tickers,
        tracker=None,
        trigger_map=trigger_map,
        scheduled_horizons_override=[5],  # intraday always 5d only
    )

    log.info("\nIntraday batch complete.", event_type="phase_end")
