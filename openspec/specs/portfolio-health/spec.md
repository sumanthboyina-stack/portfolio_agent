# portfolio-health Specification

## Purpose
Defines the Portfolio Health checkup: a private, owner-scoped set of deterministic checks of one person's holdings against their own investor profile and the global health thresholds, shown with four statuses and no single score. Covers scopes, daily snapshots and history, notifications when a check becomes ACTION, and the ticker-level assessment limits that share the look-through rules. Baseline at HEAD `ca450dd`.

## Requirements

### Requirement: Four statuses and no overall score
Every health check SHALL carry exactly one of the statuses `GOOD`, `WATCH`, `ACTION` or `UNKNOWN`. The system MUST NOT compute or show a single overall health score; it SHALL show how many checks are ACTION, WATCH, UNKNOWN and GOOD.

#### Scenario: Counts instead of a score
- **WHEN** a household checkup is built
- **THEN** the result carries counts per status and no score field
- **AND** the page text states that there is no single overall score on purpose

<!-- tests: tests/test_portfolio_health.py::test_an_empty_portfolio_is_unknown_not_good_with_a_reason_code, tests/test_portfolio_health.py::test_the_health_section_renders_and_is_not_its_own_nav_entry -->

### Requirement: UNKNOWN is never GOOD
When the data needed to judge a check is missing or too partial, the check SHALL be `UNKNOWN` with a reason code. When statuses are combined the order of severity MUST be ACTION, then WATCH, then UNKNOWN, then GOOD, and an empty set of statuses MUST combine to UNKNOWN.

#### Scenario: Combining statuses
- **WHEN** statuses GOOD and UNKNOWN are combined
- **THEN** the result is UNKNOWN
- **AND** combining WATCH, ACTION and GOOD gives ACTION, and combining nothing gives UNKNOWN

<!-- tests: tests/test_portfolio_health.py::test_status_ordering_puts_unknown_above_good_and_an_empty_set_is_unknown -->

### Requirement: Empty portfolio
A portfolio with no holdings and no cash SHALL have every data-dependent check `UNKNOWN` with reason code `NO_HOLDINGS`, never GOOD.

#### Scenario: Nothing held
- **WHEN** an owner has a portfolio with no holdings or cash
- **THEN** allocation, emergency reserve, concentration, diversification, leveraged/inverse, fees and data-quality checks are UNKNOWN with `NO_HOLDINGS`
- **AND** the compliance check is GOOD because nothing is held or restricted

<!-- tests: tests/test_portfolio_health.py::test_an_empty_portfolio_is_unknown_not_good_with_a_reason_code -->

### Requirement: Household check set
The household scope SHALL contain these checks in this order: `allocation`, `fixed_income_international`, `excess_cash`, `emergency_reserve`, `concentration`, `diversification`, `leveraged_inverse`, `fees`, `profile`, `compliance`, `data_quality`.

#### Scenario: Household checks
- **WHEN** a household checkup is built for a portfolio with holdings
- **THEN** its checks are exactly the eleven keys above in that order
- **AND** its counts equal the number of checks in each status

<!-- tests: tests/test_portfolio_health.py::test_scopes_household_accounts_tax_buckets_and_paper_is_excluded -->

### Requirement: Allocation band statuses
Each allocation class SHALL be GOOD inside its profile band, WATCH outside it, and ACTION when beyond `health_action_band_multiple` band-widths from the target. A band narrower than one percentage point MUST be treated as one point wide.

#### Scenario: Band edges
- **WHEN** a class has target 45, minimum 40, maximum 50 and the multiple is 2.0
- **THEN** 40 to 50 is GOOD, 35 to below 40 and above 50 up to 55 are WATCH
- **AND** below 35 and above 55 are ACTION

<!-- tests: tests/test_portfolio_health.py::test_band_status_watch_outside_the_band_action_beyond_twice_the_band, tests/test_portfolio_health.py::test_allocation_statuses_against_the_profile_bands -->

