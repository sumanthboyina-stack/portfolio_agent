"""
Health service — the single writer of Portfolio Health snapshots and the producer of "new ACTION item" notifications.

  get_health_view(ctx)            the actor's PortfolioHealth plus what changed since the last snapshot and since ~30 days
                                  ago. Takes today's snapshot lazily (first view of the day), so the page, the home card
                                  and the chat tool all keep the history going without a scheduler.
  record_health_snapshot(ctx)     the same snapshot, explicitly.
  snapshot_all_owners(...)        the evening-batch loop: one snapshot per owner with a portfolio.

Each snapshot is one transaction with its audit event (written once per day; a second view the same day refreshes the
row without a new audit event). Snapshots are skipped, with a clear status, when the global setting
health_snapshot_enabled is off — the page still shows the live health.

Notifications: a check that BECOMES ACTION (it was not ACTION in the previous snapshot) notifies once per run of ACTION
(on the household, or on a single account):
the de-duplication key is health:<check>:<date the run began> (health:<scope>:<check>:<date> for an account), so an ongoing problem does not re-notify every day and a
new one after it clears does. Only ACTION notifies — never WATCH — at severity 4, through notification_service.notify: the
person's own routing, minimum severity, quiet hours and content level decide how it goes out (a quiet-hours message is held
or dropped as they chose). This module only enqueues; the delivery worker sends.
"""
from __future__ import annotations

from datetime import date, datetime

from portfolio_agent.domain import HealthStatus
from portfolio_agent.log import get_logger as _get_logger
from portfolio_agent.services.account_service import resolve_portfolio
from portfolio_agent.services.context import RequestContext, require_context
from portfolio_agent.tools import health_snapshots_db as sdb
from portfolio_agent.tools import holdings_db as repo
from portfolio_agent.tools import investor_settings_db as settings_db
from portfolio_agent.tools import portfolio_health as ph

_log = _get_logger("services.health")

ACTION_NOTIFICATION_SEVERITY = 4


def _snapshot_for_owner(owner: str, portfolio: dict, *, actor: str, source: str, request_id: str | None,
                        health: ph.PortfolioHealth | None = None, today: date | None = None) -> dict:
    """Build (or reuse) the owner's health, write today's snapshot with its audit event, then notify. Returns
    {status: created|updated|disabled, snapshot_date, notified: [...]}."""
    today = today or date.today()
    health = health or ph.get_portfolio_health_for_owner(owner, portfolio)
    if not settings_db.get_portfolio_settings().get("health_snapshot_enabled"):
        return {"status": "disabled", "snapshot_date": today.isoformat(), "notified": []}
    sdb.ensure_schema_once()
    settings_db._ensure_once()
    payload = sdb.payload_from_health(health)
    with repo.unit_of_work() as c:
        outcome = sdb.upsert_snapshot(owner, today.isoformat(), payload, conn=c)
        if outcome == "created":
            counts = payload["scopes"]["household"]["counts"]
            repo.record_audit_event(actor=actor, source=source, request_id=request_id, operation="save_health_snapshot",
                                    portfolio_id=portfolio["portfolio_id"],
                                    after={"snapshot_date": today.isoformat(), "counts": counts, "health_version": health.health_version},
                                    conn=c)
    notified = _notify_new_actions(owner, health, today)          # after the commit: a notification never rolls a snapshot back
    return {"status": outcome, "snapshot_date": today.isoformat(), "notified": notified}


