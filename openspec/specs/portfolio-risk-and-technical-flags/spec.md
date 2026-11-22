# portfolio-risk-and-technical-flags Specification

## Purpose
Produces the daily Risk and Technical flags for held tickers and the portfolio-level risk math behind them. Risk flags are private to each owner. Technical flags are shared market data and use no language model. A held ticker's risk check re-runs only when something changed, otherwise weekly. Baseline at HEAD `ca450dd`.

## Requirements

### Requirement: Risk flags are per owner and technical flags are shared
The system SHALL store one flag row per ticker, date, source (`risk` or `technical`) and owner. A `risk` row MUST belong to a real owner. A `technical` row MUST be written once per ticker and date under the shared owner sentinel, never per owner.

#### Scenario: Two owners hold the same ticker
- **WHEN** owners A and B both hold TICKER and a risk check is stored for A
- **THEN** B still needs its own risk row for TICKER on that date
- **AND** one shared technical row serves both owners

#### Scenario: Risk row without an owner
- **WHEN** a `risk` row is written with the shared owner sentinel or no owner
- **THEN** the write is refused with an error and nothing is stored

<!-- tests: tests/test_execution_trace_characterization.py::test_risk_flag_upsert_is_idempotent_and_updates_in_place, tests/test_execution_trace_characterization.py::test_upsert_risk_flag_rejects_a_risk_row_without_a_real_owner -->

### Requirement: Flag reads never expose another owner's risk row
Reading flags for an owner SHALL return that owner's own `risk` rows plus the shared `technical` rows, and MUST NOT return any other owner's `risk` row.

#### Scenario: Another owner asks for the same ticker
- **WHEN** owner A has a stored risk row for TICKER and owner B reads flags for TICKER on the same date
- **THEN** B receives only the shared technical row

<!-- tests: tests/test_execution_trace_characterization.py::test_risk_flag_upsert_is_idempotent_and_updates_in_place -->

### Requirement: Flag rows are idempotent per key
Writing a flag for an existing (ticker, date, source, owner) key SHALL update that row in place instead of adding a second row.

#### Scenario: Re-run on the same day
- **WHEN** a risk row for owner A and TICKER on a date is written again with a different score
- **THEN** exactly one row remains for that key and it holds the new score and flag

<!-- tests: tests/test_execution_trace_characterization.py::test_risk_flag_upsert_is_idempotent_and_updates_in_place -->

### Requirement: Active flags are read per owner from the latest date
Reading active flags for an owner SHALL return only rows with `flag = 1` for the requested date, or for the most recent date present when none is given, covering the owner's `risk` rows and all shared `technical` rows.

#### Scenario: Latest date by default
- **WHEN** active flags are read for owner A with no date
- **THEN** rows from the newest `as_of_date` in the table are considered
- **AND** unflagged rows and other owners' risk rows are excluded

<!-- tests: NONE -->

### Requirement: Legacy flag rows are migrated once to the owner-keyed table
When the flag table has no owner column, the system SHALL rebuild it once with the owner in its unique key. Existing `technical` rows MUST become shared and existing `risk` rows MUST be assigned to the local owner. A table that already has the column MUST be left unchanged.

#### Scenario: Old table upgraded
- **WHEN** the table predates the owner column and holds one risk row and one technical row
- **THEN** the risk row belongs to the local owner and the technical row is shared

<!-- tests: NONE -->

### Requirement: Risk checks cover enabled owners' holdings only
The daily risk pass SHALL consider each enabled login user's own portfolio and only that owner's held tickers. Owners with no holdings and users that are disabled MUST be skipped.

#### Scenario: Disabled user
- **WHEN** a user is disabled and still has a portfolio with holdings
- **THEN** no risk check is run for that owner

<!-- tests: tests/test_shared_market_snapshots.py::test_risk_technical_pipeline_technical_is_rule_based_and_survives_feature_failure -->

