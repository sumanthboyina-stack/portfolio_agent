# prediction-validation Specification

## Purpose
Measures how well stored predictions turned out once their horizon matured: outcome assignment, rolling accuracy and probability metrics, baselines, per-role and per-instrument-type comparison, scorecard verdicts, and the Validation pages. Validation reads only shared and legacy predictions, never private ones. Baseline at HEAD `ca450dd`. Beat-the-benchmark accuracy for long horizons, effective N, provisional marks and calibration analysis belong to `long-horizon-and-calibration`; the horizon edges themselves belong to `prediction-composition-and-buckets`.

## Requirements

### Requirement: Matured predictions are evaluated from real closing prices
The nightly evaluation SHALL score every pending prediction whose evaluation date is today or earlier, using its stored start price (else the close on its as-of date) and the close on its evaluation date. It MUST NOT call an LLM and MUST skip a non-trading day unless forced.

#### Scenario: Matured prediction scored
- **WHEN** a pending prediction matures and both closes are available
- **THEN** its actual return is end close divided by start close minus one
- **AND** the benchmark return is SPY's return over the same dates and the excess return is the difference
- **AND** the row is stored as evaluated with its direction, bucket, outcome label, outcome score, Brier score and log-loss

#### Scenario: Non-trading day
- **WHEN** the evaluation runs on a non-trading day without force
- **THEN** it returns skipped and evaluates nothing

<!-- tests: NONE -->

### Requirement: Missing prices mark a row data_missing
When a start or end close is unavailable (or the start price is zero), the system SHALL store the prediction with evaluation status `data_missing`, actual direction `UNKNOWN`, outcome `data_missing` and score 0, and MUST NOT count it in accuracy metrics.

#### Scenario: End close unavailable
- **WHEN** the end close cannot be fetched for a matured prediction
- **THEN** the row is stored as `data_missing` and the run continues with the next prediction

<!-- tests: NONE -->

### Requirement: Predictions are scored with the edges they were made under
Direction and bucket classification SHALL use the bucket edges of the prediction's own horizon and its stored `bucket_edges_version`, never a global constant. Changing the edges MUST NOT rescore stored rows.

#### Scenario: Legacy row
- **WHEN** a row has no edges version
- **THEN** it is scored with version 1 edges (FLAT within ±1% at 5 days)

#### Scenario: Edges changed later
- **WHEN** an admin changes the edges after a row was scored
- **THEN** the row's stored outcome and bucket are unchanged

<!-- tests: tests/test_bucket_edges.py::test_legacy_rows_are_scored_with_version_1_and_new_rows_with_their_version, tests/test_bucket_edges.py::test_changing_edges_does_not_rescore_old_rows, tests/test_bucket_edges.py::test_no_scoring_code_uses_the_fixed_flat_threshold, tests/test_validation_engine.py::test_actual_return_bucket_boundaries -->

### Requirement: Outcome labels and scores
Each evaluated prediction SHALL receive exactly one label with a fixed score: `strong_correct` 1.0, `flat_correct` 0.8, `directionally_correct` 0.7, `wrong_minor` 0.3, `wrong_significant` 0.0. The correct labels are `strong_correct`, `directionally_correct` and `flat_correct`.

#### Scenario: Correct direction inside the stated range
- **WHEN** the predicted and actual directions match and the actual return is inside the stated range
- **THEN** the label is `strong_correct`

#### Scenario: Matching FLAT call
- **WHEN** both directions are FLAT and the return is outside the stated range
- **THEN** the label is `flat_correct`

#### Scenario: Wrong direction
- **WHEN** the directions differ and the absolute return exceeds the horizon's significant edge
- **THEN** the label is `wrong_significant`; otherwise it is `wrong_minor`

<!-- tests: tests/test_validation_engine.py::test_score_outcome_strong_correct, tests/test_validation_engine.py::test_score_outcome_directionally_correct_but_outside_range, tests/test_validation_engine.py::test_score_outcome_matching_flat_out_of_range_is_flat_correct, tests/test_validation_engine.py::test_score_outcome_matching_flat_in_range_is_strong_correct, tests/test_validation_engine.py::test_score_outcome_flat_call_on_a_move_is_wrong, tests/test_validation_engine.py::test_score_outcome_wrong_significant, tests/test_validation_engine.py::test_score_outcome_wrong_minor -->

