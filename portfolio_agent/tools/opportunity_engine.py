"""
Opportunity Engine — ranks today's APEX-analyzed discovery candidates
("what are the best new opportunities for MY portfolio today") instead of
just surfacing raw screener signals.

Pure Python, read-time only — no LLM calls, no new DB table. Reuses:
  - predictions rows already generated for trigger_type='trending_opportunity'
    (news-trending tickers + top quant-screener candidates, see
    pipeline/batch/morning.py::_run_morning_apex_event_driven)
  - screening_signals conviction data (universe_db.rank_by_conviction)
  - portfolio_risk.compute_hypothetical_addition_impact for the portfolio-fit story

Classification reuses APEX's own `recommendation` field directly rather than
inventing a second classifier: STRONG_BUY/BUY -> BUY, HOLD -> WATCH (only if
composite_score clears a floor), SELL/STRONG_SELL -> dropped.
"""

from __future__ import annotations

import json
from typing import Optional

from portfolio_agent.tools.db import db_conn

_WATCH_COMPOSITE_FLOOR = 5.5
_CONVICTION_BONUS_SCALE = 3.0
_CONVICTION_BONUS_CAP = 15.0
_CONCENTRATION_BONUS_CAP = 6.0
_CONCENTRATION_BONUS_SCALE = 8.0


def _latest_trending_opportunity_predictions() -> list[dict]:
    """One row per ticker (preferring the 21d horizon when present) for the
    most recent as_of_date that has trending_opportunity predictions."""
    with db_conn() as conn:
        latest_date_row = conn.execute(
            "SELECT MAX(as_of_date) AS d FROM predictions WHERE trigger_type = 'trending_opportunity'"
        ).fetchone()
        latest_date = latest_date_row["d"] if latest_date_row else None
        if not latest_date:
            return []

        rows = conn.execute(
            """SELECT ticker, horizon_days, recommendation, composite_score,
                      fundamental_score, research_score, macro_score, news_score,
                      reasoning, reasoning_text, panel_summary, created_at
               FROM predictions
               WHERE trigger_type = 'trending_opportunity' AND as_of_date = ?
               ORDER BY created_at DESC""",
            (latest_date,),
        ).fetchall()

    best_by_ticker: dict[str, dict] = {}
    for r in rows:
        d = dict(r)
        prev = best_by_ticker.get(d["ticker"])
        if prev is None or (d.get("horizon_days") == 21 and prev.get("horizon_days") != 21):
            best_by_ticker[d["ticker"]] = d
    return list(best_by_ticker.values())


def _conviction_bonus(ticker: str, conviction_by_ticker: dict[str, float]) -> float:
    conviction = conviction_by_ticker.get(ticker)
    if conviction is None:
        return 0.0
    return min(_CONVICTION_BONUS_CAP, max(0.0, conviction) * _CONVICTION_BONUS_SCALE)


def _portfolio_fit_bonus(impact: Optional[dict]) -> float:
    """0-15: rewards low correlation to current holdings and dilution of the
    portfolio's most concentrated sector. A rough heuristic, not a precise
    risk model — see compute_hypothetical_addition_impact for the raw numbers."""
    if not impact:
        return 0.0

    from portfolio_agent.tools.portfolio_risk import correlation_diversification_bonus

    corr_bonus = correlation_diversification_bonus(impact.get("correlation_with_portfolio"))

    concentration_bonus = 0.0
    top_before = impact.get("top_sector_before_pct")
    top_after = impact.get("top_sector_after_pct")
    if top_before is not None and top_after is not None and top_before > top_after:
        relief = top_before - top_after
        concentration_bonus = min(_CONCENTRATION_BONUS_CAP, relief * _CONCENTRATION_BONUS_SCALE)

    return corr_bonus + concentration_bonus


def _classify(recommendation: str, composite_score: Optional[float]) -> Optional[str]:
    if recommendation in ("STRONG_BUY", "BUY"):
        return "BUY"
    if recommendation == "HOLD" and composite_score is not None and composite_score >= _WATCH_COMPOSITE_FLOOR:
        return "WATCH"
    return None


