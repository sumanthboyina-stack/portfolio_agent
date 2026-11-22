# accounts-and-holdings Specification

## Purpose
Defines how APEX records investment accounts and the positions and cash they hold: account identity, types and tax treatment, position corrections, snapshot imports from broker files and retirement-plan statements, ticker-less funds, price maintenance, and the owner-scoped, audited service boundary all of it goes through. It covers snapshot-managed accounts; ledger-managed accounts are specified in `transaction-ledger`.

## Requirements

### Requirement: Account identity
The system SHALL identify an account by its portfolio, a lower-cased broker (blank becomes `unknown`) and an account number. An account with no number SHALL be keyed by its nickname, and creation SHALL be refused when it has neither.

#### Scenario: Unnumbered accounts are keyed by name
- **WHEN** an actor creates two manual accounts with different nicknames and no account number
- **THEN** each becomes its own account keyed by its nickname

#### Scenario: Neither number nor name
- **WHEN** an account is created with no account number and no name
- **THEN** creation is refused with an error and nothing is written

<!-- tests: tests/test_account_service.py::test_create_account_keys_unnumbered_accounts_by_name_and_rejects_duplicates -->

### Requirement: Duplicate accounts are rejected
The system SHALL refuse to create an account whose broker and account number already exist in the portfolio, including when the existing account is archived (the error says to restore it).

#### Scenario: Same key twice
- **WHEN** an actor creates a second account with the same broker and nickname as an existing one
- **THEN** the system raises a duplicate-account error and the first account is unchanged

<!-- tests: tests/test_account_service.py::test_create_account_keys_unnumbered_accounts_by_name_and_rejects_duplicates -->

### Requirement: Account type vocabulary
An account's type SHALL be one of `TAXABLE`, `TRADITIONAL_IRA`, `ROTH_IRA`, `ROLLOVER_IRA`, `SEP_IRA`, `401K`, `ROTH_401K`, `403B`, `457B`, `HSA`, `529`, `CASH`, `OTHER`. Free-text and legacy values (such as `IRA`) SHALL be normalized when an account is created or imported; setting a type directly SHALL accept only the listed values.

#### Scenario: Legacy IRA on create
- **WHEN** an account is created with type `IRA`
- **THEN** its type is stored as `TRADITIONAL_IRA`

#### Scenario: Legacy value on direct set
- **WHEN** an actor sets an account's type to `IRA`
- **THEN** the system rejects the value and the type is unchanged

#### Scenario: Importer default
- **WHEN** an importer cannot recognize an account's type
- **THEN** the account type defaults to `TAXABLE`

<!-- tests: tests/test_instrument_taxonomy.py::test_account_type_normalization_maps_legacy_and_free_text, tests/test_instrument_taxonomy.py::test_account_type_normalized_on_create_and_set_with_audit, tests/test_instrument_taxonomy.py::test_parsers_emit_normalized_account_types -->

### Requirement: Tax treatment is derived from account type
The system SHALL derive tax treatment from the account type and never store it: `TAXABLE` and `CASH` are taxable; `TRADITIONAL_IRA`, `ROLLOVER_IRA`, `SEP_IRA`, `401K`, `403B`, `457B` are tax-deferred; `ROTH_IRA`, `ROTH_401K`, `HSA`, `529` are tax-free; `OTHER` or an unset type has none.

#### Scenario: Roth account
- **WHEN** treatment is requested for a `ROTH_401K` account
- **THEN** it is tax-free

#### Scenario: Unknown type
- **WHEN** treatment is requested for an `OTHER` or unset type
- **THEN** no treatment is returned and callers do not assume taxable

<!-- tests: tests/test_instrument_taxonomy.py::test_tax_treatment_is_derived_from_account_type -->

### Requirement: Account type changes are audited and versioned
The system SHALL record one audit event with before and after values for each account type change, and SHALL refuse a change when the caller's expected version no longer matches.

#### Scenario: Stale version
- **WHEN** an actor sets an account type with an expected version that is out of date
- **THEN** a version-conflict error is raised and the type is unchanged

