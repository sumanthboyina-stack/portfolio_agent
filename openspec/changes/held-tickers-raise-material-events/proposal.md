# Proposal

## Why

Tickers a person holds can never raise a material-news event. The news phase sends held tickers to the analyst without a Stage 3 score, and the event detector only acts on a non-null score at or above the severity threshold. So the one group of tickers where an alert matters most never fires one, while non-held tickers (watchlist, trending) can fire events that no forecast follows, because event-driven forecasts run only for portfolio tickers.

## What Changes

- Stage 3 scores held and extra tickers that survive triage, in addition to everyone else. The score is recorded on their `news_filter_log` row. It still never gates summarizing: held tickers with articles are summarized as before.
- A held ticker whose score reaches the severity threshold raises a `material_news` event like any other ticker, and so triggers the existing event-driven forecast and notifications.

## Capabilities

### New Capabilities
<!-- None. -->

### Modified Capabilities
- `news-ingestion`: held and extra tickers are scored for severity (still never gated), and a scored held ticker can produce a material-news event.

## Out of scope

- What the alert says about the forecast: handled by the separate change `event-forecast-followup-alert` (a second message once the event-driven forecast is saved).
- Other detectors (earnings, 8-K, rating changes, macro stay stubs) and any change to the severity threshold.

## Impact

- `portfolio_agent/pipeline/daily/news.py` (Stage 3 input and filter-log row).
- Tests: new `tests/test_held_ticker_material_events.py`.
- Slightly larger Stage 3 call (held tickers added to the existing batched scorer call).
