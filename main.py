"""
Portfolio Analysis CLI

Usage examples:
  python main.py AAPL                        # full pipeline
  python main.py AAPL MSFT NVDA              # multiple tickers, full pipeline each
  python main.py --watchlist                 # run all tickers in the watchlist
  python main.py AAPL --agent fundamentals   # single specialist
  python main.py AAPL --agent technical      # single specialist
  python main.py AAPL --agent synthesis      # synthesis only (needs prior state or empty)
  python main.py AAPL --json                 # dump final state as JSON
  python main.py --daily                     # full daily job: EDGAR check + fundamentals + news
  python main.py --daily-news                # news triage + news agent only

Available --agent values:
  fundamentals | news | macro | technical | research | risk | synthesis | clearance | all (default)
"""

import argparse
import asyncio
import sys

from dotenv import load_dotenv

load_dotenv()

from portfolio_agent.log import get_logger as _get_logger
from portfolio_agent.pipeline.runner import run_analysis  # noqa: F401 (used by _main)
from portfolio_agent.pipeline.daily import (
    run_daily               as _run_daily,
    run_daily_news          as _run_daily_news,
    run_daily_fundamentals_only as _run_daily_fundamentals_only,
    run_daily_research_only as _run_daily_research_only,
    run_batch_morning       as _run_batch_morning,
    run_batch_intraday      as _run_batch_intraday,
    run_batch_evening       as _run_batch_evening,
)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="main.py",
        description="Portfolio Analysis Agent — powered by Google ADK",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "tickers",
        nargs="*",
        metavar="TICKER",
        help="One or more ticker symbols to analyze (e.g. AAPL MSFT).",
    )
    p.add_argument(
        "--watchlist",
        action="store_true",
        help="Analyze all tickers in the watchlist.",
    )
    p.add_argument(
        "--agent",
        default="all",
        metavar="NAME",
        help=(
            "Which agent to run: fundamentals | news | macro | technical | "
            "research | risk | synthesis | clearance | all  (default: all)"
        ),
    )
    p.add_argument(
        "--json",
        dest="output_json",
        action="store_true",
        help="Print final session state as JSON instead of formatted output.",
    )
    p.add_argument(
        "--daily",
        action="store_true",
        help=(
            "Full daily job: (1) check EDGAR for new 10-K/10-Q filings, run fundamentals "
            "agent + upsert DB for any ticker with a new filing or data older than 95 days; "
            "(2) run news triage + news agent for tickers with material headlines. "
            "Combine with explicit tickers: 'AAPL TSLA --daily'."
        ),
    )
    p.add_argument(
        "--daily-news",
        action="store_true",
        help=(
            "News-only daily job: triage + news agent for all watchlist tickers. "
            "Does NOT run fundamentals. Combine with explicit tickers: 'AAPL TSLA --daily-news'."
        ),
    )
    p.add_argument(
        "--daily-fundamentals",
        action="store_true",
        help="Fundamentals-only daily job: EDGAR check + fundamentals agent for all watchlist tickers.",
    )
    p.add_argument(
        "--daily-research",
        action="store_true",
        help="Research-only daily job: raw broker fetch + LLM summary for all watchlist tickers.",
    )
    p.add_argument(
        "--batch",
        choices=["morning", "intraday", "evening"],
        metavar="BATCH",
        help=(
            "Run a named batch job: morning (news+research+fundamentals+event-driven APEX), "
            "intraday (severity-3 event check + APEX if triggered), "
            "evening (validation + Score Calibration outcome-fill + slow-data refresh)."
        ),
    )
    p.add_argument(
        "--force-all",
        action="store_true",
        help=(
            "With --batch morning or --daily: bypass event-driven cadence and generate "
            "predictions for every portfolio ticker using the legacy daily schedule. "
            "Useful for testing and comparison."
        ),
    )
    p.add_argument(
        "--validate",
        action="store_true",
        help="Alias for --batch evening: evaluate matured predictions, recompute "
             "rolling metrics, fill Score Calibration outcomes, snapshot portfolio "
             "prices, and score screener outcomes.",
    )
    p.add_argument(
        "--weekly-analysis",
        action="store_true",
        help="Layer 3: weekly LLM pattern analysis of wrong predictions",
    )
    p.add_argument(
        "--query",
        default="",
        metavar="QUESTION",
        help=(
            "Specific question for the agents to answer "
            "(e.g. \"Is AAPL's debt concerning?\"). "
            "Agents pick only the tools needed to answer it. "
            "Omit for a full analysis."
        ),
    )
    return p.parse_args()


async def _main() -> None:
    log = _get_logger("main")
    args = _parse_args()

    tickers: list[str] = [t.upper() for t in args.tickers]

    if args.batch == "morning":
        await _run_batch_morning(extra_tickers=tickers or None, force_all=args.force_all)
        return

    if args.batch == "intraday":
        await _run_batch_intraday(extra_tickers=tickers or None)
        return

    if args.batch == "evening" or args.validate:
        await _run_batch_evening(extra_tickers=tickers or None)
        return

    if args.daily:
        # --daily now routes through event-driven morning batch (unless --force-all)
        await _run_batch_morning(extra_tickers=tickers or None, force_all=args.force_all)
        return

    if args.daily_news:
        await _run_daily_news(extra_tickers=tickers or None)
        return

    if args.daily_fundamentals:
        await _run_daily_fundamentals_only(extra_tickers=tickers or None)
        return

    if args.daily_research:
        await _run_daily_research_only(extra_tickers=tickers or None)
        return

    if args.weekly_analysis:
        from portfolio_agent.tools.validation_engine import weekly_pattern_analysis
        log.info("=== Layer 3: Weekly pattern analysis ===", event_type="phase_start")
        r3 = weekly_pattern_analysis()
        log.info(f"  {r3}", event_type="summary")
        log.info("Weekly analysis complete.", event_type="phase_end")
        return

    if args.watchlist:
        from portfolio_agent.tools.watchlist_db import load_watchlist_tickers
        wl_tickers = load_watchlist_tickers()
        tickers = list(dict.fromkeys(tickers + wl_tickers))

    if not tickers:
        if args.agent == "news":
            tickers = ["MARKET"]
        else:
            log.error(
                "[error] Provide at least one ticker or use --watchlist / --daily-news.",
                event_type="error",
            )
            sys.exit(1)

    for ticker in tickers:
        await run_analysis(
            ticker,
            agent_name=args.agent,
            output_json=args.output_json,
            query=args.query,
        )


if __name__ == "__main__":
    asyncio.run(_main())
