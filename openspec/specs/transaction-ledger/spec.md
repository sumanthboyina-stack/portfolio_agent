# transaction-ledger Specification

## Purpose
The transaction ledger is the append-only record of what happened in a ledger-managed account. Tax lots, realized gains, positions and cash are derived from it by deterministic replay. The ledger records facts and never executes trades, never blocks a recorded trade, and never invents transactions.

## Requirements

### Requirement: Ledger is append-only
The system SHALL refuse every UPDATE or DELETE of a stored transaction at the database level, so history cannot be edited by any code path.

#### Scenario: Update or delete is refused
- **WHEN** any statement attempts to UPDATE or DELETE a row of the transactions table
- **THEN** the database aborts the statement
- **AND** the stored row is unchanged

<!-- tests: tests/test_ledger.py::test_the_ledger_is_append_only_in_the_database_itself -->

### Requirement: Corrections are VOID rows
The system SHALL correct a mistake only by appending a VOID row that points at the transaction it cancels. A reason MUST be supplied. A VOID cannot be voided, a transaction can be voided once, and a void that would leave the ledger inconsistent MUST be refused.

#### Scenario: Void keeps the original
- **WHEN** an owner voids a transaction with a reason
- **THEN** a VOID row is appended pointing at it
- **AND** the original row remains stored and is reported as voided
- **AND** the original and the VOID row are both left out of the replay

#### Scenario: Void without reason or twice is refused
- **WHEN** a void is requested with a blank reason, or for a transaction that is already voided, or for a VOID row
- **THEN** the request is refused and nothing is written

#### Scenario: Void that breaks a later sale is refused
- **WHEN** an owner voids a BUY whose shares a later SELL closed
- **THEN** the void is refused because the ledger without that BUY does not replay
- **AND** nothing is written

<!-- tests: tests/test_ledger.py::test_void_removes_both_rows_and_is_validated, tests/test_ledger.py::test_void_cancels_by_appending_and_keeps_the_original -->

### Requirement: Transaction types
The system SHALL accept exactly these transaction types in the ledger: BUY, SELL, DEPOSIT, WITHDRAWAL, DIVIDEND, INTEREST, EMPLOYER_MATCH, FEE, SPLIT, VOID and OPENING_BALANCE.

#### Scenario: Unknown type is rejected by the store
- **WHEN** a row with any other type is inserted
- **THEN** the database rejects it

#### Scenario: Older databases are upgraded
- **WHEN** the schema is created on a database whose transactions table predates OPENING_BALANCE
- **THEN** the table is rebuilt in place with every row and id preserved
- **AND** running the migration again changes nothing

<!-- tests: tests/test_ledger.py::test_migration_adds_the_ledger_to_an_existing_database_idempotently, tests/test_ledger_coexistence.py::test_a_ledger_created_before_opening_balances_existed_is_upgraded_in_place -->

### Requirement: Idempotent writes by external id
The system SHALL treat a write that carries an `external_id` already used in the same account as a duplicate: it returns the stored row and writes nothing.

#### Scenario: Repeat write
- **WHEN** a trade is recorded twice with the same account and external id
- **THEN** the second call returns the stored transaction flagged as duplicate
- **AND** the account holds one transaction

<!-- tests: tests/test_ledger.py::test_external_id_makes_a_write_idempotent -->

### Requirement: Transaction field validation
The system SHALL reject a transaction whose side is not BUY or SELL, whose quantity or amount is not a finite number greater than 0, whose fees are negative, whose date is not YYYY-MM-DD or is in the future, whose lot method is given on a BUY, or whose cash-transfer direction or income kind is unknown. A rejected request writes nothing.

#### Scenario: Invalid fields
- **WHEN** a trade is submitted with side HOLD, quantity 0, a negative price, a non-date, a future date, negative fees, NaN quantity, or a lot method on a BUY
- **THEN** the request is refused
- **AND** the account has no transactions

<!-- tests: tests/test_ledger.py::test_field_validation -->

### Requirement: Cash effect of each transaction
The system SHALL store each transaction's signed cash effect: a BUY moves cash by minus (quantity times price plus fees), a SELL by plus (quantity times price minus fees), DEPOSIT, DIVIDEND, INTEREST and cash EMPLOYER_MATCH by plus the amount, WITHDRAWAL and FEE by minus the amount. A share EMPLOYER_MATCH moves no cash.

#### Scenario: Cash replay
- **WHEN** an account's transactions are replayed
- **THEN** derived cash equals the sum of the signed cash effects of the effective transactions
- **AND** totals report deposits, withdrawals, dividends, interest, employer-match cash and fees separately