### Requirement: Outcome relabeling never changes right or wrong
The relabel backfill SHALL re-derive outcome labels from stored fields only (no price fetching), SHALL be idempotent, and MUST skip any row whose new label would flip it between correct and wrong, counting it as skipped.

#### Scenario: Rerun
- **WHEN** the backfill is run twice
- **THEN** the second run changes nothing

#### Scenario: Would flip correctness
- **WHEN** a recomputed label would turn a wrong row into a correct one
- **THEN** the row is left unchanged and counted in the skipped-flip total

<!-- tests: tests/test_validation_engine.py::test_recompute_outcome_labels_is_idempotent_and_guards_correctness, tests/test_validation_engine.py::test_label_fix_does_not_change_headline_accuracy -->

### Requirement: Binary probability scores per prediction
Each evaluated prediction with a full five-bucket distribution SHALL store a binary Brier score and log-loss for "up" versus "not up", where p(up) is the moderate-up plus strong-up share of the normalized distribution and "up" means the return exceeds the row's FLAT edge. Log-loss MUST clip probabilities at 1e-6. A missing distribution stores neither.

#### Scenario: Known values
- **WHEN** a row has p(up) of 0.7 and the actual return is above the FLAT edge
- **THEN** its Brier score is 0.09

#### Scenario: Missing distribution
- **WHEN** any bucket probability is missing
- **THEN** the Brier score and log-loss are null

<!-- tests: tests/test_validation_engine.py::test_extract_p_up_computes_upside_probability, tests/test_validation_engine.py::test_extract_p_up_normalizes_against_fractional_total, tests/test_validation_engine.py::test_compute_distribution_metrics_known_values, tests/test_validation_engine.py::test_compute_distribution_metrics_actual_down, tests/test_validation_engine.py::test_compute_distribution_metrics_missing_distribution_returns_none_none -->

### Requirement: Five-bucket probability scores
The five-bucket Brier score SHALL be the sum over buckets of squared differences between the normalized probabilities and the outcome indicator (range 0 to 2), and the five-bucket log-loss SHALL be the negative log of the probability of the bucket the return fell in, floored at 1e-6. A missing bucket probability yields no score.

#### Scenario: Normalized totals
- **WHEN** probabilities are stored as integers summing to about 100 or as fractions summing to 1
- **THEN** both give the same score

<!-- tests: tests/test_validation_engine.py::test_brier_5bucket_hand_computed, tests/test_validation_engine.py::test_5bucket_scores_normalise_totals, tests/test_validation_engine.py::test_5bucket_logloss_hand_computed_and_clipped, tests/test_validation_engine.py::test_5bucket_missing_bucket_is_none -->

### Requirement: Probability baselines are the sample's own base rates
Probability skill SHALL be 1 minus the system's score divided by the baseline score of always forecasting the sample's own base rates: for the binary score p(1-p) and binary entropy with p the empirical up rate, and for the five-bucket score the sum of q(1-q) and the entropy of the empirical bucket frequencies. Rows without a full distribution MUST be skipped.

#### Scenario: Skill sign
- **WHEN** the system's Brier score is below the baseline
- **THEN** the skill is positive

#### Scenario: No distribution
- **WHEN** no row has a full distribution
- **THEN** the result has n of 0 and no binary or five-bucket block

<!-- tests: tests/test_validation_metrics.py::test_probability_baselines_hand_computed, tests/test_validation_metrics.py::test_probability_baselines_skip_rows_without_distribution, tests/test_validation_metrics.py::test_probability_baseline_skill_sign -->

### Requirement: Calibration summary carries the fixed baselines
The calibration read SHALL report the overall binary Brier score and log-loss with the constants 0.25 (coin flip) and ln 2 as baselines, a ten-bin reliability table of stated p(up) against actual hit rate, conviction-band accuracy, and per-horizon Brier and log-loss for horizons 5, 21, 63 and 250. It SHALL accept an optional edges version that restricts rows to that version.

#### Scenario: Edges version filter
- **WHEN** the read is asked for edges version 2
- **THEN** only rows made under version 2 are included

