# pipeline-orchestration Specification

## Purpose
Defines how APEX's scheduled work is launched and sequenced: the command-line entry points, the morning, intraday and evening batches, the horizon cadence that decides which forecasts are due, event detection and event-driven overrides, missed-run recovery, work leases, progress tracking and resumable checkpoints. Baseline at HEAD `ca450dd`. What each phase computes is specified in its own capability.

## Requirements

### Requirement: Batch entry points
The system SHALL expose the batches through `main.py` as `--batch morning`, `--batch intraday` and `--batch evening`. `--validate` MUST be an alias for the evening batch, and `--daily` MUST run the morning batch. Positional tickers are added to the batch as extra tickers.

#### Scenario: Daily routes to morning
- **WHEN** `main.py --daily` is run
- **THEN** the morning batch runs with the event-driven cadence

#### Scenario: Validate alias
- **WHEN** `main.py --validate` is run
- **THEN** the evening batch runs

#### Scenario: Force all
- **WHEN** `--force-all` is passed with `--batch morning` or `--daily`
- **THEN** the morning batch bypasses the event-driven cadence and runs APEX for every non-skipped portfolio ticker plus trending tickers on the legacy schedule

<!-- tests: tests/test_execution_trace_characterization.py::test_scheduled_horizons_fixed_dates -->

### Requirement: Standalone phase jobs
The system SHALL provide `--daily-news`, `--daily-fundamentals` and `--daily-research`, each running only its own phase over the full ticker universe. Portfolio tickers and explicit extra tickers MUST always be run in the fundamentals and research jobs, even when their stored data is fresh.

#### Scenario: News only
- **WHEN** `main.py --daily-news` is run
- **THEN** only the news phase runs and no fundamentals, research or APEX work is done

<!-- tests: NONE -->

### Requirement: Ticker universe for a run
A run's ticker universe SHALL be the de-duplicated union, in this priority order, of portfolio tickers, explicit extra tickers, watchlist tickers and market-trending tickers. A failed trending scan MUST yield no trending tickers and MUST NOT abort the run.

#### Scenario: Priority order and de-duplication
- **WHEN** a ticker is both held and on the watchlist
- **THEN** it appears once, in the portfolio position of the order

#### Scenario: Trending scan fails
- **WHEN** the trending scan raises an error
- **THEN** the run continues with the portfolio, extra and watchlist tickers only

<!-- tests: NONE -->

### Requirement: Single-ticker analysis CLI
The system SHALL run the legacy analysis for each ticker given on the command line, with `--agent` selecting one specialist or `all`, `--watchlist` adding every watchlist ticker, and `--json` printing the final state as JSON. With no ticker the command MUST exit with an error, except `--agent news`, which analyses `MARKET`.

#### Scenario: No tickers
- **WHEN** `main.py` is run with no ticker, no `--watchlist` and `--agent` other than `news`
- **THEN** it logs an error and exits with status 1

#### Scenario: Unknown agent
- **WHEN** `--agent` names a specialist that does not exist
- **THEN** the run logs the valid names and exits with status 1

<!-- tests: NONE -->

### Requirement: Morning batch phase order
The morning batch SHALL run these phases in order: news, short-interest check, research, fundamentals, APEX predictions, valuation, risk and technical flags, universe screen, score-calibration snapshot, price-history backfill. Valuation MUST run after APEX so that same-day trending-opportunity predictions are included.

#### Scenario: Normal morning run
- **WHEN** the morning batch runs on a trading day
- **THEN** the phases execute in the stated order and the run is recorded as completed

<!-- tests: NONE -->

### Requirement: Morning batch stops when a model phase is exhausted
The morning batch SHALL stop, recording the run as `error`, when the news, research or fundamentals phase reports that every model in its failover chain is exhausted. No later phase MUST run.

#### Scenario: News phase exhausted
- **WHEN** the news phase returns failure because all models failed
- **THEN** the batch logs that it is stopping, marks the run `error` and runs no further phase

<!-- tests: NONE -->