### Requirement: Unclassified share limits allocation verdicts
When the unclassified share of the portfolio, or the unknown equity geography share for stock classes, exceeds `health_unknown_share_tolerance_pct`, an allocation verdict at or under the band maximum SHALL be UNKNOWN. A verdict over the band maximum MUST still be reported as over.

#### Scenario: Under the band with unclassified holdings
- **WHEN** more than the tolerated share of the portfolio is unclassified and a class is below its band
- **THEN** that check is UNKNOWN with reason `ALLOCATION_UNKNOWN_SHARE` instead of an alarm

<!-- tests: tests/test_portfolio_health.py::test_unclassified_holdings_make_an_under_the_band_verdict_unknown_not_a_false_alarm -->

### Requirement: Excess cash is judged only from above
The `excess_cash` check SHALL be GOOD whenever cash is at or below its band maximum, unless the verdict is UNKNOWN because of unclassified holdings; being under the cash band MUST NOT be a finding of this check.

#### Scenario: Low cash
- **WHEN** cash is below its target band and nothing is unclassified
- **THEN** `excess_cash` is GOOD

<!-- tests: tests/test_portfolio_health.py::test_allocation_statuses_against_the_profile_bands -->

### Requirement: Emergency reserve counting rule
The emergency reserve check SHALL count only cash balances and `CASH` or `MONEY_MARKET` holdings in accounts whose tax treatment is taxable (`TAXABLE` and `CASH` account types). Cash in retirement, HSA, 529 and paper accounts, and non-cash funds, MUST NOT count.

#### Scenario: Mixed accounts
- **WHEN** an owner has cash and money-market holdings in taxable, cash, IRA, 401(k) and paper accounts
- **THEN** only the taxable and cash-account amounts are counted toward the reserve

<!-- tests: tests/test_portfolio_health.py::test_reserve_counts_only_cash_and_money_market_in_taxable_or_cash_accounts_never_retirement_or_paper -->

### Requirement: Emergency reserve status
The reserve check SHALL be ACTION with the dollar shortfall when counted cash is below the reserve, GOOD at or above it, and UNKNOWN with `NO_CASH_DATA` when no cash balance is known in a taxable or cash account. Cash in an account of unset type MUST NOT be counted and SHALL add `ACCOUNT_TYPE_UNKNOWN`.

#### Scenario: Shortfall against the default
- **WHEN** counted cash is below the default reserve and no personal reserve is set
- **THEN** the check is ACTION, states the shortfall and says it used the default reserve

#### Scenario: Personal reserve
- **WHEN** the owner has set their own reserve
- **THEN** the check judges against that amount and says it is the owner's own reserve

#### Scenario: No known cash
- **WHEN** no cash is recorded in a taxable or cash account
- **THEN** the check is UNKNOWN, not ACTION and not GOOD
- **AND** a recorded zero balance counts as known and gives ACTION

<!-- tests: tests/test_portfolio_health.py::test_reserve_is_action_with_the_dollar_shortfall_below_the_default_and_good_at_or_above_it, tests/test_portfolio_health.py::test_reserve_uses_the_users_own_amount_and_says_so, tests/test_portfolio_health.py::test_reserve_is_unknown_without_a_known_cash_balance_and_when_only_unknown_type_accounts_have_cash -->

### Requirement: Concentration limits
The concentration check SHALL compare the largest single security, the largest issuer and the largest sector with the owner's effective limits. A value above the limit MUST be ACTION, a value from `health_watch_limit_fraction` of the limit up to the limit WATCH, and a lower value GOOD.

#### Scenario: Watch line
- **WHEN** a position is at or above the watch fraction of its limit but not over it
- **THEN** the check is WATCH
- **AND** lowering the setting changes the status on the same holdings

<!-- tests: tests/test_portfolio_health.py::test_concentration_action_over_the_limit_watch_near_it_good_below, tests/test_portfolio_health.py::test_a_threshold_change_in_the_settings_table_changes_the_status -->

