# news-ingestion Specification

## Purpose
Defines how the daily news phase gathers headlines per ticker, triages them with deterministic rules, scores materiality with a cheap model, summarizes survivors in batches, and stores summaries, filter decisions and article hashes. Its stored material-news scores are what the event detector reads. Baseline at HEAD `ca450dd`.

## Requirements

### Requirement: Per-ticker article sources
For each ticker the system SHALL fetch recent headlines from Finnhub and Yahoo Finance in parallel and use Finnhub's articles when it returned any, falling back to Yahoo only when Finnhub returned none. Articles MUST be sorted newest first, and the default look-back MUST be 48 hours.

#### Scenario: Finnhub has articles
- **WHEN** Finnhub returns articles for a ticker and Yahoo also does
- **THEN** only the Finnhub articles are used and the ticker's source is labelled `finnhub`

#### Scenario: Only Yahoo has articles
- **WHEN** Finnhub returns nothing and Yahoo returns articles
- **THEN** the Yahoo articles are used and the source is labelled `yahoo_fallback`

#### Scenario: Neither has articles
- **WHEN** both sources return nothing
- **THEN** the ticker has no articles and its source is labelled `none`

<!-- tests: NONE -->

### Requirement: Fetch failures degrade to empty
A failed or empty fetch for a source or a ticker SHALL yield an empty article list and MUST NOT stop the phase. With no Finnhub API key configured, Finnhub fetches SHALL return empty lists without any request being made.

#### Scenario: Missing Finnhub key
- **WHEN** the Finnhub API key is not configured
- **THEN** every ticker's Finnhub list is empty and no request is sent to Finnhub

#### Scenario: Provider error
- **WHEN** a Yahoo or Finnhub request raises an error for one ticker
- **THEN** that ticker gets an empty list from that source and other tickers are unaffected

<!-- tests: NONE -->

### Requirement: Finnhub request pacing
Finnhub requests SHALL be paced process-wide to at least 1.2 seconds apart, with a 10-second timeout. A rate-limit response (HTTP 429) on a company-news request MUST be retried once after 30 seconds, then given up as empty.

#### Scenario: Parallel callers
- **WHEN** several threads request Finnhub news at once
- **THEN** their requests are spaced at least 1.2 seconds apart

#### Scenario: Rate limited twice
- **WHEN** a company-news request receives HTTP 429 on both attempts
- **THEN** the ticker's list is empty

<!-- tests: NONE -->

### Requirement: Normalised article shape
Each fetched article SHALL carry `title`, `publisher`, `published_at` (ISO 8601 UTC), `url`, `summary` and `_source` (`finnhub` or `yahoo`). Finnhub articles with no timestamp, no headline, or older than the look-back window MUST be dropped, and a Finnhub summary MUST be truncated to 500 characters.

#### Scenario: Old article dropped
- **WHEN** a Finnhub article was published before the look-back cutoff
- **THEN** it is not returned

#### Scenario: Long summary
- **WHEN** a Finnhub summary exceeds 500 characters
- **THEN** it is returned truncated to 500 characters

<!-- tests: NONE -->

### Requirement: Keyword triage tiers
Stage 2 triage SHALL match article titles, case-insensitively, against three keyword tiers and mark a ticker interesting when any one HIGH keyword matches, or two distinct MEDIUM keywords match, or three distinct LOW keywords match. HIGH includes `earnings`, `eps`, `merger`, `acquisition`, `fda`, `lawsuit`, `ceo`, `layoffs` and `guidance`. LOW is `forecast` and `outlook`.

#### Scenario: One HIGH keyword
- **WHEN** a ticker's headlines contain "earnings"
- **THEN** the ticker is interesting with reason listing the matched keyword

#### Scenario: One MEDIUM keyword only
- **WHEN** a ticker's headlines match exactly one MEDIUM keyword and nothing else
- **THEN** the ticker is not interesting

#### Scenario: Two MEDIUM keywords
- **WHEN** a ticker's headlines match two distinct MEDIUM keywords such as "upgrade" and "dividend"
- **THEN** the ticker is interesting

<!-- tests: NONE -->

### Requirement: Volume-spike triage
Triage SHALL also mark a ticker interesting when at least 3 articles published in the last 24 hours come from at least 2 distinct publishers. A ticker with no articles MUST be not interesting with reason `no_news_48h`, and triage MUST NOT call any language model.

#### Scenario: Multi-publisher surge
- **WHEN** a ticker has 3 articles from 2 different publishers in the last 24 hours and no keyword match
- **THEN** it is interesting with reason `volume_spike:3art/2pubs`

#### Scenario: Single publisher repeated
- **WHEN** a ticker has 3 articles in 24 hours all from one publisher and no keyword match
- **THEN** it is not interesting

<!-- tests: NONE -->

### Requirement: Triage output carries a bounded article set
Triage output SHALL include, per ticker, at most 12 articles and at most 5 matched keywords per tier, and SHALL include the full merged article list (up to 12) for every ticker so later stages reuse it without a second fetch.

