# Spec Delta

## Purpose

Defines how APEX tells a person something: which channels can deliver, who may be reached, how each person's routing, filters and quiet hours decide whether a message goes out, the outbox and delivery worker that send it, and the alerts the system produces. Every notification is informational, owner-scoped and free of amounts in its brief form. Baseline at HEAD `ca450dd`.

## ADDED Requirements

### Requirement: Only email and phone push can deliver
The system SHALL deliver notifications only on the `EMAIL` and `PUSH` channels. `WHATSAPP` and `SMS` SHALL exist as reserved channels with no sender, and `SLACK` SHALL exist only so the legacy value still reads. None of the three MUST ever be switched on, routed, or given a contact.

#### Scenario: Reserved channel cannot be enabled
- **WHEN** an administrator tries to turn on the WhatsApp or SMS switch in the global settings
- **THEN** the change is refused and the stored settings are unchanged

#### Scenario: Reserved channel stub sends nothing
- **WHEN** a message is handed to the WhatsApp or SMS sender
- **THEN** it returns "skipped" with reason "channel not implemented" and sends nothing

<!-- tests: tests/test_notification_model.py::test_effective_channels_only_email_and_push_ever_deliver, tests/test_notification_delivery.py::test_the_whatsapp_and_sms_stubs_send_nothing_and_are_never_reachable, tests/test_notification_model.py::test_invalid_settings_are_refused_and_change_nothing -->

### Requirement: Alert categories
Every notification SHALL belong to exactly one category: `MARKET_EVENT`, `PORTFOLIO_HEALTH`, `COMPLIANCE`, `DAILY_DIGEST` or `SYSTEM`. A notification with an unknown category MUST NOT be enqueued.

#### Scenario: Unknown category
- **WHEN** a producer asks to notify with a category outside the five
- **THEN** nothing is enqueued and the call reports an error without raising

<!-- tests: tests/test_notification_delivery.py::test_notify_never_raises_into_its_caller -->

### Requirement: Five-condition delivery rule
A channel SHALL deliver a message in a category to a person only if all hold: the channel is an active channel; it is switched on globally (email also needs an SMTP host and a from-address, push needs an ntfy server); routing is on for that category; the person has a contact on it; and that contact is verified and not opted out. When one fails, the first failing reason in that order SHALL be reported.

#### Scenario: First failing reason reported
- **WHEN** a person has routing off for a category and no contact on the channel
- **THEN** the channel is reported as not routed, not as having no contact

#### Scenario: Unverified alternate email
- **WHEN** a person's only verified email is their sign-in email and an alternate email is unverified
- **THEN** messages go to the sign-in email only

<!-- tests: tests/test_notification_model.py::test_a_channel_delivers_only_if_all_five_conditions_hold, tests/test_notification_model.py::test_a_verified_alternate_email_is_a_second_deliverable_contact_and_an_unverified_one_is_not, tests/test_notification_delivery.py::test_nothing_is_enqueued_for_a_channel_that_is_not_deliverable_and_the_reason_is_reported -->

### Requirement: Sign-in email is a verified contact
The system SHALL create each person's `EMAIL` contact of kind `LOGIN` from their sign-in address, verified from the start, and SHALL update it when the sign-in address changes. A person with no sign-in email MUST get no email contact, and the sign-in contact MUST NOT be removable (it can be opted out).

#### Scenario: Created and followed
- **WHEN** a person's contacts are first read and later their sign-in address changes
- **THEN** a verified sign-in contact exists and carries the new address

#### Scenario: Removal refused
- **WHEN** a person tries to remove the sign-in contact
- **THEN** the removal is refused with a message to opt out instead

<!-- tests: tests/test_notification_model.py::test_the_sign_in_email_becomes_a_verified_contact_automatically_and_follows_a_change, tests/test_notification_model.py::test_a_person_with_no_sign_in_email_gets_no_email_contact, tests/test_notification_model.py::test_login_email_cannot_be_removed_and_opt_out_and_in_are_audited_once_per_change -->

### Requirement: One alternate email
A person MAY add at most one alternate email, which SHALL start unverified and receive nothing until verified. The address MUST be a valid email and MUST NOT equal the sign-in address.

#### Scenario: Second alternate refused
- **WHEN** a person who already has an alternate email adds another
- **THEN** it is refused and the existing one is kept

#### Scenario: Same as sign-in refused
- **WHEN** a person adds their sign-in address as the alternate
- **THEN** it is refused as "already your sign-in email"

<!-- tests: tests/test_notification_model.py::test_one_alternate_email_unverified_and_never_the_same_as_the_login -->

### Requirement: Push topic is private and unguessable
A person's phone-push contact SHALL be a generated topic of `apex-` followed by 32 random hexadecimal characters, unique across people and unverified until the person confirms a test push arrived. Regenerating the topic MUST replace it, mark it unverified and stop the old one working.