### Requirement: Secondary phases never abort a batch
In the morning batch, a failure in the short-interest check, valuation, risk and technical flags, universe screen, score snapshot or price backfill SHALL be logged as a warning and SHALL NOT stop the remaining phases or the run.

#### Scenario: Valuation fails
- **WHEN** the valuation phase raises an error
- **THEN** a warning is logged and the risk and technical phase still runs

<!-- tests: NONE -->

### Requirement: Globally skipped tickers are excluded from APEX and risk
Tickers whose global pipeline-skip entry covers all phases SHALL be excluded from the morning APEX phase and from the risk and technical phase.

#### Scenario: Skipped portfolio ticker
- **WHEN** a held ticker has an all-phases pipeline-skip entry
- **THEN** it receives no APEX prediction and no risk or technical flag in that run, and the skip is logged

<!-- tests: NONE -->

### Requirement: Non-trading days produce no forecasts or evaluations
On a day that is not an NYSE trading day, the morning event-driven APEX phase, the intraday batch and the evening batch SHALL do no forecast or evaluation work and SHALL finish as completed.

#### Scenario: Weekend evening run
- **WHEN** the evening batch starts on a Saturday
- **THEN** it logs that it is not a trading day, runs no layer and records the run as completed

<!-- tests: tests/test_execution_trace_characterization.py::test_is_trading_day_fixed_dates_including_observed_holiday -->

### Requirement: Scheduled horizon cadence
The system SHALL decide which horizons are due on a trading day from one rule per horizon in {5, 21, 63, 250}. The rules are `weekly_monday` (a Monday that is a trading day), `monthly_first_trading_day`, `quarterly_first_trading_day` and `never`. The seed is 5 and 21 on `weekly_monday`, 63 on `monthly_first_trading_day`, 250 on `quarterly_first_trading_day`.

#### Scenario: Ordinary Monday
- **WHEN** the day is a mid-month Monday and the cadence is the seed
- **THEN** horizons 5 and 21 are due

#### Scenario: First trading day of a quarter
- **WHEN** the day is the first trading day of a quarter and not a Monday
- **THEN** horizons 63 and 250 are due

#### Scenario: Monday that is also the first trading day of a non-quarter month
- **WHEN** the day is such a Monday
- **THEN** horizons 5, 21 and 63 are due

#### Scenario: Tuesday
- **WHEN** the day is a Tuesday that is not the first trading day of a month
- **THEN** no horizon is due

#### Scenario: Never
- **WHEN** every horizon's rule is `never`
- **THEN** no horizon is ever due

<!-- tests: tests/test_horizon_cadence.py::test_seed_cadence_equals_the_previous_hard_coded_schedule, tests/test_horizon_cadence.py::test_scheduler_follows_the_default_cadence, tests/test_horizon_cadence.py::test_never_for_every_horizon_schedules_nothing -->

### Requirement: Cadence is read from settings on every run
The scheduler SHALL read the stored cadence (`engine_settings.horizon_cadence_json`) each time the event-driven configuration is built, so an admin change applies from the next run. An unreadable setting MUST fall back to the seed cadence, and an unknown rule or missing horizon in the stored value MUST fall back to the seed for that horizon.

#### Scenario: Change applies on next read
- **WHEN** an admin sets horizon 250 to `monthly_first_trading_day`
- **THEN** the next first trading day of any month schedules 250

#### Scenario: Settings unreadable
- **WHEN** reading the cadence setting raises an error
- **THEN** the seed cadence is used and the run continues

#### Scenario: Corrupt stored value
- **WHEN** the stored cadence is not valid JSON or names an unknown rule
- **THEN** the seed rule is used for the affected horizons

<!-- tests: tests/test_horizon_cadence.py::test_changing_the_db_changes_the_schedule_on_the_next_read, tests/test_horizon_cadence.py::test_scheduler_config_falls_back_to_seed_if_settings_unreadable, tests/test_horizon_cadence.py::test_migration_is_idempotent_and_an_unreadable_value_falls_back_to_the_seed -->