### Requirement: A risk check already stored today is not repeated
The system SHALL return no refresh reason for an owner and ticker whose risk row is dated today, whatever else has changed. When no risk row exists at all, the reason MUST be `no_risk_flag`.

#### Scenario: First ever check
- **WHEN** an owner has never had a risk row for TICKER
- **THEN** the refresh reason is `no_risk_flag`

#### Scenario: Done today
- **WHEN** a risk row dated today exists and the day's price move is 9%
- **THEN** there is no refresh reason

#### Scenario: Another owner's row does not count
- **WHEN** owner A has today's row for TICKER and owner B has none
- **THEN** owner B's reason is `no_risk_flag`

<!-- tests: tests/test_risk_cadence.py::test_never_run_then_skip_when_nothing_changed, tests/test_risk_cadence.py::test_already_done_today_and_owner_isolation -->

### Requirement: Risk checks fall back to a weekly cadence
When nothing else triggers, the system SHALL re-run an owner's risk check once the last row is 7 or more days old, with reason `weekly_<N>d`, and MUST skip it while the last row is younger and nothing changed.

#### Scenario: Seven days old
- **WHEN** the last risk row is 7 days old and nothing changed
- **THEN** the reason starts with `weekly_`

#### Scenario: Two days old, no change
- **WHEN** the last risk row is 2 days old, the holdings fingerprint matches, the price move is 1% and no event or filing is new
- **THEN** there is no refresh reason

<!-- tests: tests/test_risk_cadence.py::test_weekly_fallback, tests/test_risk_cadence.py::test_never_run_then_skip_when_nothing_changed -->

### Requirement: A change in the owner's holdings triggers a risk re-check
The system SHALL re-run an owner's risk check with reason `holdings_changed` when the stored holdings fingerprint differs from the current one. A stored row with no fingerprint MUST NOT trigger this.

#### Scenario: Shares changed
- **WHEN** the owner's current holdings fingerprint differs from the one stored on the last risk row
- **THEN** the reason is `holdings_changed`

#### Scenario: Legacy row without fingerprint
- **WHEN** the last risk row stores no fingerprint
- **THEN** the holdings comparison does not trigger a re-check

<!-- tests: tests/test_risk_cadence.py::test_each_trigger_fires -->

### Requirement: The holdings fingerprint reflects what is held, not its value
The holdings fingerprint SHALL be derived from each held ticker and its share count rounded to 4 decimals, independent of order, and MUST NOT include prices or values.

#### Scenario: Price drift only
- **WHEN** only prices change between two runs
- **THEN** the fingerprint is identical and no `holdings_changed` reason arises

<!-- tests: NONE -->

### Requirement: A large one-day price move triggers a risk re-check
The system SHALL re-run an owner's risk check with reason `price_move_<X>%` when the absolute one-day price move is 5.0 percent or more. A ticker with fewer than 3 closes MUST NOT trigger.

#### Scenario: Six percent drop
- **WHEN** the one-day move is -6.2% and the last row is 2 days old
- **THEN** the reason starts with `price_move`

<!-- tests: tests/test_risk_cadence.py::test_each_trigger_fires -->

### Requirement: A severity-3 event triggers a risk re-check
The system SHALL re-run an owner's risk check with reason `event_<type>` when a trigger event of severity 3 or higher for that ticker was detected after the last risk row was stored. The event lookup MUST NOT depend on the owner.

#### Scenario: 8-K event
- **WHEN** a severity-3 `sec_8k` event for TICKER was detected after the last risk row
- **THEN** the reason is `event_sec_8k`

<!-- tests: tests/test_risk_cadence.py::test_each_trigger_fires -->

### Requirement: A new filing triggers a risk re-check
The system SHALL re-run an owner's risk check with reason `new_filing_<date>` when the newest stored filing date for the ticker is later than the date of the last risk row.

