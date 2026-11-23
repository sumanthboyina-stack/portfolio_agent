## ADDED Requirements

### Requirement: Morning batch stores position views
The morning batch SHALL run a position-view phase after the score snapshot and before the price backfill that stores each owner's view of every ticker they hold, and a failure in that phase MUST be logged without stopping the batch.

#### Scenario: Phase runs
- **WHEN** the morning batch reaches the phase
- **THEN** each owner's held tickers get a stored view for the day

#### Scenario: Phase fails
- **WHEN** the phase raises
- **THEN** the batch logs a warning and continues to the price backfill

<!-- tests: NONE -->
