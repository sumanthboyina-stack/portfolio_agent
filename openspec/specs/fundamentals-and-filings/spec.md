# fundamentals-and-filings Specification

## Purpose
Defines how APEX turns SEC filings into stored company fundamentals: which tickers are processed, how new filings are detected, how annual XBRL figures and the latest 8-K guidance are fetched, how the LLM batch analysis is run and stored, and what is guaranteed when data is missing. Baseline at HEAD `ca450dd`. The data is shared market data: it carries no owner and never holds private portfolio information.

## Requirements

### Requirement: Only company filers are processed
The fundamentals phase SHALL leave out funds, money-market funds, cash, stable-value funds and ticker-less funds before any EDGAR check, prefetch or 8-K guidance fetch, and an unclassified ticker MUST stay on the filer path.

#### Scenario: Funds never reach EDGAR
- **WHEN** the phase runs for one stock and two funds
- **THEN** the filing check and the prefetch receive only the stock
- **AND** no 8-K guidance fetch is made for the funds
- **AND** the log names the skipped funds as routing, not as a data error

#### Scenario: Unclassified ticker
- **WHEN** a ticker has no stored instrument type
- **THEN** it is treated as a filer

<!-- tests: tests/test_etf_routing.py::test_fundamentals_phase_never_reaches_edgar_or_guidance_for_fund_tickers, tests/test_etf_routing.py::test_split_company_filers_separates_funds_cash_and_ciT_from_filers -->

### Requirement: Globally skipped tickers are excluded
The fundamentals phase SHALL exclude every ticker on the pipeline skip list for the `fundamentals` phase, or for all phases, and MUST log them.

#### Scenario: Ticker skipped for fundamentals
- **WHEN** a ticker is configured to skip phase `fundamentals`
- **THEN** it is removed from the phase's ticker list and a skip line is logged

<!-- tests: NONE -->

### Requirement: Ticker-to-CIK lookup
The system SHALL resolve a ticker to a zero-padded CIK from SEC's company tickers file, cache the map locally for 24 hours, and treat a ticker with no CIK as having no EDGAR data.

#### Scenario: Fresh cache
- **WHEN** the cached map is younger than 24 hours
- **THEN** it is used and SEC's tickers file is not downloaded

#### Scenario: Unknown ticker
- **WHEN** a ticker is not in the map
- **THEN** its bundle is an error `no_cik` and it is counted as skipped for no EDGAR data

<!-- tests: NONE -->

### Requirement: SEC request identity and concurrency
SEC requests SHALL carry the User-Agent from the `EDGAR_USER_AGENT` environment variable, with a generic placeholder when it is unset, and batch checks and prefetches MUST use at most 4 parallel workers.

#### Scenario: Header from environment
- **WHEN** `EDGAR_USER_AGENT` is set when the module loads
- **THEN** every SEC request uses it as its User-Agent

#### Scenario: Parallelism cap
- **WHEN** a batch check or prefetch runs for many tickers
- **THEN** at most 4 requests are in flight at once

<!-- tests: NONE -->

### Requirement: New-filing detection
The system SHALL find each ticker's most recent 10-K or 10-Q from SEC's submissions data and return its form type, filing date, period of report, accession and filing folder URL, or nothing when the CIK is unknown or the request fails.

#### Scenario: Latest 10-K or 10-Q
- **WHEN** a company's recent filings include a 10-Q newer than its last 10-K
- **THEN** the 10-Q's details are returned

#### Scenario: Request failure
- **WHEN** the submissions request fails
- **THEN** the result for that ticker is none and no error is raised

<!-- tests: NONE -->

### Requirement: Refresh decision
A ticker SHALL be refreshed when it has no stored row, when EDGAR shows a filing date later than the stored one, or when its stored as-of date is more than 95 days old, and otherwise MUST be skipped as current.

#### Scenario: No stored data
- **WHEN** a ticker has no stored fundamentals
- **THEN** the decision is refresh with reason `no_data`

#### Scenario: New filing
- **WHEN** EDGAR's latest filing date is later than the stored filing date
- **THEN** the decision is refresh with reason `new_filing`

