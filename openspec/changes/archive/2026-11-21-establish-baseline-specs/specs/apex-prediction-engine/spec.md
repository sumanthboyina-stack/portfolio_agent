## Purpose

Defines how the daily APEX phase turns shared market context into stored per-horizon forecasts: which model makes each call (champion, fallbacks, random pool, challenger), how failures and out-of-credit models are handled, how every row is labelled with a prediction role, and what never reaches a forecast (private data, placeholders). Baseline at HEAD `ca450dd`. Composite scoring, weights and return buckets are specified in `prediction-composition-and-buckets`; outcome metrics in `prediction-validation`.

## ADDED Requirements

### Requirement: Default model plan
The system SHALL read the APEX model plan from `engine_settings` and, until changed, use these defaults: `apex_selection_mode` = `champion`, `apex_champion_model` = `Claude-Sonnet`, `apex_fallback_models` = `["GPT-5", "GPT-4o", "o3"]`, `apex_random_pool` = `["GPT-5", "DeepSeek-Chat"]`, `apex_challenger_model` unset (off) and `apex_challenger_sample_pct` = 0.

#### Scenario: Fresh settings
- **WHEN** no APEX setting has ever been edited
- **THEN** the plan is the champion `Claude-Sonnet` followed by fallbacks `GPT-5`, `GPT-4o`, `o3` in that order
- **AND** the challenger is off

<!-- tests: tests/test_apex_champion.py::test_default_plan_is_champion_then_ordered_fallbacks_with_no_randomness, tests/test_apex_random_pool.py::test_default_mode_is_still_one_fixed_champion, tests/test_apex_champion.py::test_challenger_is_off_by_default -->

### Requirement: Registered model labels
The system SHALL accept only these labels in APEX settings: `Claude-Sonnet`, `GPT-4o`, `GPT-5`, `o3`, `DeepSeek-Chat`, `Mistral-Large`, `Magistral-Medium`, `OR-Grok-4.3`, `OR-Qwen3.7-Max`. Each label maps to one provider and one exact model id, and the label is what a stored prediction records as its model name.

#### Scenario: Provider of a registered label
- **WHEN** the label `DeepSeek-Chat` is looked up
- **THEN** it resolves to the `deepseek` provider
- **AND** `OR-Grok-4.3` resolves to the `openrouter` provider

<!-- tests: tests/test_apex_random_pool.py::test_deepseek_is_a_registered_apex_model_with_its_own_provider, tests/test_apex_random_pool.py::test_mistral_higher_models_are_apex_models_and_lower_ones_are_pipeline_only, tests/test_apex_random_pool.py::test_openrouter_high_reasoning_models_are_registered_with_their_real_provider -->

### Requirement: Deterministic champion selection
In `champion` mode the same settings SHALL always select the same models in the same order, with no randomness, so validation compares like with like over time.

#### Scenario: Same settings, same model
- **WHEN** the plan is resolved twice for different tickers and dates under unchanged settings
- **THEN** the champion and the ordered fallbacks are identical both times

<!-- tests: tests/test_apex_champion.py::test_same_settings_always_pick_the_same_model, tests/test_apex_champion.py::test_engine_settings_set_champion_and_fallback_order -->

### Requirement: Model setting validation
An update of APEX settings SHALL refuse an unknown model label, a challenger percentage outside 0 to 100, an unknown selection mode and a random pool with fewer than two different models, and SHALL leave the stored settings unchanged when it refuses.

#### Scenario: Short pool
- **WHEN** an update sets `apex_random_pool` to a single model
- **THEN** it is refused and the stored pool is unchanged

#### Scenario: Percentage out of range
- **WHEN** an update sets `apex_challenger_sample_pct` to 150
- **THEN** it is refused

<!-- tests: tests/test_apex_champion.py::test_engine_settings_validate_model_labels_and_pct, tests/test_apex_random_pool.py::test_engine_settings_refuse_a_bad_mode_a_short_pool_and_an_unknown_model, tests/test_apex_champion.py::test_engine_settings_updates_persist -->

### Requirement: Bad stored settings never stop the pipeline
When the stored plan contains values that cannot be used, the system SHALL degrade with a warning instead of failing: an unknown champion becomes the default champion, unknown fallbacks are dropped, the champion is removed from its own fallback list, a challenger equal to the champion is off, and a random pool with fewer than two usable models falls back to `champion` mode.

