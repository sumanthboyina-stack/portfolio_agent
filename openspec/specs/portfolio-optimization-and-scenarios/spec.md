# portfolio-optimization-and-scenarios Specification

## Purpose
Turns a cash contribution or a hand-picked set of hypothetical trades into a deterministic, explained scenario: which candidates would receive cash, what could be trimmed, and whether the resulting book respects the owner's limits. Research only: it never places or routes an order. Baseline at HEAD `ca450dd`.

## Requirements

### Requirement: Research only, no execution
The system SHALL treat every scenario and allocation as a hypothetical calculation. It MUST NOT place, route or transmit an order, and it MUST NOT connect to a broker.

#### Scenario: Scenario evaluated
- **WHEN** a scenario is evaluated, whether proposed by the system or selected by a person
- **THEN** the only outputs are an in-memory result and, for the legacy allocation entry point, a dictionary of recommendations
- **AND** no holding, cash balance or ledger row is written by the engine or the proposer

<!-- tests: NONE -->

### Requirement: Evaluation is pure and deterministic
Scenario evaluation SHALL read only its frozen inputs and the trades given, with no database, network or clock access. The same inputs and trades MUST give the same result and the same inputs digest.

#### Scenario: Repeat evaluation
- **WHEN** two identical input sets are evaluated with the same trades
- **THEN** both results are equal
- **AND** the two inputs digests are equal

#### Scenario: Pinned price changes
- **WHEN** one pinned price differs between two otherwise identical input sets
- **THEN** their digests differ

#### Scenario: Proposal without I/O
- **WHEN** trades are proposed from a prepared context and then evaluated
- **THEN** no database or network access is needed
- **AND** every proposed trade has source `proposed` and the funding account

<!-- tests: tests/test_scenario_engine.py::test_same_inputs_give_the_same_result_and_digest, tests/test_scenario_engine.py::test_propose_then_evaluate_without_io -->

### Requirement: No language model in scenarios
Allocation, trade proposal and scenario evaluation SHALL be computed by deterministic code with no language-model call.

#### Scenario: Allocation request
- **WHEN** an allocation or scenario is computed
- **THEN** every number, label and status comes from code over stored predictions and market data
- **AND** the stored reasoning text of a prediction is only quoted as the "why" of a candidate

<!-- tests: NONE -->

### Requirement: Decimal accounting identities
The engine SHALL use exact decimal arithmetic and keep these identities true to the cent for every account and for the whole scope: ending cash = starting cash + external contribution + sale proceeds − purchase cost − fees; ending portfolio value = starting portfolio value + external contribution − fees.

#### Scenario: Buys, sells and fees across two accounts
- **WHEN** a scenario with a per-trade fee buys in one account and sells in both
- **THEN** the reported total and per-account figures reconcile exactly
- **AND** the identity residuals for cash and portfolio value are both `0.00`

<!-- tests: tests/test_scenario_engine.py::test_accounting_identities_hold_to_the_cent_with_fees_and_both_sides -->

### Requirement: Buys are rounded down to whole shares
By default the engine SHALL round a buy down to whole shares before applying it, and MUST reject a buy that rounds to zero shares.

#### Scenario: Dollar buy below one share
- **WHEN** a buy by amount is smaller than one share's price
- **THEN** it is rejected with a reason that it rounds down to zero shares
- **AND** the result is infeasible when the trade was user-selected

#### Scenario: Fractional quantity
- **WHEN** a buy of 2.9 shares is evaluated
- **THEN** 2 shares are bought at the pinned price

<!-- tests: tests/test_scenario_engine.py::test_buys_round_down_to_whole_shares_and_zero_share_buys_are_rejected -->

### Requirement: Sells cannot exceed holdings
The engine SHALL reject a sell larger than the quantity held in that account, and a trade for an account outside the scenario's scope, each with an explanatory reason.

#### Scenario: Oversell
- **WHEN** a sell exceeds the shares held in the named account
- **THEN** it is rejected and the reason states the held quantity
- **AND** a position held only in another account counts as zero held

#### Scenario: Account out of scope
- **WHEN** a trade names an account not in the scenario
- **THEN** it is rejected as not in the scenario's scope

