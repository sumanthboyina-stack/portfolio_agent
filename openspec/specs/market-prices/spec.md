# market-prices Specification

## Purpose
Defines how APEX obtains public market prices, shares fetches between callers, decides whether a stored price is out of date, and keeps stored holding prices and daily price history current. Prices come from Yahoo Finance through `yfinance`; the trading calendar is owned by `prediction-composition-and-buckets` and per-account position storage by `accounts-and-holdings`. Baseline at HEAD `ca450dd`.

## Requirements

### Requirement: Ticker-less funds are never sent to the price provider
Every entry point that fetches market prices SHALL drop ticker-less fund keys (tickers starting with `CIT:`) before calling the provider, whoever the caller is.

#### Scenario: Last closes for a ticker-less fund
- **WHEN** last closes are requested for only a ticker-less fund key
- **THEN** the result is empty with no as-of date and the provider is not called

#### Scenario: Daily closes and shared series for a ticker-less fund
- **WHEN** daily closes or a shared price series are requested for a ticker-less fund key
- **THEN** the result is empty

<!-- tests: tests/test_instrument_taxonomy.py::test_cit_is_excluded_from_pipeline_universe_and_provider_calls -->

### Requirement: Overlapping price requests share one fetch per ticker
The shared price-series fetch SHALL cache close series per (ticker, period, interval, adjustment) in a process-local cache with a default lifetime of 900 seconds, fetch only tickers not already warm, and cover all still-missing tickers in one provider download.

#### Scenario: Second overlapping request
- **WHEN** a request for tickers A and B is followed by a request for A, B and C within the cache lifetime
- **THEN** the provider is asked for A and B once and for C only on the second request

#### Scenario: Same ticker requested twice
- **WHEN** the same ticker is requested twice within the cache lifetime
- **THEN** the second call returns the stored series without a provider call

#### Scenario: Cache cleared
- **WHEN** the cache is invalidated
- **THEN** the next request fetches every ticker again

<!-- tests: tests/test_shared_market_snapshots.py::test_overlapping_ticker_requests_fetch_each_ticker_at_most_once, tests/test_shared_market_snapshots.py::test_two_overlapping_requests_for_the_same_ticker_reuse_the_stored_result -->

### Requirement: Shared price series are best-effort and carry no private data
The shared price-series fetch SHALL return only tickers with at least the requested minimum number of rows (default 20), SHALL omit a ticker the provider cannot supply and SHALL NOT raise when the provider download fails.

#### Scenario: Thin history
- **WHEN** a ticker has fewer rows than the minimum
- **THEN** it is absent from the result and other tickers are unaffected

#### Scenario: Provider download fails
- **WHEN** the provider download raises
- **THEN** the call returns an empty result for the uncached tickers and does not raise

<!-- tests: NONE -->

### Requirement: Last closes tolerate partial provider failure
The last-close fetch SHALL request tickers in chunks of 20, return a close per ticker it could price and the most recent trading date among them, omit tickers it could not price, and SHALL NOT let a failed chunk fail the others.

#### Scenario: One chunk fails
- **WHEN** the provider download for one chunk raises
- **THEN** closes from the other chunks are still returned

#### Scenario: Unpriceable ticker
- **WHEN** the provider returns no close for a ticker
- **THEN** that ticker is absent from the result

<!-- tests: tests/test_holdings_db.py::test_refresh_prices_reprices_every_position_in_one_pass -->

### Requirement: Daily closes omit closed days and surface provider failure
The daily-close fetch SHALL return adjusted closes per ticker from the start date (inclusive) to the end date (exclusive), SHALL leave days the market was shut absent, never zero, and SHALL raise when the provider download itself fails.

#### Scenario: Market holiday inside the range
- **WHEN** the range includes a day with no trading
- **THEN** that day has no entry for any ticker

#### Scenario: Download failure
- **WHEN** the provider download raises
- **THEN** the call raises so the caller can report the error

<!-- tests: NONE -->

### Requirement: Stock and ETF prices are judged against the 16:00 close
A stock, ETF or other market-priced instrument SHALL be stale only when its price date is older than the latest close that could exist at the current New York time: today's if it is a trading day and the time is 16:00 or later, else the previous trading day's.

#### Scenario: Morning of a trading day
- **WHEN** it is 10:00 New York time on a trading day and a stock's price is yesterday's close
- **THEN** the price is not stale

#### Scenario: After the close
- **WHEN** it is 17:00 on a trading day and an ETF's price is yesterday's close
- **THEN** the price is stale