#### Scenario: Filing after last run
- **WHEN** the newest filing date is later than the last row's date
- **THEN** the reason starts with `new_filing_`

<!-- tests: tests/test_risk_cadence.py::test_each_trigger_fires -->

### Requirement: Refresh triggers are evaluated in a fixed order
The system SHALL decide the refresh reason in this order and stop at the first match: computed today, never computed, weekly age, holdings changed, price move, event, new filing.

#### Scenario: Weekly beats the others
- **WHEN** the last row is 8 days old and the price also moved 9%
- **THEN** the reason is `weekly_8d`

<!-- tests: NONE -->

### Requirement: Risk checks are sent to the model in batches of 10 per owner
The system SHALL send due tickers to the risk model in batches of at most 10 tickers per call, grouped by owner so no batch mixes owners.

#### Scenario: Twenty-three due tickers
- **WHEN** one owner has 23 tickers due
- **THEN** three calls are made with 10, 10 and 3 tickers

<!-- tests: tests/test_risk_cadence.py::test_batches_of_ten -->

### Requirement: Risk numbers are computed without a model and win over model output
The system SHALL compute each holding's portfolio weight, sector exposure, daily volatility, correlation with the portfolio, most-correlated peer, sector and issuer concentration and beta without a model. These values MUST be stored as computed and MUST override any value the model returns, which supplies only the score, flags and narrative.

#### Scenario: Model restates a number
- **WHEN** the model returns a different `sector_concentration_pct` than the computed one
- **THEN** the stored row holds the computed value

<!-- tests: NONE -->

### Requirement: Risk flag raised by thresholds
A stored risk row SHALL have `flag = 1` when the model sets `concentration_flag`, the risk score is 8 or higher, sector concentration exceeds 25%, issuer concentration exceeds 15%, or the absolute beta versus SPY exceeds 1.5.

#### Scenario: Issuer concentration
- **WHEN** a holding's issuer concentration is 16% and no other condition holds
- **THEN** the row is flagged

#### Scenario: Within limits
- **WHEN** the score is 3, sector concentration is 20%, issuer concentration is 10% and beta is 1.0
- **THEN** the row is not flagged

<!-- tests: NONE -->

### Requirement: Risk failures write no row
When a risk batch fails, or the model returns no entry for a ticker, the system SHALL write no risk row for that ticker, count it as failed and continue with the other batches and owners. It MUST NOT store a placeholder score.

#### Scenario: Model returns nothing for one ticker
- **WHEN** a batch returns analyses for 9 of its 10 tickers
- **THEN** 9 rows are stored and the tenth is counted failed with no row

<!-- tests: NONE -->

### Requirement: Risk batches use the flash failover chain
The risk model call SHALL walk the `flash` failover chain in order. A failover-class error or an unparseable answer MUST advance to the next model, and the chain position carries over to later batches in the same run. Exhausting the chain fails the batch.

#### Scenario: First model returns no JSON
- **WHEN** the current model returns text with no JSON array
- **THEN** the next model in the chain is tried

<!-- tests: NONE -->

### Requirement: Technical flags are computed once per ticker per day without a model
The system SHALL compute a ticker's technical flag only when no shared technical row exists for it today, once per ticker regardless of how many owners hold it, and MUST use no language model.

#### Scenario: Already flagged today
- **WHEN** a shared technical row for TICKER dated today exists
- **THEN** no technical computation runs for TICKER

#### Scenario: Missing row
- **WHEN** no shared technical row exists for TICKER today
- **THEN** a technical row is computed from stored technical features and saved under the shared owner

<!-- tests: tests/test_execution_trace_characterization.py::test_risk_flag_upsert_is_idempotent_and_updates_in_place, tests/test_shared_market_snapshots.py::test_risk_technical_pipeline_technical_is_rule_based_and_survives_feature_failure -->

