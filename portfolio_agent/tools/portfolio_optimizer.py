"""
Portfolio Optimizer — turns "NVDA = STRONG BUY" into an actionable answer to
three questions: where should new cash go, which existing holding deserves
more capital, and which positions are worth trimming.

Pure Python/pandas, read-time only — no LLM calls, no new DB table. Reuses:
  - portfolio_risk.compute_portfolio_aggregate_metrics/compute_portfolio_risk_context
    for per-holding concentration/correlation and the before/after impact projection
  - prediction_db.get_all_latest_predictions for APEX conviction on existing holdings
  - opportunity_engine.get_daily_opportunities/_classify for the "new buy" universe
    and BUY/WATCH/drop classification (shared, not reinvented)
"""

from __future__ import annotations

from typing import Optional

_SECTOR_CONCENTRATION_THRESHOLD = 25.0   # % of portfolio in one sector -> reduce candidate
_ISSUER_CONCENTRATION_THRESHOLD = 15.0   # % of portfolio in one issuer (share classes) -> reduce candidate
_MAX_PER_CANDIDATE_PCT_OF_CASH  = 0.4    # no single allocation > 40% of the new cash
_MAX_POST_TRADE_POSITION_PCT    = 0.15   # no resulting position > 15% of post-trade total value
_ALLOCATION_SCORE_FLOOR         = 45.0   # candidates below this don't get cash
_ROUND_TO                       = 50.0   # round dollar allocations to the nearest $50


def _round_dollars(amount: float) -> float:
    return round(amount / _ROUND_TO) * _ROUND_TO


def _consolidate_by_ticker(holdings: list[dict]) -> list[dict]:
    """
    Merge holding rows that share a ticker across multiple brokers/accounts
    (e.g. the same stock held at both Vanguard and Fidelity) into one position
    per ticker — otherwise a ticker can be scored/flagged twice with two
    different share counts, producing duplicate or contradictory recommendations.
    """
    by_ticker: dict[str, dict] = {}
    for h in holdings:
        ticker = str(h.get("ticker", "")).upper()
        if not ticker:
            continue
        if ticker not in by_ticker:
            by_ticker[ticker] = {"ticker": ticker, "shares": 0.0, "sector": h.get("sector")}
        by_ticker[ticker]["shares"] += float(h.get("shares") or 0)
    return list(by_ticker.values())


def _overweight_penalty(sector_pct: Optional[float], issuer_pct: Optional[float]) -> float:
    """0-20: an already-concentrated holding gets less new cash even with strong
    conviction — the point of new cash is diversifying, not doubling down."""
    penalty = 0.0
    if sector_pct is not None and sector_pct > _SECTOR_CONCENTRATION_THRESHOLD:
        penalty += min(12.0, (sector_pct - _SECTOR_CONCENTRATION_THRESHOLD) * 0.8)
    if issuer_pct is not None and issuer_pct > _ISSUER_CONCENTRATION_THRESHOLD:
        penalty += min(8.0, (issuer_pct - _ISSUER_CONCENTRATION_THRESHOLD) * 0.8)
    return penalty


def _existing_holding_candidates(
    holdings: list[dict], risk_ctx: dict, latest_preds: dict, exclude: set[str],
) -> list[dict]:
    """Current holdings whose latest APEX call is BUY-classified — candidates to top up.
    exclude: tickers already flagged in the reduce list — never recommend adding to a
    position in the same breath as flagging it for concentration/SELL risk."""
    from portfolio_agent.tools.opportunity_engine import _classify
    from portfolio_agent.tools.portfolio_risk import correlation_diversification_bonus

    candidates = []
    for h in holdings:
        ticker = str(h["ticker"]).upper()
        if ticker in exclude:
            continue
        pred = latest_preds.get(ticker)
        if pred is None:
            continue
        composite_score = pred.get("composite_score")
        if _classify(pred.get("recommendation") or "", composite_score) != "BUY":
            continue

        ctx = risk_ctx.get(ticker, {})
        score = (
            (composite_score or 0) / 10 * 70
            + correlation_diversification_bonus(ctx.get("correlation_with_portfolio"))
            - _overweight_penalty(ctx.get("sector_concentration_pct"), ctx.get("issuer_concentration_pct"))
        )
        candidates.append({
            "ticker": ticker,
            "kind": "existing",
            "score": round(max(0.0, score), 1),
            "why": pred.get("reasoning_text") or pred.get("reasoning") or "",
            "composite_score": composite_score,
            "recommendation": pred.get("recommendation"),
            "candidate_sector_before_pct": ctx.get("sector_concentration_pct"),
        })
    return candidates


