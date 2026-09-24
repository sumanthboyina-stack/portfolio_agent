"""
Score Calibration snapshot phase — captures APEX/Valuation/Opportunity/
Portfolio Fit/Final scores for every ticker scored today, so
calibration_analysis.py has a historical record to check against realized
returns 30/60/90/250 days later. No LLM calls — pure math over data the
APEX and Universe Screen phases already produced earlier in this run.

Covers every ticker with a fresh prediction today (portfolio holdings AND
Opportunity Engine candidates alike) via opportunity_engine.score_all_scored_tickers()
extended with a portfolio-holdings query — scoring only Opportunity Engine's own
picks would bias the eventual comparison toward names it already chose as winners.
"""

from __future__ import annotations

from datetime import date

from portfolio_agent.log import get_logger as _get_logger

_FINAL_WEIGHTS = {"apex": 0.30, "valuation": 0.20, "opportunity": 0.30, "portfolio_fit": 0.20}


def _todays_prediction_rows(today: str) -> list[dict]:
    """One row per ticker (most recently created) with an as_of_date == today,
    regardless of trigger_type — covers portfolio holdings and Opportunity
    Engine candidates alike.

    Explicitly scoped to SHARED_SCOPES: this had NO scope filter at all
    (not even a trigger_type restriction) — any private row (a chat
    forecast) with today's as_of_date would have been picked up. The
    calibration snapshot this feeds is a SHARED, pipeline-wide record;
    it must never fold in a private forecast just because a caller writes
    one with today's date.
    """
    from portfolio_agent.tools.db import db_conn
    from portfolio_agent.tools.prediction_db import SHARED_SCOPES, scope_clause

    _sc, _sp = scope_clause(SHARED_SCOPES)
    with db_conn() as conn:
        rows = conn.execute(
            f"""SELECT ticker, composite_score, valuation_score, recommendation,
                      reasoning, reasoning_text, panel_summary, created_at, guardrail_flags
               FROM predictions
               WHERE as_of_date = ? AND {_sc}
               ORDER BY created_at DESC""",
            (today, *_sp),
        ).fetchall()

    best_by_ticker: dict[str, dict] = {}
    for r in rows:
        d = dict(r)
        if d["ticker"] not in best_by_ticker:
            best_by_ticker[d["ticker"]] = d
    return list(best_by_ticker.values())


def _final_score(apex_score, valuation_score, opportunity_score, portfolio_fit_score) -> float:
    """
    Documented fixed blend (not learned yet — not enough matured outcome data
    exists to fit weights against realized returns). Stored alongside its raw
    components specifically so a future iteration can refit this once it does.
    """
    apex_pct = min(100.0, max(0.0, ((apex_score or 1.0) / 10.0) * 100.0))
    valuation_pct = min(100.0, max(0.0, ((valuation_score or 5.5) / 10.0) * 100.0))
    opportunity_pct = opportunity_score if opportunity_score is not None else 0.0
    fit_pct = portfolio_fit_score if portfolio_fit_score is not None else 50.0
    return round(
        _FINAL_WEIGHTS["apex"] * apex_pct
        + _FINAL_WEIGHTS["valuation"] * valuation_pct
        + _FINAL_WEIGHTS["opportunity"] * opportunity_pct
        + _FINAL_WEIGHTS["portfolio_fit"] * fit_pct,
        1,
    )


async def _run_daily_scoring_snapshot(tracker=None) -> None:
    log = _get_logger("scoring_snapshot")
    from portfolio_agent.tools.opportunity_engine import _score_candidate, _load_conviction_and_holdings
    from portfolio_agent.tools.portfolio_risk import compute_portfolio_fit_score
    from portfolio_agent.tools.scoring_snapshot_db import upsert_snapshot
    from portfolio_agent.tools.yfinance_tools import get_close

    today = date.today().isoformat()
    rows = _todays_prediction_rows(today)
    if not rows:
        log.info("  No predictions today — nothing to snapshot.", event_type="info")
        if tracker:
            tracker.start_phase("scoring_snapshot", total=0)
            tracker.finish_phase("scoring_snapshot", 0, 0, note="no predictions today")
        return

    conviction_by_ticker, holdings = _load_conviction_and_holdings()

    if tracker:
        tracker.start_phase("scoring_snapshot", total=len(rows))

    saved, failed = 0, 0
    for row in rows:
        ticker = row["ticker"]
        try:
            scored = _score_candidate(row, conviction_by_ticker, holdings)
            fit_score = compute_portfolio_fit_score(ticker, holdings)
            entry_price = get_close(ticker, today)
            final = _final_score(
                row.get("composite_score"), row.get("valuation_score"),
                scored["opportunity_score"], fit_score,
            )
            upsert_snapshot(
                ticker=ticker,
                as_of_date=today,
                entry_price=entry_price,
                apex_score=row.get("composite_score"),
                valuation_score=row.get("valuation_score"),
                opportunity_score=scored["opportunity_score"],
                portfolio_fit_score=fit_score,
                final_score=final,
            )
            saved += 1
        except Exception as exc:
            log.warning(f"  [warn] Snapshot failed for {ticker}: {exc}", event_type="warning")
            failed += 1
        if tracker:
            tracker.tick("scoring_snapshot", saved, len(rows), ticker=ticker)

    if tracker:
        tracker.finish_phase("scoring_snapshot", saved, failed)
    log.info(
        f"  Scoring snapshot done — {saved} saved, {failed} failed ({len(rows)} tickers)",
        event_type="phase_end", saved=saved, failed=failed,
    )