### Requirement: Technical trend, momentum and score follow fixed rules
The technical row SHALL classify trend as `UPTREND` when price is above the medium average and the medium above the slow, `DOWNTREND` when below both in that order, else `SIDEWAYS`. Momentum SHALL come from votes on MACD histogram, RSI versus 50 and price versus the 20-day average. The score SHALL be bounded to 1 to 10.

#### Scenario: Clear uptrend
- **WHEN** price 120, SMA20 115, SMA50 110, SMA200 100, RSI 60 and MACD histogram 1.2
- **THEN** trend is `UPTREND`, momentum `BULLISH`, MACD signal `BULLISH` and the score is 9

#### Scenario: Clear downtrend
- **WHEN** price 80, SMA20 90, SMA50 95, SMA200 100, RSI 35 and MACD histogram -1.0
- **THEN** trend is `DOWNTREND`, momentum `BEARISH` and the score is 3 or lower

#### Scenario: Overbought penalty
- **WHEN** RSI is above 70
- **THEN** the score is reduced by 1 before bounding

<!-- tests: tests/test_shared_market_snapshots.py::test_rule_based_technical_trend_momentum_and_flag -->

### Requirement: Technical flag is raised for bearish momentum or a low score
A stored technical row SHALL have `flag = 1` when momentum is `BEARISH` or the technical score is 3 or lower.

#### Scenario: Strong setup
- **WHEN** the score is 9 and momentum is `BULLISH`
- **THEN** the row is not flagged

#### Scenario: Weak setup
- **WHEN** momentum is `BEARISH`
- **THEN** the row is flagged

<!-- tests: tests/test_shared_market_snapshots.py::test_risk_technical_pipeline_technical_is_rule_based_and_survives_feature_failure, tests/test_shared_market_snapshots.py::test_rule_based_technical_trend_momentum_and_flag -->

### Requirement: Technical rows with too little data are failures, not guesses
When no usable price and average data exist, or the feature fetch raises, the system SHALL store no technical row, count the ticker as failed and leave the risk pass unaffected. Missing 200-day data MUST leave that comparison empty and still allow a row.

#### Scenario: Feature fetch fails
- **WHEN** fetching technical features raises an error for TICKER
- **THEN** no technical row is stored and the TICKER risk row is still stored

#### Scenario: Thin history
- **WHEN** the 200-day average is unavailable but SMA20 and SMA50 exist
- **THEN** a row is stored with price versus 200-day empty

<!-- tests: tests/test_shared_market_snapshots.py::test_risk_technical_pipeline_technical_is_rule_based_and_survives_feature_failure, tests/test_shared_market_snapshots.py::test_rule_based_technical_trend_momentum_and_flag -->

### Requirement: Technical rows leave option and level fields empty
A rule-based technical row SHALL record `implied_volatility`, `put_call_ratio`, `support_level` and `resistance_level` as empty, state its source as rules and record the model label `rules/technical-features`.

#### Scenario: Stored row
- **WHEN** a technical row is stored for TICKER
- **THEN** the four option and level fields are empty and the model label is `rules/technical-features`

<!-- tests: NONE -->

### Requirement: Technical features are computed purely and mark missing data UNKNOWN
The system SHALL compute SMA 20/50/200, RSI 14, MACD line, signal and histogram, Bollinger bands, ATR 14 and price versus SMA50 from price series with no network call and no database access. An indicator without enough bars MUST be `UNKNOWN`, never omitted, and a coverage map MUST record which indicators had enough bars.

#### Scenario: Thirty bars
- **WHEN** only 30 daily bars are available
- **THEN** SMA20 has a value, SMA200 is `UNKNOWN`, coverage for SMA200 is false and `bars_available` is 30

#### Scenario: Agreement with the legacy indicator tool
- **WHEN** the same 250 bars are given to the pure computation and to the legacy technical indicator tool
- **THEN** every shared indicator agrees within 0.0001