#### Scenario: Topic format and uniqueness
- **WHEN** two people set up push
- **THEN** each has a different topic matching `apex-` plus 32 hex characters, and neither is verified

#### Scenario: Regenerate after a leak
- **WHEN** a person regenerates their topic
- **THEN** the contact carries a new topic and is unverified

<!-- tests: tests/test_notification_model.py::test_push_topic_is_long_random_unique_per_person_and_unverified_until_confirmed, tests/test_notification_pages.py::test_replace_topic_opt_out_and_remove_work_and_the_login_email_has_no_remove_button -->

### Requirement: Unavailable channels are refused clearly
Adding a contact or a routing entry on `WHATSAPP`, `SMS` or `SLACK` SHALL be refused with a "not available yet" message, and an unknown channel or category MUST be refused. A routing entry for a channel a category does not allow (a digest on push) MUST be refused.

#### Scenario: Reserved contact refused
- **WHEN** a person tries to add a WhatsApp, SMS or Slack contact
- **THEN** it is refused as not available yet and nothing is stored

#### Scenario: Digest on push refused
- **WHEN** a person tries to route the daily digest to push
- **THEN** it is refused because the digest is not sent by push

<!-- tests: tests/test_notification_model.py::test_whatsapp_sms_and_slack_contacts_are_refused_clearly, tests/test_notification_model.py::test_routing_refuses_unavailable_channels_and_combinations_and_audits_only_changes -->

### Requirement: Contacts are private to their owner
Every contact read and write SHALL act only on the verified actor's own rows. A contact id belonging to someone else MUST behave exactly like one that does not exist.

#### Scenario: Someone else's contact id
- **WHEN** person A uses a contact id that belongs to person B to opt out, remove or verify
- **THEN** the call fails as "no such contact" and B's contact is unchanged

<!-- tests: tests/test_notification_model.py::test_contacts_are_owner_scoped_someone_elses_id_looks_like_no_such_contact, tests/test_notification_pages.py::test_every_read_is_the_actors_own_one_person_never_sees_anothers_contacts_or_notifications, tests/test_notification_delivery.py::test_verification_is_refused_for_the_login_email_or_when_email_is_off_or_for_someone_elses_contact -->

### Requirement: Addresses and secrets never appear raw
Email addresses and push topics SHALL NOT appear raw in logs, audit events, error messages, the outbox error text or any admin view. They SHALL appear only as a short non-reversible tag, or (admin views) as a partial mask. An error string stored from a send MUST have secret values, email addresses and push topics scrubbed.

#### Scenario: Tag in audit and logs
- **WHEN** a contact is added, removed or opted out
- **THEN** the audit event and log line carry the contact's tag and never the address

#### Scenario: Scrubbed send error
- **WHEN** an SMTP or ntfy error text contains a password, an address or a topic
- **THEN** the stored error shows `[secret]`, an address tag or `[topic]` in their place

<!-- tests: tests/test_notification_model.py::test_addresses_are_masked_tags_in_logs_audit_and_errors, tests/test_notification_delivery.py::test_secrets_and_addresses_never_reach_the_outbox_the_logs_or_the_audit_trail, tests/test_notification_delivery.py::test_email_errors_are_classified_retry_or_failed_and_scrubbed, tests/test_notification_delivery.py::test_ntfy_network_errors_retry_and_never_leak_the_topic_or_token -->

### Requirement: Alternate email verification code
Verifying an alternate email SHALL send a 6-digit code stored only as a hash, valid for `verification_code_ttl_minutes` (default 10), with at most one code per 60 seconds. At most `verification_max_attempts` (default 5) guesses SHALL be counted, wrong ones included. A correct code MUST verify the contact; an expired or over-limit code MUST require a new request.

#### Scenario: Wrong guesses exhaust the code
- **WHEN** a person submits more than the allowed number of wrong codes
- **THEN** the code is cleared and a new one must be requested

#### Scenario: Code that could not be sent
- **WHEN** the verification email fails to send
- **THEN** the stored code is cleared and no cooldown starts

#### Scenario: Not for the sign-in email
- **WHEN** a person asks to verify their sign-in email, or email is switched off globally
- **THEN** the request is refused

<!-- tests: tests/test_notification_delivery.py::test_email_code_flow_hashes_expires_limits_attempts_and_rate_limits_resends, tests/test_notification_delivery.py::test_too_many_wrong_codes_and_expired_codes_need_a_new_code, tests/test_notification_delivery.py::test_a_code_that_could_not_be_sent_clears_itself_and_does_not_start_the_cooldown, tests/test_notification_delivery.py::test_verification_is_refused_for_the_login_email_or_when_email_is_off_or_for_someone_elses_contact -->

### Requirement: Push verification by test and confirmation
A person SHALL verify their push contact by sending a test push and then confirming it arrived. The confirmation MUST be accepted only within 15 minutes after a test push was actually sent successfully.

