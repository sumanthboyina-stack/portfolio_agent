## Purpose

Supplies the shared, market-only macroeconomic inputs to every forecast: FRED series access with caching, release and surprise tracking, an event calendar, a live macro snapshot with a regime label, and the market context built around it. Contains no holdings or profile data and no LLM-computed numbers. Baseline at HEAD `ca450dd`.

## ADDED Requirements

### Requirement: Series registry
The system SHALL define the tracked FRED series in a registry giving each a key, FRED id, frequency, tier and category. Tier-1 series SHALL be the ones fetched on every release run.

#### Scenario: Tier-1 series
- **WHEN** the registry is read
- **THEN** the tier-1 keys are `CPI` (`CPIAUCSL`), `CORE_CPI` (`CPILFESL`), `PCE` (`PCEPI`), `NFP` (`PAYEMS`), `UNEMPLOYMENT` (`UNRATE`), `JOBLESS_CLAIMS` (`ICSA`), `GDP` (`GDPC1`), `FED_FUNDS` (`FEDFUNDS`), `10Y` (`DGS10`), `2Y` (`DGS2`) and `VIX` (`VIXCLS`)

#### Scenario: Tier-2 series
- **WHEN** the registry is read
- **THEN** `PPI` (`PPIACO`), `RETAIL_SALES` (`RSAFS`), `3M` (`DGS3MO`), `HY_SPREAD` (`BAMLH0A0HYM2`), `DXY` (`DTWEXBGS`) and `CONSUMER_SENTIMENT` (`UMCSENT`) are tier 2

<!-- tests: NONE -->

### Requirement: FRED access degrades without a key
When no FRED API key is configured, every FRED read SHALL return an error result and MUST NOT raise or block the run.

#### Scenario: Latest value without a key
- **WHEN** the latest value of a series is requested and no key is configured
- **THEN** the result contains the series id and an `error` of "FRED_API_KEY not configured"

#### Scenario: Historical series without a key
- **WHEN** historical observations are requested and no key is configured
- **THEN** the result is a single entry carrying an `error`

<!-- tests: NONE -->

### Requirement: Observation fetch
The client SHALL fetch the newest observations first, drop observations whose value is missing, empty or ".", and return the rest oldest to newest.

#### Scenario: Missing values dropped
- **WHEN** FRED returns observations including a "." value
- **THEN** that observation is absent and the remainder are in chronological order

#### Scenario: Default historical window
- **WHEN** historical observations are requested without a start date
- **THEN** the start date is `observations × 35` days before today

<!-- tests: NONE -->

### Requirement: One-hour in-process cache
Observation and series-info results SHALL be cached in process for 3600 seconds, keyed by series, limit and start date. A failed fetch SHALL be cached the same way.

#### Scenario: Repeat read inside the hour
- **WHEN** the same observations are requested twice within an hour
- **THEN** the second read makes no network call

#### Scenario: Failure cached
- **WHEN** an observation fetch fails
- **THEN** the single `error` result is returned and a repeat request within the hour returns that same error without a new fetch

<!-- tests: NONE -->

### Requirement: Latest value and series metadata
The latest-value read SHALL return the series id, the newest value, its date and the series' short units. A failure to read metadata MUST NOT fail the read.

#### Scenario: Latest value
- **WHEN** the latest value of a series is requested
- **THEN** the result has `series_id`, `value`, `date` and `units`

#### Scenario: Metadata unavailable
- **WHEN** the series metadata call fails
- **THEN** the value and date are still returned and units are empty

<!-- tests: NONE -->

### Requirement: Year-over-year change
The system SHALL compute year-over-year change from the newest observation against the observation nearest 365 days earlier, over a window of the last 400 days, rounded to 2 decimals.

#### Scenario: Normal series
- **WHEN** a series has an observation about a year before its newest one
- **THEN** the result has `current`, `year_ago`, `yoy_pct`, `date` and `year_ago_date`

#### Scenario: Too little data
- **WHEN** fewer than two usable observations exist
- **THEN** the result is an `error` of "insufficient data"