### Requirement: Legacy schedule when no cadence is configured
When the event-driven configuration carries no scheduled cadence, the scheduler SHALL fall back to the legacy schedule: horizon 5 on every trading day, 21 on Mondays, and 63 on the first trading day of the month.

#### Scenario: Legacy first trading day
- **WHEN** no cadence is supplied and the day is the first trading day of a month that is not a Monday
- **THEN** horizons 5 and 63 are due

<!-- tests: tests/test_execution_trace_characterization.py::test_scheduled_horizons_fixed_dates -->

### Requirement: Event-driven configuration defaults
The system SHALL default the event-driven configuration to enabled, with an intraday severity threshold of 3, and with the earnings, SEC 8-K, rating-change, material-news and macro-event detectors each enabled. Setting `enabled` to false MUST stop detection and make the intraday batch exit without work.

#### Scenario: Disabled
- **WHEN** the configuration has `enabled` false
- **THEN** running the detectors returns no events and the intraday batch logs that event-driven mode is disabled

<!-- tests: NONE -->

### Requirement: Material-news detector
The material-news detector SHALL emit one event for each of today's analyzed news-filter rows whose stage-3 score is at least the intraday severity threshold and whose ticker is in the run's ticker list. The event's type MUST be `material_news`, its severity MUST be the score, and its source MUST be `news_filter_log`.

#### Scenario: Score at threshold
- **WHEN** a ticker in the list has an analyzed row with score 8 and the threshold is 3
- **THEN** one `material_news` event of severity 8 is recorded

#### Scenario: Ticker not in the list
- **WHEN** a row meets the threshold but its ticker is not in the run's list
- **THEN** no event is emitted for it

#### Scenario: Query failure
- **WHEN** reading the news-filter log fails
- **THEN** the detector logs a warning and returns no events

<!-- tests: tests/test_notification_producers.py::test_the_events_end_to_end_through_detect_material_news, tests/test_review_findings.py::test_run_all_detectors_detects_and_persists_material_news -->

### Requirement: Event rows are unique per ticker, type and day
Recording an event SHALL return the existing event when one with the same ticker, event type and day already exists, and MUST NOT create a duplicate.

#### Scenario: Detector re-run
- **WHEN** the detectors run twice on the same day and find the same ticker's news
- **THEN** one event row exists and both runs return its id

<!-- tests: NONE -->

### Requirement: Event-driven forecast triggers
A ticker with a pending event at or above the severity threshold SHALL be due for horizons 5 and 21 even when not scheduled, and MUST NOT be due for horizons 63 or 250 on event alone. A scheduled horizon MUST be labelled `scheduled_weekly`, `scheduled_monthly` or `scheduled_quarterly`, and an event-driven one `event_material_news`.

#### Scenario: Event on an off day
- **WHEN** a held ticker has a severity-3 event on a Tuesday
- **THEN** horizons 5 and 21 are generated with trigger `event_material_news` and the event id

#### Scenario: Event does not trigger long horizons
- **WHEN** a held ticker has a severity-3 event and 63 and 250 are not scheduled
- **THEN** neither 63 nor 250 is generated

<!-- tests: NONE -->

### Requirement: Morning event-driven selection
In the event-driven morning, the system SHALL run APEX only for held, non-skipped tickers that have at least one due horizon. On days with a scheduled horizon it MUST also add news-trending tickers outside the portfolio, plus up to 10 top-conviction screener candidates, each labelled `trending_opportunity` for the scheduled horizons.

#### Scenario: Nothing due
- **WHEN** no horizon is scheduled and no portfolio ticker has an event
- **THEN** APEX does not run and the phase is recorded with the note that no predictions are needed

#### Scenario: Trending on a scheduled day
- **WHEN** horizons are scheduled and a trending ticker is neither held nor already selected
- **THEN** it is added for the scheduled horizons with trigger `trending_opportunity`

<!-- tests: NONE -->