def _new_buy_candidates(existing_tickers: set[str]) -> list[dict]:
    """Today's Opportunity Engine BUY list, reshaped to the same candidate schema
    used for existing holdings so both compete on one ranked list."""
    from portfolio_agent.tools.opportunity_engine import get_daily_opportunities
    from portfolio_agent.tools.portfolio_risk import correlation_diversification_bonus

    candidates = []
    for opp in get_daily_opportunities(top_n=15):
        if opp["call"] != "BUY" or opp["ticker"] in existing_tickers:
            continue
        impact = opp.get("portfolio_impact") or {}
        if "candidate_price" not in impact:
            continue  # can't size a $ allocation without a price

        score = (
            (opp["composite_score"] or 0) / 10 * 70
            + correlation_diversification_bonus(impact.get("correlation_with_portfolio"))
            - _overweight_penalty(impact.get("candidate_sector_before_pct"), None)
        )
        candidates.append({
            "ticker": opp["ticker"],
            "kind": "new",
            "score": round(max(0.0, score), 1),
            "why": opp["why"],
            "composite_score": opp["composite_score"],
            "recommendation": opp["recommendation"],
            "candidate_price": impact["candidate_price"],
        })
    return candidates


def _allocate_cash(candidates: list[dict], cash_amount: float, baseline: Optional[dict]) -> tuple[list[dict], float]:
    """
    Greedy proportional allocation: each qualifying candidate's target share of
    cash is proportional to its score among the ranked pool, capped so no single
    allocation is oversized relative to the new cash or the resulting position.
    Capped/excluded amounts are NOT redistributed to other candidates — they show
    up honestly as CASH rather than being silently reallocated.
    """
    qualifying = [c for c in candidates if c["score"] >= _ALLOCATION_SCORE_FLOOR]
    if not qualifying or cash_amount <= 0:
        return [], _round_dollars(cash_amount)

    total_score = sum(c["score"] for c in qualifying)
    post_trade_total = (baseline["total_value"] if baseline else 0.0) + cash_amount

    allocations = []
    remaining = cash_amount
    for c in qualifying:
        if remaining <= 0:
            break
        raw_amount = cash_amount * (c["score"] / total_score)

        position_cap = remaining
        if baseline:
            current_value = 0.0
            if c["kind"] == "existing":
                # current_value approximated from baseline holdings via price * shares
                # isn't directly available here — caller passes it in via risk_ctx-derived
                # weight when present; fall back to 0 (no cap tightening) otherwise.
                current_value = c.get("current_value", 0.0)
            position_cap = max(0.0, _MAX_POST_TRADE_POSITION_PCT * post_trade_total - current_value)

        cap = min(cash_amount * _MAX_PER_CANDIDATE_PCT_OF_CASH, position_cap, remaining)
        amount = _round_dollars(min(raw_amount, cap))
        if amount <= 0:
            continue

        allocations.append({
            "ticker": c["ticker"],
            "kind": c["kind"],
            "amount": amount,
            "why": c["why"],
            "composite_score": c["composite_score"],
            "recommendation": c["recommendation"],
        })
        remaining -= amount

    cash_reserved = round(cash_amount - sum(a["amount"] for a in allocations), 2)
    return allocations, cash_reserved


def _reduce_candidates(holdings: list[dict], risk_ctx: dict, latest_preds: dict, prices: dict) -> list[dict]:
    """Current holdings worth trimming: a SELL/STRONG_SELL call, or concentration
    (sector or issuer) past threshold. Suggested trim is a simple proportional
    pull-back, not a precise target-weight solve."""
    reduce_list = []
    for h in holdings:
        ticker = str(h["ticker"]).upper()
        pred = latest_preds.get(ticker)
        ctx = risk_ctx.get(ticker, {})
        reasons: list[str] = []
        trim_pct = 0.0

        rec = pred.get("recommendation") if pred else None
        if rec == "STRONG_SELL":
            reasons.append(pred.get("reasoning_text") or pred.get("reasoning") or "APEX recommends STRONG_SELL")
            trim_pct = max(trim_pct, 60.0)
        elif rec == "SELL":
            reasons.append(pred.get("reasoning_text") or pred.get("reasoning") or "APEX recommends SELL")
            trim_pct = max(trim_pct, 30.0)

        sector_pct = ctx.get("sector_concentration_pct")
        if sector_pct is not None and sector_pct > _SECTOR_CONCENTRATION_THRESHOLD:
            reasons.append(f"{sector_pct:.1f}% of portfolio is in this ticker's sector (>{_SECTOR_CONCENTRATION_THRESHOLD:.0f}% threshold)")
            trim_pct = max(trim_pct, min(50.0, (sector_pct - _SECTOR_CONCENTRATION_THRESHOLD) / sector_pct * 100))

        issuer_pct = ctx.get("issuer_concentration_pct")
        has_issuer_peers = bool(ctx.get("issuer_peers"))
        if issuer_pct is not None and issuer_pct > _ISSUER_CONCENTRATION_THRESHOLD:
            # issuer_concentration_pct with no issuer_peers just IS this ticker's own
            # portfolio weight — phrase it as position size, not share-class overlap.
            label = (
                f"across share classes with {', '.join(ctx['issuer_peers'])}"
                if has_issuer_peers else "in this single position"
            )
            reasons.append(f"{issuer_pct:.1f}% of portfolio is {label} (>{_ISSUER_CONCENTRATION_THRESHOLD:.0f}% threshold)")
            trim_pct = max(trim_pct, min(50.0, (issuer_pct - _ISSUER_CONCENTRATION_THRESHOLD) / issuer_pct * 100))

        if not reasons:
            continue

        current_value = float(h.get("shares") or 0) * prices.get(ticker, 0.0)
        reduce_list.append({
            "ticker": ticker,
            "rationale": "; ".join(reasons),
            "suggested_trim_dollars": _round_dollars(current_value * trim_pct / 100),
        })
    return reduce_list


