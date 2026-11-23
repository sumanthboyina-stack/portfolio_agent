## Purpose

A private, deterministic companion to the shared market forecast for tickers a person holds: the forecast labels beside the person's own position, cost, limits and action permission. It is information, not advice, and nothing in it flows back into any forecast.

## ADDED Requirements

### Requirement: Position view for a held ticker
The system SHALL build, for a ticker the owner holds in a real account, a view with the latest shared forecast label, direction, horizon and age for each of 5, 21 and 63 days; the owner's shares, value, weight, cost basis, unrealized gain and holding period; their concentration limits; their action permission; a relation; and a one-line summary. It MUST return nothing for a ticker the owner does not hold or an owner with no portfolio.

#### Scenario: Held ticker
- **WHEN** owner A holds 50 shares of TICKER bought at an average cost of $X and shared forecasts exist for 5 and 21 days
- **THEN** the view lists both forecast labels with their ages, 50 shares, the weight, the cost basis and the unrealized gain percentage, and the limit statuses

#### Scenario: Not held
- **WHEN** the owner does not hold the ticker, or has no portfolio
- **THEN** no view is returned

<!-- tests: tests/test_position_view.py::test_the_view_carries_position_facts_limits_and_the_shared_forecast, tests/test_position_view.py::test_no_view_for_a_ticker_that_is_not_held_or_an_owner_with_no_portfolio -->

### Requirement: The forecast side reads only shared forecasts
The view SHALL read forecasts from shared, non-challenger prediction rows only; private and challenger rows MUST NOT appear, and the view MUST NOT feed any forecast, prompt or shared table.

#### Scenario: Private and challenger rows ignored
- **WHEN** newer private or challenger forecasts exist for the ticker
- **THEN** the view lists only the shared non-challenger labels

<!-- tests: tests/test_position_view.py::test_only_shared_non_challenger_forecasts_are_read -->

### Requirement: Relation between forecast direction and limits
The view SHALL set `relation` only from the forecast direction (positive for STRONG_BUY or BUY, neutral for HOLD, negative for SELL or STRONG_SELL, mixed when horizons differ, none when no forecast) and the limit status (over when the position, sector or issuer concentration limit is exceeded, within when all are within, unknown otherwise).

#### Scenario: Negative forecast and an exceeded limit
- **WHEN** the shared forecasts are SELL and STRONG_SELL and the position exceeds the owner's position limit
- **THEN** the relation is `forecast_negative_over_limit`

#### Scenario: Relation values
- **WHEN** a relation is set
- **THEN** it is one of `no_forecast`, `forecast_mixed`, `forecast_neutral`, `fit_unknown`, `forecast_negative_over_limit`, `forecast_positive_over_limit`, `forecast_negative_within_limits` or `forecast_positive_within_limits`

#### Scenario: No forecast or mixed horizons
- **WHEN** no shared forecast exists, or one horizon is positive and another negative
- **THEN** the relation is `no_forecast` or `forecast_mixed` respectively

<!-- tests: tests/test_position_view.py::test_the_view_carries_position_facts_limits_and_the_shared_forecast, tests/test_position_view.py::test_relation_without_a_forecast_or_with_mixed_horizons -->

### Requirement: Cost basis and holding period are facts, not inputs
The view SHALL report cost basis, unrealized gain and the short or long holding period (long means more than 365 days, from open lots of ledger-managed accounts only) as facts, and MUST NOT use them to set the relation. An unknown cost basis MUST be reported as unknown, never as zero, and the summary MUST NOT tell the owner to buy, sell, increase or reduce.

#### Scenario: Cost unknown
- **WHEN** a position has no average cost or cost basis
- **THEN** cost basis and unrealized gain are unknown and the summary says the cost basis is not known

#### Scenario: Snapshot account
- **WHEN** the ticker is held only in a snapshot-imported account
- **THEN** the holding period is not available

<!-- tests: tests/test_position_view.py::test_cost_basis_unknown_is_reported_as_unknown_not_zero, tests/test_position_view.py::test_the_view_carries_position_facts_limits_and_the_shared_forecast -->

### Requirement: Owner scoping of the view
The view and its stored rows SHALL be computed from and read for one owner only, and the checked entry points MUST refuse an actor with no portfolio.

#### Scenario: Two owners
- **WHEN** owners A and B hold different amounts of the same ticker
- **THEN** each view shows only that owner's own position

#### Scenario: Stranger
- **WHEN** an actor with no portfolio asks for a view or a stored view
- **THEN** the request is refused as not authorized

<!-- tests: tests/test_position_view.py::test_each_owner_sees_only_their_own_position, tests/test_position_view.py::test_the_checked_entry_point_refuses_a_stranger, tests/test_position_view.py::test_a_stored_view_is_readable_only_by_its_owner -->

### Requirement: Daily stored position views
The system SHALL store, once per day per owner and held ticker, the owner's position view in a private table, refresh a row on a same-day re-run without rewriting earlier days, and write one audit event `save_position_views` per owner per day. A failure for one ticker or owner MUST NOT stop the others.

#### Scenario: Batch run and re-run
- **WHEN** the batch runs for two owners and then runs again the same day
- **THEN** each held ticker has one row per owner, the re-run updates them, and each owner has exactly one audit event

<!-- tests: tests/test_position_view.py::test_the_batch_stores_each_owners_views_with_one_audit_event_and_refreshes_on_rerun -->

### Requirement: Chat shows the position view
After a chat prediction for a ticker the signed-in owner holds, and when such a card is replayed, the chat SHALL show the live position view with shares, weight, cost basis and unrealized gain, a warning naming any exceeded limit, and a caption saying it is information only, not investment advice, and that the forecast never sees the holdings. For a ticker not held it MUST show nothing.

#### Scenario: Held ticker with an exceeded limit
- **WHEN** a prediction is shown for a held ticker whose weight exceeds the owner's position limit
- **THEN** the card shows the summary, the four figures, a warning naming the position concentration limit, and the caption

#### Scenario: Not held
- **WHEN** a prediction is shown for a ticker the owner does not hold
- **THEN** no position view is shown

<!-- tests: tests/test_position_view.py::test_the_card_shows_the_position_next_to_the_forecast_and_flags_an_exceeded_limit, tests/test_position_view.py::test_the_card_shows_nothing_for_a_ticker_not_held -->

### Requirement: Chat tool for the position view
The chat SHALL offer a read-only tool `get_position_view` that returns the signed-in owner's view of a ticker and the previous stored view's date, relation and forecast labels, says so when the ticker is not held, and refuses a context that is not the owner's. Owner and portfolio identifiers supplied by the model MUST be ignored.

#### Scenario: Held ticker with a stored earlier view
- **WHEN** the owner asks about a held ticker and yesterday's view is stored
- **THEN** the tool returns today's view and the previous stored view

#### Scenario: Not held, other actor, injected owner
- **WHEN** the ticker is not held, the context is not the owner's, or the model supplies another owner
- **THEN** the tool says the ticker is not held, refuses, or uses the server-side owner respectively

<!-- tests: tests/test_position_view.py::test_the_chat_tool_returns_the_owners_view_and_the_previous_stored_one, tests/test_position_view.py::test_the_chat_tool_says_so_for_a_ticker_not_held_and_refuses_another_actor, tests/test_position_view.py::test_the_model_cannot_pick_another_owner_through_arguments -->