### Requirement: A new event creates a new forecast, a repeated one does not
An event-driven run SHALL write a new shared forecast version for a ticker and horizon that already has a routine forecast today, and SHALL NOT write a second forecast for an event id it has already processed.

#### Scenario: Event after the routine run
- **WHEN** a 5-day forecast exists for a ticker today and an event with a new id triggers 5 again
- **THEN** a second forecast row exists carrying that event id

#### Scenario: Same event reprocessed
- **WHEN** the same event id is run twice
- **THEN** only one forecast row exists for it

<!-- tests: tests/test_scheduler_notifications.py::test_event_with_new_id_generates_despite_horizon_already_done_today, tests/test_scheduler_notifications.py::test_reprocessing_the_same_event_id_is_suppressed, tests/test_scheduler_notifications.py::test_routine_generation_without_an_event_id_is_unaffected_by_the_fix -->

### Requirement: Intraday portfolio track
The intraday batch SHALL generate a 5-day forecast, labelled `event_material_news`, for each held ticker that has a pending event at or above the severity threshold, and nothing else for held tickers.

#### Scenario: Event on a held ticker
- **WHEN** a held ticker has a severity-3 event at an intraday run
- **THEN** only horizon 5 is generated for it

#### Scenario: No events
- **WHEN** no held ticker has an event
- **THEN** no portfolio forecast is generated and the track logs that

<!-- tests: NONE -->

### Requirement: Intraday trending track
The intraday batch SHALL run news, research and fundamentals, then a 5-day forecast, for trending tickers outside the portfolio that have an event today or have not been analyzed today, and for today's math-screen candidates with signal score at least 2 that have not been analyzed. These tickers MUST NOT be added to the watchlist.

#### Scenario: Unanalyzed trending ticker
- **WHEN** a trending non-portfolio ticker has no news row today
- **THEN** it is researched and forecast for horizon 5 with trigger `trending_opportunity`

#### Scenario: Trending ticker already analyzed with no event
- **WHEN** a trending ticker already has a news row today and no event
- **THEN** it is not processed again

<!-- tests: NONE -->

### Requirement: Intraday depends on the morning batch
The intraday batch SHALL, on a trading day, make sure the morning batch has completed before doing any work, after checking the trading-day rule and before checking whether event-driven mode is enabled.

#### Scenario: Morning missing at intraday
- **WHEN** the intraday batch starts and the morning batch has not completed today
- **THEN** the morning batch runs first

<!-- tests: tests/test_scheduler_notifications.py::test_ensure_morning_ran_today_is_deduplicated_across_concurrent_callers -->

### Requirement: Missed morning is recovered once
The system SHALL run the morning batch from the intraday or evening batch when it has not completed today, under its own run id and with a service identity. A one-hour lease keyed by day MUST ensure that only one concurrent caller runs it, and a caller that loses the claim MUST return without running it.

#### Scenario: Two callers race
- **WHEN** intraday and evening both find the morning batch missing
- **THEN** exactly one runs it and the other returns false

#### Scenario: Recovery fails
- **WHEN** the recovered morning run raises an error
- **THEN** the lease is released as failed so a later caller can try again, and the error propagates

<!-- tests: tests/test_scheduler_notifications.py::test_ensure_morning_ran_today_is_deduplicated_across_concurrent_callers -->

### Requirement: Evening batch layers
The evening batch SHALL run in order: outcome assignment for matured predictions, provisional marks for open long-horizon calls, shared fund-data refresh, rolling metrics, score-calibration outcome fill, portfolio price snapshot, screener outcome scoring, and portfolio-health snapshots for all owners. It MUST NOT generate predictions.

#### Scenario: No forecast generation
- **WHEN** the evening batch runs
- **THEN** no APEX forecast is generated

<!-- tests: tests/test_scheduler_notifications.py::test_evening_batch_never_calls_apex_generation -->

### Requirement: Optional evening layers never fail the batch
A failure in provisional marks, fund-data refresh, calibration fill, price snapshot, screener scoring, health snapshots or notification delivery SHALL be logged as a warning and SHALL NOT stop the later layers.