<!-- tests: tests/test_scenario_engine.py::test_overselling_and_wrong_account_are_infeasible_and_explained -->

### Requirement: A sell by amount larger than the position sells the position
When a sell is given as an amount that exceeds the value held, the engine SHALL sell the whole position instead of rejecting it.

#### Scenario: Sell more dollars than held
- **WHEN** a sell by amount converts to more shares than the account holds
- **THEN** the quantity is limited to the shares held

<!-- tests: NONE -->

### Requirement: A trade states exactly one of quantity or amount
The engine SHALL reject a trade that gives both or neither of quantity and amount, and a trade whose side is not BUY or SELL.

#### Scenario: Both given
- **WHEN** a trade has a quantity and an amount
- **THEN** it is rejected with a reason that exactly one is needed

#### Scenario: Unknown side
- **WHEN** a trade's side is neither BUY nor SELL
- **THEN** it is rejected as an unknown side

<!-- tests: NONE -->

### Requirement: Cash is per account and never negative
The engine SHALL track cash per account, never pool it across accounts, and fail the cash constraint for an account whose ending cash would be negative.

#### Scenario: Overdrawn account beside a rich one
- **WHEN** a buy in an account with little cash would overdraw it while another account has plenty
- **THEN** the cash constraint fails for the overdrawn account and passes for the other
- **AND** the result is infeasible

<!-- tests: tests/test_scenario_engine.py::test_cash_is_not_pooled_across_accounts -->

### Requirement: Unknown starting cash is never silently ignored
When an account's starting cash is unknown, the engine SHALL pass its cash constraint only if its contribution plus sale proceeds cover its purchases and fees, and otherwise report the constraint as not evaluable.

#### Scenario: Covered by new money
- **WHEN** starting cash is unknown and purchases fit within contribution plus proceeds
- **THEN** the cash constraint passes with a note that starting cash is unknown

#### Scenario: Not covered
- **WHEN** starting cash is unknown and purchases exceed contribution plus proceeds
- **THEN** the constraint is not evaluable and the result is `not_evaluable`
- **AND** the accounting records that starting cash is not known for all accounts

<!-- tests: tests/test_scenario_engine.py::test_unknown_starting_cash_is_not_silently_ignored -->

### Requirement: Per-trade cap on new money
The engine SHALL fail the per-trade constraint when a single buy in an account exceeds the owner's per-candidate share of that account's contribution.

#### Scenario: Buy above the cap
- **WHEN** an account has a contribution and one buy costs more than the limit fraction of it
- **THEN** the per-trade cap constraint fails and its detail states the limit and the contribution

<!-- tests: tests/test_scenario_engine.py::test_per_trade_cap_is_relative_to_the_accounts_contribution -->

### Requirement: Concentration limits are checked after rounding
The engine SHALL check position, sector and issuer limits on the post-rounding book at scope level. A group that received a buy and ends above its limit MUST fail; a group already above its limit and untouched MUST only produce a warning.

#### Scenario: Untouched breach
- **WHEN** a position is already above the position limit and no buy touches it
- **THEN** the result stays feasible and carries a warning that it was already above the limit before these trades

#### Scenario: Rounding keeps a buy under the limit
- **WHEN** a dollar buy would exceed the position limit unrounded but rounds down to whole shares below it
- **THEN** the position limit passes

#### Scenario: Rounded buy over the limit
- **WHEN** the rounded buy makes the position exceed the limit
- **THEN** the position limit fails for that ticker and the result is infeasible

<!-- tests: tests/test_scenario_engine.py::test_limits_are_checked_after_rounding_and_only_blame_groups_that_received_buys -->

### Requirement: Restricted tickers and excluded sectors
The engine SHALL reject any trade in a restricted ticker, and any buy (not sell) in an excluded sector, each with a stated reason.

#### Scenario: Restricted buy, excluded-sector buy, excluded-sector sell
- **WHEN** a restricted ticker is bought, an excluded-sector ticker is bought, and an excluded-sector holding is sold
- **THEN** the first two are rejected with reasons naming the restricted list and the sector
- **AND** the sale is accepted

