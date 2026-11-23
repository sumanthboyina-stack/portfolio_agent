# Proposal

## Why

The Portfolio page failed with `no such view: holdings`. The holdings schema setup runs on every new database connection and dropped and rebuilt the `holdings` view each time, in steps that are not atomic. With several web sessions, or a batch beside the web, one connection could drop the view while another was about to drop it or query it, so the second failed.

## What Changes

- The setup leaves the `holdings` view alone when its stored definition already matches the current one, and only rebuilds an out-of-date view.
- The drop and the create tolerate another connection getting there first.

## Capabilities

### Modified Capabilities
- `accounts-and-holdings`: concurrent connections can open the holdings storage without failing (ADDED requirement).

## Out of scope

- Any change to the view's columns, the migrations, or other tables' setup.

## Impact

- `portfolio_agent/tools/holdings_db.py`; new `tests/test_holdings_schema_concurrency.py`.
