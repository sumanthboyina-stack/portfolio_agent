#!/usr/bin/env python3
"""
Quarterly universe refresh — fetch S&P 500/400/600 and full Nasdaq listed tickers,
write them to the local SQLite universe_tickers table.

Run once per quarter (or when you want to add new index members to coverage):
  python scripts/refresh_universe.py

Example cron (first Monday of each quarter, 07:00 CST):
  0 7 1-7 1,4,7,10 1  /path/to/.venv/bin/python /path/to/scripts/refresh_universe.py

After running, the morning pipeline's universe screen phase (Phase 5) will find
tickers in the DB and begin screening them automatically.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# Allow running from repo root without installing the package
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
)
_log = logging.getLogger("refresh_universe")


class _PipelineLog:
    """Thin adapter so universe.refresh_universe(log=) can log to stdlib."""
    def info(self, msg: str, **_):
        _log.info(msg.strip())
    def warning(self, msg: str, **_):
        _log.warning(msg.strip())


def main() -> None:
    from portfolio_agent.tools.universe import refresh_universe
    from portfolio_agent.tools.universe_db import get_tickers

    print("=" * 60)
    print("  Quarterly Universe Refresh")
    print("=" * 60)

    summary = refresh_universe(log=_PipelineLog())

    extended = get_tickers(tier="extended")
    broad    = get_tickers(tier="broad")

    print("\nFetched this run:")
    if summary:
        for index_name, count in summary.items():
            print(f"  {index_name}: {count:,} tickers")
    else:
        print("  (all indexes already fresh — nothing to fetch)")

    print(f"\nUniverse totals in DB:")
    print(f"  extended : {len(extended):,} tickers")
    print(f"  broad    : {len(broad):,} tickers")
    print(f"  total    : {len(extended) + len(broad):,} tickers")
    print()


if __name__ == "__main__":
    main()