#### Scenario: Stale row
- **WHEN** the stored as-of date is 96 days old and no newer filing exists
- **THEN** the decision is refresh with a `stale_96d` reason

#### Scenario: Current
- **WHEN** none of those apply
- **THEN** the ticker is skipped as `current`

<!-- tests: NONE -->

### Requirement: Forced and resumed tickers
Tickers in the run's always-run set SHALL be refreshed even when current, and tickers left pending by a previous run's checkpoint MUST be resumed first with reason `resume`.

#### Scenario: Always-run ticker
- **WHEN** a held ticker is current but is in the always-run set
- **THEN** it is refreshed with reason `forced`

#### Scenario: Pending from yesterday
- **WHEN** the checkpoint holds pending fundamentals tickers from an earlier day
- **THEN** they are processed ahead of the other tickers and labelled `resume`

<!-- tests: NONE -->

### Requirement: Annual XBRL figures
The system SHALL read financial figures from SEC company facts using only 10-K and 10-K/A entries, accept only point-in-time facts or durations of 300 to 400 days, de-duplicate by period end and value, and return at most the 4 most recent periods.

#### Scenario: Quarterly slices excluded
- **WHEN** a 10-K tags quarterly figures under the same concept as the annual figure
- **THEN** the quarterly entries are ignored

#### Scenario: Four periods
- **WHEN** more than four annual periods exist
- **THEN** only the four most recent are returned, newest first

<!-- tests: NONE -->

### Requirement: Revenue concept choice
The system SHALL consider every supported revenue concept and use the series whose most recent period is the latest, so a retired tag with old data does not hide a current one.

#### Scenario: Concept migration
- **WHEN** a company's old revenue tag stops at an earlier year and its newer tag has later years
- **THEN** the newer tag's series is used

<!-- tests: NONE -->

### Requirement: Fundamentals bundle
For each refreshed ticker the system SHALL assemble one bundle of income, balance-sheet and cash-flow series, the latest 10-K date and the 8-K guidance, fetching company facts once, and a ticker whose facts cannot be fetched MUST come back as an error entry and be skipped for lack of EDGAR data.

#### Scenario: One facts download
- **WHEN** a bundle is built
- **THEN** income, balance and cash-flow series all come from a single company-facts response

#### Scenario: Facts failure
- **WHEN** the company-facts request fails
- **THEN** the bundle is an error entry and the ticker is not sent to the LLM

<!-- tests: NONE -->

### Requirement: Latest 8-K guidance retrieval
The system SHALL fetch the exhibit EX-99.1 of the most recent filing whose form is exactly `8-K`, strip markup, and return the guidance text with the filing date and accession, or a reason of `no_cik`, `no_8k`, `no_ex991` or the error message.

#### Scenario: Exhibit found
- **WHEN** the latest 8-K folder lists an EX-99.1 file
- **THEN** guidance text, filing date and accession are returned

#### Scenario: No exhibit
- **WHEN** the folder lists no EX-99.1 file
- **THEN** guidance text is empty, the filing date is kept and the reason is `no_ex991`

<!-- tests: NONE -->

### Requirement: Guidance section extraction
The system SHALL pick the guidance or outlook section of the exhibit using ordered text patterns, the most specific first, falling back to the start of the text, and cap the extract at 3,000 characters; the analysis prompt MUST carry at most 2,500 of them.

#### Scenario: Pattern match
- **WHEN** the text contains "providing the following guidance"
- **THEN** the extract starts shortly before that phrase

#### Scenario: No pattern
- **WHEN** no pattern matches
- **THEN** the extract is the first 3,000 characters

#### Scenario: Prompt block cap
- **WHEN** the guidance text is 3,000 characters
- **THEN** the data block given to the model includes only its first 2,500

<!-- tests: NONE -->

### Requirement: Missing guidance never blocks analysis
A ticker with no retrievable guidance SHALL still be analysed and stored, with empty guidance text and a guidance direction of `none`.

#### Scenario: No 8-K
- **WHEN** guidance retrieval returns `no_8k`
- **THEN** the ticker is analysed from financial data alone

<!-- tests: NONE -->

