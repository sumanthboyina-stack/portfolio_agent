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
from portfolio_agent.tools.prediction_db import SHARED_SCOPES, scope_clause

_WATCH_COMPOSITE_FLOOR = 5.5
_CONVICTION_BONUS_SCALE = 3.0
_CONVICTION_BONUS_CAP = 15.0
_CONCENTRATION_BONUS_CAP = 6.0
_CONCENTRATION_BONUS_SCALE = 8.0


def _latest_trending_opportunity_predictions() -> list[dict]:
    """One row per ticker (preferring the 21d horizon when present) for the
    most recent as_of_date that has trending_opportunity predictions.

    Explicitly scope-filtered to SHARED_SCOPES: trigger_type='trending_opportunity'
    happens to be enough on its own today only because the one other writer
    (web/pages/4_🤖_Chat.py's private chat forecasts) never sets trigger_type —
    an accident of the current call sites, not a guarantee. This engine ranks
    candidates for display to the local owner from what the SHARED, market-only
    pipeline discovered; it must never surface a private row just because a
    future caller starts stamping trigger_type on one.
    """
    _sc, _sp = scope_clause(SHARED_SCOPES)
    with db_conn() as conn:
        latest_date_row = conn.execute(
            f"SELECT MAX(as_of_date) AS d FROM predictions WHERE trigger_type = 'trending_opportunity' AND {_sc}",
            _sp,
        ).fetchone()
        latest_date = latest_date_row["d"] if latest_date_row else None
        if not latest_date:
            return []

        rows = conn.execute(
            f"""SELECT ticker, horizon_days, recommendation, composite_score,
                      fundamental_score, research_score, macro_score, news_score,
                      reasoning, reasoning_text, panel_summary, created_at, guardrail_flags
               FROM predictions
               WHERE trigger_type = 'trending_opportunity' AND as_of_date = ? AND {_sc}
               ORDER BY created_at DESC""",
            (latest_date, *_sp),
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


def _classify(recommendation: str, composite_score: Optional[float],
              guardrail_flags=None) -> Optional[str]:
    """
    A STRONG_BUY/BUY call only counts as a BUY pick when the conviction
    guardrails never fired on it -- a capped bounce/no-news call is
    deliberately no longer trustworthy enough to rank as a BUY (it would
    otherwise still lead the Opportunity Engine's list on its unmodified
    recommendation string, even after its own conviction/composite_score
    were capped). It falls through to the same score-only judgment an
    uncapped HOLD gets: WATCH if it still clears the floor, else dropped.
    """
    was_capped = bool(guardrail_flags)
    if recommendation in ("STRONG_BUY", "BUY"):
        if not was_capped:
            return "BUY"
        if composite_score is not None and composite_score >= _WATCH_COMPOSITE_FLOOR:
            return "WATCH"
        return None
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


def _score_candidate(
    row: dict, conviction_by_ticker: dict[str, float], holdings: list[dict],
) -> dict:
    """
    Score one prediction row regardless of its call — classification (BUY/WATCH/
    dropped) is a separate concern from scoring, so callers that need the full
    universe (e.g. the calibration snapshot) aren't limited to already-filtered
    winners. Reused by both get_daily_opportunities() and score_all_scored_tickers().
    """
    from portfolio_agent.tools.portfolio_risk import compute_hypothetical_addition_impact

    ticker = row["ticker"]
    composite_score = row.get("composite_score")
    call = _classify(row.get("recommendation") or "", composite_score, row.get("guardrail_flags"))

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

    return {
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
    }


def _load_conviction_and_holdings() -> tuple[dict[str, float], list[dict]]:
    from portfolio_agent.tools.universe_db import (
        get_latest_signal_date, get_signals, get_conviction_data, rank_by_conviction,
    )
    from portfolio_agent.tools.portfolio_tools import get_portfolio_holdings

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

    return conviction_by_ticker, holdings


def get_opportunity_history(ticker: str, window_days: int = 30) -> dict:
    """
    How many distinct days in the trailing *window_days* this ticker qualified
    as BUY/WATCH among trending_opportunity predictions, plus the first
    qualifying day in that window and its composite_score then vs. now —
    "has this been flagged before, and did anything change since." Mirrors
    the persistence concept universe_db already tracks for raw screener
    signals, applied here to APEX-vetted opportunities instead.
    """
    from datetime import date, timedelta

    cutoff = (date.today() - timedelta(days=window_days)).isoformat()
    _sc, _sp = scope_clause(SHARED_SCOPES)
    with db_conn() as conn:
        rows = conn.execute(
            f"""SELECT as_of_date, composite_score, recommendation, guardrail_flags
               FROM predictions
               WHERE trigger_type = 'trending_opportunity' AND ticker = ?
                 AND as_of_date >= ? AND {_sc}
               ORDER BY as_of_date ASC""",
            (ticker, cutoff, *_sp),
        ).fetchall()

    all_rows = [dict(r) for r in rows]
    qualifying = [
        d for d in all_rows
        if _classify(d.get("recommendation") or "", d.get("composite_score"), d.get("guardrail_flags")) is not None
    ]
    if not qualifying:
        return {
            "times_identified": 0,
            "first_identified_date": None,
            "first_composite_score": None,
            "current_composite_score": None,
            "score_change": None,
        }

    first, current = qualifying[0], qualifying[-1]
    first_cs, current_cs = first.get("composite_score"), current.get("composite_score")
    score_change = (current_cs - first_cs) if first_cs is not None and current_cs is not None else None

    return {
        "times_identified": len(qualifying),
        "first_identified_date": first["as_of_date"],
        "first_composite_score": first_cs,
        "current_composite_score": current_cs,
        "score_change": score_change,
    }


def get_daily_opportunities(top_n: int = 10) -> list[dict]:
    """
    Rank today's trending_opportunity APEX predictions into a portfolio-aware
    top *top_n* list. Returns [] if the morning batch hasn't produced any
    trending_opportunity predictions yet today.

    Each item: {ticker, rank, opportunity_score, call (BUY|WATCH), why,
                composite_score, recommendation, portfolio_impact}
    """
    predictions = _latest_trending_opportunity_predictions()
    if not predictions:
        return []

    conviction_by_ticker, holdings = _load_conviction_and_holdings()

    scored = [_score_candidate(row, conviction_by_ticker, holdings) for row in predictions]
    scored = [s for s in scored if s["call"] is not None]

    scored.sort(key=lambda s: s["opportunity_score"], reverse=True)
    top = scored[:top_n]
    for i, item in enumerate(top, 1):
        item["rank"] = i
    return top


def score_all_scored_tickers() -> list[dict]:
    """
    Same scoring as get_daily_opportunities(), but every trending_opportunity
    ticker today — including SELL/low-HOLD names get_daily_opportunities()
    would drop. Used by the calibration snapshot so the sample isn't biased
    toward names the Opportunity Engine already picked as winners.
    """
    predictions = _latest_trending_opportunity_predictions()
    if not predictions:
        return []

    conviction_by_ticker, holdings = _load_conviction_and_holdings()
    return [_score_candidate(row, conviction_by_ticker, holdings) for row in predictions]