#### Scenario: Test then confirm
- **WHEN** a person sends a test push successfully and confirms within 15 minutes
- **THEN** the push contact becomes verified

#### Scenario: Stale or failed test
- **WHEN** the test was sent long ago, or the test failed to send
- **THEN** the confirmation is refused with "send a test notification first"

<!-- tests: tests/test_notification_delivery.py::test_a_push_test_then_i_got_it_verifies_the_push_contact, tests/test_notification_delivery.py::test_confirming_push_is_refused_when_the_test_was_long_ago_and_a_failed_test_does_not_count -->

### Requirement: Test sends
A person SHALL be able to send a test notification on email or push, at most one per 30 seconds per channel. An email test MUST go only to verified, not-opted-out email contacts, and a test on a channel that is off globally or has no usable contact MUST be refused.

#### Scenario: Email test recipients
- **WHEN** a person has a verified sign-in email and an unverified alternate and sends an email test
- **THEN** only the verified address receives it

#### Scenario: Cooldown
- **WHEN** a second test on the same channel is requested within 30 seconds
- **THEN** it is refused with "a test was just sent"

<!-- tests: tests/test_notification_delivery.py::test_an_email_test_goes_only_to_verified_not_opted_out_addresses_and_unavailable_channels_are_refused, tests/test_notification_pages.py::test_send_test_buttons_follow_what_is_switched_on_and_report_the_result -->

### Requirement: Default routing for a new person
A new person SHALL get this routing: market events on push and email; portfolio health on email only; compliance on push and email; daily digest on email only and off; system on email and push.

#### Scenario: Fresh defaults
- **WHEN** a person's routing is first read
- **THEN** it matches the defaults above

<!-- tests: tests/test_notification_model.py::test_a_new_person_gets_the_specified_routing_defaults, tests/test_notification_model.py::test_someone_added_after_routing_existed_gets_the_new_defaults_not_the_legacy_mapping -->

### Requirement: Routing changes are validated and audited
A person SHALL be able to switch one category on one channel on or off. Only changes that alter the stored value SHALL be audited.

#### Scenario: No-op change
- **WHEN** a person sets a routing cell to the value it already has
- **THEN** no audit event is written

<!-- tests: tests/test_notification_model.py::test_routing_refuses_unavailable_channels_and_combinations_and_audits_only_changes, tests/test_notification_pages.py::test_saving_the_routing_grid_changes_only_what_was_edited_and_audits_it -->

### Requirement: Legacy single-channel setting is migrated once
The deprecated single `channel` setting SHALL remain readable. A person who had `email` SHALL keep email routing on for exactly market events and portfolio health; `none`, `slack` or unknown SHALL turn every route off. The migration MUST run once and never overwrite a later choice.

#### Scenario: Email user migrated
- **WHEN** an existing person with channel `email` is migrated
- **THEN** only market events and portfolio health are routed to email

#### Scenario: Re-running the migration
- **WHEN** the migration runs again after the person changed their routing
- **THEN** their later choices are unchanged

<!-- tests: tests/test_notification_model.py::test_existing_email_users_keep_exactly_what_they_had_and_everyone_else_is_switched_off, tests/test_notification_model.py::test_the_migration_is_idempotent_and_never_overwrites_a_later_choice, tests/test_notification_model.py::test_the_oldest_singleton_table_shape_also_migrates -->

### Requirement: Personal notification preferences
Each person SHALL have preferences with these defaults: content level `BRIEF`, quiet-hours mode `DEFER`, digest time `07:30`, minimum severity 3, no minimum conviction and no quiet hours. Content level MUST be `BRIEF` or `DETAILED`, quiet-hours mode `DEFER` or `SUPPRESS`, and times `HH:MM`. Only fields that change SHALL be audited.

#### Scenario: Defaults and normalization
- **WHEN** a person's preferences are first read, then digest time is set to "6:5"
- **THEN** the defaults above apply first and the digest time is stored as "06:05"

#### Scenario: Half a quiet-hours window
- **WHEN** a person saves a quiet-hours start without an end
- **THEN** the screen rejects it in plain words

<!-- tests: tests/test_notification_model.py::test_new_preference_columns_default_validate_and_audit_only_what_changed, tests/test_user_notifications_db.py::test_defaults_seeded_on_first_access, tests/test_user_notifications_db.py::test_update_rejects_bad_input, tests/test_notification_pages.py::test_saving_preferences_persists_them_and_rejects_half_a_quiet_hours_window -->

### Requirement: Severity and conviction filters
A notification with a severity below the person's minimum severity SHALL be skipped for all channels. A notification with no severity MUST skip the severity filter. A conviction threshold MUST apply only when both the threshold and a conviction exist.

