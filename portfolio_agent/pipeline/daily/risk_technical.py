"""
Risk + Technical specialists — daily portfolio flagging (Approach B).

Runs alongside APEX for portfolio holdings only, writing to the standalone
`risk_flags` table rather than the `predictions` table / weight engine — no
schema change to predictions, no re-weighting, no disruption to the
composite-score history the Validation/Track Record tooling analyzes.
`flag=1` rows feed the dashboard's Attention Queue directly (see
web/app.py::_build_queue).

Risk is per-owner, Technical is shared (see tools.risk_flags_db's module
docstring for why): the Risk specialist's own tools only see one ticker at a
time and can't compute correlation/sector-aggregate/beta/issuer-concentration
across a portfolio — portfolio_risk.py computes that once PER OWNER (pure
math, no LLM) and this module folds it into the Risk specialist's query text,
so two owners holding the same ticker get their own numbers, not one blended
across everyone's combined book. Technical output (SMA/RSI/MACD/momentum) is
pure market data, computed once per ticker regardless of who holds it.

Technical: the LLM Technical specialist below produces an optional,
market-only NARRATIVE (score + summary) written to risk_flags, unchanged.
The deterministic indicator MATH behind it (SMA/RSI/MACD/Bollinger/ATR) is
computed and stored separately by tools.technical_features, keyed by
(instrument, data_snapshot, interval, lookback, adjustment_method,
calculation_version) — see _store_deterministic_technical_features below.
That write is best-effort and never affects this phase's pass/fail
accounting: a caller wanting the raw numbers reads technical_features
directly rather than parsing the specialist's narrative.
"""

from __future__ import annotations

import json
from datetime import date

from portfolio_agent.log import get_logger as _get_logger


def _owners_and_their_tickers() -> dict[str, list[str]]:
    """
    owner -> sorted unique tickers currently held in that owner's portfolio,
    for every ENABLED auth_users owner (never every row in `portfolios` —
    a portfolio can outlive the identity that created it, e.g. after a
    rename/re-invite, and computing risk for an owner no login can reach
    would just burn LLM calls on unreachable data). Owners with no holdings
    are omitted. System-wide batch use only — never a per-request read.
    """
    from portfolio_agent.tools import auth_users_db, holdings_db as repo

    out: dict[str, list[str]] = {}
    for u in auth_users_db.list_users():
        if not u.enabled:
            continue
        portfolio = repo.get_portfolio_by_owner(u.owner)
        if portfolio is None:
            continue
        tickers = sorted({h["ticker"] for h in repo.get_holdings(portfolio_id=portfolio["portfolio_id"]) if h.get("ticker")})
        if tickers:
            out[u.owner] = tickers
    return out