#### Scenario: Weekend
- **WHEN** it is a Saturday
- **THEN** the expected price date is the previous trading day

<!-- tests: tests/test_fund_lookthrough.py::test_stocks_are_judged_against_the_4pm_close_and_weekends_roll_back -->

### Requirement: Mutual-fund NAVs are judged against 21:00
A `MUTUAL_FUND` or `TARGET_DATE_FUND` price SHALL be stale only when it is older than the latest NAV that could have posted: today's if it is a trading day and the time is 21:00 New York time or later, else the previous trading day's.

#### Scenario: Evening before the NAV posts
- **WHEN** it is 17:00 on a trading day and a mutual fund's price is yesterday's NAV
- **THEN** the price is not stale

#### Scenario: After the NAV should have posted
- **WHEN** it is 22:00 on a trading day and the fund's price is yesterday's NAV
- **THEN** the price is stale

<!-- tests: tests/test_fund_lookthrough.py::test_a_mutual_fund_nav_is_not_stale_until_todays_nav_has_posted -->

### Requirement: A missing or unreadable price date is unknown, never fresh
Staleness SHALL be unknown (not true and not false) when a price has no date or an unreadable date.

#### Scenario: No date
- **WHEN** a stock's price date is empty
- **THEN** staleness is unknown

#### Scenario: Unreadable date
- **WHEN** the price date is not a valid ISO date
- **THEN** staleness is unknown

<!-- tests: tests/test_fund_lookthrough.py::test_stocks_are_judged_against_the_4pm_close_and_weekends_roll_back, tests/test_statement_freshness.py::test_a_statement_priced_fund_with_no_price_date_is_unknown_not_fresh_and_market_prices_keep_their_own_rule -->

### Requirement: Statement-priced funds are identified by type or key
A holding SHALL be statement-priced when its ticker is a ticker-less fund key or its instrument type is `CIT` or `STABLE_VALUE`.

#### Scenario: Ticker-less key
- **WHEN** a holding's ticker starts with `CIT:`
- **THEN** it is statement-priced

#### Scenario: Market-priced instrument
- **WHEN** a holding is a `STOCK` or `MUTUAL_FUND` with an ordinary ticker
- **THEN** it is not statement-priced

<!-- tests: tests/test_statement_freshness.py::test_which_holdings_are_statement_priced -->

### Requirement: Statement prices age by calendar days from the statement date
A statement price SHALL be stale only when it is more than `portfolio_settings.statement_price_max_age_days` calendar days old (seed 100, allowed range 1 to 730), ignoring the market clock; exactly that many days old is still fresh.

#### Scenario: Boundary of the default window
- **WHEN** a statement price is 100 days old, then 101 days old, with the default setting
- **THEN** it is fresh at 100 days and stale at 101

#### Scenario: Weekend does not matter
- **WHEN** a statement price is 8 days old on a Saturday
- **THEN** it is fresh, while an 8-day-old stock price is stale

#### Scenario: Setting overrides the window
- **WHEN** the setting is 30 and a price is 40 days old
- **THEN** it is stale

<!-- tests: tests/test_statement_freshness.py::test_a_statement_price_is_fresh_for_exactly_the_allowed_number_of_days, tests/test_statement_freshness.py::test_a_statement_price_ignores_the_market_clock_and_market_priced_holdings_are_unchanged -->

### Requirement: Statement price window is an admin setting
`statement_price_max_age_days` SHALL be a validated, versioned, resettable `portfolio_settings` value editable only through the admin settings path, and a change SHALL take effect on the next verdict without waiting for a cache to expire.

#### Scenario: Admin changes the window
- **WHEN** an admin sets it to 120
- **THEN** the stored value is 120 and later staleness verdicts use it

<!-- tests: tests/test_statement_freshness.py::test_the_window_is_a_database_setting_with_a_default_validated_and_resettable, tests/test_statement_freshness.py::test_the_window_setting_changes_the_verdict_without_waiting_for_a_cache_to_expire -->

### Requirement: Price refresh reprices every market-priced holding atomically
The price refresh SHALL fetch the latest close for every holding with a market ticker (not only those missing a price), update current price, current value and price-as-of date in one database transaction after the fetch, leave holdings the provider could not price unchanged, leave ticker-less funds unchanged, and write one audit event `refresh_prices` that lists the failed tickers.

#### Scenario: Refresh reprices all positions
- **WHEN** a refresh runs and the provider prices every ticker
- **THEN** every market-priced position gets the new price and the observed trading date as price-as-of