<!-- tests: tests/test_instrument_taxonomy.py::test_account_type_normalized_on_create_and_set_with_audit -->

### Requirement: Rename, archive and restore keep identity
Renaming an account SHALL change only its display name (blank names refused). Archiving SHALL hide the account, its positions and its cash from holdings and cash views while keeping all rows; restoring SHALL bring them back with the same ids. An archived account SHALL NOT accept edits.

#### Scenario: Archive then restore
- **WHEN** an actor archives an account and later restores it
- **THEN** holdings and cash views omit it while archived, and show the same position ids and cash after restore

#### Scenario: Edit while archived
- **WHEN** an actor edits a position of an archived account
- **THEN** the system raises a not-found error

<!-- tests: tests/test_account_service.py::test_archive_hides_account_and_restore_brings_it_back_with_ids_intact, tests/test_holdings_import.py::test_rename_account_keeps_identity -->

### Requirement: Permanent delete is distinct from archive
Permanently deleting an account SHALL remove it with its positions and cash, record an audit event holding the removed positions, and SHALL keep daily price history so past portfolio values are not rewritten.

#### Scenario: Delete keeps history
- **WHEN** an actor permanently deletes an account that has price-history rows
- **THEN** the account and its positions are gone, the history rows remain, and the audit event lists the removed positions

<!-- tests: tests/test_account_service.py::test_permanent_delete_is_distinct_and_keeps_price_history -->

### Requirement: Account and position writes are owner-scoped
Every account, position, cash, classification and price write SHALL be authorized against the portfolio of the verified actor in the request context. An actor with no portfolio, or acting on another owner's account or position, SHALL be refused and nothing changed. No role supplied by the UI SHALL override this.

#### Scenario: Another owner's position
- **WHEN** owner B tries to change the shares of owner A's position
- **THEN** a not-authorized error is raised and the position is unchanged

#### Scenario: Actor with no portfolio
- **WHEN** a verified actor with no portfolio calls any write
- **THEN** the call is refused rather than creating or reading data

<!-- tests: tests/test_account_service.py::test_foreign_actor_cannot_touch_the_local_portfolio, tests/test_instrument_taxonomy.py::test_new_service_verbs_are_owner_scoped, tests/test_trusted_context_scoping.py::test_cross_user_cannot_read_or_write_holdings -->

### Requirement: Account and position reads are owner-scoped
The service read functions for holdings, accounts, cash balances and price history SHALL check the actor's portfolio first and return only that portfolio's rows. A verified actor with no portfolio SHALL be refused, never given an empty or global result.

#### Scenario: Two owners
- **WHEN** owners A and B each hold positions and A lists holdings
- **THEN** only A's positions are returned

#### Scenario: Batch reads
- **WHEN** a system-wide batch job calls the repository holdings read without a portfolio filter
- **THEN** it receives every portfolio's holdings, which is deliberate for batch jobs only

<!-- tests: tests/test_trusted_context_scoping.py::test_list_holdings_for_never_mixes_two_owners_holdings, tests/test_trusted_context_scoping.py::test_a_second_verified_identity_with_no_portfolio_is_refused_not_given_an_empty_result -->

### Requirement: Price history is attributed only to unambiguous accounts
Price history SHALL be returned to an actor only for broker and account-number keys that belong to exactly one portfolio, that portfolio being the actor's. A key shared by two portfolios SHALL return nothing to either.

#### Scenario: Shared external key
- **WHEN** two owners each have an account with the same broker and number and one has history rows
- **THEN** a price-history read by either owner returns an empty list

<!-- tests: tests/test_account_service.py::test_price_history_for_only_returns_unique_account_keys_owned_by_actor, tests/test_account_service.py::test_price_history_for_hides_keys_shared_across_portfolios -->

### Requirement: Account and position writes are audited atomically
Each account, position, cash, classification and price write SHALL run in one database transaction together with one audit event recording actor, source, operation, ids, before and after values, reason and request id. If the audit write fails the data change SHALL roll back. A caller-supplied transaction SHALL let several operations commit or roll back together.

