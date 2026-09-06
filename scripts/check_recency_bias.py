"""
Recency-bias check: for every 5-day UP prediction, look at the ticker's
trailing 10-trading-day return *before* the prediction was made, and see
whether wrong UP calls cluster on tickers that had already run up sharply.

If wrong UP calls have meaningfully higher trailing momentum than correct
UP calls, that's evidence the model is extrapolating recent strength
forward rather than genuinely forecasting the next 5 days.

Usage: python scripts/check_recency_bias.py
   or: python -m scripts.check_recency_bias   (run from the repo root)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portfolio_agent.tools.prediction_db import _db
from portfolio_agent.tools.yfinance_tools import get_trailing_return as trailing_return


def main():
    with _db() as conn:
        rows = conn.execute(
            """SELECT ticker, as_of_date, actual_return, actual_direction
               FROM predictions
               WHERE horizon_days = 5
                 AND predicted_direction = 'UP'
                 AND evaluation_status = 'evaluated'
                 AND actual_return IS NOT NULL"""
        ).fetchall()
        rows = [dict(r) for r in rows]

    print(f"Checking trailing momentum for {len(rows)} UP predictions (this hits yfinance, be patient)…")

    correct_mom, wrong_mom = [], []
    for i, r in enumerate(rows, 1):
        mom = trailing_return(r["ticker"], r["as_of_date"])
        if mom is None:
            continue
        was_correct = r["actual_direction"] == "UP"
        (correct_mom if was_correct else wrong_mom).append(mom)
        if i % 25 == 0:
            print(f"  …{i}/{len(rows)}")

    def _stats(vals):
        if not vals:
            return "n=0"
        avg = sum(vals) / len(vals)
        pct_already_up = sum(1 for v in vals if v > 0.03) / len(vals) * 100
        return f"n={len(vals)}  mean_trailing_10d_return={avg*100:+.2f}%  %already_up_>3%={pct_already_up:.0f}%"

    print("\n--- Trailing 10-day momentum BEFORE the prediction was made ---")
    print(f"UP calls that were CORRECT: {_stats(correct_mom)}")
    print(f"UP calls that were WRONG:   {_stats(wrong_mom)}")
    print("\nIf 'WRONG' shows meaningfully higher trailing momentum than 'CORRECT',")
    print("that's evidence of recency-bias / momentum extrapolation in the 5d horizon.")


if __name__ == "__main__":
    main()