#### Scenario: Unknown champion
- **WHEN** the stored champion label is not registered
- **THEN** the plan uses `Claude-Sonnet` as champion
- **AND** a warning is logged

<!-- tests: tests/test_apex_champion.py::test_bad_labels_degrade_to_defaults_instead_of_failing, tests/test_apex_random_pool.py::test_a_pool_needs_two_usable_models_otherwise_the_champion_plan_is_used -->

### Requirement: Unreadable settings use code defaults
If the APEX settings cannot be read, the system SHALL log a warning and use the default model plan rather than abort.

#### Scenario: Settings read fails
- **WHEN** reading `engine_settings` raises an error at the start of the phase
- **THEN** the run proceeds with the default plan

<!-- tests: NONE -->

### Requirement: Fallback on timeout, provider error or unparseable output
Within one run the system SHALL try the champion first and move to the next fallback, in order, only when the previous model timed out after 90 seconds, raised a provider failover-class error, or returned no valid JSON object containing `recommendation`. Each model SHALL be attempted at most once per ticker run.

#### Scenario: Timeout
- **WHEN** the champion does not answer within the timeout
- **THEN** the first fallback is called
- **AND** the result is labelled as a fallback with reason `timeout`

#### Scenario: Invalid JSON
- **WHEN** the champion answers without a valid prediction object
- **THEN** the next model in the plan is called

#### Scenario: One call per model
- **WHEN** every model fails with a rate-limit error
- **THEN** each model in the plan has been called exactly once

<!-- tests: tests/test_apex_champion.py::test_fallback_triggers_on_timeout_and_is_labelled, tests/test_apex_champion.py::test_invalid_json_moves_to_next_model, tests/test_execution_trace_characterization.py::test_run_apex_primary_success_is_exactly_one_model_call, tests/test_execution_trace_characterization.py::test_run_apex_primary_failure_falls_back_to_second_model_once, tests/test_execution_trace_characterization.py::test_run_apex_all_models_failing_returns_none_after_one_call_per_plan_model -->

### Requirement: Non-provider errors are not masked by fallback
An exception that is not a provider failover-class error SHALL NOT trigger a fallback inside the model run. In the daily phase it SHALL skip that one ticker, log an error, and let the rest of the batch continue.

#### Scenario: Unexpected error
- **WHEN** a model call raises an error that is not a failover-class error
- **THEN** the model run raises it to its caller
- **AND** the daily phase records that ticker as failed and continues with the next ticker

<!-- tests: tests/test_apex_champion.py::test_non_failover_error_still_propagates -->

### Requirement: No placeholder on failure
If every model in a ticker's plan fails, the system SHALL return no result for that ticker, write no prediction row of any role, and count the ticker as failed.

#### Scenario: All models exhausted
- **WHEN** the champion and every fallback fail for a ticker
- **THEN** no row is stored for that ticker for that run
- **AND** an `apex_exhausted` event is logged

<!-- tests: tests/test_execution_trace_characterization.py::test_run_apex_all_models_failing_returns_none_after_one_call_per_plan_model -->

### Requirement: Champion and fallback roles
A row made by the first model in the plan SHALL carry `prediction_role` = `champion` and `used_fallback` = false. A row made by any later model, including when earlier models were skipped as out of credit, SHALL carry `fallback`, `used_fallback` = true and the reason the earlier model was abandoned.

#### Scenario: Fallback row
- **WHEN** the champion fails and the first fallback answers
- **THEN** the stored row has role `fallback`, `used_fallback` = 1 and the fallback's model name

<!-- tests: tests/test_apex_champion.py::test_fallback_rows_are_stored_as_fallback, tests/test_apex_dead_models.py::test_a_skipped_champion_still_records_the_row_as_a_fallback_with_a_reason -->

### Requirement: Random pool pick is stable per ticker and day
In `random_pool` mode each ticker SHALL draw exactly one model from `apex_random_pool` by a hash of the upper-cased ticker and the date, so the same ticker gets the same model all day regardless of re-runs or horizons, and tickers are spread across the pool.

#### Scenario: Same ticker, same day
- **WHEN** the draw is made twice for one ticker and date
- **THEN** it returns the same model both times

#### Scenario: Spread across tickers
- **WHEN** many tickers draw on one date from a two-model pool
- **THEN** both models are drawn

