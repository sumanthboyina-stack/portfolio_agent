# Spec Delta

## Purpose

Defines how APEX's global, admin-edited configuration is stored and changed: database-backed singleton settings that are seeded from code, versioned, validated, audited and resettable, plus the catalogue of setting keys with their defaults and limits. Per-feature behavior that reads these settings is specified in the capability that owns it. Baseline at HEAD `ca450dd`.

## ADDED Requirements

### Requirement: Global settings live in the database
Global configuration SHALL be stored in database tables (`engine_settings`, `portfolio_settings`, `risk_quiz_settings`, `risk_profile_settings` and the notification settings table), seeded from code constants on first use, and MUST NOT be read from config files. These settings are global, not owner-scoped.

#### Scenario: First use seeds the defaults
- **WHEN** a fresh database is first read
- **THEN** the settings rows exist with their seed values at version 1 and no `updated_by`

#### Scenario: Seeds are not re-applied
- **WHEN** a row already exists and the process restarts
- **THEN** the stored values are kept and the seed does not overwrite them

<!-- tests: tests/test_investor_profile.py::test_global_settings_are_seeded_exactly_as_specified_and_versioned, tests/test_notification_model.py::test_settings_are_seeded_exactly_as_specified_and_versioned -->

### Requirement: Every settings change is versioned
Each change to a settings row SHALL increment its `version` and record `updated_at` and `updated_by` (the verified actor, or a script name for a command-line change).

#### Scenario: Change bumps the version
- **WHEN** an update changes one setting
- **THEN** the row's version increases by one and `updated_by` names who made it

#### Scenario: Engine groups share one counter
- **WHEN** an admin changes any engine-settings group (cadence, horizons, edges, plan, simulation, chat tier or APEX models)
- **THEN** the single `engine_settings.version` increases by one

<!-- tests: tests/test_apex_champion.py::test_engine_settings_updates_persist, tests/test_horizon_cadence.py::test_admin_only_audited_versioned_and_reset, tests/test_chat_tiering.py::test_the_setting_is_versioned_audited_admin_only_and_resettable -->

### Requirement: Stale writes are refused
An update that carries `expected_version` SHALL be refused, changing nothing, when the stored version differs. Engine-settings updates raise a runtime error and the other tables raise `SettingsVersionConflict`.

#### Scenario: Stale expected version
- **WHEN** an admin saves with the version they loaded and another change has landed since
- **THEN** the save is refused with a "reload and retry" message and the stored values are unchanged

#### Scenario: Stale write leaves no audit event
- **WHEN** a stale write to a service-layer setting is refused
- **THEN** no audit event is recorded for it

<!-- tests: tests/test_investor_profile.py::test_updates_are_validated_versioned_and_guard_against_stale_writes, tests/test_bucket_edges.py::test_unchanged_save_does_not_bump_the_version_and_stale_version_is_refused, tests/test_gap_fixes.py::test_horizons_change_and_audit_land_together_and_a_stale_version_leaves_neither, tests/test_notification_model.py::test_update_bumps_the_version_conflicts_when_stale_and_resets_to_defaults -->

### Requirement: Unchanged updates do not bump the version
An update that changes no stored value SHALL NOT increment the version.

#### Scenario: Nothing to change
- **WHEN** an update is called with no fields
- **THEN** the current settings are returned and the version is unchanged

#### Scenario: Same edges
- **WHEN** bucket edges are saved equal to those in force
- **THEN** neither `version` nor `bucket_edges_version` changes

<!-- tests: tests/test_bucket_edges.py::test_unchanged_save_does_not_bump_the_version_and_stale_version_is_refused, tests/test_notification_model.py::test_update_bumps_the_version_conflicts_when_stale_and_resets_to_defaults -->

### Requirement: Invalid settings are refused atomically
An update with an invalid value SHALL raise a validation error, leave every stored value unchanged, and record no audit event. A partial update is validated as the merged result.

