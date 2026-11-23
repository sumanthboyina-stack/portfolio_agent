"""
Position view service — the single writer of stored position views.

  snapshot_all_owners(today)   the morning-batch phase: for each owner with a portfolio, build the view of every ticker they
                               hold (their own exposures and policy, computed once) and store it for the day.
  get_latest_view(ctx, ticker) the actor's newest stored view of one ticker (owner-scoped).

Each owner's rows are written in one transaction with one audit event (written once per owner per day; a re-run the same
day refreshes the rows without a new audit event). A failure for one ticker or owner never stops the others. This module
reads shared forecasts and the owner's own rows only; it sends nothing.
"""
from __future__ import annotations

from datetime import date

from portfolio_agent.log import get_logger as _get_logger
from portfolio_agent.services.account_service import resolve_portfolio
from portfolio_agent.services.context import RequestContext, require_context
from portfolio_agent.tools import holdings_db as repo
from portfolio_agent.tools import position_views_db as pdb
from portfolio_agent.tools.position_view import build_position_view_for_owner

_log = _get_logger("services.position_view")


def _snapshot_owner(owner: str, today: date, *, actor: str, source: str) -> dict:
    from portfolio_agent.tools.portfolio_assessment import compute_exposures_for_owner
    from portfolio_agent.tools.portfolio_policy import get_effective_policy_for_owner
    portfolio = repo.get_portfolio_by_owner(owner)
    if portfolio is None:
        return {"created": 0, "updated": 0, "failed": 0, "skipped": 0}
    policy = get_effective_policy_for_owner(owner)
    exposures = compute_exposures_for_owner(owner, portfolio, policy=policy)
    out = {"created": 0, "updated": 0, "failed": 0, "skipped": 0}
    views = []
    for ticker in sorted(exposures.per_holding_value):
        try:
            view = build_position_view_for_owner(owner, ticker, exposures=exposures, policy=policy, today=today)
        except Exception as exc:
            out["failed"] += 1
            _log.warning(f"  [position_view] {ticker} failed for one owner: {type(exc).__name__}", event_type="warning")
            continue
        if view is None:
            out["skipped"] += 1
            continue
        views.append((ticker, view))
    if not views:
        return out
    pdb.ensure_schema_once()
    with repo.unit_of_work() as c:
        for ticker, view in views:
            out[pdb.upsert_view(owner, ticker, today.isoformat(), view, conn=c)] += 1
        if out["created"]:
            repo.record_audit_event(actor=actor, source=source, request_id=None, operation="save_position_views",
                                    portfolio_id=portfolio["portfolio_id"],
                                    after={"view_date": today.isoformat(), "tickers": [t for t, _ in views]}, conn=c)
    return out


def snapshot_all_owners(*, today: date | None = None) -> dict:
    """Morning batch: store today's position views for every owner with a portfolio."""
    today = today or date.today()
    totals = {"owners": 0, "created": 0, "updated": 0, "failed": 0, "skipped": 0}
    for owner in repo.list_portfolio_owners():
        try:
            r = _snapshot_owner(owner, today, actor="system:position_views", source="batch:morning")
            totals["owners"] += 1
            for k in ("created", "updated", "failed", "skipped"):
                totals[k] += r[k]
        except Exception as exc:
            totals["failed"] += 1
            _log.warning(f"  [position_view] owner failed: {type(exc).__name__}: {exc}", event_type="warning")
    return totals


def get_latest_view(ctx: RequestContext, ticker: str) -> dict | None:
    """The actor's own newest stored view of *ticker*, or None. NotAuthorized for an actor with no portfolio."""
    require_context(ctx)
    resolve_portfolio(ctx)
    return pdb.get_latest(ctx.actor, ticker)
