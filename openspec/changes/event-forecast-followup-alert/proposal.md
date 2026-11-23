# Proposal

## Why

A material-news alert is queued the moment the event is detected, before the event-driven forecast runs, so it cannot say what APEX now thinks. Quoting the latest stored forecast beside fresh news would be stale (possibly days old) and could read as the system's current view. The owner wants to know, for tickers they hold, whether the forecast stance moved after the news.

## What Changes

- When an event-driven forecast for a ticker is saved, everyone who holds or watches that ticker gets a second `MARKET_EVENT` message stating APEX's label after the news: the change ("HOLD -> SELL"), "unchanged", or the first label when none existed before.
- The prior label is read, from shared non-challenger forecasts only, just before the new rows are written. The message is linked to the event, sent once per event (de-duplicated), and judged by each person's own routing, severity filter, quiet hours and content level.
- The first alert is unchanged.
- The notification wording guard test exempts exactly the "Forecast label ..." phrase, as it already exempts an approval's own action label.

## Capabilities

### New Capabilities
<!-- None. -->

### Modified Capabilities
- `notifications`: a second, forecast-label message follows an event-driven forecast.

## Out of scope

- "Increase" or "reduce" exposure wording; the message carries APEX's own label only.
- Challenger and private forecasts: never read or quoted.
- A threshold for sending (the message is sent whenever an event-driven forecast is saved; a changed-only or conviction threshold can be added later).
- Scheduled (non-event) forecasts, which send nothing.

## Impact

- `portfolio_agent/events/detector.py` (follow-up producer), `portfolio_agent/events/db.py` (event lookup), `portfolio_agent/pipeline/daily/apex.py` (hook after the champion/fallback rows are saved).
- Tests: new `tests/test_event_forecast_followup.py`; guard test in `tests/test_notification_producers.py`.
- One extra notification per event for each holder or watcher.
