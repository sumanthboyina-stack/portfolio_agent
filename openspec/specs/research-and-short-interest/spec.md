# research-and-short-interest Specification

## Purpose
Collects sell-side analyst consensus, price targets and rating changes, plus FINRA short-interest data, for every ticker in the shared pipeline universe. It stores them as shared market data (no owner) and digests them into the stored research summary that the APEX Research analyst reads. Baseline at HEAD `ca450dd`.

## Requirements

### Requirement: Research is shared market data
The system SHALL store research and short-interest rows once per ticker with no owner column, and the research phase MUST NOT read holdings quantities, accounts or investor profile when fetching, summarizing or storing them. The ticker universe is the union of portfolio, extra, watchlist and trending tickers.

#### Scenario: One row per ticker
- **WHEN** research is stored twice for the same ticker, in either case
- **THEN** exactly one `research` row exists for the upper-cased ticker

#### Scenario: No private data in a research prompt
- **WHEN** the batch research prompt is built for a chunk of tickers
- **THEN** it contains only broker data and the short-interest line for those tickers

<!-- tests: NONE -->

### Requirement: Research is two-track
The research phase SHALL run Track A (raw broker fetch with no LLM) for every ticker on each run, and Track B (LLM summary) only for tickers that need one. Track A MUST NOT overwrite the Track B fields (highlights, score, summary), and Track B MUST NOT overwrite the raw fields.

#### Scenario: Raw refresh keeps the summary
- **WHEN** a ticker with a stored LLM summary is refreshed by Track A
- **THEN** its consensus and price-target fields change and its highlights, `research_score` and summary are unchanged

#### Scenario: Summary keeps the raw data
- **WHEN** Track B stores a summary for a ticker
- **THEN** `last_llm_run_date` becomes today (America/Chicago) and the raw broker fields are unchanged

<!-- tests: NONE -->

### Requirement: Raw broker data fields
The Track A fetch SHALL return for a ticker: consensus key and mean (1.0 strong buy to 5.0 strong sell, as Yahoo reports it), analyst count, target mean, high, low and median, current price, upside to the mean target as `(mean - price) / price * 100` rounded to 2 places, the last 4 quarterly rating rows, and up to 10 recent firm actions, newest first. `latest_upgrade_date` is the newest action's date.

#### Scenario: Upside computed
- **WHEN** the current price and the mean target are both present
- **THEN** `upside_to_mean_pct` is the percent difference of the mean target over the price, rounded to 2 places

#### Scenario: Missing price or target
- **WHEN** the current price or the mean target is missing
- **THEN** `upside_to_mean_pct` is null

#### Scenario: Missing action history
- **WHEN** a ticker has no recorded firm actions
- **THEN** `recent_upgrades` is empty and `latest_upgrade_date` is null

<!-- tests: NONE -->

### Requirement: Finnhub is a secondary source that never blocks
The system SHALL add Finnhub's monthly recommendation trend (newest 6 periods as `YYYY-MM`) and an independent price target to the broker data, and a missing key, HTTP error or empty answer MUST give an empty trend and a null target, never an exception. A price target with no `targetMean` is treated as absent.

#### Scenario: No Finnhub key
- **WHEN** `FINNHUB_API_KEY` is unset
- **THEN** the trend is an empty list and the Finnhub target is null, and no request is made

#### Scenario: Percent bullish
- **WHEN** a monthly snapshot has 3 strong buy, 5 buy, 2 hold and no sells
- **THEN** its total is 10 and `pct_bullish` is 80.0

#### Scenario: Zero analysts in a month
- **WHEN** a monthly snapshot totals 0
- **THEN** `pct_bullish` is null

<!-- tests: NONE -->

### Requirement: Track A stores and isolates failures
Track A SHALL fetch tickers in parallel (8 workers) and upsert each ticker's raw data. A failure for one ticker MUST be logged as a warning and MUST NOT stop the others, and that ticker is left out of the Track B queue as having no Track A data.

#### Scenario: One ticker fails
- **WHEN** the fetch for one of ten tickers raises
- **THEN** the other nine are stored and the failed ticker is counted as failed

<!-- tests: NONE -->

