## Purpose

Defines the deterministic trade plan (entry, stop, target, time stop) attached to shared UP forecasts, the private position sizing derived from it, the history-replay simulation that estimates how often a plan reaches its target first, the realized plan outcomes, and the validation of simulator and conviction against those outcomes. All of it is rule-based with no LLM and is research output only. Baseline at HEAD `ca450dd`.

## ADDED Requirements

### Requirement: Plans are research output and never execute trades
The system SHALL present trade plans, sizing and simulations as research information only and MUST NOT place, route or stage an order, or connect to a broker, from any plan or simulation code path.

#### Scenario: Plan block content
- **WHEN** a plan is rendered for a forecast
- **THEN** it shows entry, stop, target, time stop and reward:risk as information
- **AND** no control exists to submit an order

<!-- tests: NONE -->

### Requirement: Plans exist only for shared UP forecasts on supported horizons
A trade plan SHALL be built only for a shared-market forecast whose direction is UP and whose horizon is 5, 21 or 63 days. Any other forecast stores a no-plan record carrying the reason.

#### Scenario: Non-UP forecast
- **WHEN** a forecast's direction is DOWN, FLAT or missing
- **THEN** the stored plan status is `no_plan` with reason `not_up`

#### Scenario: Unsupported horizon
- **WHEN** an UP forecast has horizon 250 or a missing horizon
- **THEN** the stored plan status is `no_plan` with reason `unsupported_horizon`

<!-- tests: tests/test_trade_plan.py::test_every_no_plan_reason, tests/test_trade_plan.py::test_non_up_and_missing_atr_rows_store_the_reason, tests/test_trade_plan.py::test_plan_is_stored_for_a_shared_up_row -->

### Requirement: Plan levels are a pure function of entry, ATR and settings
The entry SHALL be the forecast's as-of close. The risk per share SHALL be the horizon's stop multiple times the 14-day ATR, the stop SHALL be entry minus risk, and the target SHALL be entry plus the horizon's R multiple times risk. No model output SHALL influence these numbers.

#### Scenario: 5-day plan
- **WHEN** entry is 100 and ATR is 2 on the 5-day horizon under seed settings
- **THEN** risk is 3, stop is 97, target is 104.5 and reward:risk is 1.5

#### Scenario: 63-day plan
- **WHEN** entry is 100 and ATR is 2 on the 63-day horizon under seed settings
- **THEN** stop is 92, target is 120 and reward:risk is 2.5

<!-- tests: tests/test_trade_plan.py::test_build_plan_math -->

### Requirement: Seed plan settings
The system SHALL seed the plan settings as: stop multiples 1.5 / 2.5 / 4.0 and R multiples 1.5 / 2.0 / 2.5 for horizons 5 / 21 / 63; minimum reward:risk 1.5; widest allowed stop 8% / 15% / 25% of entry for horizons 5 / 21 / 63.

#### Scenario: Seeds on a fresh database
- **WHEN** plan settings are read before any admin change
- **THEN** they equal the seed values above

<!-- tests: tests/test_trade_plan.py::test_settings_are_seeded_and_updatable_and_validated -->

### Requirement: No-plan reasons and their precedence
When a plan cannot be built the system SHALL record the first applicable reason in this order: `not_up`, `unsupported_horizon`, `missing_price`, `missing_atr`, `too_volatile`, `below_min_rr`. A stop exactly at the widest allowed distance SHALL still produce a plan.

#### Scenario: Missing inputs
- **WHEN** the entry price is missing or zero
- **THEN** the reason is `missing_price`
- **AND** when ATR is missing, not a number or zero, the reason is `missing_atr`

#### Scenario: Stop too wide
- **WHEN** the stop distance exceeds the horizon's widest allowed stop, or would put the stop at or below zero
- **THEN** the reason is `too_volatile`

#### Scenario: Reward below minimum
- **WHEN** the horizon's R multiple gives reward:risk below the minimum
- **THEN** the reason is `below_min_rr`

#### Scenario: Boundary stop
- **WHEN** the stop distance equals exactly the widest allowed stop
- **THEN** a plan is produced

<!-- tests: tests/test_trade_plan.py::test_every_no_plan_reason, tests/test_trade_plan.py::test_stop_exactly_at_the_widest_allowed_is_still_a_plan -->