<!-- tests: tests/test_ledger.py::test_cash_flows_totals_and_employer_match_both_forms -->

### Requirement: Lot basis and sale proceeds
The system SHALL open a lot for each BUY with unit cost equal to (quantity times price plus fees) divided by quantity, and SHALL compute a SELL's net proceeds as quantity times price minus fees, split across the lots it closes in proportion to quantity, each closure carrying proceeds, cost basis and realized gain.

#### Scenario: Fees on buy and sell
- **WHEN** a BUY with fees and a SELL with fees are replayed
- **THEN** the lot's basis includes the buy fees
- **AND** the sale's closures' proceeds add up to quantity times price minus fees

#### Scenario: Sale spans lots
- **WHEN** a SELL is larger than the first lot picked
- **THEN** it closes that lot fully and takes the remainder from the next lot
- **AND** partly closed lots keep their remaining quantity

<!-- tests: tests/test_ledger.py::test_fees_raise_basis_on_a_buy_and_cut_proceeds_on_a_sale_and_roll_up_pro_rata, tests/test_ledger.py::test_a_sale_spans_lots_in_order_and_partial_lots_keep_their_remainder -->

### Requirement: Lot selection methods
The system SHALL close lots in order by the sale's method: FIFO (oldest first), LIFO (newest first), HIFO (highest unit cost first, ties by date then id), or SPECIFIC (the lots the sale names).

#### Scenario: Each method picks its lots
- **WHEN** the same sale is replayed with FIFO, LIFO and HIFO against three lots
- **THEN** each method closes its own lots and realizes its own gain

<!-- tests: tests/test_ledger.py::test_each_method_picks_its_lot_and_realizes_the_hand_computed_gain -->

### Requirement: SPECIFIC lot sales
The system SHALL require a SPECIFIC sale to name open lots of the sold instrument whose quantities add up to the sale quantity, with no lot named twice and no quantity above a lot's remainder, and SHALL store the selection on the sale.

#### Scenario: Valid selection
- **WHEN** a sale names lots whose quantities add up to the sale
- **THEN** those lots are closed in those quantities and the selection is stored on the sale

#### Scenario: Invalid selection
- **WHEN** the named quantities do not add up, a lot is not open, a lot is named twice, or a lot has less than the named quantity
- **THEN** the sale is refused

<!-- tests: tests/test_ledger.py::test_specific_lots_are_honoured_and_checked, tests/test_ledger.py::test_specific_lot_sale_through_the_service -->

### Requirement: Lot method resolved when the sale is recorded
The system SHALL resolve a sale's lot method when it is recorded as: the sale's own choice, else the account's lot method, else the portfolio default (seed FIFO), and SHALL store the result on the sale so later setting changes never alter a past sale.

#### Scenario: Precedence and stability
- **WHEN** a sale names a method, or the account has one, or neither
- **THEN** the sale uses its own, else the account's, else the portfolio default
- **AND** changing the account or default method afterwards leaves earlier sales' stored method and gains unchanged

<!-- tests: tests/test_ledger.py::test_lot_method_precedence_is_sale_then_account_then_default_and_is_stored_on_the_sale -->

### Requirement: Standing lot method excludes SPECIFIC
The system SHALL allow only FIFO, LIFO or HIFO (or none, meaning use the default) as an account's standing lot method and as the portfolio default; SPECIFIC and any other value MUST be refused. Changing an account's method MUST be audited and owner-checked.

#### Scenario: Refused values
- **WHEN** an owner sets an account's lot method or the portfolio default to SPECIFIC, AVERAGE or an unknown value
- **THEN** the change is refused

#### Scenario: Reset
- **WHEN** portfolio settings are reset to defaults
- **THEN** the default lot method is FIFO

<!-- tests: tests/test_ledger.py::test_lot_method_precedence_is_sale_then_account_then_default_and_is_stored_on_the_sale -->

### Requirement: Holding period classification
The system SHALL classify each lot closure as LONG when the lot was held more than 365 days (open date to close date) and SHORT otherwise.

#### Scenario: Boundary
- **WHEN** a lot is sold exactly 365 days after it opened
- **THEN** the closure is SHORT
- **AND** a sale 366 days after opening is LONG

<!-- tests: tests/test_ledger.py::test_holding_period_is_long_only_after_more_than_365_days -->

### Requirement: Stock splits
The system SHALL apply a split by multiplying the open lots' shares by numerator over denominator and dividing their unit cost by the same ratio, leaving total basis unchanged, effective at the start of its day. A split MUST be recorded once for each account that held the ticker on the date and refused when none did.