#### Scenario: Below threshold
- **WHEN** a severity-2 notification is offered to a person whose minimum is 3
- **THEN** it is skipped with reason `BELOW_SEVERITY` and nothing is enqueued

<!-- tests: tests/test_notification_delivery.py::test_severity_and_conviction_filters_use_the_persons_thresholds, tests/test_notification_delivery.py::test_personal_filters_and_quiet_hour_boundaries -->

### Requirement: Push is always brief
A `PUSH` message SHALL always carry the brief text. An `EMAIL` message SHALL carry the detailed text only when the person chose `DETAILED` and a detailed text was supplied; otherwise it carries the brief text. The brief text MUST contain no amounts, position sizes or portfolio values.

#### Scenario: Detailed email, brief push
- **WHEN** a person who chose DETAILED receives one notification with a detailed text on email and push
- **THEN** the email carries the detailed text and the push carries the brief text

<!-- tests: tests/test_notification_delivery.py::test_push_is_always_brief_and_email_is_detailed_only_when_chosen_and_supplied, tests/test_notification_producers.py::test_brief_text_has_no_amounts_and_the_detailed_text_adds_the_summary_and_why -->

### Requirement: Quiet hours
Quiet hours SHALL be evaluated in the recipient's profile timezone and may span midnight; a window whose start equals its end is disabled. With mode `DEFER` a notification arriving inside quiet hours SHALL be held until they end; with `SUPPRESS` it MUST be recorded as suppressed with reason `QUIET_HOURS`.

#### Scenario: Deferred to the end of the window
- **WHEN** a notification arrives at 23:00 in the recipient's timezone with quiet hours 22:00 to 07:00 and mode DEFER
- **THEN** it is held and becomes due at 07:00 the next day

#### Scenario: Suppressed
- **WHEN** the same notification arrives and the mode is SUPPRESS
- **THEN** it is recorded as suppressed with reason `QUIET_HOURS`

<!-- tests: tests/test_notification_delivery.py::test_quiet_hours_defer_holds_until_they_end_in_the_recipients_timezone, tests/test_notification_delivery.py::test_quiet_hours_suppress_drops_what_arrives_inside_them, tests/test_user_notifications_db.py::test_in_quiet_hours_same_day_overnight_and_disabled, tests/test_notification_producers.py::test_digests_are_per_owner_and_quiet_hours_defer_them -->

### Requirement: Hourly message cap
A person SHALL receive at most `max_messages_per_user_per_hour` (default 10) accepted messages per channel per rolling hour. A message over the cap SHALL be recorded as suppressed with reason `RATE_LIMITED` and flagged for the next digest. The cap MUST NOT apply to the daily digest itself.

#### Scenario: Over the cap
- **WHEN** a third message is offered within an hour to a person with a cap of two
- **THEN** it is suppressed as rate-limited and flagged for the digest, while the first two are pending

#### Scenario: An hour later
- **WHEN** a message is offered two hours later
- **THEN** it is accepted normally

<!-- tests: tests/test_notification_delivery.py::test_over_the_hourly_limit_a_message_is_folded_into_the_digest_not_dropped -->

### Requirement: One enqueue per owner, key and channel
The same notification SHALL be enqueued once for the same person, dedup key and channel however often a producer re-runs.

#### Scenario: Re-run
- **WHEN** a producer offers the same dedup key twice to one person
- **THEN** the second call enqueues nothing and reports the channel as a duplicate

#### Scenario: Different person or channel
- **WHEN** the same dedup key is offered to another person
- **THEN** it is enqueued for that person

<!-- tests: tests/test_notification_delivery.py::test_dedup_is_per_owner_key_and_channel, tests/test_scheduler_notifications.py::test_notification_delivery_is_deduplicated_by_event_and_channel, tests/test_scheduler_notifications.py::test_a_different_event_id_for_the_same_ticker_still_delivers -->

### Requirement: Notify only enqueues and never raises
`notify` SHALL only write outbox rows: it MUST make no network call, and it MUST NOT raise into its caller; a failure SHALL be logged and returned as an error name. A notification is per person and MUST NOT reach anyone else.

#### Scenario: Failure inside notify
- **WHEN** a store used by notify fails
- **THEN** the caller receives a result with an error and no exception

#### Scenario: Per-owner isolation
- **WHEN** two people are notified about the same event
- **THEN** each person's outbox holds only their own rows

<!-- tests: tests/test_notification_delivery.py::test_notify_fans_out_to_every_deliverable_channel_and_only_enqueues, tests/test_notification_delivery.py::test_notify_never_raises_into_its_caller, tests/test_notification_delivery.py::test_notifications_are_per_owner_nobody_else_gets_anyone_elses -->

### Requirement: Worker claims rows atomically
The delivery worker SHALL claim due rows (`PENDING` or `DEFERRED` whose due time has passed) in one immediate transaction that marks them `SENDING`, so two workers never claim the same row. A `SENDING` row older than 600 seconds SHALL be reclaimed.