#### Scenario: Bad value
- **WHEN** an admin submits a value outside its limits
- **THEN** the update is refused and the stored settings and version are unchanged

#### Scenario: Unknown key
- **WHEN** an update names a setting that does not exist
- **THEN** it is refused

<!-- tests: tests/test_investor_profile.py::test_updates_are_validated_versioned_and_guard_against_stale_writes, tests/test_gap_fixes.py::test_horizons_change_and_audit_land_together_and_a_stale_version_leaves_neither, tests/test_notification_model.py::test_invalid_settings_are_refused_and_change_nothing -->

### Requirement: Partial updates change only the keys given
For settings held as maps (instrument-type horizons, horizon cadence, bucket edges, trade-plan and simulation settings), an update SHALL change only the keys supplied and keep the rest.

#### Scenario: One horizon
- **WHEN** an admin sets the cadence of 63 days only
- **THEN** the cadence of 5, 21 and 250 days is unchanged

#### Scenario: One instrument type
- **WHEN** an admin sets horizons for `ETF` only
- **THEN** every other instrument type keeps its horizons

<!-- tests: tests/test_horizon_cadence.py::test_admin_only_audited_versioned_and_reset, tests/test_gap_fixes.py::test_horizons_change_and_audit_land_together_and_a_stale_version_leaves_neither, tests/test_trade_plan.py::test_settings_are_seeded_and_updatable_and_validated -->

### Requirement: Global settings are admin-only in the service
Every service verb that changes a global setting SHALL require a verified context whose owner maps to an enabled `auth_users` row with role `admin`, and MUST refuse a member, an unknown identity and a disabled admin with `NotAdmin`, regardless of what the UI hides.

#### Scenario: Member refused
- **WHEN** a member calls a settings verb directly
- **THEN** `NotAdmin` is raised and nothing is written or audited

#### Scenario: Disabled admin refused
- **WHEN** an admin whose user row is disabled calls a settings verb
- **THEN** `NotAdmin` is raised

#### Scenario: Enabled admin allowed
- **WHEN** an enabled admin calls a settings verb with valid input
- **THEN** the change is applied

<!-- tests: tests/test_investor_profile.py::test_global_settings_are_admin_only_even_when_the_ui_is_bypassed, tests/test_horizon_cadence.py::test_admin_only_audited_versioned_and_reset, tests/test_bucket_edges.py::test_edges_validation_and_admin_only_and_reset, tests/test_notification_model.py::test_global_settings_are_admin_only_audited_and_never_expose_a_secret -->

### Requirement: Admin changes are audited with the change
Each admin settings change SHALL be written in one database transaction with an audit event carrying the actor, source, operation, before, after and optional reason, so the change and its audit record land together or not at all.

#### Scenario: Audited change
- **WHEN** an admin changes the default emergency reserve with a reason
- **THEN** one `update_portfolio_settings` event holds the old and new values, the actor and the reason

#### Scenario: Rolled back together
- **WHEN** a settings change is refused or fails
- **THEN** no audit event exists for it

<!-- tests: tests/test_investor_profile.py::test_admin_changes_write_audit_events_with_before_and_after, tests/test_gap_fixes.py::test_horizons_change_and_audit_land_together_and_a_stale_version_leaves_neither -->

### Requirement: Audit operation names
Admin verbs SHALL audit under these operation names: `update_quiz_settings`, `reset_quiz_settings`, `update_risk_profile_settings`, `reset_risk_profile_settings`, `update_portfolio_settings`, `reset_portfolio_settings`, `update_instrument_type_horizons`, `update_horizon_bucket_edges`, `update_horizon_cadence`, `update_trade_plan_settings`, `update_sim_settings`, `update_chat_synthesis_tier`, `update_notification_settings`, `reset_notification_settings`.