#### Scenario: Audit store failure
- **WHEN** the audit write fails during a position removal
- **THEN** the position still exists

#### Scenario: Composed transaction aborts
- **WHEN** an add-position and a record-cash call share a transaction that is then aborted
- **THEN** neither the position nor the cash is stored

<!-- tests: tests/test_account_service.py::test_audit_event_lands_in_the_same_transaction_as_the_mutation, tests/test_account_service.py::test_operations_compose_into_one_transaction_via_conn, tests/test_instrument_taxonomy.py::test_every_new_verb_writes_one_audit_event -->

### Requirement: Adding a position never silently doubles up
The system SHALL reject adding a ticker an account already holds unless the caller explicitly asks to update, in which case the existing position's quantity and cost are corrected. A ticker is required.

#### Scenario: Duplicate rejected
- **WHEN** an actor adds 20 shares of TICKER to an account already holding 10
- **THEN** a duplicate-position error is raised and the position still holds 10

#### Scenario: Explicit update
- **WHEN** the same call asks for update on duplicate
- **THEN** the existing position is set to 20 shares with its version incremented

<!-- tests: tests/test_account_service.py::test_add_position_rejects_duplicate_unless_update_is_explicit, tests/test_holdings_db.py::test_add_holding_rejects_duplicate_ticker_in_same_account -->

### Requirement: Setting a position is a correction with optimistic locking
Setting a position's shares or cost SHALL be a holdings correction, not a trade. It SHALL derive cost basis from shares times average cost when not given, recompute current value from the stored price, and refuse when the expected version is stale.

#### Scenario: Correction
- **WHEN** an actor sets a priced position to 20 shares at an average cost of C
- **THEN** cost basis becomes 20 times C, value becomes 20 times the stored price, and the audit event records before and after

#### Scenario: Stale editor
- **WHEN** a second editor submits with the old version
- **THEN** a version-conflict error is raised and the first correction stands

<!-- tests: tests/test_account_service.py::test_set_position_is_a_correction_with_optimistic_locking, tests/test_holdings_db.py::test_update_position_refuses_identity_columns_and_unknown_ids -->

### Requirement: Removing a position is not a sale
Removing a position SHALL delete exactly that position and record an audit event; it SHALL NOT create any transaction.

#### Scenario: Remove one
- **WHEN** an actor removes one position of an account holding several
- **THEN** only that position is deleted and the others are unchanged

<!-- tests: tests/test_holdings_db.py::test_delete_holding_by_id_removes_single_row -->

### Requirement: Account cash line
The system SHALL record or forget one cash amount per account, rounded to cents with an as-of date and optional detail. An audit event SHALL be written only when the amount changes.

#### Scenario: Record cash
- **WHEN** an actor records a cash amount for an account
- **THEN** the cash balance view shows that amount for the account

<!-- tests: tests/test_account_service.py::test_operations_compose_into_one_transaction_via_conn -->

### Requirement: Ledger-managed accounts refuse hand edits
For an account whose `holdings_source` is `ledger`, adding, setting or removing a position, recording cash, and importing a snapshot over it SHALL be refused with a ledger-managed error. The same calls SHALL work unchanged on `snapshot` accounts. A hand-entered price SHALL remain allowed on ledger accounts.

#### Scenario: Hand edit on ledger account
- **WHEN** an actor sets shares on a position of a ledger-managed account
- **THEN** a ledger-managed error is raised and the shares are unchanged

#### Scenario: Price on ledger account
- **WHEN** an actor enters a price for a position of a ledger-managed account
- **THEN** the price is stored and the shares are unchanged

<!-- tests: tests/test_ledger_coexistence.py::test_hand_edits_and_imports_are_refused_for_ledger_accounts_but_not_snapshot_ones -->

### Requirement: Holdings source values
An account's `holdings_source` SHALL be `snapshot` (the default for every account, positions are the truth) or `ledger` (positions derive from the transaction ledger).