### Requirement: Concentration counts what funds hold
Issuer and sector concentration SHALL count what the owner's funds hold when look-through explains at least `lookthrough_min_coverage_pct` of the portfolio, and the summary SHALL say it counts what funds hold. The single-security position check MUST consider single securities only, so a fund is judged through its contents.

#### Scenario: Index fund contents
- **WHEN** a fund holds a large share of one issuer that the owner also holds directly
- **THEN** the issuer concentration is the direct plus via-fund total and its summary says "counting what funds hold"
- **AND** the fund itself is not the largest single security

<!-- tests: tests/test_portfolio_health.py::test_concentration_looks_through_funds_for_issuers_and_flags_a_partial_look_through_unknown, tests/test_portfolio_health.py::test_position_check_judges_single_securities_a_big_index_fund_is_judged_through_what_it_holds -->

### Requirement: Partial look-through is never GOOD
When fund contents are known below the coverage minimum, a concentration or diversification result that would be GOOD SHALL be UNKNOWN with reason `LOOKTHROUGH_PARTIAL`. A result over a limit MUST still be reported as over.

#### Scenario: Top-10-only fund data
- **WHEN** a fund's stored holdings explain only a small part of the fund
- **THEN** issuer concentration and diversification are not GOOD and carry `LOOKTHROUGH_PARTIAL`

<!-- tests: tests/test_portfolio_health.py::test_concentration_looks_through_funds_for_issuers_and_flags_a_partial_look_through_unknown -->

### Requirement: Diversification thresholds
The diversification check SHALL be ACTION when the effective number of holdings is below `health_min_effective_n_action` (default 4) and WATCH below `health_min_effective_n_watch` (default 8). At household scope an average correlation above `health_max_avg_correlation_watch` (0.70) SHALL be WATCH and above `health_max_avg_correlation_action` (0.90) ACTION, taking the worse status.

#### Scenario: Single holding
- **WHEN** the portfolio is one security
- **THEN** diversification is ACTION with effective N of 1

#### Scenario: Correlation unavailable
- **WHEN** no correlation data can be computed and effective N is adequate
- **THEN** the check is UNKNOWN with `CORRELATION_UNAVAILABLE`, not GOOD

#### Scenario: High correlation
- **WHEN** average correlation is 0.95
- **THEN** the check is ACTION

<!-- tests: tests/test_portfolio_health.py::test_diversification_effective_n_thresholds_and_correlation, tests/test_portfolio_health.py::test_a_single_holding_is_not_diversified -->

### Requirement: Leveraged and inverse check
The leveraged/inverse check SHALL compare the leverage-adjusted share of leveraged or inverse funds with the profile limit (ACTION above it, WATCH from the watch fraction, else GOOD). When the share of unclassified instrument types exceeds the unknown-share tolerance, a GOOD result SHALL be UNKNOWN with `INSTRUMENT_TYPE_UNKNOWN`.

#### Scenario: Over the limit
- **WHEN** leveraged or inverse funds exceed the profile limit
- **THEN** the check is ACTION

#### Scenario: Unclassified book
- **WHEN** much of the book has no instrument type and no leveraged fund is found
- **THEN** the check is UNKNOWN, not GOOD

<!-- tests: tests/test_portfolio_health.py::test_leveraged_inverse_share_against_the_profile_limit, tests/test_portfolio_health.py::test_leveraged_inverse_is_unknown_when_much_of_the_book_is_unclassified -->

### Requirement: Fund fee check
The fees check SHALL report the value-weighted expense ratio and the estimated annual fund cost (labelled as estimated), WATCH when any fund or the weighted ratio exceeds `high_expense_ratio_pct` (default 0.50), and GOOD with no funds. When expense ratios are unknown for more than the unknown-share tolerance, a GOOD result SHALL be UNKNOWN with `EXPENSE_RATIO_UNKNOWN`.

#### Scenario: High-cost fund
- **WHEN** a held fund's expense ratio is above the high line
- **THEN** the check is WATCH and names the fund and its ratio

