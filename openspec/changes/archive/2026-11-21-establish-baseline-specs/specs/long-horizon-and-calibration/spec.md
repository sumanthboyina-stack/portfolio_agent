# Spec Delta

## Purpose

Defines how APEX judges 63-day and 250-day forecasts, where the 3-class label mostly tracks market drift, and how it checks whether its parallel scoring systems predicted forward returns. Covers beat-the-benchmark accuracy with an effective sample size, provisional interim marks, the daily scoring snapshot and score calibration. Baseline at HEAD `ca450dd`; the general validation pipeline is owned by `prediction-validation`.

## ADDED Requirements

### Requirement: Beat-the-benchmark accuracy
For a single horizon's matured predictions, the system SHALL compute beat-the-benchmark accuracy over UP and DOWN calls that have an excess return over SPY: an UP call is a hit when its excess return is above 0, a DOWN call when it is below 0. FLAT calls MUST NOT be scored. Each horizon report SHALL carry this figure.

#### Scenario: Hand-computed accuracy
- **WHEN** four matured calls exist (UP with +10% excess, UP with -2%, DOWN with -4%, DOWN with +3%) plus one FLAT call with +50%
- **THEN** the raw call count is 4 and the accuracy is 50%
- **AND** the FLAT call is not counted

#### Scenario: Always-UP baseline
- **WHEN** excess return is above 0 on 3 of 5 matured rows, FLAT rows included
- **THEN** the always-UP rate is 60%

#### Scenario: Rows without an excess return
- **WHEN** a matured row has no excess return over SPY
- **THEN** it is left out of the figure and of the always-UP baseline

#### Scenario: Horizon report carries it
- **WHEN** a horizon report is built for 63-day rows
- **THEN** it contains the beat-the-benchmark figure with the raw call count

#### Scenario: No rows
- **WHEN** no matured rows exist
- **THEN** the raw count is 0, the accuracy is empty and the figure is flagged too early

<!-- tests: tests/test_long_horizon.py::test_beat_benchmark_accuracy_hand_computed, tests/test_long_horizon.py::test_horizon_report_carries_beat_benchmark, tests/test_long_horizon.py::test_beat_benchmark_with_no_rows_is_empty_and_too_early -->

### Requirement: Headline uses non-overlapping calls
The headline beat-the-benchmark accuracy and its Wilson 95% interval SHALL be computed on the non-overlapping calls (the effective n), while the raw accuracy over every call is reported beside it. A call is kept only when its forecast date is on or after the previous kept call's evaluation date for the same ticker and horizon.

#### Scenario: Daily calls on one ticker
- **WHEN** 28 daily 250-day calls on one ticker all overlap
- **THEN** the raw count is 28 and the effective n is 1

#### Scenario: Independent tickers
- **WHEN** 30 independent tickers each have one matured UP call that beat SPY
- **THEN** the effective n is 30, the accuracy is 100% and the interval has a lower bound and an upper bound of 100%

#### Scenario: Window touching the next forecast
- **WHEN** a call's forecast date equals the previous kept call's evaluation date
- **THEN** both calls are kept

#### Scenario: Missing evaluation date
- **WHEN** a row has no evaluation date
- **THEN** its window is taken as the forecast date plus the horizon scaled by 7/5 in calendar days, rounded up

<!-- tests: tests/test_long_horizon.py::test_effective_n_counts_non_overlapping_calls_and_gates_too_early, tests/test_validation_metrics.py::test_nonoverlap_drops_overlapping_and_keeps_boundary_touching, tests/test_validation_metrics.py::test_nonoverlap_missing_evaluation_date_falls_back_to_horizon, tests/test_validation_metrics.py::test_wilson_ci_known_values -->

### Requirement: Too early to judge below 30 effective calls
The system SHALL flag a beat-the-benchmark figure as too early while its effective n is below 30, and the flag MUST accompany the numbers, which are still shown. The Validation QA page SHALL display a warning with the effective n and the minimum.

#### Scenario: Below the minimum
- **WHEN** the effective n is 29
- **THEN** the figure is flagged too early
- **AND** the Validation QA long-horizon block states that the numbers are shown for completeness, not as evidence

#### Scenario: At the minimum
- **WHEN** the effective n is 30
- **THEN** the figure is not flagged

<!-- tests: tests/test_long_horizon.py::test_effective_n_counts_non_overlapping_calls_and_gates_too_early, tests/test_validation_metrics.py::test_effective_sample_counts_accuracy_and_warning -->

### Requirement: Long-horizon block on the QA page
The Validation QA page SHALL show the long-horizon block only when the selected horizon is 63 or 250 days, with a final table (all matured calls, effective calls with the Wilson interval, and the always-UP baseline) above a separately labelled provisional section.

#### Scenario: Long horizon selected
- **WHEN** an admin selects the 250-day horizon
- **THEN** the page shows the final beat-the-benchmark table and a "Provisional" heading marked as not included in any number above

