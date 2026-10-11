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

Notifications: every detected event is offered to _maybe_notify(), which fans it out to EVERY owner who holds the ticker
(real, non-paper accounts) or watches it. Each owner is judged by their own settings — notification routing, minimum severity,
quiet hours in their own timezone, content level — through services.notification_service.notify, which only ENQUEUES; the
delivery worker sends. The pipeline run triggered by the event is unaffected by any of this.

De-duplication is per owner, per event id (dedup key event:<trigger_event_id>) and per channel, enforced by the notification
outbox, so a detector re-run (self-healed missed morning, an intraday re-scan that rediscovers the same trigger_events row)
never queues the same notification twice. Each notification is enriched with THAT owner's private ActionPermission
(tools.clearance) as INFORMATION ONLY — never as a filter: a blacked-out or restricted-list ticker's event still notifies if
the owner holds it or is watching it, because monitoring an existing position is not the same thing as being allowed to
trade it. The text just says plainly that no new trade is currently permitted, alongside the event itself.

The brief text (always used for push) has no amounts, position sizes or portfolio values. Notification text is informational
and never a trade instruction.
"""

from __future__ import annotations

from portfolio_agent.log import get_logger as _get_logger

_log = _get_logger("events.detector")


def _affected_owners(ticker: str) -> dict[str, set[str]]:
    """{owner: {"hold" and/or "watch"}} — everyone who HOLDS the ticker in a real (non-paper) account or WATCHES it.
    Nobody else is ever told: an event on a ticker you neither own nor watch is not yours to hear about."""
    from portfolio_agent.tools.holdings_db import list_owners_holding
    from portfolio_agent.tools.watchlist_db import list_owners_watching
    owners: dict[str, set[str]] = {}
    for kind, getter in (("hold", list_owners_holding), ("watch", list_owners_watching)):
        try:
            for owner in getter(ticker):
                owners.setdefault(owner, set()).add(kind)
        except Exception as exc:
            _log.warning(f"  [notify] could not list who {kind}s {ticker}: {type(exc).__name__}", event_type="warning")
    return owners


def _action_permission_note(ticker: str, owner: str | None = None) -> str:
    """Best-effort, informational only — never gates delivery (see module docstring). Evaluated for THIS owner: their own
    restricted list, approvals and profile (the old version evaluated the local owner's for everyone)."""
    try:
        from portfolio_agent.clearance import evaluate_clearance
        from portfolio_agent.domain import UserProfile
        from portfolio_agent.tools.user_profile_db import get_user_profile, get_user_profile_by_owner
        try:
            profile = get_user_profile_by_owner(owner) if owner else get_user_profile()
        except Exception:
            profile = UserProfile()
        decision = evaluate_clearance(ticker, profile, owner_scope=f"user:{owner}" if owner else None)
        if decision.decision == "ALLOWED_BY_RULES":
            return ""
        return f" [{decision.decision}: no new trade currently permitted]"
    except Exception:
        return ""   # never let enrichment break delivery


def _emit(owner: str, event: dict, note: str = "", relation: set[str] | None = None) -> dict:
    """Deliver one notification to ONE owner: write the structured log record (as before) and hand the message to
    notification_service.notify, which applies that person's own filters and routing and enqueues it for the delivery worker.
    The brief text carries no amounts or position sizes (it may sit on a lock screen); the detailed text, for email, adds the
    event's summary. Never raises."""
    from portfolio_agent.services.notification_service import notify
    ticker, severity = event["ticker"], event.get("severity")
    summary = (event.get("summary") or "")[:300]
    label = str(event["event_type"]).replace("_", " ")
    _log.info(
        f"  [notify] {ticker} {event['event_type']} severity={severity}: {summary[:120]}{note}",
        event_type="notification", ticker=ticker, trigger_event_id=event.get("id"),
    )
    brief = f"APEX: {ticker} - {label} (severity {severity}).{note} Open APEX for details."
    why = " and ".join(sorted({"hold": "you hold it", "watch": "you watch it"}[k] for k in (relation or set()) if k in ("hold", "watch")))
    detailed = f"{brief}\n\n{summary}" + (f"\n\nYou are getting this because {why}." if why else "")
    return notify(owner, "MARKET_EVENT", dedup_key=f"event:{event['id']}", title=f"APEX: {ticker} - {label}", brief=brief,
                  detailed=detailed, severity=severity, link="/Predictions")


def _maybe_notify(event: dict) -> bool:
    """
    Offer one detected event to every owner who holds or watches its ticker. Each owner's own preferences, routing,
    quiet hours and per-event dedup (event:<trigger_event_id>) apply — nothing is shared between owners, and a ticker nobody
    holds or watches notifies nobody. Returns True when at least one notification was enqueued. Never raises: a failure
    here must not stop the detector from returning its events to the pipeline.
    """
    if event.get("id") is None:
        return False
    try:
        enqueued = False
        for owner, relation in _affected_owners(event["ticker"]).items():
            try:
                result = _emit(owner, event, _action_permission_note(event["ticker"], owner), relation)
                enqueued = enqueued or bool(result.get("enqueued"))
            except Exception as exc:
                _log.warning(f"  [notify] event {event.get('id')} not offered to one owner ({type(exc).__name__})", event_type="warning")
        return enqueued
    except Exception as exc:
        _log.warning(f"  [notify] event {event.get('id')} not offered ({type(exc).__name__})", event_type="warning")
        return False


def detect_material_news(
    tickers: list[str],
    today: str,
    config: dict,
) -> list[dict]:
    """
    Read today's news_filter_log rows and emit trigger_events for any ticker
    whose stage_3_score >= intraday_severity_threshold.

    Returns list of event dicts with keys: ticker, event_type, severity, source, summary.
    """
    from portfolio_agent.tools.db import db_conn
    from portfolio_agent.events.db import insert_event

    threshold = config.get("intraday_severity_threshold", 3)
    if not config.get("detectors", {}).get("material_news", True):
        return []

    events: list[dict] = []
    try:
        with db_conn() as c:
            rows = c.execute(
                """SELECT ticker, stage_3_score, stage_3_summary, final_decision
                   FROM news_filter_log
                   WHERE as_of_date = ?
                   AND stage_3_score IS NOT NULL
                   AND stage_3_score >= ?
                   AND final_decision = 'analyzed'""",
                [today, threshold],
            ).fetchall()
    except Exception as exc:
        _log.warning(f"  [events] news_filter_log query failed: {exc}", event_type="warning")
        return []

    for row in rows:
        ticker = row["ticker"]
        if ticker not in {t.upper() for t in tickers}:
            continue
        score = row["stage_3_score"]
        summary = row["stage_3_summary"] or f"Material news score={score}"
        ev_id = insert_event(
            ticker=ticker,
            event_type="material_news",
            severity=int(score),
            source="news_filter_log",
            summary=summary,
        )
        event = {
            "id": ev_id,
            "ticker": ticker,
            "event_type": "material_news",
            "severity": int(score),
            "source": "news_filter_log",
            "summary": summary,
        }
        events.append(event)
        _log.info(
            f"  [event] {ticker} material_news severity={score}: {summary[:60]}",
            event_type="event_detected", ticker=ticker,
        )
        _maybe_notify(event)

    return events


def detect_earnings(tickers: list[str], today: str, config: dict) -> list[dict]:
    """Stub — future: query earnings calendar API for surprise announcements."""
    if not config.get("detectors", {}).get("earnings", True):
        return []
    return []


def detect_sec_8k(tickers: list[str], today: str, config: dict) -> list[dict]:
    """Stub — future: poll SEC EDGAR RSS feed for 8-K filings."""
    if not config.get("detectors", {}).get("sec_8k", True):
        return []
    return []


def detect_rating_changes(tickers: list[str], today: str, config: dict) -> list[dict]:
    """Stub — future: Finnhub upgrade/downgrade endpoint."""
    if not config.get("detectors", {}).get("rating_changes", True):
        return []
    return []


def detect_macro_events(today: str, config: dict) -> list[dict]:
    """Stub — future: macro economic calendar (FOMC, CPI, NFP)."""
    if not config.get("detectors", {}).get("macro_events", True):
        return []
    return []


def run_all_detectors(
    tickers: list[str],
    today: str,
    config: dict,
) -> list[dict]:
    """
    Run all enabled detectors and return the combined list of detected events.
    Events are also persisted to trigger_events via insert_event().
    """
    if not config.get("enabled", True):
        _log.info("  [events] event_driven disabled in config — skipping detectors",
                  event_type="info")
        return []

    all_events: list[dict] = []
    all_events.extend(detect_material_news(tickers, today, config))
    all_events.extend(detect_earnings(tickers, today, config))
    all_events.extend(detect_sec_8k(tickers, today, config))
    all_events.extend(detect_rating_changes(tickers, today, config))
    all_events.extend(detect_macro_events(today, config))

    _log.info(
        f"  [events] Detected {len(all_events)} event(s) for {len(tickers)} tickers",
        event_type="summary",
    )
    return all_events