def _why(row: dict, impact: Optional[dict]) -> str:
    narrative = row.get("reasoning_text") or row.get("reasoning") or ""
    if not impact:
        return narrative

    parts = []
    top_sector = impact.get("top_sector")
    top_before, top_after = impact.get("top_sector_before_pct"), impact.get("top_sector_after_pct")
    if top_sector and top_before is not None and top_after is not None and top_before > top_after:
        parts.append(f"dilutes your {top_sector} concentration {top_before:.0f}%→{top_after:.0f}%")

    corr = impact.get("correlation_with_portfolio")
    if corr is not None:
        parts.append(f"{corr:.2f} correlation with current holdings")

    weakest = impact.get("weakest_holding") or {}
    cand_rar = impact.get("candidate_risk_adjusted_return")
    weak_rar = weakest.get("risk_adjusted_return")
    if weakest.get("ticker") and cand_rar is not None and weak_rar is not None:
        comparison = "better" if cand_rar > weak_rar else "weaker"
        parts.append(f"{comparison} risk-adjusted return than your weakest holding ({weakest['ticker']})")

    if not parts:
        return narrative
    fit_sentence = "; ".join(parts).capitalize() + "."
    return f"{narrative} {fit_sentence}".strip()


def get_daily_opportunities(top_n: int = 10) -> list[dict]:
    """
    Rank today's trending_opportunity APEX predictions into a portfolio-aware
    top *top_n* list. Returns [] if the morning batch hasn't produced any
    trending_opportunity predictions yet today.

    Each item: {ticker, rank, opportunity_score, call (BUY|WATCH), why,
                composite_score, recommendation, portfolio_impact}
    """
    from portfolio_agent.tools.universe_db import (
        get_latest_signal_date, get_signals, get_conviction_data, rank_by_conviction,
    )
    from portfolio_agent.tools.portfolio_risk import compute_hypothetical_addition_impact
    from portfolio_agent.tools.portfolio_tools import get_portfolio_holdings

    predictions = _latest_trending_opportunity_predictions()
    if not predictions:
        return []

    screen_date = get_latest_signal_date()
    conviction_by_ticker: dict[str, float] = {}
    if screen_date:
        signals = get_signals(screen_date, min_score=2)
        conviction_data = get_conviction_data(screen_date, lookback_days=30, min_score=2)
        ranked_signals = rank_by_conviction(signals, conviction_data)
        conviction_by_ticker = {s["ticker"]: s["_conviction"] for s in ranked_signals}

    try:
        holdings = json.loads(get_portfolio_holdings()).get("holdings", [])
    except Exception:
        holdings = []

    scored: list[dict] = []
    for row in predictions:
        ticker = row["ticker"]
        composite_score = row.get("composite_score")
        call = _classify(row.get("recommendation") or "", composite_score)
        if call is None:
            continue

        impact: Optional[dict] = None
        if holdings:
            try:
                impact = compute_hypothetical_addition_impact(ticker, holdings)
            except Exception:
                impact = None

        base = (composite_score or 0) / 10 * 70
        opportunity_score = round(min(100, max(0, (
            base + _conviction_bonus(ticker, conviction_by_ticker) + _portfolio_fit_bonus(impact)
        ))))

        scored.append({
            "ticker": ticker,
            "opportunity_score": opportunity_score,
            "call": call,
            "why": _why(row, impact),
            "composite_score": composite_score,
            "recommendation": row.get("recommendation"),
            "panel_summary": (
                json.loads(row["panel_summary"]) if row.get("panel_summary") else {}
            ),
            "portfolio_impact": impact,
        })

    scored.sort(key=lambda s: s["opportunity_score"], reverse=True)
    top = scored[:top_n]
    for i, item in enumerate(top, 1):
        item["rank"] = i
    return top