#### Scenario: Fund data refresh fails
- **WHEN** the fund-data refresh raises an error
- **THEN** a warning is logged and rolling metrics still run

<!-- tests: NONE -->

### Requirement: Evening notification delivery
At the end of the evening batch the system SHALL queue the clock-driven notification producers and run one delivery pass, and a delivery problem MUST NOT fail the batch.

#### Scenario: Delivery raises
- **WHEN** the producers or the delivery worker raise an error
- **THEN** a warning is logged and the batch completes

<!-- tests: NONE -->

### Requirement: Event notification does not affect detection
Each detected event SHALL be offered to every owner who holds the ticker in a non-paper account or watches it, and to nobody else, through the notification service. A failure in notifying MUST NOT stop the detector from returning its events.

#### Scenario: Notifier down
- **WHEN** the notification service raises an error for an event
- **THEN** the detector still returns the event and the pipeline proceeds

#### Scenario: Restricted ticker
- **WHEN** an owner's action permission for the ticker is restricted
- **THEN** the event still notifies that owner

<!-- tests: tests/test_notification_producers.py::test_a_failing_notifier_never_breaks_the_detector, tests/test_scheduler_notifications.py::test_a_restricted_or_unevaluatable_action_permission_still_notifies_monitoring_is_not_disabled -->

### Requirement: Work leases
The system SHALL grant at most one active lease per work key to a claimant, and MUST let an expired lease be claimed by anyone. Completing a job MUST succeed only for the worker holding the current lease, so a worker whose lease was reclaimed cannot publish.

#### Scenario: Active lease blocks a second claim
- **WHEN** a second worker claims a work key whose lease is still active
- **THEN** the claim returns nothing

#### Scenario: Expired lease reclaimed
- **WHEN** a lease expires and another worker claims the key
- **THEN** the claim succeeds and the first worker's completion is rejected

<!-- tests: tests/test_run_planner.py::test_lease_recovery_works_and_publication_does_not_loop, tests/test_run_planner.py::test_concurrent_identical_requests_normally_produce_one_provider_call -->

### Requirement: Generation planning decisions
For each ticker and horizon the run planner SHALL decide one of `REUSE`, `GENERATE`, `WAIT_FOR_EXISTING_JOB`, `DEFER` or `UNAVAILABLE`. Reuse requires the stored forecast's policy, guardrail and context versions and its fundamentals, research, valuation and technical as-of values to match the current ones, and no new material evidence.

#### Scenario: Nothing changed
- **WHEN** a stored shared forecast matches every current version and as-of value
- **THEN** the decision is `REUSE`

#### Scenario: Source data changed
- **WHEN** the fundamentals as-of value differs from the stored forecast's
- **THEN** the decision is `GENERATE` with the changed source in the reason

#### Scenario: Writing a forecast does not invalidate it
- **WHEN** a forecast is written and other rows are later appended to history
- **THEN** the forecast is still reusable

<!-- tests: tests/test_run_planner.py::test_plan_reuse_no_stored_row_means_generate, tests/test_run_planner.py::test_plan_reuse_matches_when_nothing_changed, tests/test_run_planner.py::test_plan_reuse_detects_stale_source_data, tests/test_run_planner.py::test_plan_reuse_detects_guardrail_version_change, tests/test_run_planner.py::test_writing_a_forecast_does_not_self_invalidate, tests/test_run_planner.py::test_new_material_evidence_forces_generate_even_with_matching_versions -->

### Requirement: Planner eligibility, budget and de-duplication
The run planner SHALL return `UNAVAILABLE` for a globally skipped ticker or an unsupported horizon, `WAIT_FOR_EXISTING_JOB` when another worker holds an active lease, and `DEFER` when the generation budget is exhausted. It MUST NOT consult any person's restricted list, and MUST collapse duplicate (ticker, horizon) demand to one decision that uses the budget once.

#### Scenario: Budget exhausted
- **WHEN** a ticker needs generation and the remaining budget is zero
- **THEN** the decision is `DEFER`

