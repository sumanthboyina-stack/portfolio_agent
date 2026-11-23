## ADDED Requirements

### Requirement: Holdings storage opens safely under concurrent connections
Opening a connection to the holdings storage SHALL NOT fail or make another concurrent connection fail because of schema setup: an up-to-date `holdings` view MUST be left in place, and an out-of-date one rebuilt in a way that tolerates another connection rebuilding it at the same time.

#### Scenario: Many connections at once
- **WHEN** eight threads repeatedly open connections and read holdings at the same time
- **THEN** none of them raises an error

#### Scenario: Up-to-date and stale views
- **WHEN** setup runs against a database whose view matches the current definition
- **THEN** the view is neither dropped nor re-created
- **AND** when the stored view is out of date, setup rebuilds it to the current definition

<!-- tests: tests/test_holdings_schema_concurrency.py::test_concurrent_connections_never_fail_on_the_holdings_view, tests/test_holdings_schema_concurrency.py::test_an_up_to_date_view_is_left_alone_and_a_stale_one_is_rebuilt -->
