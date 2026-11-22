# prediction-composition-and-buckets Specification

## Purpose
Defines how analyst scores become a stored, scorable prediction: the dynamic source weights, conviction guardrails, the five recommendation labels, the horizon-scaled return buckets and their versions, the prediction table and its two write doors, the NYSE trading calendar used for evaluation dates, and the read-only composition of a stored forecast with private portfolio fit. Baseline at HEAD `ca450dd`.

## Requirements

### Requirement: Supported horizons
The system SHALL forecast over exactly four horizons: 5, 21, 63 and 250 trading days. Weights, return-bucket edges and market contexts are defined only for these horizons, and a request for any other horizon MUST be refused where a table is validated or a context is built.

#### Scenario: Unsupported horizon in a market context
- **WHEN** a market context is built for a horizon other than 5, 21, 63 or 250
- **THEN** the build is refused

#### Scenario: Edge table missing a horizon
- **WHEN** a return-bucket table omits one of the four horizons or adds a fifth
- **THEN** validation fails and names the horizon

<!-- tests: tests/test_market_context_boundary.py::test_market_context_rejects_unsupported_horizons, tests/test_bucket_edges.py::test_edges_validation_and_admin_only_and_reset -->

### Requirement: Horizon base weights
The weight engine SHALL start each horizon from a base weight per source (news, research, macro, fundamentals, valuation) that sums to 1.0, with news dominating at 5 days and fundamentals plus valuation dominating at 250 days. A horizon outside the table uses the default base row.

#### Scenario: Seed base weights
- **WHEN** the engine settings are at their seed
- **THEN** 5d is news 0.55, research 0.23, macro 0.13, fundamentals 0.05, valuation 0.04
- **AND** 21d is 0.28, 0.27, 0.13, 0.20, 0.12
- **AND** 63d is 0.13, 0.20, 0.17, 0.30, 0.20
- **AND** 250d is 0.05, 0.12, 0.18, 0.35, 0.30

#### Scenario: Valuation share rises with horizon
- **WHEN** the base rows are compared across horizons
- **THEN** valuation's share increases from 5d to 250d

<!-- tests: tests/test_weight_engine.py::test_horizon_base_weights_sum_to_one, tests/test_weight_engine.py::test_valuation_weight_share_increases_with_horizon, tests/test_market_context_boundary.py::test_fixed_input_weights_match_pre_refactor_golden_values -->

### Requirement: Weight floor and normalization
Per-horizon weights SHALL be floored at the configured weight floor (seed 5%) before normalization and then normalized so they sum to 1.0 within rounding at three decimals. The floor MUST be an administrator setting between 0.0 and 0.3.

#### Scenario: Zeroed source keeps the floor
- **WHEN** a source's adjusted weight falls to zero for a stock
- **THEN** its final weight is at least the floor and all weights sum to 1.0

#### Scenario: Floor out of range
- **WHEN** an administrator sets the weight floor above 0.3 or below 0
- **THEN** the change is refused

<!-- tests: tests/test_weight_engine.py::test_compute_dynamic_weights_for_horizon_sums_to_one_and_respects_floor, tests/test_weight_engine.py::test_normalize_applies_floor_to_zeroed_source, tests/test_weight_engine.py::test_normalize_floor_is_pre_normalization_only -->

### Requirement: News signal adjusts news weight
The weight engine SHALL boost the news weight for material events found in news from the last 2 days and SHALL reduce it when news is absent or inert. Earnings results and leadership changes boost by 4.5, earnings previews, M&A and regulatory events by 2.5, macro news by 1.0, and mild sentiment by 0.4.

#### Scenario: No news data
- **WHEN** a ticker has no news
- **THEN** its news weight is reduced by a 0.60 penalty and redistributed to the other sources

#### Scenario: Earnings print in the last two days
- **WHEN** recent news contains results keywords such as beat or miss
- **THEN** the event is `EARNINGS_RELEASE` with a 4.5 boost

#### Scenario: Executive change
- **WHEN** recent news contains a leadership-change keyword and no earnings keyword
- **THEN** the event is `LEADERSHIP_CHANGE`

#### Scenario: News present but inert
- **WHEN** news exists with no event keyword and average sentiment magnitude of 0.2 or less
- **THEN** there is no boost and a 0.35 penalty

