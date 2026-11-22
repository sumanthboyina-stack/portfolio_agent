# Spec Delta

## Purpose

Defines the personal compliance controls around a possible trade: each owner's restricted list, organizational blackout windows, recorded pre-clearance approvals, the deterministic clearance decision built from them, the append-only record of those decisions, and the informational notifications they produce. The controls only label and record. APEX never places, blocks or routes an order. Baseline at HEAD `ca450dd`.

## ADDED Requirements

### Requirement: Restricted list is personal to one owner
The system SHALL keep a restricted list per owner, unique on (owner, ticker), with no shared or global list. Every read and write MUST name an owner, and one owner's entries MUST never affect another owner's checks, badges or notifications.

#### Scenario: Another owner's restriction is invisible
- **WHEN** owner A restricts TICKER and owner B asks whether TICKER is restricted or lists their restrictions
- **THEN** owner B sees no restriction

#### Scenario: Missing owner is refused
- **WHEN** a restricted-list read or write is made with an empty owner
- **THEN** it raises an error and does not fall back to any other list

#### Scenario: Re-adding a ticker keeps one row
- **WHEN** an owner adds a ticker already on their list with a new reason
- **THEN** one row remains for that ticker and its reason is the new one

<!-- tests: tests/test_predictions_page_data.py::test_personal_overlay_does_not_leak_a_different_owners_restricted_list, tests/test_notification_producers.py::test_restricted_list_changes_notify_only_the_owner_who_holds_the_ticker, tests/test_trusted_context_scoping.py::test_repeated_restricted_list_and_watchlist_migrations_preserve_owner_and_dont_duplicate -->

### Requirement: Restricted List page edits only the signed-in member's list
The Restricted List page SHALL require login and list, add and remove restricted tickers for the signed-in member only. Tickers are stored upper-cased and the reason is optional; an empty ticker is refused with a warning.

#### Scenario: Member adds a restriction
- **WHEN** a signed-in member submits ticker `xyz` with a reason
- **THEN** `XYZ` appears on that member's list only

#### Scenario: Empty ticker
- **WHEN** a member submits the add form with a blank ticker
- **THEN** nothing is stored and the page shows a "Enter a ticker" warning

<!-- tests: NONE -->

### Requirement: Pipeline skip list is shared admin configuration
The system SHALL keep a pipeline skip list of tickers, each with phases drawn from `news`, `research`, `fundamentals` or `all`, and a reason. It MUST have no owner column and MUST NOT be affected by any member's restricted list. The page section that edits it SHALL be shown to admins only.

#### Scenario: Phase-specific skip
- **WHEN** a ticker is skipped for `research` only
- **THEN** the research phase's skip set contains it and the news phase's does not

#### Scenario: All phases
- **WHEN** a ticker is skipped with `all`, or with all three phases listed
- **THEN** it is reported as skipped in every phase

#### Scenario: Non-admin member
- **WHEN** a non-admin opens the Restricted List page
- **THEN** the Pipeline Skip section is not shown

<!-- tests: NONE -->

### Requirement: Personal restrictions never reach shared generation
Run planning and the shared generation path SHALL NOT read any member's personal restricted list. A ticker becomes unavailable to the shared pipeline only through the admin pipeline skip list.

#### Scenario: Personal restriction does not skip generation
- **WHEN** a member restricts a ticker they hold
- **THEN** the run planner's availability for that ticker is unchanged

<!-- tests: tests/test_run_planner.py::test_a_personal_restriction_never_reaches_the_planner_at_all -->

### Requirement: Compliance restrictions never disable monitoring
Portfolio risk and technical monitoring SHALL run without consulting clearance, restricted-list or blackout state. A non-allowed permission MUST only annotate alerts, never suppress them.

#### Scenario: Risk and technical code is independent of clearance
- **WHEN** the risk and technical monitoring phase runs for a restricted or blacked-out ticker
- **THEN** it does not import or call clearance or blackout logic

<!-- tests: tests/test_scheduler_notifications.py::test_risk_technical_monitoring_never_imports_clearance_or_blackout -->

### Requirement: Blackout windows are organizational, scoped and time-bounded
A blackout window SHALL have a scope of `global` or `ticker:<TICKER>`, optional inclusive start and end dates, an IANA timezone (default `America/Chicago`), a source, a reason and an active flag. Inactive windows MUST never block. A scope string of any other form MUST never match any ticker.