<!-- tests: NONE -->

### Requirement: Momentum trend label
The system SHALL compare the average of the latest N observations with the prior N and label the change `rising` above +1%, `falling` below −1%, otherwise `stable`, or `unknown` when the prior average is zero.

#### Scenario: Rising
- **WHEN** the recent average is 2% above the prior average
- **THEN** the trend is `rising`

#### Scenario: Insufficient observations
- **WHEN** fewer than 2N usable observations exist
- **THEN** the result is an `error` of "insufficient data"

<!-- tests: NONE -->

### Requirement: Surprise severity
The system SHALL score a release surprise 0 to 3 as the absolute deviation (from consensus when given, else from the prior value) divided by the series' typical monthly sigma: above 1.5 is 3, above 0.5 is 2, above 0.1 is 1, otherwise 0.

#### Scenario: Major surprise
- **WHEN** the deviation is more than 1.5 sigma
- **THEN** severity is 3

#### Scenario: Series without a sigma
- **WHEN** the series has no typical sigma in the registry
- **THEN** severity is 0

<!-- tests: NONE -->

### Requirement: Release tracking
For each tier-1 series the system SHALL read the latest two observations, skip a series whose latest observation is older than the lookback (default 30 days), and compute month-over-month change, year-over-year change (when 13 observations exist) and severity.

#### Scenario: Fresh release
- **WHEN** a tier-1 series has a latest observation within the lookback
- **THEN** a release is reported with actual, prior, period label, mom and yoy changes and severity

#### Scenario: Stale series
- **WHEN** a series' latest observation is older than the lookback
- **THEN** it is omitted

#### Scenario: Period label
- **WHEN** a quarterly series is dated in May 2026
- **THEN** its period label is "Q2 2026", a weekly series reads "Week of <date>" and a monthly series reads "<Mon> <year>"

<!-- tests: NONE -->

### Requirement: Release persistence is best-effort
Releases SHALL be written to the `macro_releases` table, unique per series and release date, and a failure to persist MUST NOT stop release tracking or the macro snapshot. Recent surprises SHALL be the stored releases with severity 1 or more within the lookback, newest first, at most 5.

#### Scenario: Surprises listed
- **WHEN** recent surprises are requested
- **THEN** at most 5 stored releases with severity 1 or more are returned, newest first

#### Scenario: Persistence fails
- **WHEN** writing or reading release storage raises
- **THEN** release tracking continues, and recent surprises are an empty list

<!-- tests: NONE -->

### Requirement: FOMC schedule
The system SHALL report FOMC announcement dates from a hard-coded 2026 schedule (28 Jan, 18 Mar, 29 Apr, 17 Jun, 29 Jul, 16 Sep, 28 Oct, 16 Dec), marking 18 Mar, 17 Jun, 16 Sep and 16 Dec as "Fed decision + dot plot".

#### Scenario: Upcoming meeting
- **WHEN** a meeting date falls within the requested window
- **THEN** an `FOMC` event with date, days away, importance `high` and its detail is returned

#### Scenario: After the last 2026 meeting
- **WHEN** today is after 16 Dec 2026
- **THEN** no FOMC events are returned

<!-- tests: NONE -->

### Requirement: Computed release dates
The system SHALL estimate NFP as the first Friday of the month, CPI as the Tuesday between the 8th and 15th (the 12th if none), and PCE as two days before the last weekday of the month, for the previous, current and next two months.

#### Scenario: NFP date
- **WHEN** a month's first Friday is the 5th
- **THEN** the NFP event is dated the 5th

#### Scenario: PCE labelled as estimate
- **WHEN** a PCE event is listed
- **THEN** its detail reads "PCE Price Index (estimated)"

<!-- tests: NONE -->

### Requirement: Upcoming events list
The upcoming-events view SHALL combine the FOMC, computed and optional Finnhub economic events (only `high` or `medium` impact), remove duplicates by event and date, sort by date and return at most 10.

