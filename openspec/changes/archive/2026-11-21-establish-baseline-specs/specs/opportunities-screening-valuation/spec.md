## Purpose

Finds and ranks new research candidates and values companies, all with deterministic math and no LLM. It covers the tiered ticker universe, the quant screener and its conviction ranking, the manual watchlist, the Opportunity Engine that ranks APEX-analyzed candidates, and the DCF and relative valuation. Output is research only; nothing here places or routes an order. Baseline at HEAD `ca450dd`.

## ADDED Requirements

### Requirement: Universe tiers
The system SHALL keep a ticker universe in two tiers: `extended` (S&P 500, S&P 400 and S&P 600 members) and `broad` (Nasdaq-listed tickers that are not in the S&P 1500). An S&P member that also appears in the Nasdaq list MUST be stored in the `extended` tier.

#### Scenario: S&P member also listed on Nasdaq
- **WHEN** a ticker is in an S&P index and in the Nasdaq listing
- **THEN** it is stored in the `extended` tier

#### Scenario: Ticker outside the S&P indexes
- **WHEN** a Nasdaq-listed ticker is in none of the three S&P indexes
- **THEN** it is stored in the `broad` tier

<!-- tests: NONE -->

### Requirement: Universe ticker filtering
When reading the Nasdaq listing, the system SHALL replace dots with hyphens, upper-case the symbol, and skip a symbol that is empty, longer than 6 characters, or not 1 to 5 letters once hyphens are removed. A failed or empty fetch MUST yield no rows and no error.

#### Scenario: Warrant or preferred symbol
- **WHEN** a listed symbol contains a character other than letters and a hyphen
- **THEN** it is not added to the universe

#### Scenario: Fetch failure
- **WHEN** the listing request fails
- **THEN** zero rows are returned and the failure is logged as a warning

<!-- tests: NONE -->

### Requirement: Universe refresh cadence
The system SHALL refresh each universe source (`sp500`, `sp400`, `sp600`, `nasdaq_listed`) only when it has never been fetched, has no readable fetch date, or was fetched more than 90 days ago. A source that returns zero rows MUST NOT be marked as refreshed.

#### Scenario: Fresh source skipped
- **WHEN** a source was fetched 30 days ago
- **THEN** a refresh skips it

#### Scenario: Empty result not recorded
- **WHEN** a source returns no rows
- **THEN** its last-fetched date is unchanged and the next refresh tries it again

<!-- tests: NONE -->

### Requirement: Universe refresh command
The system SHALL provide `python scripts/refresh_universe.py`, which takes no flags, refreshes the universe, and prints the tickers fetched per source and the totals per tier.

#### Scenario: Everything fresh
- **WHEN** the command runs and every source is within 90 days
- **THEN** it prints that nothing was fetched and still prints the totals per tier

<!-- tests: NONE -->

### Requirement: Peer selection
The system SHALL pick up to 5 peers for a ticker from the same cached sector, nearest first by market-cap proximity. It MUST return an empty list when the ticker or its sector is not cached, and MUST return the first candidates unranked when the ticker has no market-cap value.

#### Scenario: Ticker not cached
- **WHEN** peers are requested for a ticker with no cached sector
- **THEN** the result is an empty list

<!-- tests: NONE -->

### Requirement: Screener signal tiers
The system SHALL screen a tier with these thresholds. `extended`: volume spike 2.5x, price at least 1.08 times its long moving average, overnight gap 3.0%, price within 2% of its 52-week high, minimum score 2. `broad`: 3.0x, 1.15, 5.0%, within 1% of the high, minimum score 3.

#### Scenario: Extended tier is more sensitive
- **WHEN** a ticker rises 10% on 2.7 times its average volume
- **THEN** it qualifies in the `extended` tier
- **AND** it qualifies in the `broad` tier only on the gap and near-high signals

#### Scenario: Unknown tier name
- **WHEN** a tier other than `extended` or `broad` is screened
- **THEN** the `extended` thresholds apply