<!-- tests: NONE -->

### Requirement: Rolling metrics are recomputed nightly
The nightly job SHALL compute, for lookbacks of 30, 90 and 365 days, per horizon, segment, system version and edges cohort: directional accuracy, in-range share, mean excess return, mean error magnitude, high-conviction accuracy (conviction 7 or higher), low-conviction accuracy, mean Brier score, mean log-loss and the number of predictions.

#### Scenario: Conviction split
- **WHEN** rolling metrics are computed
- **THEN** rows with conviction of at least 7 count toward high-conviction accuracy and the rest toward low

#### Scenario: Brier fallback
- **WHEN** no row in a group has a stored Brier score
- **THEN** a conviction-based proxy is used for old rows

<!-- tests: NONE -->

### Requirement: Rolling segments
Every evaluated row SHALL be counted in the `all` segment and in exactly one of `opportunity` (trigger type `trending_opportunity`), `portfolio` (the ticker is held in any portfolio) or `watchlist`.

#### Scenario: Held ticker
- **WHEN** a non-opportunity row's ticker is currently held
- **THEN** it counts in `all` and `portfolio` only

<!-- tests: NONE -->

### Requirement: Same-day regenerations count once
Rolling metrics SHALL collapse each (ticker, horizon, as-of date) cohort to its latest version (highest id) before aggregating, and MUST leave each row's own stored fields unchanged.

#### Scenario: Two same-day versions
- **WHEN** a morning and an intraday prediction exist for one ticker, horizon and date
- **THEN** only the latest counts toward n and accuracy

<!-- tests: tests/test_scheduler_notifications.py::test_multiple_same_day_versions_count_once_in_validation_not_once_each, tests/test_scheduler_notifications.py::test_cohort_collapse_uses_the_latest_version_not_an_average, tests/test_scheduler_notifications.py::test_individual_rows_are_untouched_by_cohort_collapse -->

### Requirement: Rolling metrics exclude challengers and private rows
The stored rolling series SHALL read only shared and legacy-unclassified predictions and MUST exclude challenger rows. It keeps legacy random-model rows as a continuous history.

#### Scenario: Challenger row
- **WHEN** a challenger row shares a ticker, horizon and date with a champion row
- **THEN** the champion row is the one counted

<!-- tests: NONE -->

### Requirement: Cohorts never mix bucket edges or horizons
Metrics SHALL work on one horizon and one set of bucket edges at a time. Rows whose edges versions have identical edges for the horizon are one cohort, and a frame mixing horizons or differing edges MUST raise an error. Rolling metrics group by the canonical edges version.

#### Scenario: Mixed horizons
- **WHEN** a metric is given rows of two horizons
- **THEN** it raises an error naming the horizons

#### Scenario: Different edges at 250 days
- **WHEN** rows of edges version 1 and the seed version are mixed at 250 days
- **THEN** it raises an error
- **AND** the same two versions at 5 days are one cohort

<!-- tests: tests/test_validation_metrics.py::test_mixed_horizons_raise, tests/test_bucket_edges.py::test_a_cohort_mixing_different_edges_raises -->

### Requirement: Validation reads exclude private predictions
Every validation and dashboard read SHALL be restricted to shared-market and legacy-unclassified predictions, and MUST NOT include private-scope predictions.

#### Scenario: Private prediction
- **WHEN** a private-scope prediction has matured
- **THEN** it appears in no validation metric, model list or evaluated-predictions frame

<!-- tests: tests/test_validation_page_data.py::test_load_metric_series_excludes_private_predictions, tests/test_validation_page_data.py::test_compute_filtered_metrics_excludes_private_predictions, tests/test_validation_page_data.py::test_load_evaluated_predictions_df_excludes_private_predictions, tests/test_validation_page_data.py::test_get_model_names_excludes_private_predictions -->

### Requirement: Default validation counts champion and fallback only
Validation reads SHALL default to prediction roles `champion` and `fallback`. Legacy random-model rows MUST appear only when a caller asks for them, and challenger rows only in the role comparison.

#### Scenario: Legacy rows on the user page
- **WHEN** the only evaluated rows are legacy random-model rows
- **THEN** the Validation page shows no scored predictions

