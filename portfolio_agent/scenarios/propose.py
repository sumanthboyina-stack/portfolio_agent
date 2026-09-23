"""
Trade proposal — turns prepared research into candidate trades. This is the
"suggest possibilities" step; it never decides feasibility. Its output goes
through engine.evaluate() like any user-selected trade, which rounds
quantities down and validates every constraint on the rounded book.

Pure given (inputs, context): all scoring reuses the optimizer's ranking
helpers on the float research the context carries. Dollar suggestions become
`amount` trades; the engine converts them to whole shares.
"""

from __future__ import annotations

from decimal import Decimal

from portfolio_agent.scenarios.engine import D, ZERO, ScenarioInputs, Trade


def propose_trades(inputs: ScenarioInputs, context: dict, *, top_n: int = 5) -> list[Trade]:
    from portfolio_agent.tools.portfolio_optimizer import (
        _allocate_cash, _existing_holding_candidates, _new_buy_candidates, _reduce_candidates,
    )
    consolidated = context.get("consolidated") or []
    baseline = context.get("baseline")
    risk_ctx = context.get("risk_ctx") or {}
    preds = context.get("latest_preds") or {}
    prices_f = {t: float(p) for t, p in inputs.prices.items()}
    sector_pct = float(inputs.limits.max_sector * 100)
    issuer_pct = float(inputs.limits.max_issuer * 100)
    funding = context.get("funding_account_id") or inputs.account_ids[0]
    contribution = inputs.external_contribution.get(funding, ZERO)

    trades: list[Trade] = []

    # Trims: split a consolidated trim across the accounts holding the ticker, by shares.
    reduce_list = _reduce_candidates(consolidated, risk_ctx, preds, prices_f,
                                     sector_threshold=sector_pct, issuer_threshold=issuer_pct) if consolidated else []
    reduce_tickers = {r["ticker"] for r in reduce_list}
    for r in reduce_list:
        holders = [p for p in inputs.positions if p.ticker == r["ticker"] and p.shares > 0]
        total = sum((p.shares for p in holders), ZERO)
        if not total or r["suggested_trim_dollars"] <= 0:
            continue
        for p in holders:
            share = D(r["suggested_trim_dollars"]) * p.shares / total
            if share > 0:
                trades.append(Trade(p.account_id, p.ticker, "SELL", amount=share, source="proposed",
                                    why=r["rationale"], meta={"kind": "reduce"}))

    # Buys: existing BUY-rated holdings and new opportunities compete on one list.
    existing = _existing_holding_candidates(consolidated, risk_ctx, preds, exclude=reduce_tickers,
                                            sector_threshold=sector_pct, issuer_threshold=issuer_pct) if consolidated else []
    for c in existing:
        c["current_value"] = float(next((h.get("shares") or 0 for h in consolidated
                                         if str(h["ticker"]).upper() == c["ticker"]), 0)) * prices_f.get(c["ticker"], 0.0)
    new = _new_buy_candidates({p.ticker for p in inputs.positions}, sector_threshold=sector_pct,
                              opportunities=context.get("opportunities") or [])
    ranked = sorted(existing + new, key=lambda c: c["score"], reverse=True)[:top_n]
    allocations, _ = _allocate_cash(
        ranked, float(contribution), baseline,
        max_per_candidate_pct_of_cash=float(inputs.limits.max_per_trade_of_contribution),
        max_post_trade_position_pct=float(inputs.limits.max_post_trade_position),
    ) if contribution > 0 else ([], 0.0)
    for a in allocations:
        trades.append(Trade(funding, a["ticker"], "BUY", amount=D(a["amount"]), source="proposed", why=a["why"],
                            meta={"kind": a["kind"], "composite_score": a["composite_score"],
                                  "recommendation": a["recommendation"]}))
    return trades
