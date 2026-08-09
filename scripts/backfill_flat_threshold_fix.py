"""
One-time backfill: recompute actual_direction / outcome / outcome_score /
brier_score / log_loss for already-evaluated predictions under the corrected
FLAT_THRESHOLD (±1%, matching what the model is actually prompted with),
which replaces the old inconsistent ±0.5% used in six places in
validation_engine.py.

Uses actual_return already stored on each row — no new price fetches, no
network calls, purely a re-derivation of stored data. Safe to re-run;
it's idempotent (recomputing with the same threshold twice gives the same
result).

Usage:
    python -m scripts.backfill_flat_threshold_fix            # apply
    python -m scripts.backfill_flat_threshold_fix --dry-run  # preview only
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portfolio_agent.tools.prediction_db import _db, update_prediction_outcome
from portfolio_agent.tools.validation_engine import score_outcome, compute_distribution_metrics


def backfill(dry_run: bool = False) -> dict:
    with _db() as conn:
        rows = conn.execute(
            """SELECT id, ticker, horizon_days, predicted_direction,
                      predicted_return_low, predicted_return_high,
                      actual_return, actual_direction, outcome, outcome_score,
                      brier_score, log_loss, benchmark_return, excess_return,
                      p_strong_down, p_moderate_down, p_flat, p_moderate_up, p_strong_up
               FROM predictions
               WHERE evaluation_status = 'evaluated'
                 AND actual_return IS NOT NULL"""
        ).fetchall()
        rows = [dict(r) for r in rows]

    print(f"Found {len(rows)} evaluated predictions to re-score.")

    changed = 0
    unchanged = 0
    for r in rows:
        actual_return = r["actual_return"]

        outcome = score_outcome(r, actual_return)
        new_actual_dir = (
            "UP" if actual_return > 0.01 else "DOWN" if actual_return < -0.01 else "FLAT"
        )
        brier, log_loss = compute_distribution_metrics(r, actual_return)

        did_change = (
            new_actual_dir != r["actual_direction"]
            or outcome.label != r["outcome"]
            or abs((outcome.score or 0) - (r["outcome_score"] or 0)) > 1e-9
            or (brier is not None and r["brier_score"] is not None
                and abs(brier - r["brier_score"]) > 1e-9)
        )

        if did_change:
            changed += 1
            if not dry_run:
                # Recompute in_predicted_range / error_magnitude too, same
                # formulas evaluate_matured_predictions() already uses.
                r_lo = (r.get("predicted_return_low") or 0.0) / 100
                r_hi = (r.get("predicted_return_high") or 0.0) / 100
                in_range = bool(r_lo != 0 or r_hi != 0) and (r_lo <= actual_return <= r_hi)
                midpoint = (r_lo + r_hi) / 2 if (r_lo or r_hi) else 0.0
                error_mag = abs(actual_return - midpoint)

                update_prediction_outcome(
                    r["id"], actual_return, new_actual_dir,
                    r.get("benchmark_return"), r.get("excess_return"),  # preserve, don't null out
                    outcome.label, outcome.score, error_mag, in_range,
                    brier_score=brier, log_loss=log_loss,
                )
        else:
            unchanged += 1

    print(f"{'Would change' if dry_run else 'Changed'}: {changed}   Unchanged: {unchanged}")
    return {"changed": changed, "unchanged": unchanged, "total": len(rows)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = backfill(dry_run=args.dry_run)

    if not args.dry_run and result["changed"] > 0:
        print("\nRefreshing rolling metrics (metrics_rolling table)…")
        from portfolio_agent.tools.validation_engine import recompute_rolling_metrics
        recompute_rolling_metrics()
        print("Done. Reload the Validation page to see corrected numbers.")