#### Scenario: Unknown ratios
- **WHEN** a large share of fund value has no expense ratio on record
- **THEN** the check is UNKNOWN

#### Scenario: No funds
- **WHEN** no funds are held
- **THEN** the check is GOOD with zero fund value

<!-- tests: tests/test_portfolio_health.py::test_fees_weighted_expense_ratio_cost_high_list_and_unknown_never_good, tests/test_portfolio_health.py::test_no_funds_means_no_fund_expenses_known -->

### Requirement: Cheaper share class note
A cheaper-class note SHALL appear only for the same fund held under another share class with a lower expense ratio on record (same share-class group), and MUST NOT name a different product or recommend a switch.

#### Scenario: Different product
- **WHEN** another fund has a lower expense ratio but is not in the same share-class group
- **THEN** no cheaper-class note is produced

<!-- tests: tests/test_portfolio_health.py::test_cheaper_share_class_note_only_within_the_same_share_class_group -->

### Requirement: Risk profile check
The risk profile check SHALL be UNKNOWN with `PROFILE_DEFAULT` when the profile is still the default, WATCH with `ABOVE_QUIZ_CAP_ACKNOWLEDGED` when a pick above the quiz cap was acknowledged, and GOOD otherwise.

#### Scenario: Default profile
- **WHEN** nobody has chosen a profile
- **THEN** the check is UNKNOWN and points to the quiz

<!-- tests: tests/test_portfolio_health.py::test_profile_default_nudges_to_the_quiz_and_is_unknown_chosen_is_good_above_cap_is_watch -->

### Requirement: Restricted list check
The compliance check SHALL be ACTION when a held ticker is on the owner's restricted list, UNKNOWN with `RESTRICTED_LIST_UNREADABLE` when the list cannot be read, and GOOD otherwise.

#### Scenario: Held restricted ticker
- **WHEN** a held ticker is on the owner's restricted list with a reason
- **THEN** the check is ACTION and names the ticker and reason

#### Scenario: List unreadable
- **WHEN** reading the restricted list fails
- **THEN** the check is UNKNOWN, not GOOD

<!-- tests: tests/test_portfolio_health.py::test_restricted_holdings_are_action_and_an_unreadable_list_is_unknown -->

### Requirement: Ledger clearance in the compliance check
When any recorded, not-voided ledger trade on a non-paper account has `clearance_status` `missing`, the compliance check SHALL be ACTION with `TRADES_WITHOUT_CLEARANCE` listing the tickers. Trades with status `unknown` SHALL make it at least WATCH with `TRADE_CLEARANCE_UNKNOWN`. With none flagged, the check MUST equal the restricted-list check.

#### Scenario: Trades without approval
- **WHEN** two recorded trades have no covering approval
- **THEN** the compliance check is ACTION and its summary counts two trades and lists their tickers

#### Scenario: Voided trade
- **WHEN** one of those trades is voided
- **THEN** it no longer counts

#### Scenario: Nothing flagged
- **WHEN** no trade is flagged
- **THEN** the check is the restricted-list check titled "Restricted list"

<!-- tests: tests/test_ledger_compliance_import.py::test_health_compliance_check_is_action_when_trades_lack_clearance, tests/test_ledger_compliance_import.py::test_health_compliance_is_unchanged_when_nothing_needs_attention_and_unknown_is_a_watch -->

### Requirement: Data quality check
The data-quality check SHALL list each kind of gap found: unpriced holdings, unclassified instrument types, out-of-date prices, unknown price dates, stale or missing fund data, ticker-less funds with no breakdown or proxy, and partial look-through. It SHALL be WATCH for any gap, ACTION when unpriced holdings exceed `health_unpriced_action_pct` (default 25) of value, and GOOD with none.

#### Scenario: Several gaps
- **WHEN** holdings include an unpriced ticker, an unclassified ticker and a stale price
- **THEN** the check is WATCH and lists each gap kind with its tickers

