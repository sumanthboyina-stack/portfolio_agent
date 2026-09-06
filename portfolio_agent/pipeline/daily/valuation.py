"""
Valuation phase — DCF (bear/base/bull) + relative valuation vs peers/market.

Unlike Fundamentals, this is pure deterministic math (portfolio_agent.tools.
valuation_engine) — no LLM calls, no batching/failover/checkpoint machinery
needed. Runs on the same ~95-day staleness cadence as Fundamentals and reuses
the same EDGAR bundle fetch, but is its own phase so a valuation failure never
blocks or gets blocked by the Fundamentals LLM batch.
"""

from __future__ import annotations

from portfolio_agent.log import get_logger as _get_logger


def _todays_trending_opportunity_tickers() -> list[str]:
    """
    Screener/news candidates APEX ran under trigger_type='trending_opportunity'
    (see pipeline/batch/morning.py::_run_morning_apex_event_driven) — these change
    daily and are never part of the static watchlist+portfolio list, so they must be
    pulled in separately or the Opportunity Engine's candidates never get a DCF at all.

    Matches the most recent as_of_date on record rather than requiring an exact
    match to date.today() — same pattern opportunity_engine.get_daily_opportunities()
    already uses, so this stays correct even if called hours after the batch that
    generated it (or, in a dev/manual run, days after).
    """
    from portfolio_agent.tools.db import db_conn

    with db_conn() as conn:
        latest = conn.execute(
            "SELECT MAX(as_of_date) AS d FROM predictions WHERE trigger_type = 'trending_opportunity'"
        ).fetchone()
        latest_date = latest["d"] if latest else None
        if not latest_date:
            return []
        rows = conn.execute(
            "SELECT DISTINCT ticker FROM predictions WHERE trigger_type = 'trending_opportunity' AND as_of_date = ?",
            (latest_date,),
        ).fetchall()
    return [r["ticker"] for r in rows]


async def _run_daily_valuation(all_tickers: list[str], tracker=None) -> None:
    log = _get_logger("valuation")
    from portfolio_agent.tools.valuation_db import needs_refresh, upsert_valuation
    from portfolio_agent.tools.edgar_check import _load_cik_map
    from portfolio_agent.tools.edgar import get_fundamentals_bundle
    from portfolio_agent.tools.yfinance_tools import get_valuation_multiples
    from portfolio_agent.tools.universe_db import get_peers
    from portfolio_agent.macro.fred_client import get_latest_value
    from portfolio_agent.tools.valuation_engine import (
        run_dcf_scenarios, sensitivity_grid, relative_valuation,
    )

    opportunity_tickers = _todays_trending_opportunity_tickers()
    all_tickers = list(dict.fromkeys(list(all_tickers) + opportunity_tickers))
    if opportunity_tickers:
        log.info(
            f"  +{len(opportunity_tickers)} today's Opportunity Engine candidate(s) added to the valuation universe",
            event_type="info",
        )

    to_refresh = [t for t in dict.fromkeys(all_tickers) if needs_refresh(t)]
    if not to_refresh:
        log.info("  All tickers have current valuation data.", event_type="info")
        if tracker:
            tracker.start_phase("valuation", total=0)
            tracker.finish_phase("valuation", 0, 0, note="all current")
        return

    log.info(
        f"  {len(to_refresh)}/{len(all_tickers)} tickers need valuation refresh (~95d cadence)",
        event_type="summary",
    )
    if tracker:
        tracker.start_phase("valuation", total=len(to_refresh))

    cik_map = _load_cik_map()
    rf_data = get_latest_value("DGS10")
    risk_free_rate_pct = rf_data.get("value") if "error" not in rf_data else 4.0

    # Peers/market tickers recur across many tickers' comparisons within one run.
    _multiples_cache: dict[str, dict] = {}

    def _cached_multiples(t: str) -> dict:
        if t not in _multiples_cache:
            _multiples_cache[t] = get_valuation_multiples(t)
        return _multiples_cache[t]

    market_multiples = _cached_multiples("SPY")

    completed: list[str] = []
    failed: list[str] = []

    for i, ticker in enumerate(to_refresh, 1):
        try:
            bundle = get_fundamentals_bundle(ticker, cik_map)
            if bundle.get("error"):
                failed.append(ticker)
                continue

            revenue = bundle["income"]["revenue"]
            operating_cf = bundle["cashflow"]["operating_cf"]
            capex = bundle["cashflow"]["capex"]
            multiples = _cached_multiples(ticker)

            scenarios = run_dcf_scenarios(revenue, operating_cf, capex, multiples, risk_free_rate_pct)
            if scenarios is None:
                failed.append(ticker)
                continue

            peers = get_peers(ticker, limit=5)
            peer_multiples = [_cached_multiples(p) for p in peers]
            rel_val = relative_valuation(multiples, peer_multiples, market_multiples)
            grid = sensitivity_grid(revenue, operating_cf, capex, multiples, risk_free_rate_pct)

            upsert_valuation(ticker, scenarios, relative_valuation=rel_val, sensitivity_grid=grid)
            completed.append(ticker)
            log.info(
                f"  [{i}/{len(to_refresh)}] {ticker}: {scenarios['valuation_label']} "
                f"(margin of safety {scenarios['margin_of_safety_pct']}%)",
                event_type="db_write", ticker=ticker, action=scenarios["valuation_label"],
            )
        except Exception as exc:
            log.warning(f"  [warn] Valuation failed for {ticker}: {exc}", event_type="warning")
            failed.append(ticker)

        if tracker:
            tracker.tick("valuation", len(completed), len(to_refresh), ticker=ticker)

    if tracker:
        tracker.finish_phase("valuation", len(completed), len(failed))
    log.info(
        f"  Valuation phase done — {len(completed)} saved, {len(failed)} failed",
        event_type="phase_end", saved=len(completed), failed=len(failed),
    )
