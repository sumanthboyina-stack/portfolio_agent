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

# Mandate limits. These four are fallback defaults only — recommend_allocation()
# reads the live values from the user_profile table (user_profile_db) and passes
# them down explicitly; the module constants keep every other call site working.
_SECTOR_CONCENTRATION_THRESHOLD = 25.0   # % of portfolio in one sector -> reduce candidate
_ISSUER_CONCENTRATION_THRESHOLD = 15.0   # % of portfolio in one issuer (share classes) -> reduce candidate
_MAX_PER_CANDIDATE_PCT_OF_CASH  = 0.4    # no single allocation > 40% of the new cash
_MAX_POST_TRADE_POSITION_PCT    = 0.15   # no resulting position > 15% of post-trade total value
# Modeling/UX parameters — not part of the user mandate, stay module-level.
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


def _overweight_penalty(
    sector_pct: Optional[float],
    issuer_pct: Optional[float],
    sector_threshold: float = _SECTOR_CONCENTRATION_THRESHOLD,
    issuer_threshold: float = _ISSUER_CONCENTRATION_THRESHOLD,
) -> float:
    """0-20: an already-concentrated holding gets less new cash even with strong
    conviction — the point of new cash is diversifying, not doubling down."""
    penalty = 0.0
    if sector_pct is not None and sector_pct > sector_threshold:
        penalty += min(12.0, (sector_pct - sector_threshold) * 0.8)
    if issuer_pct is not None and issuer_pct > issuer_threshold:
        penalty += min(8.0, (issuer_pct - issuer_threshold) * 0.8)
    return penalty