#### Scenario: New account
- **WHEN** an account is created
- **THEN** its holdings source is `snapshot` and it is not a paper account

<!-- tests: tests/test_instrument_taxonomy.py::test_taxonomy_migration_maps_legacy_values_keeps_originals_and_is_idempotent -->

### Requirement: Paper and archived accounts are excluded from real holdings
Paper accounts (`is_paper`) and archived accounts SHALL be excluded from holdings views, cash balances, totals and the pipeline ticker universe unless a caller explicitly asks to include them.

#### Scenario: Paper account
- **WHEN** a paper account holds a position and a cash balance
- **THEN** the holdings view, cash balances and portfolio ticker list omit them

<!-- tests: tests/test_instrument_taxonomy.py::test_paper_accounts_are_excluded_from_real_holdings_cash_and_pipeline_universe, tests/test_account_service.py::test_archive_hides_account_and_restore_brings_it_back_with_ids_intact -->

### Requirement: Instruments are shared across accounts and tickers can change
A security SHALL be one instrument row shared by every account holding it. Changing an instrument's ticker SHALL keep its id, positions in every account and audit trail, relabel its price history, and refuse a ticker already used by another instrument or a stale version. Nothing is bought or sold.

#### Scenario: Ticker change
- **WHEN** an actor changes TICKER_OLD to TICKER_NEW
- **THEN** positions keep their ids, history rows carry TICKER_NEW, and holdings show TICKER_NEW

#### Scenario: Collision
- **WHEN** TICKER_NEW already belongs to another instrument
- **THEN** a duplicate-instrument error is raised and nothing changes

<!-- tests: tests/test_instruments.py::test_same_ticker_in_two_accounts_is_one_instrument, tests/test_instruments.py::test_change_ticker_keeps_ids_and_relabels_history_in_one_transaction, tests/test_instruments.py::test_change_ticker_rejects_collisions_and_stale_versions, tests/test_instruments.py::test_reimport_under_new_ticker_matches_the_renamed_instrument -->

### Requirement: Manual classification override
An actor who holds a security in an active account SHALL be able to override its instrument type, asset class, sub-asset class, leverage factor (non-zero, within ±5) and proxy ticker. The row SHALL become `classification_source = manual`, the change SHALL be audited, and it SHALL apply wherever the shared instrument is held. A proxy ticker SHALL be a real market symbol.

#### Scenario: Invalid leverage
- **WHEN** an actor sets a leverage factor of 0 or above 5 in magnitude
- **THEN** the override is rejected and nothing changes

#### Scenario: Not a holder
- **WHEN** an actor who holds no position in the instrument tries to override it
- **THEN** a not-authorized error is raised

<!-- tests: tests/test_instrument_taxonomy.py::test_override_validation, tests/test_instrument_taxonomy.py::test_manual_override_sticks_against_automatic_reclassification, tests/test_instrument_taxonomy.py::test_new_service_verbs_are_owner_scoped -->

### Requirement: Automatic classification never overwrites a manual one
Storing an automatic classification SHALL leave a manual or ticker-less-fund classification untouched, SHALL write nothing when the lookup failed (so the next run retries), and SHALL write an audit event only when a value changed. Automatic classification SHALL run after a position add or import commits, never inside the transaction.

#### Scenario: Failed lookup
- **WHEN** the classifier lookup for a classified instrument fails
- **THEN** the stored classification is unchanged and no audit event is written

#### Scenario: Add inside caller transaction
- **WHEN** a position is added with a caller-supplied transaction
- **THEN** no classification lookup runs inside that transaction

<!-- tests: tests/test_instrument_taxonomy.py::test_manual_override_sticks_against_automatic_reclassification, tests/test_instrument_taxonomy.py::test_failed_lookup_stores_nothing_wipes_nothing_and_retries_next_time, tests/test_instrument_taxonomy.py::test_add_position_classifies_after_commit_but_not_inside_a_caller_transaction, tests/test_instrument_taxonomy.py::test_import_commit_classifies_new_securities -->