<!-- tests: tests/test_weight_engine.py::test_news_signal_no_data_applies_real_penalty, tests/test_weight_engine.py::test_news_signal_detects_earnings_release, tests/test_weight_engine.py::test_news_signal_detects_leadership_change -->

### Requirement: Macro signal adjusts macro weight
The weight engine SHALL boost the macro weight from the macro snapshot: VIX above 30 by 2.5, VIX above 22 by 1.0, S&P 500 one-month change below -5 by 1.0, above +6 by 0.4, a yield-curve spread below -0.5 by 1.0, and a `RISK_OFF` regime hint by 1.0. Without a snapshot the boost is zero.

#### Scenario: Extreme volatility
- **WHEN** VIX is above 30
- **THEN** the event is `EXTREME_VOLATILITY` with a 2.5 boost

#### Scenario: Elevated volatility
- **WHEN** VIX is above 22 and not above 30
- **THEN** the event is `ELEVATED_VOLATILITY` with a 1.0 boost

#### Scenario: No snapshot
- **WHEN** no macro snapshot is available
- **THEN** the event is `NO_DATA` and the macro weight is unchanged

<!-- tests: tests/test_weight_engine.py::test_macro_signal_extreme_volatility, tests/test_weight_engine.py::test_macro_signal_elevated_volatility, tests/test_weight_engine.py::test_macro_signal_neutral, tests/test_weight_engine.py::test_macro_signal_no_data -->

### Requirement: Research signal adjusts research weight
The weight engine SHALL boost the research weight when an analyst action is under 7 days old (by 1.0 with three or more actions in 7 days, else 0.4) and SHALL penalize it when research is missing (0.35) or over 30 days old (0.15).

#### Scenario: Missing research
- **WHEN** no research data is stored for the ticker
- **THEN** research weight takes a 0.35 penalty

#### Scenario: Stale research
- **WHEN** research data is over 30 days old with no recent analyst action
- **THEN** research weight takes a 0.15 penalty

<!-- tests: NONE -->

### Requirement: Fundamentals and valuation staleness penalties
The weight engine SHALL penalize the fundamentals and valuation weights by age: none under 30 days, 0.15 from 30 to under 90 days, 0.35 at 90 days or more, 0.35 when missing and 0.15 when the age cannot be read. During an earnings event without a filing or DCF from the last 3 days the penalty SHALL be 0.60.

#### Scenario: Fresh data
- **WHEN** the fundamentals are under 30 days old
- **THEN** the penalty is 0

#### Scenario: Missing data
- **WHEN** no fundamentals are stored
- **THEN** the penalty is 0.35

#### Scenario: Earnings event without a new filing
- **WHEN** news shows an earnings event and the latest filing is older than 3 days
- **THEN** the fundamentals penalty is 0.60

<!-- tests: tests/test_weight_engine.py::test_fundamentals_penalty_fresh_data, tests/test_weight_engine.py::test_fundamentals_penalty_stale_data, tests/test_weight_engine.py::test_fundamentals_penalty_missing_data, tests/test_weight_engine.py::test_fundamentals_penalty_heavy_during_earnings_without_fresh_filing, tests/test_weight_engine.py::test_valuation_penalty_fresh_data, tests/test_weight_engine.py::test_valuation_penalty_stale_data, tests/test_weight_engine.py::test_valuation_penalty_missing_data, tests/test_weight_engine.py::test_valuation_penalty_heavy_during_earnings_without_fresh_dcf -->

### Requirement: Fund instruments exclude company sources
For ETF, ETN, mutual fund and target-date fund instrument types the weight engine SHALL set fundamentals and valuation weights to exactly 0 on every horizon, with no floor, and renormalize the remaining sources to 1.0 with the floor applied to them. Any other or missing type MUST leave every number identical to the stock path.

#### Scenario: Fund weights
- **WHEN** weights are computed for an ETF on any horizon
- **THEN** fundamentals and valuation are 0.0 and research, macro and news sum to 1.0 with each at least the floor

#### Scenario: Stock path unchanged
- **WHEN** the instrument type is a stock, unknown or absent
- **THEN** the weights equal those computed without an instrument type

#### Scenario: No data cap for excluded sources
- **WHEN** the legacy single-horizon result is computed for a fund
- **THEN** no data cap exists for fundamentals or valuation and the rationale says they do not apply to a fund