<!-- tests: tests/test_apex_random_pool.py::test_the_draw_is_the_same_for_the_same_ticker_and_day_and_spread_across_tickers, tests/test_apex_random_pool.py::test_each_ticker_is_predicted_by_exactly_one_pool_model_and_both_models_get_used -->

### Requirement: Random pool plan composition
Under `random_pool` the drawn model SHALL be that run's champion, the other pool members SHALL follow as fallbacks in pool order, then the configured fallbacks without repeating any model, and the configured `apex_champion_model` SHALL be ignored.

#### Scenario: Pick and fallbacks
- **WHEN** a ticker draws one of two pool models and the fallbacks include the other
- **THEN** the other pool model is the first fallback and is not repeated later

<!-- tests: tests/test_apex_random_pool.py::test_a_ticker_gets_a_champion_plan_with_the_other_pool_member_first_then_the_configured_fallbacks, tests/test_apex_random_pool.py::test_the_configured_champion_is_ignored_under_a_pool_and_a_fallback_equal_to_the_pick_is_not_repeated, tests/test_apex_random_pool.py::test_a_pool_member_that_returns_invalid_json_falls_through_to_the_other_one -->

### Requirement: Out-of-credit models are marked and not called
When a provider refuses a call for lack of credit, quota, billing, subscription tier or authentication, the system SHALL mark that model unavailable for 600 seconds and SHALL NOT call it, as pick, fallback or challenger, until the mark expires or it is cleared. Timeouts, rate limits and invalid JSON SHALL NOT mark a model.

#### Scenario: Later tickers skip a dead model
- **WHEN** the champion and two fallbacks are refused for credit on the first ticker
- **THEN** the second ticker's run calls only the model that worked

#### Scenario: Timeout is not a mark
- **WHEN** a model times out or returns invalid JSON
- **THEN** it is not marked unavailable

#### Scenario: Mark expiry
- **WHEN** 600 seconds pass after a mark
- **THEN** the model is called again and a success clears the mark

<!-- tests: tests/test_apex_dead_models.py::test_a_dead_champion_and_dead_fallbacks_are_not_called_again_after_the_first_ticker, tests/test_apex_dead_models.py::test_bad_json_and_timeouts_are_never_marked_dead, tests/test_apex_dead_models.py::test_a_mark_expires_so_a_topped_up_account_is_retried_and_cleared_on_success, tests/test_apex_dead_models.py::test_a_mark_that_is_still_dead_after_the_ttl_is_re_marked, tests/test_apex_dead_models.py::test_openai_no_credits_remaining_is_recognised_without_relying_on_a_url_in_the_message, tests/test_apex_dead_models.py::test_hand_set_marks_never_expire_and_a_cleared_set_forgets_its_expiries -->

### Requirement: All-dead chains probe one model
When every model in a ticker's plan is marked unavailable, the system SHALL call only the one whose mark is closest to expiry, so recovery is noticed without walking the whole plan. When every pool member is marked, a pool draw SHALL still draw from the pool.

#### Scenario: Probe rotates
- **WHEN** every model in the plan is marked and the probe fails
- **THEN** the next ticker probes a different model

#### Scenario: Probe recovers
- **WHEN** the probe succeeds
- **THEN** that model is back in rotation

<!-- tests: tests/test_apex_dead_models.py::test_when_every_model_is_marked_only_one_is_probed_per_ticker_and_the_probe_rotates, tests/test_apex_dead_models.py::test_a_probe_that_succeeds_brings_that_model_back, tests/test_apex_random_pool.py::test_if_every_pool_model_is_out_of_credit_the_pool_is_still_tried_rather_than_giving_up_silently, tests/test_apex_dead_models.py::test_the_pool_mode_log_scenario_every_pool_model_dead_but_one_fallback -->

### Requirement: Dead pool member is not drawn
A model marked unavailable SHALL NOT be drawn from the random pool while another pool member is usable.

#### Scenario: DeepSeek out of credit
- **WHEN** `DeepSeek-Chat` is marked out of credit and the pool is `GPT-5` and `DeepSeek-Chat`
- **THEN** every ticker draws `GPT-5` and `DeepSeek-Chat` is not called again