#### Scenario: Legacy opt-in
- **WHEN** a caller requests champion, fallback and legacy roles
- **THEN** legacy rows are included in that read

<!-- tests: tests/test_apex_champion.py::test_validation_views_default_to_champion_and_fallback, tests/test_apex_champion.py::test_user_validation_page_never_shows_legacy_rows, tests/test_validation_page_data.py::test_load_metric_series_includes_legacy_but_not_private -->

### Requirement: Role breakdown
The role breakdown SHALL return one row per (horizon, role) in the order champion, fallback, legacy random, challenger, each with n, accuracy, balanced accuracy, best naive baseline, and binary and five-bucket Brier skill.

#### Scenario: Roles side by side
- **WHEN** champion and challenger rows exist for a horizon
- **THEN** the breakdown has a row for each role at that horizon

<!-- tests: tests/test_apex_champion.py::test_role_breakdown_and_champion_vs_challenger_use_the_same_tickers_and_dates -->

### Requirement: Champion versus challenger is paired
The comparison SHALL score the production row (champion or fallback) and the challenger row only on the same (ticker, as-of date, horizon) keys, taking the latest row on each side, and report pairs, dates, models, both scores and a head-to-head tally of both right, production only, challenger only and both wrong. Horizons with no pairs MUST be omitted.

#### Scenario: Unpaired challenger
- **WHEN** a challenger row has no production row for the same key
- **THEN** it is not counted in the comparison

<!-- tests: tests/test_apex_champion.py::test_role_breakdown_and_champion_vs_challenger_use_the_same_tickers_and_dates, tests/test_apex_champion.py::test_validation_qa_roles_tab_compares_champion_and_challenger -->

### Requirement: Instrument-type breakdown
Validation SHALL break results down by horizon and instrument type with n, accuracy, best baseline, Brier skill and the count of leveraged or inverse rows (leverage factor other than 1). Rows with no recorded type MUST be grouped as `UNCLASSIFIED`.

#### Scenario: Pre-classification rows
- **WHEN** a prediction was stored without an instrument type
- **THEN** it counts under `UNCLASSIFIED`, never under `STOCK`

<!-- tests: tests/test_etf_routing.py::test_validation_engine_breakdown_by_instrument_type, tests/test_etf_routing.py::test_pandas_breakdown_by_instrument_type_uses_the_standard_yardsticks -->

### Requirement: Naive baselines on the same rows
Predictor comparison SHALL score the system against `always_up`, `always_down`, `always_flat`, `majority_class` (an in-sample oracle), `spy_same_window`, `spy_prior_window` and `momentum` on the rows where every available baseline is defined, plus expected `class_frequency_random` and `uniform_random`. The best baseline MUST exclude `spy_same_window` because it looks ahead.

#### Scenario: Baseline without prices
- **WHEN** a baseline is undefined for every row for lack of price data
- **THEN** it is listed as unavailable and the comparison still runs

<!-- tests: tests/test_validation_metrics.py::test_always_and_majority_and_spy_same_window_baselines, tests/test_validation_metrics.py::test_prior_window_baselines_use_prior_trading_days, tests/test_validation_metrics.py::test_expected_random_baselines, tests/test_validation_metrics.py::test_compare_predictors_common_subset_and_best_baseline, tests/test_validation_metrics.py::test_compare_predictors_restricts_to_rows_where_all_baselines_defined -->

### Requirement: Classification metrics
Accuracy, balanced accuracy and macro F1 SHALL be computed over the three classes UP, DOWN and FLAT. A class with no true positives MUST score an F1 of 0.

#### Scenario: Confusion matrix
- **WHEN** a confusion matrix is requested
- **THEN** it has predicted classes as rows and actual classes as columns, with predicted and actual class mix

<!-- tests: tests/test_validation_metrics.py::test_apex_metrics_hand_computed, tests/test_validation_metrics.py::test_confusion_matrix_and_mixes -->

### Requirement: Direction versus the market
Among UP and DOWN calls, the hit rate SHALL be the share whose sign matches the sign of the excess return versus SPY, reported next to the always-UP hit rate over all rows. FLAT calls MUST be reported but not scored.