#### Scenario: Forward and reverse
- **WHEN** a 2-for-1 and a 1-for-10 split are replayed
- **THEN** shares scale by the ratio, unit cost scales inversely, and total basis is unchanged

#### Scenario: Same-day buy
- **WHEN** a BUY is dated the same day as a split
- **THEN** the split is applied first and that BUY's lot is not split

#### Scenario: No holder
- **WHEN** a split is recorded for a ticker no account of the owner held that day
- **THEN** the request is refused

<!-- tests: tests/test_ledger.py::test_split_and_reverse_split_scale_shares_and_unit_cost_but_not_total_basis, tests/test_ledger.py::test_a_split_goes_first_on_its_day_so_a_same_day_buy_is_not_split, tests/test_ledger.py::test_income_fee_match_and_split_verbs -->

### Requirement: Employer match, income, fee and cash transfer entries
The system SHALL record an employer match either as cash or as shares (never both), where shares open a lot at the given price with no cash movement. It SHALL record DIVIDEND or INTEREST income, account-level FEE, and DEPOSIT or WITHDRAWAL transfers with a positive amount, and SHALL upper-case an optional contribution source.

#### Scenario: Match forms
- **WHEN** an employer match gives both an amount and shares, or neither
- **THEN** the request is refused
- **AND** a share match opens a lot at its price with no cash movement

<!-- tests: tests/test_ledger.py::test_income_fee_match_and_split_verbs, tests/test_ledger.py::test_cash_flows_totals_and_employer_match_both_forms -->

### Requirement: Writes are validated by replay and atomic
The system SHALL, for a ledger account, replay the whole ledger with each new row included before accepting it, and SHALL refuse the write, storing nothing, when the replay fails (for example selling shares not held on that date, including backdated entries replayed in date order).

#### Scenario: Oversell
- **WHEN** a SELL exceeds the shares held at its trade date
- **THEN** it is refused and nothing is written

#### Scenario: Sale before purchase
- **WHEN** a SELL is dated before the BUY that would supply its shares
- **THEN** it is refused

<!-- tests: tests/test_ledger.py::test_overselling_and_selling_before_buying_are_refused, tests/test_ledger.py::test_an_invalid_change_writes_nothing -->

### Requirement: Replay is deterministic and lots are derived
The system SHALL derive tax lots and lot closures only by replay, rebuilt in the same database transaction as every ledger change, never edited. Replay MUST give the same state regardless of input order, and a lot's identity MUST be the id of the transaction that opened it.

#### Scenario: Derived equals fresh replay
- **WHEN** several ledger writes have been made
- **THEN** stored lots and closures equal a fresh replay of the ledger
- **AND** lot ids are unchanged by rebuilding

<!-- tests: tests/test_ledger.py::test_replay_is_deterministic_and_independent_of_input_order, tests/test_ledger.py::test_derived_tables_equal_a_fresh_replay_and_lot_ids_are_stable, tests/test_ledger.py::test_a_ledger_account_gets_lots_closures_and_cash_from_its_trades -->

### Requirement: Positions and cash of a ledger account are derived
The system SHALL, on every ledger write, set a ledger account's positions (shares, average cost, cost basis from open lots) and cash to what replay says, keeping existing price, sector and ids and removing fully sold positions. The system SHALL provide a rebuild that repeats this on demand, idempotently, and refuse it for snapshot accounts.

#### Scenario: Sync and removal
- **WHEN** a trade is recorded, or a sale sells out a position
- **THEN** positions and cash equal the replay result in the same transaction
- **AND** a sold-out position is removed

#### Scenario: Rebuild
- **WHEN** positions have drifted and a rebuild is requested for a ledger account
- **THEN** positions are repaired and an immediate second rebuild changes nothing

#### Scenario: Rebuild on snapshot account
- **WHEN** a rebuild is requested for a snapshot account
- **THEN** it is refused

<!-- tests: tests/test_ledger_coexistence.py::test_every_ledger_write_keeps_positions_and_cash_in_step_including_removal, tests/test_ledger_coexistence.py::test_rebuild_repairs_positions_that_drifted_and_is_refused_for_snapshot_accounts -->

### Requirement: Snapshot accounts take transactions as history only
The system SHALL store transactions recorded on a snapshot-source account without validating them against positions, building lots, or changing positions or cash, because the snapshot is the truth there.

#### Scenario: History on a snapshot account
- **WHEN** a SELL is recorded on a snapshot account that holds fewer shares
- **THEN** the transaction is stored
- **AND** no lots are built and positions and cash are unchanged