### Requirement: Track B cadence
The system SHALL queue a ticker for an LLM summary when, in this order: it has no stored summary (`no_summary`); its last LLM run is 7 or more days old (`weekly_<n>d`); its latest firm-action date is newer than its last LLM run (`new_action_<date>`); or a stored fundamentals filing date is newer than its last LLM run (`post_earnings_<date>`). Otherwise it is current and skipped.

#### Scenario: Never summarized
- **WHEN** a ticker has no `last_llm_run_date`
- **THEN** the reason is `no_summary`

#### Scenario: Weekly refresh
- **WHEN** the last LLM run was 7 days ago and no new action or filing exists
- **THEN** the reason is `weekly_7d`

#### Scenario: Six days and nothing new
- **WHEN** the last LLM run was 6 days ago and no newer action or filing exists
- **THEN** the ticker is current and not queued

#### Scenario: New firm action
- **WHEN** the stored latest-action date is after the last LLM run
- **THEN** the reason is `new_action_` followed by that date

<!-- tests: NONE -->

### Requirement: Track B batching and resume
Track B SHALL send tickers to the `flash` model chain in batches of 10 with temperature 0, and persist each batch's summaries as soon as the batch returns. Tickers left unfinished when every model is exhausted MUST be saved to the `research` checkpoint and queued first (reason `resume`) on the next run.

#### Scenario: Models exhausted mid-run
- **WHEN** every model in the chain fails on batch 2 of 3
- **THEN** batch 1's summaries stay stored, the unfinished tickers are checkpointed, and the phase reports an error

#### Scenario: Resume next run
- **WHEN** a checkpoint from the previous run lists pending research tickers
- **THEN** those tickers are queued first with reason `resume`, without duplicates

<!-- tests: NONE -->

### Requirement: Track B output handling
Track B SHALL store, per ticker, highlights, a `research_score` (1 to 10, or null when data is insufficient), a summary and the model name and provider that produced them. A ticker missing from a batch answer, or one that cannot be saved, MUST be counted as failed without stopping the batch.

#### Scenario: Ticker missing from the answer
- **WHEN** a model's answer for a 10-ticker batch omits one ticker
- **THEN** that ticker is logged as a warning and counted failed, and the other nine are stored

#### Scenario: Tickers with no raw data
- **WHEN** a ticker is due for a summary but has no Track A data
- **THEN** it is skipped for Track B and counted failed

<!-- tests: NONE -->

### Requirement: Pipeline skip list honored
The research phase SHALL drop every ticker whose pipeline-skip phases include `research` or `all` before Track A, and record the skip.

#### Scenario: Restricted ticker skipped
- **WHEN** a ticker is on the pipeline skip list for `research`
- **THEN** it is not fetched and not summarized in that run

<!-- tests: NONE -->

### Requirement: Short-interest settlement calendar
The system SHALL compute FINRA settlement dates without any network call: the 15th of the month and the month's last day, each rolled back to the nearest prior weekday, with weekday-only lag. The latest published settlement date is the newest candidate whose date plus 8 business days is on or before today, scanning the current and two prior months.

#### Scenario: Mid-month on a weekend
- **WHEN** the 15th of a month falls on a Saturday
- **THEN** that month's mid settlement date is the Friday before

#### Scenario: Not yet published
- **WHEN** today is fewer than 8 business days after the newest settlement date
- **THEN** the latest published date is the previous settlement date

#### Scenario: Month boundary
- **WHEN** today is in the first days of a month
- **THEN** the prior month's end-of-month date is still considered

<!-- tests: NONE -->

### Requirement: Short-interest refresh is skipped when current
The system SHALL record each fully fetched settlement date, and when the latest published date is already recorded it MUST skip the refresh without a network call.

#### Scenario: Already fetched
- **WHEN** the latest published settlement date is already recorded
- **THEN** the refresh returns "already current" and makes no FINRA request

<!-- tests: NONE -->

### Requirement: Short-interest batch fetch
When a new settlement date is due, the system SHALL download the full FINRA `consolidatedShortInterest` file for that date with a filtered POST request in pages of 5000, keep only rows whose upper-cased symbol is in the ticker universe, and upsert them by (ticker, settlement date).