#### Scenario: Large unpriced share
- **WHEN** unpriced holdings exceed the unpriced ACTION percentage
- **THEN** the check is ACTION

<!-- tests: tests/test_portfolio_health.py::test_data_quality_lists_each_kind_of_gap, tests/test_portfolio_health.py::test_unpriced_share_above_the_threshold_is_action -->

### Requirement: Price freshness in data quality
A mutual fund priced from the prior close SHALL NOT be flagged stale on a morning before the next NAV posts. A ticker-less fund with a manual breakdown or a proxy ticker MUST NOT be flagged as lacking a breakdown.

#### Scenario: Prior-close mutual fund
- **WHEN** a mutual fund's price date is the prior trading day and the check runs in the morning
- **THEN** no stale-price gap is reported for it

#### Scenario: CIT with a breakdown
- **WHEN** a ticker-less fund has a manual breakdown or a proxy
- **THEN** the `CIT_NO_BREAKDOWN` gap is cleared

<!-- tests: tests/test_portfolio_health.py::test_a_mutual_fund_nav_from_the_prior_close_is_not_flagged_stale_on_a_morning, tests/test_portfolio_health.py::test_a_manual_breakdown_or_proxy_clears_the_cit_gap -->

### Requirement: Scopes
The checkup SHALL provide the household scope, one scope per non-archived account and one per tax treatment (`TAXABLE`, `TAX_DEFERRED`, `TAX_FREE`, `UNKNOWN` for unset account type). Paper accounts MUST be excluded from every scope.

#### Scenario: Account and tax slices
- **WHEN** an owner has a taxable account, a Roth IRA and a paper account
- **THEN** scopes exist for the household, the two real accounts, and the `TAXABLE` and `TAX_FREE` buckets
- **AND** the household total excludes the paper account

<!-- tests: tests/test_portfolio_health.py::test_scopes_household_accounts_tax_buckets_and_paper_is_excluded -->

### Requirement: Slice checks
An account or tax-treatment scope SHALL contain only `concentration`, `diversification`, `leveraged_inverse`, `fees` and `data_quality`, judged on the slice's own value against the same limits. Allocation targets, emergency reserve, profile and compliance checks MUST belong to the household only.

#### Scenario: Account slice
- **WHEN** an account scope is built
- **THEN** its checks are exactly the five slice keys

<!-- tests: tests/test_portfolio_health.py::test_scopes_household_accounts_tax_buckets_and_paper_is_excluded -->

### Requirement: Unvested employer money is excluded by default
When `health_exclude_unvested` is 1 (default), a position whose statement states a vested amount below its value SHALL be valued at the vested amount in every health total, and the unvested total SHALL be reported separately. A position with no stated vested amount MUST be counted in full.

#### Scenario: Partly vested position
- **WHEN** a plan position's vested amount is below its value and the setting is 1
- **THEN** health totals use the vested amount and the page reports the unvested money separately

<!-- tests: NONE -->

### Requirement: Health thresholds are admin settings
The health thresholds SHALL be stored in `portfolio_settings` with defaults `health_action_band_multiple` 2.0, `health_watch_limit_fraction` 0.8, `health_min_effective_n_watch` 8, `health_min_effective_n_action` 4, `health_max_avg_correlation_watch` 0.70, `health_max_avg_correlation_action` 0.90, `health_unknown_share_tolerance_pct` 10, `health_unpriced_action_pct` 25, `health_stale_fund_data_multiple` 2.0 and `health_snapshot_enabled` 1.

#### Scenario: Admin-only and validated
- **WHEN** a non-admin changes a health threshold
- **THEN** the change is refused
- **AND** an admin change outside a setting's range, an unknown name, or an action level on the wrong side of its watch level is refused

#### Scenario: Reset
- **WHEN** an admin resets the portfolio settings
- **THEN** the health thresholds return to the defaults above and the change is audited