#### Scenario: Reuse of fetched articles
- **WHEN** triage completes for a list of tickers
- **THEN** the per-ticker article lists are available to the scoring and summarizing stages with no further Finnhub request

<!-- tests: NONE -->

### Requirement: Materiality scoring
Stage 3 SHALL score each interesting ticker 0 to 3 with one short summary, in chunks of at most 35 tickers, using the `triage` model chain and a direct completion at temperature 0. Scores for tickers not in the chunk MUST be discarded, and a ticker missing from the model's reply MUST count as score 0.

#### Scenario: Hallucinated ticker
- **WHEN** the model returns a score for a ticker that was not in the input chunk
- **THEN** that score is discarded and logged as a warning

#### Scenario: Chunking
- **WHEN** 80 tickers are interesting
- **THEN** they are scored in three calls of at most 35 tickers

<!-- tests: NONE -->

### Requirement: Keyword fallback when no scorer is available
When every scoring model in the chain fails, the system SHALL score from keywords instead: a ticker with a strong HIGH keyword gets score 2 with summary `keyword-fallback`, and every other ticker gets score 0 with summary `scorer-unavailable`. Volume-spike-only tickers MUST get 0.

#### Scenario: Scorer outage
- **WHEN** all scorer models fail and a ticker matched "earnings"
- **THEN** it scores 2 and a ticker with only a volume spike scores 0

<!-- tests: NONE -->

### Requirement: Portfolio and extra tickers bypass scoring
Tickers that are held in a portfolio or passed as extras SHALL skip Stage 3 and go to summarizing when they have any articles, recorded with stage-3 decision `bypassed_portfolio` and a null score. Other tickers MUST be summarized only when their Stage 3 score is above 0.

#### Scenario: Held ticker with articles
- **WHEN** a held ticker has articles but was never scored
- **THEN** it is summarized and its filter-log row has stage-3 decision `bypassed_portfolio` and no score

#### Scenario: Watchlist ticker scored 0
- **WHEN** a non-held ticker scores 0
- **THEN** it is dropped with skip reason `low_materiality`

<!-- tests: NONE -->

### Requirement: Unchanged articles are skipped
A ticker whose current article titles were all already seen in the last 7 days SHALL be skipped with reason `no_new_articles`, using a lower-cased, trimmed title hash. Tickers carried over from a previous incomplete run MUST NOT be skipped by this rule, and a ticker with no articles MUST be skipped with reason `no_articles`.

#### Scenario: Same headlines as yesterday
- **WHEN** every title of a ticker's articles was stored in the hash table within 7 days
- **THEN** the ticker is skipped as `no_new_articles`

#### Scenario: Resumed ticker
- **WHEN** a ticker was left pending by a previous run's checkpoint
- **THEN** it is summarized even if its titles were seen before

<!-- tests: NONE -->

### Requirement: Restricted skip list applies to the news phase
Tickers configured to skip the news phase, or all phases, SHALL be removed before any fetching and the skip MUST be logged. The skip list itself is owned by compliance-controls.

#### Scenario: Ticker skipped for news
- **WHEN** a ticker's pipeline-skip entry lists the news phase
- **THEN** no articles are fetched for it and it does not appear in the filter log

<!-- tests: NONE -->

### Requirement: Filter decision audit log
Every ticker considered in a news run SHALL get one `news_filter_log` row per date, upserted by (date, ticker), recording the stage-2 decision (`interesting` or `routine`), reason and matched keywords, the stage-3 decision (`pass`, `fail`, `bypassed_portfolio` or `skipped_stage2`), score and summary, and the final decision (`analyzed` or `dropped`). A logging failure MUST NOT stop the run.

#### Scenario: Re-run on the same day
- **WHEN** the news phase runs twice on one date for the same ticker
- **THEN** the table keeps one row for that ticker and date holding the latest decision

#### Scenario: Log write fails
- **WHEN** the filter-log write raises an error
- **THEN** a warning is logged and the phase continues

<!-- tests: tests/test_review_findings.py::test_run_all_detectors_detects_and_persists_material_news -->

### Requirement: Batched summarization
Tickers that pass filtering SHALL be summarized by the `triage` model chain in batches of 5, each ticker's block containing its 5 best articles ranked by keyword strength, publisher tier, recency and title uniqueness (word overlap above 60% counts as a duplicate). Each result MUST give a headline, sentiment POSITIVE, NEUTRAL or NEGATIVE, a score from -1.0 to 1.0, up to 3 themes and a trend note.

#### Scenario: Duplicate headlines
- **WHEN** two articles share more than 60% of their title words
- **THEN** only the higher-ranked one is included in the ticker's block

#### Scenario: Batch size
- **WHEN** 12 tickers are summarized
- **THEN** three model calls are made, of 5, 5 and 2 tickers

<!-- tests: NONE -->