<!-- tests: tests/test_shared_market_snapshots.py::test_technical_features_records_coverage_and_unknown_for_thin_history, tests/test_shared_market_snapshots.py::test_technical_features_pure_compute_makes_zero_network_calls, tests/test_shared_market_snapshots.py::test_technical_features_matches_get_technical_indicators_on_identical_input -->

### Requirement: Technical features are stored and reused per data snapshot
The system SHALL store technical features keyed by ticker, data snapshot (the last trading date present in the series), interval, lookback, adjustment method and calculation version, currently `technical-features/1.0`. A stored row for the same key MUST be reused without recomputation, and an empty history MUST yield no row.

#### Scenario: Two callers, one ticker
- **WHEN** two callers request TICKER features within the history cache window
- **THEN** the price history is fetched once and both receive identical features

#### Scenario: No history
- **WHEN** the price history for a ticker is empty
- **THEN** nothing is stored and no result is returned

<!-- tests: tests/test_shared_market_snapshots.py::test_two_overlapping_requests_for_the_same_ticker_reuse_the_stored_result -->

### Requirement: Technical features hold market data only
Technical features and technical flags SHALL be derived from a ticker and public price history only. No holdings, portfolio or identity data MUST reach the computation or its storage, and the stored features MUST carry no owner.

#### Scenario: Shared across owners
- **WHEN** two owners hold TICKER
- **THEN** both read the same stored features and technical row

<!-- tests: NONE -->

### Requirement: Portfolio risk context per holding
For an owner's holdings with at least 2 tickers having 6 months of daily prices and at least 20 aligned return rows, the system SHALL compute per ticker the value-weighted correlation with the other holdings, the most-correlated peer, sector concentration, beta versus SPY and issuer concentration with its peer tickers. Otherwise it MUST return an empty result.

#### Scenario: Single holding
- **WHEN** an owner holds one ticker
- **THEN** the risk context is empty

#### Scenario: Three technology holdings
- **WHEN** three held tickers are all in the same sector
- **THEN** each has sector concentration 100.0 and a correlation between -1 and 1

<!-- tests: tests/test_shared_market_snapshots.py::test_portfolio_risk_refactor_matches_pre_refactor_golden_values -->

### Requirement: Implausible betas are dropped and issuers are grouped by SEC identity
Betas with an absolute value above 5 SHALL be omitted. Tickers that map to the same SEC-registered issuer MUST be grouped for issuer concentration, and a ticker with no mapping is its own issuer. A holding's sector comes from its own record first, else the cached universe sector, else `Unknown`.

#### Scenario: Two share classes of one issuer
- **WHEN** two held tickers share one SEC identity
- **THEN** each reports the combined weight as issuer concentration and lists the other as an issuer peer

<!-- tests: NONE -->

### Requirement: Portfolio risk math makes no network call
The numerical core of the risk context, the aggregate metrics and the hypothetical-addition impact SHALL operate only on supplied price series and MUST NOT call the network.

#### Scenario: Network blocked
- **WHEN** price downloads are made to raise an error and price series are supplied directly
- **THEN** the risk context and aggregate metrics are still computed

<!-- tests: tests/test_shared_market_snapshots.py::test_portfolio_risk_core_functions_make_zero_network_calls -->

### Requirement: Overlapping price requests share one fetch
Each ticker's raw close series SHALL be fetched at most once per cache window across overlapping requests, including between the risk context and the aggregate metrics for the same holdings, and the series MUST be reused as the same object.

#### Scenario: Overlapping tickers
- **WHEN** a request for AAA and BBB is followed by one for AAA, BBB and CCC
- **THEN** only CCC is fetched the second time

<!-- tests: tests/test_shared_market_snapshots.py::test_overlapping_ticker_requests_fetch_each_ticker_at_most_once, tests/test_shared_market_snapshots.py::test_portfolio_risk_functions_share_one_fetch_across_overlapping_calls -->