<!-- tests: tests/test_portfolio_health.py::test_health_thresholds_are_admin_only_validated_and_reset_to_defaults, tests/test_portfolio_health.py::test_new_health_columns_migrate_onto_an_existing_portfolio_settings_row -->

### Requirement: Health is owner-scoped
A checkup SHALL be built only from the actor's own portfolio, and an actor with no portfolio MUST be refused with an authorization error. One owner's checkup MUST NOT contain another owner's holdings, snapshots or restricted list.

#### Scenario: Two owners
- **WHEN** two owners each hold different tickers
- **THEN** each checkup contains only that owner's tickers and totals

#### Scenario: Stranger
- **WHEN** an actor with no portfolio asks for a checkup
- **THEN** the request is refused as not authorized

<!-- tests: tests/test_portfolio_health.py::test_health_is_owner_scoped_one_person_never_sees_anothers_holdings -->

### Requirement: Health result caching
A checkup SHALL be cached per owner and per everything that feeds it (holdings, cash, policy, thresholds, fund data, restricted list, clearance counts, day). A change to any input MUST produce a new result and never a stale one.

#### Scenario: Input change
- **WHEN** cash, a threshold or the restricted list changes
- **THEN** the next checkup has a different version from the cached one
- **AND** an unchanged input returns the same cached result

<!-- tests: tests/test_portfolio_health.py::test_health_is_cached_and_every_input_that_feeds_it_invalidates_the_cache -->

### Requirement: One snapshot per owner per day
The system SHALL store at most one health snapshot per owner per day, taken on the first view of the day or by the evening batch. A second snapshot the same day SHALL refresh the row without a second audit event; the first writes one `save_health_snapshot` audit event in the same transaction.

#### Scenario: Same day twice
- **WHEN** a snapshot is taken twice on one date
- **THEN** one row exists for the date and one audit event was written

#### Scenario: First view of the day
- **WHEN** an owner opens the health view and has no snapshot for today
- **THEN** today's snapshot is created lazily

<!-- tests: tests/test_portfolio_health.py::test_a_snapshot_is_one_row_a_day_audited_once_and_refreshed_not_duplicated, tests/test_portfolio_health.py::test_first_view_has_no_previous_and_a_missing_thirty_day_snapshot_is_none -->

### Requirement: Snapshots can be turned off
When `health_snapshot_enabled` is off, no snapshot SHALL be written and the view MUST still show the live checkup with snapshot status `disabled`.

#### Scenario: Disabled
- **WHEN** the setting is off and a snapshot is requested
- **THEN** the status is `disabled` and no snapshot row exists

<!-- tests: tests/test_portfolio_health.py::test_snapshots_can_be_turned_off_in_settings -->

### Requirement: Snapshots are private and compact
Snapshots SHALL be owner-scoped. A snapshot SHALL keep the numbers (metrics) only for the household scope and statuses and summaries for account and tax slices.

#### Scenario: Two owners
- **WHEN** one owner takes a snapshot
- **THEN** another owner's snapshot list is unaffected

<!-- tests: tests/test_portfolio_health.py::test_snapshots_are_owner_scoped -->

### Requirement: What changed since the last snapshot
The health view SHALL report status changes per scope and check since the latest earlier snapshot and since the newest snapshot on or before 30 days ago, each marked worse or better by severity. With no earlier snapshot it SHALL report no previous, and a missing 30-day snapshot MUST be none.

#### Scenario: Reserve drops
- **WHEN** the emergency reserve went from GOOD to ACTION since the last snapshot
- **THEN** the change is listed as `worse` with its from and to statuses and the previous date

#### Scenario: First view
- **WHEN** an owner has no earlier snapshot
- **THEN** the view reports no previous and no 30-day comparison

<!-- tests: tests/test_portfolio_health.py::test_diff_against_the_last_snapshot_and_thirty_days_ago, tests/test_portfolio_health.py::test_first_view_has_no_previous_and_a_missing_thirty_day_snapshot_is_none -->

