## MODIFIED Requirements

### Requirement: Morning event-driven selection
In the event-driven morning, the system SHALL run APEX only for held and watchlist, non-skipped tickers that have at least one due horizon; a watchlist-only ticker's event counts at `watchlist_severity_threshold`. On days with a scheduled horizon it MUST also add news-trending tickers outside both lists, plus up to 10 top-conviction screener candidates, each labelled `trending_opportunity`.

#### Scenario: Nothing due
- **WHEN** no horizon is scheduled and no portfolio ticker has an event
- **THEN** APEX does not run and the phase is recorded with the note that no predictions are needed

#### Scenario: Trending on a scheduled day
- **WHEN** horizons are scheduled and a trending ticker is neither held nor already selected
- **THEN** it is added for the scheduled horizons with trigger `trending_opportunity`

#### Scenario: Watchlist ticker on a scheduled day
- **WHEN** horizons are scheduled and a ticker is on the watchlist but not held
- **THEN** it is forecast for the scheduled horizons with a scheduled trigger

<!-- tests: tests/test_watchlist_forecasts.py -->

### Requirement: Intraday portfolio track
The intraday batch SHALL generate a 5-day forecast, labelled `event_material_news`, for each held or watchlist ticker that has a pending event at or above its severity threshold (watchlist-only tickers use `watchlist_severity_threshold`), and nothing else for those tickers. Watchlist tickers MUST NOT also be treated as trending discoveries.

#### Scenario: Event on a held ticker
- **WHEN** a held ticker has a severity-3 event at an intraday run
- **THEN** only horizon 5 is generated for it

#### Scenario: No events
- **WHEN** no held ticker has an event
- **THEN** no portfolio forecast is generated and the track logs that

#### Scenario: Event on a watchlist ticker
- **WHEN** a watchlist-only ticker has an event at or above the watchlist threshold
- **THEN** horizon 5 is generated for it once, and it is not researched again as a trending ticker

<!-- tests: tests/test_watchlist_forecasts.py -->
