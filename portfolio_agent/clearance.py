"""
Clearance — deterministic, private, action-specific policy evaluation.

Every evaluation is a PolicyDecision (see tools.policy_decisions_db): a
private, owner-scoped, append-only record of whether one (ticker, action)
may proceed right now, decided ENTIRELY in Python before any LLM runs. The
LLM in clearance_agent below is unreachable dead weight kept only so the
existing ADK SequentialAgent pipeline (portfolio_agent.agent.root_agent)
still has a "clearance" step to call — evaluate_clearance() always resolves
the decision in the before_agent_callback and short-circuits the model, for
every input. This is the enforcement point for "an LLM cannot override a
policy decision or grant external approval": there is no code path left
where the model's output determines the decision, and
tools.trade_approvals_db refuses "llm" as a granted_by value outright.

Decision values
  ALLOWED_BY_RULES   nothing in THIS system's rules blocks the action. This
                     is NOT external compliance approval — it means the
                     restricted list, blackout windows, and (if an approval
                     workflow applies) a matching active approval all
                     checked out; it says nothing about any approval process
                     outside this system.
  BLOCKED            a restricted-list hit or an active blackout window —
                     enforced regardless of the pre_clearance_required
                     preference or whether an approval exists.
  PENDING_APPROVAL   not restricted, no active blackout, but the profile
                     requires pre-clearance and no matching, unexpired,
                     unrevoked approval was found for this exact
                     (ticker, action).
  UNKNOWN            a required check could not be completed (a DB lookup
                     failed, a blackout window's dates are present but
                     unparseable, or a holding-period-sensitive action was
                     asked for and this system has no reliable per-lot
                     acquisition data to answer it). Invalid or missing
                     policy data is NEVER treated as "no restriction" — it
                     is UNKNOWN, and UNKNOWN is never upgraded to
                     ALLOWED_BY_RULES.

Evaluation order (every applicable check runs and contributes its own
reason code to evidence_refs["reason_codes"], regardless of which one ends
up deciding the overall outcome — "collect all applicable reason codes"):
  1. Restricted list — deterministic DB lookup. A lookup FAILURE makes the
     whole decision UNKNOWN; a HIT makes it BLOCKED. Neither is affected by
     pre_clearance_required.
  2. Blackout windows (tools.blackout_windows_db) — organizational policy,
     possibly several, each with its own scope/timezone/source/reason. Any
     window whose configured dates are present but unparseable makes the
     decision UNKNOWN (not "no restriction"); any currently-active window
     (evaluated in ITS OWN timezone, not the user's) makes it BLOCKED.
     Disabling pre_clearance_required never reaches this check's outcome.
  3. Holding period — only evaluated when the caller marks the action as
     holding-period-sensitive (check_holding_period=True). This system has
     no reliable per-lot acquisition/lot data, so this ALWAYS resolves
     UNKNOWN when evaluated — it can only ever pull an otherwise-clean
     decision down to UNKNOWN, never up.
  4. Approval workflow — reached only once 1-3 raised no block/unknown. If
     pre_clearance_required is False, the action is ALLOWED_BY_RULES with
     reason PRE_CLEARANCE_DISABLED. If True, an active tools.
     trade_approvals_db approval for this exact (ticker, action) is
     required; missing or expired -> PENDING_APPROVAL.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4
from zoneinfo import ZoneInfo

from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.genai import types

try:
    from ._models import FAST_MODEL as SPECIALIST_MODEL
    from .tools.portfolio_tools import check_restricted_list
except ImportError:
    import pathlib, sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from portfolio_agent._models import FAST_MODEL as SPECIALIST_MODEL
    from portfolio_agent.tools.portfolio_tools import check_restricted_list

POLICY_VERSION = "clearance-policy/3.0"   # bump whenever the evaluation order or rules below change

DECISION_ALLOWED = "ALLOWED_BY_RULES"
DECISION_BLOCKED = "BLOCKED"
DECISION_PENDING_APPROVAL = "PENDING_APPROVAL"
DECISION_UNKNOWN = "UNKNOWN"
VALID_DECISIONS = frozenset({DECISION_ALLOWED, DECISION_BLOCKED, DECISION_PENDING_APPROVAL, DECISION_UNKNOWN})

# Reason codes — short, stable, machine-checkable; `memo` carries the human sentence.
RC_RESTRICTED_LIST_HIT = "RESTRICTED_LIST_HIT"
RC_RESTRICTED_LIST_LOOKUP_FAILED = "RESTRICTED_LIST_LOOKUP_FAILED"
RC_BLACKOUT_ACTIVE = "BLACKOUT_ACTIVE"
RC_BLACKOUT_DATA_INVALID = "BLACKOUT_WINDOW_DATA_INVALID"
RC_BLACKOUT_LOOKUP_FAILED = "BLACKOUT_WINDOW_LOOKUP_FAILED"
RC_HOLDING_PERIOD_UNAVAILABLE = "HOLDING_PERIOD_DATA_UNAVAILABLE"
RC_PRE_CLEARANCE_DISABLED = "PRE_CLEARANCE_DISABLED"
RC_APPROVAL_ACTIVE = "APPROVAL_ACTIVE"
RC_APPROVAL_MISSING = "APPROVAL_MISSING"
RC_APPROVAL_LOOKUP_UNAVAILABLE = "APPROVAL_LOOKUP_UNAVAILABLE"
RC_NO_RESTRICTIONS_APPLY = "NO_RESTRICTIONS_APPLY"

DEFAULT_ACTION = "trade"


@dataclass(frozen=True)
class PolicyDecision:
    ticker: str
    action: str
    decision: str                       # ALLOWED_BY_RULES | BLOCKED | PENDING_APPROVAL | UNKNOWN
    reason: Optional[str]               # the primary human sentence (also see reason_codes)
    memo: str
    evidence_refs: dict = field(default_factory=dict)
    policy_version: str = POLICY_VERSION
    request_id: str = field(default_factory=lambda: uuid4().hex)
    next_transition_at: Optional[str] = None    # ISO datetime; when this decision may next change, if known
    valid_until: Optional[str] = None            # ISO datetime; a caller should not reuse this decision past here

    def to_dict(self) -> dict:
        return {"ticker": self.ticker, "action": self.action, "clearance_status": self.decision,
                "restriction_reason": self.reason, "memo": self.memo,
                "evidence_refs": self.evidence_refs, "policy_version": self.policy_version,
                "request_id": self.request_id, "next_transition_at": self.next_transition_at,
                "valid_until": self.valid_until}


# ── Deterministic checks (no LLM involved in any of them) ────────────────────

def _parse_iso_date_strict(raw: Optional[str]) -> tuple[Optional[date], bool]:
    """(parsed_date_or_None, is_invalid). None input -> (None, False): legitimately
    unset. A non-empty string that fails to parse -> (None, True): INVALID data,
    which callers must never treat the same as "unset" (see module docstring)."""
    if raw is None or str(raw).strip() == "":
        return None, False
    try:
        return date.fromisoformat(str(raw).strip()[:10]), False
    except ValueError:
        return None, True


def is_in_blackout(today: date, blackout_start: Optional[str], blackout_end: Optional[str]) -> bool:
    """
    Single-window legacy helper. True when *today* falls inside the inclusive
    [blackout_start, blackout_end] window; an open-ended window (one bound
    unset) is honoured; no bounds means no blackout. Raises ValueError if a
    bound is present but unparseable — invalid data is never silently "no
    restriction" (see evaluate_blackout_windows for the current, multi-window,
    timezone-aware evaluation path this feeds into via migration).
    """
    start, start_invalid = _parse_iso_date_strict(blackout_start)
    end, end_invalid = _parse_iso_date_strict(blackout_end)
    if start_invalid or end_invalid:
        raise ValueError(f"is_in_blackout: unparseable blackout bound(s): start={blackout_start!r} end={blackout_end!r}")
    if start is None and end is None:
        return False
    if start is not None and today < start:
        return False
    if end is not None and today > end:
        return False
    return True


def _today_in(tz_name: Optional[str]) -> date:
    try:
        return datetime.now(ZoneInfo(tz_name or "America/Chicago")).date()
    except Exception:
        return date.today()


def _now_in(tz_name: Optional[str]) -> datetime:
    try:
        return datetime.now(ZoneInfo(tz_name or "America/Chicago"))
    except Exception:
        return datetime.now(timezone.utc)


def _check_restricted(ticker: str) -> tuple[Optional[bool], Optional[str], Optional[str]]:
    """(is_restricted, reason, error). error is set (and the other two None) on lookup
    failure — the caller must treat that as UNKNOWN, never as "not restricted"."""
    try:
        from portfolio_agent.tools.restricted_list_db import is_restricted
        restricted, reason = is_restricted(ticker)
        return restricted, reason, None
    except Exception as exc:
        return None, None, f"{type(exc).__name__}: {exc}"


def evaluate_blackout_windows(windows: list[dict], ticker: str, *, at_utc: datetime) -> dict:
    """
    Pure: evaluate every window whose scope is 'global' or 'ticker:<TICKER>'
    against a single instant (converted into EACH window's own timezone,
    correctly handling DST since zoneinfo does the conversion). Returns:
      active            bool — True if any applicable window currently blocks
      invalid           bool — True if any applicable window's dates couldn't be parsed
      reason_codes      list[str]
      details           list[dict] — one per applicable window, for evidence_refs
      earliest_block_end     the earliest end (as UTC ISO) among currently-active,
                              closed-ended windows — a candidate next_transition_at
      earliest_upcoming_start the earliest start (as UTC ISO) among not-yet-active,
                              closed-started windows — another candidate
    """
    scope_keys = {"global", f"ticker:{ticker.upper()}"}
    active = False
    invalid = False
    reason_codes: list[str] = []
    details: list[dict] = []
    block_ends: list[datetime] = []
    upcoming_starts: list[datetime] = []

    for w in windows:
        if w.get("scope") not in scope_keys:
            continue
        tz_name = w.get("timezone") or "America/Chicago"
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("America/Chicago")
        local_today = at_utc.astimezone(tz).date()
        start, start_invalid = _parse_iso_date_strict(w.get("start_date"))
        end, end_invalid = _parse_iso_date_strict(w.get("end_date"))
        entry = {"id": w.get("id"), "scope": w.get("scope"), "source": w.get("source"),
                "reason": w.get("reason"), "start_date": w.get("start_date"), "end_date": w.get("end_date"),
                "timezone": tz_name}
        if start_invalid or end_invalid:
            invalid = True
            reason_codes.append(RC_BLACKOUT_DATA_INVALID)
            entry["invalid"] = True
            details.append(entry)
            continue

        is_active = True
        if start is not None and local_today < start:
            is_active = False
        if end is not None and local_today > end:
            is_active = False
        entry["active"] = is_active
        details.append(entry)

        if is_active:
            active = True
            reason_codes.append(RC_BLACKOUT_ACTIVE)
            if end is not None:
                block_ends.append(datetime.combine(end, datetime.min.time(), tzinfo=tz) + timedelta(days=1))
        elif start is not None and local_today < start:
            upcoming_starts.append(datetime.combine(start, datetime.min.time(), tzinfo=tz))

    return {
        "active": active, "invalid": invalid, "reason_codes": reason_codes, "details": details,
        "earliest_block_end": min(block_ends).astimezone(timezone.utc).isoformat(timespec="seconds") if block_ends else None,
        "earliest_upcoming_start": (min(upcoming_starts).astimezone(timezone.utc).isoformat(timespec="seconds")
                                    if upcoming_starts else None),
    }


def evaluate_clearance(ticker: str, profile, *, action: str = DEFAULT_ACTION, side: Optional[str] = None,
                       proposed_size: Optional[float] = None, account_id: Optional[int] = None,
                       portfolio_id: Optional[int] = None, owner_scope: Optional[str] = None,
                       today: Optional[date] = None, check_holding_period: bool = False) -> PolicyDecision:
    """
    The single source of truth for whether (ticker, action) may proceed right
    now. Never touches an LLM. See module docstring for evaluation order.
    account_id/portfolio_id/side/proposed_size are captured as evidence for
    audit; no rule in this evaluation currently depends on their value (no
    size-based or account-based rule exists yet in this codebase) — see the
    Phase 6 report for what that means going forward.
    """
    ticker = ticker.upper()
    tz_name = getattr(profile, "timezone", None)
    if today is not None:
        # A caller-pinned evaluation date drives the WHOLE evaluation (blackout
        # instant, next_transition_at) — noon on that date in the evaluation
        # timezone is an arbitrary but stable instant within it.
        try:
            tz = ZoneInfo(tz_name or "America/Chicago")
        except Exception:
            tz = timezone.utc
        now_local = datetime.combine(today, datetime.min.time(), tzinfo=tz).replace(hour=12)
    else:
        now_local = _now_in(tz_name)
        today = now_local.date()
    now_utc = now_local.astimezone(timezone.utc)

    reason_codes: list[str] = []
    evidence: dict = {
        "evaluated_at": today.isoformat(),
        "policy_version": POLICY_VERSION,
        "action": action, "side": side, "proposed_size": proposed_size,
        "account_id": account_id, "portfolio_id": portfolio_id,
        "pre_clearance_required": bool(getattr(profile, "pre_clearance_required", True)),
        "check_holding_period": check_holding_period,
    }

    outcome: Optional[str] = None   # set once by the highest-priority applicable check
    next_transition_candidates: list[str] = []

    # 1. Restricted list.
    restricted, restricted_reason, lookup_error = _check_restricted(ticker)
    evidence["restricted_list_checked"] = lookup_error is None
    if lookup_error is not None:
        evidence["restricted_list_error"] = lookup_error
        reason_codes.append(RC_RESTRICTED_LIST_LOOKUP_FAILED)
        outcome = DECISION_UNKNOWN
    else:
        evidence["is_restricted"] = restricted
        if restricted:
            reason_codes.append(RC_RESTRICTED_LIST_HIT)
            evidence["restricted_reason"] = restricted_reason
            if outcome is None:
                outcome = DECISION_BLOCKED

    # 2. Blackout windows — always evaluated (evidence + reason codes), independent
    #    of pre_clearance_required and regardless of what check 1 already decided.
    try:
        from portfolio_agent.tools.blackout_windows_db import (
            list_active_blackout_windows, migrate_legacy_profile_blackout,
        )
        # Self-healing, idempotent: if this profile's legacy blackout_start/end
        # hasn't been copied into blackout_windows_db yet, do it now — a caller
        # relying only on evaluate_clearance() (never evaluate_and_record_
        # clearance) must still see a pre-existing blackout enforced, never
        # silently dropped because the new table happened to be empty.
        migrate_legacy_profile_blackout(
            getattr(profile, "blackout_start", None), getattr(profile, "blackout_end", None), tz=tz_name or "America/Chicago",
        )
        windows = list_active_blackout_windows(scope_in=("global", f"ticker:{ticker}"))
        blackout_error = None
    except Exception as exc:
        windows = []
        blackout_error = f"{type(exc).__name__}: {exc}"

    if blackout_error is not None:
        evidence["blackout_lookup_error"] = blackout_error
        reason_codes.append(RC_BLACKOUT_LOOKUP_FAILED)
        if outcome is None:
            outcome = DECISION_UNKNOWN
    else:
        bw = evaluate_blackout_windows(windows, ticker, at_utc=now_utc)
        evidence["blackout_windows"] = bw["details"]
        reason_codes.extend(bw["reason_codes"])
        if bw["earliest_block_end"]:
            next_transition_candidates.append(bw["earliest_block_end"])
        if bw["earliest_upcoming_start"]:
            next_transition_candidates.append(bw["earliest_upcoming_start"])
        if bw["invalid"] and outcome in (None, DECISION_PENDING_APPROVAL, DECISION_ALLOWED):
            outcome = DECISION_UNKNOWN
        elif bw["active"] and outcome in (None, DECISION_PENDING_APPROVAL, DECISION_ALLOWED, None):
            if outcome != DECISION_BLOCKED:
                outcome = DECISION_BLOCKED

    # 3. Holding period — opt-in; this system has no reliable per-lot data,
    #    so it can only ever pull a clean decision down to UNKNOWN.
    if check_holding_period:
        reason_codes.append(RC_HOLDING_PERIOD_UNAVAILABLE)
        evidence["holding_period_available"] = False
        if outcome in (None, DECISION_PENDING_APPROVAL, DECISION_ALLOWED):
            outcome = DECISION_UNKNOWN

    # 4. Approval workflow — only reached if nothing above has already decided
    #    BLOCKED or UNKNOWN; a personal preference never re-opens 1-3.
    if outcome is None:
        if not evidence["pre_clearance_required"]:
            reason_codes.append(RC_PRE_CLEARANCE_DISABLED)
            outcome = DECISION_ALLOWED
        else:
            active_approval = None
            approval_error = None
            if owner_scope is not None:
                try:
                    from portfolio_agent.tools.trade_approvals_db import get_active_approval
                    active_approval = get_active_approval(owner_scope, ticker, action, now=now_utc.isoformat(timespec="seconds"))
                except Exception as exc:
                    approval_error = f"{type(exc).__name__}: {exc}"
            else:
                approval_error = "no owner_scope supplied"

            if approval_error is not None:
                evidence["approval_lookup_error"] = approval_error
                reason_codes.append(RC_APPROVAL_LOOKUP_UNAVAILABLE)
                outcome = DECISION_PENDING_APPROVAL
            elif active_approval is not None:
                reason_codes.append(RC_APPROVAL_ACTIVE)
                evidence["approval"] = {"granted_by": active_approval.get("granted_by"),
                                        "recorded_at": active_approval.get("recorded_at"),
                                        "expires_at": active_approval.get("expires_at")}
                if active_approval.get("expires_at"):
                    next_transition_candidates.append(active_approval["expires_at"])
                outcome = DECISION_ALLOWED
            else:
                reason_codes.append(RC_APPROVAL_MISSING)
                outcome = DECISION_PENDING_APPROVAL

    if outcome == DECISION_ALLOWED and not any(
        c in reason_codes for c in (RC_PRE_CLEARANCE_DISABLED, RC_APPROVAL_ACTIVE)
    ):
        reason_codes.append(RC_NO_RESTRICTIONS_APPLY)

    evidence["reason_codes"] = reason_codes

    # next_transition_at / valid_until: the earliest SPECIFIC known future change
    # (a blackout boundary, an approval's expiry) when one exists — a known
    # transition already tells us the decision holds until then, so there's no
    # reason to force an earlier re-check. Only when nothing specific is known
    # do we fall back to "start of tomorrow" (local), so an otherwise-static
    # decision is still never cached/reused past a calendar day.
    if next_transition_candidates:
        next_transition_at = min(datetime.fromisoformat(c) for c in next_transition_candidates).isoformat(timespec="seconds")
    else:
        tomorrow_local = datetime.combine(today + timedelta(days=1), datetime.min.time(),
                                          tzinfo=now_local.tzinfo).astimezone(timezone.utc)
        next_transition_at = tomorrow_local.isoformat(timespec="seconds")

    reason_primary = reason_codes[0] if reason_codes else None
    memo = _build_memo(ticker, action, outcome, reason_codes, evidence)

    return PolicyDecision(
        ticker=ticker, action=action, decision=outcome, reason=reason_primary, memo=memo,
        evidence_refs=evidence, next_transition_at=next_transition_at, valid_until=next_transition_at,
    )


def _build_memo(ticker: str, action: str, decision: str, reason_codes: list[str], evidence: dict) -> str:
    if decision == DECISION_BLOCKED:
        why = "on the restricted list" if RC_RESTRICTED_LIST_HIT in reason_codes else "an active blackout window applies"
        return f"{ticker} {action}: BLOCKED — {why}. This applies regardless of your pre-clearance preference."
    if decision == DECISION_UNKNOWN:
        return (f"{ticker} {action}: could not be evaluated ({', '.join(reason_codes)}) — "
               "NOT treated as allowed. Retry once the underlying data is available.")
    if decision == DECISION_PENDING_APPROVAL:
        return (f"{ticker} {action}: PENDING_APPROVAL — not restricted, no active blackout, but your profile "
               "requires pre-clearance and no active approval was found for this exact action.")
    return (f"{ticker} {action}: ALLOWED_BY_RULES — not restricted, no active blackout"
           + (", pre-clearance is disabled" if RC_PRE_CLEARANCE_DISABLED in reason_codes else
              ", an active approval covers this action" if RC_APPROVAL_ACTIVE in reason_codes else "")
           + ". This reflects THIS system's rules only, not external compliance approval.")


def evaluate_and_record_clearance(ticker: str, profile, *, owner_scope: str, action: str = DEFAULT_ACTION,
                                  side: Optional[str] = None, proposed_size: Optional[float] = None,
                                  account_id: Optional[int] = None, portfolio_id: Optional[int] = None,
                                  today: Optional[date] = None, check_holding_period: bool = False) -> PolicyDecision:
    """evaluate_clearance() plus persisting the result as a private PolicyDecision row."""
    from portfolio_agent.tools.policy_decisions_db import record_policy_decision
    decision = evaluate_clearance(ticker, profile, action=action, side=side, proposed_size=proposed_size,
                                  account_id=account_id, portfolio_id=portfolio_id, owner_scope=owner_scope,
                                  today=today, check_holding_period=check_holding_period)
    record_policy_decision(
        owner_scope=owner_scope, ticker=decision.ticker, action=decision.action, decision=decision.decision,
        reason=decision.reason, evidence_refs=decision.evidence_refs, policy_version=decision.policy_version,
        request_id=decision.request_id, next_transition_at=decision.next_transition_at,
        valid_until=decision.valid_until,
    )
    return decision


def _profile_gate(callback_context: CallbackContext) -> types.Content:
    """
    before_agent_callback — ALWAYS resolves the clearance decision in Python
    and short-circuits the LLM, for every ticker/profile combination. There is
    no remaining path where clearance_agent's model or its check_restricted_list
    tool call determines the decision.
    """
    from portfolio_agent.domain import UserProfile
    from portfolio_agent.services.context import local_context
    from portfolio_agent.tools.user_profile_db import get_user_profile

    ticker = str(callback_context.state.get("current_ticker") or "").upper()
    try:
        profile = get_user_profile()
    except Exception:
        profile = UserProfile()   # profile store unavailable — evaluate with safe defaults, never skip the gate
    owner_scope = f"user:{local_context('adk.clearance').actor}"
    decision = evaluate_and_record_clearance(ticker, profile, owner_scope=owner_scope)
    payload = json.dumps(decision.to_dict())
    callback_context.state["clearance"] = payload
    return types.Content(role="model", parts=[types.Part(text=payload)])


# The instruction and tool below are DEAD CODE from a policy standpoint: _profile_gate
# above always returns a Content, so the ADK Runner never invokes this model. They are
# kept only so `clearance_agent` remains a valid, inspectable ADK agent for the
# SequentialAgent pipeline and CLI (`adk run`) to reference by name.
INSTRUCTION = """
This step is decided deterministically in Python before you are ever invoked
(see portfolio_agent.clearance.evaluate_clearance) — you should never run.
An LLM may explain a decision after the fact; it never decides or overrides one.
""".strip()

clearance_agent = LlmAgent(
    name="clearance_agent",
    model=SPECIALIST_MODEL,
    instruction=INSTRUCTION,
    tools=[check_restricted_list],
    output_key="clearance",
    before_agent_callback=_profile_gate,
)

if __name__ == "__main__":
    import pathlib, sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from portfolio_agent.specialists._runner import run
    run(clearance_agent, "clearance")