#### Scenario: Hand-priced fund untouched
- **WHEN** a refresh runs with a ticker-less fund held
- **THEN** the fund's hand-entered price and value are unchanged and it is not sent to the provider

#### Scenario: Provider cannot price a ticker
- **WHEN** one ticker has no close
- **THEN** its position is unchanged and the audit event lists it as failed

<!-- tests: tests/test_holdings_db.py::test_refresh_prices_reprices_every_position_in_one_pass, tests/test_instrument_taxonomy.py::test_cit_is_excluded_from_pipeline_universe_and_provider_calls -->

### Requirement: Daily price snapshot writes one row per position
The daily snapshot SHALL write one price-history row per position per date, keyed by date, account and ticker with source `snapshot`; re-running on the same day SHALL replace each position's own row and no other.

#### Scenario: Two accounts hold one ticker
- **WHEN** two accounts at one broker hold the same ticker and the snapshot runs
- **THEN** two rows exist whose market values sum to the total

#### Scenario: Re-run on the same day
- **WHEN** the snapshot runs twice on one day
- **THEN** there are still two rows, not four

#### Scenario: Ticker-less fund
- **WHEN** a ticker-less fund is held
- **THEN** its row records the price on file and no provider call is made for it

<!-- tests: tests/test_holdings_db.py::test_snapshot_keeps_one_row_per_account, tests/test_instrument_taxonomy.py::test_cit_is_excluded_from_pipeline_universe_and_provider_calls -->

### Requirement: History backfill fills only gaps and invents nothing
The history backfill SHALL, for each weekday from the earliest holding load date to yesterday, fill price-history rows only for positions with no row, SHALL skip days that have an observed snapshot, SHALL NOT overwrite existing rows, SHALL write no row for a position or day the provider has no close for, SHALL skip ticker-less funds, SHALL be a no-op when history is complete, and SHALL return an error value instead of raising when the provider fails.

#### Scenario: Partially covered day
- **WHEN** a day has rows for one account but not another
- **THEN** only the missing position gets a row with source `backfill` and the existing row is unchanged

#### Scenario: Complete history
- **WHEN** backfill runs again with nothing missing
- **THEN** it reports zero rows backfilled

#### Scenario: No close available
- **WHEN** the provider returns no close for a day
- **THEN** no row is written for that day

#### Scenario: Provider failure
- **WHEN** the daily-close fetch raises
- **THEN** the result reports zero backfilled and carries the error text

<!-- tests: tests/test_holdings_db.py::test_backfill_fills_partial_days_per_position_without_overwriting, tests/test_instrument_taxonomy.py::test_cit_is_excluded_from_pipeline_universe_and_provider_calls -->

### Requirement: Single-date close lookup
The close lookup SHALL return the adjusted close on or just after a date, within 7 calendar days after it, and SHALL return no value, without raising, when no data exists or the provider fails.

#### Scenario: Date is a weekend
- **WHEN** the date is a Saturday
- **THEN** the close of the next trading day within 7 days is returned

#### Scenario: No data in the window
- **WHEN** the provider has no rows in the 7-day window
- **THEN** no value is returned

<!-- tests: NONE -->

### Requirement: Range close lookup includes the end date
The range lookup SHALL return `{date: adjusted close}` for every trading day from the start date through the end date inclusive, and SHALL return an empty mapping, without raising, on failure or no data.

#### Scenario: End date is a trading day
- **WHEN** the range ends on a trading day
- **THEN** that day's close is included

<!-- tests: NONE -->

### Requirement: Trailing return needs both prices
The trailing return SHALL be the ratio of the close at the as-of date to the close about the lookback window earlier, minus one (default lookback 10 days), and SHALL be unavailable when either close is missing or the earlier close is zero.

#### Scenario: Missing start price
- **WHEN** no close exists for the earlier date
- **THEN** the return is unavailable

<!-- tests: NONE -->

### Requirement: Valuation inputs never raise
The valuation-input primitive SHALL return a mapping whose every field is independently null when the provider lacks it, and SHALL NOT raise when the provider call fails.

#### Scenario: Provider call fails
- **WHEN** the provider's info lookup raises
- **THEN** the mapping is returned with the ticker set and all other fields null

<!-- tests: NONE -->

### Requirement: Market price data carries no private content
Fetches and caches in this capability SHALL carry only public tickers and prices; no owner, account or position identifier SHALL be sent to the provider or used as a cache key.

#### Scenario: Provider call content
- **WHEN** any price fetch runs for held tickers
- **THEN** only ticker symbols, periods and dates reach the provider

<!-- tests: NONE -->