#### Scenario: Row without excess return
- **WHEN** a row has no excess return
- **THEN** it is skipped

<!-- tests: tests/test_validation_metrics.py::test_beat_market_hit_rate_and_always_up_baseline, tests/test_validation_metrics.py::test_beat_market_skips_rows_without_excess_return -->

### Requirement: Scorecard has four checks per horizon
A scorecard SHALL cover one horizon and one edges cohort and contain four items: ranking (whether BUY and STRONG_BUY rows beat the rest versus SPY, by a date-block bootstrap with a fixed seed), direction, probabilities and ranges. It SHALL carry the cohort's bucket edges and edges version, ignore rows of other horizons, and use the same numbers as the underlying metrics.

#### Scenario: Other horizons ignored
- **WHEN** a scorecard is computed for 21 days on rows of several horizons
- **THEN** only the 21-day rows are used

#### Scenario: Ranges are information only
- **WHEN** the ranges item is built
- **THEN** its verdict is `Info only`

<!-- tests: tests/test_validation_scorecard.py::test_scorecard_numbers_are_the_existing_metric_numbers, tests/test_validation_scorecard.py::test_only_the_requested_horizon_is_used, tests/test_validation_metrics.py::test_bootstrap_is_date_blocked_not_row_level, tests/test_validation_metrics.py::test_ranking_report_splits_by_model_and_is_seeded -->

### Requirement: One verdict rule
A verdict SHALL be decided in one place: fewer than 10 distinct prediction dates gives `Too early`; no interval gives `Unproven`; an interval entirely better than the baseline gives `Working`, entirely worse gives `Not working`, and otherwise (including touching the baseline) `Unproven`. The evidence level SHALL be `Too early` below 10 dates, `Early` from 10 to 29 and `Established` from 30.

#### Scenario: Exactly ten dates
- **WHEN** a scorecard has exactly 10 prediction dates
- **THEN** it is allowed to give a verdict and its evidence level is `Early`

#### Scenario: Lower is better
- **WHEN** the metric is lower-is-better
- **THEN** an interval entirely below the baseline gives `Working`

<!-- tests: tests/test_validation_scorecard.py::test_verdict_boundaries, tests/test_validation_scorecard.py::test_verdict_lower_is_better_flips_direction, tests/test_validation_scorecard.py::test_verdict_threshold_is_configurable, tests/test_validation_scorecard.py::test_evidence_level_boundaries, tests/test_validation_scorecard.py::test_working_verdict_when_buy_clearly_better_and_enough_dates, tests/test_validation_scorecard.py::test_not_working_when_buy_clearly_worse, tests/test_validation_scorecard.py::test_too_early_below_min_dates_but_numbers_still_shown -->

### Requirement: Scorecards degrade instead of failing
Empty, short or malformed input and missing optional columns SHALL produce `Too early` items per check and MUST NOT raise. Numbers SHALL still be shown below the minimum dates.

#### Scenario: Empty frame
- **WHEN** a scorecard is computed on no rows
- **THEN** every item is `Too early` (ranges is `Info only`)

<!-- tests: tests/test_validation_scorecard.py::test_empty_or_short_or_malformed_input_yields_too_early_items, tests/test_validation_scorecard.py::test_missing_optional_columns_degrade_per_item, tests/test_validation_scorecard.py::test_no_non_buy_group_is_unproven_and_says_so -->

### Requirement: User Validation page
The Validation page SHALL show one headline sentence naming the horizon with the most evidence and its days of results, with a "Too early to judge" warning below 10 days and a "Still early" notice below 30. It MUST offer no filters or sidebar controls, show every horizon in a four-check grid, and never show legacy or challenger rows.

#### Scenario: Few dates
- **WHEN** a horizon has fewer than 10 dates
- **THEN** it still appears in the grid as too early

#### Scenario: Empty data
- **WHEN** there are no evaluated predictions
- **THEN** the page renders without error

<!-- tests: tests/test_validation_page.py::test_page_renders_with_empty_data, tests/test_validation_page.py::test_page_renders_four_cards_banner_and_charts_from_the_scorecard, tests/test_validation_page.py::test_every_card_number_is_the_scorecards_number, tests/test_validation_page.py::test_page_has_no_filters_and_nothing_lives_in_the_sidebar, tests/test_validation_page.py::test_every_horizon_is_a_column_of_the_grid_and_the_headline_names_the_best_one, tests/test_validation_page.py::test_a_horizon_with_few_dates_is_still_shown_in_the_grid_as_too_early -->