### Requirement: Funds with no ticker
A fund with no ticker (such as a 401(k) collective trust) SHALL be created as an instrument keyed `CIT:<PLAN>-<FUND>` (upper-case slug) with type `CIT`, `STABLE_VALUE` or `MONEY_MARKET`, classified manually, creation being idempotent and never overwriting an existing instrument's type. It SHALL never be sent to a price provider, the classifier or the prediction pipeline universe. An optional proxy ticker is for analytics only.

#### Scenario: Idempotent creation
- **WHEN** the same plan and fund name are created twice
- **THEN** one instrument exists and one audit event is written

#### Scenario: Unsupported type
- **WHEN** creation asks for a type other than `CIT`, `STABLE_VALUE` or `MONEY_MARKET`
- **THEN** it is rejected

<!-- tests: tests/test_instrument_taxonomy.py::test_cit_instrument_shape_and_proxy_rules, tests/test_instrument_taxonomy.py::test_cit_is_excluded_from_pipeline_universe_and_provider_calls, tests/test_instrument_taxonomy.py::test_classifier_never_looks_up_a_cit, tests/test_instrument_taxonomy.py::test_synthetic_ticker_helpers, tests/test_statement_import.py::test_create_synthetic_instrument_rejects_a_type_it_does_not_support_and_never_changes_an_existing_one -->

### Requirement: Hand-entered position price
An actor SHALL be able to record a positive price and as-of date for a position, which sets its value from shares and writes an audit event. For a ticker-less fund this is the only price source; for a market-priced position the next price refresh overwrites it.

#### Scenario: Non-positive price
- **WHEN** an actor records a price of zero or below
- **THEN** the call is rejected

<!-- tests: tests/test_instrument_taxonomy.py::test_manual_price_for_a_cit_and_its_audit -->

### Requirement: Retirement-plan account details
An account MAY carry plan name, provider, vested balance, loan balance and statement period end. Amounts SHALL be zero or more (rounded to cents) and the date ISO; only fields passed change, empty clears one, an unchanged call writes no audit event, and unknown fields are rejected. A loan balance SHALL NOT be a holding or count as investable value.

#### Scenario: Invalid value
- **WHEN** a negative loan balance or a non-ISO date is passed
- **THEN** the call is rejected and the account is unchanged

#### Scenario: Partial update
- **WHEN** only the provider is cleared
- **THEN** the plan name and other fields keep their values

<!-- tests: tests/test_retirement_extras.py::test_plan_facts_are_nullable_validated_audited_and_clearable, tests/test_retirement_extras.py::test_import_records_plan_facts_on_the_main_account_and_the_loan_is_not_a_holding -->

### Requirement: Upload format detection
Parsing an upload SHALL treat a PDF as a retirement-plan statement, an Excel file through the AI extractor, and a CSV through the Fidelity or Vanguard parser when recognized, otherwise through the AI extractor under the fallback broker. It SHALL raise an error when no valid holdings are found.

#### Scenario: Unrecognized CSV
- **WHEN** a CSV that is neither Fidelity nor Vanguard layout is uploaded
- **THEN** its rows are extracted by the AI extractor and filed under the fallback broker

#### Scenario: Nothing usable
- **WHEN** an upload yields no securities and no cash rows
- **THEN** an error asks for a holdings or positions export

<!-- tests: tests/test_llm_holdings_parser.py::test_is_excel_by_extension_and_signature, tests/test_pdf_statement.py::test_pdf_detection_by_extension_and_signature_and_never_a_spreadsheet, tests/test_holdings_import.py::test_plan_counts_reflect_diff_against_stored_holdings -->

### Requirement: Brokerage CSV parsing
The Fidelity and Vanguard parsers SHALL emit rows with a normalized account type and SHALL treat money-market, sweep, cash and pending-activity lines as cash rows apart from securities. The Fidelity parser SHALL also drop rows without a valid ticker.