<!-- tests: tests/test_ledger.py::test_snapshot_accounts_take_transactions_as_history_only -->

### Requirement: Hand edits are refused on ledger accounts
The system SHALL refuse manual position add, set or remove, cash entry and snapshot import over a ledger-managed account, while still allowing price updates, and SHALL allow all of these on snapshot accounts.

#### Scenario: Refused edit
- **WHEN** an owner tries to set a position or record cash on a ledger account
- **THEN** a LedgerManaged refusal is returned and nothing changes
- **AND** the same action on a snapshot account succeeds

<!-- tests: tests/test_ledger_coexistence.py::test_hand_edits_and_imports_are_refused_for_ledger_accounts_but_not_snapshot_ones -->

### Requirement: Switching a snapshot account to the ledger
The system SHALL turn a snapshot account into a ledger account by recording, as of an opening date, one OPENING_BALANCE row per position (unit cost equal to cost basis divided by shares) and one for cash, set the account's holdings source to ledger and its ledger start date to the opening date. These rows are not trades and positions are unchanged by the switch. An account already on the ledger MUST be refused. A preview that writes nothing SHALL show the opening rows.

#### Scenario: Opening balance
- **WHEN** an owner switches an account holding positions and cash to the ledger on a date
- **THEN** OPENING_BALANCE rows are recorded and positions are unchanged
- **AND** the opening lots can be sold later like any lot

#### Scenario: Empty account
- **WHEN** an account with no positions is switched
- **THEN** it becomes a ledger account starting from nothing

<!-- tests: tests/test_ledger_coexistence.py::test_switch_records_opening_balances_not_trades_and_positions_are_unchanged, tests/test_ledger_coexistence.py::test_opening_lots_are_real_lots_for_later_sales, tests/test_ledger_coexistence.py::test_an_empty_snapshot_account_can_switch_and_start_from_nothing, tests/test_ledger_page.py::test_switch_preview_shows_the_opening_balances_writes_nothing_and_flags_missing_basis -->

### Requirement: Opening balance requires a cost basis
The system SHALL refuse the switch for a position with no known cost basis unless a per-ticker unit cost is supplied or unknown basis is explicitly allowed, in which case that lot opens at unit cost 0 and is reported as unknown basis. Short positions MUST be refused.

#### Scenario: Missing basis
- **WHEN** a position has no cost basis and no override or allowance is given
- **THEN** the switch is refused naming the ticker

#### Scenario: Allowed unknown basis
- **WHEN** the owner allows unknown basis
- **THEN** the lot opens at unit cost 0, the opening row is marked "cost basis unknown", and the ticker is returned as unknown basis

<!-- tests: tests/test_ledger_coexistence.py::test_unknown_cost_basis_is_refused_unless_given_or_explicitly_allowed, tests/test_ledger_coexistence.py::test_short_positions_and_future_dates_are_refused, tests/test_ledger_unknown_basis_and_history.py::test_a_basis_given_up_front_is_kept_exactly -->

### Requirement: Unknown basis is reported as unknown, not zero
The system SHALL report cost basis and average cost as unknown (not 0) for a position that includes any unknown-basis lot, mark such lots and closures as unknown basis, exclude sales of them from realized gain, and report their proceeds separately.

#### Scenario: Position with unknown lot
- **WHEN** a position was opened without a basis and then more shares are bought with a known price
- **THEN** the position's cost basis and average cost remain unknown as a whole

#### Scenario: Sale of unknown-basis shares
- **WHEN** unknown-basis shares are sold
- **THEN** the realized gain total excludes the sale
- **AND** its proceeds are reported as unknown-basis proceeds

<!-- tests: tests/test_ledger_unknown_basis_and_history.py::test_a_position_opened_without_a_basis_has_no_cost_basis_not_a_zero_one, tests/test_ledger_unknown_basis_and_history.py::test_lots_show_unknown_and_a_sale_of_unknown_shares_has_no_gain_and_is_reported_separately, tests/test_ledger_unknown_basis_and_history.py::test_buying_more_of_an_unknown_position_adds_a_known_lot_but_the_position_as_a_whole_stays_unknown, tests/test_ledger_unknown_basis_and_history.py::test_unrealized_gain_leaves_out_the_unknown_position_instead_of_counting_all_of_it_as_profit -->

### Requirement: Ledger start date
The system SHALL keep transactions dated before a ledger account's start date as history that is not replayed, and SHALL refuse to add a non-VOID transaction dated before that start.

#### Scenario: Earlier history
- **WHEN** history exists before the opening date and a new trade is dated before it
- **THEN** the history stays stored but is not replayed
- **AND** the new trade is refused