<!-- tests: tests/test_screener.py::test_extended_tier_is_more_sensitive_than_broad_tier, tests/test_screener.py::test_all_signals_fire_above_both_tier_thresholds -->

### Requirement: Screener scoring
The system SHALL score a ticker 2 points for a volume spike, 1 for momentum, 2 for a gap and 1 for being near its 52-week high, and MUST report only tickers whose score reaches the tier minimum, highest score first. A ticker with fewer than 20 closing prices MUST be skipped.

#### Scenario: All four signals fire
- **WHEN** a ticker breaks out 20% on three times its average volume
- **THEN** its score is 6 in both tiers

#### Scenario: Flat history
- **WHEN** a ticker's price and volume are flat
- **THEN** no signal fires and it is not reported

#### Scenario: Short history
- **WHEN** a ticker has 10 days of prices
- **THEN** it is skipped

#### Scenario: Below the minimum score
- **WHEN** a ticker's score is under the tier minimum
- **THEN** it is excluded

<!-- tests: tests/test_screener.py::test_all_signals_fire_above_both_tier_thresholds, tests/test_screener.py::test_flat_history_fires_no_signals, tests/test_screener.py::test_short_history_is_skipped, tests/test_screener.py::test_score_below_min_score_is_excluded, tests/test_screener.py::test_results_sorted_by_score_descending -->

### Requirement: Screener run and promotion
The system SHALL download one year of daily prices in batches of 500 tickers, store every reported signal, and return the top 15 tickers by score (default) for downstream triage. An empty universe or a tier with no candidates MUST return an empty list, and a failed batch MUST be logged and skipped without stopping the others.

#### Scenario: Universe not loaded
- **WHEN** a tier is screened and the universe table is empty
- **THEN** an empty list is returned and a skip is logged

#### Scenario: One batch fails
- **WHEN** one download batch raises an error
- **THEN** the remaining batches are still screened

<!-- tests: NONE -->

### Requirement: Screener signal storage
The system SHALL store at most one screening signal per ticker, tier and day; screening the same tier twice in a day MUST replace that day's row.

#### Scenario: Re-screened the same day
- **WHEN** a tier is screened again on the same date
- **THEN** each ticker keeps one row for that date with the latest score and signals

<!-- tests: NONE -->

### Requirement: Screener outcome evaluation
The system SHALL give each signal aged 7 to 37 days with no forward return the value (closing price today ÷ closing price on the signal date) − 1, oldest first and at most 300 per run. A signal whose prices are missing MUST be left for a later run.

#### Scenario: Matured signal
- **WHEN** a signal is 8 days old and both prices are available
- **THEN** its forward return is recorded

#### Scenario: Price missing
- **WHEN** either price cannot be found
- **THEN** the signal stays unevaluated and is counted as data missing

<!-- tests: NONE -->

### Requirement: Conviction ranking
The system SHALL rank screener signals by conviction, which is the day's score plus 0.5 per extra day flagged in the trailing 30 days (at most 5 extra days) plus an outcome adjustment of 20 times the average forward return, limited to ±2. Each ticker MUST appear once with its best score, and excluded tickers MUST be dropped.

#### Scenario: Persistent signal
- **WHEN** a ticker was flagged on 4 distinct days in the window and has no matured outcomes
- **THEN** its conviction is its score plus 1.5

#### Scenario: Outcome adjustment capped
- **WHEN** its average forward return is +30%
- **THEN** the outcome adjustment is +2.0

<!-- tests: NONE -->

### Requirement: Screening is deterministic
The system SHALL compute screener signals, conviction and outcomes from price and volume history with no LLM call.

#### Scenario: Screening run
- **WHEN** a tier is screened
- **THEN** every signal comes from the price-and-volume rules above

<!-- tests: NONE -->

