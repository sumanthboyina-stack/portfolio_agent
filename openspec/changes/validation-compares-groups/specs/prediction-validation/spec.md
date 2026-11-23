## MODIFIED Requirements

### Requirement: User Validation page
The Validation page SHALL compare Portfolio, Watchlist and Opportunities side by side on one screen, showing each group's days of results with a "too early to judge" level below 10 days and "still early" below 30. It MUST offer no filters, tabs or sidebar controls, show every horizon as a four-check grid with the groups as columns, give an empty group no results instead of everyone's data, and never show legacy or challenger rows.

#### Scenario: Few dates
- **WHEN** a horizon has fewer than 10 dates
- **THEN** it still appears in the grid as too early

#### Scenario: Empty data
- **WHEN** there are no evaluated predictions
- **THEN** the page renders without error

#### Scenario: Groups compared
- **WHEN** portfolio and opportunity predictions are both scored
- **THEN** each group shows its own days of results and the grid has one column per group

#### Scenario: No holdings
- **WHEN** the viewer holds nothing and watches nothing
- **THEN** those groups show no results and the viewer is not shown other tickers' data

<!-- tests: tests/test_validation_page.py -->