### Requirement: Model target is shown for comparison only
The plan SHALL record the model's own upper return as `llm_target` (entry times one plus the predicted high return) beside the plan, and that value MUST NOT change the stop, target or reward:risk.

#### Scenario: Different model targets, same plan
- **WHEN** two otherwise identical forecasts carry predicted high returns of 1% and 50%
- **THEN** their stop, target and reward:risk are identical
- **AND** their `llm_target` values differ

<!-- tests: tests/test_trade_plan.py::test_llm_target_is_recorded_but_never_moves_the_plan -->

### Requirement: Time stop is the horizon in trading days
The plan's time stop SHALL be the forecast date plus the horizon in trading days, skipping weekends and NYSE holidays.

#### Scenario: 5-day time stop
- **WHEN** a 5-day plan is made on a Monday with no holidays in the week
- **THEN** the time stop is the following Monday

<!-- tests: tests/test_trade_plan.py::test_build_plan_math -->

### Requirement: Plans are written once and are immutable
The system SHALL store one plan per shared forecast. Re-running creation SHALL return the stored plan unchanged, and the database SHALL refuse any UPDATE or DELETE of a stored plan.

#### Scenario: Settings and data change afterwards
- **WHEN** the plan rules or the ATR data change after a plan was stored and creation is called again
- **THEN** the stored plan is returned unchanged and no second row exists

#### Scenario: Direct edit refused
- **WHEN** an UPDATE or DELETE is run against a stored plan
- **THEN** the database aborts with an "immutable" error

<!-- tests: tests/test_trade_plan.py::test_second_call_is_a_no_op_and_rows_are_immutable -->

### Requirement: Private forecasts never get a plan
Plans SHALL be created only for forecasts in the shared-market scope; a private (owner-scoped) forecast MUST NOT receive a plan or a simulation.

#### Scenario: Private chat forecast
- **WHEN** plan creation is called for a private forecast
- **THEN** it returns nothing and stores no plan

<!-- tests: tests/test_trade_plan.py::test_private_rows_never_get_a_plan, tests/test_plan_simulator.py::test_only_planned_shared_rows_are_simulated -->

### Requirement: ATR uses only data on or before the forecast date
The ATR for a plan SHALL come from bars dated on or before the forecast's as-of date, no older than 7 days before it. A stored technical snapshot dated after the forecast date SHALL be ignored, and fetched history SHALL be cut at the as-of date before ATR is computed.

#### Scenario: Next-day spike
- **WHEN** price history contains a large move the day after the forecast date
- **THEN** the plan's ATR and levels are unchanged

#### Scenario: Stale data
- **WHEN** the newest available bar is more than 7 days before the forecast date
- **THEN** no ATR is used and the reason is `missing_atr`

<!-- tests: tests/test_trade_plan.py::test_atr_ignores_bars_after_the_forecast_date, tests/test_trade_plan.py::test_plan_does_not_change_when_the_next_day_spikes, tests/test_trade_plan.py::test_a_stored_snapshot_dated_after_the_forecast_is_ignored, tests/test_trade_plan.py::test_stale_data_is_not_used -->

### Requirement: Plan backfill is look-ahead safe and repeatable
Backfill SHALL create plans for every shared forecast on or after a given date that has none, each using only data on or before its own as-of date, skipping rows that already have a plan and counting per-row failures without stopping.

#### Scenario: Two forecast dates
- **WHEN** backfill runs over forecasts from different days
- **THEN** each plan uses ATR as of its own date
- **AND** a second run adds nothing

<!-- tests: tests/test_trade_plan.py::test_backfill_uses_each_rows_own_as_of_date -->

### Requirement: Plan creation never breaks the forecast run
The daily APEX phase SHALL create a plan after each saved shared forecast, and a failure while creating a plan MUST NOT affect the stored forecast or the rest of the run.

#### Scenario: Plan store down
- **WHEN** plan creation raises for a saved forecast
- **THEN** the forecast remains stored and the run continues without a plan row

#### Scenario: Normal run
- **WHEN** two shared forecasts are saved for horizons 5 and 21
- **THEN** one plan row exists for each

<!-- tests: tests/test_trade_plan.py::test_a_plan_failure_never_breaks_the_pipeline, tests/test_trade_plan.py::test_pipeline_writes_a_plan_after_each_shared_forecast -->

