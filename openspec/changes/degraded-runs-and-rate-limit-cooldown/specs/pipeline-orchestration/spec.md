## ADDED Requirements

### Requirement: Degraded run status and alert
A run that would finish as `completed` SHALL instead finish as `degraded` when its APEX phase attempted at least 5 tickers and at least half failed. `degraded` MUST count as finished wherever a run is awaited, and every owner with a portfolio SHALL receive one SYSTEM notification per degraded run, deduplicated by run id and carrying no tickers or amounts.

#### Scenario: Most forecasts failed
- **WHEN** a run's APEX phase has 0 completed and 32 failed
- **THEN** the run status is `degraded` and each portfolio owner is notified once

#### Scenario: Small or mostly successful run
- **WHEN** fewer than 5 tickers were attempted, or fewer than half failed
- **THEN** the status stays `completed` and no alert is sent

<!-- tests: tests/test_pipeline_degraded_run.py -->