### Requirement: Aggregate portfolio metrics
The system SHALL compute, for holdings with at least 2 priced tickers, total value, weighted beta, top sector and its percentage, annualized portfolio volatility, expected 21-day return and value-weighted average pairwise correlation, and MUST return nothing when fewer than 2 tickers have price history. Expected return SHALL use the latest stored prediction per ticker.

#### Scenario: One priced ticker
- **WHEN** fewer than 2 held tickers have usable prices
- **THEN** no aggregate result is returned

#### Scenario: All holdings in one sector
- **WHEN** all holdings are in one sector
- **THEN** `top_sector_pct` is 100.0

<!-- tests: tests/test_shared_market_snapshots.py::test_portfolio_risk_refactor_matches_pre_refactor_golden_values -->

### Requirement: Hypothetical addition excludes the ticker from its own comparison
When the system estimates the effect of adding a ticker, it SHALL exclude that ticker from the existing holdings, so scoring a held ticker gives the same result whether or not the list includes it, and its correlation with the portfolio is not 1.0 by construction.

#### Scenario: Ticker already held
- **WHEN** the same ticker is scored against a list that includes it and a list that does not
- **THEN** both give the same correlation with the portfolio

<!-- tests: tests/test_portfolio_risk.py::test_compute_hypothetical_addition_impact_excludes_ticker_from_own_holdings, tests/test_portfolio_risk.py::test_compute_hypothetical_addition_impact_same_result_whether_or_not_ticker_pre_included -->

### Requirement: Portfolio fit score
The system SHALL produce a fit score from 0 to 100 for a ticker against holdings, made of up to 40 points for low correlation (none at 0.6 or above), up to 30 for relief of the top sector's concentration, and up to 30 for risk-adjusted return against the weakest holding (15 when unknown). It MUST return nothing when the impact cannot be computed.

#### Scenario: Held and unheld ticker
- **WHEN** a held and an unheld ticker are scored against three holdings
- **THEN** both scores are between 0 and 100

#### Scenario: Single holding
- **WHEN** the holdings list has only the scored ticker
- **THEN** no score is returned

<!-- tests: tests/test_portfolio_risk.py::test_compute_portfolio_fit_score_in_range_for_held_and_unheld_tickers, tests/test_portfolio_risk.py::test_compute_portfolio_fit_score_none_with_insufficient_holdings -->

### Requirement: Flags never enter predictions
Risk and technical flags SHALL be stored in their own table, not in prediction rows, and MUST NOT change the weight engine or composite scores. Per-owner risk results MUST NOT be used in shared market forecasts.

#### Scenario: Flag run
- **WHEN** the risk and technical phase completes
- **THEN** only flag rows are written and no prediction row is created or changed

<!-- tests: NONE -->

### Requirement: The risk and technical phase is optional and quiet when idle
The system SHALL skip the phase with no model call when there are no portfolio tickers or nothing is due, and a failure in the phase MUST be logged as a warning without stopping the rest of the morning batch.

#### Scenario: Nothing due
- **WHEN** every holding's risk check is current and today's technical rows exist
- **THEN** the phase finishes with zero work and no model call

#### Scenario: Phase raises
- **WHEN** the phase raises an error during the morning batch
- **THEN** the batch logs a warning and continues

<!-- tests: NONE -->

### Requirement: Active risk flags feed each owner's attention queue
The attention queue SHALL list only `risk` active flags read for the signed-in owner, for tickers that owner holds, with the score in the title. The portfolio card SHALL show that owner's active flags, risk and technical, for held tickers only.

#### Scenario: Flag on an unheld ticker
- **WHEN** an active flag exists for a ticker the owner does not hold
- **THEN** it appears neither in that owner's queue nor on their portfolio card

#### Scenario: Technical flag on a held ticker
- **WHEN** a shared technical flag is active for a held ticker
- **THEN** it shows on the portfolio card and not in the attention queue

<!-- tests: NONE -->