#### Scenario: Cash line
- **WHEN** a Fidelity export contains a cash line
- **THEN** it is returned as a cash row and the plan records it as the account's cash amount

<!-- tests: tests/test_instrument_taxonomy.py::test_parsers_emit_normalized_account_types, tests/test_holdings_import.py::test_plan_counts_reflect_diff_against_stored_holdings -->

### Requirement: AI extraction never raises
The AI holdings extractor SHALL fail over across models and SHALL return an empty list, without raising, when every model fails, the answer is unparseable, or the file is corrupt. Long input SHALL be truncated before prompting.

#### Scenario: All models fail
- **WHEN** every model in the chain raises
- **THEN** the extractor returns an empty list

<!-- tests: tests/test_llm_holdings_parser.py::test_failover_skips_failing_and_empty_models, tests/test_llm_holdings_parser.py::test_returns_empty_when_every_model_fails, tests/test_llm_holdings_parser.py::test_returns_empty_on_unparseable_response, tests/test_llm_holdings_parser.py::test_returns_empty_on_corrupt_excel, tests/test_llm_holdings_parser.py::test_prompt_is_truncated -->

### Requirement: Unnamed account groups need a nickname
Rows without an account number SHALL be grouped by account name, and building an import plan SHALL be refused until each group has a nickname, which then becomes its own account.

#### Scenario: Missing nickname
- **WHEN** a plan is built while a group still has no nickname
- **THEN** an error lists the groups that need one

<!-- tests: tests/test_holdings_import.py::test_unnamed_accounts_need_labels_then_become_their_own_bucket -->

### Requirement: Import preview diff
Building an import plan SHALL diff each account's file rows against the actor's stored positions and report added, updated, removed and unchanged tickers, positions and cash accounts, without writing anything.

#### Scenario: Diff counts
- **WHEN** a file adds one ticker, changes one, omits one, and another account is not in the file
- **THEN** the plan reports one added, one updated, one removed, and the other account is untouched

<!-- tests: tests/test_holdings_import.py::test_plan_counts_reflect_diff_against_stored_holdings -->

### Requirement: Import commit replaces each account in place and atomically
Committing a plan SHALL, for each selected account, make its positions equal the file's (updating rows in place so position ids survive), record its cash or forget it when the file has none, and record one import run, all in one transaction with one audit event per account. A failure part-way SHALL leave every account and import history unchanged. Prices SHALL be fetched before the transaction opens.

#### Scenario: Atomic failure
- **WHEN** committing a two-account plan fails on the second account
- **THEN** the first account is unchanged and no import run is recorded

#### Scenario: Ids survive
- **WHEN** a re-import changes a position's shares
- **THEN** the position keeps its id and only its values change

<!-- tests: tests/test_holdings_import.py::test_commit_replaces_only_that_account_records_cash_and_history, tests/test_holdings_import.py::test_commit_is_atomic_across_accounts, tests/test_holdings_db.py::test_replace_account_snapshot_scoped_to_that_account_and_keeps_ids -->

### Requirement: Imports never overwrite ledger-managed accounts
When an import file covers a ledger-managed account, the commit SHALL leave that account unchanged, let the rest of the import proceed, and compare the file's rows with its ledger (reported and audited once, never written).

#### Scenario: Mixed import
- **WHEN** a file covers one snapshot account and one ledger account
- **THEN** the snapshot account is replaced and the ledger account is only compared

<!-- tests: tests/test_ledger_coexistence.py::test_hand_edits_and_imports_are_refused_for_ledger_accounts_but_not_snapshot_ones -->

### Requirement: Statement PDFs are read locally and redacted
A PDF upload SHALL have its text layer read locally; a scanned PDF with no text SHALL be refused with a clear message and never guessed. Only redacted text SHALL reach a model: SSNs, account and long digit numbers (last four kept), addresses, phones, emails, dates of birth and the participant's name SHALL be removed while fund names, plan names, dates, shares, prices and balances are kept.

#### Scenario: Scanned PDF
- **WHEN** a PDF has no text layer
- **THEN** the upload fails with a plain message and no model is called