### Requirement: Batch analysis
The system SHALL send filers to the LLM 10 tickers per call on the `flash` model tier, require a JSON array in the input order with revenue growth, net margin, free cash flow, debt-to-equity, a 1 to 10 fundamental score, two strengths, two risks, a summary and a guidance direction, and instruct the model to use only the data shown.

#### Scenario: Batch size
- **WHEN** 25 tickers need analysis
- **THEN** three calls are made carrying 10, 10 and 5 tickers

#### Scenario: Not computable
- **WHEN** a metric cannot be computed from the data
- **THEN** the prompt tells the model to return null for it

<!-- tests: NONE -->

### Requirement: Guidance direction values
The analysis SHALL ask for a guidance direction of `raised`, `maintained`, `lowered`, `withdrawn` or `none`, using `none` when no guidance text is given; the score adjustment (1 to 2 points up for a raise, 1 to 2 down for lowered or withdrawn) is an instruction in the prompt and is not enforced in code.

#### Scenario: No guidance text
- **WHEN** a ticker has no guidance text
- **THEN** the prompt instructs `none` and a score from financial data alone

#### Scenario: Value not checked
- **WHEN** the model returns a direction outside the five values
- **THEN** it is stored as returned

<!-- tests: NONE -->

### Requirement: Model failover and checkpoint
When a model fails with a failover-class error the phase SHALL move to the next `flash` model, and when every model is exhausted it MUST save the unfinished tickers as the pending checkpoint and end the phase unsuccessfully so they resume on the next run.

#### Scenario: Model failure
- **WHEN** the current model returns a provider error
- **THEN** the same batch is retried on the next model in the chain

#### Scenario: All models exhausted
- **WHEN** no model remains
- **THEN** the checkpoint lists the unfinished tickers and the phase returns failure

<!-- tests: NONE -->

### Requirement: Fundamentals storage
The system SHALL store one fundamentals row per ticker and filing date, inserting a new row or updating the existing one, and report whether it inserted or updated.

#### Scenario: Re-run for the same filing
- **WHEN** a ticker is analysed twice for the same filing date
- **THEN** one row exists and the second write is an update

#### Scenario: Missing filing date
- **WHEN** no filing date is known
- **THEN** the as-of date is used as the key

<!-- tests: NONE -->

### Requirement: Guidance fields survive empty updates
An update SHALL replace stored guidance text, date, direction and model fields only with non-empty new values.

#### Scenario: Update without guidance
- **WHEN** a row with stored guidance is updated with no new guidance text
- **THEN** the stored guidance text is kept

<!-- tests: NONE -->

### Requirement: Partial batch results are kept
A ticker for which the model returned no analysis SHALL be counted as failed without affecting the other tickers in its batch, and a save error for one ticker MUST NOT stop the rest.

#### Scenario: One ticker missing from the reply
- **WHEN** a batch reply omits one ticker
- **THEN** that ticker is failed, the others are saved, and the phase still completes

<!-- tests: NONE -->

### Requirement: Latest fundamentals read
Readers SHALL get the stored row with the latest filing date for a ticker, or nothing when none exists.

#### Scenario: Two filings stored
- **WHEN** a ticker has rows for two filing dates
- **THEN** the later filing's row is returned

<!-- tests: NONE -->

### Requirement: Fundamentals are shared and deterministic where possible
Fundamentals rows SHALL carry no owner and no portfolio data, and the filing check, XBRL extraction, guidance retrieval and refresh decision MUST use no LLM.

#### Scenario: Shared table
- **WHEN** two owners hold the same ticker
- **THEN** both read the same fundamentals row

<!-- tests: NONE -->

### Requirement: Interactive fundamentals specialist
The interactive fundamentals specialist SHALL expose tools for the income statement, balance sheet, cash flow and recent filings, each returning an error object when the ticker has no CIK, and answer with a single JSON object with the metric fields, a 1 to 10 score, strengths, risks and a summary.

#### Scenario: Unknown ticker
- **WHEN** a statement tool is called for a ticker with no CIK
- **THEN** it returns an error saying the CIK was not found

<!-- tests: NONE -->