#### Scenario: Reset is audited under the update name
- **WHEN** an admin resets instrument-type horizons, bucket edges, cadence, trade plan, simulation settings or the chat tier
- **THEN** the event uses the matching `update_*` operation name with the reason "reset to defaults" (or "reset to default" for the chat tier)

#### Scenario: Portfolio, quiz and profile resets have their own names
- **WHEN** an admin resets portfolio, quiz or risk-profile settings
- **THEN** the event is `reset_portfolio_settings`, `reset_quiz_settings` or `reset_risk_profile_settings`

<!-- tests: tests/test_investor_profile.py::test_reset_to_defaults_restores_every_table, tests/test_horizon_cadence.py::test_admin_only_audited_versioned_and_reset, tests/test_notification_model.py::test_global_settings_are_admin_only_audited_and_never_expose_a_secret -->

### Requirement: Reset to defaults
Each settings group SHALL offer a reset that restores its seed values as a versioned, audited write. Resetting engine settings MUST restore weights, guardrail thresholds, instrument-type horizons, cadence, trade-plan and simulation settings and bucket edges, and MUST leave APEX model selection unchanged.

#### Scenario: Reset one risk profile
- **WHEN** an admin resets only the `AGGRESSIVE` profile
- **THEN** its seed numbers return and the other profiles keep their edited values

#### Scenario: Reset all
- **WHEN** an admin resets portfolio, quiz and profile settings
- **THEN** each table holds its seed values at a higher version than before the reset

#### Scenario: Engine reset leaves model selection
- **WHEN** engine settings are reset
- **THEN** the APEX champion, fallbacks, challenger and selection mode keep their stored values

<!-- tests: tests/test_investor_profile.py::test_reset_to_defaults_restores_every_table, tests/test_trade_plan.py::test_settings_are_seeded_and_updatable_and_validated, tests/test_plan_simulator.py::test_sim_settings_defaults_update_validation_and_reset -->

### Requirement: Unreadable stored values fall back to the seed
When a stored settings value is missing or unreadable, the reader SHALL use the seed value for it and MUST NOT guess or raise into the caller.

#### Scenario: Corrupt cadence
- **WHEN** the stored cadence JSON cannot be parsed
- **THEN** the seed cadence is used

#### Scenario: Horizon missing from the stored map
- **WHEN** a horizon or instrument type is absent from the stored map
- **THEN** it takes its seed rule

<!-- tests: tests/test_horizon_cadence.py::test_migration_is_idempotent_and_an_unreadable_value_falls_back_to_the_seed, tests/test_horizon_cadence.py::test_scheduler_config_falls_back_to_seed_if_settings_unreadable -->

### Requirement: Settings hold no secrets
Settings tables MUST NOT store API keys, passwords, tokens or the SEC contact string; where a setting needs one it SHALL store only the environment variable name, and admin views SHALL show a secret as present or missing, never its value.

#### Scenario: Secret in the environment
- **WHEN** an admin opens the notification settings view and the SMTP password variable is set
- **THEN** the view reports it as present and its value appears nowhere in the view or in audit events

<!-- tests: tests/test_notification_model.py::test_global_settings_are_admin_only_audited_and_never_expose_a_secret -->

### Requirement: Admin section is shown only to admins
The Profile page SHALL render the admin settings section (Investor settings, Notification settings, Users, Access requests) only when the signed-in user is an admin, and every edit made there SHALL go through the admin service verbs.

#### Scenario: Investor settings tabs
- **WHEN** an admin opens Profile and expands Investor settings
- **THEN** the tabs are Default emergency reserve, Profiles, Quiz, Fund data & snapshots, Health thresholds, Horizons by type, Return buckets, Schedule cadence, Trade Plan, Fund sources and Chat

#### Scenario: Member sees no admin section
- **WHEN** a non-admin opens Profile
- **THEN** no admin section is rendered

<!-- tests: tests/test_investor_profile.py::test_profile_page_admin_section_is_only_for_admins_and_edits_go_through_the_service, tests/test_portfolio_health.py::test_admin_investor_settings_has_the_health_thresholds_in_a_tab -->