def _existing_holding_candidates(
    holdings: list[dict], risk_ctx: dict, latest_preds: dict, exclude: set[str],
    sector_threshold: float = _SECTOR_CONCENTRATION_THRESHOLD,
    issuer_threshold: float = _ISSUER_CONCENTRATION_THRESHOLD,
) -> list[dict]:
    """Current holdings whose latest APEX call is BUY-classified — candidates to top up.
    exclude: tickers already flagged in the reduce list — never recommend adding to a
    position in the same breath as flagging it for concentration/SELL risk.
    sector/issuer thresholds are forwarded to _overweight_penalty."""
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
        if _classify(pred.get("recommendation") or "", composite_score, pred.get("guardrail_flags")) != "BUY":
            continue

        ctx = risk_ctx.get(ticker, {})
        score = (
            (composite_score or 0) / 10 * 70
            + correlation_diversification_bonus(ctx.get("correlation_with_portfolio"))
            - _overweight_penalty(
                ctx.get("sector_concentration_pct"), ctx.get("issuer_concentration_pct"),
                sector_threshold, issuer_threshold,
            )
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


def _new_buy_candidates(
    existing_tickers: set[str],
    sector_threshold: float = _SECTOR_CONCENTRATION_THRESHOLD,
    opportunities: list[dict] | None = None,
) -> list[dict]:
    """Today's Opportunity Engine BUY list, reshaped to the same candidate schema
    used for existing holdings so both compete on one ranked list.
    sector_threshold is forwarded to _overweight_penalty (no issuer data for new buys).
    Pass *opportunities* (already fetched) to keep this step free of I/O."""
    from portfolio_agent.tools.portfolio_risk import correlation_diversification_bonus

    if opportunities is None:
        from portfolio_agent.tools.opportunity_engine import get_daily_opportunities
        opportunities = get_daily_opportunities(top_n=15)

    candidates = []
    for opp in opportunities:
        if opp["call"] != "BUY" or opp["ticker"] in existing_tickers:
            continue
        impact = opp.get("portfolio_impact") or {}
        if "candidate_price" not in impact:
            continue  # can't size a $ allocation without a price

        score = (
            (opp["composite_score"] or 0) / 10 * 70
            + correlation_diversification_bonus(impact.get("correlation_with_portfolio"))
            - _overweight_penalty(
                impact.get("candidate_sector_before_pct"), None, sector_threshold=sector_threshold,
            )
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


def _allocate_cash(
    candidates: list[dict],
    cash_amount: float,
    baseline: Optional[dict],
    max_per_candidate_pct_of_cash: float = _MAX_PER_CANDIDATE_PCT_OF_CASH,
    max_post_trade_position_pct: float = _MAX_POST_TRADE_POSITION_PCT,
) -> tuple[list[dict], float]:
    """
    Greedy proportional allocation: each qualifying candidate's target share of
    cash is proportional to its score among the ranked pool, capped so no single
    allocation is oversized relative to the new cash (max_per_candidate_pct_of_cash)
    or the resulting position (max_post_trade_position_pct of post-trade total).
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
            position_cap = max(0.0, max_post_trade_position_pct * post_trade_total - current_value)

        cap = min(cash_amount * max_per_candidate_pct_of_cash, position_cap, remaining)
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


def _reduce_candidates(
    holdings: list[dict],
    risk_ctx: dict,
    latest_preds: dict,
    prices: dict,
    sector_threshold: float = _SECTOR_CONCENTRATION_THRESHOLD,
    issuer_threshold: float = _ISSUER_CONCENTRATION_THRESHOLD,
) -> list[dict]:
    """Current holdings worth trimming: a SELL/STRONG_SELL call, or concentration
    (sector or issuer) past the given thresholds. Suggested trim is a simple
    proportional pull-back, not a precise target-weight solve."""
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
        if sector_pct is not None and sector_pct > sector_threshold:
            reasons.append(f"{sector_pct:.1f}% of portfolio is in this ticker's sector (>{sector_threshold:.0f}% threshold)")
            trim_pct = max(trim_pct, min(50.0, (sector_pct - sector_threshold) / sector_pct * 100))

        issuer_pct = ctx.get("issuer_concentration_pct")
        has_issuer_peers = bool(ctx.get("issuer_peers"))
        if issuer_pct is not None and issuer_pct > issuer_threshold:
            # issuer_concentration_pct with no issuer_peers just IS this ticker's own
            # portfolio weight — phrase it as position size, not share-class overlap.
            label = (
                f"across share classes with {', '.join(ctx['issuer_peers'])}"
                if has_issuer_peers else "in this single position"
            )
            reasons.append(f"{issuer_pct:.1f}% of portfolio is {label} (>{issuer_threshold:.0f}% threshold)")
            trim_pct = max(trim_pct, min(50.0, (issuer_pct - issuer_threshold) / issuer_pct * 100))

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


def recommend_allocation(cash_amount: float, top_n_candidates: int = 5,
                         holdings: list[dict] | None = None, scope: str = "all") -> dict:
    """
    Compatibility entry point: prepare frozen inputs → propose → evaluate, and
    return the legacy {allocation, reduce, cash_reserved, impact} shape built
    from the EVALUATED trades (rounded down to whole shares, every constraint
    checked), plus the full scenario under "scenario" and its pinned inputs
    under "inputs". See portfolio_agent.scenarios.

    "impact" keeps the live before/after risk projection (beta, volatility,
    correlation) from portfolio_risk — a market-fetched estimate, not part of
    the deterministic result; the engine's own pinned estimates are in
    scenario["risk"].
    """
    import json
    from portfolio_agent.scenarios.engine import evaluate
    from portfolio_agent.scenarios.prepare import prepare_inputs
    from portfolio_agent.scenarios.propose import propose_trades
    from portfolio_agent.tools.portfolio_risk import compute_portfolio_aggregate_metrics
    from portfolio_agent.tools.portfolio_tools import get_portfolio_holdings

    raw_holdings = ([dict(h) for h in holdings] if holdings is not None
                    else json.loads(get_portfolio_holdings()).get("holdings", []))
    inputs, ctx = prepare_inputs(scope=scope, holdings=raw_holdings, contribution=cash_amount)
    trades = propose_trades(inputs, ctx, top_n=top_n_candidates)
    result = evaluate(inputs, trades)

    held = {p.ticker for p in inputs.positions}
    allocation, reduce_by_ticker = [], {}
    for et in result.trades:
        t = et.trade
        if t.side == "BUY":
            allocation.append({"ticker": t.ticker, "kind": t.meta.get("kind", "existing" if t.ticker in held else "new"),
                               "amount": float(et.gross), "shares": float(et.quantity), "why": t.why or "",
                               "composite_score": t.meta.get("composite_score"),
                               "recommendation": t.meta.get("recommendation")})
        else:
            r = reduce_by_ticker.setdefault(t.ticker, {"ticker": t.ticker, "rationale": t.why or "",
                                                        "suggested_trim_dollars": 0.0, "shares": 0.0})
            r["suggested_trim_dollars"] = round(r["suggested_trim_dollars"] + float(et.gross), 2)
            r["shares"] = round(r["shares"] + float(et.quantity), 4)
    reduce_list = list(reduce_by_ticker.values())
    cash_reserved = round(float(cash_amount) - sum(a["amount"] for a in allocation), 2)

    baseline = ctx.get("baseline")
    after = None
    if baseline is not None:
        synthetic = _apply_trades_to_holdings(ctx["consolidated"], allocation, reduce_list,
                                              {t: float(p) for t, p in inputs.prices.items()}, {})
        try:
            after = compute_portfolio_aggregate_metrics(synthetic)
        except Exception:
            after = None

    return {
        "cash_amount": cash_amount,
        "allocation": allocation,
        "cash_reserved": cash_reserved,
        "reduce": reduce_list,
        "impact": {"before": baseline, "after": after},
        "scenario": result.to_dict(),
        "inputs": inputs.to_dict(),
    }