#### Scenario: Finnhub unavailable
- **WHEN** no Finnhub key is set or the call fails
- **THEN** the list contains the FOMC and computed events only, with no error

#### Scenario: Duplicate event
- **WHEN** Finnhub reports an event already present with the same name and date
- **THEN** it appears once

<!-- tests: NONE -->

### Requirement: Macro snapshot content
The macro snapshot SHALL combine yfinance (VIX, S&P 500 and its 1-month change) with FRED rates, inflation, labor, credit and currency blocks, upcoming events (at most 5) and recent surprises, and MUST keep the legacy top-level fields the weight engine reads.

#### Scenario: Legacy fields
- **WHEN** a snapshot is built
- **THEN** it has `as_of_date`, `vix`, `treasury_10y_yield`, `treasury_2y_proxy`, `yield_curve_spread`, `sp500_1mo_pct` and `macro_regime_hint`

#### Scenario: Structured blocks
- **WHEN** a snapshot is built
- **THEN** it has `fed`, `inflation`, `labor`, `credit`, `currency`, `recent_surprises`, `upcoming_events` and `interpretation`

<!-- tests: NONE -->

### Requirement: Regime hint
The snapshot's `macro_regime_hint` SHALL be `HIGH_VOLATILITY` when VIX is above 25, else `RISK_OFF` when the S&P 500 1-month change is below −3% or the high-yield spread is above 500 bps, else `RISK_ON` when that change is above +3%, else `NEUTRAL`.

#### Scenario: High volatility wins
- **WHEN** VIX is 30 and the S&P 500 is down 5% in a month
- **THEN** the hint is `HIGH_VOLATILITY`

#### Scenario: Credit stress
- **WHEN** VIX is 20 and the high-yield spread is 520 bps
- **THEN** the hint is `RISK_OFF`

<!-- tests: tests/test_weight_engine.py::test_macro_signal_neutral -->

### Requirement: Deterministic derived macro fields
The snapshot's credit regime, implied Fed move and interpretations SHALL be computed by fixed rules with no LLM: credit regime `tight` below 300 bps, `normal` below 500, else `wide`; implied move `cut` when the 3-month yield is more than 0.25 below fed funds, `hike` when more than 0.25 above, else `hold`.

#### Scenario: Wide credit
- **WHEN** the high-yield spread is 520 bps
- **THEN** the credit regime is `wide`

#### Scenario: Implied cut
- **WHEN** the 3-month yield is 0.4 below fed funds
- **THEN** the implied move is `cut`

<!-- tests: NONE -->

### Requirement: Snapshot degrades gracefully
When FRED data is unavailable the snapshot MUST still be produced from the remaining sources, with unavailable values null; the 10-year yield SHALL fall back to Yahoo Finance when FRED has none.

#### Scenario: FRED down
- **WHEN** every FRED read returns an error
- **THEN** the snapshot contains the yfinance fields and null FRED-derived fields

<!-- tests: NONE -->

### Requirement: Next FOMC lookup
The snapshot's `fed` block SHALL report the next FOMC date within 90 days with days until and detail, independent of the 14-day events window.

#### Scenario: Meeting in 60 days
- **WHEN** the next meeting is 60 days away
- **THEN** `next_fomc_date` and `days_until_fomc` are set though the event is outside the 14-day window

<!-- tests: NONE -->

### Requirement: One snapshot shared across a batch
A batch run SHALL fetch the macro snapshot once and inject it into every ticker's market context. A snapshot SHALL carry a version separate from its fetch time and a content digest.

#### Scenario: Several tickers
- **WHEN** contexts for several tickers are built with an injected snapshot
- **THEN** each context carries that snapshot and the macro loader is not called

#### Scenario: Version and fetch time
- **WHEN** a snapshot is fetched
- **THEN** it has a version, a UTC fetch time and a digest, and the version is not the fetch time

<!-- tests: tests/test_shared_market_snapshots.py::test_multiple_ticker_contexts_share_one_injected_macro_snapshot, tests/test_shared_market_snapshots.py::test_fetch_macro_snapshot_separates_version_from_fetched_at -->