#### Scenario: Window in range
- **WHEN** an active `global` window covers today in its own timezone
- **THEN** the window is reported active for every ticker

#### Scenario: Other ticker's window
- **WHEN** a window is scoped to a different ticker
- **THEN** it does not apply to the ticker being evaluated

#### Scenario: Open-ended window
- **WHEN** a window has a start date and no end date
- **THEN** it is active from the start date with no end boundary

<!-- tests: tests/test_clearance_gate.py::test_overlapping_windows_both_contribute_and_active_wins, tests/test_clearance_gate.py::test_window_scoped_to_a_different_ticker_does_not_apply, tests/test_clearance_gate.py::test_open_ended_window_has_no_earliest_block_end, tests/test_clearance_gate.py::test_is_in_blackout_inclusive_window -->

### Requirement: Blackout dates are judged in the window's own timezone
The system SHALL evaluate each window's dates against the current instant converted into that window's timezone, including across daylight-saving transitions, and not in the acting member's timezone.

#### Scenario: Spring-forward boundary
- **WHEN** an instant falls just before and just after a daylight-saving transition in the window's timezone
- **THEN** the local date used for the window check is the correct local date on both sides

<!-- tests: tests/test_clearance_gate.py::test_dst_boundary_local_date_stays_correct_across_the_spring_forward_transition -->

### Requirement: Invalid blackout data is unknown, never unrestricted
A window whose start or end date is present but unparseable SHALL be flagged invalid and MUST make the clearance decision `UNKNOWN`, not allow the action.

#### Scenario: Unparseable date
- **WHEN** an applicable window has an end date that is not an ISO date
- **THEN** the clearance decision is `UNKNOWN` with reason code `BLACKOUT_WINDOW_DATA_INVALID`

<!-- tests: tests/test_clearance_gate.py::test_invalid_window_data_is_flagged_not_treated_as_no_restriction, tests/test_clearance_gate.py::test_invalid_blackout_data_makes_the_whole_decision_unknown, tests/test_clearance_gate.py::test_is_in_blackout_raises_on_invalid_data_never_treats_it_as_unset -->

### Requirement: Legacy profile blackout migrates once into a global window
When a clearance evaluation sees a profile with legacy blackout start or end dates, the system SHALL copy them into exactly one `global` blackout window, once, and never add a second migrated window. A profile with no legacy dates MUST create nothing.

#### Scenario: First evaluation enforces a legacy blackout
- **WHEN** a profile with legacy blackout dates covering today is evaluated before any migration
- **THEN** the decision is `BLOCKED` and exactly one `global` window exists afterwards

#### Scenario: Later migration attempt
- **WHEN** a migration is attempted again with different legacy dates
- **THEN** no additional window is created

<!-- tests: tests/test_clearance_gate.py::test_evaluate_clearance_self_migrates_and_enforces_a_pre_existing_legacy_blackout, tests/test_clearance_gate.py::test_migrate_legacy_profile_blackout_is_idempotent_and_never_broadens, tests/test_clearance_gate.py::test_migrate_legacy_profile_blackout_no_legacy_data_creates_nothing -->

### Requirement: Trade approvals are append-only and action-specific
The system SHALL record each approval as a `granted` or `revoked` event keyed by (owner, ticker, action), and record a withdrawal as a new `revoked` event, never by changing the grant. A grant MUST name a non-empty `granted_by` and MUST refuse `llm` as the grantor.

#### Scenario: LLM cannot grant
- **WHEN** an approval is recorded with `granted_by` equal to `llm`
- **THEN** it is refused and nothing is stored

#### Scenario: Revocation is its own event
- **WHEN** an approval is revoked
- **THEN** a `revoked` event is appended and the earlier grant row is unchanged

<!-- tests: tests/test_clearance_gate.py::test_trade_approvals_db_refuses_an_llm_as_granted_by, tests/test_clearance_gate.py::test_revoked_approval_is_pending_not_allowed -->

### Requirement: An approval is active only for its exact action
An approval SHALL be active when the latest event for that (owner, ticker, action) is a grant that is not revoked and whose expiry, if set, is later than now. An approval for one action MUST NOT cover another action or another ticker.