### Requirement: Admin form errors are shown, not raised
The admin forms SHALL show a validation error, version conflict or `NotAdmin` refusal as an error message and a success as a confirmation, without raising into the page.

#### Scenario: Refused edit
- **WHEN** an admin submits a value the service refuses
- **THEN** the page shows the refusal message and the stored settings are unchanged

<!-- tests: NONE -->

### Requirement: Emergency reserve default
`portfolio_settings.default_emergency_reserve_amount` SHALL default to 3000.00 (dollars) and MUST be at least 0. How the reserve is used is specified in `investor-profile`.

#### Scenario: Seed
- **WHEN** portfolio settings are first created
- **THEN** the default reserve is 3000 at version 1

#### Scenario: Negative refused
- **WHEN** an admin sets the default reserve below 0
- **THEN** the update is refused

<!-- tests: tests/test_investor_profile.py::test_global_settings_are_seeded_exactly_as_specified_and_versioned, tests/test_investor_profile.py::test_updates_are_validated_versioned_and_guard_against_stale_writes -->

### Requirement: Fund data and look-through settings
`portfolio_settings` SHALL hold `etf_data_ttl_hours` (default 24, above 0 and at most 8760), `mutual_fund_data_ttl_hours` (168, same limits), `lookthrough_min_coverage_pct` (80, 0 to 100), `lookthrough_max_depth` (3, whole number 1 to 6) and `high_expense_ratio_pct` (0.50, 0 to 10). The rules that use them are in `fund-data-and-lookthrough` and `portfolio-health`.

#### Scenario: Valid change
- **WHEN** an admin sets the ETF freshness window to 12 hours and the coverage threshold to 70
- **THEN** both are stored and audited

#### Scenario: Out-of-range values
- **WHEN** an admin sets coverage 120, depth 0 or an ETF window of 0
- **THEN** each is refused

<!-- tests: tests/test_fund_lookthrough.py::test_horizons_and_portfolio_settings_are_admin_only_audited_and_validated, tests/test_fund_lookthrough.py::test_the_minimum_coverage_is_a_setting_not_a_constant -->

### Requirement: Snapshot and lot-method defaults
`portfolio_settings.health_snapshot_enabled` SHALL default to 1 (on) and `default_lot_method` SHALL default to `FIFO` and accept only `FIFO`, `LIFO` or `HIFO`; `SPECIFIC` MUST be refused as a standing default.

#### Scenario: Snapshots off
- **WHEN** an admin turns `health_snapshot_enabled` off
- **THEN** no daily health snapshot is taken

#### Scenario: Default lot method
- **WHEN** neither a sale nor its account names a lot method
- **THEN** the default method is used and stored on the sale

<!-- tests: tests/test_portfolio_health.py::test_snapshots_can_be_turned_off_in_settings, tests/test_ledger.py::test_lot_method_precedence_is_sale_then_account_then_default_and_is_stored_on_the_sale -->

### Requirement: Health threshold settings
`portfolio_settings` SHALL hold ten health thresholds, each with a default and an allowed range, and MUST refuse a value outside its range. The rules that use them are in `portfolio-health`.

#### Scenario: Defaults and ranges
- **WHEN** the thresholds are read at their seed, as (default, min, max)
- **THEN** `health_action_band_multiple` is (2.0, 1, 10), `health_watch_limit_fraction` (0.8, 0, 1), `health_min_effective_n_watch` (8, 1, 1000) and `health_min_effective_n_action` (4, 1, 1000)
- **AND** `health_max_avg_correlation_watch` is (0.70, 0, 1), `health_max_avg_correlation_action` (0.90, 0, 1) and `health_unknown_share_tolerance_pct` (10, 0, 100)
- **AND** `health_unpriced_action_pct` is (25, 0, 100), `health_exclude_unvested` (1, 0, 1) and `health_stale_fund_data_multiple` (2.0, 1, 100)