<!-- tests: tests/test_etf_routing.py::test_fund_weights_zero_fundamentals_and_valuation_and_sum_to_one_on_every_horizon, tests/test_etf_routing.py::test_fund_weights_respect_the_floor_for_the_remaining_sources, tests/test_etf_routing.py::test_floor_never_resurrects_an_excluded_source_and_rounding_remainder_goes_to_a_remaining_one, tests/test_etf_routing.py::test_stock_path_is_numerically_unchanged_whatever_the_non_fund_type, tests/test_etf_routing.py::test_fund_result_has_no_data_cap_for_excluded_sources_and_says_why -->

### Requirement: Data caps on analyst scores
The weight engine SHALL return a score cap per source: 5 when that source's data is missing, 7 for fundamentals or valuation under the 0.60 earnings penalty, and 10 otherwise. Macro is always capped at 10.

#### Scenario: Missing news
- **WHEN** a ticker has no news
- **THEN** the news cap is 5

#### Scenario: Complete data
- **WHEN** all sources have data and no earnings penalty applies
- **THEN** every cap is 10

<!-- tests: tests/test_etf_routing.py::test_fund_result_has_no_data_cap_for_excluded_sources_and_says_why -->

### Requirement: Weight regime label
The weight engine SHALL label each computation with a regime: the news event when it is `EARNINGS_RELEASE`, `LEADERSHIP_CHANGE` or `MA_EVENT`; else `EXTREME_VOLATILITY`, `RISK_OFF` or `INVERTED_CURVE` from macro; else `MARKET_SELLOFF`; else `HIGH_SIGNAL` when a news or macro boost is 2.5 or more; else `QUIET_DAY` when no boost applies; else `MIXED`.

#### Scenario: Quiet day
- **WHEN** news, macro and research boosts are all zero and no earlier rule matches
- **THEN** the regime is `QUIET_DAY`

#### Scenario: Earnings wins over macro
- **WHEN** news shows an earnings release and VIX is above 30
- **THEN** the regime is `EARNINGS_RELEASE`

<!-- tests: NONE -->

### Requirement: Weights are deterministic and profile-free
The weight engine SHALL compute weights only from the explicit horizons, news, research, fundamentals, valuation, macro snapshot and instrument type it is given, with no language model and no investor profile. Identical inputs MUST give identical weights for every caller. A personal horizon preference MAY filter horizons only after generation, for display.

#### Scenario: Golden values
- **WHEN** the fixed golden inputs are weighed
- **THEN** the results equal the stored golden weights for the legacy row and horizons 5, 21 and 63

#### Scenario: Preference filters display only
- **WHEN** a preferred-horizon filter is applied to a list of horizons
- **THEN** only those horizons remain in their original order and no weight changes

#### Scenario: Unparseable preference
- **WHEN** the preferred labels are empty or unparseable
- **THEN** no filtering is applied

<!-- tests: tests/test_market_context_boundary.py::test_fixed_input_weights_match_pre_refactor_golden_values, tests/test_weight_engine.py::test_generation_never_reads_the_profile_and_takes_explicit_horizons, tests/test_weight_engine.py::test_filter_preferred_horizons_is_pure_and_display_only -->

### Requirement: Weight settings are database-backed with a seed fallback
The weight engine SHALL compute with the weights, floor and guardrail thresholds stored in engine settings, and MUST fall back to its built-in seed values when the settings cannot be read, so a settings failure never stops a run.

#### Scenario: Settings unreadable
- **WHEN** the engine settings read raises
- **THEN** the seed base weights, floor 0.05, no-news threshold 7.0 and cap 5.0, bounce threshold -0.05 and cap 5.0 are used

<!-- tests: NONE -->

### Requirement: Conviction guardrails
After the model responds and before storage, the system SHALL cap the conviction of UP calls: to the no-news cap (seed 5.0) when there is no news and conviction is at least the no-news threshold (seed 7.0), and to the bounce cap (seed 5.0) when the trailing 10-day return is below the bounce threshold (seed -5%). Each applied cap MUST add a flag, and non-UP calls MUST be unchanged.

#### Scenario: No-news bullish call capped
- **WHEN** an UP call has conviction 8.0, no news and a 1% trailing return
- **THEN** conviction becomes 5 and the flag `no_news_bullish_capped` is recorded