### Requirement: Summarization failover and checkpoint
If a model fails with a failover-class error or returns no JSON array, the system SHALL advance to the next model in the chain. If a prompt is too large, it MUST retry that batch's tickers one at a time. When all models are exhausted it SHALL save a checkpoint of the remaining tickers and report the phase as not completed.

#### Scenario: Model returns prose
- **WHEN** a model's reply contains no JSON array
- **THEN** the next model in the chain is tried for that batch

#### Scenario: Chain exhausted
- **WHEN** every model has failed
- **THEN** the unfinished tickers are saved in a checkpoint and the phase returns failure

<!-- tests: NONE -->

### Requirement: Stored ticker news summary
The system SHALL store one summary per ticker per day (Central time date) in `news_daily_update` with row type `ticker`, upserted on repeat, together with the source (`finnhub` or `yahoo_fallback`) and the model name and provider that produced it. A ticker that got no analysis MUST be counted as failed and no row written.

#### Scenario: Repeat save same day
- **WHEN** a ticker's summary is saved twice on one date
- **THEN** one row exists and the second save reports `updated`

#### Scenario: No analysis returned
- **WHEN** the model's batch reply omits a ticker
- **THEN** no row is written for it and it is reported as failed

<!-- tests: NONE -->

### Requirement: Stored market news items
Market news items SHALL be stored in `news_daily_update` with row type `market_item` and ticker `MARKET`, unique per date and headline, so the same headline from another source is rejected as a duplicate.

#### Scenario: Same headline from two feeds
- **WHEN** an item is saved with a headline already stored today
- **THEN** it is not inserted and the result reason is `duplicate`

<!-- tests: NONE -->

### Requirement: Market RSS headlines
Market headlines SHALL be read from five RSS feeds (`reuters_business`, `seeking_alpha`, `marketwatch`, `yahoo_finance`, `cnbc`), de-duplicated by exact title, with summaries cut to 300 characters. An unreachable feed MUST be skipped without error, and an unknown feed name MUST return an error naming the valid feeds.

#### Scenario: One feed down
- **WHEN** one feed cannot be parsed
- **THEN** headlines from the other feeds are still returned

#### Scenario: Unknown feed name
- **WHEN** a headline request names a feed outside the five
- **THEN** the result is an error listing the valid feed names

<!-- tests: NONE -->

### Requirement: Market context for summarization
Each news run SHALL condense up to 5 market headlines into 3 to 5 short bullets with the first model of the `triage` chain, and MUST fall back to raw headline bullets if that call fails, or to the text `(not available)` if headlines cannot be fetched.

#### Scenario: Condensing fails
- **WHEN** the condensing call raises an error
- **THEN** the top 3 raw headlines are used as the market context

#### Scenario: No headlines
- **WHEN** the market headlines cannot be fetched at all
- **THEN** the market context is `(not available)`

<!-- tests: NONE -->

### Requirement: Finnhub sentiment signal
The per-ticker sentiment lookup SHALL report bullish and bearish percentages, last-week article count and company score, with signal `BULLISH` when bullish is at least 60, `BEARISH` when bearish is at least 60, otherwise `NEUTRAL`, and an `unavailable` error when Finnhub gives nothing.

#### Scenario: Bullish reading
- **WHEN** Finnhub reports 65% bullish and 20% bearish
- **THEN** the signal is `BULLISH`

#### Scenario: No data
- **WHEN** Finnhub returns no sentiment or the key is missing
- **THEN** the result is an error `unavailable`

<!-- tests: NONE -->

### Requirement: Material-news score feeds event detection
The event detector SHALL treat a ticker as having material news when its `news_filter_log` row for the date has a non-null `stage_3_score` at or above the severity threshold (default 3) and final decision `analyzed`, and SHALL record the score as the event's severity. Rows with no score, such as bypassed portfolio tickers, MUST NOT produce an event.

#### Scenario: Scored at threshold
- **WHEN** a ticker's log row has score 3 and final decision `analyzed`
- **THEN** a `material_news` event with severity 3 is recorded

#### Scenario: Bypassed ticker
- **WHEN** a ticker's log row has a null score
- **THEN** no material-news event is recorded for it

<!-- tests: tests/test_notification_producers.py::test_the_events_end_to_end_through_detect_material_news, tests/test_review_findings.py::test_run_all_detectors_detects_and_persists_material_news -->

### Requirement: Company name lookup
Company names SHALL come from the SEC ticker file, cached on disk for 24 hours and in memory for 5 minutes. An unknown ticker, such as an ETF or foreign ADR, MUST return no name, and a failed refresh MUST use the stale cache or an empty map. Search MUST rank ticker-prefix matches, then name-prefix, then name-contains, each alphabetical, limited to 8 by default.

#### Scenario: ETF has no name
- **WHEN** a name is requested for a ticker not in the SEC file
- **THEN** no name is returned and callers show the bare ticker

#### Scenario: Search ranking
- **WHEN** a fragment matches one ticker prefix and one company-name prefix
- **THEN** the ticker-prefix match is listed first

<!-- tests: NONE -->