#### Scenario: Redaction
- **WHEN** statement text contains an account number and an SSN
- **THEN** the model input shows only the account's last four digits and no SSN

<!-- tests: tests/test_pdf_statement.py::test_a_scanned_pdf_is_refused_with_a_clear_message_and_nothing_is_guessed, tests/test_pdf_statement.py::test_ssns_are_removed_in_every_form, tests/test_pdf_statement.py::test_account_numbers_keep_only_the_last_four_and_amounts_are_left_alone, tests/test_pdf_statement.py::test_street_addresses_phones_emails_and_dates_of_birth_are_removed, tests/test_statement_extraction.py::test_only_redacted_text_reaches_the_model_and_the_instructions_name_no_provider, tests/test_statement_extraction.py::test_a_scanned_pdf_never_reaches_the_model -->

### Requirement: Statement extraction keeps funds with no ticker
Statement extraction SHALL keep funds with no ticker apart from ticker-bearing rows, drop total and balance-summary rows, aggregate per-source lines into one row per fund per account (Roth money in its own account type), and never invent shares for dollars-only lines. A model that returns nothing SHALL give a clear error and no rows.

#### Scenario: Dollars-only line
- **WHEN** a statement line gives a balance but no units
- **THEN** the row has no invented shares and keeps its balance

<!-- tests: tests/test_statement_extraction.py::test_empower_style_keeps_cits_stable_value_target_date_and_money_market_and_drops_totals, tests/test_statement_extraction.py::test_per_source_lines_become_one_row_per_fund_per_account_with_roth_in_its_own_account, tests/test_statement_extraction.py::test_dollars_only_lines_never_get_invented_shares_and_a_partial_total_is_left_blank, tests/test_statement_extraction.py::test_a_model_that_returns_nothing_or_junk_gives_a_clear_error_and_no_rows, tests/test_statement_import.py::test_roth_money_lands_in_its_own_account_and_both_are_created -->

### Requirement: Statement ending-balance cross-check never blocks
When a statement states an ending balance, the system SHALL compare the extracted rows with it and report any difference as a warning with the numbers, never as an error; a difference equal to the stated loan SHALL be explained as the loan. With no stated balance the system SHALL note that no cross-check was made.

#### Scenario: Mismatch
- **WHEN** extracted rows differ from the stated ending balance
- **THEN** the preview shows a warning with both numbers and the import remains possible

<!-- tests: tests/test_statement_extraction.py::test_a_difference_is_a_warning_with_the_numbers_and_never_an_error, tests/test_statement_extraction.py::test_no_stated_balance_means_no_cross_check_and_a_note, tests/test_retirement_extras.py::test_a_difference_equal_to_the_loan_is_explained_as_the_loan_not_as_a_missed_fund -->