#### Scenario: Duplicate demand
- **WHEN** two requesters ask for the same ticker and horizon in one batch
- **THEN** one decision is produced and the budget is decremented once

#### Scenario: Personal restriction
- **WHEN** one person has restricted a ticker
- **THEN** the planner's decision for that ticker is unaffected

<!-- tests: tests/test_run_planner.py::test_globally_excluded_ticker_is_unavailable, tests/test_run_planner.py::test_unsupported_horizon_is_unavailable, tests/test_run_planner.py::test_exhausted_budget_defers_generate_candidates, tests/test_run_planner.py::test_active_lease_means_wait_for_existing_job, tests/test_run_planner.py::test_plan_batch_deduplicates_identical_demand_from_different_requesters, tests/test_run_planner.py::test_plan_batch_decrements_shared_budget_once_per_generate_not_per_requester, tests/test_run_planner.py::test_a_personal_restriction_never_reaches_the_planner_at_all -->

### Requirement: Generation unit publishes at most once
Running a generation unit SHALL call the provider at most once per successful claim, outside any database transaction, and SHALL publish the result only if the worker's lease is still current. A provider failure MUST mark the job failed and publish nothing, and a successful horizon MUST NOT be regenerated when a sibling horizon fails.

#### Scenario: Provider fails
- **WHEN** the provider raises an error for 21-day while 5-day succeeded
- **THEN** nothing is published for 21-day, the job is marked failed, and a retry regenerates only 21-day

#### Scenario: Lease lost before publish
- **WHEN** the lease expires and is reclaimed before the provider returns
- **THEN** the result is discarded and not written

<!-- tests: tests/test_run_planner.py::test_successful_horizons_are_not_repeated_when_another_horizon_fails, tests/test_run_planner.py::test_lease_recovery_works_and_publication_does_not_loop -->

### Requirement: Run progress is recorded
When a run id is present in the environment, each batch SHALL record a `pipeline_runs` row (job type, pid, start, status `running`, then `completed` or `error`, total tickers, and trigger source from `PIPELINE_TRIGGER`, default `cli`) and per-phase progress rows. A batch started without a run id MUST record nothing.

#### Scenario: Run started from the web page
- **WHEN** a job is started with a run id and `PIPELINE_TRIGGER=web`
- **THEN** its run row carries trigger source `web`

#### Scenario: No run id
- **WHEN** a batch is started without `PIPELINE_RUN_ID`
- **THEN** no run or phase row is written

<!-- tests: NONE -->

### Requirement: Completed-today check
The system SHALL treat a job type as having completed today only when a run of that type with status `completed` started on the current America/Chicago date.

#### Scenario: Errored morning
- **WHEN** today's only morning run has status `error`
- **THEN** the morning batch is treated as not completed

<!-- tests: NONE -->

### Requirement: Resumable checkpoints
When a news, research or fundamentals phase stops because every model is exhausted or a provider asks for a wait over 5 minutes, the system SHALL save the unprocessed tickers to `data/batch_checkpoint.json` by phase. A later run on a different day MUST process those tickers first, and a run on the same day as the checkpoint MUST NOT.

#### Scenario: Resume next day
- **WHEN** a checkpoint saved yesterday lists pending tickers for research
- **THEN** today's research phase processes them first

#### Scenario: Same-day retry
- **WHEN** the checkpoint's run date is today
- **THEN** no tickers are resumed from it, so a run never loops

<!-- tests: NONE -->

### Requirement: Launch wrappers
The repository SHALL provide `run_daily.sh` (runs `main.py --daily`), `run_daily_news.sh`, `run_validate.sh` (runs `--batch evening` with `PIPELINE_RUN_ID` set) and `run_web.sh`, each writing its output to a timestamped file under `logs/`. The batches are launched by an external scheduler or by the Schedule page; the application does not schedule itself.

#### Scenario: Evening wrapper
- **WHEN** `run_validate.sh` runs
- **THEN** the evening batch runs with a run id of the form timestamp plus `_evening`

<!-- tests: NONE -->