def _notify_new_actions(owner: str, health: ph.PortfolioHealth, today: date) -> list[str]:
    """Notify (once per run) for each household or single-account check that is ACTION now, through
    notification_service.notify (category PORTFOLIO_HEALTH): the person's own routing, severity, quiet hours and content level
    decide whether and how it goes out, and the outbox de-duplicates. Returns the dedup keys that were ENQUEUED on at least one
    channel. Never raises. (Tax-treatment slices are not notified separately: they are the same accounts grouped.)

    The brief text — always used for push — names the check and nothing else: no amounts, no account names. The detailed
    text, for email only and only if the person chose DETAILED, adds the full summary."""
    try:
        from portfolio_agent.services.notification_service import notify
        scopes = [health.household, *health.by_account.values()]
        actions = [(sc, c) for sc in scopes for c in sc.checks if c.status == HealthStatus.ACTION.value]
        if not actions:
            return []
        history = sdb.list_recent(owner, 90)
        sent = []
        for sc, c in actions:
            start = sdb.action_streak_start(history, c.key, today.isoformat(), c.status, scope=sc.scope)
            # household keys keep their original shape; an account's carries its scope so two accounts never collide
            key = f"health:{c.key}:{start}" if sc.scope == "household" else f"health:{sc.scope}:{c.key}:{start}"
            where = "" if sc.scope == "household" else " in one of your accounts"
            result = notify(owner, "PORTFOLIO_HEALTH", dedup_key=key, title=f"APEX: Portfolio Health - {c.title}",
                            brief=f"APEX: Portfolio Health - {c.title} needs attention{where}. Open APEX for details.",
                            detailed=f"{c.title}{'' if sc.scope == 'household' else f' ({sc.label})'}: {c.summary}",
                            severity=ACTION_NOTIFICATION_SEVERITY, link="/Portfolio_Health")
            if result.get("enqueued"):
                sent.append(key)
                _log.info(f"  [notify] Portfolio Health ACTION — {c.title} ({'household' if sc.scope == 'household' else 'account'})",
                          event_type="notification", dedup_key=key)
        return sent
    except Exception as exc:
        _log.warning(f"  [health-notify] skipped ({type(exc).__name__})", event_type="warning")
        return []


def record_health_snapshot(ctx: RequestContext) -> dict:
    """Take (or refresh) today's snapshot of the actor's own portfolio."""
    require_context(ctx)
    portfolio = resolve_portfolio(ctx)
    return _snapshot_for_owner(ctx.actor, portfolio, actor=ctx.actor, source=ctx.source, request_id=ctx.request_id)


def get_health_view(ctx: RequestContext, *, take_snapshot: bool = True) -> dict:
    """{health, snapshot: {...}, vs_last: diff, vs_30d: diff|None, last_date, thirty_days_date}. Owner-scoped."""
    require_context(ctx)
    portfolio = resolve_portfolio(ctx)
    health = ph.get_portfolio_health_for_owner(ctx.actor, portfolio)
    snap = (_snapshot_for_owner(ctx.actor, portfolio, actor=ctx.actor, source=ctx.source, request_id=ctx.request_id, health=health)
            if take_snapshot else {"status": "skipped", "snapshot_date": health.as_of, "notified": []})
    return _view(ctx.actor, health, snap)


def get_health_view_for_owner(owner: str, *, take_snapshot: bool = False) -> dict | None:
    """The same view for a server-side caller that holds a trusted owner string and no RequestContext (the chat tool, whose
    owner the web layer took from the verified login). READ-ONLY by default. None if the owner has no portfolio."""
    portfolio = repo.get_portfolio_by_owner(owner)
    if portfolio is None:
        return None
    health = ph.get_portfolio_health_for_owner(owner, portfolio)
    snap = (_snapshot_for_owner(owner, portfolio, actor=owner, source="chat", request_id=None, health=health)
            if take_snapshot else {"status": "skipped", "snapshot_date": health.as_of, "notified": []})
    return _view(owner, health, snap)


def _view(owner: str, health: ph.PortfolioHealth, snap: dict) -> dict:
    sdb.ensure_schema_once()
    cur = sdb.payload_from_health(health)
    today = health.as_of
    last = sdb.latest_before(owner, today)
    old = sdb.thirty_days_ago(owner, today)
    return {"health": health, "snapshot": snap, "payload": cur,
            "vs_last": sdb.diff_payloads(last["payload"] if last else None, cur), "last_date": last["snapshot_date"] if last else None,
            "vs_30d": sdb.diff_payloads(old["payload"], cur) if old else None, "thirty_days_date": old["snapshot_date"] if old else None}


def snapshot_all_owners(*, today: date | None = None) -> dict:
    """Evening batch: one snapshot per owner with a portfolio. A failure for one owner never stops the others."""
    out = {"created": 0, "updated": 0, "disabled": 0, "failed": 0}
    for owner in repo.list_portfolio_owners():
        try:
            portfolio = repo.get_portfolio_by_owner(owner)
            r = _snapshot_for_owner(owner, portfolio, actor=f"system:health", source="batch:evening", request_id=None, today=today)
            out[r["status"]] += 1
        except Exception as exc:
            out["failed"] += 1
            _log.warning(f"  [health] snapshot failed for {owner}: {type(exc).__name__}: {exc}", event_type="warning")
    return out