#### Scenario: Call with news is untouched
- **WHEN** an UP call has conviction 8.0 and news is present with a 12% trailing return
- **THEN** conviction stays 8.0 and no flag is recorded

#### Scenario: Non-UP call
- **WHEN** the direction is DOWN or FLAT
- **THEN** conviction is unchanged

#### Scenario: Dip bounce on a falling ticker
- **WHEN** an UP call's trailing 10-day return is below -5%
- **THEN** conviction is capped and `bounce_thesis_capped` is recorded

<!-- tests: tests/test_market_context_boundary.py::test_fixed_input_guardrails_match_pre_refactor_golden_values -->

### Requirement: Guardrail cap reaches the composite score
When a guardrail fires for a horizon the system SHALL also cap that row's composite score at the capped conviction, and MUST leave other horizons of the same ticker uncapped.

#### Scenario: Composite capped with conviction
- **WHEN** a no-news bullish call has conviction 9 and composite 9
- **THEN** both are stored at no more than 5 and the row carries `no_news_bullish_capped`

<!-- tests: tests/test_review_findings.py::test_guardrail_cap_reaches_composite_score_not_just_conviction -->

### Requirement: Raw conviction preserved
When a guardrail caps conviction the system SHALL store the capped value as the prediction's conviction and the model's original value as its raw conviction, and SHALL stamp shared forecasts with guardrail version `conviction-guardrails/1.0`. A row with no cap has no raw conviction.

#### Scenario: Capped row
- **WHEN** a guardrail caps a row
- **THEN** conviction holds the cap and raw conviction holds the model's value

#### Scenario: Uncapped row
- **WHEN** no guardrail fires
- **THEN** raw conviction is unset

<!-- tests: tests/test_review_findings.py::test_guardrail_cap_preserves_raw_conviction_for_mining, tests/test_review_findings.py::test_uncapped_call_leaves_raw_conviction_unset -->

### Requirement: Recommendation labels
A prediction's recommendation SHALL be one of `STRONG_BUY`, `BUY`, `HOLD`, `SELL`, `STRONG_SELL`. The analyst prompt instructs the model to map the 1 to 10 composite as 7.5 or more STRONG_BUY, 6.0 to 7.4 BUY, 4.0 to 5.9 HOLD, 2.5 to 3.9 SELL and under 2.5 STRONG_SELL; the code does not recompute the label from the composite.

#### Scenario: Prompt states the mapping
- **WHEN** the reasoning prompt is rendered
- **THEN** it contains the five composite ranges above

#### Scenario: Label is the model's
- **WHEN** a model returns a recommendation and a composite that disagree
- **THEN** the stored recommendation is the model's, unchanged by code

<!-- tests: NONE -->

### Requirement: Prediction rows are append-only and report change
Storing a prediction SHALL append a new row with evaluation status `pending` and return whether it saved, whether the recommendation changed from the previous one, the previous recommendation and the new id. A shared row compares against shared history only; a private row against everything its owner may see. A storage failure MUST return `saved: false` with the error and not raise.

#### Scenario: Recommendation changes
- **WHEN** a ticker's new recommendation differs from its previous one
- **THEN** the row is marked changed and records the previous recommendation

#### Scenario: Storage error
- **WHEN** the insert fails
- **THEN** the result is `saved: false` with the error text

<!-- tests: NONE -->

### Requirement: Prediction role and scope vocabularies
A stored prediction's role SHALL be one of `champion`, `fallback`, `challenger`, `legacy_random`, `interactive`, and its scope one of `shared_market`, `private` or (read-only) `legacy_unclassified`. An unknown role or an unwritable scope MUST be refused with an error, and the default scope MUST be `private`.

#### Scenario: Unknown role
- **WHEN** a prediction is inserted with role `other`
- **THEN** the insert raises an error naming the allowed roles

#### Scenario: Legacy scope not writable
- **WHEN** a prediction is inserted with scope `legacy_unclassified`
- **THEN** the insert raises an error

#### Scenario: Default scope
- **WHEN** a prediction is inserted without a scope
- **THEN** it is stored as private

<!-- tests: tests/test_market_context_boundary.py::test_legacy_rows_are_classified_unclassified_never_promoted_to_shared -->

