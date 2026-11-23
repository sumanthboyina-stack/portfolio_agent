## MODIFIED Requirements

### Requirement: Intraday trending track
The intraday batch SHALL run news, research, fundamentals and a 5-day forecast for non-portfolio tickers only when they have an unforecast severity-3 event (every run) or a math-screen score at or above the configured minimum and were not examined today (first run of the day only). At most the configured cap run per pass, ranked by event severity then score. These tickers MUST NOT be added to the watchlist.

#### Scenario: Unanalyzed trending ticker
- **WHEN** a trending non-portfolio ticker has an unforecast severity-3 event
- **THEN** it is researched and forecast for horizon 5 with trigger `event_material_news`, and a screen-promoted ticker with trigger `trending_opportunity`

#### Scenario: Trending ticker already analyzed with no event
- **WHEN** a trending ticker has no event and no screen signal, or was already examined today
- **THEN** it is not processed

#### Scenario: Cap reached
- **WHEN** more tickers qualify than the cap allows
- **THEN** the top-ranked run and the rest are deferred and logged

<!-- tests: tests/test_intraday_track_b.py -->
