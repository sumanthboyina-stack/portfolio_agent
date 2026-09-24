"""
Event detector — identifies market events that should trigger predictions.

Each detector reads existing data sources (news_filter_log, SEC filings, etc.)
and writes rows to trigger_events via events/db.py.  No new LLM calls are made
here — detectors are pure data readers + writers.

Detectors:
  detect_material_news(tickers, today, config)  — reads news_filter_log stage_3_score
  detect_earnings(tickers, today, config)        — stub (future: earnings calendar API)
  detect_sec_8k(tickers, today, config)          — stub (future: SEC EDGAR RSS)
  detect_rating_changes(tickers, today, config)  — stub (future: Finnhub upgrades)
  detect_macro_events(today, config)             — stub (future: macro calendar API)

run_all_detectors(tickers, today, config) — convenience wrapper.

Notifications: every detected event is also offered to _maybe_notify(), which
applies the user's personal filter (user_notifications: channel, min severity,
min conviction, quiet hours) via scheduler.should_notify_trigger. The pipeline
run triggered by the event is unaffected by that filter — only whether a
notification is emitted.

Delivery is private and deduplicated (tools.notification_log_db) by
(owner, this exact event, channel) — a detector re-run (self-healed missed
morning, an intraday re-scan that rediscovers the same trigger_events row)
never re-delivers the same notification. Delivery is enriched with the
ticker's current private ActionPermission (tools.clearance) as INFORMATION
ONLY — never as a filter: a blacked-out or restricted-list ticker's event
still notifies if the user holds it or is watching it, because monitoring an
existing position is not the same thing as being allowed to trade it (a
blackout must not disable monitoring). The enrichment just says plainly
that no new trade is currently permitted, alongside the event itself.

Delivery is a structured "notification" log record carrying the channel —
the single hook where a real email/slack sender attaches (_emit()).
"""

from __future__ import annotations

from portfolio_agent.log import get_logger as _get_logger

_log = _get_logger("events.detector")


def _action_permission_note(ticker: str) -> str:
    """Best-effort, informational only — never gates delivery (see module docstring)."""
    try:
        from portfolio_agent.clearance import evaluate_clearance
        from portfolio_agent.domain import UserProfile
        from portfolio_agent.tools.user_profile_db import get_user_profile
        try:
            profile = get_user_profile()
        except Exception:
            profile = UserProfile()
        decision = evaluate_clearance(ticker, profile)
        if decision.decision == "ALLOWED_BY_RULES":
            return ""
        return f" [{decision.decision}: no new trade currently permitted]"
    except Exception:
        return ""   # never let enrichment break delivery


def _emit(channel: str, event: dict) -> None:
    """Deliver one notification. Currently a structured log record tagged with
    the channel — the single hook where a real email/slack sender attaches."""
    summary = (event.get("summary") or "")[:120]
    note = _action_permission_note(event["ticker"])
    _log.info(
        f"  [notify:{channel}] {event['ticker']} {event['event_type']} "
        f"severity={event.get('severity')}: {summary}{note}",
        event_type="notification", channel=channel,
        ticker=event["ticker"], trigger_event_id=event.get("id"),
    )


def _maybe_notify(event: dict) -> bool:
    """
    Personal notification filter for one detected event. Returns True when a
    notification was emitted. Never raises — a failure here must not stop the
    detector from returning its events to the pipeline.
    """
    from portfolio_agent.events.scheduler import should_notify_trigger
    try:
        ok, detail = should_notify_trigger(
            f"event_{event['event_type']}",
            severity=event.get("severity"),
            conviction=event.get("conviction"),
        )
    except Exception as exc:
        _log.warning(f"  [notify] filter failed for {event.get('ticker')}: {exc}",
                     event_type="warning")
        return False
    if not ok:
        _log.info(f"  [notify] {event['ticker']} suppressed — {detail}",
                  event_type="notification_suppressed", ticker=event["ticker"])
        return False

    channel = detail
    event_id = event.get("id")
    if event_id is not None:
        from portfolio_agent.services.context import system_context
        from portfolio_agent.tools.notification_log_db import record_if_new
        owner_scope = f"user:{system_context('events.detector').actor}"
        dedup_key = f"event:{event_id}"
        try:
            is_new = record_if_new(owner_scope, dedup_key, channel)
        except Exception as exc:
            _log.warning(f"  [notify] dedup check failed for {event['ticker']}: {exc} — delivering anyway",
                         event_type="warning")
            is_new = True
        if not is_new:
            _log.info(f"  [notify] {event['ticker']} already delivered — {dedup_key}/{channel}",
                      event_type="notification_suppressed", ticker=event["ticker"])
            return False

    _emit(channel, event)
    return True