#### Scenario: Concurrent workers
- **WHEN** two workers run at the same time over the same due rows
- **THEN** every row is claimed by exactly one of them

#### Scenario: Dead worker
- **WHEN** a row has been `SENDING` for more than 600 seconds
- **THEN** a later worker claims it again

<!-- tests: tests/test_notification_delivery.py::test_concurrent_workers_can_never_claim_the_same_row, tests/test_notification_delivery.py::test_a_row_abandoned_by_a_dead_worker_is_reclaimed_after_the_timeout -->

### Requirement: Deliverability is re-checked at send time
Before sending, the worker SHALL re-evaluate the five-condition rule. A row whose channel was switched off, routing changed, or contact opted out, removed or unverified since enqueue MUST become suppressed with a reason and MUST NOT be sent.

#### Scenario: Opt-out after enqueue
- **WHEN** a person opts out of a contact after a row was queued for it
- **THEN** the worker marks the row suppressed with reason `OPTED_OUT` and sends nothing

#### Scenario: Routing turned off after enqueue
- **WHEN** routing for the category is turned off after enqueue
- **THEN** the row is suppressed with reason `ROUTING_OFF`

<!-- tests: tests/test_notification_delivery.py::test_an_opt_out_after_enqueue_is_honoured_at_send_time, tests/test_notification_delivery.py::test_routing_or_global_changes_after_enqueue_also_suppress_at_send_time -->

### Requirement: Sending holds no database connection
The worker SHALL send each message with no database connection open, and SHALL record the outcome afterwards. One failing row MUST NOT stop the others, and the worker MUST NOT raise.

#### Scenario: No connection during send
- **WHEN** a message is being sent
- **THEN** no database connection is held open by the worker

#### Scenario: One bad row
- **WHEN** processing one row raises an error
- **THEN** that row is marked failed and the remaining rows are still processed

<!-- tests: tests/test_notification_delivery.py::test_no_database_connection_is_held_while_a_message_is_being_sent, tests/test_notification_delivery.py::test_one_bad_row_never_strands_the_others -->

### Requirement: Retry with exponential backoff
A transient send failure SHALL return the row to `PENDING` due after `retry_backoff_seconds` × 2^(attempt−1), capped at one day, until `retry_max_attempts` (default 5) attempts have been made, after which the row is `FAILED`. A permanent failure MUST mark the row `FAILED` at once, and a skip MUST mark it suppressed.

#### Scenario: Backoff then fail
- **WHEN** a send keeps returning a transient failure with a 60-second base and a limit of three attempts
- **THEN** retries are due after 60 then 120 seconds and the third failure marks the row `FAILED`

#### Scenario: Permanent failure
- **WHEN** a send fails with a bad login
- **THEN** the row is `FAILED` on the first attempt

<!-- tests: tests/test_notification_delivery.py::test_transient_failures_retry_with_exponential_backoff_then_fail_after_the_limit, tests/test_notification_delivery.py::test_a_permanent_failure_fails_at_once_and_a_skip_is_suppressed -->

### Requirement: Administrator requeue of failed rows
An enabled administrator SHALL be able to requeue a `FAILED` row, which resets it to `PENDING` with a fresh attempt count and an audit event. Requeuing a row that is not `FAILED` MUST do nothing.

#### Scenario: Requeue
- **WHEN** an administrator requeues a failed row
- **THEN** it returns to `PENDING` with zero attempts and an audit event exists

#### Scenario: Not failed
- **WHEN** a requeue targets a row that is not failed
- **THEN** it returns false and nothing changes

<!-- tests: tests/test_notification_pages.py::test_failed_notifications_show_masked_recipients_never_addresses_bodies_or_names_and_retry_is_audited, tests/test_notification_delivery.py::test_transient_failures_retry_with_exponential_backoff_then_fail_after_the_limit -->

### Requirement: Dry run
With the global `dry_run` setting on, the worker SHALL send nothing: it SHALL log what it would send, mark the row `SENT` with provider id `dry-run`, and count it as a dry run.

#### Scenario: Dry run on
- **WHEN** the worker runs with dry run on
- **THEN** no sender is called and rows are marked `SENT` with provider id `dry-run`

<!-- tests: tests/test_notification_delivery.py::test_dry_run_sends_nothing_and_logs_what_it_would_send -->

### Requirement: Sent rows keep the legacy delivery log in step
When a row is sent, the system SHALL record the person, dedup key and channel in the legacy delivery log so earlier "was it delivered" checks keep working.

#### Scenario: Log after send
- **WHEN** the worker sends a row successfully
- **THEN** the delivery log reports that dedup key as delivered for that person and channel

<!-- tests: tests/test_notification_delivery.py::test_the_worker_sends_records_the_outcome_and_keeps_notification_log_in_step, tests/test_scheduler_notifications.py::test_notification_log_is_private_and_never_touches_shared_prediction_tables -->

