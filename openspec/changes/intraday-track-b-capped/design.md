# Design

Selection is a pure function (`select_track_b`) so it can be tested without the pipeline; `run_batch_intraday` gathers its inputs (events, screen scores, examined set, forecast-done set, universe) and passes them in.

- Once-a-day discovery uses `has_completed_today("intraday")`: the current run is not yet completed, so the first run of the day discovers and later runs only handle events. A first run that crashed lets the next one discover.
- Universe check is skipped when no universe is loaded, so a fresh install is not blocked.
- Trigger type stays `trending_opportunity` because the Opportunity Engine, Predictions, Validation and valuation read it.
- Cost: with the defaults a pass runs at most 5 non-portfolio tickers instead of up to 30, and discovery happens once instead of three times.