<!-- tests: tests/test_ledger_coexistence.py::test_history_before_the_opening_date_is_kept_but_not_replayed_and_cannot_be_added_later -->

### Requirement: Switching a ledger account back to a snapshot
The system SHALL hand a ledger account back to snapshot management by keeping its positions and cash as last derived, clearing its tax lots and closures, keeping its transactions as history, and clearing its ledger start date. Switching to the ledger again MUST require a fresh opening balance.

#### Scenario: Switch back
- **WHEN** a ledger account is switched to snapshot
- **THEN** positions and cash are unchanged and lots are cleared
- **AND** a later switch to the ledger records a new opening balance

<!-- tests: tests/test_ledger_coexistence.py::test_switch_back_keeps_positions_clears_lots_and_a_second_switch_starts_a_fresh_opening -->

### Requirement: Accounts with transactions cannot be hard-deleted
The system SHALL refuse to hard-delete an account that has ledger transactions; it MUST be archived instead.

#### Scenario: Delete attempt
- **WHEN** an owner hard-deletes an account with transactions
- **THEN** the deletion is refused

<!-- tests: tests/test_ledger.py::test_an_account_with_transactions_cannot_be_hard_deleted -->

### Requirement: Reconciliation compares a statement with the ledger
The system SHALL compare the ledger (optionally as of a date, ignoring later transactions) with a broker statement of positions and optional cash, labelling each ticker MATCH, DIFFERENT, ONLY_IN_LEDGER or ONLY_IN_STATEMENT within a share tolerance and cash MATCH or DIFFERENT within a cash tolerance.

#### Scenario: Each kind of difference
- **WHEN** a statement differs from the ledger in shares, omits a ledger ticker, or adds a ticker the ledger lacks
- **THEN** each ticker is labelled with the matching status and the overall result reports no match

#### Scenario: As-of and rounding
- **WHEN** reconciliation runs as of an earlier date, or shares differ within tolerance
- **THEN** later transactions are ignored and the within-tolerance ticker is MATCH

<!-- tests: tests/test_ledger_coexistence.py::test_reconcile_reports_every_kind_of_difference_and_changes_nothing, tests/test_ledger_coexistence.py::test_reconcile_as_of_a_date_ignores_later_transactions_and_tolerates_rounding, tests/test_ledger_page.py::test_reconciliation_view_adds_tones_reports_parse_errors_and_changes_nothing -->

### Requirement: Reconciliation never writes
The system SHALL NOT create a transaction or edit a position or cash balance during reconciliation. It MAY write an audit event (suppressible for previews). A person decides what happened and records it or voids a wrong row.

#### Scenario: Differences are only reported
- **WHEN** reconciliation finds differences
- **THEN** the ledger, positions and cash are identical before and after

<!-- tests: tests/test_ledger_coexistence.py::test_reconcile_reports_every_kind_of_difference_and_changes_nothing -->

### Requirement: Clearance never blocks a recorded trade
The system SHALL record a clearance status on each BUY or SELL of a non-paper account, audit it, and MUST NOT refuse or alter the trade because of it. Statuses: `approved` (with approval id) when an approval covering the ticker and side (or a general trade) was in force on the trade date, `missing`, `not_required` (pre-clearance off), and `unknown` when approvals could not be read (never shown as missing or approved).

#### Scenario: No approval
- **WHEN** an owner records a trade with pre-clearance required and no approval on file
- **THEN** the trade is stored with clearance status `missing` and an audit event

#### Scenario: Approval read as of trade date
- **WHEN** an approval was granted after the trade date, or revoked after it
- **THEN** the later grant does not cover the earlier trade and the later revoke does not un-cover it

#### Scenario: Other statuses
- **WHEN** pre-clearance is off, approvals are unreadable, or the account is a paper account
- **THEN** the status is `not_required`, `unknown`, or empty (not checked) respectively

<!-- tests: tests/test_ledger_compliance_import.py::test_a_trade_without_an_approval_is_recorded_flagged_missing_and_audited_never_blocked, tests/test_ledger_compliance_import.py::test_an_approval_covering_ticker_side_and_date_marks_the_trade_approved_with_its_id, tests/test_ledger_compliance_import.py::test_approvals_are_read_as_of_the_trade_date, tests/test_ledger_compliance_import.py::test_not_required_when_the_owner_turned_pre_clearance_off, tests/test_ledger_compliance_import.py::test_a_paper_account_is_not_checked_at_all, tests/test_ledger_compliance_import.py::test_unreadable_approvals_are_unknown_not_missing_and_send_nothing -->

