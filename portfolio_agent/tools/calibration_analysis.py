"""
Score Calibration analysis — for each scoring system (APEX, Valuation,
Opportunity, Portfolio Fit, Final), how well did it actually predict forward
returns? Pure math over portfolio_agent.tools.scoring_snapshot_db's matured
rows, no persistence of its own — this is the empirical answer to "is the
Opportunity Engine actually better than plain APEX," not a hand-wavy claim.

Two views per score, since a bare correlation coefficient is easy to
misread: Pearson correlation (score vs. realized return), and the more
legible top-quintile-vs-bottom-quintile average-return spread — "did the
names we scored highly actually do better."
"""

from __future__ import annotations

from typing import Optional

SCORE_COLUMNS = ("apex_score", "valuation_score", "opportunity_score", "portfolio_fit_score", "final_score")
SCORE_LABELS = {
    "apex_score": "APEX",
    "valuation_score": "Valuation",
    "opportunity_score": "Opportunity",
    "portfolio_fit_score": "Portfolio Fit",
    "final_score": "Final",
}
MIN_SAMPLE = 20


def _pearson_correlation(xs: list[float], ys: list[float]) -> Optional[float]:
    n = len(xs)
    if n < 2:
        return None
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x == 0 or var_y == 0:
        return None
    return cov / (var_x ** 0.5 * var_y ** 0.5)


def _score_vs_return(rows: list[dict], score_col: str) -> dict:
    pairs = [
        (r[score_col], r["return_pct"])
        for r in rows
        if r.get(score_col) is not None and r.get("return_pct") is not None
    ]
    n = len(pairs)
    if n < MIN_SAMPLE:
        return {"n": n, "insufficient_data": True}

    scores = [p[0] for p in pairs]
    returns = [p[1] for p in pairs]
    correlation = _pearson_correlation(scores, returns)

    ranked = sorted(pairs, key=lambda p: p[0])
    quintile_size = max(1, n // 5)
    bottom = ranked[:quintile_size]
    top = ranked[-quintile_size:]
    bottom_avg = sum(r for _, r in bottom) / len(bottom)
    top_avg = sum(r for _, r in top) / len(top)

    return {
        "n": n,
        "insufficient_data": False,
        "correlation": round(correlation, 3) if correlation is not None else None,
        "top_quintile_avg_return_pct": round(top_avg * 100, 2),
        "bottom_quintile_avg_return_pct": round(bottom_avg * 100, 2),
        "spread_pct": round((top_avg - bottom_avg) * 100, 2),
    }


def compute_calibration_report(horizon_days: int) -> dict:
    """
    {horizon_days, min_sample, scores: {score_col: {n, correlation,
     top_quintile_avg_return_pct, bottom_quintile_avg_return_pct, spread_pct}
     or {n, insufficient_data: True}}}
    """
    from portfolio_agent.tools.scoring_snapshot_db import get_snapshots_for_calibration

    rows = get_snapshots_for_calibration(horizon_days)
    return {
        "horizon_days": horizon_days,
        "min_sample": MIN_SAMPLE,
        "scores": {col: _score_vs_return(rows, col) for col in SCORE_COLUMNS},
    }