#### Scenario: Different action
- **WHEN** only a `buy` approval exists and a `sell` is evaluated
- **THEN** the approval is not active for the `sell`

#### Scenario: Expired approval
- **WHEN** the latest grant's expiry is in the past
- **THEN** no approval is active

<!-- tests: tests/test_clearance_gate.py::test_active_approval_for_the_exact_action_allows, tests/test_clearance_gate.py::test_approval_for_a_different_action_does_not_cover_this_one, tests/test_clearance_gate.py::test_expired_approval_is_pending_not_allowed, tests/test_clearance_gate.py::test_revoked_approval_is_pending_not_allowed -->

### Requirement: Clearance decision values
Clearance SHALL resolve every (ticker, action) to exactly one of `ALLOWED_BY_RULES`, `BLOCKED`, `PENDING_APPROVAL` or `UNKNOWN`, under policy version `clearance-policy/3.0`. The default action is `trade`.

#### Scenario: Value set
- **WHEN** any clearance evaluation completes
- **THEN** its decision is one of the four values and records the policy version

<!-- tests: tests/test_clearance_gate.py::test_profile_gate_resolves_every_ticker_profile_combination_without_a_model_call, tests/test_clearance_gate.py::test_evaluate_and_record_persists_a_private_owner_scoped_action_tagged_decision -->

### Requirement: Restricted list and blackout windows block regardless of preference
A hit on the owner's restricted list, or an active applicable blackout window, SHALL make the decision `BLOCKED` even when the profile's `pre_clearance_required` is off or an approval exists.

#### Scenario: Restricted with pre-clearance off
- **WHEN** a ticker is on the owner's restricted list and `pre_clearance_required` is false
- **THEN** the decision is `BLOCKED` with reason code `RESTRICTED_LIST_HIT`

#### Scenario: Blackout with pre-clearance off
- **WHEN** a global window is active and `pre_clearance_required` is false
- **THEN** the decision is `BLOCKED` with reason code `BLACKOUT_ACTIVE`

<!-- tests: tests/test_clearance_gate.py::test_restricted_list_blocks_even_when_pre_clearance_is_disabled_and_no_blackout, tests/test_clearance_gate.py::test_blackout_blocks_even_when_pre_clearance_is_disabled -->

### Requirement: Every applicable reason code is collected
Clearance SHALL run the restricted-list and blackout checks every time and record each check's reason code in the decision's evidence, regardless of which check decides the outcome.

#### Scenario: Both apply
- **WHEN** a ticker is restricted and also inside an active blackout window
- **THEN** the decision is `BLOCKED` and both `RESTRICTED_LIST_HIT` and `BLACKOUT_ACTIVE` are recorded

<!-- tests: tests/test_clearance_gate.py::test_both_restricted_and_blackout_apply_both_reason_codes_collected -->

### Requirement: Approval workflow decides when nothing blocks
When nothing blocks, clearance SHALL return `ALLOWED_BY_RULES` if `pre_clearance_required` is off (reason `PRE_CLEARANCE_DISABLED`) or an active approval covers the action (reason `APPROVAL_ACTIVE`), and `PENDING_APPROVAL` otherwise (reason `APPROVAL_MISSING`).

#### Scenario: Pre-clearance off, nothing blocks
- **WHEN** no restriction or blackout applies and `pre_clearance_required` is false
- **THEN** the decision is `ALLOWED_BY_RULES`

#### Scenario: Required and no approval
- **WHEN** pre-clearance is required and no active approval exists for the exact action
- **THEN** the decision is `PENDING_APPROVAL`

#### Scenario: Required and approved
- **WHEN** pre-clearance is required and an active approval covers the exact action
- **THEN** the decision is `ALLOWED_BY_RULES` with reason `APPROVAL_ACTIVE`

<!-- tests: tests/test_clearance_gate.py::test_pre_clearance_disabled_with_no_blocks_is_allowed_by_rules, tests/test_clearance_gate.py::test_pending_approval_when_required_and_no_approval_exists, tests/test_clearance_gate.py::test_active_approval_for_the_exact_action_allows -->

### Requirement: Failed or unusable checks are unknown, never allowed
A restricted-list or blackout lookup that fails, or a restricted-list check with no resolvable owner, SHALL make the decision `UNKNOWN`. `UNKNOWN` MUST NOT be upgraded to `ALLOWED_BY_RULES`.