### Requirement: Evening batch snapshots every owner
The evening batch SHALL take one snapshot for every owner with a portfolio, and a failure for one owner MUST NOT stop the others.

#### Scenario: One owner fails
- **WHEN** one of two owners' health build raises an error
- **THEN** the other owner's snapshot is created and the failure is counted

<!-- tests: tests/test_portfolio_health.py::test_evening_batch_snapshots_every_owner_and_one_failure_does_not_stop_the_rest -->

### Requirement: ACTION notifies once per run
A household check that is ACTION SHALL notify through the `PORTFOLIO_HEALTH` category at severity 4, once per run of consecutive ACTION snapshots, with dedup key `health:<check>:<date the run began>`. It MUST notify again when the check clears and later returns, and WATCH, UNKNOWN and GOOD MUST NOT notify.

#### Scenario: Ongoing and returning
- **WHEN** a check is ACTION on two consecutive days
- **THEN** one notification is queued with the key of the first day
- **AND** after it clears and returns, a new notification is queued with the new start date

#### Scenario: WATCH
- **WHEN** a check is only WATCH, UNKNOWN or GOOD
- **THEN** no notification is queued

<!-- tests: tests/test_portfolio_health.py::test_a_new_action_notifies_once_per_run_and_again_after_it_clears_and_returns, tests/test_portfolio_health.py::test_watch_never_notifies_and_the_users_preferences_are_respected, tests/test_portfolio_health.py::test_action_streak_start_walks_back_over_consecutive_action_snapshots_only -->

### Requirement: Account-level ACTION notifies with its own key
A check that is ACTION inside one account SHALL notify once per run with dedup key `health:account:<id>:<check>:<date the run began>`, and its brief text MUST say "in one of your accounts" without naming the account. Tax-treatment slices MUST NOT notify separately.

#### Scenario: One concentrated account
- **WHEN** diversification is ACTION in one account and fine in another
- **THEN** only the concentrated account's key is queued, once
- **AND** the brief text does not contain the account name

<!-- tests: tests/test_gap_fixes.py::test_an_action_inside_one_account_notifies_once_with_its_own_key -->

### Requirement: Health notifications honour preferences and carry no amounts
Health notifications SHALL be delivered only as the person's own routing, minimum severity, quiet hours and content level allow, and the brief text MUST NOT contain amounts, tickers or account names. The system MUST enqueue notifications after the snapshot commits, and a notification failure MUST NOT roll back or break the snapshot.

#### Scenario: Preferences
- **WHEN** routing for the category is off, or the minimum severity is above 4, or quiet hours suppress
- **THEN** nothing is delivered

#### Scenario: Brief text
- **WHEN** a notification is queued
- **THEN** its brief text contains no dollar sign and no ticker

<!-- tests: tests/test_portfolio_health.py::test_watch_never_notifies_and_the_users_preferences_are_respected, tests/test_portfolio_health.py::test_quiet_hours_defer_holds_a_health_alert_until_they_end, tests/test_portfolio_health.py::test_a_new_action_notifies_once_per_run_and_again_after_it_clears_and_returns -->

### Requirement: Health uses no language model
Every health status, number and label SHALL be computed deterministically from the gathered inputs, and the health module MUST NOT call a language model or a market-data service. The shared forecast path MUST NOT import the health, health-service or health-snapshot modules.

#### Scenario: Boundary
- **WHEN** the shared instrument-reference and market-context modules are imported
- **THEN** no health module is loaded

<!-- tests: tests/test_portfolio_health.py::test_no_model_decides_anything_and_the_shared_forecast_path_does_not_reach_health -->

### Requirement: Read-only health lookup for trusted callers
A server-side caller holding a verified owner string and no request context SHALL be able to read the owner's health view without taking a snapshot or sending a notification, and with no owner or an owner with no portfolio it MUST return no health data.

#### Scenario: Chat lookup
- **WHEN** the chat tool is called with the signed-in owner
- **THEN** it returns that owner's statuses and counts, no score, and writes no snapshot or notification