#### Scenario: Action level above watch level
- **WHEN** an admin sets the effective-N action threshold above the watch threshold
- **THEN** the update is refused

#### Scenario: Correlation action below watch
- **WHEN** an admin sets the correlation action level below the watch level
- **THEN** the update is refused

#### Scenario: Threshold change moves a status
- **WHEN** an admin lowers `health_watch_limit_fraction`
- **THEN** the next health run judges concentration against the new line

<!-- tests: tests/test_portfolio_health.py::test_health_thresholds_are_admin_only_validated_and_reset_to_defaults, tests/test_portfolio_health.py::test_a_threshold_change_in_the_settings_table_changes_the_status, tests/test_retirement_extras.py::test_the_setting_turns_the_exclusion_off_and_the_health_cache_notices -->

### Requirement: Fund source settings
`portfolio_settings` SHALL hold eleven fund-source settings, each with a default and an allowed range, and MUST refuse a value outside its range. Secrets for these sources are never stored here.

#### Scenario: Defaults and ranges
- **WHEN** the settings are read at their seed, as (default, min, max)
- **THEN** `sec_fund_ids_refresh_days` is (7, 1, 365), `nport_requests_per_second` (8, 1, 10) and `nport_http_timeout_seconds` (30, 5, 120)
- **AND** `openfigi_enabled` is (1, 0, 1), `openfigi_batch_size` (10, 1, 100), `mapping_refresh_days` (30, 1, 3650) and `openfigi_max_ids_per_refresh` (300, 1, 5000)
- **AND** `issuer_csv_max_age_days` is (3, 1, 3650), `nport_max_age_days` (150, 1, 3650), `yfinance_max_age_days` (7, 1, 3650) and `fund_holdings_complete_pct` (90, 0, 100)

#### Scenario: SEC rate cap
- **WHEN** an admin sets `nport_requests_per_second` to 11 or 0
- **THEN** the update is refused

#### Scenario: Timeout floor
- **WHEN** an admin sets `nport_http_timeout_seconds` to 1
- **THEN** the update is refused

<!-- tests: tests/test_nport_provider.py::test_fund_source_settings_are_admin_editable_validated_and_capped_at_ten_per_second -->

### Requirement: Statement price window setting
`portfolio_settings.statement_price_max_age_days` SHALL default to 100 and accept 1 to 730; it sets how long a statement-priced fund's price counts as current.

#### Scenario: Change takes effect
- **WHEN** an admin changes the window
- **THEN** the next health run judges statement-priced funds against the new window

#### Scenario: Out-of-range
- **WHEN** an admin sets 0, a negative number, 731 or a non-number
- **THEN** each is refused

<!-- tests: tests/test_statement_freshness.py::test_the_window_is_a_database_setting_with_a_default_validated_and_resettable, tests/test_statement_freshness.py::test_the_window_setting_changes_the_verdict_without_waiting_for_a_cache_to_expire -->

### Requirement: Fund provider order setting
`portfolio_settings.fund_provider_order_json` SHALL hold a non-empty list of distinct sources from `issuer_csv`, `nport`, `yfinance`, defaulting to that order; an unreadable or empty stored value reads as the default.

#### Scenario: Reordered
- **WHEN** an admin sets the order to `nport` then `yfinance`
- **THEN** the default provider chain asks sources in that order

#### Scenario: Repeat or unknown source
- **WHEN** an admin submits a repeated or unknown source
- **THEN** the update is refused

<!-- tests: tests/test_fund_provider_chain.py::test_default_chain_is_built_from_the_database_settings_and_can_be_reordered -->