<!-- tests: tests/test_scenario_engine.py::test_restricted_and_excluded_sector_buys_are_rejected_with_reasons -->

### Requirement: Missing inputs make a result not evaluable
The engine SHALL report `not_evaluable`, never a silent pass, when a required price or a bought ticker's sector is missing.

#### Scenario: No pinned price
- **WHEN** a trade names a ticker with no pinned price
- **THEN** the trade is rejected with kind `not_evaluable` and the result is `not_evaluable`

#### Scenario: Unknown sector
- **WHEN** a bought ticker has no known sector
- **THEN** the sector limit is not evaluable and names the ticker

#### Scenario: Unpriced holding
- **WHEN** a held position has no pinned price
- **THEN** the price-availability constraint is not evaluable

<!-- tests: tests/test_scenario_engine.py::test_missing_price_or_sector_makes_the_result_not_evaluable -->

### Requirement: Result status and trade source
The engine SHALL return `infeasible` when a user-selected trade is infeasible or any constraint fails, `not_evaluable` when only a required input is missing, and otherwise `feasible`. A rejected proposed trade MUST NOT change the status.

#### Scenario: Rejected proposed trade
- **WHEN** a proposed sell exceeds the holding and a proposed buy is valid
- **THEN** the result is feasible with one rejection and one accepted trade

#### Scenario: Rejected user trade
- **WHEN** the same oversell is user-selected
- **THEN** the result is infeasible

<!-- tests: tests/test_scenario_engine.py::test_proposed_rejections_do_not_flip_status_but_user_ones_do -->

### Requirement: Risk figures are labelled estimates
The scenario result SHALL include before and after position and sector concentration and an expected return that is labelled as an estimate with its horizon (21 days), model (`apex-latest-prediction`) and prediction version, plus the share of value that has an estimate.

#### Scenario: Buy changes the book
- **WHEN** a buy is evaluated against inputs that carry expected returns
- **THEN** the risk block labels the expected return as an estimate with horizon 21 days and the model name
- **AND** the after figures differ from the before figures

<!-- tests: tests/test_scenario_engine.py::test_risk_estimates_are_labelled_and_move_with_the_book -->

### Requirement: Limits come from the owner's own profile
Scenario preparation SHALL take its limits and excluded sectors from the profile of the owner it is prepared for, converting the stored percentage limits (sector, issuer) and fraction limits (per-candidate share of cash, post-trade position) to one common fraction form. With no owner it MUST use the local single-operator profile.

#### Scenario: Two owners with different limits
- **WHEN** a scenario is prepared for an owner whose own limits are wider than the local operator's
- **THEN** that owner's limits are used
- **AND** preparing with no owner uses the local operator's limits

#### Scenario: Mixed units
- **WHEN** a profile stores sector and issuer limits as percentages and the other two as fractions
- **THEN** all four limits are represented as fractions of one

<!-- tests: tests/test_prepare_inputs_owner_scoping.py::test_prepare_inputs_uses_the_scenarios_own_owner_not_local_owner, tests/test_scenario_engine.py::test_limits_from_profile_normalise_mixed_units -->

### Requirement: Restricted list applies only for a known owner
Scenario preparation SHALL exclude tickers on the preparing owner's own restricted list. When no owner is given it MUST treat no tickers as restricted, not check every owner's list.

#### Scenario: Owner given
- **WHEN** a scenario is prepared for an owner with restricted tickers
- **THEN** those tickers are in the scenario's restricted set

#### Scenario: No owner
- **WHEN** a scenario is prepared without an owner
- **THEN** the restricted set is empty

<!-- tests: NONE -->

### Requirement: Preparation pins inputs and versions
Preparation SHALL freeze positions, prices, cash, limits, sectors, issuers, expected returns and the versions of holdings, predictions, profile, prices and engine into the scenario inputs, so a later change cannot rewrite an earlier scenario.

#### Scenario: Prepare inputs
- **WHEN** inputs are prepared
- **THEN** they carry a `versions` map with holdings, predictions, profile, prices and engine entries
- **AND** a prepared-at timestamp that is excluded from the digest

<!-- tests: NONE -->