### Requirement: Email format and transport
Email SHALL be sent over SMTP with STARTTLS when enabled, logging in with credentials read from the environment at send time, with a network timeout. Each message SHALL carry plain-text and HTML parts with HTML-escaped content, an `Auto-Submitted` header, an informational-only footer, a manage link, and a `List-Unsubscribe` header and footer link when the app URL and signing key are configured.

#### Scenario: Footer and header
- **WHEN** an email is built with the app URL and signing key configured
- **THEN** it carries a one-click unsubscribe link in the `List-Unsubscribe` header and the footer, and "Informational only — not investment advice."

#### Scenario: Missing configuration
- **WHEN** the app URL or signing key is missing
- **THEN** no unsubscribe or "Open in APEX" link is included

<!-- tests: tests/test_notification_delivery.py::test_email_sender_speaks_smtp_with_starttls_login_from_env_and_a_footer_with_unsubscribe, tests/test_notification_delivery.py::test_email_html_is_escaped_and_links_are_omitted_when_the_app_url_or_key_is_missing, tests/test_notification_delivery.py::test_email_sender_needs_credentials_and_a_recipient_and_always_sets_a_timeout -->

### Requirement: Send result classification
A transient failure (timeout, dropped connection, SMTP 4xx, ntfy 429 or 5xx) SHALL be reported as retry. A permanent failure (bad login, refused recipient, ntfy 401, 403 or 404, missing credentials) SHALL be reported as failed. Nothing to do (no recipient, channel not configured) SHALL be reported as skipped.

#### Scenario: ntfy status codes
- **WHEN** ntfy answers 429, 500, 401 or 404
- **THEN** 429 and 500 are retry and 401 and 404 are failed

#### Scenario: Missing SMTP credentials
- **WHEN** the SMTP username or password is not set in the environment
- **THEN** the send is reported as failed

<!-- tests: tests/test_notification_delivery.py::test_email_errors_are_classified_retry_or_failed_and_scrubbed, tests/test_notification_delivery.py::test_ntfy_status_codes_map_to_retry_or_failed, tests/test_notification_delivery.py::test_email_sender_needs_credentials_and_a_recipient_and_always_sets_a_timeout -->

### Requirement: Push delivery through ntfy
A push SHALL be one JSON post per recipient topic, with the topic in the body, not the URL, a priority from the severity (1 to 5, default 3), the click link when present and an optional bearer token from the environment.

#### Scenario: Payload
- **WHEN** a push with severity 4 and a link is sent
- **THEN** the post body carries the topic, title, message, priority 4 and the click link

<!-- tests: tests/test_notification_delivery.py::test_ntfy_sender_posts_json_with_the_topic_in_the_body_priority_click_and_optional_token -->

### Requirement: One-click unsubscribe
Every email footer link SHALL carry a signed token naming one contact of one person. Confirming on the unsubscribe page, which needs no login, SHALL opt out that one contact and nothing else, idempotently, with one audit event. A bad, missing or tampered token MUST change nothing and MUST NOT reveal why it was bad.

#### Scenario: Confirm required
- **WHEN** someone opens the unsubscribe link
- **THEN** the page asks for one click and opts out nothing until it is given

#### Scenario: Repeat click
- **WHEN** the same link is confirmed twice
- **THEN** the contact stays opted out and only one audit event exists

#### Scenario: Tampered token
- **WHEN** a token has been altered
- **THEN** the page says the link is not valid and no contact changes

<!-- tests: tests/test_notification_delivery.py::test_unsubscribe_tokens_are_signed_and_tamper_proof, tests/test_notification_delivery.py::test_one_click_unsubscribe_opts_out_exactly_one_contact_idempotently_and_audits_once, tests/test_notification_pages.py::test_the_unsubscribe_page_needs_no_login_asks_to_confirm_and_opts_out_one_contact, tests/test_notification_pages.py::test_the_unsubscribe_page_rejects_missing_and_bad_tokens_without_changing_anything -->

### Requirement: Global notification settings
Global notification settings SHALL be admin-only (an enabled administrator checked in the service), versioned with `expected_version`, validated, audited with before and after values, and resettable to the seed. The seed SHALL be email and push on, WhatsApp and SMS off, `smtp.gmail.com` port 587 with TLS, `https://ntfy.sh`, 10 messages per person per hour, 5 retry attempts, 60-second backoff, 10-minute code lifetime, 5 code attempts and dry run off.

#### Scenario: Email without a sender address
- **WHEN** an administrator turns email on with no SMTP host or from-address
- **THEN** the change is refused

#### Scenario: Stale version
- **WHEN** an update carries an out-of-date `expected_version`
- **THEN** it is refused and nothing changes