### Requirement: Watchlist is manual
The system SHALL add tickers to the watchlist only through user actions (the Opportunity Engine page's Watchlist buttons and the Portfolio page); nothing in the pipeline adds a ticker automatically. The watchlist MUST hold at most 70 tickers.

#### Scenario: Cap reached
- **WHEN** adding tickers would take the watchlist past 70
- **THEN** only the tickers that fit are added and a warning names them

#### Scenario: Duplicate ticker
- **WHEN** a ticker already on the watchlist is added
- **THEN** it is not added twice

<!-- tests: NONE -->

### Requirement: Watchlist owner scoping on read
The system SHALL return, through the authorized watchlist read, only the tickers owned by the caller's verified identity; a different owner MUST get an empty list.

#### Scenario: Another owner reads
- **WHEN** a ticker was added by the local owner and a second owner reads their watchlist
- **THEN** the second owner's result is empty

<!-- tests: tests/test_validation_page_data.py::test_load_watchlist_tickers_scopes_to_the_caller_not_every_owner, tests/test_trusted_context_scoping.py::test_repeated_restricted_list_and_watchlist_migrations_preserve_owner_and_dont_duplicate -->

### Requirement: Watchlist migration
The system SHALL stamp every watchlist row that has no owner with the local owner, rename the legacy owner value `local` to the local owner, and seed the watchlist once from the legacy YAML file, which is then renamed with a `.migrated` suffix. Repeating the migration MUST NOT duplicate rows or change owners.

#### Scenario: Repeated migration
- **WHEN** the watchlist schema setup runs three times
- **THEN** each ticker exists once with the same owner

<!-- tests: tests/test_trusted_context_scoping.py::test_repeated_restricted_list_and_watchlist_migrations_preserve_owner_and_dont_duplicate -->

### Requirement: Watchlist additions use the local owner
The system SHALL stamp every watchlist addition made through the unscoped add path with the local owner, whichever user triggered it.

#### Scenario: Addition by any signed-in user
- **WHEN** a ticker is added through the watchlist add function
- **THEN** its owner is the local owner

<!-- tests: tests/test_validation_page_data.py::test_load_watchlist_tickers_scopes_to_the_caller_not_every_owner -->

### Requirement: Watchlist data coverage
The system SHALL report, for each watchlist ticker, whether the database holds fundamentals, research and prediction rows for it, and MUST return an empty result when the database file does not exist.

#### Scenario: No database
- **WHEN** the database file is missing
- **THEN** the coverage result is empty

<!-- tests: NONE -->

### Requirement: Opportunity candidates come from shared predictions
The system SHALL build opportunity candidates from the latest date that has `trending_opportunity` predictions in the shared scopes, excluding challenger rows and every private row, keeping one row per ticker and preferring the 21-day horizon.

#### Scenario: Private row with a matching trigger type
- **WHEN** a private forecast carries the trigger type `trending_opportunity`
- **THEN** its ticker is not among the candidates

#### Scenario: Several horizons for one ticker
- **WHEN** a ticker has 5-day and 21-day rows on the latest date
- **THEN** the 21-day row is used

<!-- tests: tests/test_review_findings.py::test_opportunity_engine_excludes_a_private_row_even_with_a_matching_trigger_type -->

### Requirement: Opportunity classification
The system SHALL classify a candidate as `BUY` when its recommendation is STRONG_BUY or BUY and no conviction guardrail fired, and as `WATCH` when its recommendation is HOLD with a composite score of at least 5.5. A guardrail-capped STRONG_BUY or BUY MUST count as `WATCH` at that score and be dropped below it. Every other recommendation MUST be dropped.

#### Scenario: Capped buy keeps the floor
- **WHEN** a BUY call with composite 6.0 has a guardrail flag
- **THEN** its call is `WATCH`

#### Scenario: Capped buy below the floor
- **WHEN** a BUY call with composite 4.0 has a guardrail flag
- **THEN** it is dropped

#### Scenario: Uncapped buy
- **WHEN** a STRONG_BUY call has no guardrail flag
- **THEN** its call is `BUY`

#### Scenario: Other recommendations
- **WHEN** the recommendation is SELL, or HOLD at 4.0
- **THEN** it is dropped

<!-- tests: tests/test_review_findings.py::test_classify_downgrades_a_capped_buy_call_to_watch, tests/test_review_findings.py::test_classify_drops_a_capped_buy_call_that_no_longer_clears_the_floor, tests/test_review_findings.py::test_classify_still_returns_buy_for_an_uncapped_call, tests/test_review_findings.py::test_classify_unaffected_for_non_buy_recommendations -->

### Requirement: Opportunity score
The system SHALL score each candidate as the composite score ÷ 10 × 70, plus a conviction bonus of the screener conviction × 3 (at most 15), plus a portfolio-fit bonus from low correlation to the holdings and from relief of the largest sector's weight (relief bonus at most 6), clamped to 0–100 and rounded. When the fit impact cannot be computed the fit bonus MUST be 0.

#### Scenario: No screener conviction and no holdings
- **WHEN** a candidate with composite 8.0 has no screener signal and no holdings are known
- **THEN** its opportunity score is 56

<!-- tests: NONE -->

### Requirement: Opportunity ranking
The system SHALL return the candidates that have a call, sorted by opportunity score descending, at most 10 by default, each with a rank starting at 1, and MUST return an empty list when no candidate predictions exist yet.

#### Scenario: Morning batch has not run
- **WHEN** there are no `trending_opportunity` predictions
- **THEN** the ranking is empty

#### Scenario: More candidates than the limit
- **WHEN** 15 candidates have a call and the limit is 10
- **THEN** the top 10 are returned with ranks 1 to 10

<!-- tests: NONE -->

### Requirement: Opportunity fit uses the supplied book
The system SHALL, when the caller supplies a list of holdings, judge portfolio fit against exactly that list and MUST NOT read any other holdings.

#### Scenario: Holdings supplied
- **WHEN** an owner's own holdings are passed to the ranking
- **THEN** the default holdings read is never called

#### Scenario: Two owners
- **WHEN** two owners each request personal opportunities
- **THEN** each result is judged against that owner's own holdings

<!-- tests: tests/test_chat_engine_tools.py::test_opportunity_engine_defaults_are_unchanged_and_explicit_holdings_are_used, tests/test_chat_engine_tools.py::test_personal_opportunities_judge_against_each_owners_own_holdings, tests/test_chat_engine_tools.py::test_prepare_inputs_passes_the_owners_book_and_shared_scopes_only_when_asked -->

### Requirement: Opportunity history
The system SHALL report, for a ticker over a trailing window (30 days by default), how many days it qualified as `BUY` or `WATCH`, the first such date, its composite score then and now, and the change, counting shared predictions only.

#### Scenario: Private rows ignored
- **WHEN** a private row for the same ticker exists in the window
- **THEN** it is not counted

#### Scenario: Never qualified
- **WHEN** no row in the window qualified
- **THEN** the count is 0 and the dates and scores are empty

<!-- tests: tests/test_review_findings.py::test_opportunity_history_excludes_private_rows -->

### Requirement: Full candidate scoring
The system SHALL offer the same scoring for every candidate of the latest date, including those with no call, so a calibration sample is not limited to the picks.

#### Scenario: SELL candidate
- **WHEN** full scoring runs and a candidate is a SELL
- **THEN** it is returned with a score and an empty call

<!-- tests: NONE -->

### Requirement: Opportunity Engine page
The Opportunity Engine page SHALL require sign-in and show, in order: today's top 10 ranked picks with their call, score and fit notes; every APEX-analyzed candidate of the day before ranking (at most 20 tickers, by conviction); and the raw screener feed not yet analyzed. Each pick MUST offer a Watchlist button that is unavailable once the watchlist holds 70 tickers.

#### Scenario: No predictions yet today
- **WHEN** no opportunities are scored
- **THEN** the page says the morning batch has not produced any and still shows the other sections

#### Scenario: Universe not cached
- **WHEN** the universe table is empty
- **THEN** the screener section tells the user to run `scripts/refresh_universe.py`

<!-- tests: NONE -->

### Requirement: DCF minimum inputs
The system SHALL return no DCF result when revenue history, a free-cash-flow margin, market capitalisation or shares outstanding is unavailable, and otherwise SHALL return an intrinsic value per share that is positive for a profitable company.

#### Scenario: Missing inputs
- **WHEN** a DCF is run without revenue history or without market capitalisation
- **THEN** the result is `None`

<!-- tests: tests/test_valuation_engine.py::test_run_dcf_returns_none_without_minimum_inputs, tests/test_valuation_engine.py::test_run_dcf_produces_positive_intrinsic_value -->

### Requirement: DCF revenue growth and margin
The system SHALL use the average year-over-year growth of the annual revenue history as trend growth, falling back to 5% when fewer than two years exist, and SHALL compute the free-cash-flow margin as (operating cash flow − capex magnitude) ÷ revenue for the latest period.

#### Scenario: Growth from history
- **WHEN** four years of revenue are given
- **THEN** trend growth is the average of the three year-over-year changes

#### Scenario: Too little history
- **WHEN** one year of revenue is given
- **THEN** trend growth is 0.05

<!-- tests: tests/test_valuation_engine.py::test_revenue_growth_trend_uses_yoy_average, tests/test_valuation_engine.py::test_revenue_growth_trend_falls_back_with_insufficient_history, tests/test_valuation_engine.py::test_current_fcf_margin_matches_operating_cf_minus_capex_over_revenue -->

### Requirement: DCF projection
The system SHALL project free cash flow for 5 years, fading growth linearly from trend growth to the terminal growth of 2.5%, at a flat margin, and SHALL discount it with a terminal value, subtracting debt and adding cash before dividing by shares outstanding.

#### Scenario: Default run
- **WHEN** a DCF runs with defaults
- **THEN** its assumptions report 5 projection years and a terminal growth of 0.025

<!-- tests: NONE -->

### Requirement: Discount rate
The system SHALL compute the discount rate as the capital-structure-weighted blend of a CAPM cost of equity (equity risk premium 5%, beta defaulting to 1.0) and an after-tax cost of debt (risk-free rate plus 1.75%, tax 21%), report the full breakdown, and flag the cost of debt as approximate. The rate used MUST be at least the terminal growth plus 1 percentage point.

#### Scenario: Higher beta
- **WHEN** beta rises
- **THEN** the cost of equity rises

#### Scenario: Breakdown reported
- **WHEN** the discount rate is computed
- **THEN** the result includes the weights, the components and `cost_of_debt_is_approximate` set to true

<!-- tests: tests/test_valuation_engine.py::test_compute_wacc_blends_by_capital_structure_and_flags_debt_cost_as_approximate, tests/test_valuation_engine.py::test_compute_wacc_higher_beta_raises_cost_of_equity -->

### Requirement: DCF scenarios
The system SHALL value bear, base and bull cases (growth multiplier 0.5 / 1.0 / 1.5, margin adjustment −1.5 / 0 / +1.5 points, discount-rate adjustment +1.0 / 0 / −1.0 point) with default probabilities 25%, 50% and 25%, and report their probability-weighted expected value. When the base case fails the result MUST be `None`; a failed bear or bull case MUST fall back to the base value.

#### Scenario: Ordering and probabilities
- **WHEN** scenarios are valued
- **THEN** bear is below base and base is below bull
- **AND** the probabilities sum to 1

<!-- tests: tests/test_valuation_engine.py::test_run_dcf_scenarios_orders_bear_base_bull_and_matches_margin_of_safety_formula, tests/test_valuation_engine.py::test_run_dcf_scenarios_probabilities_sum_to_one_by_default -->

### Requirement: Margin of safety and label
The system SHALL compute margin of safety as (base intrinsic − price) ÷ base intrinsic × 100 and label it `UNDERVALUED` above 10, `OVERVALUED` below −10, otherwise `FAIRLY_VALUED`; with no current price both MUST be empty.

#### Scenario: Price far below value
- **WHEN** the price is well under the base intrinsic value
- **THEN** the label is `UNDERVALUED`

#### Scenario: Price far above value
- **WHEN** the price is well over the base intrinsic value
- **THEN** the label is `OVERVALUED`

<!-- tests: tests/test_valuation_engine.py::test_valuation_label_thresholds, tests/test_valuation_engine.py::test_run_dcf_scenarios_orders_bear_base_bull_and_matches_margin_of_safety_formula -->

### Requirement: Sensitivity grid
The system SHALL produce a 5 by 5 grid of intrinsic values for discount-rate changes of −1.0, −0.5, 0, +0.5, +1.0 points and terminal-growth changes of −0.5, −0.25, 0, +0.25, +0.5 points, falling as the discount rate rises and rising with terminal growth.

#### Scenario: Monotonic grid
- **WHEN** the grid is built
- **THEN** values fall down each column as the discount rate rises
- **AND** values rise along each row as terminal growth rises

<!-- tests: tests/test_valuation_engine.py::test_sensitivity_grid_is_monotonic_in_wacc_and_terminal_growth -->

### Requirement: Relative valuation
The system SHALL compare a ticker's forward P/E, EV/EBITDA, EV/revenue, price/sales and PEG with the average of its peers and with the market, and report its free-cash-flow yield. When the market has no forward P/E it MUST use 20.0 and flag it as a fallback, and the historical P/E range MUST be flagged approximate.

#### Scenario: Peers and market fallback
- **WHEN** peers have forward P/E 20 and 10 and no market multiples exist
- **THEN** the peer average is 15
- **AND** the market forward P/E is the flagged fallback

#### Scenario: FCF yield
- **WHEN** free cash flow is 10% of market capitalisation
- **THEN** the FCF yield is 10.0

<!-- tests: tests/test_valuation_engine.py::test_relative_valuation_computes_fcf_yield_and_peer_average -->

### Requirement: Valuation storage
The system SHALL store one valuation per ticker and day, replacing the day's row on a rerun, and SHALL read the most recent one per ticker with its stored JSON parsed; a ticker with no valuation MUST be absent from a multi-ticker read.

#### Scenario: Rerun on the same day
- **WHEN** a ticker is valued twice on one date
- **THEN** one row remains with the later values

#### Scenario: Unvalued ticker
- **WHEN** valuations are read for a list including an unvalued ticker
- **THEN** that ticker is not in the result

<!-- tests: NONE -->

### Requirement: Valuation phase
The system SHALL refresh a ticker's valuation when none exists or it is older than 95 days, include the day's `trending_opportunity` candidates, use the 10-year Treasury yield as the risk-free rate (4.0% when unavailable), and count a ticker as failed, without stopping the run, when its filings or DCF cannot be produced. It MUST be skipped for funds, money-market funds, cash and ticker-less funds before any EDGAR call.

#### Scenario: Fund in the list
- **WHEN** the phase receives a stock and an ETF
- **THEN** only the stock is checked for refresh and the skip is logged

#### Scenario: One ticker fails
- **WHEN** a ticker's filings return an error
- **THEN** it is counted as failed and the others are still valued

<!-- tests: tests/test_etf_routing.py::test_valuation_phase_skips_fund_tickers_before_any_edgar_call -->

### Requirement: Valuation is deterministic
The system SHALL compute DCF, scenarios, sensitivity and relative valuation with no LLM call and SHALL store each result with the model name `deterministic`.

#### Scenario: Stored valuation
- **WHEN** the phase saves a valuation
- **THEN** its model name is `deterministic`

<!-- tests: NONE -->

### Requirement: Valuation screen
The Valuation screen SHALL group tickers as All, Portfolio, Watchlist and New Opportunities, rank valued tickers by margin of safety, list unvalued tickers separately, and offer a look-up that computes and stores a valuation on demand for any ticker the batch has not covered. The Portfolio group MUST list only the signed-in owner's holdings.

#### Scenario: Unvalued ticker in a group
- **WHEN** a group contains a ticker with no stored valuation
- **THEN** it is listed as not yet valued with a pointer to the look-up

#### Scenario: Portfolio group
- **WHEN** two owners view the Portfolio group
- **THEN** each sees only their own holdings' tickers

<!-- tests: NONE -->