### Requirement: Statement import keeps what a statement does not list
Importing a retirement-plan statement SHALL create or match funds with no ticker (by key or normalized name, never guessing between look-alikes; another owner's funds never matched), set their prices from the statement's end date, leave the account's cash unchanged, and KEEP positions the statement does not list unless the person chose to remove them. A broker CSV SHALL still replace the whole account by default. Re-importing the same statement SHALL update in place without duplicates.

#### Scenario: Unlisted position kept
- **WHEN** a statement omits a fund the account holds
- **THEN** that position is kept and listed as kept

#### Scenario: Look-alike funds
- **WHEN** two existing funds both resemble a statement fund
- **THEN** neither is chosen automatically and the preview asks the person

<!-- tests: tests/test_statement_import.py::test_commit_creates_funds_with_the_right_types_prices_them_from_the_statement_and_leaves_cash_alone, tests/test_statement_import.py::test_reimporting_the_same_statement_updates_in_place_with_no_duplicates_and_no_deletions, tests/test_statement_import.py::test_a_fund_you_added_by_hand_is_matched_by_its_key_or_its_normalised_name_and_updated_not_duplicated, tests/test_statement_import.py::test_two_funds_that_look_alike_are_never_guessed_between, tests/test_statement_import.py::test_other_owners_funds_are_never_matched_or_shown, tests/test_statement_import.py::test_a_statement_without_a_cit_row_does_not_remove_a_fund_you_added_by_hand_unless_you_tick_the_box, tests/test_statement_import.py::test_a_broker_csv_still_replaces_the_whole_account_by_default -->

### Requirement: Statement price freshness window
A statement-priced holding SHALL be judged against `portfolio_settings.statement_price_max_age_days` (default 100; values of 0, negative or above 730 rejected; admin-editable and resettable) rather than the market clock. A price older than the window SHALL be flagged as needing the next statement, a missing price date SHALL be unknown, and market-priced holdings keep their own rule.

#### Scenario: Inside the window
- **WHEN** a statement price is 90 days old and the window is 100
- **THEN** it is not flagged

#### Scenario: Outside the window
- **WHEN** a statement price is 120 days old
- **THEN** it is flagged as needing the next statement

<!-- tests: tests/test_statement_freshness.py::test_a_statement_price_is_fresh_for_exactly_the_allowed_number_of_days, tests/test_statement_freshness.py::test_the_window_is_a_database_setting_with_a_default_validated_and_resettable, tests/test_statement_freshness.py::test_a_statement_priced_fund_with_no_price_date_is_unknown_not_fresh_and_market_prices_keep_their_own_rule, tests/test_statement_freshness.py::test_the_accounts_page_note_says_when_the_next_statement_is_due_or_overdue -->

### Requirement: Price refresh skips ticker-less funds and is atomic
Refreshing holding prices SHALL re-fetch the latest close for every market holding, stamp the observed trading date, update all rows in one transaction with one audit event, and report tickers it could not price. A ticker-less fund SHALL never be sent to the market-data provider and its hand-entered price SHALL be left alone.

#### Scenario: Refresh all
- **WHEN** two accounts hold the same ticker and prices are refreshed
- **THEN** both positions get the new price, value and price date

<!-- tests: tests/test_holdings_db.py::test_refresh_prices_reprices_every_position_in_one_pass, tests/test_instrument_taxonomy.py::test_cit_is_excluded_from_pipeline_universe_and_provider_calls, tests/test_statement_import.py::test_enrich_prices_never_asks_the_market_about_a_fund_with_no_ticker -->

### Requirement: Daily price snapshot and history repair
The daily snapshot SHALL write one price-history row per position (date, account, ticker), recording the hand-entered price on file for ticker-less funds; re-running on the same day SHALL refresh only each position's own row. Backfill SHALL fill gaps per position, never overwrite existing rows, and never touch days that have an observed snapshot.

#### Scenario: Backfill
- **WHEN** a day has rows for one account but not another
- **THEN** only the missing account's rows are filled and existing rows are unchanged

<!-- tests: tests/test_holdings_db.py::test_snapshot_keeps_one_row_per_account, tests/test_holdings_db.py::test_backfill_fills_partial_days_per_position_without_overwriting, tests/test_holdings_db.py::test_snapshot_days_are_complete_so_estimates_on_them_are_phantoms -->

### Requirement: Holdings storage migrates legacy data safely
Opening the holdings store SHALL migrate a legacy flat holdings table to accounts and positions with identities and legacy values preserved, SHALL be idempotent, and SHALL be able to rebuild collapsed history rows.

#### Scenario: Idempotent migration
- **WHEN** the taxonomy migration runs twice
- **THEN** the second run changes nothing

<!-- tests: tests/test_holdings_db.py::test_legacy_flat_holdings_table_migrates_to_accounts_and_positions, tests/test_holdings_db.py::test_legacy_table_migrates_to_account_grain_and_flags_rows, tests/test_holdings_db.py::test_repair_rebuilds_collapsed_rows_and_rekeys_single_account_rows, tests/test_instrument_taxonomy.py::test_taxonomy_migration_maps_legacy_values_keeps_originals_and_is_idempotent -->