### Requirement: Risk profile settings seed
`risk_profile_settings` SHALL hold one row per profile seeded as: CONSERVATIVE allocation US_EQUITY 25, INTL_EQUITY 5, FIXED_INCOME 55, CASH 15, limits position 8, issuer 8, sector 20, leveraged/inverse 0, candidate share of cash 40, ETN not allowed; MODERATE 45, 15, 33, 7 with 12, 12, 25, 5, 40; AGGRESSIVE 62, 25, 10, 3 with 20, 20, 35, 15, 40. Bands are plus or minus 5 points (cash plus or minus 3).

#### Scenario: Moderate bands
- **WHEN** the seed is read
- **THEN** MODERATE US_EQUITY runs 40 to 50 and CASH runs 4 to 10, and each profile's targets sum to 100

<!-- tests: tests/test_investor_profile.py::test_global_settings_are_seeded_exactly_as_specified_and_versioned -->

### Requirement: Risk profile settings validation
A risk-profile update SHALL require a known profile, an allocation whose asset classes are allocatable (never `MULTI_ASSET` or `UNKNOWN`), numbers 0 to 100 with min at most target at most max, and targets summing to 100; limit fields 0 to 100 and finite; a non-blank display name; and a non-empty list of valid instrument types.

#### Scenario: Targets not summing to 100
- **WHEN** an admin saves an allocation whose targets sum to 90
- **THEN** the update is refused and the profile is unchanged

#### Scenario: Looked-through class
- **WHEN** an allocation targets `MULTI_ASSET` or `UNKNOWN`
- **THEN** the update is refused

#### Scenario: Unknown profile
- **WHEN** an update names a profile other than the three
- **THEN** it is refused

<!-- tests: tests/test_investor_profile.py::test_updates_are_validated_versioned_and_guard_against_stale_writes -->

### Requirement: Quiz settings seed and validation
`risk_quiz_settings` SHALL hold the quiz questions, the cap rule and the preference thresholds, seeded as three questions (`horizon`, `drop`, `priority`; options worth 0, 1 and 2 points), cap by option `lt3` to CONSERVATIVE, `3to10` to MODERATE, `10plus` to AGGRESSIVE, and preference bands 0 to 1 CONSERVATIVE, up to 3 MODERATE, up to 4 AGGRESSIVE. The three parts MUST be validated together.

#### Scenario: Unreachable top score
- **WHEN** the last preference band cannot reach the highest possible score
- **THEN** the update is refused

#### Scenario: Cap question used as a preference question
- **WHEN** the preference questions include the cap question
- **THEN** the update is refused

#### Scenario: Unmapped cap option
- **WHEN** the cap rule leaves an option of the cap question unmapped
- **THEN** the update is refused

<!-- tests: tests/test_investor_profile.py::test_quiz_settings_validation, tests/test_investor_profile.py::test_global_settings_are_seeded_exactly_as_specified_and_versioned -->

### Requirement: Quiz version bumps on change
Each change to the quiz, including a reset, SHALL increment `quiz_version` as well as `version`, so recorded answers stay tied to the quiz they were taken under.

#### Scenario: Quiz edit
- **WHEN** an admin edits the quiz
- **THEN** `quiz_version` increases by one

<!-- tests: NONE -->

### Requirement: APEX model selection settings
`engine_settings` SHALL hold `apex_champion_model` (default `Claude-Sonnet`), `apex_fallback_models` (`["GPT-5", "GPT-4o", "o3"]`), `apex_challenger_model` (none), `apex_challenger_sample_pct` (0, 0 to 100), `apex_selection_mode` (`champion` or `random_pool`) and `apex_random_pool` (`["GPT-5", "DeepSeek-Chat"]`, at least 2 distinct). Labels MUST be known APEX model labels. How the plan runs is specified in `apex-prediction-engine`.

#### Scenario: Unknown model label
- **WHEN** a champion, fallback, challenger or pool entry is not a known label
- **THEN** the update is refused

#### Scenario: Short pool
- **WHEN** the random pool has fewer than 2 different models
- **THEN** the update is refused

#### Scenario: Challenger off
- **WHEN** the challenger model is set to none or an empty string
- **THEN** the challenger is stored as off