### Requirement: Missing clearance notification has no amounts
The system SHALL enqueue one brief COMPLIANCE notification for a trade with clearance status `missing`, naming only side, ticker and date and no quantities, prices, fees or amounts, deduplicated per transaction by the key `ledger_clearance:<transaction id>`, and only after the trade's transaction has committed. Approved, not-required and unknown trades send nothing.

#### Scenario: One notification per trade
- **WHEN** a trade without approval is recorded and the notifier is called again for it
- **THEN** one notification per active channel exists, with no amounts in its text
- **AND** the repeat call enqueues nothing

#### Scenario: Caller-owned transaction
- **WHEN** a trade is recorded inside a caller-owned database transaction
- **THEN** nothing is enqueued until the caller notifies after its commit

<!-- tests: tests/test_ledger_compliance_import.py::test_a_missing_clearance_enqueues_one_brief_compliance_notification_without_amounts, tests/test_ledger_compliance_import.py::test_with_a_caller_owned_transaction_the_notification_waits_for_the_caller -->

### Requirement: CSV column mapping and preview write nothing
The system SHALL suggest a mapping of a CSV's headers to the fields date, type, ticker, quantity, price, amount, fees, external_ref and contribution_source, require date and type to be mapped, reject unknown fields, missing columns or one column mapped twice, and SHALL preview an import by labelling each row new, duplicate, skipped or invalid with errors and warnings without writing anything. New rows MUST be dry-run through the replay so unsupported sales are reported before import.

#### Scenario: Preview
- **WHEN** an owner previews a file against a ledger account
- **THEN** no transaction, import run or lot is written
- **AND** a sale the holdings cannot support is reported in the preview and the import is not allowed

#### Scenario: Unreadable file or mapping
- **WHEN** the file is empty, has a blank or repeated header, or the mapping omits date or type
- **THEN** a clear error is returned

<!-- tests: tests/test_ledger_compliance_import.py::test_the_mapping_step_suggests_columns_and_the_preview_writes_nothing, tests/test_ledger_compliance_import.py::test_a_sale_the_ledger_cannot_support_is_caught_in_the_preview_and_nothing_is_written, tests/test_ledger_compliance_import.py::test_mapping_and_file_errors_are_clear -->

### Requirement: CSV row interpretation
The system SHALL map row types through an alias table (for example Bought to BUY, Contribution to DEPOSIT), make a row with an unknown type invalid rather than guessing, and refuse SPLIT rows. BUY and SELL need ticker, quantity and a price, deriving the price from the amount with a warning when blank. Cash types need a non-zero amount whose sign is ignored, and DIVIDEND needs a ticker. Dates and numbers accept common formats including parenthesised negatives.

#### Scenario: Unknown type
- **WHEN** a row's type is not in the alias table
- **THEN** the row is invalid and the error names the type

#### Scenario: Derived price
- **WHEN** a BUY row has an amount but no price
- **THEN** the price is derived from the amount less fees divided by quantity and a warning is attached

<!-- tests: tests/test_ledger_compliance_import.py::test_cell_parsing_and_price_derivation_warning, tests/test_ledger_compliance_import.py::test_invalid_rows_block_the_whole_import_with_the_line_numbers -->

### Requirement: CSV import is all or nothing and ledger-only
The system SHALL apply a CSV import in one database transaction, refuse it with nothing written when any row is invalid, when the new rows do not replay, or when the account is a snapshot account, push every row through the same validation, lot rebuild, clearance check and audit as a hand entry, and tag rows with an import run (broker `csv_transactions`) and contribution source.

#### Scenario: Invalid row blocks all
- **WHEN** any row is invalid
- **THEN** nothing is imported and the refusal lists line numbers

#### Scenario: Failure midway
- **WHEN** a row fails while applying
- **THEN** no row of the file remains stored

#### Scenario: Snapshot account
- **WHEN** an import targets a snapshot account
- **THEN** it is rejected and nothing is written

<!-- tests: tests/test_ledger_compliance_import.py::test_import_applies_everything_through_the_ledger_and_records_the_run, tests/test_ledger_compliance_import.py::test_import_into_a_snapshot_account_is_rejected_and_writes_nothing, tests/test_ledger_compliance_import.py::test_the_import_is_all_or_nothing_if_a_row_fails_midway, tests/test_ledger_compliance_import.py::test_invalid_rows_block_the_whole_import_with_the_line_numbers -->

### Requirement: CSV import deduplication
The system SHALL treat a row as a duplicate when its external reference is already used in the account, else when a non-voided transaction has the same date, type, ticker, quantity and cash amount, matched by count so genuinely identical rows are kept the first time and a re-import adds nothing.

