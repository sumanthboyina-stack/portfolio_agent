# investor-profile Specification

## Purpose
Defines each person's optional, private investment profile: a risk profile (Conservative, Moderate or Aggressive), an optional quiz, an emergency cash reserve, an optional custom target allocation, the source of concentration limits, and optional goals. It also defines the admin-only global settings behind them and the fingerprinted effective policy that computations read. Baseline at HEAD `ca450dd`. Educational only; not investment advice.

## Requirements

### Requirement: Everyone has a default profile with no input
The system SHALL give every person a Moderate profile with source `DEFAULT` and the global default emergency reserve without requiring any stored row or any user input. Reading this state MUST NOT write a row.

#### Scenario: New person
- **WHEN** a person with no stored investor profile asks for their state
- **THEN** the profile is `MODERATE`, the source is `DEFAULT`, `is_default` is true, and the reserve is the global default with source `DEFAULT`
- **AND** there is no custom allocation and the pick is not above a quiz cap
- **AND** no investor profile row exists afterwards

#### Scenario: New person's effective policy
- **WHEN** the effective policy is read for that person
- **THEN** it carries the Moderate target allocation, the Moderate leveraged/inverse limit, `allocation_source` `PROFILE` and `limits_source` `profile`

<!-- tests: tests/test_investor_profile.py::test_everyone_starts_moderate_with_the_3000_default_and_no_row -->

### Requirement: Three seeded risk profiles
The system SHALL provide exactly three risk profiles ordered `CONSERVATIVE`, `MODERATE`, `AGGRESSIVE`, each with target allocation bands for `US_EQUITY`, `INTL_EQUITY`, `FIXED_INCOME` and `CASH` summing to 100.

#### Scenario: Seeded targets
- **WHEN** the risk profile settings are at their seed
- **THEN** Conservative targets are US 25, international 5, fixed income 55, cash 15
- **AND** Moderate targets are 45, 15, 33, 7
- **AND** Aggressive targets are 62, 25, 10, 3

#### Scenario: Seeded bands
- **WHEN** the seed bands are read
- **THEN** each equity and fixed-income target has a band of plus or minus 5 points and cash a band of plus or minus 3 points
- **AND** the Moderate US equity band is 40 to 50 and its cash band is 4 to 10

<!-- tests: tests/test_investor_profile.py::test_global_settings_are_seeded_exactly_as_specified_and_versioned -->

### Requirement: Seeded profile limits and instrument rules
Each risk profile SHALL carry numeric concentration limits, a leveraged/inverse share limit and a list of allowed instrument types, seeded as stated in the scenarios.

#### Scenario: Seeded limits
- **WHEN** the risk profile settings are at their seed
- **THEN** maximum position, issuer and sector percentages are 8, 8, 20 for Conservative; 12, 12, 25 for Moderate; 20, 20, 35 for Aggressive
- **AND** the maximum leveraged/inverse percentages are 0, 5 and 15 respectively
- **AND** the maximum share of new cash per candidate is 40 for every profile

#### Scenario: Seeded allowed types
- **WHEN** the allowed instrument types are read
- **THEN** `ETN` is absent from Conservative and present for Moderate and Aggressive

<!-- tests: tests/test_investor_profile.py::test_global_settings_are_seeded_exactly_as_specified_and_versioned -->