def _apply_trades_to_holdings(
    holdings: list[dict], allocations: list[dict], reduce_list: list[dict],
    prices: dict, new_candidate_prices: dict,
) -> list[dict]:
    """Build a synthetic post-trade holdings list (share-count deltas at the
    latest close) so compute_portfolio_aggregate_metrics can project 'after'."""
    synthetic = [dict(h, ticker=str(h["ticker"]).upper()) for h in holdings]
    by_ticker = {h["ticker"]: h for h in synthetic}

    for a in allocations:
        price = prices.get(a["ticker"]) or new_candidate_prices.get(a["ticker"])
        if not price:
            continue
        delta_shares = a["amount"] / price
        if a["ticker"] in by_ticker:
            by_ticker[a["ticker"]]["shares"] = float(by_ticker[a["ticker"]].get("shares") or 0) + delta_shares
        else:
            new_holding = {"ticker": a["ticker"], "shares": delta_shares}
            synthetic.append(new_holding)
            by_ticker[a["ticker"]] = new_holding

    for r in reduce_list:
        price = prices.get(r["ticker"])
        h = by_ticker.get(r["ticker"])
        if not price or not h:
            continue
        delta_shares = r["suggested_trim_dollars"] / price
        h["shares"] = max(0.0, float(h.get("shares") or 0) - delta_shares)

    return synthetic


def recommend_allocation(cash_amount: float, top_n_candidates: int = 5) -> dict:
    """
    Answers "where should $cash_amount go," "which existing holding should get
    more," and "what should I trim" in one call. See module docstring for the
    data sources reused.

    Degrades gracefully: if the aggregate-metrics/risk-context yfinance calls
    fail, still returns allocation/reduce lists with impact=None rather than
    failing outright.
    """
    import json
    from portfolio_agent.tools.portfolio_tools import get_portfolio_holdings
    from portfolio_agent.tools.portfolio_risk import (
        compute_portfolio_aggregate_metrics, compute_portfolio_risk_context,
    )
    from portfolio_agent.tools.prediction_db import get_all_latest_predictions

    raw_holdings = json.loads(get_portfolio_holdings()).get("holdings", [])
    holdings = _consolidate_by_ticker(raw_holdings)
    baseline = compute_portfolio_aggregate_metrics(holdings) if holdings else None
    risk_ctx = compute_portfolio_risk_context(holdings) if holdings else {}
    latest_preds = get_all_latest_predictions()
    prices = baseline.get("prices", {}) if baseline else {}

    # Reduce list first: a ticker already flagged for concentration/SELL risk
    # is never also offered new cash in the same recommendation.
    reduce_list = _reduce_candidates(holdings, risk_ctx, latest_preds, prices) if holdings else []
    reduce_tickers = {r["ticker"] for r in reduce_list}

    existing_tickers = {str(h["ticker"]).upper() for h in holdings}
    candidates = _existing_holding_candidates(holdings, risk_ctx, latest_preds, exclude=reduce_tickers)

    # Fold in each existing candidate's current $ value so _allocate_cash can
    # cap post-trade position size correctly.
    for c in candidates:
        c["current_value"] = float(next(
            (h.get("shares") or 0 for h in holdings if str(h["ticker"]).upper() == c["ticker"]), 0
        )) * prices.get(c["ticker"], 0.0)

    new_candidates = _new_buy_candidates(existing_tickers)
    new_candidate_prices = {c["ticker"]: c["candidate_price"] for c in new_candidates}

    ranked = sorted(candidates + new_candidates, key=lambda c: c["score"], reverse=True)[:top_n_candidates]
    allocations, cash_reserved = _allocate_cash(ranked, cash_amount, baseline)

    after = None
    if baseline is not None:
        synthetic_holdings = _apply_trades_to_holdings(
            holdings, allocations, reduce_list, prices, new_candidate_prices,
        )
        after = compute_portfolio_aggregate_metrics(synthetic_holdings)

    return {
        "cash_amount": cash_amount,
        "allocation": allocations,
        "cash_reserved": cash_reserved,
        "reduce": reduce_list,
        "impact": {"before": baseline, "after": after},
    }