#### Scenario: Non-admin
- **WHEN** a non-administrator calls an update
- **THEN** it is refused and nothing changes

<!-- tests: tests/test_notification_model.py::test_settings_are_seeded_exactly_as_specified_and_versioned, tests/test_notification_model.py::test_update_bumps_the_version_conflicts_when_stale_and_resets_to_defaults, tests/test_notification_model.py::test_email_cannot_be_enabled_without_a_host_and_a_from_address, tests/test_notification_model.py::test_global_settings_are_admin_only_audited_and_never_expose_a_secret, tests/test_notification_pages.py::test_a_non_admin_who_somehow_renders_the_admin_section_is_refused_by_the_service -->

### Requirement: Secrets live only in the environment
Secrets (SMTP username and password, ntfy token, unsubscribe signing key) SHALL come only from environment variables. The database SHALL store only each variable's name, and the admin view SHALL show each as present or missing and never its value.

#### Scenario: Presence only
- **WHEN** an administrator views the settings
- **THEN** each secret shows its variable name and present or missing, with no value

<!-- tests: tests/test_notification_model.py::test_env_presence_reports_present_or_missing_and_never_the_value, tests/test_notification_pages.py::test_the_admin_view_is_admin_only_and_shows_secrets_only_as_present_or_missing -->

### Requirement: Market events reach only holders and watchers
A detected market event SHALL be offered to every person who holds the ticker in a real (non-paper) account or watches it, and to nobody else. Each person SHALL be judged by their own routing, severity, quiet hours and content level, with dedup key `event:<event id>`. Their own clearance decision SHALL be appended to the text as information and MUST NOT filter delivery.

#### Scenario: Holder and non-holder
- **WHEN** an event fires on a ticker one person holds and another neither holds nor watches
- **THEN** only the holder is notified

#### Scenario: Restricted ticker still notifies
- **WHEN** the holder has the ticker on their restricted list
- **THEN** the notification is still queued and its brief text notes that no new trade is currently permitted

#### Scenario: Notifier failure
- **WHEN** the notifier raises while offering an event
- **THEN** the detector still returns its events

<!-- tests: tests/test_notification_producers.py::test_an_event_reaches_only_the_owners_who_hold_or_watch_the_ticker, tests/test_notification_producers.py::test_a_ticker_nobody_holds_or_watches_notifies_nobody_and_a_macro_event_is_not_anyones, tests/test_notification_producers.py::test_each_owner_is_judged_by_their_own_preferences_and_dedup, tests/test_notification_producers.py::test_the_clearance_note_is_evaluated_per_owner_and_never_filters, tests/test_scheduler_notifications.py::test_a_restricted_or_unevaluatable_action_permission_still_notifies_monitoring_is_not_disabled, tests/test_notification_producers.py::test_a_failing_notifier_never_breaks_the_detector -->

### Requirement: Compliance notifications
A recorded or withdrawn pre-clearance approval SHALL notify its owner once (`approval:<id>:granted` or `approval:<id>:revoked`); an active approval expiring within 24 hours SHALL notify once (`approval:<id>:expiring:<expiry>`); a restricted-list change SHALL notify only an owner who holds the ticker (`restricted:<ticker>:<added|removed>:<time>`). Each SHALL have severity 4 and category `COMPLIANCE`, and a notification failure MUST NOT undo or block the compliance record.

#### Scenario: Expiry announced once
- **WHEN** the expiry scan runs repeatedly for an approval expiring in 20 hours
- **THEN** one notification is enqueued for it

#### Scenario: Restricted change for a non-holder
- **WHEN** a ticker is added to the list of an owner who does not hold it
- **THEN** no notification is queued

<!-- tests: tests/test_notification_producers.py::test_approval_granted_and_withdrawn_notify_their_owner_once_each, tests/test_notification_producers.py::test_approvals_expiring_within_a_day_are_announced_once_and_only_while_active, tests/test_notification_producers.py::test_restricted_list_changes_notify_only_the_owner_who_holds_the_ticker, tests/test_notification_producers.py::test_a_notification_failure_can_never_undo_or_block_a_compliance_record -->

### Requirement: Health and ledger-clearance alerts use stable dedup keys
Portfolio Health alerts SHALL use dedup key `health:<check>:<date the run of ACTION began>` (with the account scope for an account-level check) and ledger clearance alerts `ledger_clearance:<transaction id>`. Their brief text MUST carry no amounts, and the health brief MUST name only the check.

#### Scenario: Health key and brief
- **WHEN** a health check becomes ACTION
- **THEN** the notification uses the `health:` key shape and its brief names the check without amounts

<!-- tests: tests/test_notification_producers.py::test_health_alerts_use_notify_with_the_same_dedup_key_and_no_amounts_in_the_brief -->