### Requirement: Quiz scoring takes the more conservative of cap and preference
The system SHALL score the optional three-question quiz as the more conservative of a cap (from the time-horizon answer) and a preference (from the sum of the other two answers' points).

#### Scenario: Cap beats an aggressive preference
- **WHEN** horizon is less than 3 years and the other answers are the most aggressive
- **THEN** the result is `CONSERVATIVE`

#### Scenario: Preference beats an aggressive cap
- **WHEN** horizon is 10+ years and both other answers are the most cautious
- **THEN** the result is `CONSERVATIVE`

#### Scenario: Preference bands
- **WHEN** the preference points sum to 0 or 1, 2 or 3, or 4 under a 10+ year horizon
- **THEN** the preference profile is Conservative, Moderate or Aggressive respectively

<!-- tests: tests/test_investor_profile.py::test_quiz_cap_preference_and_unanswered_are_scored_as_specified -->

### Requirement: Unanswered quiz questions count as the middle answer
Every quiz question SHALL be optional, an unanswered question SHALL score as its middle default answer, and an unknown question id or option key MUST be refused instead of scored.

#### Scenario: Whole quiz skipped
- **WHEN** no question is answered
- **THEN** the result is `MODERATE` with preference points 2 and every question listed as unanswered
- **AND** the cap is not treated as answered

#### Scenario: Typo in an answer
- **WHEN** an answer names an option or question that does not exist
- **THEN** scoring is refused with an error and no profile is recorded

<!-- tests: tests/test_investor_profile.py::test_quiz_cap_preference_and_unanswered_are_scored_as_specified -->

### Requirement: Taking the quiz records the result
Taking the quiz SHALL replace the person's active profile with source `QUIZ`, store the answers and quiz version, store the cap only when the cap question was answered, carry over the emergency reserve, and clear any earlier above-cap acknowledgment.

#### Scenario: Quiz recorded
- **WHEN** a person with a reserve of their own takes the quiz with horizon under 3 years
- **THEN** their profile is `CONSERVATIVE`, source `QUIZ`, cap `CONSERVATIVE`, and their reserve is unchanged
- **AND** a `take_risk_quiz` audit event exists

#### Scenario: Cap question skipped
- **WHEN** the quiz is taken with the horizon question unanswered
- **THEN** no cap profile is stored

<!-- tests: tests/test_investor_profile.py::test_taking_the_quiz_records_the_result_the_cap_and_an_audit_event -->

### Requirement: Quiz content is database-backed
The quiz questions, option points, cap rule and preference thresholds SHALL come from stored settings, so that changing them changes scoring without a code change, and `quiz_version` SHALL increase when they change.

#### Scenario: Admin flips the points
- **WHEN** an admin reverses the points of the drop question
- **THEN** `quiz_version` becomes 2 and the same answers can score a different profile

<!-- tests: tests/test_investor_profile.py::test_quiz_scoring_comes_from_the_database_not_from_code -->

### Requirement: A profile can be picked without the quiz
A person SHALL be able to choose any valid profile directly; the choice is stored with source `PICKED`, and an invalid profile name MUST be refused.

#### Scenario: Pick with no quiz
- **WHEN** a person with no quiz result picks `AGGRESSIVE`
- **THEN** the profile is `AGGRESSIVE`, source `PICKED`, acknowledged false and cap empty
- **AND** it is not above a cap

#### Scenario: Invalid name
- **WHEN** a person picks a profile name that is not one of the three
- **THEN** the pick is refused with an error

<!-- tests: tests/test_investor_profile.py::test_pick_without_a_quiz_needs_no_acknowledgment -->

### Requirement: Picking above the quiz cap needs an acknowledgment
The system MUST refuse a pick riskier than the stored quiz cap unless the person explicitly acknowledges it; the acknowledgment SHALL be stored on the profile row and in the audit event.

#### Scenario: Refused without acknowledgment
- **WHEN** a person whose cap is `CONSERVATIVE` picks `AGGRESSIVE` without acknowledging
- **THEN** the pick is refused, the profile is unchanged and no audit event is written

#### Scenario: Acknowledged pick
- **WHEN** the same person picks `AGGRESSIVE` with the acknowledgment
- **THEN** the profile is `AGGRESSIVE`, `above_cap` and `above_cap_acknowledged` are true
- **AND** the `pick_investor_profile` audit event records the acknowledgment and the cap in its reason

#### Scenario: Back at or below the cap
- **WHEN** the person picks `CONSERVATIVE` afterwards
- **THEN** the acknowledgment is cleared, and picking the same profile again changes nothing

<!-- tests: tests/test_investor_profile.py::test_pick_above_the_quiz_cap_requires_acknowledgment_and_is_audited -->

### Requirement: The Profile page flags an acknowledged above-cap pick
The Profile page SHALL block an above-cap pick until its acknowledgment box is ticked and SHALL show a small flag "above your quiz result" while an acknowledged above-cap pick stands.

#### Scenario: Blocked then flagged
- **WHEN** a person with a `CONSERVATIVE` cap selects `AGGRESSIVE` on the Profile page
- **THEN** a warning says it is riskier and the submit button is disabled until the acknowledgment is ticked
- **AND** after saving, the page shows the "above your quiz result" flag

<!-- tests: tests/test_investor_profile.py::test_profile_page_saving_a_reserve_and_picking_above_the_cap_work_end_to_end -->

### Requirement: Profile history keeps one active row
The system SHALL keep every investor profile change as history and exactly one active row per owner, closing the previous row when a profile, reserve or quiz result changes.

#### Scenario: Three changes
- **WHEN** a person picks a profile, sets a reserve and takes the quiz
- **THEN** three history rows exist, only the newest is active, and a second active row is refused by the database

<!-- tests: tests/test_investor_profile.py::test_history_is_kept_and_exactly_one_row_is_active -->

### Requirement: Emergency reserve is one dollar amount
The effective emergency reserve SHALL be the person's own dollar amount when set and otherwise the global default (seed 3000 dollars), reported with source `USER` or `DEFAULT`. Setting it to none restores the default and zero is a valid own value. It is not a goal.

#### Scenario: Own amount and reset
- **WHEN** a person sets a reserve of 8000 and later sets none
- **THEN** the reserve is 8000 with source `USER`, then the default with source `DEFAULT`
- **AND** setting none again changes nothing and adds no row

#### Scenario: Zero reserve
- **WHEN** a person sets the reserve to 0
- **THEN** their effective reserve is 0

<!-- tests: tests/test_investor_profile.py::test_user_reserve_overrides_default_and_none_goes_back_to_it -->

### Requirement: Emergency reserve validation
The system MUST refuse a reserve that is negative, not finite or not a number, and MUST refuse one for a person whose base currency is not USD.

#### Scenario: Bad amounts
- **WHEN** a reserve of -1, NaN, infinity or non-numeric text is submitted
- **THEN** it is refused and no row is written

#### Scenario: Non-USD base currency
- **WHEN** a person whose base currency is not USD sets a reserve
- **THEN** it is refused with an error naming USD

<!-- tests: tests/test_investor_profile.py::test_bad_reserve_amounts_are_rejected, tests/test_investor_profile.py::test_reserve_is_usd_only -->

### Requirement: Only reserve cash counts toward the reserve
The emergency reserve check SHALL count only cash and money-market holdings in taxable and cash accounts, and MUST NOT count retirement, HSA, 529 or paper accounts or non-money-market funds. This rule is evaluated by Portfolio Health.

#### Scenario: Mixed accounts
- **WHEN** cash and money-market funds sit in taxable, cash, IRA, 401(k) and paper accounts
- **THEN** only the taxable and cash-account cash and money-market amounts are counted

<!-- tests: tests/test_portfolio_health.py::test_reserve_counts_only_cash_and_money_market_in_taxable_or_cash_accounts_never_retirement_or_paper -->

### Requirement: Default reserve changes move only people without their own
Changing the global default reserve SHALL change the effective reserve and policy fingerprint only for people who have not set their own amount.

#### Scenario: Admin raises the default
- **WHEN** an admin raises the default while one person has their own reserve and another does not
- **THEN** the second person's reserve follows the new default and their fingerprint changes
- **AND** the first person's reserve and fingerprint are unchanged

<!-- tests: tests/test_investor_profile.py::test_admin_changing_the_default_moves_users_without_their_own_value_only -->

### Requirement: Custom target allocation replaces the profile's
A person SHALL be able to replace their profile's target allocation with their own, and clearing it SHALL return to the profile's allocation. Custom allocations are kept as history and the profile itself is unchanged.

#### Scenario: Set then clear
- **WHEN** a person sets a valid custom allocation and later clears it
- **THEN** the effective allocation source is `CUSTOM` with their targets, then `PROFILE` again
- **AND** clearing when none exists returns false and writes nothing
- **AND** the superseded custom row is kept with no active row

<!-- tests: tests/test_investor_profile.py::test_custom_allocation_replaces_the_profile_default_and_can_be_cleared -->

### Requirement: Allocation validation
Any target allocation, custom or profile default, SHALL name only allocatable asset classes (`US_EQUITY`, `INTL_EQUITY`, `FIXED_INCOME`, `REAL_ESTATE`, `COMMODITY`, `CASH`), have every number between 0 and 100 with min at most target at most max, and have targets summing to 100.

#### Scenario: Sum off
- **WHEN** targets sum to 90
- **THEN** the allocation is refused and the stored allocation is unchanged

#### Scenario: Looked-through classes
- **WHEN** an allocation targets `MULTI_ASSET`, `UNKNOWN` or an unknown class
- **THEN** it is refused

#### Scenario: Band does not contain its target
- **WHEN** a class has min above target or target above max
- **THEN** it is refused

<!-- tests: tests/test_investor_profile.py::test_custom_allocation_replaces_the_profile_default_and_can_be_cleared, tests/test_investor_profile.py::test_updates_are_validated_versioned_and_guard_against_stale_writes -->

### Requirement: Limits source selects the concentration limits
The four concentration limits (sector, issuer, position, share of cash per candidate) SHALL come from the investor profile's settings when `limits_source` is `profile` and from the person's own stored numbers when it is `custom`. New people default to `profile`; people who existed before the feature were set to `custom`.

#### Scenario: Profile limits follow the profile and admin changes
- **WHEN** a `profile`-source person picks `AGGRESSIVE`
- **THEN** their sector, issuer, position and leveraged/inverse limits are 35, 20, 20 and 15
- **AND** an admin change to that profile's position limit is reflected with a higher settings version

#### Scenario: Custom limits
- **WHEN** the person's source is set to `custom`
- **THEN** their own stored sector, issuer and position numbers apply, with the stored fractions converted to percentage points

#### Scenario: Migration keeps existing limits
- **WHEN** profiles that predate `limits_source` are migrated, and the migration is run again
- **THEN** every existing profile is `custom` and a profile created afterwards is `profile`

<!-- tests: tests/test_investor_profile.py::test_profile_limits_come_from_the_settings_and_follow_admin_changes_and_picks, tests/test_investor_profile.py::test_migration_marks_every_existing_profile_custom_so_no_limit_changes_silently, tests/test_investor_profile.py::test_migration_also_preserves_custom_through_the_old_singleton_rebuild -->

### Requirement: Limits source is validated and audited
Setting `limits_source` SHALL accept only `profile` or `custom`, record an audit event, and write nothing when the value is unchanged.

#### Scenario: Invalid and repeated values
- **WHEN** an unknown source is submitted
- **THEN** it is refused
- **AND** setting `custom` twice records exactly one `set_limits_source` event

<!-- tests: tests/test_investor_profile.py::test_profile_limits_come_from_the_settings_and_follow_admin_changes_and_picks -->

### Requirement: Effective policy is fingerprinted on what changes a computation
The effective policy SHALL carry one fingerprint that changes when any of limits, profile, profile source, acknowledgment, allocation, leveraged/inverse limit, allowed types, reserve or limits source changes, and MUST NOT change with display name, employer, pre-clearance flag, notification or horizon preferences, goals, or a profile's description.

#### Scenario: Inputs move the fingerprint
- **WHEN** the reserve, profile, quiz result, custom allocation, limits source, a custom limit, or a profile setting behind the policy changes
- **THEN** the fingerprint changes
- **AND** restoring the earlier inputs restores the earlier fingerprint

#### Scenario: Non-inputs do not
- **WHEN** display name, employer, pre-clearance flag, a goal, or a profile's description changes
- **THEN** the fingerprint is unchanged

#### Scenario: Picked is not default
- **WHEN** a person picks Moderate while on the default
- **THEN** the profile source is `PICKED` and the fingerprint differs from the default one

<!-- tests: tests/test_investor_profile.py::test_fingerprint_changes_with_any_policy_input_and_with_nothing_else -->

### Requirement: Policy without a database uses seed defaults
Building a policy from a user profile alone SHALL use the seed Moderate profile, source `DEFAULT` and the seeded default reserve, and two people differing only in name or employer SHALL get the same fingerprint.

#### Scenario: Pure policy
- **WHEN** a policy is built from two user profiles differing only in name and employer
- **THEN** their fingerprints are equal and differ from one with another sector limit

<!-- tests: tests/test_investor_profile.py::test_effective_policy_from_profile_is_pure_and_keeps_its_four_limits -->

### Requirement: Cached exposures follow the policy fingerprint
Cached portfolio exposures SHALL be invalidated by a reserve change and SHALL NOT be invalidated by a display-name change.

#### Scenario: Name versus reserve
- **WHEN** a person changes their display name and then their reserve
- **THEN** the cached exposures keep their version after the name change
- **AND** the policy fingerprint and version change after the reserve change

<!-- tests: tests/test_investor_profile.py::test_exposures_cache_is_invalidated_by_a_reserve_change_but_not_a_name_change -->

### Requirement: Goals are optional private notes
A person SHALL be able to add, update, complete and remove goals of type `RETIREMENT`, `HOUSE`, `EDUCATION` or `OTHER`; no goal is required, the emergency reserve is not a goal, and goals never enter the policy fingerprint.

#### Scenario: Validated goal
- **WHEN** a goal has an unknown type, a blank name, a negative amount or a non-ISO date
- **THEN** it is refused

#### Scenario: Goal lifecycle
- **WHEN** a goal is added, marked `DONE`, then removed
- **THEN** it leaves the active list when done, disappears when removed, and each step writes an audit event
- **AND** removing it again fails as a missing goal

<!-- tests: tests/test_investor_profile.py::test_goals_are_optional_validated_and_the_reserve_is_not_one -->

### Requirement: Investor data is private and owner-scoped
Every read and write of a person's profile, reserve, custom allocation and goals SHALL use only the verified actor from the request context, MUST fail without one, and another owner's goal id SHALL be indistinguishable from a missing one. This is the owner-scoping invariant for this capability.

#### Scenario: Two owners
- **WHEN** owner A sets a reserve, picks `AGGRESSIVE`, sets a custom allocation and adds a goal
- **THEN** owner B sees only defaults, no goals, and a different fingerprint
- **AND** owner B updating or removing A's goal fails as a missing goal
- **AND** owner B's changes never alter A's state

#### Scenario: No verified identity
- **WHEN** a raw owner string or no context is passed to read the state or the policy
- **THEN** access is refused

<!-- tests: tests/test_investor_profile.py::test_everything_private_is_scoped_to_its_owner -->

### Requirement: Every private investor write is audited
Each private write (quiz, pick, reserve, limits source, custom allocation set or clear, goal add, update or remove) SHALL record an audit event with the actor, operation, before and after, and request id.

#### Scenario: Audit coverage
- **WHEN** one person performs each private write once
- **THEN** exactly the events `take_risk_quiz`, `pick_investor_profile`, `set_emergency_reserve`, `set_limits_source`, `set_custom_allocation`, `clear_custom_allocation`, `add_goal`, `update_goal` and `remove_goal` exist for that actor

#### Scenario: Reserve event content
- **WHEN** a reserve is set for the first time
- **THEN** its event shows a before reserve of none, an after reserve of the amount, and a request id

<!-- tests: tests/test_investor_profile.py::test_every_private_verb_writes_an_audit_event_attributed_to_the_actor -->

### Requirement: Global investor settings are admin-only in the service
Changing or resetting the quiz settings, risk profile settings or portfolio settings SHALL require an enabled administrator, checked in the service and not only the UI; anyone else MUST be refused with no change.

#### Scenario: Non-admins refused
- **WHEN** a member, an unknown person, a disabled admin, or a person without an allow-list row calls any update or reset
- **THEN** it is refused as not an administrator and the settings version is unchanged
- **AND** no `update_portfolio_settings` audit event exists

#### Scenario: Enabled admin
- **WHEN** an enabled admin updates the default reserve
- **THEN** the change is stored

<!-- tests: tests/test_investor_profile.py::test_global_settings_are_admin_only_even_when_the_ui_is_bypassed -->

### Requirement: Global investor settings are validated and versioned
Updates to risk profile, quiz and portfolio settings SHALL be validated, increment a version, record the updater, and be refused when `expected_version` is stale; an invalid write MUST change nothing. Generic admin versioning and reset behavior is further defined by `admin-and-engine-settings`.

#### Scenario: Stale version
- **WHEN** a profile update is submitted with an out-of-date `expected_version`
- **THEN** it is refused as a version conflict and the stored version is unchanged

#### Scenario: Invalid profile settings
- **WHEN** a limit is above 100 or negative or not finite, allowed types are empty or unknown, a display name is blank, or the profile name is unknown
- **THEN** the update is refused

#### Scenario: Invalid quiz settings
- **WHEN** questions are empty, a question has fewer than two options, the cap rule leaves options unmapped or names an unknown profile, or the preference bands cannot reach the maximum points or include the cap question
- **THEN** the update is refused and the quiz version is unchanged

<!-- tests: tests/test_investor_profile.py::test_updates_are_validated_versioned_and_guard_against_stale_writes, tests/test_investor_profile.py::test_quiz_settings_validation -->

### Requirement: Global investor settings can be reset to defaults
An administrator SHALL be able to reset the portfolio settings, one or all risk profile settings, and the quiz settings to their seeds; each reset is itself a versioned write.

#### Scenario: Reset one profile
- **WHEN** an admin resets only `AGGRESSIVE` after changing two profiles
- **THEN** only that profile returns to its seed and the other keeps its change

#### Scenario: Reset all
- **WHEN** the portfolio, risk profile and quiz settings are reset
- **THEN** the default reserve is 3000 dollars, every profile is at its seed, the quiz cap rule is the seed, and each reset writes an audit event

<!-- tests: tests/test_investor_profile.py::test_reset_to_defaults_restores_every_table -->

### Requirement: Admin settings changes are audited with before and after
Every administrator change to investor settings SHALL write an audit event with actor, operation, before, after and the stated reason.

#### Scenario: Default reserve change
- **WHEN** an admin changes the default reserve with a reason
- **THEN** the `update_portfolio_settings` event names the admin and shows before 3000, the new amount and the reason

<!-- tests: tests/test_investor_profile.py::test_admin_changes_write_audit_events_with_before_and_after -->

### Requirement: Investor data never reaches shared forecasts
Shared market-forecast and prediction-pipeline code SHALL NOT read or import a person's investor profile, reserve, custom allocation, goals or limits source, and a forecast context MUST be identical whatever any person chose. This is the privacy-boundary invariant for this capability.

#### Scenario: Source and import boundary
- **WHEN** the shared market context, forecast writer, APEX run, weight engine, prediction store, instrument reference and validation modules are inspected
- **THEN** none references the private investor names
- **AND** importing the shared forecast path does not load the investor profile, service, settings, policy or quiz modules

#### Scenario: Context unchanged by private choices
- **WHEN** a person picks a profile, sets a reserve, adds a goal and sets a custom allocation
- **THEN** the forecast context digest and payload for a ticker are unchanged

<!-- tests: tests/test_investor_profile.py::test_shared_market_and_pipeline_code_never_reads_the_private_investor_data, tests/test_investor_profile.py::test_a_forecast_context_is_identical_whatever_a_person_chose -->

### Requirement: Profile page shows optional controls
The Profile page SHALL show the person's profile with its source, the target allocation, the reserve as an own or default amount, and optional quiz and pick controls, SHALL make limit fields read-only while limits come from the profile, and SHALL show the investor-settings admin section only to administrators.

#### Scenario: Default view
- **WHEN** a non-admin with no profile opens the page
- **THEN** it shows Moderate marked default, the Moderate allocation, a reserve input at the default amount with no reset button, and quiz and pick controls
- **AND** there is no investor-settings admin tile

#### Scenario: Read-only limits
- **WHEN** limits come from the profile
- **THEN** the sector limit input is disabled at the profile's value and becomes editable after the source is set to `custom`

#### Scenario: Admin view
- **WHEN** an administrator opens the page
- **THEN** the investor-settings admin tile is present with a default reserve editor

<!-- tests: tests/test_investor_profile.py::test_profile_page_shows_the_default_profile_reserve_and_optional_controls, tests/test_investor_profile.py::test_profile_page_limits_are_read_only_when_they_come_from_the_profile, tests/test_investor_profile.py::test_profile_page_admin_section_is_only_for_admins_and_edits_go_through_the_service -->

### Requirement: Profile page states it is not investment advice
The Profile page SHALL show a caption stating that APEX is an educational tool, not investment advice, and that profiles, targets and reserves are starting points.

#### Scenario: Caption shown
- **WHEN** a person opens the investor profile section
- **THEN** the educational, not-investment-advice caption is rendered

<!-- tests: NONE -->