#### Scenario: No owner supplied
- **WHEN** clearance is evaluated with no owner scope
- **THEN** the decision is `UNKNOWN` and the restricted list is recorded as not checked

#### Scenario: Restricted-list lookup fails
- **WHEN** the restricted-list lookup raises
- **THEN** the decision is `UNKNOWN` with reason code `RESTRICTED_LIST_LOOKUP_FAILED`

#### Scenario: Blackout lookup fails
- **WHEN** the blackout lookup raises
- **THEN** the decision is `UNKNOWN` with reason code `BLACKOUT_WINDOW_LOOKUP_FAILED`

<!-- tests: tests/test_clearance_gate.py::test_no_owner_scope_makes_the_restricted_list_check_unknown_not_skipped, tests/test_clearance_gate.py::test_restricted_list_lookup_failure_is_unknown_not_allowed, tests/test_clearance_gate.py::test_blackout_lookup_failure_is_unknown -->

### Requirement: Approval lookup failure is pending, not allowed
When pre-clearance is required and the approval lookup cannot be completed, clearance SHALL return `PENDING_APPROVAL` with reason code `APPROVAL_LOOKUP_UNAVAILABLE`, never `ALLOWED_BY_RULES`.

#### Scenario: Approvals unreadable
- **WHEN** nothing blocks, pre-clearance is required and reading approvals raises
- **THEN** the decision is `PENDING_APPROVAL`

<!-- tests: NONE -->

### Requirement: Holding-period checks are always unknown
When a caller marks an action holding-period-sensitive, clearance SHALL add reason code `HOLDING_PERIOD_DATA_UNAVAILABLE` and lower an otherwise clean decision to `UNKNOWN`; it MUST NOT raise a `BLOCKED` decision.

#### Scenario: Clean decision capped
- **WHEN** nothing blocks and the holding-period check is requested
- **THEN** the decision is `UNKNOWN`

#### Scenario: Blocked stays blocked
- **WHEN** the ticker is restricted and the holding-period check is requested
- **THEN** the decision stays `BLOCKED`

<!-- tests: tests/test_clearance_gate.py::test_holding_period_check_is_always_unknown_and_caps_an_otherwise_clean_decision, tests/test_clearance_gate.py::test_holding_period_check_does_not_upgrade_an_already_blocked_decision -->

### Requirement: Decisions state when they may next change
Each decision SHALL carry `next_transition_at` and `valid_until`: the earliest known blackout boundary or approval expiry, else the start of the next local day.

#### Scenario: Blackout end
- **WHEN** a closed-ended blackout window is active
- **THEN** `next_transition_at` is the instant after that window's last day

#### Scenario: Nothing known
- **WHEN** no boundary or expiry is known
- **THEN** `next_transition_at` is the start of tomorrow

<!-- tests: tests/test_clearance_gate.py::test_next_transition_at_matches_the_blackout_window_end, tests/test_clearance_gate.py::test_next_transition_at_defaults_to_start_of_tomorrow_when_nothing_else_known -->

### Requirement: Allowed by rules is not external approval
`ALLOWED_BY_RULES` SHALL mean only that this system's rules do not block the action, and every rendering of it MUST say it is not external compliance approval.

#### Scenario: Allowed memo
- **WHEN** a decision is `ALLOWED_BY_RULES`
- **THEN** its memo states that it reflects this system's rules only

<!-- tests: tests/test_clearance_gate.py::test_allowed_by_rules_is_not_external_compliance_approval_by_construction, tests/test_rendering.py::test_allowed_recommendation_prints_allowed_by_rules_qualifier -->

### Requirement: No model decides or overrides a clearance
The clearance decision SHALL be computed entirely in code. The clearance agent's model MUST NOT run, and a decision MUST NOT be recorded as decided by anything other than the deterministic engine.

#### Scenario: Gate short-circuits the model
- **WHEN** the clearance step runs for any ticker and profile
- **THEN** the decision is written to session state and the model is never invoked

#### Scenario: Non-deterministic decider refused
- **WHEN** a policy decision is recorded with a decider other than the deterministic engine
- **THEN** it is refused