#### Scenario: Short horizon selected
- **WHEN** an admin selects the 5-day horizon
- **THEN** no long-horizon block is shown

<!-- tests: NONE -->

### Requirement: Provisional marks for pending long-horizon calls
Each evening the system SHALL record, for every pending 63-day and 250-day prediction made before today whose evaluation date is after today, the return so far, SPY's return so far, the excess and the trading days elapsed, in `prediction_provisional_marks`. Only the latest mark per prediction is kept, and a failure on one prediction MUST NOT stop the rest.

#### Scenario: Long-horizon rows only
- **WHEN** marks are updated with one pending 250-day row and one pending 5-day row
- **THEN** only the 250-day row is marked
- **AND** the return so far is the current close over the start price minus 1, and the excess is that return minus SPY's return over the same dates

#### Scenario: Replace, not append
- **WHEN** a second mark is written for the same prediction on a later day
- **THEN** only the later mark remains

#### Scenario: Price unavailable
- **WHEN** a pending row has no start price or no current close
- **THEN** it is skipped and counted as skipped, and the other rows are still marked

#### Scenario: Evening run
- **WHEN** the evening batch runs after outcome assignment
- **THEN** it updates provisional marks and a failure there is logged and does not stop the batch

<!-- tests: tests/test_long_horizon.py::test_update_provisional_marks_marks_pending_long_rows_only_with_no_live_prices, tests/test_long_horizon.py::test_marks_live_in_their_own_table_and_never_touch_final_columns -->

### Requirement: Provisional marks never become results
Provisional marks SHALL live in their own table, MUST NOT write to `predictions` (no outcome, status, bucket or score), and MUST NOT be read by any final metric. A prediction's mark SHALL disappear from the report as soon as the prediction is no longer pending.

#### Scenario: Final columns untouched
- **WHEN** a mark is written for a pending prediction
- **THEN** every column of its `predictions` row is unchanged

#### Scenario: Matured row drops its mark
- **WHEN** a prediction with a stored mark becomes evaluated
- **THEN** the provisional marks for that horizon no longer include it

#### Scenario: Final report unchanged by marks
- **WHEN** a mark with a large excess is written
- **THEN** the horizon report's beat-the-benchmark figure for matured rows is identical to before

<!-- tests: tests/test_long_horizon.py::test_marks_live_in_their_own_table_and_never_touch_final_columns, tests/test_long_horizon.py::test_a_matured_row_no_longer_shows_a_provisional_mark, tests/test_long_horizon.py::test_provisional_marks_do_not_change_the_final_report -->

### Requirement: Provisional standing report
The provisional report SHALL be returned under its own keys (`provisional` true, `accuracy_so_far`) so it cannot be mistaken for the final figure. It SHALL count calls with an excess so far and a direction of UP or DOWN, apply the same non-overlap rule and the same too-early threshold of 30, and report the mean excess and the average fraction of the window elapsed (capped at 100%).

#### Scenario: Labelled provisional
- **WHEN** five pending UP calls each with +2% excess so far are reported
- **THEN** the result is marked provisional and has no `accuracy` or `final` key
- **AND** the count is 5, the accuracy so far is 100% and it is flagged too early

#### Scenario: Fraction elapsed
- **WHEN** marks show 50 trading days elapsed on a 250-day horizon
- **THEN** the mean fraction elapsed is 20%

#### Scenario: No marks
- **WHEN** no marks exist
- **THEN** the count is 0

#### Scenario: Roles and scopes
- **WHEN** the QA page builds the provisional report
- **THEN** it reads marks for the default validation roles (champion and fallback) and the legacy-review scopes only

<!-- tests: tests/test_long_horizon.py::test_provisional_report_is_labelled_provisional_and_uses_its_own_keys -->

### Requirement: Daily scoring snapshot
The morning batch SHALL store, for each ticker with a shared, non-challenger prediction whose forecast date is today, one row per ticker and date in `scoring_snapshots` with the entry price and five scores: APEX, Valuation, Opportunity, Portfolio Fit and Final. A second write for the same ticker and date SHALL replace the first. The step MUST use no language model and a failure on one ticker MUST NOT stop the rest.

#### Scenario: One row per ticker per day
- **WHEN** a snapshot is written twice for the same ticker and date
- **THEN** one row exists, holding the second write's values

#### Scenario: Private forecasts excluded
- **WHEN** a private forecast has today's forecast date
- **THEN** its ticker is not snapshotted

#### Scenario: No predictions today
- **WHEN** there are no qualifying predictions today
- **THEN** nothing is written and the phase is recorded as empty

#### Scenario: Latest row per ticker
- **WHEN** a ticker has several qualifying rows today
- **THEN** the most recently created one is used

<!-- tests: tests/test_scoring_snapshot_db.py::test_upsert_and_get_snapshots_for_date, tests/test_scoring_snapshot_db.py::test_upsert_is_idempotent_per_ticker_and_date, tests/test_review_findings.py::test_scoring_snapshot_excludes_private_rows_even_without_a_trigger_type -->