#### Scenario: Percentage out of range
- **WHEN** the challenger sample percentage is 150
- **THEN** the update is refused

<!-- tests: tests/test_apex_champion.py::test_engine_settings_validate_model_labels_and_pct, tests/test_apex_random_pool.py::test_engine_settings_refuse_a_bad_mode_a_short_pool_and_an_unknown_model, tests/test_apex_random_pool.py::test_engine_settings_store_the_mode_and_pool_and_the_plan_loads_them -->

### Requirement: Weight and guardrail settings
`engine_settings` SHALL hold the base weights, per-horizon weights, default weights, `weight_floor` (seed 0.05, allowed 0 to 0.3) and the guardrail thresholds (`no_news_conviction_threshold` 7.0, `no_news_cap` 5.0, `bounce_return_threshold` -0.05, `bounce_cap` 5.0). Each weight map MUST name exactly the sources news, research, macro, fundamentals and valuation with non-negative values, and is normalized to sum to 1.0.

#### Scenario: Weights normalized
- **WHEN** weights are saved that sum to 2.0
- **THEN** each is stored divided by the total, rounded to four decimals

#### Scenario: Wrong sources
- **WHEN** a weight map has a missing or extra source, or a negative weight
- **THEN** the update is refused

#### Scenario: Floor out of range
- **WHEN** `weight_floor` is above 0.3
- **THEN** the update is refused

<!-- tests: NONE -->

### Requirement: Instrument-type horizons setting validation
`instrument_type_horizons_json` SHALL accept only known instrument types, horizons from 5, 21, 63 and 250, and lists of days (an empty list means never predicted); duplicates collapse and lists are stored sorted. Seeds and routing are in `instrument-taxonomy-and-routing`.

#### Scenario: Unsupported horizon
- **WHEN** an admin sets ETF horizons to [7]
- **THEN** the update is refused and no audit event is written

#### Scenario: Unknown type
- **WHEN** an admin names an instrument type that does not exist
- **THEN** the update is refused

<!-- tests: tests/test_gap_fixes.py::test_horizons_change_and_audit_land_together_and_a_stale_version_leaves_neither, tests/test_fund_lookthrough.py::test_horizons_and_portfolio_settings_are_admin_only_audited_and_validated -->

### Requirement: Horizon cadence setting validation
`horizon_cadence_json` SHALL map horizons 5, 21, 63 and 250 to one of `weekly_monday`, `monthly_first_trading_day`, `quarterly_first_trading_day` or `never`, seeded 5 and 21 `weekly_monday`, 63 `monthly_first_trading_day`, 250 `quarterly_first_trading_day`. Scheduling behavior is in `pipeline-orchestration`.

#### Scenario: Unknown rule
- **WHEN** an admin sets a rule outside the four
- **THEN** the update is refused

#### Scenario: Unsupported horizon or empty map
- **WHEN** the map names a horizon other than 5, 21, 63 or 250, or is empty
- **THEN** the update is refused

<!-- tests: tests/test_horizon_cadence.py::test_validation_rejects_unknown_rule_and_horizon_and_empty, tests/test_horizon_cadence.py::test_seed_cadence_equals_the_previous_hard_coded_schedule -->

### Requirement: Bucket edges setting and history
`horizon_bucket_edges_json` SHALL hold `{flat, significant, strong}` fractions per horizon with 0 below flat below significant below strong. A change MUST increment `bucket_edges_version` and append the new table to `bucket_edges_history`, whose existing versions are never overwritten. Version 1 is the legacy fixed edges and the seed is version 2. Resetting to the edges already in force does not bump the version.

#### Scenario: Invalid ordering
- **WHEN** an admin sets flat above significant for a horizon
- **THEN** the update is refused

#### Scenario: Change appends history
- **WHEN** an admin changes the 21-day edges
- **THEN** `bucket_edges_version` increases and the new table is appended to the history