<!-- tests: tests/test_clearance_gate.py::test_profile_gate_resolves_every_ticker_profile_combination_without_a_model_call, tests/test_clearance_gate.py::test_profile_gate_writes_state_and_never_falls_through_to_the_llm, tests/test_clearance_gate.py::test_clearance_agent_instruction_never_lets_the_model_choose_status, tests/test_clearance_gate.py::test_record_policy_decision_rejects_an_llm_as_the_decider -->

### Requirement: Clearance gate uses the run owner's identity
In the agent pipeline the clearance gate SHALL evaluate against the owner named in session state, using that owner's profile and lists, and MUST NOT substitute the local operator's identity when an owner is supplied.

#### Scenario: Owner in state
- **WHEN** the session state names owner A
- **THEN** the decision is recorded under owner A's scope using owner A's profile

<!-- tests: tests/test_clearance_gate.py::test_profile_gate_uses_the_state_owners_identity_not_the_local_operators -->

### Requirement: Policy decisions are private, append-only and action-tagged
Each recorded evaluation SHALL be a new row scoped to one owner and tagged with ticker, action, decision value (one of the four), reason, evidence, policy version and request id. Rows MUST NOT be updated, reads MUST be by owner and ticker (optionally action), and an unknown decision value MUST be refused.

#### Scenario: History newest first
- **WHEN** two decisions are recorded for the same owner and ticker
- **THEN** both rows are kept and the latest is returned first

#### Scenario: Owners are separate
- **WHEN** owner A has a recorded decision
- **THEN** owner B's reads for the same ticker return nothing

#### Scenario: Actions are separate
- **WHEN** `buy` and `sell` decisions are recorded for one ticker
- **THEN** each action's latest decision is returned independently

#### Scenario: Unknown value refused
- **WHEN** a decision value outside the four is recorded
- **THEN** it is refused

<!-- tests: tests/test_policy_decisions_db.py::test_record_and_read_round_trip, tests/test_policy_decisions_db.py::test_rejects_unknown_decision_value, tests/test_policy_decisions_db.py::test_append_only_keeps_every_evaluation_newest_first, tests/test_policy_decisions_db.py::test_decisions_never_cross_owners, tests/test_policy_decisions_db.py::test_decisions_are_independent_per_action, tests/test_clearance_gate.py::test_evaluate_and_record_persists_a_private_owner_scoped_action_tagged_decision -->

### Requirement: Ledger clearance statuses
For a BUY or SELL already recorded on a non-paper account, clearance SHALL return one of `approved` (with the approval id), `missing`, `not_required` or `unknown`. It MUST return `not_required` when the owner's `pre_clearance_required` is off and `unknown`, never `missing` or `approved`, when approvals cannot be read.

#### Scenario: Covered trade
- **WHEN** an approval covers the ticker, side and trade date
- **THEN** the status is `approved` and carries that approval's id

#### Scenario: No approval
- **WHEN** no approval covers the trade
- **THEN** the status is `missing`

#### Scenario: Pre-clearance off
- **WHEN** the owner's pre-clearance preference is off
- **THEN** the status is `not_required`

#### Scenario: Approvals unreadable
- **WHEN** reading approvals fails
- **THEN** the status is `unknown`

<!-- tests: tests/test_ledger_compliance_import.py::test_an_approval_covering_ticker_side_and_date_marks_the_trade_approved_with_its_id, tests/test_ledger_compliance_import.py::test_a_trade_without_an_approval_is_recorded_flagged_missing_and_audited_never_blocked, tests/test_ledger_compliance_import.py::test_not_required_when_the_owner_turned_pre_clearance_off, tests/test_ledger_compliance_import.py::test_unreadable_approvals_are_unknown_not_missing_and_send_nothing -->

### Requirement: Ledger clearance reads approvals as of the trade date
Ledger clearance SHALL accept an approval for the trade's side (`buy` or `sell`) or a general `trade` approval, evaluated as of the trade date: a grant recorded after the trade MUST NOT cover it and a revoke recorded after the trade MUST NOT un-cover it. It MUST ignore the restricted list and blackout windows.

#### Scenario: Later grant
- **WHEN** an approval is recorded after the trade date
- **THEN** the trade's status is `missing`

#### Scenario: Later revoke
- **WHEN** an approval covered the trade date and was revoked afterwards
- **THEN** the trade's status stays `approved`