### Requirement: Shared forecast write door
A shared forecast SHALL be stored only through the shared writer, which MUST receive a market-only market context, an origin on the allow-list (only `pipeline.apex`), a role of champion, fallback or challenger, a ticker matching the context, and a horizon that was part of the context. A shared row MUST carry the context digest and provenance and no owner.

#### Scenario: Disallowed origin
- **WHEN** the origin is `chat.ticker_prediction`
- **THEN** the write is refused with a scope error

#### Scenario: Not a market context
- **WHEN** the context argument is not a market-only market context
- **THEN** the write is refused

#### Scenario: Ticker or horizon outside the context
- **WHEN** the ticker differs from the context's or the horizon was not in it
- **THEN** the write is refused

#### Scenario: Role not allowed
- **WHEN** the role is `interactive` or `legacy_random`
- **THEN** the write is refused

#### Scenario: Shared row without a digest
- **WHEN** a shared-scope row is inserted without a context digest
- **THEN** the insert raises an error

<!-- tests: tests/test_market_context_boundary.py::test_write_shared_forecast_requires_a_market_only_context_from_an_allowed_origin, tests/test_market_context_boundary.py::test_write_shared_forecast_round_trips_full_provenance_onto_the_readable_prediction, tests/test_apex_champion.py -->

### Requirement: Shared forecasts refuse private text
The shared writer SHALL refuse a forecast whose prediction, recommendation, reasoning or panel summary contains a private term, return `saved: false` with a `private_text_rejected` error, and never log the forecast text or the private term.

#### Scenario: Private term in reasoning
- **WHEN** the reasoning names a private account or holding term
- **THEN** nothing is stored and the result is `saved: false`

#### Scenario: Private term in panel summary
- **WHEN** only the panel summary contains the term
- **THEN** the write is refused

<!-- tests: tests/test_market_context_boundary.py::test_write_shared_forecast_refuses_text_containing_a_private_term, tests/test_market_context_boundary.py::test_private_sentinel_text_cannot_enter_the_shared_writer_even_via_panel_summary, tests/test_market_context_boundary.py::test_private_text_refusal_never_logs_the_private_text -->

### Requirement: Private forecast write door
A personalized or chat-origin forecast SHALL be stored only through the private writer as scope `private` with owner `user:<owner>`, no context digest, and a role defaulting to `interactive`. Private rows MUST never be read as shared market history.

#### Scenario: Private row
- **WHEN** an owner's forecast is stored privately
- **THEN** it has scope `private`, that owner and role `interactive`

#### Scenario: Private row does not suppress shared generation
- **WHEN** a private forecast exists for a ticker today
- **THEN** the shared-scope check for today's horizons does not count it

<!-- tests: tests/test_market_context_boundary.py::test_write_private_forecast_is_always_private_and_owned -->