async def _run_daily_risk_technical(portfolio_tickers: list[str], tracker=None) -> None:
    log = _get_logger("risk_technical")
    from portfolio_agent.pipeline.runner import run_analysis_with_failover
    from portfolio_agent._models import ModelSession
    from portfolio_agent.tools.risk_flags_db import needs_risk_refresh, needs_technical_refresh
    from portfolio_agent.tools.portfolio_risk import compute_portfolio_risk_context
    from portfolio_agent.pipeline.json_parsing import _extract_json_block

    if not portfolio_tickers:
        log.info("  No portfolio tickers — skipping Risk/Technical phase.", event_type="info")
        if tracker:
            tracker.start_phase("risk_technical", total=0)
            tracker.finish_phase("risk_technical", 0, 0, note="no portfolio tickers")
        return

    today = date.today().isoformat()

    # Technical: shared, ticker-only (union across every owner — same list
    # portfolio_tickers already is).
    technical_to_run = [t for t in dict.fromkeys(portfolio_tickers) if needs_technical_refresh(t, today)]

    # Risk: per owner, only that owner's own holdings.
    owner_tickers = _owners_and_their_tickers()
    risk_to_run: list[tuple[str, str]] = [
        (owner, t) for owner, tickers in owner_tickers.items() for t in tickers
        if needs_risk_refresh(t, today, owner)
    ]

    if not technical_to_run and not risk_to_run:
        log.info(
            f"  All {len(set(portfolio_tickers))} portfolio tickers already flagged today.",
            event_type="info",
        )
        if tracker:
            tracker.start_phase("risk_technical", total=0)
            tracker.finish_phase("risk_technical", 0, 0, note="already flagged today")
        return

    total_calls = len(technical_to_run) + len(risk_to_run)
    if tracker:
        tracker.start_phase("risk_technical", total=total_calls)

    completed = 0
    failed: list[str] = []
    risk_session = ModelSession("risk")
    # "technical" has no registered failover factory (single-attempt only) —
    # matches its existing on-demand behavior elsewhere in the codebase.

    # ── Risk pass — grouped by owner so risk_ctx is computed once per owner ──
    by_owner: dict[str, list[str]] = {}
    for owner, ticker in risk_to_run:
        by_owner.setdefault(owner, []).append(ticker)

    risk_call_i = 0
    for owner, tickers in by_owner.items():
        log.info(
            f"  Computing cross-holding risk context for {owner!r} ({len(tickers)} holdings)…",
            event_type="fetch_start",
        )
        try:
            from portfolio_agent.tools import holdings_db as repo
            portfolio = repo.get_portfolio_by_owner(owner)
            holdings = [h.to_dict() for h in repo.get_holdings(portfolio_id=portfolio["portfolio_id"])] if portfolio else []
            risk_ctx = compute_portfolio_risk_context(holdings)
        except Exception as exc:
            log.warning(
                f"  [warn] Portfolio risk context failed for {owner!r} ({exc}) — Risk will run per-ticker only.",
                event_type="warning",
            )
            risk_ctx = {}

        for ticker in tickers:
            risk_call_i += 1
            rctx = risk_ctx.get(ticker.upper())
            risk_query = "Full risk analysis for this position."
            if rctx:
                risk_query += (
                    "\n\nPre-computed portfolio-wide context (use directly, do not "
                    f"re-derive): {json.dumps(rctx)}"
                )

            risk_data: dict = {}
            risk_model = ""
            try:
                risk_state = await run_analysis_with_failover(
                    ticker, "risk", query=risk_query, model_session=risk_session,
                )
                risk_data = _extract_json_block(risk_state.get("risk") or "") or {}
                risk_model = risk_state.get("_model_used", "")
            except Exception as exc:
                log.warning(f"  [warn] Risk specialist failed for {owner!r}/{ticker}: {exc}", event_type="warning")
                failed.append(f"{owner}:{ticker}:risk")

            if risk_data:
                _save_risk_flag(ticker, today, risk_data, risk_model, owner)
                completed += 1
            if tracker:
                tracker.tick("risk_technical", completed, total_calls, ticker=ticker,
                            note=f"risk {risk_call_i}/{len(risk_to_run)}")

            log.info(
                f"  [risk {risk_call_i}/{len(risk_to_run)}] {owner!r}/{ticker} — "
                f"risk={'✓' if risk_data else '✗'}",
                event_type="db_write", ticker=ticker,
            )

    # ── Technical pass — once per ticker, shared across every owner ──────────
    for i, ticker in enumerate(technical_to_run, 1):
        tech_data: dict = {}
        tech_model = ""
        try:
            tech_state = await run_analysis_with_failover(ticker, "technical", query="Full technical analysis.")
            tech_data = _extract_json_block(tech_state.get("technical") or "") or {}
            tech_model = tech_state.get("_model_used", "")
        except Exception as exc:
            log.warning(f"  [warn] Technical specialist failed for {ticker}: {exc}", event_type="warning")
            failed.append(f"{ticker}:technical")

        if tech_data:
            _save_technical_flag(ticker, today, tech_data, tech_model)
            completed += 1
        if tracker:
            tracker.tick("risk_technical", completed, total_calls, ticker=ticker,
                        note=f"technical {i}/{len(technical_to_run)}")

        _store_deterministic_technical_features(ticker, log)

        log.info(
            f"  [technical {i}/{len(technical_to_run)}] {ticker} — "
            f"technical={'✓' if tech_data else '✗'}",
            event_type="db_write", ticker=ticker,
        )

    if tracker:
        tracker.finish_phase("risk_technical", completed, len(failed))
    log.info(
        f"\n  Risk/Technical phase done — {completed} flag(s) saved · {len(failed)} failed",
        event_type="phase_end", saved=completed, failed=len(failed),
    )


def _save_risk_flag(ticker: str, as_of_date: str, data: dict, model_used: str, owner: str) -> None:
    from portfolio_agent.tools.risk_flags_db import upsert_risk_flag

    score = data.get("portfolio_risk_score")
    flag = bool(
        data.get("concentration_flag")
        or (isinstance(score, (int, float)) and score >= 8)
        or (data.get("sector_concentration_pct") or 0) > 25
        or (data.get("issuer_concentration_pct") or 0) > 15
        or abs(data.get("beta_vs_spy") or 0) > 1.5
    )
    upsert_risk_flag(
        ticker=ticker, as_of_date=as_of_date, source="risk",
        flag=flag, score=score, summary=data.get("summary", ""),
        raw_json=json.dumps(data), model_used=model_used, owner=owner,
    )


def _save_technical_flag(ticker: str, as_of_date: str, data: dict, model_used: str) -> None:
    from portfolio_agent.tools.risk_flags_db import upsert_risk_flag

    score = data.get("technical_score")
    flag = bool(
        data.get("momentum") == "BEARISH"
        or (isinstance(score, (int, float)) and score <= 3)
    )
    upsert_risk_flag(
        ticker=ticker, as_of_date=as_of_date, source="technical",
        flag=flag, score=score, summary=data.get("summary", ""),
        raw_json=json.dumps(data), model_used=model_used,
    )


def _store_deterministic_technical_features(ticker: str, log) -> None:
    """
    Best-effort, market-only side write of the deterministic indicator math
    (see module docstring). Uses the shared history cache and the store's own
    reuse-by-key check — a ticker already computed today by this run, another
    run, or a chat request reading the same instrument is not recomputed.
    Never raises into the caller and never affects risk_technical's own
    completed/failed counts — a caller wanting these numbers reads
    tools.technical_features directly, not this pipeline's pass/fail state.
    """
    from portfolio_agent.tools.technical_features import fetch_and_store_technical_features
    try:
        fetch_and_store_technical_features(ticker)
    except Exception as exc:
        log.warning(f"  [warn] technical_features store failed for {ticker}: {exc}", event_type="warning")
