"""
Compliance notifications — category COMPLIANCE.

Three things are worth telling a person about their own trading permissions:

  * a pre-clearance approval for a ticker was recorded (granted) or withdrawn (revoked) — dedup key approval:<id>:<event>;
  * an approval is about to expire (within 24 hours) — approval:<id>:expiring:<expires_at>;
  * their restricted list changed for a ticker they HOLD — restricted:<ticker>:<added|removed>:<time>.

(The approvals store has two events, granted and revoked; there is no separate "denied" event, so a withdrawn approval is the
closest thing and is worded that way.)

Each goes to ONE owner, through notification_service.notify: that person's own routing, severity, quiet hours and content
level apply, and the outbox de-duplicates. The text is informational — it never tells anyone to trade — and the brief form
(always used for push) carries no amounts or position sizes. Nothing here is sent directly and nothing here raises: the
approval and restricted-list stores call these hooks best-effort after their own commit, so a notification problem can never
undo or block a compliance record.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from portfolio_agent.log import get_logger as _get_logger

_log = _get_logger("services.compliance_notifications")

COMPLIANCE_SEVERITY = 4                     # above the usual minimum (3): a permission change is worth a message


def _owner_of(owner_scope: str) -> str | None:
    return owner_scope[5:] if owner_scope and owner_scope.startswith("user:") else None


def _send(owner: str, dedup_key: str, title: str, brief: str, detailed: str, link: str) -> dict:
    from portfolio_agent.services.notification_service import notify
    return notify(owner, "COMPLIANCE", dedup_key=dedup_key, title=title, brief=brief, detailed=detailed,
                  severity=COMPLIANCE_SEVERITY, link=link)


def notify_approval_event(row: dict) -> dict:
    """One trade_approvals row (just recorded) -> a notification to its owner. Returns notify()'s summary ({} if the owner
    cannot be determined)."""
    try:
        owner = _owner_of(row["owner_scope"])
        if not owner:
            return {}
        ticker, action = row["ticker"], row["action"]
        if row["event"] == "granted":
            until = f" It expires {row['expires_at']}." if row.get("expires_at") else ""
            return _send(owner, f"approval:{row['id']}:granted", f"APEX: Trade approval recorded - {ticker}",
                         f"APEX: a pre-clearance approval for {ticker} was recorded. Open APEX for details.",
                         f"A pre-clearance approval for {ticker} ({action}) was recorded by {row['granted_by']}.{until}", "/Restricted_List")
        return _send(owner, f"approval:{row['id']}:revoked", f"APEX: Trade approval withdrawn - {ticker}",
                     f"APEX: the pre-clearance approval for {ticker} was withdrawn; no new trade is currently permitted. Open APEX for details.",
                     f"The pre-clearance approval for {ticker} ({action}) was withdrawn by {row['granted_by']}. "
                     f"No new trade is currently permitted without a new approval.", "/Restricted_List")
    except Exception as exc:
        _log.warning(f"  [compliance-notify] approval not offered ({type(exc).__name__})", event_type="warning")
        return {}


def notify_restricted_change(owner: str, ticker: str, added: bool, reason: str | None = None, *, now: datetime | None = None) -> dict:
    """The owner's restricted list changed for *ticker*. Only told if they HOLD it (a change on something they do not own is
    no news to them)."""
    try:
        from portfolio_agent.tools.holdings_db import list_owners_holding
        ticker = ticker.upper()
        if owner not in list_owners_holding(ticker):
            return {}
        stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%S")
        if added:
            brief = f"APEX: {ticker} was added to your restricted list; no new trade is currently permitted. Open APEX for details."
            detailed = f"{ticker} was added to your restricted list." + (f" Reason on file: {reason}." if reason else "") + \
                       " No new trade is currently permitted."
        else:
            brief = f"APEX: {ticker} was removed from your restricted list. Open APEX for details."
            detailed = f"{ticker} was removed from your restricted list."
        return _send(owner, f"restricted:{ticker}:{'added' if added else 'removed'}:{stamp}",
                     f"APEX: Restricted list {'added' if added else 'removed'} - {ticker}", brief, detailed, "/Restricted_List")
    except Exception as exc:
        _log.warning(f"  [compliance-notify] restricted change not offered ({type(exc).__name__})", event_type="warning")
        return {}


def scan_expiring_approvals(*, now: datetime | None = None, within_hours: int = 24) -> dict:
    """Notify the owner of every ACTIVE approval that expires within *within_hours*. Idempotent (the dedup key includes the
    expiry time, so a re-scan, or the every-few-minutes cron, queues each once). Returns {"found": n, "enqueued": n}."""
    out = {"found": 0, "enqueued": 0}
    try:
        from portfolio_agent.tools.trade_approvals_db import list_expiring
        now = now or datetime.now(timezone.utc)
        rows = list_expiring(now.isoformat(timespec="seconds"), (now + timedelta(hours=within_hours)).isoformat(timespec="seconds"))
        out["found"] = len(rows)
        for row in rows:
            owner = _owner_of(row["owner_scope"])
            if not owner:
                continue
            r = _send(owner, f"approval:{row['id']}:expiring:{row['expires_at']}", f"APEX: Trade approval expiring - {row['ticker']}",
                      f"APEX: the pre-clearance approval for {row['ticker']} expires within {within_hours} hours. Open APEX for details.",
                      f"The pre-clearance approval for {row['ticker']} ({row['action']}) expires {row['expires_at']}. After that, "
                      f"no new trade is permitted without a new approval.", "/Restricted_List")
            out["enqueued"] += 1 if r.get("enqueued") else 0
    except Exception as exc:
        _log.warning(f"  [compliance-notify] expiry scan failed ({type(exc).__name__})", event_type="warning")
    return out
