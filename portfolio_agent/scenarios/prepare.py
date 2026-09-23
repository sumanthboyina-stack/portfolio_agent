"""
Input preparation for rebalance scenarios — the ONLY step that touches the
database, the profile, research and the market. It freezes everything the
engine and the proposer need into ScenarioInputs (+ a proposal context) so
that evaluation is deterministic and a saved scenario can be re-evaluated or
declared stale by comparing the pinned `versions`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from portfolio_agent.scenarios.engine import (
    D, ZERO, ENGINE_VERSION, Assumptions, Limits, Position, ScenarioInputs,
)


def _consolidate(holdings: list[dict]) -> list[dict]:
    from portfolio_agent.tools.portfolio_optimizer import _consolidate_by_ticker
    return _consolidate_by_ticker(holdings)


def prepare_inputs(*, scope: str, holdings: list[dict], contribution: float | Decimal | str = 0,
                   funding_account_id: int | None = None,
                   starting_cash_overrides: dict[int, Decimal | None] | None = None,
                   assumptions: Assumptions | None = None,
                   opportunities_top_n: int = 15) -> tuple[ScenarioInputs, dict]:
    """
    holdings: account-level holding rows for the scope (dicts with account_id,
    ticker, shares, sector, current_price, price_as_of). contribution: new
    money entering `funding_account_id` (defaults to the scoped account, else
    the account with the most value — recorded as a warning). Returns
    (inputs, proposal_context); the context carries the float-world research
    the proposer scores with (baseline metrics, risk context, predictions,
    opportunities) and is not needed to evaluate.
    """
    from portfolio_agent.tools import holdings_db as repo
    from portfolio_agent.tools.db import db_conn
    from portfolio_agent.tools.opportunity_engine import get_daily_opportunities
    from portfolio_agent.tools.portfolio_risk import (
        _expected_return, compute_portfolio_aggregate_metrics, compute_portfolio_risk_context,
    )
    from portfolio_agent.tools.prediction_db import get_all_latest_predictions
    from portfolio_agent.tools.restricted_list_db import list_restricted
    from portfolio_agent.tools.user_profile_db import get_user_profile

    warnings: list[str] = []
    rows = [dict(h) for h in holdings if h.get("ticker")]
    for r in rows:
        r["ticker"] = str(r["ticker"]).upper().strip()

    # Accounts in scope, labels, cash.
    accounts = {a["account_id"]: a for a in repo.list_accounts()}
    account_ids = sorted({int(r["account_id"]) for r in rows if r.get("account_id") is not None})
    if scope.startswith("account:"):
        account_ids = sorted(set(account_ids) | {int(scope.split(":", 1)[1])})
    if not account_ids:
        raise ValueError("scenario needs at least one account in scope")
    labels = {a: (accounts.get(a, {}).get("display_name") or accounts.get(a, {}).get("account_number") or f"account {a}")
              for a in account_ids}
    cash_rows = {c["account_id"]: c for c in repo.get_cash_balances()}
    starting_cash: dict[int, Decimal | None] = {}
    for a in account_ids:
        c = cash_rows.get(a)
        starting_cash[a] = D(c["amount"]) if c and c.get("amount") is not None else None
    for a, v in (starting_cash_overrides or {}).items():
        starting_cash[int(a)] = D(v) if v is not None else None
    unknown = [labels[a] for a in account_ids if starting_cash[a] is None]
    if unknown:
        warnings.append(f"starting cash unknown for {', '.join(unknown)} (no cash line in the last import); "
                        "set it in the assumptions to check the cash constraint")

    # Profile → limits (fractions), exclusions, version.
    profile = get_user_profile()
    limits = Limits.from_profile(profile)
    excluded = frozenset(s for s in (profile.sector_exclusions or []) if s)
    restricted = frozenset(str(r["ticker"]).upper() for r in list_restricted())

    # Research: baseline metrics (gives pinned closes), per-holding risk context, predictions, opportunities.
    consolidated = _consolidate(rows)
    baseline = compute_portfolio_aggregate_metrics(consolidated) if consolidated else None
    risk_ctx = compute_portfolio_risk_context(consolidated) if consolidated else {}
    latest_preds = get_all_latest_predictions()
    held = {r["ticker"] for r in rows}
    opportunities = [o for o in get_daily_opportunities(top_n=opportunities_top_n)
                     if o.get("ticker") and str(o["ticker"]).upper() not in held]

    # Pinned prices: latest close where the market answered, else the price the last import carried.
    prices: dict[str, Decimal] = {t: D(p) for t, p in (baseline or {}).get("prices", {}).items() if p}
    fallback: list[str] = []
    for r in rows:
        t = r["ticker"]
        if t not in prices and r.get("current_price"):
            prices[t] = D(r["current_price"]); fallback.append(t)
    if fallback:
        warnings.append(f"no market close for {', '.join(sorted(set(fallback)))}; using the price from the last import")
    for o in opportunities:
        impact = o.get("portfolio_impact") or {}
        t = str(o["ticker"]).upper()
        if impact.get("candidate_price") and t not in prices:
            prices[t] = D(impact["candidate_price"])
    missing = sorted(held - set(prices))
    if missing:
        warnings.append(f"no price at all for {', '.join(missing)}; they cannot be valued or traded")

    # Sectors (holding row first, then the cached universe data — broker files often leave sector blank), issuers, expected returns.
    from portfolio_agent.tools.universe_db import get_sectors
    sector_by_ticker: dict[str, str] = {}
    for r in rows:
        if r.get("sector") and r["ticker"] not in sector_by_ticker:
            sector_by_ticker[r["ticker"]] = str(r["sector"])
    candidate_tickers = {str(o["ticker"]).upper() for o in opportunities}
    for t, sct in get_sectors(sorted((held | candidate_tickers) - set(sector_by_ticker))).items():
        if sct and sct != "Unknown":
            sector_by_ticker[t] = str(sct)
    for o in opportunities:
        impact = o.get("portfolio_impact") or {}
        s = o.get("sector") or impact.get("candidate_sector") or impact.get("sector")
        if s:
            sector_by_ticker.setdefault(str(o["ticker"]).upper(), str(s))
    issuer_by_ticker: dict[str, str] = {}
    for t, ctx in (risk_ctx or {}).items():
        peers = sorted({t, *(ctx.get("issuer_peers") or [])})
        issuer_by_ticker[t] = "+".join(peers)
    expected_returns: dict[str, Decimal] = {}
    for t in held | {str(o["ticker"]).upper() for o in opportunities}:
        er = _expected_return(latest_preds.get(t))
        if er is not None:
            expected_returns[t] = D(round(er, 4))

    # Funding account for the contribution.
    contribution = D(contribution) or ZERO
    if funding_account_id is None:
        if scope.startswith("account:"):
            funding_account_id = int(scope.split(":", 1)[1])
        else:
            value_by_acct: dict[int, Decimal] = {a: ZERO for a in account_ids}
            for r in rows:
                if r.get("account_id") is not None and r["ticker"] in prices:
                    value_by_acct[int(r["account_id"])] += D(r.get("shares") or 0) * prices[r["ticker"]]
            funding_account_id = max(value_by_acct, key=value_by_acct.get)
            if contribution > 0 and len(account_ids) > 1:
                warnings.append(f"contribution of {contribution} assigned to {labels[funding_account_id]} "
                                "(largest account); change the funding account in the assumptions if needed")
    external = {a: (contribution if a == funding_account_id else ZERO) for a in account_ids}

    with db_conn() as c:
        pred_version = str(c.execute("SELECT MAX(created_at) FROM predictions").fetchone()[0])
    versions = {
        "holdings": repo.get_holdings_version(),
        "predictions": pred_version,
        "profile": str(profile.updated_at),
        "prices": "latest-close@" + datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M"),
        "engine": ENGINE_VERSION,
    }
    positions = tuple(
        Position(int(r["account_id"]), r["ticker"], D(r.get("shares") or 0), r.get("sector") or None)
        for r in rows if r.get("account_id") is not None and (r.get("shares") or 0)
    )
    inputs = ScenarioInputs(
        scope=scope, account_ids=tuple(account_ids), account_labels=labels, positions=positions,
        prices=prices, price_as_of=datetime.now(timezone.utc).date().isoformat(),
        starting_cash=starting_cash, external_contribution=external, limits=limits,
        assumptions=assumptions or Assumptions(), restricted=restricted, excluded_sectors=excluded,
        sector_by_ticker=sector_by_ticker, issuer_by_ticker=issuer_by_ticker, expected_returns=expected_returns,
        versions=versions, prepared_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        warnings=tuple(warnings),
    )
    context = {"holdings": rows, "consolidated": consolidated, "baseline": baseline, "risk_ctx": risk_ctx,
               "latest_preds": latest_preds, "opportunities": opportunities,
               "funding_account_id": funding_account_id}
    return inputs, context
