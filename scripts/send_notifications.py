"""
Deliver the notifications that are due (the outbox -> email / phone push). Safe to run at any time and concurrently: rows are
claimed atomically, sent outside any database transaction, and retried with backoff.

Usage:
    python -m scripts.send_notifications              # queue clock-driven alerts (digests, expiring approvals), deliver what is due
    python -m scripts.send_notifications --limit 200  # at most 200 rows this run
    python -m scripts.send_notifications --status     # outbox counts by status; sends nothing

Schedule it every few minutes (see run_notifications.sh and the README). Whether anything is really sent is a global setting
(Profile -> Investor settings, Notifications): with dry_run on, it only logs what it WOULD send.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv()                                   # SMTP / ntfy secrets may live in .env (env vars only — never in the database)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--limit", type=int, default=50, help="most rows to deliver in this run (default 50)")
    parser.add_argument("--status", action="store_true", help="print outbox counts by status and exit")
    args = parser.parse_args(argv)

    if args.status:
        from portfolio_agent.tools import notification_outbox_db as obx
        print(obx.counts_by_status())
        return 0
    from portfolio_agent.services.notification_producers import run_scheduled_producers
    from portfolio_agent.services.notification_worker import run_once
    produced = run_scheduled_producers()                 # clock-driven alerts first: expiring approvals, daily digests
    summary = run_once(limit=args.limit)
    print({"produced": produced, **{k: v for k, v in summary.items() if v or k == "claimed"}})
    return 1 if summary.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
