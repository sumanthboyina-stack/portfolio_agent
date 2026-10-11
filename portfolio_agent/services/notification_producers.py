"""
Scheduled notification producers — things that are not triggered by one event but by the clock.

    run_scheduled_producers()    compliance expiry scan + the daily digests; safe to call as often as you like

  * Compliance: every ACTIVE trade approval that expires within 24 hours is announced to its owner, once
    (services.compliance_notifications.scan_expiring_approvals).
  * Daily digest (category DAILY_DIGEST, EMAIL only, OFF by default): at each person's digest_time (in THEIR timezone) one email
    rolling up what is worth knowing since yesterday — the Portfolio Health checks that need ACTION or a WATCH, the market
    events on tickers they hold or watch, and anything held back by the hourly message limit. It is skipped when there is
    nothing to say, and sent at most once per person per local day (dedup key digest:<local date>; a skipped empty day leaves
    a suppressed marker row so the every-few-minutes cron does not rebuild it).

The BRIEF digest names checks, tickers and events — never an amount, a position size or a portfolio value. The DETAILED
digest (email, only if the person chose DETAILED) adds each check's full summary. The text is informational and never a
trade instruction. Nothing here sends anything: it enqueues, and the delivery worker sends. Nothing here raises.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from portfolio_agent.domain import AlertCategory, Channel, HealthStatus
from portfolio_agent.log import get_logger as _get_logger
from portfolio_agent.services import compliance_notifications as compliance
from portfolio_agent.services import notification_service as ns
from portfolio_agent.tools import holdings_db as repo
from portfolio_agent.tools import notification_outbox_db as obx
from portfolio_agent.tools import user_notifications_db as udb

_log = _get_logger("services.notification_producers")

DIGEST_WINDOW_HOURS = 24
EMPTY_DIGEST = "EMPTY_DIGEST"
_CST = ZoneInfo("America/Chicago")                    # events.db stores detected_at in this zone
_ACT, _WAT = HealthStatus.ACTION.value, HealthStatus.WATCH.value


def _tz_of(owner: str) -> ZoneInfo:
    try:
        from portfolio_agent.tools.user_profile_db import get_user_profile_by_owner
        return ZoneInfo(get_user_profile_by_owner(owner).timezone or "America/Chicago")
    except Exception:
        return _CST


def digest_content(owner: str, since_utc: datetime) -> dict:
    """What a digest for *owner* would say: {"empty", "brief", "detailed", "health", "events", "folded"}. Pure reads."""
    from portfolio_agent.events.db import list_events_since
    from portfolio_agent.tools import portfolio_health as ph
    from portfolio_agent.tools.watchlist_db import list_tickers_by_owner

    portfolio = repo.get_portfolio_by_owner(owner)
    health_rows, tickers = [], set(list_tickers_by_owner(owner))
    if portfolio is not None:
        tickers |= {h.ticker for h in repo.get_holdings(portfolio_id=portfolio["portfolio_id"])}
        try:
            hh = ph.get_portfolio_health_for_owner(owner, portfolio).household
            health_rows = [c for c in hh.checks if c.status in (_ACT, _WAT)]
        except Exception as exc:
            _log.warning(f"  [digest] health unavailable for one owner ({type(exc).__name__})", event_type="warning")
    events = [e for e in list_events_since(since_utc.astimezone(_CST).isoformat(timespec="seconds")) if e["ticker"] in tickers]
    folded = obx.digest_candidates(owner, since_utc)
    act = [c for c in health_rows if c.status == _ACT]
    wat = [c for c in health_rows if c.status == _WAT]
    if not (health_rows or events or folded):
        return {"empty": True, "brief": "", "detailed": "", "health": [], "events": [], "folded": []}

    brief = ["APEX daily digest", ""]
    if health_rows:
        brief.append(f"Portfolio Health: {len(act)} need action, {len(wat)} to watch.")
        brief += [f"- {c.title} ({'action' if c.status == _ACT else 'watch'})" for c in health_rows]
        brief.append("")
    if events:
        brief.append("Market events on tickers you hold or watch:")
        brief += [f"- {e['ticker']}: {str(e['event_type']).replace('_', ' ')} (severity {e['severity']})" for e in events]
        brief.append("")
    if folded:
        brief.append("Held back earlier because of the hourly message limit:")
        brief += [f"- {r['subject']}" for r in folded]
        brief.append("")
    brief.append("Open APEX for details. Informational only — not investment advice.")
    detailed = list(brief[:-1])
    if health_rows:
        detailed += ["Portfolio Health details:"] + [f"- {c.title}: {c.summary}" for c in health_rows] + [""]
    if events:
        detailed += ["Event summaries:"] + [f"- {e['ticker']}: {(e.get('summary') or '')[:200]}" for e in events] + [""]
    detailed.append(brief[-1])
    return {"empty": False, "brief": "\n".join(brief), "detailed": "\n".join(detailed),
            "health": [c.key for c in health_rows], "events": [e["id"] for e in events], "folded": [r["id"] for r in folded]}


def _digest_for_owner(owner: str, now_utc: datetime) -> str:
    prefs = udb.get_user_notifications_by_owner(owner)
    local = now_utc.astimezone(_tz_of(owner))
    due_at = udb._parse_hhmm(prefs.digest_time) or time(7, 30)
    if local.time() < due_at:
        return "not_due"
    key = f"digest:{local.date().isoformat()}"
    if obx.exists(owner, key):
        return "already"
    if ns.explain_channels(owner, AlertCategory.DAILY_DIGEST.value).get(Channel.EMAIL.value) != ns.DELIVERABLE:
        return "not_deliverable"
    content = digest_content(owner, now_utc - timedelta(hours=DIGEST_WINDOW_HOURS))
    if content["empty"]:
        obx.enqueue(owner, AlertCategory.DAILY_DIGEST.value, Channel.EMAIL.value, dedup_key=key, subject="APEX daily digest",
                    body_brief="(nothing to report)", status=obx.SUPPRESSED, reason_code=EMPTY_DIGEST,
                    created_at=obx.iso(now_utc))
        return "empty"
    result = ns.notify(owner, AlertCategory.DAILY_DIGEST.value, dedup_key=key, title="APEX daily digest", brief=content["brief"],
                       detailed=content["detailed"], severity=None, link="/Portfolio_Health", now=local)
    if result.get("enqueued"):
        obx.mark_digested(content["folded"])
        return "created"
    return "failed" if result.get("error") else "not_deliverable"


def build_daily_digests(now: datetime | None = None) -> dict:
    """One pass over every owner. Returns counts by outcome: created / empty / not_due / already / not_deliverable / failed."""
    now = now or datetime.now(timezone.utc)
    out = {"created": 0, "empty": 0, "not_due": 0, "already": 0, "not_deliverable": 0, "failed": 0}
    try:
        owners = repo.list_portfolio_owners()
    except Exception as exc:
        _log.warning(f"  [digest] could not list owners ({type(exc).__name__})", event_type="warning")
        return out
    for owner in owners:
        try:
            out[_digest_for_owner(owner, now)] += 1
        except Exception as exc:                                  # one person's problem never blocks the others
            out["failed"] += 1
            _log.warning(f"  [digest] one owner skipped ({type(exc).__name__})", event_type="warning")
    return out


def run_scheduled_producers(now: datetime | None = None) -> dict:
    """The clock-driven producers, in order. Never raises."""
    out: dict = {"compliance": {}, "digest": {}}
    try:
        out["compliance"] = compliance.scan_expiring_approvals(now=now)
        out["digest"] = build_daily_digests(now)
    except Exception as exc:
        _log.warning(f"  [notifications] scheduled producers failed ({type(exc).__name__})", event_type="warning")
    return out
