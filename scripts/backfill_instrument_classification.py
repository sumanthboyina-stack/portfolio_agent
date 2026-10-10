"""
One-off: classify every instrument that has no (or a stale) automatic classification — stock / ETF / ETN /
mutual fund / money market / target-date fund / bond, asset class, leverage, expense ratio, fund family.

Safe to re-run: an instrument classified within the last 30 days is skipped, a manual classification is never
touched, a ticker-less (CIT:...) fund is never looked up, and a failed lookup leaves the instrument as it was
(UNKNOWN stays UNKNOWN) so the next run retries it. Lookups happen first, outside any database transaction.

Usage:
    python -m scripts.backfill_instrument_classification --dry-run     # show what would be looked up
    python -m scripts.backfill_instrument_classification               # look up and store
    python -m scripts.backfill_instrument_classification --force       # also re-check ones classified recently
    python -m scripts.backfill_instrument_classification --stamp-predictions
        # after classifying: stamp older predictions with their ticker's type so they leave "UNCLASSIFIED" in validation
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portfolio_agent.domain import RECLASSIFY_AFTER_DAYS
from portfolio_agent.services.context import system_context
from portfolio_agent.services.instrument_classifier import classify_instruments
from portfolio_agent.tools import holdings_db as repo


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="list the instruments that would be looked up; change nothing")
    parser.add_argument("--stamp-predictions", action="store_true",
                        help="also stamp predictions that predate the instrument_type column from the instruments table")
    parser.add_argument("--force", action="store_true", help=f"re-check automatic classifications younger than {RECLASSIFY_AFTER_DAYS} days too")
    args = parser.parse_args(argv)

    def _stamp() -> None:                       # runs LAST, so it uses the types classified just above
        if args.stamp_predictions and not args.dry_run:
            from portfolio_agent.tools.prediction_db import backfill_instrument_types
            print(f"stamped instrument_type on {backfill_instrument_types()} older prediction(s)")

    todo = repo.list_instruments_needing_classification(older_than_days=0 if args.force else RECLASSIFY_AFTER_DAYS)
    print(f"{len(todo)} instrument(s) to classify" + (" (dry run)" if args.dry_run else ""))
    if args.dry_run or not todo:
        for i in todo:
            print(f"  {i['ticker']:<10} {i.get('instrument_type')}/{i.get('asset_class')}  classified_at={i.get('classified_at')}")
        _stamp()
        return 0

    results = classify_instruments(system_context("backfill_classification"), todo)
    failed = [i["ticker"] for i in todo if not results.get(i["instrument_id"], None) or not results[i["instrument_id"]].lookup_ok]
    for i in todo:
        c = results.get(i["instrument_id"])
        if c is not None and c.lookup_ok:
            lev = f" {c.leverage_factor:g}x" if c.leverage_factor != 1.0 else ""
            print(f"  {i['ticker']:<10} {c.instrument_type}/{c.asset_class}{lev}  ({c.basis})")
    if failed:
        print(f"lookup failed for {len(failed)}: {', '.join(failed)} — left unchanged, will retry next run")
    _stamp()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
