# Proposal

## Why

Watchlist tickers went through news, research and fundamentals every morning but were never forecast: the morning schedule and the intraday event track both used holdings only. The Validation page's Watchlist group therefore had nothing to score, while the news/research cost was still paid.

## What Changes

- The morning batch forecasts held and watchlist tickers on the same cadence (5-day weekly, 21-day weekly, 63-day monthly).
- A watchlist-only ticker with a pending event at or above `watchlist_severity_threshold` is forecast off-schedule, in the morning and at every intraday run (Track A). Other tickers keep `intraday_severity_threshold`.
- Watchlist tickers are no longer also treated as trending discoveries (Track B).
- The Validation page's Watchlist block says forecasts are weekly and on news, and that results begin about a week after the first one.

## Capabilities

### Modified Capabilities
- `pipeline-orchestration`: Morning event-driven selection; Intraday portfolio track.

## Impact

- `pipeline/batch/morning.py`, `pipeline/batch/intraday.py`, new `events/scheduler.select_event_tickers`, Validation page note, `tests/test_watchlist_forecasts.py`.
- Cost: about one forecast per watchlist ticker per cadence day, a small addition next to the discovery spend removed earlier.