<!-- tests: tests/test_ledger_compliance_import.py::test_approvals_are_read_as_of_the_trade_date -->

### Requirement: Clearance never blocks a recorded trade
Recording a trade in the ledger SHALL succeed regardless of its clearance status, which is stored on the row and audited. A paper account MUST NOT be checked. See `transaction-ledger` for the recording rules.

#### Scenario: Trade without approval
- **WHEN** a BUY is recorded with no covering approval
- **THEN** the trade is stored with status `missing` and an audit event, and is not refused

#### Scenario: Paper account
- **WHEN** a trade is recorded on a paper account
- **THEN** no clearance status is stored

<!-- tests: tests/test_ledger_compliance_import.py::test_a_trade_without_an_approval_is_recorded_flagged_missing_and_audited_never_blocked, tests/test_ledger_compliance_import.py::test_a_paper_account_is_not_checked_at_all -->

### Requirement: Missing clearance sends one brief notification without amounts
A trade whose clearance is `missing` SHALL enqueue one `COMPLIANCE` notification with dedup key `ledger_clearance:<transaction id>`, containing ticker, side and date and no amounts. It MUST be queued only after the trade commits, and a caller-owned transaction MUST defer it until the caller commits.

#### Scenario: One notification
- **WHEN** a trade is recorded with clearance `missing`
- **THEN** one brief notification is queued and its text contains no amounts

#### Scenario: Caller-owned transaction
- **WHEN** the trade is recorded inside a caller's transaction
- **THEN** nothing is queued until the caller commits

<!-- tests: tests/test_ledger_compliance_import.py::test_a_missing_clearance_enqueues_one_brief_compliance_notification_without_amounts, tests/test_ledger_compliance_import.py::test_with_a_caller_owned_transaction_the_notification_waits_for_the_caller -->

### Requirement: Approval events notify their owner
Recording or withdrawing an approval SHALL notify only that approval's owner, category `COMPLIANCE`, severity 4, with dedup keys `approval:<id>:granted` and `approval:<id>:revoked`. The brief text MUST be informational and exclude the grantor's name and any amounts.

#### Scenario: Granted then withdrawn
- **WHEN** an approval is granted and later revoked for owner A
- **THEN** owner A gets two notifications with the two keys and no other owner is notified

#### Scenario: Brief content
- **WHEN** the brief text is built
- **THEN** it names the ticker and omits the grantor and any amounts

<!-- tests: tests/test_notification_producers.py::test_approval_granted_and_withdrawn_notify_their_owner_once_each -->

### Requirement: Expiring approvals are announced once
The system SHALL notify an owner of each still-active approval that expires within the next 24 hours, with dedup key `approval:<id>:expiring:<expiry>`. Approvals already expired, expiring later, or revoked MUST NOT be announced, and a repeated scan MUST NOT enqueue a duplicate.

#### Scenario: Scan
- **WHEN** approvals expire in 6 hours, in 60 hours, were already expired, or were revoked
- **THEN** only the 6-hour approval is announced

#### Scenario: Re-scan
- **WHEN** the scan runs again with nothing new
- **THEN** it finds the same approval and enqueues nothing

<!-- tests: tests/test_notification_producers.py::test_approvals_expiring_within_a_day_are_announced_once_and_only_while_active -->

### Requirement: Restricted-list changes notify only holders
When a ticker is newly added to, or removed from, an owner's restricted list, the system SHALL notify that owner only if they hold the ticker, with dedup key `restricted:<ticker>:<added|removed>:<time>`. Editing the reason of an existing entry, or removing an absent ticker, MUST NOT notify.

#### Scenario: Holder and non-holder
- **WHEN** owner A holds TICKER and owner B does not, and each restricts TICKER
- **THEN** only owner A is notified

#### Scenario: Reason edit
- **WHEN** an owner changes the reason on an existing entry
- **THEN** no notification is sent

<!-- tests: tests/test_notification_producers.py::test_restricted_list_changes_notify_only_the_owner_who_holds_the_ticker -->

### Requirement: Compliance notifications are best-effort after commit
Compliance notifications SHALL be sent after the approval or restricted-list change has committed, and a notification failure MUST NOT undo, block or raise into that change.

#### Scenario: Notifier fails
- **WHEN** the notifier raises while an approval and a restriction are recorded
- **THEN** both records exist and are readable