### Requirement: Plan settings are validated, versioned with engine settings and resettable
Plan settings SHALL be changed through the engine settings update, validated, and reset with the engine settings. Stop and R multiples MUST be above 0; horizons MUST be 5, 21 or 63; widest stop MUST be above 0 and at most 1; max loss per risk profile MUST be above 0 and at most 0.1 for `conservative`, `moderate`, `aggressive`; minimum reward:risk MUST be above 0 and at most 20.

#### Scenario: Invalid patch rejected
- **WHEN** an update sets a stop multiple of 0, horizon 7, a widest stop of 1.5, an unknown profile or a max loss of 0.5
- **THEN** it is refused and the stored settings are unchanged

#### Scenario: Changed settings drive new plans only
- **WHEN** the 5-day stop multiple is changed to 2.0 and a new forecast is planned
- **THEN** the new plan uses risk of 2.0 times ATR
- **AND** previously stored plans are unchanged

<!-- tests: tests/test_trade_plan.py::test_settings_are_seeded_and_updatable_and_validated, tests/test_trade_plan.py::test_changed_settings_drive_new_plans -->

### Requirement: Position sizing from a loss budget and position limit
Sizing SHALL be shares equal to the floor of (portfolio value times the profile's max-loss fraction) divided by risk per share, limited so the position value stays within the owner's maximum position percent. Seed max-loss fractions are 0.5% conservative, 1% moderate and 1.5% aggressive, and the result SHALL report which cap bound.

#### Scenario: Loss cap binds
- **WHEN** a moderate profile has a 100,000 portfolio, risk per share 3, entry 100 and a 50% position limit
- **THEN** shares are 333, the binding cap is `loss` and the loss if stopped is 999

#### Scenario: Position cap binds
- **WHEN** the position limit is 10% in the same case
- **THEN** shares are 100 and the binding cap is `position`

#### Scenario: Profile differences
- **WHEN** the same plan is sized for conservative and aggressive profiles
- **THEN** shares are 166 and 500

<!-- tests: tests/test_trade_plan.py::test_sizing_loss_cap_binding, tests/test_trade_plan.py::test_sizing_position_cap_binding, tests/test_trade_plan.py::test_sizing_by_risk_profile -->

### Requirement: Unknown or non-positive sizing inputs buy nothing
Sizing SHALL return zero shares when portfolio value, risk per share or entry is missing, not a number or not positive, when the profile is unknown, or when the budget or limit cannot afford one share. A negative position limit SHALL be ignored, not obeyed.

#### Scenario: Zero portfolio value
- **WHEN** the portfolio value is 0, negative or missing
- **THEN** shares, position value and loss if stopped are all 0

#### Scenario: Budget below one share
- **WHEN** the loss budget is smaller than the risk of one share
- **THEN** shares are 0

#### Scenario: Negative limit
- **WHEN** the position limit is negative
- **THEN** only the loss budget limits the shares

<!-- tests: tests/test_trade_plan.py::test_sizing_zero_or_negative_inputs_buy_nothing, tests/test_trade_plan.py::test_sizing_too_small_a_budget_or_limit_buys_nothing -->

### Requirement: Sizing is private and computed at read time
Sizing SHALL be computed for the verified owner when a decision is composed, from that owner's portfolio value and effective policy, and MUST NOT be stored with the shared plan or appear in the shared market outlook. With no planned trade, sizing is empty.

#### Scenario: Decision for an owner
- **WHEN** a decision is composed for an owner with holdings and a planned trade
- **THEN** the portfolio-fit part carries shares, max loss and loss if stopped
- **AND** the market-outlook part and the stored plan carry no sizing

#### Scenario: No planned trade
- **WHEN** the plan is missing or has status `no_plan`
- **THEN** sizing is empty

<!-- tests: tests/test_trade_plan.py::test_market_outlook_carries_the_shared_plan_and_fit_carries_private_sizing, tests/test_trade_plan.py::test_plan_sizing_is_none_without_a_planned_trade -->

### Requirement: Simulation replays the stock's own past bars
The system SHALL estimate, for each planned shared forecast, the share of 10,000 bootstrapped futures (moving blocks of the stock's own real days) that reach the target first, the stop first, or neither by the time stop. Output is a replay of history and MUST be labelled as not a prediction.

#### Scenario: Steady rise
- **WHEN** the pool of bars rises steadily
- **THEN** the target is reached first in nearly all paths

#### Scenario: Steady fall
- **WHEN** the pool of bars falls steadily
- **THEN** the stop is hit first in nearly all paths

#### Scenario: Flat series
- **WHEN** the pool has zero volatility
- **THEN** every path times out

<!-- tests: tests/test_plan_simulator.py::test_steady_rise_reaches_the_target_first, tests/test_plan_simulator.py::test_steady_fall_hits_the_stop_first, tests/test_plan_simulator.py::test_a_flat_zero_volatility_series_always_times_out, tests/test_plan_simulator.py::test_ten_thousand_paths_over_63_days_fits_the_budget -->

### Requirement: Intraday touch conventions
A day SHALL touch the stop when its low is at or below the stop and the target when its high is at or above the target. When both occur on the same day it SHALL count as the stop. Exits fill exactly at the level, and a path touching neither is marked to its horizon close, with R equal to reward:risk for a target, -1 for a stop, and (close minus entry) / risk for a timeout.

#### Scenario: Same-day touch
- **WHEN** one day's range touches both stop and target
- **THEN** the outcome is a stop with R of -1

#### Scenario: Probabilities
- **WHEN** a simulation completes
- **THEN** target-first, stop-first and neither probabilities sum to 1
- **AND** the 5th, 50th and 95th percentile end returns are ordered

<!-- tests: tests/test_plan_simulator.py::test_same_day_stop_and_target_counts_as_the_stop, tests/test_plan_simulator.py::test_probabilities_add_up_and_percentiles_are_ordered -->

### Requirement: Simulation is deterministic
The simulation seed SHALL be a stable 32-bit value derived from ticker, as-of date, horizon and plan rules version, so the same inputs give identical results and different keys give different seeds.

#### Scenario: Repeat run
- **WHEN** a simulation is run twice with the same inputs
- **THEN** the results are identical

#### Scenario: Different key
- **WHEN** the ticker, date or horizon differs
- **THEN** the seed differs

<!-- tests: tests/test_plan_simulator.py::test_same_inputs_give_identical_results_and_seeds_differ_by_key -->

### Requirement: Simulation pool excludes the future and needs 250 bars
The simulation pool SHALL contain only usable bars dated on or before the forecast's as-of date, the most recent lookback days of them. With fewer than 250 bars the status SHALL be `insufficient_history` and no simulation is run.

#### Scenario: Crash after the forecast
- **WHEN** the stock crashes the day after the forecast date
- **THEN** the stored simulation is unchanged by it

#### Scenario: Short history
- **WHEN** fewer than 250 bars exist as of the forecast date
- **THEN** the stored status is `insufficient_history` and no flag is written

<!-- tests: tests/test_plan_simulator.py::test_bars_after_the_forecast_date_never_enter_the_pool, tests/test_plan_simulator.py::test_prepare_bars_takes_the_latest_lookback_and_needs_250, tests/test_plan_simulator.py::test_the_simulation_ignores_a_crash_on_the_day_after_the_forecast, tests/test_plan_simulator.py::test_short_history_is_recorded_as_insufficient_and_a_fetch_failure_as_error -->

### Requirement: Zero-drift baseline
The simulation SHALL also report the target-first probability after removing the pool's mean daily log return, as the "stock just wiggles, no edge" baseline.

#### Scenario: Strong trend removed
- **WHEN** the pool has a strong upward trend
- **THEN** the zero-drift target-first probability is lower than the plain one

<!-- tests: tests/test_plan_simulator.py::test_zero_drift_baseline_removes_the_trend -->

### Requirement: Simulation records are stored with provenance
Each simulation SHALL be stored once per planned shared forecast with its status (`ok`, `insufficient_history`, `error`), probabilities, expected R, close-above-entry probability, end-return percentiles, history-replay counts, path count, block length, lookback, seed and simulation version. A simulation error SHALL be stored as `error`, never raised, and retried by backfill.

#### Scenario: Stored row
- **WHEN** a simulation succeeds
- **THEN** the row carries the settings it used and its version

#### Scenario: Fetch failure
- **WHEN** fetching bars raises
- **THEN** the stored status is `error` and nothing is raised

#### Scenario: Backfill retries errors
- **WHEN** backfill runs and a prior row has status `error`
- **THEN** the row is removed and recomputed

<!-- tests: tests/test_plan_simulator.py::test_simulation_row_is_stored_with_its_provenance, tests/test_plan_simulator.py::test_short_history_is_recorded_as_insufficient_and_a_fetch_failure_as_error, tests/test_plan_simulator.py::test_backfill_flags_but_never_caps_and_retries_errors -->

### Requirement: Only planned shared forecasts are simulated, once
Simulation SHALL run only for shared forecasts with status `planned`; forecasts with no plan, no-plan status, a private scope or an unknown id yield nothing, and a second call returns the stored row unchanged.

#### Scenario: Ineligible rows
- **WHEN** simulation is requested for a no-plan, private, unplanned or unknown forecast
- **THEN** it returns nothing and stores no row

#### Scenario: Repeat call
- **WHEN** a simulation exists and is requested again
- **THEN** the stored row is returned unchanged

<!-- tests: tests/test_plan_simulator.py::test_only_planned_shared_rows_are_simulated, tests/test_plan_simulator.py::test_second_call_is_a_no_op -->

### Requirement: Historical replay of the plan rule
The system SHALL report the real track record of the plan rule on the stock: on every past day in the pool where a valid plan would have existed and a full horizon of later bars is known, it counts targets, stops and timeouts with the same touch conventions, and the average R. Overlapping days are not independent trials.

#### Scenario: Small series
- **WHEN** replay runs over a small known series
- **THEN** target, stop and timeout counts match the hand count

#### Scenario: Invalid or unfinished days
- **WHEN** a day has no valid plan or its horizon runs past the pool
- **THEN** it is skipped

<!-- tests: tests/test_plan_simulator.py::test_historical_replay_counts_on_a_small_series, tests/test_plan_simulator.py::test_historical_replay_skips_invalid_days_and_unfinished_horizons, tests/test_plan_simulator.py::test_replay_atr_matches_the_technical_features_formula -->

### Requirement: Simulation settings and defaults
The simulation SHALL read engine settings with defaults `sim_paths` 10000 (100 to 100000), `sim_lookback_days` 500 (250 to 2500), `sim_block_len` 5 (1 to 30), `sim_flag_min_conviction` 7.0 (0 to 10), `sim_flag_max_p_target` 0.35 (above 0, at most 1), `sim_guardrail_mode` `shadow` (`shadow` or `cap`) and `blend_min_n` 150. Invalid updates SHALL be refused and settings reset with the engine settings.

#### Scenario: Defaults and update
- **WHEN** settings are read, then paths and mode are updated and later reset
- **THEN** the defaults above apply, the update takes effect on the next simulation and reset restores `shadow` and 10000

#### Scenario: Invalid values
- **WHEN** an update sets paths to 10, a block length of 0, a conviction of 11, a probability of 0, or an unknown mode
- **THEN** it is refused and settings are unchanged

<!-- tests: tests/test_plan_simulator.py::test_sim_settings_defaults_update_validation_and_reset, tests/test_plan_simulator.py::test_settings_drive_the_simulation -->

### Requirement: Sim-disagrees flag rule
The system SHALL flag a forecast `sim_disagrees` when its raw conviction is at or above the minimum conviction and the simulated target-first probability is strictly below the maximum probability. A missing or non-numeric value on either side MUST never flag.

#### Scenario: Boundaries
- **WHEN** conviction is exactly 7 and probability is 0.34
- **THEN** the forecast is flagged
- **AND** a probability of exactly 0.35 or a conviction of 6.9 is not flagged

#### Scenario: Unknown value
- **WHEN** conviction is missing or probability is not a number
- **THEN** no flag is raised

<!-- tests: tests/test_plan_simulator.py::test_sim_disagrees_rule, tests/test_plan_simulator.py::test_no_flag_when_the_history_agrees_or_conviction_is_low -->

### Requirement: Shadow mode flags without changing scores
In `shadow` mode the system SHALL append `sim_disagrees` to the forecast's guardrail flags and change nothing else about the forecast.

#### Scenario: Shadow flag
- **WHEN** a flagged forecast is simulated in shadow mode
- **THEN** its guardrail flags include `sim_disagrees`
- **AND** its conviction and composite scores are unchanged

<!-- tests: tests/test_plan_simulator.py::test_shadow_mode_flags_but_changes_nothing_else -->

### Requirement: Cap mode limits conviction and composite to 5
In `cap` mode a flagged forecast SHALL have its conviction and composite scores limited to 5, never raised if already lower, with the model's own conviction kept in the raw conviction value and, where it was empty, stamped with the pre-cap conviction. Backfill SHALL flag but never cap.

#### Scenario: Cap applied
- **WHEN** a flagged forecast with conviction 8 and composite 8 is simulated in cap mode
- **THEN** both become 5 and the raw conviction is 8

#### Scenario: Existing raw value and low scores
- **WHEN** a raw conviction already exists or a score is below 5
- **THEN** the raw value is kept and the low score is not raised

#### Scenario: Backfill
- **WHEN** backfill simulates an old flagged forecast
- **THEN** the flag is written and conviction is not capped

<!-- tests: tests/test_plan_simulator.py::test_cap_mode_caps_conviction_and_composite_and_keeps_the_raw_value, tests/test_plan_simulator.py::test_cap_mode_never_touches_an_existing_raw_value_and_never_raises_a_low_score, tests/test_plan_simulator.py::test_the_flag_uses_the_pre_cap_conviction_when_one_exists, tests/test_plan_simulator.py::test_backfill_flags_but_never_caps_and_retries_errors -->

### Requirement: Simulation never breaks the forecast run
The daily APEX phase SHALL simulate each plan after creating it, in its own failure guard, and a simulation failure MUST NOT affect the forecast, the plan or the rest of the run.

#### Scenario: Simulator raises
- **WHEN** simulation raises after a plan was created
- **THEN** the forecast and plan remain and the run continues

#### Scenario: Normal run
- **WHEN** a plan is created in the pipeline
- **THEN** its simulation is created immediately after

<!-- tests: tests/test_plan_simulator.py::test_a_simulation_failure_never_breaks_the_pipeline, tests/test_plan_simulator.py::test_pipeline_simulates_each_plan_after_creating_it -->

### Requirement: Plan outcomes from real bars
A matured plan's outcome SHALL be found by walking the real daily bars from the day after the forecast date through the time stop and never past it: low at or below the stop is `STOP` (R of -1), high at or above the target is `TARGET` (R of the plan's reward:risk), both on one day is `STOP`, and neither is `TIMEOUT` at the time-stop close. An outcome SHALL be unknown when no bars exist in the window, or, if neither level was touched, when the last bar is more than 4 days before the time stop.

#### Scenario: Target day
- **WHEN** a bar's high reaches the target before any bar's low reaches the stop
- **THEN** the outcome is `TARGET` with R equal to reward:risk

#### Scenario: Not yet knowable
- **WHEN** data has not caught up to the time stop and neither level was touched
- **THEN** no outcome is recorded yet

#### Scenario: Touched level is final
- **WHEN** a stop or target was already touched and later bars are missing
- **THEN** the outcome is final

<!-- tests: tests/test_plan_validation.py::test_target_day, tests/test_plan_validation.py::test_stop_day, tests/test_plan_validation.py::test_same_day_target_and_stop_counts_as_stop, tests/test_plan_validation.py::test_stop_first_then_target_is_a_stop_and_target_first_then_stop_is_a_target, tests/test_plan_validation.py::test_timeout_marks_to_the_time_stop_close, tests/test_plan_validation.py::test_the_walker_never_reads_past_the_time_stop, tests/test_plan_validation.py::test_bars_on_or_before_the_forecast_date_are_not_part_of_the_walk, tests/test_plan_validation.py::test_not_yet_knowable_returns_none -->

### Requirement: Outcome evaluation is parallel to direction scoring
Evaluation SHALL record one outcome per planned shared forecast whose time stop is on or before today and that has none, leaving unresolved rows for the next run, and MUST NOT alter direction or bucket scoring. A failure while recording outcomes MUST NOT break direction evaluation.

#### Scenario: Matured plan
- **WHEN** evaluation runs after a plan's time stop
- **THEN** one outcome row is stored and a rerun changes nothing

#### Scenario: Premature or missing data
- **WHEN** the time stop is in the future, or bars cannot be fetched
- **THEN** no outcome is stored and the row stays pending or counts as an error

#### Scenario: Failure isolation
- **WHEN** outcome recording raises during prediction evaluation
- **THEN** direction evaluation still completes

<!-- tests: tests/test_plan_validation.py::test_matured_plans_get_an_outcome_and_a_rerun_changes_nothing, tests/test_plan_validation.py::test_plans_before_their_time_stop_or_without_data_are_left_alone, tests/test_plan_validation.py::test_evaluate_matured_predictions_records_plan_outcomes_without_touching_direction_scoring, tests/test_plan_validation.py::test_a_plan_outcome_failure_never_breaks_direction_evaluation -->

### Requirement: Plan validation reads shared forecasts in validation roles
Plan validation SHALL use only matured, planned, shared-scope forecasts that have an `ok` simulation and a recorded outcome, restricted by default to the default validation roles, and SHALL report per horizon without mixing horizons.

#### Scenario: Challenger rows
- **WHEN** the plan rows are loaded with default roles
- **THEN** challenger forecasts are not included

<!-- tests: NONE -->

### Requirement: Simulator calibration and edge metrics
Per horizon the system SHALL compute a reliability table of simulated target-first probability against realized TARGET rate, a three-outcome Brier score against a base-rate forecast, and the edge (mean of realized hit minus simulated probability, also against the zero-drift baseline), with intervals that resample whole as-of dates rather than rows.

#### Scenario: Reality matches simulator
- **WHEN** realized hits follow the simulated probabilities
- **THEN** the edge is about zero

#### Scenario: Model beats simulator
- **WHEN** realized hits exceed the simulated probabilities
- **THEN** the edge is positive

#### Scenario: Interval resamples dates
- **WHEN** many rows share a few dates
- **THEN** the interval is as wide as the number of dates implies, not the number of rows

<!-- tests: tests/test_plan_validation.py::test_edge_is_about_zero_when_reality_matches_the_simulator, tests/test_plan_validation.py::test_edge_is_positive_when_the_model_beats_the_simulator, tests/test_plan_validation.py::test_edge_by_band_rises_with_conviction_when_conviction_has_edge, tests/test_plan_validation.py::test_the_interval_resamples_dates_not_rows, tests/test_plan_validation.py::test_reliability_table_and_brier_on_calibrated_data -->

### Requirement: Conviction bands and agreement grids
The system SHALL report realized TARGET rate and mean R by raw-conviction band (1-4, 5-6, 7-8, 9-10) and grids of conviction band by simulated-probability band (under 15%, 15-25%, 25-35%, 35% and above), plus a confident-or-not by simulator-agrees-or-not 2x2.

#### Scenario: Banding
- **WHEN** rows with known conviction and probability are grouped
- **THEN** each falls in exactly one band and each cell reports n, target rate and mean R

<!-- tests: tests/test_plan_validation.py::test_conviction_bands_and_grids -->

### Requirement: Disagreement significance test
The "sim disagrees" cell SHALL be judged significantly worse than the "both confident" cell only when the two-proportion p-value is below 0.05, the disagree rate is lower, and each cell has at least 30 rows.

#### Scenario: Thin cells
- **WHEN** either cell has fewer than 30 rows
- **THEN** the result is not significantly worse regardless of p-value

#### Scenario: Clear gap
- **WHEN** both cells have at least 30 rows, the disagree rate is lower and p is below 0.05
- **THEN** the result is significantly worse

<!-- tests: tests/test_plan_validation.py::test_disagreement_test_flags_a_significantly_worse_cell_and_respects_min_n -->

### Requirement: Blended probability is used only when it earns its place
The system SHALL fit, per horizon with at least `blend_min_n` matured rows, a logistic blend of simulated probability (clipped to 0.01-0.99) and conviction, store it weekly, and treat it as valid only when its out-of-sample Brier score beats the simulator alone. Only a valid blend SHALL be shown on prediction cards.

#### Scenario: Too few rows
- **WHEN** a horizon has fewer than the minimum matured rows
- **THEN** no blend is fitted or shown

#### Scenario: Blend not better
- **WHEN** the blend's out-of-sample Brier is not below the simulator-only Brier
- **THEN** it is stored but not used, and `p_blend` returns nothing

#### Scenario: Valid blend
- **WHEN** conviction adds information and the blend beats the simulator
- **THEN** the blended probability is available for display

<!-- tests: tests/test_plan_validation.py::test_logistic_fit_recovers_known_coefficients, tests/test_plan_validation.py::test_blend_is_withheld_below_the_minimum_n, tests/test_plan_validation.py::test_blend_is_used_when_conviction_adds_information, tests/test_plan_validation.py::test_blend_is_not_valid_when_it_does_not_beat_the_simulator_alone, tests/test_plan_validation.py::test_only_a_valid_blend_is_stored_for_use_and_p_blend_reads_it, tests/test_plan_validation.py::test_p_clip_keeps_extreme_probabilities_finite, tests/test_plan_validation.py::test_blended_odds_on_cards_only_with_a_valid_blend -->

### Requirement: Weekly refit and cap recommendation
The weekly analysis SHALL refit the blends and record the disagreement check, a failure in it MUST NOT break the weekly analysis, and the system SHALL recommend `sim_guardrail_mode = cap` only when the check was significantly worse in at least 2 of the last 4 weekly reports. The recommendation SHALL change nothing; an admin applies it in one click.

#### Scenario: Two of four weeks
- **WHEN** two of the last four weekly reports show a significant result
- **THEN** a cap recommendation is made

#### Scenario: No reports
- **WHEN** no weekly reports exist
- **THEN** no recommendation is made

#### Scenario: Non-admin switch
- **WHEN** a non-admin tries the one-click switch
- **THEN** it is refused and the mode is unchanged

<!-- tests: tests/test_plan_validation.py::test_cap_recommendation_needs_two_significant_weeks_of_the_last_four, tests/test_plan_validation.py::test_cap_recommendation_is_quiet_with_no_reports, tests/test_plan_validation.py::test_one_click_switch_sets_cap_and_requires_admin, tests/test_plan_validation.py::test_weekly_analysis_refits_blends_and_records_the_sim_check -->

### Requirement: Plan validation page judges like the Predictions grid
The Plans and Simulation view SHALL show one column per horizon with three checks (model edge, simulator calibration, doubted calls), hide any cell with fewer than 10 rows, give no verdict before the minimum number of prediction dates, and say so when no plans have matured.

#### Scenario: Thin data
- **WHEN** a band or cell has fewer than 10 rows
- **THEN** its numbers are hidden

#### Scenario: Too early
- **WHEN** a horizon has too few prediction dates
- **THEN** its checks show "too early" instead of a verdict

<!-- tests: tests/test_plan_validation.py::test_plans_page_is_a_filterless_grid_judged_like_the_predictions_page, tests/test_plan_validation.py::test_view_helpers_on_a_full_report -->

### Requirement: Plan and replay blocks on forecast cards
For each shared forecast row the UI SHALL show entry, stop, target, time stop and reward:risk, or the plain-words no-plan reason, plus the model's own target labelled "not used"; the history replay SHALL show target-first, stop-first and neither shares, expected R, a "Sim disagrees" chip when flagged, and the text "Not a prediction". A row with no plan record, or a private forecast, SHALL show nothing.

#### Scenario: Planned row
- **WHEN** a planned row is rendered
- **THEN** the block shows entry, stop, target, time stop and reward:risk
- **AND** the model target appears marked "not used"

#### Scenario: No plan
- **WHEN** the row's plan status is `no_plan` with reason `too_volatile`
- **THEN** the block reads "No trade plan" with the reason text

#### Scenario: Insufficient history
- **WHEN** the simulation status is `insufficient_history`
- **THEN** the block says there is not enough price history to replay the past

#### Scenario: Flagged replay
- **WHEN** the row is flagged
- **THEN** the chip "Sim disagrees" and "Not a prediction" appear

<!-- tests: tests/test_trade_plan.py::test_plan_block_html_for_planned_no_plan_and_missing_rows, tests/test_plan_simulator.py::test_sim_block_html -->

### Requirement: Sizing line appears only for the caller's own plan
The plan block SHALL show "Max loss ... shares" only when the caller passes sizing computed for the logged-in owner, and the module MUST NOT look up a portfolio itself. With sizing present but zero shares it SHALL say the loss budget or position limit is too small for one share.

#### Scenario: Sizing passed
- **WHEN** sizing with shares is supplied to the block
- **THEN** the block shows max loss, share count, position value and which cap limited it

#### Scenario: No sizing supplied
- **WHEN** no sizing is passed
- **THEN** no max-loss text appears

<!-- tests: tests/test_trade_plan.py::test_plan_block_html_for_planned_no_plan_and_missing_rows -->
