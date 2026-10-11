"""
Notification delivery worker — turns outbox rows into sent messages.

    run_once()    claim due rows -> send each (OUTSIDE any database transaction) -> record the outcome

How it stays safe
  * CLAIM is one IMMEDIATE transaction that marks rows SENDING, so two workers running at once (cron overlap, a manual run
    while the evening batch is delivering) can never take the same row; a worker that dies leaves SENDING rows that are
    reclaimed after a timeout.
  * SEND happens with NO database connection open: nothing holds a lock while a mail or push server is slow.
  * Everything is RE-CHECKED at send time — the channel may have been switched off, routing changed, or the contact opted out
    or removed since the row was enqueued. Such a row becomes SUPPRESSED with a reason, never sent.
  * RETRY: a transient failure goes back to PENDING after retry_backoff_seconds x 2^(attempt-1) (capped at one day); after
    retry_max_attempts the row is FAILED. A permanent failure (bad login, refused recipient) is FAILED at once. An
    administrator can requeue a FAILED row.
  * DRY RUN (a global setting): rows are logged ("would send") and marked sent with provider id "dry-run"; nothing is sent.
  * Never raises: one bad row cannot stop the others, and a worker problem can never break a pipeline run that calls it.
  * Logs carry ids, channels and address TAGS only — never an address, a topic, a message body or a secret.

Run it from cron (run_notifications.sh, every few minutes), from the batch jobs (the evening batch calls it), or by hand:
    python -m scripts.send_notifications
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from portfolio_agent.log import get_logger as _get_logger
from portfolio_agent.services import notification_senders as senders
from portfolio_agent.services import notification_service as ns
from portfolio_agent.tools import notification_contacts_db as cdb
from portfolio_agent.tools import notification_outbox_db as obx
from portfolio_agent.tools import notification_settings_db as sdb

_log = _get_logger("services.notification_worker")

MAX_BACKOFF_SECONDS = 86400

# explain_channels() reason -> the reason recorded on a row that became undeliverable after it was enqueued
_SUPPRESS_REASON = {ns.GLOBALLY_DISABLED: "CHANNEL_DISABLED", ns.NOT_ROUTED: "ROUTING_OFF", ns.NO_CONTACT: "NO_CONTACT",
                    ns.UNVERIFIED: "UNVERIFIED", ns.OPTED_OUT: "OPTED_OUT", ns.NOT_ACTIVE: "CHANNEL_NOT_ACTIVE"}


def backoff_seconds(settings: dict, attempts: int) -> int:
    """Seconds to wait before attempt number attempts+1: retry_backoff_seconds x 2^(attempts-1), capped at one day."""
    return int(min(MAX_BACKOFF_SECONDS, int(settings["retry_backoff_seconds"]) * (2 ** max(0, attempts - 1))))


def _message_for(row: dict, settings: dict, recipients: list[senders.Recipient], env: dict | None) -> senders.Message:
    email = row["channel"] == "EMAIL"
    return senders.Message(
        owner=row["owner"], channel=row["channel"], category=row["category"], subject=row["subject"],
        body=(row["body_detailed"] or row["body_brief"]) if email else row["body_brief"],       # push is ALWAYS the brief text
        recipients=recipients, dedup_key=row["dedup_key"], severity=row["severity"], link_url=row["link_url"],
        unsubscribe_urls=({r.contact_id: u for r in recipients if (u := senders.unsubscribe_url(settings, row["owner"], r.contact_id, env))}
                          if email else {}),
        manage_url=ns.full_link(settings, "/Profile") if email else None)


def _record_delivered(row: dict) -> None:
    """Keep notification_log (the legacy dedup record) in step so was_delivered() stays true. Best effort."""
    try:
        from portfolio_agent.tools.notification_log_db import record_if_new
        record_if_new(f"user:{row['owner']}", row["dedup_key"], row["channel"])
    except Exception:
        pass


def _process(row: dict, settings: dict, registry: dict, now: datetime, env: dict | None, summary: dict) -> None:
    owner, channel, category = row["owner"], row["channel"], row["category"]
    why = ns.explain_channels(owner, category, settings=settings)
    reason = why.get(channel, ns.NOT_ACTIVE)
    if reason != ns.DELIVERABLE:
        obx.mark_suppressed(row["id"], _SUPPRESS_REASON.get(reason, reason))
        summary["suppressed"] += 1
        return
    contacts = [c for c in ns.deliverable_contacts(owner, category, settings=settings) if c["channel"] == channel]
    recipients = [senders.Recipient(c["id"], c["address"]) for c in contacts]
    tags = ",".join(cdb.address_tag(c["address"]) for c in contacts)
    if settings["dry_run"]:
        _log.info(f"  [notifications:dry-run] would send id={row['id']} channel={channel} category={category} to={tags}",
                  event_type="notification_dry_run")
        obx.mark_sent(row["id"], "dry-run")
        summary["dry_run"] += 1
        return
    sender = registry.get(channel)
    if sender is None:
        obx.mark_suppressed(row["id"], "NO_SENDER")
        summary["suppressed"] += 1
        return
    result = sender.send(_message_for(row, settings, recipients, env))          # NO database connection is open here
    if result.status == senders.SENT:
        obx.mark_sent(row["id"], result.provider_message_id)
        _record_delivered(row)
        summary["sent"] += 1
        _log.info(f"  [notifications] sent id={row['id']} channel={channel} category={category} to={tags}", event_type="notification_sent")
    elif result.status == senders.RETRY and row["attempts"] < int(settings["retry_max_attempts"]):
        obx.mark_retry(row["id"], now + timedelta(seconds=backoff_seconds(settings, row["attempts"])), result.error or "temporary failure")
        summary["retried"] += 1
        _log.info(f"  [notifications] will retry id={row['id']} channel={channel} attempt={row['attempts']}", event_type="notification_retry")
    elif result.status == senders.SKIPPED:
        obx.mark_suppressed(row["id"], result.reason or "SKIPPED")
        summary["suppressed"] += 1
    else:                                                                       # permanent failure, or retries exhausted
        obx.mark_failed(row["id"], result.error or "delivery failed")
        summary["failed"] += 1
        _log.warning(f"  [notifications] FAILED id={row['id']} channel={channel} attempts={row['attempts']}", event_type="notification_failed")


def run_once(*, limit: int = 50, now: datetime | None = None, registry: dict | None = None, env: dict | None = None) -> dict:
    """
    Deliver what is due. Returns {"claimed", "sent", "retried", "failed", "suppressed", "dry_run", "error"}. Never raises.
    `registry` (channel -> sender) and `env` are for tests; production builds the real senders from the settings row.
    """
    summary = {"claimed": 0, "sent": 0, "retried": 0, "failed": 0, "suppressed": 0, "dry_run": 0, "error": None}
    try:
        now = now or datetime.now(timezone.utc)
        settings = sdb.get_notification_settings()
        registry = registry if registry is not None else senders.build_registry(settings, env=env)
        rows = obx.claim_due(limit=limit, now=now)
        summary["claimed"] = len(rows)
        for row in rows:
            try:
                _process(row, settings, registry, now, env, summary)
            except Exception as exc:                                            # one bad row must not strand the others
                summary["failed"] += 1
                try:
                    obx.mark_failed(row["id"], f"worker error: {type(exc).__name__}")
                except Exception:
                    pass
                _log.warning(f"  [notifications] worker error id={row['id']} ({type(exc).__name__})", event_type="warning")
    except Exception as exc:
        summary["error"] = type(exc).__name__
        _log.warning(f"  [notifications] delivery run failed ({type(exc).__name__})", event_type="warning")
    return summary