### Requirement: Validation QA page is admin only
The Validation QA page SHALL show "Admin only." and stop for any signed-in user who is not an admin. For admins it offers a role selector (default champion plus fallback, optionally plus legacy) and a Roles tab, the only place challenger rows appear.

#### Scenario: Non-admin
- **WHEN** a non-admin opens the page
- **THEN** it shows "Admin only." and renders nothing else

#### Scenario: Admin sees roles
- **WHEN** an admin opens the Roles tab with champion and challenger rows
- **THEN** it shows the champion-versus-challenger comparison on the same tickers and dates

<!-- tests: tests/test_validation_qa_page.py::test_qa_page_renders_all_tabs_with_empty_data, tests/test_apex_champion.py::test_validation_qa_roles_tab_compares_champion_and_challenger -->

### Requirement: Weekly failure-pattern analysis
The weekly analysis SHALL run at most once in seven days, skip with fewer than 30 evaluated predictions in the week, and mine only shared-scope, non-challenger rows. A segment is a confirmed failure only with at least 20 rows on each side, an accuracy gap of at least 15 points and a two-proportion p-value below 0.05.

#### Scenario: Already ran
- **WHEN** a weekly report exists from the last seven days
- **THEN** the analysis returns skipped

#### Scenario: Too little data
- **WHEN** fewer than 30 predictions were evaluated in the week
- **THEN** it returns skipped with the row count

#### Scenario: Shared rows only
- **WHEN** private-scope rows exist
- **THEN** pattern mining reads none of them

<!-- tests: tests/test_market_context_boundary.py::test_failure_pattern_mining_reads_only_shared_rows -->

### Requirement: Recurring segments become candidate guardrails
A confirmed segment SHALL be promoted to a candidate guardrail when it appears in at least 2 of the last 4 weekly reports, this week included. The latest report's candidates (up to 5 by default) are what later APEX calls receive as active failure patterns.

#### Scenario: Seen once
- **WHEN** a segment appears in only this week's report
- **THEN** it is not a candidate guardrail

<!-- tests: NONE -->

### Requirement: The language model only narrates confirmed segments
The weekly analysis SHALL ask one language model call, tried down the `flash` chain, to write a narrative of the confirmed segments only. Segment flags and candidate guardrails MUST come from the statistics, not the model, and a model failure MUST NOT lose them. This is the only language-model use in validation.

#### Scenario: No confirmed segment
- **WHEN** no segment clears the bar and fewer than 5 predictions were wrong
- **THEN** a report is stored saying no significant failure segments, without calling a model

<!-- tests: NONE -->

### Requirement: Trade-plan outcomes never disturb prediction scoring
After each evaluation run the system SHALL record outcomes of matured trade plans, and a failure there MUST NOT stop or change direction scoring.

#### Scenario: Plan outcome failure
- **WHEN** recording plan outcomes raises an error
- **THEN** direction evaluation still completes and returns its counts

<!-- tests: tests/test_plan_validation.py::test_evaluate_matured_predictions_records_plan_outcomes_without_touching_direction_scoring, tests/test_plan_validation.py::test_a_plan_outcome_failure_never_breaks_direction_evaluation -->

### Requirement: Dropped-news follow-up returns are backfilled
Each evaluation run SHALL fill the 5-day follow-up return of news-filter rows that were dropped 5 to 10 days ago and have none, and a failure MUST NOT fail the evaluation.

#### Scenario: Dropped ticker
- **WHEN** a ticker was dropped by the news filter 6 days ago
- **THEN** its follow-up return is the move from that date to today

<!-- tests: NONE -->

### Requirement: Validation computes no order or advice output
Validation SHALL only score past predictions and MUST NOT connect to a broker, place orders or produce trade instructions.

#### Scenario: Any validation run
- **WHEN** a validation job or page runs
- **THEN** it reads and writes only prediction, metric and report data

<!-- tests: NONE -->
