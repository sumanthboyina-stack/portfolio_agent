# Proposal

## Why

From 2026-10-29 provider credits ran out and the morning APEX phase failed for every ticker on three Mondays, yet each run was recorded as `completed` and nobody was told. Rate-limited models were also retried on every ticker, each paying for a call that failed the same way.

## What Changes

- A run whose APEX phase attempted at least 5 tickers with at least half failing finishes as `degraded` instead of `completed`. `degraded` counts as finished everywhere a run is waited for (including the morning dependency check), and the Schedule page shows it as "Finished with failures".
- Every person with a portfolio gets one SYSTEM notification per degraded run.
- A model that fails with a rate limit is skipped for a 90-second cool-down (the credit mark stays 10 minutes).

## Capabilities

### Modified Capabilities
- `pipeline-orchestration`: degraded run status and alert (ADDED).
- `apex-prediction-engine`: rate-limit cool-down (ADDED).

## Out of scope

- Retrying failed forecasts automatically, per-run call budgets.

## Impact

- `tools/progress_tracker.py`, new `services/pipeline_health_service.py`, `tools/apex_dual_run.py`, Schedule page labels; tests `test_pipeline_degraded_run.py`, `test_apex_dead_models.py`; autouse mark reset in `tests/conftest.py`.