### Requirement: Final score blend
The Final score SHALL be a fixed, documented blend normalised to 0-100: 30% APEX (composite score over 10), 20% Valuation (over 10), 30% Opportunity and 20% Portfolio Fit, rounded to one decimal. A missing APEX score MUST count as 1.0, a missing Valuation as 5.5, a missing Opportunity as 0 and a missing Portfolio Fit as 50. The component scores SHALL be stored beside it.

#### Scenario: Blend arithmetic
- **WHEN** APEX is 8.0, Valuation 5.0, Opportunity 70 and Portfolio Fit 60
- **THEN** Final is 30% x 80 + 20% x 50 + 30% x 70 + 20% x 60 = 67.0

#### Scenario: Missing components
- **WHEN** Valuation, Opportunity and Portfolio Fit are all missing
- **THEN** Final uses 5.5, 0 and 50 for them

<!-- tests: NONE -->

### Requirement: Snapshot outcomes are filled when each horizon matures
The evening batch SHALL fill, independently for each of 30, 60, 90 and 250 days, the realised return and SPY's return from the snapshot date to the date that many calendar days later, for snapshots old enough and not yet filled. A snapshot older than the horizon plus 90 days MUST NOT be retried, and a snapshot with no entry price or no exit price SHALL be counted as data missing.

#### Scenario: Matured 30-day outcome
- **WHEN** a snapshot's 30-day date has passed with entry 100, exit 120, SPY entry 100 and SPY exit 110
- **THEN** its 30-day return is 20% and its 30-day benchmark return is 10%

#### Scenario: Age window
- **WHEN** a snapshot is younger than the horizon, or older than the horizon plus 90 days
- **THEN** it is not selected for that horizon

#### Scenario: Horizons are independent
- **WHEN** a snapshot's 30-day return is filled but its 250-day date is not due
- **THEN** only the 30-day return is set

#### Scenario: Missing price
- **WHEN** a due snapshot has no entry price or no exit close
- **THEN** it is counted as data missing and left unfilled

<!-- tests: tests/test_scoring_snapshot_db.py::test_get_snapshots_needing_outcome_respects_age_window, tests/test_scoring_snapshot_db.py::test_fill_matured_outcomes_writes_return_and_benchmark -->

### Requirement: Score calibration report
For a chosen horizon the system SHALL report, for each of the five scores, the number of matured snapshots, the Pearson correlation of the score with the realised return, and the average return of the top and bottom fifth by score and their spread. A score with fewer than 20 usable pairs MUST be reported as insufficient data without statistics, and rows missing the score or the return MUST be ignored.

#### Scenario: Insufficient data
- **WHEN** fewer than 20 snapshots have both a score and a return
- **THEN** the score is reported with its count and insufficient data

#### Scenario: Known relationship
- **WHEN** returns rise exactly with the score
- **THEN** the correlation is 1 and the top-fifth average exceeds the bottom-fifth average

#### Scenario: Constant score
- **WHEN** a score has no variance
- **THEN** its correlation is empty

#### Scenario: All five scores covered
- **WHEN** the report is built with no matured snapshots
- **THEN** it lists APEX, Valuation, Opportunity, Portfolio Fit and Final, each as insufficient data

<!-- tests: tests/test_calibration_analysis.py::test_pearson_correlation_perfect_positive, tests/test_calibration_analysis.py::test_pearson_correlation_perfect_negative, tests/test_calibration_analysis.py::test_pearson_correlation_none_with_no_variance, tests/test_calibration_analysis.py::test_score_vs_return_insufficient_data_below_min_sample, tests/test_calibration_analysis.py::test_score_vs_return_quintile_spread_matches_known_correlation, tests/test_calibration_analysis.py::test_score_vs_return_ignores_rows_missing_either_field, tests/test_calibration_analysis.py::test_compute_calibration_report_covers_all_score_columns -->

### Requirement: Scoring snapshot and calibration views
The Validation QA page SHALL show the latest snapshot date's scoring table (ticker and the five scores) with a message while no snapshot exists, and a score-versus-returns view for a selectable horizon of 30, 60, 90 or 250 days that shows a message while no horizon has enough matured snapshots.

#### Scenario: No snapshots yet
- **WHEN** no snapshot exists
- **THEN** the page says the table fills once the morning batch's score calibration phase runs

#### Scenario: Not enough matured snapshots
- **WHEN** no score has enough matured snapshots at the chosen horizon
- **THEN** the page says there are not enough matured snapshots yet

<!-- tests: NONE -->

### Requirement: Long-horizon and calibration computations are deterministic
Beat-the-benchmark accuracy, the effective sample, provisional marks, scoring snapshots and score calibration SHALL be computed with no language model, from stored prediction rows, stored snapshots and closing prices.

#### Scenario: No model call
- **WHEN** any of these computations runs
- **THEN** no language model is called

<!-- tests: NONE -->