<!-- tests: tests/test_apex_random_pool.py::test_when_deepseek_is_out_of_credit_the_ticker_falls_back_to_openai_and_deepseek_is_not_drawn_again, tests/test_apex_random_pool.py::test_deepseek_running_out_of_credit_counts_as_a_failover_error_so_the_next_model_is_tried -->

### Requirement: Challenger sampling is deterministic
When a challenger model and a percentage above 0 are configured, the system SHALL include a ticker in the challenger sample by a hash of ticker and date, so a re-run never adds or drops rows. At 0 no ticker is selected and at 100 every ticker is; raising the percentage only adds tickers. A challenger equal to the model that made the production row SHALL NOT run.

#### Scenario: Sample size
- **WHEN** 4000 tickers are sampled at 10, 30 and 75 percent
- **THEN** each sample is within 3 percentage points of its target and a lower sample is a subset of a higher one

<!-- tests: tests/test_apex_champion.py::test_challenger_sampling_respects_pct_and_is_deterministic -->

### Requirement: Challenger runs once, on the same prompt, and never affects production
A challenger run SHALL use the same prompt as the production call, make a single attempt with no fallback, store its rows with role `challenger` for the horizons the production rows covered, and never change the ticker's own completed or failed status. A challenger marked out of credit SHALL NOT be called.

#### Scenario: Challenger on
- **WHEN** the challenger percentage is 100 and the champion saved horizons 5 and 21
- **THEN** one challenger row exists for each of those horizons

#### Scenario: Challenger fails
- **WHEN** the challenger call raises an error
- **THEN** no challenger row is written
- **AND** the ticker's production rows and status are unchanged

#### Scenario: Challenger off
- **WHEN** no challenger is configured
- **THEN** no challenger call is made

<!-- tests: tests/test_apex_champion.py::test_challenger_rows_are_stored_but_never_surface_as_the_prediction, tests/test_apex_champion.py::test_challenger_failure_never_fails_the_ticker, tests/test_apex_champion.py::test_pipeline_writes_only_champion_rows_when_challenger_is_off, tests/test_apex_dead_models.py::test_the_challenger_is_not_called_while_it_is_marked_dead -->

### Requirement: Challenger predictions are never shown to users
Challenger rows SHALL be excluded from every latest-prediction, all-latest and history read.

#### Scenario: Written later, never latest
- **WHEN** a challenger row is stored after a champion row for the same ticker and horizon
- **THEN** the latest prediction for that ticker and horizon is the champion row
- **AND** the ticker's history contains no challenger row

<!-- tests: tests/test_apex_champion.py::test_challenger_rows_are_stored_but_never_surface_as_the_prediction -->

### Requirement: Prediction role vocabulary
A stored prediction's `prediction_role` SHALL be one of `champion`, `fallback`, `challenger`, `legacy_random`, `interactive`. An insert with any other role MUST be refused. Rows that existed before roles were introduced SHALL be backfilled once as `legacy_random`.

#### Scenario: Unknown role
- **WHEN** a prediction is inserted with role `random`
- **THEN** the insert is refused

#### Scenario: Migration backfill
- **WHEN** the role column is added to a table that already holds rows
- **THEN** those rows become `legacy_random` and rows added afterwards default to `champion`
- **AND** running the migration again changes nothing

<!-- tests: tests/test_apex_champion.py::test_insert_rejects_an_unknown_role, tests/test_apex_champion.py::test_role_column_migration_backfills_old_rows_as_legacy_random_once -->

### Requirement: Interactive role is private-only
A forecast written through the private writer SHALL carry role `interactive`, and a shared forecast SHALL be refused if it claims `interactive`.

#### Scenario: Shared writer claims interactive
- **WHEN** the shared forecast writer is called with role `interactive`
- **THEN** it raises a scope error and writes nothing

<!-- tests: tests/test_apex_champion.py::test_private_forecasts_are_interactive_and_shared_ones_cannot_claim_it -->

### Requirement: Roles in default validation
Default validation views SHALL count only `champion` and `fallback` rows; `legacy_random` rows SHALL be included only when the user switches legacy on, and `challenger` rows SHALL appear only in the champion-versus-challenger comparison on Validation QA. The user-facing Validation page SHALL never show legacy rows. See `prediction-validation`.

#### Scenario: Default view
- **WHEN** champion, fallback, legacy and challenger rows have all matured
- **THEN** the default validation counts only the champion and fallback rows