### Requirement: Scope needs at least one account
Preparation SHALL refuse a scenario whose scope contains no account.

#### Scenario: Empty scope
- **WHEN** preparation receives no holdings and a scope that names no account
- **THEN** it raises an error that a scenario needs at least one account in scope

<!-- tests: NONE -->

### Requirement: Starting cash and overrides
Preparation SHALL take each account's starting cash from its stored cash balance, allow a per-account override, and add a warning naming every account whose starting cash is unknown.

#### Scenario: No cash line
- **WHEN** an account has no stored cash balance and no override
- **THEN** its starting cash is unknown and a warning names the account

<!-- tests: NONE -->

### Requirement: Price pinning order
Preparation SHALL pin each ticker's price from the latest market close, falling back to the price carried by the last import, then to a candidate's price, and MUST warn about every fallback and every ticker with no price at all.

#### Scenario: No market close
- **WHEN** a held ticker has no market close but its holding row has a price
- **THEN** the holding row's price is pinned and a warning says the import price was used

<!-- tests: NONE -->

### Requirement: Sector resolution order
Preparation SHALL resolve a ticker's sector from its holding row first, then the cached universe data (ignoring "Unknown"), then an opportunity's own sector.

#### Scenario: Blank broker sector
- **WHEN** a holding row has no sector and the universe cache has one
- **THEN** the cached sector is used

<!-- tests: NONE -->

### Requirement: Funding account for a contribution
Preparation SHALL assign the contribution to the scoped account when the scope names one, otherwise to the funding account given, otherwise to the account with the most priced value, adding a warning when more than one account was in scope.

#### Scenario: All-accounts scope
- **WHEN** a contribution is prepared for several accounts with no funding account
- **THEN** it is assigned to the largest account and a warning names it

<!-- tests: NONE -->

### Requirement: Per-request callers can confine research to the owner's data
Preparation SHALL accept the owner's own holdings to judge candidates' portfolio fit and a set of prediction scopes to read expected returns from; when given, no other book or scope is read for those two inputs.

#### Scenario: Owner's book and shared scopes only
- **WHEN** a caller passes `opportunity_holdings` and `prediction_scopes=("shared_market",)`
- **THEN** opportunity ranking uses exactly those holdings and prediction reads use exactly those scopes

#### Scenario: Defaults
- **WHEN** neither is passed
- **THEN** the opportunity engine and prediction reader use their defaults, which are not confined to the owner

<!-- tests: tests/test_chat_engine_tools.py::test_prepare_inputs_passes_the_owners_book_and_shared_scopes_only_when_asked -->

### Requirement: Trim candidates
The proposer SHALL propose trims for holdings with a SELL (30% of value) or STRONG_SELL (60%) latest call, or whose sector or issuer share exceeds the owner's limit (trim up to 50%, the excess over the limit as a share of the position), and split a trim across accounts by shares held.

#### Scenario: SELL-rated holding
- **WHEN** a holding's latest call is SELL
- **THEN** a proposed SELL trade is created for it with the call's reasoning as the "why"

#### Scenario: Sector over the owner's limit
- **WHEN** a holding's sector share is above the limit
- **THEN** it is a trim candidate and the rationale names the percentage and threshold
- **AND** with a higher limit it is not a candidate

<!-- tests: tests/test_portfolio_optimizer.py::test_reduce_candidates_uses_thresholds, tests/test_scenario_engine.py::test_propose_then_evaluate_without_io -->

### Requirement: Buy candidates compete on one ranked list
The proposer SHALL rank held tickers whose latest call is classified BUY (and not already flagged for trimming) together with unheld opportunities classified BUY that have a price, by a score of composite score × 7 plus a diversification bonus minus an overweight penalty, and keep the top N.

#### Scenario: Held and new together
- **WHEN** one held BUY-rated ticker and one unheld BUY opportunity exist
- **THEN** both can appear in the proposed buys

#### Scenario: Overweight penalty
- **WHEN** a candidate's sector share is above the threshold
- **THEN** its score is reduced, and with a higher threshold it is not