<!-- tests: tests/test_notification_producers.py::test_a_notification_failure_can_never_undo_or_block_a_compliance_record -->

### Requirement: Market-event alerts carry the owner's permission note
When a market event alert is built for an owner, it SHALL append that owner's own clearance outcome as ` [<DECISION>: no new trade currently permitted]` for any decision other than `ALLOWED_BY_RULES`, and MUST still be delivered. Evaluation failure MUST yield no note.

#### Scenario: Restricted holder
- **WHEN** an event fires for a ticker on the holder's restricted list
- **THEN** the alert is queued and its brief text contains "BLOCKED: no new trade currently permitted"

<!-- tests: tests/test_scheduler_notifications.py::test_a_restricted_or_unevaluatable_action_permission_still_notifies_monitoring_is_not_disabled -->

### Requirement: Predictions page shows each viewer's own compliance badges
For each ticker on the Predictions page the system SHALL report whether the viewer holds it, its weight, whether it is on the viewer's own restricted list (with reason), whether an applicable blackout window is active, and whether the viewer's profile needs pre-clearance. It MUST NOT change the recommendation.

#### Scenario: Own restriction
- **WHEN** the viewer restricted TICKER
- **THEN** its badge shows restricted with the viewer's reason

#### Scenario: Other owner's restriction
- **WHEN** a different owner restricted TICKER
- **THEN** the viewer's badge does not show it restricted

#### Scenario: Active blackout
- **WHEN** an applicable window is active
- **THEN** the badge shows blackout

<!-- tests: tests/test_predictions_page_data.py::test_personal_overlay_reports_restricted_from_the_viewers_own_list, tests/test_predictions_page_data.py::test_personal_overlay_does_not_leak_a_different_owners_restricted_list, tests/test_predictions_page_data.py::test_personal_overlay_reports_active_blackout_window, tests/test_predictions_page_data.py::test_personal_overlay_reports_needs_pre_clearance_from_the_viewers_own_profile -->

### Requirement: Recommendations are actionable only when allowed
A composed decision SHALL expose an actionable recommendation only when the owner's clearance is `ALLOWED_BY_RULES`; for `BLOCKED`, `PENDING_APPROVAL` and `UNKNOWN` it MUST be empty. The permission MUST be re-evaluated on every composition and match a direct clearance evaluation.

#### Scenario: Blocked ticker
- **WHEN** the owner restricted the ticker
- **THEN** the actionable recommendation is empty

#### Scenario: Pending approval
- **WHEN** pre-clearance is required and no approval exists
- **THEN** the actionable recommendation is empty

<!-- tests: tests/test_decision_composer.py::test_blocked_ticker_never_produces_an_actionable_recommendation, tests/test_decision_composer.py::test_pending_approval_ticker_never_produces_an_actionable_recommendation, tests/test_decision_composer.py::test_composer_and_direct_clearance_evaluation_agree_on_the_same_permission, tests/test_decision_composer.py::test_action_permission_never_depends_on_explanation_being_generated -->

### Requirement: Agent run output qualifies the recommendation with its permission
When an agent run has both a recommendation and a clearance, its printed summary SHALL state the permission: `ALLOWED_BY_RULES` with the not-external-approval caveat, otherwise that the action is not actionable with the status and memo. It MUST print nothing and not raise when either is missing.

#### Scenario: Blocked
- **WHEN** the clearance status is `BLOCKED`
- **THEN** the summary says the action is not actionable and shows the memo

#### Scenario: Missing clearance
- **WHEN** the recommendation or clearance is absent
- **THEN** nothing is printed

<!-- tests: tests/test_rendering.py::test_blocked_recommendation_prints_not_actionable, tests/test_rendering.py::test_allowed_recommendation_prints_allowed_by_rules_qualifier, tests/test_rendering.py::test_missing_recommendation_or_clearance_prints_nothing_and_does_not_raise -->

### Requirement: Compliance records are information, never order execution
Clearance, approvals, restricted lists and blackout windows SHALL only record, label and notify. No operation of this capability places, routes or blocks an order, and notifications MUST be informational and never phrased as an instruction to trade.

#### Scenario: Blocked decision
- **WHEN** a clearance decision is `BLOCKED`
- **THEN** the system records and displays it and takes no brokerage action

<!-- tests: NONE -->