#### Scenario: Re-import
- **WHEN** the same file is imported twice
- **THEN** the second import adds nothing and counts the rows as skipped

#### Scenario: Identical rows without reference
- **WHEN** a file holds two identical trades and the account has none
- **THEN** both are imported, and a later file with three adds only one

<!-- tests: tests/test_ledger_compliance_import.py::test_reimporting_the_same_file_adds_nothing, tests/test_ledger_compliance_import.py::test_without_an_external_ref_rows_dedupe_on_date_type_ticker_quantity_and_amount_by_count -->

### Requirement: CSV import order and ledger start
The system SHALL apply new rows by date with same-day money in and purchases before sales and withdrawals, so file order does not make a sale precede its purchase, and SHALL skip, not reject, rows dated on or before the account's ledger start because the opening balance already holds their effect.

#### Scenario: Sale listed first
- **WHEN** a file lists a sale before its purchase
- **THEN** the import succeeds

#### Scenario: Covered by opening balance
- **WHEN** rows are dated on or before the ledger start
- **THEN** they are skipped with a reason and later rows stay new

<!-- tests: tests/test_ledger_compliance_import.py::test_file_order_does_not_matter_a_sale_listed_before_its_purchase_still_imports, tests/test_ledger_fidelity_import.py::test_rows_on_or_before_the_ledger_start_are_skipped_not_invalid_and_later_ones_stay_new, tests/test_ledger_vanguard_import.py::test_rows_on_or_before_the_ledger_start_are_skipped -->

### Requirement: Fidelity and Vanguard history presets
The system SHALL recognise a Fidelity "History for Account" export and a Vanguard "OfxDownload" CSV by their headers and rewrite them into the generic columns, taking sale and purchase quantity from the sign, deriving price from the cash that moved, treating money-market dividends as interest, and skipping with a reason money-market sweeps and reinvestments, and Vanguard CUSIP changes. An action the preset does not know MUST become an invalid row naming it.

#### Scenario: Preset recognised
- **WHEN** a Fidelity or Vanguard file is read
- **THEN** disclaimer lines or the positions table are ignored and each transaction maps to a ledger type

#### Scenario: Cash reconciles
- **WHEN** the whole Fidelity export is imported
- **THEN** ledger cash equals the file's amount column

<!-- tests: tests/test_ledger_fidelity_import.py::test_the_export_is_recognised_the_disclaimer_lines_are_dropped_and_the_mapping_is_right, tests/test_ledger_fidelity_import.py::test_the_cash_of_the_imported_rows_equals_the_amount_column_of_the_file, tests/test_ledger_fidelity_import.py::test_an_action_the_preset_does_not_know_is_invalid_and_names_it, tests/test_ledger_vanguard_import.py::test_the_transactions_table_is_found_and_the_positions_table_above_it_is_ignored, tests/test_ledger_vanguard_import.py::test_sweeps_money_market_reinvestments_and_cusip_changes_are_skipped_with_a_reason, tests/test_ledger_vanguard_import.py::test_an_unknown_transaction_type_is_invalid_and_names_it -->

### Requirement: Saved activity history
The system SHALL save broker history rows dated on or before a ledger account's start (or any row of a snapshot account) as history for the activity summary only, all or nothing, counting already-saved rows rather than duplicating them and leaving later rows for transaction import. Saving history MUST NOT change positions, lots, cash or ledger transactions. Clearing history deletes only saved history and is audited.

#### Scenario: Save twice
- **WHEN** a history file is saved twice
- **THEN** the second save adds nothing and the ledger has no new transactions

#### Scenario: Never touches the ledger
- **WHEN** history is saved
- **THEN** positions, lots and cash are identical before and after

<!-- tests: tests/test_ledger_unknown_basis_and_history.py::test_history_rows_up_to_the_ledger_start_are_saved_and_a_second_upload_adds_nothing, tests/test_ledger_unknown_basis_and_history.py::test_saving_history_never_touches_positions_lots_or_cash, tests/test_ledger_unknown_basis_and_history.py::test_rows_dated_after_the_ledger_start_are_left_for_the_transaction_import, tests/test_ledger_unknown_basis_and_history.py::test_a_snapshot_account_takes_every_row_as_history, tests/test_ledger_unknown_basis_and_history.py::test_an_invalid_row_stops_the_whole_save_and_clearing_removes_only_history, tests/test_ledger_unknown_basis_and_history.py::test_identical_rows_in_one_file_are_both_kept_and_a_longer_file_adds_only_the_new_ones -->