#### Scenario: History survives
- **WHEN** edges are changed and then reset
- **THEN** the seed version's table in the history is intact

<!-- tests: tests/test_bucket_edges.py::test_edges_validation_and_admin_only_and_reset, tests/test_bucket_edges.py::test_unchanged_save_does_not_bump_the_version_and_stale_version_is_refused, tests/test_bucket_edges.py::test_migration_is_idempotent_and_keeps_history -->

### Requirement: Trade plan settings
`engine_settings` SHALL hold the trade-plan rules for horizons 5, 21 and 63: `plan_stop_atr_mult` (seed 1.5, 2.5, 4.0), `plan_target_r_multiple` (1.5, 2.0, 2.5), `plan_max_stop_pct` (0.08, 0.15, 0.25), `plan_min_reward_risk` (1.5) and `plan_max_loss_pct_by_risk_profile` (conservative 0.005, moderate 0.01, aggressive 0.015). Multiples and minimum reward:risk MUST be above 0 and at most 20, stop fractions above 0 and at most 1, loss fractions above 0 and at most 0.1.

#### Scenario: Valid patch
- **WHEN** an admin sets the minimum reward:risk to 1.8
- **THEN** it is stored and the other plan settings are unchanged

#### Scenario: Out of range or unknown
- **WHEN** a patch sets a multiple of 0, a stop fraction of 1.5, a loss fraction of 0.5, an unsupported horizon, an unknown risk profile or an unknown key
- **THEN** it is refused and nothing changes

<!-- tests: tests/test_trade_plan.py::test_settings_are_seeded_and_updatable_and_validated, tests/test_trade_plan.py::test_changed_settings_drive_new_plans -->

### Requirement: Simulation settings
`engine_settings` SHALL hold `sim_paths` (10000, whole number 100 to 100000), `sim_lookback_days` (500, 250 to 2500), `sim_block_len` (5, 1 to 30), `sim_flag_min_conviction` (7.0, 0 to 10), `sim_flag_max_p_target` (0.35, above 0 to 1), `sim_guardrail_mode` (`shadow` or `cap`, seed `shadow`) and `blend_min_n` (150, 30 to 100000).

#### Scenario: Valid patch
- **WHEN** an admin sets `sim_paths` to 2000 and `sim_guardrail_mode` to `cap`
- **THEN** both are stored and the other settings are unchanged

#### Scenario: Invalid values
- **WHEN** a patch sets `sim_paths` to 10, a fractional `sim_paths`, `sim_lookback_days` to 100, `sim_guardrail_mode` to another value, or an unknown key
- **THEN** it is refused and nothing changes

<!-- tests: tests/test_plan_simulator.py::test_sim_settings_defaults_update_validation_and_reset, tests/test_plan_simulator.py::test_settings_drive_the_simulation -->

### Requirement: Chat synthesis tier setting
`engine_settings.chat_synthesis_tier` SHALL be `flash` or `analyst`, default `analyst`; a missing or unreadable value reads as the default and any other value MUST be refused. What the tier does is specified by the chat change, not here.

#### Scenario: Default
- **WHEN** nothing has been set
- **THEN** the tier is `analyst`

#### Scenario: Bad value
- **WHEN** an admin sets a tier other than `flash` or `analyst`
- **THEN** the update is refused

<!-- tests: tests/test_chat_tiering.py::test_default_is_analyst_and_a_bad_value_is_rejected, tests/test_chat_tiering.py::test_the_setting_is_versioned_audited_admin_only_and_resettable -->

### Requirement: Command-line model changes are validated but not admin-gated
`python -m scripts.apex_models` SHALL change the APEX model settings through the same validation as the service, recording `updated_by` as `scripts.apex_models`; it does not require an admin identity and records no audit event.

#### Scenario: Refused change
- **WHEN** the command is given an unknown model label
- **THEN** it prints "Not changed", exits with code 2 and the stored settings are unchanged

<!-- tests: NONE -->
