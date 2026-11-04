"""
Show or change which models APEX predicts with (engine_settings), without touching the database by hand.

Usage:
    python -m scripts.apex_models                                   # show the current plan and whether each provider key is set
    python -m scripts.apex_models --pool GPT-5 DeepSeek-Chat        # random pool: each ticker draws ONE of these per day
    python -m scripts.apex_models --champion GPT-5                  # back to one fixed champion
    python -m scripts.apex_models --fallbacks o3 GPT-4o             # the ordered fallback list (tried when the pick fails)
    python -m scripts.apex_models --challenger DeepSeek-Chat --challenger-pct 20      # shadow-run DeepSeek on 20% of tickers
    python -m scripts.apex_models --challenger none                 # challenger off

A pool needs at least two different models. The draw is a hash of (ticker, date): random across tickers, the same for one ticker all day.
Model labels: Claude-Sonnet, GPT-4o, GPT-5, o3, DeepSeek-Chat. Keys live in .env (OPENAI_API_KEY, DEEPSEEK_API_KEY, ANTHROPIC_API_KEY).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv()

_KEY_FOR = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "deepseek": "DEEPSEEK_API_KEY"}


def _show() -> None:
    from portfolio_agent.tools import apex_dual_run as adr
    from portfolio_agent.tools.engine_settings_db import get_engine_settings
    s = get_engine_settings()
    plan = adr.load_model_plan()
    print(f"Plan:       {plan.describe()}")
    print(f"Mode:       {s['apex_selection_mode']}")
    print(f"Champion:   {s['apex_champion_model']}   (used when mode is 'champion')")
    print(f"Pool:       {', '.join(s['apex_random_pool'])}   (used when mode is 'random_pool')")
    print(f"Fallbacks:  {', '.join(s['apex_fallback_models']) or 'none'}")
    print(f"Challenger: {s['apex_challenger_model'] or 'off'}" + (f" on {s['apex_challenger_sample_pct']:g}% of tickers" if s['apex_challenger_model'] else ""))
    print("Provider keys:")
    for mid, prov, label in adr.APEX_MODELS:
        print(f"  {label:14s} {prov:10s} {_KEY_FOR.get(prov, '?'):18s} {'set' if os.environ.get(_KEY_FOR.get(prov, '')) else 'MISSING'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pool", nargs="+", metavar="MODEL", help="random pool (>= 2 models); also switches the mode to random_pool")
    parser.add_argument("--champion", metavar="MODEL", help="one fixed champion; also switches the mode to champion")
    parser.add_argument("--fallbacks", nargs="*", metavar="MODEL", help="ordered fallback models (empty list clears it)")
    parser.add_argument("--challenger", metavar="MODEL", help="shadow-run model, or 'none' to turn it off")
    parser.add_argument("--challenger-pct", type=float, metavar="PCT", help="% of tickers that also get a challenger run (0-100)")
    args = parser.parse_args(argv)
    if args.pool and args.champion:
        parser.error("choose either --pool or --champion, not both")

    changes: dict = {}
    if args.pool:
        changes.update(apex_random_pool=args.pool, apex_selection_mode="random_pool")
    if args.champion:
        changes.update(apex_champion_model=args.champion, apex_selection_mode="champion")
    if args.fallbacks is not None:
        changes["apex_fallback_models"] = args.fallbacks
    if args.challenger is not None:
        changes["apex_challenger_model"] = None if args.challenger.lower() in ("none", "off", "") else args.challenger
    if args.challenger_pct is not None:
        changes["apex_challenger_sample_pct"] = args.challenger_pct
    if changes:
        from portfolio_agent.tools.engine_settings_db import update_engine_settings
        try:
            update_engine_settings(updated_by="scripts.apex_models", **changes)
        except ValueError as exc:
            print(f"Not changed: {exc}", file=sys.stderr)
            return 2
        print("Updated.\n")
    _show()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