### Requirement: Activity summary
The system SHALL summarize an account, or all of the owner's active non-paper accounts with a line per account, by kind and optionally by year, combining saved history with the ledger's effective transactions: dividends, interest, deposits (by source), withdrawals, fees, employer-match cash, purchases and sale proceeds. Voided transactions, opening balances and splits MUST NOT count, and sale gains are not part of it.

#### Scenario: Combined view
- **WHEN** an account has saved history and ledger transactions
- **THEN** the summary adds both and reports how many rows came from each

#### Scenario: Voided and opening rows
- **WHEN** a transaction is voided or an opening balance exists
- **THEN** neither is counted as activity

<!-- tests: tests/test_ledger_unknown_basis_and_history.py::test_the_activity_summary_adds_up_the_file_by_kind, tests/test_ledger_unknown_basis_and_history.py::test_the_summary_combines_saved_history_with_the_ledgers_own_transactions, tests/test_ledger_unknown_basis_and_history.py::test_a_voided_ledger_transaction_is_not_counted_and_opening_balances_are_not_activity, tests/test_ledger_unknown_basis_and_history.py::test_the_all_accounts_summary_has_a_line_per_account_and_is_owner_scoped -->

### Requirement: Transaction and realized-gains exports
The system SHALL export every transaction of an account including voided and VOID rows marked as such, and realized gains by lot closure (ticker, quantity, open and close dates, proceeds, cost basis, gain, SHORT or LONG, method), optionally for one close year, header-only for an account with no sales or a snapshot account. Text cells beginning with `=`, `+`, `-`, `@`, tab or carriage return MUST be prefixed with an apostrophe.

#### Scenario: Transactions export
- **WHEN** an account with a voided trade is exported
- **THEN** the voided row and its VOID row are both listed with their void links

#### Scenario: Formula-looking text
- **WHEN** a memo starts with `=`
- **THEN** the exported cell starts with an apostrophe

#### Scenario: Year filter
- **WHEN** realized gains are exported for a year with no closures
- **THEN** only the header row is returned

<!-- tests: tests/test_ledger_compliance_import.py::test_transactions_export_lists_everything_including_voided_rows_and_defuses_formulas, tests/test_ledger_compliance_import.py::test_realized_gains_export_has_the_lots_and_filters_by_year -->

### Requirement: Ledger access is owner-scoped and audited
The system SHALL check, for every ledger read and write (including panel data functions, CSV import, exports and switches), that the account belongs to the verified actor in the request context, refusing another owner's account with NotAuthorized or NotFound, and MUST audit every ledger write with actor, source, request id, operation and the stored row. Each transaction row SHALL record who wrote it, when, and the request id.

#### Scenario: Other owner
- **WHEN** owner B calls any ledger verb, read, import, export or switch on owner A's account
- **THEN** the call is refused and nothing is returned or changed

#### Scenario: Audit
- **WHEN** an owner records and voids a trade
- **THEN** audit events `record_buy` and `void_transaction` exist for the account with the actor

<!-- tests: tests/test_ledger.py::test_only_the_owner_can_touch_an_account_and_every_write_is_audited, tests/test_ledger_coexistence.py::test_only_the_owner_can_switch_or_reconcile, tests/test_ledger_compliance_import.py::test_only_the_owner_can_import_or_preview, tests/test_ledger_page.py::test_every_page_function_refuses_another_owners_account -->

### Requirement: Accounts page ledger panel
The system SHALL present a ledger panel per account on the Accounts page: transactions (filters, voided and VOID rows struck through, void needing a reason), add forms, lots and gains, reconciliation, CSV import and export, activity and settings. Clearance badges MUST read Cleared, No approval, Not required or Unchecked. The panel MUST use only the service layer, never the database.

#### Scenario: Voided rows struck
- **WHEN** an account has a voided trade
- **THEN** both the voided row and its VOID row appear flagged as struck

#### Scenario: Snapshot account
- **WHEN** the panel shows a snapshot account
- **THEN** it offers the switch to ledger and no other write

<!-- tests: tests/test_ledger_page.py::test_transactions_view_keeps_voided_rows_struck_and_sorts_newest_first, tests/test_ledger_page.py::test_transaction_filters, tests/test_ledger_page.py::test_clearance_badges_say_what_they_mean, tests/test_ledger_page.py::test_the_panel_for_a_snapshot_account_offers_the_switch_and_nothing_else_writes, tests/test_ledger_page.py::test_the_accounts_panel_has_the_ledger_tile_and_the_panel_never_touches_the_database_directly -->