#### Scenario: Universe filter
- **WHEN** the file has rows for tickers outside the universe
- **THEN** only rows for universe tickers are stored

#### Scenario: Multiple pages
- **WHEN** a page returns exactly 5000 rows
- **THEN** the next page is requested, and the loop stops at the first short or empty page

#### Scenario: Re-fetch is idempotent
- **WHEN** the same settlement date is stored twice
- **THEN** each ticker keeps one row for that date with the latest values

<!-- tests: NONE -->

### Requirement: Short-interest refresh failures never block the run
A FINRA failure, or an answer with zero rows, SHALL be logged as a warning and the settlement date MUST NOT be marked fetched, so the next run retries. A failed short-interest step MUST NOT stop the morning pipeline.

#### Scenario: API error
- **WHEN** the FINRA request raises
- **THEN** nothing is stored, the date stays unrecorded, and the morning batch continues to research

#### Scenario: Zero rows (pre-publication)
- **WHEN** FINRA returns no rows for the date
- **THEN** the date stays unrecorded and the next run asks again

<!-- tests: NONE -->

### Requirement: Short-interest trend is computed in code
The system SHALL derive short-interest direction and squeeze pressure from stored rows without an LLM. Direction is the mean of `pct_change` over the newest up to 3 rows: rising above +5, falling below -5, else stable, and unknown with no change values. Squeeze pressure from the newest days-to-cover is high at 10 or more, elevated at 5 or more, else low, and unknown when absent.

#### Scenario: Rising
- **WHEN** the stored changes average +8
- **THEN** the trend is `rising`

#### Scenario: Boundary is stable
- **WHEN** the stored changes average exactly +5
- **THEN** the trend is `stable`

#### Scenario: Squeeze thresholds
- **WHEN** days-to-cover is 5, 10 and 4.9
- **THEN** squeeze pressure is `elevated`, `high` and `low`

#### Scenario: No history
- **WHEN** a ticker has no stored rows
- **THEN** the summary is `{ticker, available: false}`

<!-- tests: NONE -->

### Requirement: Short-interest accessor falls back to Yahoo
The short-interest accessor SHALL return the stored-data summary when available, else a Yahoo summary (`source: yfinance`, trend `unknown`, `periods_available` 1) from shares short and short ratio, with squeeze pressure from the same 5 and 10 thresholds, else `{available: false}`. It MUST NOT raise.

#### Scenario: No FINRA history
- **WHEN** a ticker has no stored rows and Yahoo reports shares short
- **THEN** the summary has `source` yfinance, trend `unknown` and the squeeze class of the Yahoo short ratio

#### Scenario: Nothing anywhere
- **WHEN** neither source has data, or Yahoo raises
- **THEN** the result is `available: false`

<!-- tests: NONE -->

### Requirement: Short-interest line in the research prompt
The batch research prompt block for a ticker SHALL end with a one-line short-interest summary (shares, days to cover, trend, percent change, and squeeze pressure when elevated or high) built from stored FINRA rows only, and the line MUST be omitted when no stored row exists.

#### Scenario: Stored data
- **WHEN** a ticker has stored short-interest rows
- **THEN** its block ends with a "Short interest (<settlement date>)" line

#### Scenario: Only Yahoo data exists
- **WHEN** a ticker has no stored rows
- **THEN** its block has no short-interest line, even if Yahoo would report data

<!-- tests: NONE -->

### Requirement: Research storage migration preserves data
Opening the research store SHALL create missing tables and columns and back-fill `as_of_date` from the legacy `raw_fetched_at` where it is empty, without losing rows.

#### Scenario: Legacy row
- **WHEN** a row has `raw_fetched_at` and a null `as_of_date`
- **THEN** `as_of_date` is set to that value

<!-- tests: NONE -->

### Requirement: Research is informational only
The research capability SHALL produce stored data and summaries for analysis and MUST NOT place, route or recommend orders or connect to a broker.

#### Scenario: No execution path
- **WHEN** research data is fetched or summarized
- **THEN** the only outputs are stored research and short-interest rows and log events

<!-- tests: NONE -->