### Requirement: Shared reads exclude private and challenger rows
Shared-history reads (latest prediction, today's horizons, event processing) SHALL use only `shared_market` scope, and every latest-prediction or history read MUST exclude challenger rows. Legacy unclassified rows are readable only through the legacy-review scopes.

#### Scenario: Unclassified history
- **WHEN** shared history is read
- **THEN** legacy unclassified rows are excluded but remain available to legacy review

<!-- tests: tests/test_market_context_boundary.py::test_unclassified_history_is_excluded_from_shared_but_kept_for_legacy_review, tests/test_market_context_boundary.py::test_failure_pattern_mining_reads_only_shared_rows -->

### Requirement: Legacy horizon labels and prediction type
When a prediction has no horizon in days the system SHALL derive it from the legacy label (`5d` or `1w` as 5, `21d` or `1m` as 21, `63d` or `3m` as 63) and SHALL label the prediction type as `<days>d`.

#### Scenario: Legacy label
- **WHEN** a prediction is stored with horizon `1m` and no horizon in days
- **THEN** its horizon in days is 21 and its type is `21d`

<!-- tests: NONE -->

### Requirement: Evaluation date counts trading days
A new prediction's evaluation date SHALL be the date that many trading days after its forecast date, skipping weekends and NYSE full-day closures. A date supplied by the caller or already stored MUST NOT be recomputed or rewritten.

#### Scenario: Skips Thanksgiving and the weekend
- **WHEN** a 3-day horizon is forecast on the Tuesday before Thanksgiving
- **THEN** the evaluation date is the following Monday

#### Scenario: Skips Good Friday
- **WHEN** a 2-day horizon is forecast on the Wednesday before Good Friday
- **THEN** the evaluation date is the Monday after

#### Scenario: Stored rows keep their date
- **WHEN** a row was stored with an evaluation date and the database is opened again
- **THEN** that date is unchanged

<!-- tests: tests/test_long_horizon.py::test_trading_day_evaluation_date_skips_weekends_and_holidays, tests/test_long_horizon.py::test_new_predictions_use_the_trading_day_rule_and_old_rows_keep_their_date, tests/test_trading_calendar.py::test_weekends_and_evaluation_dates_use_the_real_calendar -->

### Requirement: NYSE trading calendar
`is_trading_day` SHALL be false on weekends and on NYSE full-day closures: New Year's Day, Martin Luther King Jr. Day, Washington's Birthday, Good Friday, Memorial Day, Juneteenth from 2022, Independence Day, Labor Day, Thanksgiving and Christmas. A weekend holiday MUST be observed on the Friday before or the Monday after, except that a Saturday January 1 is not observed on the Friday.

#### Scenario: Columbus and Veterans Day trade
- **WHEN** Columbus Day or Veterans Day is checked
- **THEN** it is a trading day, while Good Friday is not

#### Scenario: Saturday New Year
- **WHEN** January 1 falls on a Saturday
- **THEN** the preceding Friday December 31 is a trading day

#### Scenario: Sunday holiday
- **WHEN** a fixed-date holiday falls on a Sunday
- **THEN** the following Monday is closed

#### Scenario: Juneteenth start year
- **WHEN** a year before 2022 is checked
- **THEN** June 19 is not a closure

<!-- tests: tests/test_trading_calendar.py::test_the_nyse_closures_for_a_year, tests/test_trading_calendar.py::test_columbus_and_veterans_day_are_trading_days_and_good_friday_is_not, tests/test_trading_calendar.py::test_new_year_on_a_saturday_is_not_observed_on_the_friday_and_a_sunday_one_moves_to_monday, tests/test_trading_calendar.py::test_juneteenth_only_from_2022 -->

### Requirement: Matured predictions awaiting evaluation
The system SHALL treat a prediction as ready for outcome evaluation when its evaluation date is on or before today, it has a horizon in days, and its status is pending or unset. Rows with no evaluation date MUST be ignored and already evaluated rows excluded.

#### Scenario: Missed days are caught up
- **WHEN** a row matured several days ago and is still pending
- **THEN** it is returned for evaluation

#### Scenario: Already evaluated
- **WHEN** a row's status is not pending
- **THEN** it is not returned

<!-- tests: tests/test_prediction_db.py::test_get_matured_pending_predictions_catches_up_on_missed_days, tests/test_prediction_db.py::test_get_matured_pending_predictions_excludes_already_evaluated, tests/test_prediction_db.py::test_get_matured_pending_predictions_ignores_rows_with_no_evaluation_date -->

### Requirement: Return buckets and direction
A realized return SHALL map to one of `strong_down`, `moderate_down`, `flat`, `moderate_up`, `strong_up` using the edges for the row's horizon and version: below minus strong is strong_down, below minus flat is moderate_down, below flat is flat, below strong is moderate_up, otherwise strong_up. Direction is UP above flat, DOWN below minus flat, otherwise FLAT.

#### Scenario: Flat boundary
- **WHEN** a return equals the flat edge exactly
- **THEN** it is `moderate_up` and the direction is FLAT

#### Scenario: Strong move
- **WHEN** a return is at or above the strong edge
- **THEN** the bucket is `strong_up`

<!-- tests: tests/test_bucket_edges.py::test_5d_results_do_not_move_between_versions, tests/test_bucket_edges.py::test_flat_is_achievable_at_250d_and_not_at_legacy_edges -->

### Requirement: Seed return-bucket edges
Return-bucket edges SHALL be a table per horizon of flat, significant and strong cut-offs, seeded as: 5d 1%, 2%, 5%; 21d 2%, 4%, 8%; 63d 4%, 7%, 12%; 250d 8%, 15%, 25%. Version 1 is the fixed 1%, 2%, 5% for every horizon; the seed table is version 2 and its 5-day edges equal version 1.

#### Scenario: Seed table
- **WHEN** the engine settings are at their seed
- **THEN** the stored edges equal the table above at version 2

#### Scenario: Version 1 for every horizon
- **WHEN** edges are requested for any horizon at version 1 or no version
- **THEN** they are 1%, 2%, 5%

<!-- tests: tests/test_bucket_edges.py::test_seed_edges_scale_with_the_horizon_and_5d_equals_legacy, tests/test_bucket_edges.py::test_migration_is_idempotent_and_keeps_history -->

### Requirement: Bucket edge validation
A return-bucket edge table SHALL be accepted only when every supported horizon is present and, for each, all three values are finite positive numbers with flat less than significant less than strong. A violation MUST be refused with an error naming the horizon and rule.

#### Scenario: Order violated
- **WHEN** a horizon has significant below flat
- **THEN** the table is refused and the stored table is unchanged

<!-- tests: tests/test_bucket_edges.py::test_edges_validation_and_admin_only_and_reset -->

### Requirement: Bucket edges are versioned and never rescore
Every saved change to the edges SHALL bump the edges version and append the table to an append-only history, and an unchanged save MUST NOT bump it. Each prediction SHALL store the version it was made under (default 1) and MUST always be scored with those edges, so a later change never rescores existing rows.

#### Scenario: Unchanged save
- **WHEN** the current table is saved again unchanged
- **THEN** the version does not change

#### Scenario: Stale version refused
- **WHEN** an update carries an out-of-date expected version
- **THEN** it is refused

#### Scenario: Old row keeps its edges
- **WHEN** an administrator changes the 21-day edges
- **THEN** a row made under the previous version is still scored with its original edges

#### Scenario: Legacy row
- **WHEN** a row has no edges version
- **THEN** it is scored as version 1

<!-- tests: tests/test_bucket_edges.py::test_unchanged_save_does_not_bump_the_version_and_stale_version_is_refused, tests/test_bucket_edges.py::test_changing_edges_does_not_rescore_old_rows, tests/test_bucket_edges.py::test_legacy_rows_are_scored_with_version_1_and_new_rows_with_their_version, tests/test_bucket_edges.py::test_migration_is_idempotent_and_keeps_history -->

### Requirement: Prompt and scorer share one edge source
The per-horizon return ranges shown to the model in the prompt SHALL be rendered from the same edges the scorer uses, so the two cannot differ.

#### Scenario: Prompt parsed back
- **WHEN** the prompt's range text for a horizon is parsed
- **THEN** its flat and strong edges equal the scorer's edges for that horizon and version

<!-- tests: tests/test_bucket_edges.py::test_prompt_ranges_are_exactly_the_scorers_edges, tests/test_bucket_edges.py::test_no_scoring_code_uses_the_fixed_flat_threshold -->

### Requirement: Edge cohorts never mix
Metrics SHALL group predictions by the lowest edges version whose edges for that horizon are identical, so a horizon whose edges did not change keeps one cohort, and a metric computed over rows with different edges MUST raise an error.

#### Scenario: 5-day rows stay one cohort
- **WHEN** 5-day rows of version 1 and 2 are grouped
- **THEN** both are cohort 1

#### Scenario: 250-day rows split
- **WHEN** 250-day rows of version 1 and 2 are grouped
- **THEN** they are different cohorts

#### Scenario: Mixed edges in one metric
- **WHEN** a metric is requested over rows with different edges
- **THEN** it raises an error

<!-- tests: tests/test_bucket_edges.py::test_5d_results_do_not_move_between_versions, tests/test_bucket_edges.py::test_a_cohort_mixing_different_edges_raises -->

### Requirement: Cadence rule vocabulary and seed
The schedule rule for each horizon SHALL be one of `weekly_monday`, `monthly_first_trading_day`, `quarterly_first_trading_day`, `never`, seeded as 5d and 21d `weekly_monday`, 63d `monthly_first_trading_day`, 250d `quarterly_first_trading_day`. An unknown rule, an unsupported horizon or an empty value MUST be refused; how the rules schedule runs is covered by pipeline-orchestration.

#### Scenario: Seed cadence
- **WHEN** the engine settings are at their seed
- **THEN** the stored rules are those above

#### Scenario: Unknown rule
- **WHEN** a cadence is saved with a rule outside the four
- **THEN** it is refused

#### Scenario: Unreadable stored value
- **WHEN** a stored cadence value is unreadable
- **THEN** the seed rule for that horizon is used

<!-- tests: tests/test_horizon_cadence.py::test_seed_cadence_equals_the_previous_hard_coded_schedule, tests/test_horizon_cadence.py::test_validation_rejects_unknown_rule_and_horizon_and_empty, tests/test_horizon_cadence.py::test_migration_is_idempotent_and_an_unreadable_value_falls_back_to_the_seed -->

### Requirement: Composition reads a stored shared forecast and never generates
Composing a decision for a ticker and horizon SHALL read the latest stored shared forecast and its plan and report its source as `reused` when still valid evidence, `stale` when not, or `unavailable` when none exists, and MUST NOT call a model or regenerate it. The outlook step MUST NOT accept a private question.

#### Scenario: Warm evidence
- **WHEN** a valid stored forecast exists
- **THEN** the outlook is `reused` and no model call is made

#### Scenario: Stale evidence
- **WHEN** the stored forecast's evidence has changed
- **THEN** the outlook is `stale` and nothing is regenerated

#### Scenario: No forecast
- **WHEN** no shared forecast exists for the ticker and horizon
- **THEN** the outlook is `unavailable` and no error is raised

<!-- tests: tests/test_decision_composer.py::test_warm_evidence_makes_zero_llm_calls_and_reuses_the_stored_forecast, tests/test_decision_composer.py::test_stale_market_evidence_is_reported_never_silently_regenerated, tests/test_decision_composer.py::test_no_stored_forecast_is_unavailable_not_an_error, tests/test_decision_composer.py::test_market_outlook_signature_never_accepts_a_private_question, tests/test_decision_composer.py::test_private_question_never_reaches_shared_history -->

### Requirement: Actionable recommendation requires permission
A composed decision SHALL expose an actionable recommendation only when the action permission is `ALLOWED_BY_RULES`, and MUST expose none when it is blocked, pending approval or unknown. Permission is re-evaluated on every composition and never depends on an explanation being generated.

#### Scenario: Blocked ticker
- **WHEN** the ticker is on the restricted list
- **THEN** the actionable recommendation is empty whatever the market recommendation is

#### Scenario: Pending approval
- **WHEN** the permission is pending approval
- **THEN** the actionable recommendation is empty

#### Scenario: Allowed
- **WHEN** the permission is `ALLOWED_BY_RULES`
- **THEN** the actionable recommendation equals the market recommendation

#### Scenario: Matches direct clearance
- **WHEN** the same ticker and action are evaluated directly
- **THEN** the composer's permission equals the direct result

<!-- tests: tests/test_decision_composer.py::test_blocked_ticker_never_produces_an_actionable_recommendation, tests/test_decision_composer.py::test_pending_approval_ticker_never_produces_an_actionable_recommendation, tests/test_decision_composer.py::test_allowed_ticker_does_produce_an_actionable_recommendation, tests/test_decision_composer.py::test_composer_and_direct_clearance_evaluation_agree_on_the_same_permission, tests/test_decision_composer.py::test_action_permission_never_depends_on_explanation_being_generated -->

### Requirement: Portfolio fit is private to the asking portfolio
A composed decision's portfolio fit SHALL be computed from the requesting actor's own portfolio and policy, so different portfolios receive different fit for the same shared forecast, and position sizing for a plan MUST be computed at read time and never stored.

#### Scenario: Two portfolios, one forecast
- **WHEN** two owners compose the same ticker and horizon
- **THEN** the market outlook is identical and each portfolio fit reflects that owner's holdings

<!-- tests: tests/test_decision_composer.py::test_different_portfolios_receive_different_portfolio_fit_for_the_same_forecast -->

### Requirement: Optional explanation is opt-in
Composition SHALL generate no explanation unless explicitly requested, and MUST make no model call by default. A requested explanation is not wired to a model and raises a not-implemented error.

#### Scenario: Default composition
- **WHEN** a decision is composed without requesting an explanation
- **THEN** its explanation is empty and no model call is made

<!-- tests: tests/test_decision_composer.py::test_explanation_step_is_opt_in_and_off_by_default_makes_no_llm_call -->