### Requirement: Market context is market-only
The market context builder SHALL read only market data (fundamentals, research, news, valuation, macro, technical, shared forecast history, failure patterns) and MUST NOT read a user profile, holdings, accounts or chat state. Identical inputs SHALL give an identical payload and digest regardless of who asks.

#### Scenario: Different people
- **WHEN** contexts are built for two people with different profiles and holdings
- **THEN** the payloads and digests are identical

#### Scenario: Module imports
- **WHEN** the builder module's source is inspected
- **THEN** it references no profile or holdings module

<!-- tests: tests/test_market_context_boundary.py::test_market_context_never_touches_profile_or_holdings_and_is_deterministic, tests/test_market_context_boundary.py::test_different_profiles_produce_identical_shared_market_context -->

### Requirement: Market context refuses private text
The builder SHALL refuse to return a context whose text contains a known private term, raising a leak error that never echoes the term.

#### Scenario: Leaky source
- **WHEN** a research note contains a known account nickname
- **THEN** the build fails with a private-data error

<!-- tests: tests/test_market_context_boundary.py::test_market_context_refuses_to_build_over_private_text, tests/test_market_context_boundary.py::test_private_terms_detected_case_insensitively_and_error_never_echoes_them -->

### Requirement: Market context horizons
The builder SHALL require at least one explicit horizon and refuse any horizon other than the supported ones.

#### Scenario: Unsupported horizon
- **WHEN** a context is requested for horizon 7
- **THEN** it fails with an "unsupported horizon" error

#### Scenario: No horizon
- **WHEN** an empty horizon list is given
- **THEN** it fails with "at least one horizon is required"

<!-- tests: tests/test_market_context_boundary.py::test_market_context_rejects_unsupported_horizons -->

### Requirement: Fund market context
For an ETF, ETN, mutual fund or target-date fund the builder SHALL NOT load company fundamentals or valuation, SHALL NOT list them as data gaps or score caps, and SHALL add the instrument type, leverage and shared fund context. A stock's payload MUST be unchanged.

#### Scenario: Fund
- **WHEN** a context is built for an ETF
- **THEN** no fundamentals or valuation loader is called and no placeholder is added

#### Scenario: Stock with or without instrument info
- **WHEN** a stock's context is built with a `STOCK`, `UNKNOWN` or missing instrument row
- **THEN** the payload is byte-identical

<!-- tests: tests/test_etf_routing.py::test_etf_context_runs_no_fundamentals_or_valuation_call_and_adds_no_placeholder, tests/test_etf_routing.py::test_leveraged_and_inverse_funds_are_flagged_in_the_prompt_and_context, tests/test_etf_routing.py::test_stock_context_is_byte_identical_with_or_without_instrument_info -->

### Requirement: Legacy FRED tools
The specialist FRED tools SHALL return raw JSON observations for a series, CPI and core CPI, fed funds with the 2-, 10- and 30-year Treasury yields (monthly `GS2`, `GS10`, `GS30`), and unemployment with participation and jobless claims. Without a key they SHALL return an error entry.

#### Scenario: Interest rates
- **WHEN** the interest-rate tool runs with a key
- **THEN** it returns `FEDFUNDS`, `GS2`, `GS10` and `GS30` observations

#### Scenario: No key
- **WHEN** the tool runs without a key
- **THEN** each series is a single entry with an `error`

<!-- tests: NONE -->

### Requirement: Macro specialist output contract
The macro specialist SHALL be asked for one JSON object with `macro_regime` (`RISK-ON`, `RISK-OFF` or `NEUTRAL`), `fed_policy_direction`, `current_fed_funds_rate`, `cpi_yoy_pct`, `unemployment_rate`, `yield_curve_shape`, a `macro_score` from 1 to 10, tailwinds, headwinds and a summary.

#### Scenario: Output keys
- **WHEN** the specialist's instruction is read
- **THEN** it requires exactly those keys, with `macro_score` 1 to 10 where 10 is most favorable for equities

<!-- tests: NONE -->