<!-- tests: tests/test_apex_champion.py::test_validation_views_default_to_champion_and_fallback, tests/test_apex_champion.py::test_role_breakdown_and_champion_vs_challenger_use_the_same_tickers_and_dates, tests/test_apex_champion.py::test_user_validation_page_never_shows_legacy_rows, tests/test_apex_champion.py::test_validation_qa_roles_tab_compares_champion_and_challenger -->

### Requirement: Row records the model that answered
Every stored APEX row SHALL record the winning model's label, provider, exact model id and how the model was chosen (`champion` or `random pick from <pool>`).

#### Scenario: OpenRouter winner
- **WHEN** an OpenRouter model answers
- **THEN** the row's provider is `openrouter`, its model id is the exact id called and its evidence records the selection

<!-- tests: tests/test_apex_random_pool.py::test_the_winning_model_name_id_provider_and_selection_are_stored -->

### Requirement: Daily phase runs only on trading days
The daily APEX phase SHALL do nothing and report "not a trading day" when the date is not an NYSE trading day, and SHALL do nothing when there are no tickers.

#### Scenario: Weekend run
- **WHEN** the phase starts on a Saturday
- **THEN** no model is called and no row is written

<!-- tests: NONE -->

### Requirement: Horizons per ticker and same-day deduplication
For each ticker the phase SHALL generate only the horizons that are both scheduled for the day and enabled for its instrument type, and SHALL skip horizons already saved today in shared scope. An event-driven ticker SHALL be skipped only if that exact event already produced a row.

#### Scenario: Re-run adds nothing
- **WHEN** the phase is run twice on the same day for the same horizons
- **THEN** the second run makes no model call and adds no rows

#### Scenario: New event after routine run
- **WHEN** a routine row exists today and a new identified event fires for the same ticker
- **THEN** the ticker is generated again for that event

<!-- tests: tests/test_apex_champion.py::test_challenger_rows_are_stored_but_never_surface_as_the_prediction, tests/test_scheduler_notifications.py::test_event_already_processed_helper_is_scope_and_ticker_specific -->

### Requirement: Fund rows carry no placeholder scores
For a fund the stored row SHALL have no fundamental and no valuation score whatever the model wrote, SHALL store the engine's own weights (fundamentals and valuation exactly 0), and SHALL record `instrument_type` and `leverage_factor`. Cash, money market, ticker-less funds and unpriced unknown types SHALL be skipped with a logged reason and no row.

#### Scenario: ETF and cash in one run
- **WHEN** the phase runs for a stock, an ETF, a money-market fund and an unpriced unknown ticker
- **THEN** the stock and ETF get rows and the ETF's fundamental and valuation scores are empty
- **AND** the other two are logged as skipped and get no row

<!-- tests: tests/test_etf_routing.py::test_apex_routes_funds_stores_no_placeholder_and_skips_cash_ciT_and_unpriceable -->

### Requirement: Return-bucket edges in the prompt and on the row
The prompt SHALL state each scheduled horizon's return-bucket ranges rendered from the edges in force at the start of the phase, and every row saved by that run SHALL store that edges version. See `prediction-composition-and-buckets`.

#### Scenario: Prompt matches scorer
- **WHEN** the prompt for horizons 5 and 250 is built
- **THEN** each horizon's ranges are exactly the edges the scorer uses for that horizon

<!-- tests: tests/test_bucket_edges.py::test_prompt_ranges_are_exactly_the_scorers_edges, tests/test_bucket_edges.py::test_legacy_rows_are_scored_with_version_1_and_new_rows_with_their_version -->

### Requirement: Conviction guardrails for bullish calls
Before storage, for a horizon with predicted direction `UP` the system SHALL cap conviction at `no_news_cap` (seed 5) when there is no news and conviction is at least `no_news_conviction_threshold` (seed 7), and at `bounce_cap` (seed 5) when the trailing 10-day return is below `bounce_return_threshold` (seed -0.05). Non-UP directions are never capped. A cap SHALL also cap the composite score, keep the raw conviction, and store flag `no_news_bullish_capped` or `bounce_thesis_capped`.

#### Scenario: No news, high conviction
- **WHEN** an UP call has conviction 8 and no news
- **THEN** conviction is stored as 5 with flag `no_news_bullish_capped`
- **AND** the raw conviction 8 is kept and the composite score is capped for that horizon only