<!-- tests: tests/test_portfolio_optimizer.py::test_overweight_penalty_defaults_match_module_constants, tests/test_portfolio_optimizer.py::test_overweight_penalty_respects_custom_thresholds, tests/test_scenario_engine.py::test_propose_then_evaluate_without_io -->

### Requirement: Score floor for new cash
Only candidates with a score of at least 45 SHALL receive cash.

#### Scenario: Weak candidate
- **WHEN** a candidate's score is below 45
- **THEN** it receives no allocation and its share stays in cash

<!-- tests: NONE -->

### Requirement: Cash allocation proportional to score with caps
The allocator SHALL share the contribution among qualifying candidates in proportion to score, cap each at the per-candidate fraction of the contribution (default 40%) and at the post-trade position fraction (default 15% of post-trade value), round dollars to the nearest 50, and leave capped amounts as cash rather than redistributing them.

#### Scenario: Per-candidate cap
- **WHEN** a candidate's proportional share exceeds 40% of the cash
- **THEN** its allocation is capped at 40% of the cash

#### Scenario: Custom caps
- **WHEN** a lower per-candidate cap or post-trade position cap is passed
- **THEN** every allocation respects it and total allocated cash falls accordingly

<!-- tests: tests/test_portfolio_optimizer.py::test_allocate_cash_defaults_match_module_constants, tests/test_portfolio_optimizer.py::test_allocate_cash_respects_custom_per_candidate_cap, tests/test_portfolio_optimizer.py::test_allocate_cash_respects_custom_post_trade_position_cap -->

### Requirement: Allocation entry point returns evaluated trades
The allocation entry point SHALL prepare inputs, propose trades and evaluate them, and return allocation and reduce lists built from the evaluated (rounded) trades, with cash reserved as the contribution minus allocated amounts, plus the full scenario and its pinned inputs.

#### Scenario: Request with a contribution
- **WHEN** an allocation is requested for a contribution
- **THEN** each allocation line carries ticker, kind (existing or new), amount, whole shares and the "why"
- **AND** the response includes `cash_reserved`, `reduce`, `impact`, `scenario` and `inputs`

<!-- tests: NONE -->

### Requirement: Risk impact is a best-effort market estimate
The allocation response SHALL include a before and after risk projection from market data; if the after projection cannot be computed it MUST be null without failing the allocation.

#### Scenario: After projection fails
- **WHEN** computing the after metrics raises an error
- **THEN** `impact.after` is null and the rest of the response is returned

<!-- tests: NONE -->

### Requirement: Holdings reads for tools are scoped to the calling owner
When an owner is known, the portfolio holdings and concentration readers SHALL return only that owner's holdings, and an owner with no portfolio MUST see none rather than anyone else's.

#### Scenario: Two owners
- **WHEN** two owners each hold different tickers and each calls the holdings reader as themselves
- **THEN** each sees only their own tickers

#### Scenario: New owner
- **WHEN** a signed-in owner has no portfolio yet
- **THEN** the reader returns an empty list

#### Scenario: Concentration reader
- **WHEN** the concentration reader runs for an owner
- **THEN** it uses only that owner's holdings

<!-- tests: tests/test_portfolio_tools_owner_scoping.py::test_get_portfolio_holdings_scopes_to_the_chat_owner_not_the_combined_book, tests/test_portfolio_tools_owner_scoping.py::test_a_logged_in_owner_with_no_portfolio_yet_sees_empty_not_everyone_elses, tests/test_portfolio_tools_owner_scoping.py::test_get_portfolio_concentration_is_scoped_the_same_way -->

### Requirement: Restricted-list check is the caller's own
The restricted-list check tool SHALL consult only the calling owner's restricted list and, with no owner, report the result as unknown (null) with a reason.

#### Scenario: No owner
- **WHEN** the check runs with no owner in its context
- **THEN** `is_restricted` is null and the reason says there is no owner to check against

<!-- tests: NONE -->

### Requirement: Concentration flag
The concentration tool SHALL flag a ticker whose weight in the owner's holdings is above 10% of portfolio value.

#### Scenario: Over 10%
- **WHEN** a ticker is more than 10% of the priced holdings
- **THEN** `concentration_flag` is true

<!-- tests: NONE -->