### Requirement: Daily digest
The daily digest SHALL be an email-only notification, off by default, built at each person's digest time (default 07:30) in their timezone, at most one per person per local day (dedup key `digest:<local date>`). It SHALL roll up the person's Portfolio Health checks needing action or watch, the last 24 hours of market events on tickers they hold or watch, and items held back by the hourly cap. An empty digest MUST be skipped and leave a suppressed marker row.

#### Scenario: Off by default and waits for its time
- **WHEN** the producers run for a person who has not routed the digest on, or before their digest time
- **THEN** no digest is created

#### Scenario: Rolled up and sent once
- **WHEN** a person has an ACTION check, an event and a rate-limited item and the producers run twice after digest time
- **THEN** one digest carries all three and the second run creates none

#### Scenario: Empty day
- **WHEN** there is nothing to report
- **THEN** a suppressed marker with reason `EMPTY_DIGEST` is written and no email is queued

<!-- tests: tests/test_notification_producers.py::test_the_digest_is_off_by_default_email_only_and_waits_for_its_time, tests/test_notification_producers.py::test_the_digest_rolls_up_health_events_and_rate_limited_items_and_is_sent_once_a_day, tests/test_notification_producers.py::test_a_brief_digest_has_no_detailed_body, tests/test_notification_producers.py::test_an_empty_digest_is_skipped_and_leaves_a_marker_so_it_is_not_rebuilt, tests/test_notification_producers.py::test_digests_are_per_owner_and_quiet_hours_defer_them -->

### Requirement: Notification text is informational
Notification text SHALL be informational and MUST NOT be phrased as a trade instruction; the brief form and every push MUST carry no amounts, position sizes or portfolio values.

#### Scenario: No trade instruction
- **WHEN** the text of every producer's notification is inspected
- **THEN** none instructs a person to buy, sell or hold

<!-- tests: tests/test_notification_producers.py::test_no_notification_text_is_ever_a_trade_instruction, tests/test_notification_producers.py::test_brief_text_has_no_amounts_and_the_detailed_text_adds_the_summary_and_why -->

### Requirement: Delivery command
`python -m scripts.send_notifications` SHALL queue the clock-driven alerts (expiring approvals and daily digests) and then deliver due rows, up to `--limit` rows (default 50). `--status` SHALL print outbox counts by status and send nothing. The evening batch SHALL also run the producers and the worker, and the command and the worker MUST NOT raise on a problem.

#### Scenario: Status only
- **WHEN** the command runs with `--status`
- **THEN** it prints the outbox counts by status and exits 0

<!-- tests: tests/test_notification_delivery.py::test_the_cli_reports_status_and_delivers_with_no_rows, tests/test_notification_producers.py::test_scheduled_producers_run_together_and_never_raise -->

### Requirement: People see their own notification history
A person SHALL be able to list their own recent notifications (default 50, newest first) with category, channel, status, subject, reason code and attempt data and no address, error text or anything about anyone else. An enabled administrator SHALL additionally see system-wide counts by status and the newest failed rows with the recipient masked, and no body, address or person's name.

#### Scenario: Own history only
- **WHEN** person A lists their notifications
- **THEN** only A's rows appear, without addresses or error text

#### Scenario: Admin failed view
- **WHEN** an administrator views failed notifications
- **THEN** each row shows channel, category, attempts, scrubbed error and a masked recipient

<!-- tests: tests/test_notification_pages.py::test_recent_rows_are_plain_words_with_no_addresses_or_errors, tests/test_notification_pages.py::test_recent_notifications_are_listed_for_the_owner_only, tests/test_notification_pages.py::test_failed_notifications_show_masked_recipients_never_addresses_bodies_or_names_and_retry_is_audited, tests/test_notification_pages.py::test_the_admin_view_is_admin_only_and_shows_secrets_only_as_present_or_missing -->

### Requirement: Notifications screens
The Profile page SHALL give every signed-in person a Notifications section for their contacts, routing grid, preferences, test sends and recent history, showing WhatsApp and SMS as disabled "coming later" columns and no legacy single-channel dropdown, and SHALL show the administrator editor and outbox only to administrators. The section SHALL state that alerts are informational and never tell the person to trade.

#### Scenario: Member view
- **WHEN** a non-administrator opens the Profile page
- **THEN** they see the Notifications section but not the administrator editor

#### Scenario: Reserved columns
- **WHEN** the routing grid renders
- **THEN** the WhatsApp and SMS columns are shown disabled

<!-- tests: tests/test_notification_pages.py::test_the_notifications_section_renders_contacts_routing_preferences_and_disabled_stub_columns, tests/test_notification_pages.py::test_the_profile_page_has_the_new_notifications_section_and_no_legacy_channel_dropdown, tests/test_notification_pages.py::test_the_profile_page_for_a_member_has_the_section_but_not_the_admin_editor, tests/test_notification_pages.py::test_routing_grid_marks_whatsapp_and_sms_coming_later_and_unused_channels_na -->