#### Scenario: No owner
- **WHEN** the tool is called with no owner
- **THEN** it returns a note and no portfolio data

<!-- tests: tests/test_portfolio_health.py::test_chat_tool_is_owner_scoped_read_only_and_has_no_overall_score -->

### Requirement: Health page presentation
The page SHALL list checks ACTION first, then WATCH, UNKNOWN and GOOD, with a scope selector, details on demand, the educational disclaimer that it is not investment, tax or legal advice and does not tell the owner to buy or sell, and an informational message when the owner has no portfolio. The home card MUST NOT break the home page if the checkup fails.

#### Scenario: No portfolio
- **WHEN** a signed-in user has no portfolio
- **THEN** the page tells them to add an account instead of failing

#### Scenario: Card failure
- **WHEN** building the health view raises an error
- **THEN** the home page still renders

<!-- tests: tests/test_portfolio_health.py::test_html_helpers_escape_and_use_the_four_statuses, tests/test_portfolio_health.py::test_the_health_page_for_a_user_with_no_portfolio_says_so_instead_of_failing, tests/test_portfolio_health.py::test_home_card_never_breaks_the_home_page_and_shows_action_items -->

### Requirement: Ticker assessment never reports an unverifiable limit as not breached
In the per-ticker assessment, when fund contents are known below the coverage minimum, issuer and sector limit checks SHALL use direct numbers with reason `LOOKTHROUGH_PARTIAL`; over the limit MUST be reported as breached and any other result as UNKNOWN, never as not breached.

#### Scenario: Candidate nobody holds
- **WHEN** fund data explains too little of the funds and a candidate is not held
- **THEN** its issuer concentration is UNKNOWN with `LOOKTHROUGH_PARTIAL`
- **AND** a sector already over the limit directly is still reported as breached

<!-- tests: tests/test_fund_lookthrough.py::test_partial_look_through_falls_back_to_direct_and_is_never_reported_as_not_breached -->

### Requirement: Assessment sizing is withheld on missing evidence
The per-ticker assessment SHALL give no recommended position size, with status `INSUFFICIENT_DATA`, when an enforceable limit cannot be evaluated or the current weight is unknown. It MUST give 0 when an enforceable limit is already breached.

#### Scenario: Unsupported currency
- **WHEN** the portfolio's base currency is not supported
- **THEN** sizing is `INSUFFICIENT_DATA` with no recommended percentage

<!-- tests: tests/test_portfolio_assessment.py::test_missing_data_never_produces_a_permissive_sizing_default, tests/test_fund_lookthrough.py::test_partial_look_through_falls_back_to_direct_and_is_never_reported_as_not_breached -->

### Requirement: Assessment advisory checks
The per-ticker assessment SHALL include advisory checks for the leveraged/inverse share (counting a hypothetical add of a leveraged candidate) and for whether the candidate's instrument type is allowed by the profile, with an unknown or unclassified type reported as UNKNOWN.

#### Scenario: Type not allowed
- **WHEN** a candidate's type is not in the profile's allowed types
- **THEN** the advisory check is breached and its detail says the type is not allowed

#### Scenario: Unknown type
- **WHEN** the candidate has no known type
- **THEN** the check is UNKNOWN with reason `INSTRUMENT_TYPE_UNKNOWN`

<!-- tests: tests/test_fund_lookthrough.py::test_leveraged_inverse_and_allowed_type_checks_are_advisory -->

### Requirement: Assessment is deterministic and refused before data is read
The per-ticker assessment SHALL make no language-model call, and an unauthorized actor MUST be refused before any holdings are read.

#### Scenario: Unauthorized actor
- **WHEN** an actor with no portfolio requests an assessment
- **THEN** the request is refused before holdings are read

<!-- tests: tests/test_portfolio_assessment.py::test_unauthorized_actor_is_refused_before_any_holdings_read, tests/test_portfolio_assessment.py::test_routine_assessment_makes_zero_llm_calls -->
