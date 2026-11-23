# Proposal

## Why

The Validation page scored only the viewer's own held and watched tickers, fixed to one combined group. Opportunities (tickers APEX discovers on its own) took most of the credits but had no track record anywhere a user could see, and holdings and watchlist could not be compared.

## What Changes

- One screen, no tabs: Portfolio, Watchlist and Opportunities side by side. Each group's days of results and evidence level show in a block; for each horizon one four-check grid has the groups as columns; two comparison charts (return vs SPY by rating, score-vs-result trend) draw one series per group.
- The history table is one table with a Group column.
- A viewer with no holdings (or no watchlist) gets an empty group, never everyone's data.
- Fix: the history query now returns `trigger_type`, so opportunity rows can be recognised.

## Capabilities

### Modified Capabilities
- `prediction-validation`: User Validation page.

## Out of scope

- Per-group horizon or window choice, admin Validation QA.

## Impact

- `web/pages/6_🎯_Validation.py`, `web/components/validation_charts.py` (two builders), `validation_engine.get_recent_evaluated_predictions` (one column), `tests/test_validation_page.py`.
