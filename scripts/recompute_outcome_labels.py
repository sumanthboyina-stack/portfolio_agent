"""
One-off: re-derive outcome / outcome_score for already-evaluated predictions
from stored fields only (no price fetching), so matching FLAT calls become
"flat_correct" (0.8) instead of "directionally_correct" (0.7). Idempotent;
never changes a row's right/wrong status.

Usage:
    python -m scripts.recompute_outcome_labels --dry-run   # preview
    python -m scripts.recompute_outcome_labels             # apply
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portfolio_agent.tools.validation_engine import recompute_outcome_labels

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    res = recompute_outcome_labels(dry_run=parser.parse_args().dry_run)
    print(f"Scanned {res['scanned']} evaluated rows; "
          f"{'would change' if res['dry_run'] else 'changed'} {res['changed']}; "
          f"skipped (would flip right/wrong) {res['skipped_correctness_flip']}")
    for k, v in sorted(res["transitions"].items()):
        print(f"  {k}: {v}")