#### Scenario: Not capped
- **WHEN** a DOWN call has conviction 7.5, or an UP call has conviction 4 and no news
- **THEN** conviction is unchanged and no flag is set

<!-- tests: tests/test_market_context_boundary.py::test_fixed_input_guardrails_match_pre_refactor_golden_values, tests/test_review_findings.py::test_guardrail_cap_reaches_composite_score_not_just_conviction -->

### Requirement: Forecast context is market-only
The context built for a forecast SHALL be a function of the ticker, horizons and shared market data only, and SHALL NOT read the investor profile, holdings, accounts or cash. Text containing any owner's private term MUST be refused at context build and again at the shared writer, without echoing the term.

#### Scenario: Different owners, same context
- **WHEN** two owners with different profiles and holdings build a context for one ticker
- **THEN** the contexts are byte-identical

#### Scenario: Private text in a data source
- **WHEN** a research field echoes a private term such as an account nickname
- **THEN** the context build raises a private-data error that does not contain the term

<!-- tests: tests/test_market_context_boundary.py::test_market_context_never_touches_profile_or_holdings_and_is_deterministic, tests/test_market_context_boundary.py::test_different_profiles_produce_identical_shared_market_context, tests/test_market_context_boundary.py::test_market_context_refuses_to_build_over_private_text, tests/test_market_context_boundary.py::test_private_text_refusal_never_logs_the_private_text, tests/test_market_context_boundary.py::test_write_shared_forecast_refuses_text_containing_a_private_term, tests/test_market_context_boundary.py::test_private_sentinel_text_cannot_enter_the_shared_writer_even_via_panel_summary -->

### Requirement: Context failure skips the ticker
When a ticker's context cannot be built, including because of a private-data refusal, the phase SHALL skip that ticker and log a warning and SHALL NOT fall back to a degraded or private context.

#### Scenario: Leak refused
- **WHEN** the context build for one ticker raises a private-data error
- **THEN** no model is called and no row is written for that ticker
- **AND** the remaining tickers proceed

<!-- tests: NONE -->

### Requirement: Trade plan failure never affects a forecast
After a row is saved the phase SHALL try to create its trade plan and plan simulation, and a failure of either SHALL be logged and SHALL NOT change the saved forecast or the run's result. See `trade-plans-and-simulation`.

#### Scenario: Plan creation fails
- **WHEN** trade-plan creation raises for a saved row
- **THEN** the row stays saved and the ticker counts as completed

<!-- tests: NONE -->

### Requirement: Model plan command-line contract
`python -m scripts.apex_models` SHALL show the plan and whether each provider key is set, never the key values, and with no flags change nothing. `--pool` sets the pool and mode `random_pool`; `--champion` sets the champion and mode `champion`; both together MUST be rejected. `--fallbacks` sets the ordered list (empty clears it), `--challenger` sets a model or `none`, `--challenger-pct` sets the percentage. A rejected value prints "Not changed" and exits with status 2.

#### Scenario: Turn the pool on
- **WHEN** the command is run with `--pool GPT-5 DeepSeek-Chat`
- **THEN** the stored mode is `random_pool` with that pool and the new plan is printed

#### Scenario: Invalid value
- **WHEN** the command is run with an unknown model label
- **THEN** it prints "Not changed", exits with status 2 and the settings are unchanged

<!-- tests: tests/test_apex_random_pool.py::test_the_models_script_shows_and_changes_the_plan -->

### Requirement: Model plan is logged once per phase
At the start of the phase the system SHALL log the plan once with event type `apex_model_plan`, and during a run SHALL log `apex_model_select` for each attempt (role and reason), `apex_retry` when moving to another model, `apex_complete` on success, `apex_exhausted` when all fail, `apex_challenger` for challenger activity, `apex_skip_dead` when out-of-credit models are skipped and `apex_timeout` on a timeout.

#### Scenario: Retry logged
- **WHEN** the champion fails and a fallback is about to be called
- **THEN** an `apex_retry` event names the failed model, the retry model and the reason

<!-- tests: NONE -->

### Requirement: No order execution
The APEX phase SHALL produce research forecasts only: it SHALL NOT connect to a broker, place orders or read brokerage accounts.

#### Scenario: Output of a run
- **WHEN** the phase completes
- **THEN** its only outputs are prediction rows, trade-plan and simulation rows, and logs

<!-- tests: NONE -->
