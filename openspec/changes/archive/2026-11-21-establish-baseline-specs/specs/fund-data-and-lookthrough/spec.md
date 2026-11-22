## Purpose

Defines how APEX obtains, merges and stores market data about funds (holdings, sector and asset-class mix, expense ratio) from several sources, and how the deterministic look-through engine opens funds so exposure is counted by what a person actually holds. Baseline at HEAD `ca450dd`. Fund tables are shared market data; manual breakdowns are private to one owner.

## ADDED Requirements

### Requirement: Provider results never raise
The system SHALL express every missing or failed piece of fund data as a reason code on the returned fund data, never as an exception into the caller.

#### Scenario: Source raises
- **WHEN** a source throws while fetching a fund
- **THEN** its result carries `FUND_FETCH_FAILED` and the caller continues

#### Scenario: Reason-code vocabulary
- **WHEN** data is missing or stale
- **THEN** codes are drawn from `FUND_FETCH_FAILED`, `FUND_PROFILE_MISSING`, `FUND_HOLDINGS_MISSING`, `FUND_SECTORS_MISSING`, `FUND_ASSET_CLASSES_MISSING`, `FUND_HOLDINGS_TOP_N_ONLY`, `NPORT_NOT_FILED`, `NPORT_NOT_CONFIGURED`, `ISSUER_SOURCE_NOT_CONFIGURED`, `FUND_HOLDINGS_UNMAPPED`, `FUND_DATA_STALE` and `FUND_HOLDINGS_KEPT_PREVIOUS`

<!-- tests: tests/test_fund_provider_chain.py::test_nothing_usable_gives_the_union_of_reason_codes_and_no_data, tests/test_fund_lookthrough.py::test_provider_failures_become_reason_codes_and_never_raise, tests/test_nport_provider.py::test_failures_become_reason_codes_never_exceptions -->

### Requirement: Yahoo Finance fund source exposes top holdings only
The Yahoo Finance source SHALL supply profile, top-10 holdings, sector mix and asset-class mix, and MUST flag holdings it returns with `FUND_HOLDINGS_TOP_N_ONLY`.

#### Scenario: Top holdings returned
- **WHEN** the source returns holdings for a fund
- **THEN** the result includes `FUND_HOLDINGS_TOP_N_ONLY` and its holdings coverage is the sum of those holdings' weights, capped at 1

#### Scenario: Lookup fails
- **WHEN** the lookup for a ticker fails
- **THEN** the result has `FUND_FETCH_FAILED` and no data

<!-- tests: NONE -->

### Requirement: Provider order and limits are stored settings
The provider chain SHALL ask sources in the order stored in `portfolio_settings.fund_provider_order_json`, seeded `["issuer_csv", "nport", "yfinance"]`, and MUST ignore unknown names in a stored order.

#### Scenario: Seeded order and limits
- **WHEN** the settings are at their seed
- **THEN** the order is `issuer_csv`, `nport`, `yfinance`, with maximum data ages 3, 150 and 7 days and `fund_holdings_complete_pct` 90

#### Scenario: Invalid order refused
- **WHEN** an admin saves an empty order, an unknown source or a repeated source
- **THEN** the change is refused

<!-- tests: tests/test_fund_provider_chain.py::test_defaults_order_limits_and_provides_are_what_the_spec_says, tests/test_fund_provider_chain.py::test_default_chain_is_built_from_the_database_settings_and_can_be_reordered, tests/test_fund_provider_chain.py::test_order_is_respected_and_a_failing_source_never_stops_the_chain -->

### Requirement: A source is asked only if it could add a missing piece
The chain SHALL skip a source whose possible pieces are already in hand from fresh data. Issuer files provide holdings, sectors and asset classes; N-PORT provides holdings and asset classes; Yahoo Finance provides all of these plus profile.

#### Scenario: Complete issuer file
- **WHEN** a fresh issuer file explains at least `fund_holdings_complete_pct` of the fund and has a sector mix
- **THEN** N-PORT is not called and Yahoo Finance is still asked for the profile

<!-- tests: tests/test_fund_provider_chain.py::test_each_piece_comes_from_its_best_source_and_the_merge_says_which -->

### Requirement: Staleness is judged per source on the data's own date
A source's data SHALL count as fresh when its as-of date is within that source's limit (`issuer_csv_max_age_days` 3, `nport_max_age_days` 150, `yfinance_max_age_days` 7); data with no as-of date is fresh. Stale data MUST be passed over for the next source and, when only stale data exists, used and flagged `FUND_DATA_STALE`.

#### Scenario: Stale issuer file passed over
- **WHEN** the issuer file is older than 3 days and an N-PORT filing is within 150 days
- **THEN** the N-PORT holdings are used
- **AND** a sector mix available only from the stale file is used and the fund carries `FUND_DATA_STALE`

#### Scenario: Everything stale
- **WHEN** every source with data is past its limit
- **THEN** the best-coverage data is used and `FUND_DATA_STALE` is recorded, not hidden

#### Scenario: Limit raised
- **WHEN** the issuer limit is raised above the file's age
- **THEN** the issuer file counts as fresh again

<!-- tests: tests/test_fund_provider_chain.py::test_stale_source_is_passed_over_and_if_all_are_stale_the_best_is_used_with_a_code -->

### Requirement: Merge per piece and record each piece's source
The chain SHALL take holdings from the fresh source explaining the most of the fund (ties go to the earlier source in the order), and the derivative/short weight, as-of date and source from that holdings source. It SHALL take the asset-class mix from the holdings source if it has one, the sector mix from the first fresh source that has one, and profile fields one by one from the first source that has each. The merged data MUST record which source supplied each piece.

#### Scenario: Issuer holdings with Yahoo Finance profile
- **WHEN** the issuer file, N-PORT and Yahoo Finance all return data
- **THEN** holdings, as-of date and sectors come from the issuer file, the expense ratio and family from Yahoo Finance, and the source detail names each

#### Scenario: Mutual fund
- **WHEN** only N-PORT and Yahoo Finance return data for a mutual fund
- **THEN** holdings, derivative/short weight and asset-class mix come from N-PORT, and sectors and expense ratio from Yahoo Finance

#### Scenario: Reason codes pruned
- **WHEN** the merge ends with holdings, sectors, asset classes or a profile
- **THEN** the matching "missing" codes and the "not filed or not configured" codes are dropped, and `FUND_HOLDINGS_TOP_N_ONLY` stays only when the holdings came from Yahoo Finance

<!-- tests: tests/test_fund_provider_chain.py::test_each_piece_comes_from_its_best_source_and_the_merge_says_which, tests/test_fund_provider_chain.py::test_a_mutual_fund_gets_nport_holdings_and_yfinance_sectors_and_profile, tests/test_fund_provider_chain.py::test_order_is_respected_and_a_failing_source_never_stops_the_chain -->

### Requirement: A failing source never stops the chain
The chain SHALL continue to the next source when one fails, and when no source returns usable data SHALL return no data with the union of the sources' reason codes.

#### Scenario: All sources unusable
- **WHEN** the issuer source is not configured, no N-PORT filing exists and Yahoo Finance fails
- **THEN** the result has no data and lists `ISSUER_SOURCE_NOT_CONFIGURED`, `NPORT_NOT_FILED` and `FUND_FETCH_FAILED`

<!-- tests: tests/test_fund_provider_chain.py::test_nothing_usable_gives_the_union_of_reason_codes_and_no_data, tests/test_fund_provider_chain.py::test_order_is_respected_and_a_failing_source_never_stops_the_chain -->

### Requirement: Fund data is saved atomically
Saving a fund's data SHALL replace its profile, holdings, sector weights and asset-class weights in one transaction, so a failed save leaves the previous data intact.

#### Scenario: Save fails half way
- **WHEN** a holding row is invalid during a save
- **THEN** the save raises and the previously stored holdings, as-of date and source are unchanged

<!-- tests: tests/test_fund_provider_chain.py::test_holdings_replace_is_all_or_nothing, tests/test_fund_provider_chain.py::test_source_detail_and_new_columns_round_trip_and_migrate_idempotently -->

### Requirement: A refresh without holdings keeps the stored holdings
A refresh that returns no holdings SHALL NOT erase stored holdings; it SHALL keep the previous list, coverage, as-of date and source and add `FUND_HOLDINGS_KEPT_PREVIOUS`, while still updating other pieces such as the expense ratio.

#### Scenario: Profile-only refresh
- **WHEN** a fund with stored holdings is refreshed and the source returns only a profile
- **THEN** the stored holdings and their date remain, the new expense ratio is saved and `FUND_HOLDINGS_KEPT_PREVIOUS` is recorded

<!-- tests: tests/test_fund_provider_chain.py::test_a_refresh_without_holdings_keeps_the_stored_holdings_and_says_so -->

### Requirement: Refresh is time-guarded per fund type
The refresh SHALL skip a fund whose stored data is younger than its time limit, `etf_data_ttl_hours` (24) for ETFs and ETNs and `mutual_fund_data_ttl_hours` (168) for mutual funds and target-date funds, unless forced, and MUST report a status per ticker.

#### Scenario: Within the limit
- **WHEN** a fund fetched earlier is refreshed again without force
- **THEN** its status is `fresh` and the provider is not called

#### Scenario: Different limits by type
- **WHEN** 30 hours have passed
- **THEN** an ETF is fetched again and a mutual fund is `fresh`

#### Scenario: Forced
- **WHEN** a refresh is forced
- **THEN** the fund is fetched regardless of age

<!-- tests: tests/test_fund_lookthrough.py::test_refresh_stores_shared_fund_data_and_is_ttl_guarded, tests/test_fund_lookthrough.py::test_etf_and_mutual_fund_use_their_own_ttl_settings -->

### Requirement: Refresh does network work outside transactions and never raises
Fund refresh SHALL fetch up to four funds at a time with no database transaction open, save each fund in its own short transaction, and return a status per ticker (`fetched`, `fresh`, `no_data`, `skipped:<type>`, `via_proxy:<PROXY>`) instead of raising.

#### Scenario: No write lock during fetch
- **WHEN** a provider fetch is running
- **THEN** another connection can take the database write lock

#### Scenario: Provider failure
- **WHEN** a provider raises or returns nothing for a ticker
- **THEN** its status is `no_data`, nothing is stored for it and its view is UNKNOWN

<!-- tests: tests/test_fund_lookthrough.py::test_fetch_happens_outside_any_transaction, tests/test_fund_lookthrough.py::test_provider_failures_become_reason_codes_and_never_raise -->

### Requirement: Refresh opens funds inside funds
Refresh SHALL fetch the funds held inside a mutual fund or target-date fund, to `lookthrough_max_depth` levels (seed 3, range 1 to 6), never fetching a fund twice in one run.

#### Scenario: Target-date fund with a cycle
- **WHEN** a target-date fund holds two index funds and one of them holds the parent back
- **THEN** each of the three funds is fetched once, and the parent's holdings are marked as funds

#### Scenario: Depth lowered
- **WHEN** `lookthrough_max_depth` is 1
- **THEN** the funds inside the target-date fund are not fetched

<!-- tests: tests/test_fund_lookthrough.py::test_refresh_recurses_through_a_target_date_fund_with_a_cycle_guard_and_the_depth_setting, tests/test_identifier_mapping.py::test_refresh_maps_identifiers_before_saving_and_opens_the_funds_inside -->

### Requirement: Held funds in any portfolio are refreshed on schedule
The system SHALL refresh every fund held in any portfolio through the provider chain in the daily APEX phase (for the forecast tickers) and in the evening batch (every held fund), and an administrator's "refresh now" SHALL do the same while ignoring the time limits.

#### Scenario: Evening refresh set
- **WHEN** portfolios hold an ETF, a stock and a mutual fund
- **THEN** only the ETF and the mutual fund are refreshed, and an unchanged second run reports both `fresh`

#### Scenario: Refresh now
- **WHEN** an admin runs refresh now
- **THEN** both funds are fetched again regardless of age and an audit event `refresh_fund_data_now` is written

#### Scenario: Not an admin
- **WHEN** a non-admin runs refresh now
- **THEN** it is refused

<!-- tests: tests/test_fund_provider_chain.py::test_refresh_all_covers_every_held_fund_in_any_portfolio_and_admin_refresh_ignores_the_ttl, tests/test_fund_provider_chain.py::test_refresh_now_is_admin_only, tests/test_fund_provider_chain.py::test_evening_batch_calls_the_fund_refresh -->

### Requirement: Shared fund tables hold market data only
The shared fund tables SHALL hold only data about the fund itself, and the shared forecast path MUST NOT import or read private manual breakdowns, proxies, holdings or profile data. A fund's forecast context MUST be UNKNOWN with `FUND_DATA_UNAVAILABLE` when no shared data is stored, including for a collective trust with a proxy or manual breakdown.

#### Scenario: Private data does not leak into the context
- **WHEN** an owner holds a collective trust with a proxy and a manual breakdown
- **THEN** its fund context is `{"status": "UNKNOWN", "reason_code": "FUND_DATA_UNAVAILABLE"}` and the proxy fund's context contains none of the owner's amounts

#### Scenario: Import boundary
- **WHEN** the forecast-path modules are loaded
- **THEN** none of the private fund, investor or profile modules are imported

<!-- tests: tests/test_fund_lookthrough.py::test_fund_context_never_carries_anything_private_proxy_manual_or_holdings, tests/test_fund_lookthrough.py::test_the_shared_forecast_path_does_not_import_private_modules -->

### Requirement: SEC N-PORT retrieval sequence
The N-PORT source SHALL map a ticker to a series through SEC's mutual-fund ticker table, find the series' newest `NPORT-P` filing, and read that filing's primary document; the ticker table MUST be stored in `sec_fund_ids` and downloaded again only when older than `sec_fund_ids_refresh_days` (seed 7, range 1 to 365).

#### Scenario: Ticker to holdings
- **WHEN** a fund ticker is fetched
- **THEN** the source requests the ticker table, then the filing feed for the series, then the filing document, and sends the configured User-Agent

#### Scenario: Cached table and shared filing
- **WHEN** a second share class of the same fund is fetched
- **THEN** the ticker table and the filing are not requested again

#### Scenario: Table older than the interval
- **WHEN** the stored table is older than `sec_fund_ids_refresh_days`
- **THEN** it is downloaded again

#### Scenario: Empty download
- **WHEN** a replacement table has no rows
- **THEN** the stored table is kept

<!-- tests: tests/test_nport_provider.py::test_fetch_follows_ticker_to_series_to_filing_and_sends_the_user_agent, tests/test_nport_provider.py::test_ticker_table_is_cached_and_a_second_class_of_the_same_fund_reuses_the_filing, tests/test_nport_provider.py::test_ticker_table_redownloaded_only_after_the_refresh_interval, tests/test_nport_provider.py::test_blank_download_never_wipes_the_stored_table -->

### Requirement: N-PORT weights and identifiers
N-PORT holdings SHALL use weights as fractions of net assets (reported percent divided by 100), SHALL be identified by CUSIP and ISIN with no ticker, and SHALL be sorted largest first. Holdings coverage MUST be the sum of the long holdings' weights, capped at 1.

#### Scenario: Weights are fractions
- **WHEN** a filing reports a position at 7.0 percent of net assets
- **THEN** the holding's weight is 0.07 and its symbol is empty

<!-- tests: tests/test_nport_provider.py::test_fxaix_report_weights_are_fractions_and_derivatives_are_separate, tests/test_nport_provider.py::test_n_port_data_round_trips_through_the_fund_tables -->

### Requirement: Derivatives and short positions are not holdings
N-PORT derivative and short positions SHALL stay out of the holdings list, the coverage figure and the asset-class mix, and their net weight MUST be recorded as `derivative_short_weight`.

#### Scenario: Short and derivative positions
- **WHEN** a filing contains a derivative and a short position
- **THEN** neither appears among the holdings and their net weight is stored in the derivative/short weight

<!-- tests: tests/test_nport_provider.py::test_stock_positions_are_not_flagged_as_funds_and_short_positions_leave_the_holdings, tests/test_nport_provider.py::test_fxaix_report_weights_are_fractions_and_derivatives_are_separate -->

### Requirement: N-PORT flags holdings that are funds
An N-PORT holding SHALL be flagged `holding_is_fund` when its issuer category is a registered fund or, for equity-class positions, its name or title contains the word fund or ETF; stock positions MUST NOT be flagged.

#### Scenario: Target-date fund
- **WHEN** a target-date fund files its underlying funds as ordinary corporations whose names say fund
- **THEN** those holdings are flagged as funds

#### Scenario: Stock position
- **WHEN** a holding is an ordinary stock
- **THEN** it is not flagged

<!-- tests: tests/test_nport_provider.py::test_target_date_fund_flags_its_underlying_funds_even_though_filed_as_corp, tests/test_nport_provider.py::test_stock_positions_are_not_flagged_as_funds_and_short_positions_leave_the_holdings -->

### Requirement: N-PORT failures are reason codes
The N-PORT source SHALL return `NPORT_NOT_FILED` when the fund has no ticker entry or no filing, `NPORT_NOT_CONFIGURED` when no contact string is set (making no SEC request), `FUND_FETCH_FAILED` for any request, parse or series-mismatch failure, and `FUND_SECTORS_MISSING` on successful data because N-PORT has no sector weights. Documents declaring a DTD or entities MUST be refused.

#### Scenario: No filing
- **WHEN** a ticker has no filing
- **THEN** the result carries `NPORT_NOT_FILED`

#### Scenario: Contact string unset
- **WHEN** `EDGAR_USER_AGENT` is empty
- **THEN** the result carries `NPORT_NOT_CONFIGURED` and no request is made

#### Scenario: Wrong series or hostile XML
- **WHEN** the filing's series differs from the ticker's series, or the XML declares a DTD
- **THEN** the result carries `FUND_FETCH_FAILED`

<!-- tests: tests/test_nport_provider.py::test_fund_with_no_filing_gets_nport_not_filed, tests/test_nport_provider.py::test_no_user_agent_means_no_sec_call, tests/test_nport_provider.py::test_series_mismatch_is_refused, tests/test_nport_provider.py::test_dtd_and_garbage_are_refused, tests/test_nport_provider.py::test_failures_become_reason_codes_never_exceptions -->

### Requirement: SEC fair-access rules
SEC requests SHALL read the User-Agent only from the `EDGAR_USER_AGENT` environment variable and MUST NOT store, log or display it, and SHALL share one process-wide throttle of at most 10 requests a second. `nport_requests_per_second` (seed 8, range 1 to 10) MUST NOT be settable above 10.

#### Scenario: Throttle cap
- **WHEN** a rate of 100 a second is requested
- **THEN** 11 calls take at least 1.0 seconds

#### Scenario: Shared throttle
- **WHEN** two components ask for the same rate, or any rate above 10
- **THEN** they share one throttle, and rates above 10 share the 10-a-second throttle

#### Scenario: Setting out of range
- **WHEN** an admin sets the rate to 11 or 0, or the timeout below 5 seconds
- **THEN** the change is refused

<!-- tests: tests/test_nport_provider.py::test_throttle_spaces_requests_and_never_exceeds_ten_per_second, tests/test_nport_provider.py::test_threads_share_one_throttle_per_rate, tests/test_nport_provider.py::test_fund_source_settings_are_admin_editable_validated_and_capped_at_ten_per_second, tests/test_nport_provider.py::test_no_user_agent_means_no_sec_call -->

### Requirement: Issuer holdings sources are seeded disabled and admin-managed
Issuer holdings files SHALL be downloaded only for funds with an enabled row in `fund_issuer_sources`; seeded rows MUST be disabled. Changing a row SHALL be admin-only, versioned with `expected_version`, validated and audited as `upsert_fund_issuer_source`.

#### Scenario: Nothing downloaded until enabled
- **WHEN** a fund's source row is disabled or absent
- **THEN** the issuer source makes no request and returns `ISSUER_SOURCE_NOT_CONFIGURED`

#### Scenario: Admin edit
- **WHEN** an admin saves a valid row, then updates it with the current version, then again with the old version
- **THEN** the first two succeed with versions 1 and 2 and the third is refused

#### Scenario: Not an admin
- **WHEN** a non-admin edits a source
- **THEN** it is refused

<!-- tests: tests/test_fund_provider_chain.py::test_sources_are_seeded_disabled_and_nothing_is_downloaded_until_enabled, tests/test_fund_provider_chain.py::test_issuer_source_admin_verb_is_admin_only_validated_audited_and_versioned -->

### Requirement: Issuer URLs must be https on the issuer's own domain
An issuer file URL SHALL use https and a host that is the issuer's domain (`ishares.com` for iShares, `ssga.com` for SPDR) or a subdomain of it, and MUST be re-checked at fetch time.

#### Scenario: Foreign host refused on save
- **WHEN** an admin saves a URL on another domain or with a non-https scheme
- **THEN** the save is refused

#### Scenario: Row edited directly in the database
- **WHEN** a stored row's URL is on a foreign host
- **THEN** no request is made and the result carries `FUND_FETCH_FAILED`

<!-- tests: tests/test_fund_provider_chain.py::test_issuer_urls_must_be_https_on_the_issuers_own_domain, tests/test_fund_provider_chain.py::test_issuer_source_admin_verb_is_admin_only_validated_audited_and_versioned -->

### Requirement: Issuer files become holdings, sectors and asset classes
The issuer source SHALL read iShares CSV and SPDR spreadsheet layouts, turn security rows into holdings with the file's ticker and as-of date, add cash and money-market rows only to the asset-class mix, and add futures, swaps and negative weights only to the derivative/short weight. Sector weights MUST be each sector's share of the equity part, with sector names translated to the app's names. An unreadable file MUST be a reason code, not an exception.

#### Scenario: iShares file
- **WHEN** an iShares file with holdings, cash and a future is parsed
- **THEN** holdings and sectors come from the securities, cash goes to the asset-class mix and the future to the derivative/short weight

#### Scenario: Cash line in a SPDR file
- **WHEN** a cash line has no equity sector
- **THEN** it is counted as cash, not as a stock

#### Scenario: Unreadable file
- **WHEN** the file has no holdings table or cannot be read
- **THEN** only a file error is raised internally and the source returns `FUND_FETCH_FAILED`

<!-- tests: tests/test_fund_provider_chain.py::test_ishares_csv_parses_holdings_sectors_cash_and_derivatives, tests/test_fund_provider_chain.py::test_spdr_spreadsheet_parses_and_a_cash_line_is_not_a_stock, tests/test_fund_provider_chain.py::test_unreadable_files_raise_issuer_file_error_only, tests/test_fund_provider_chain.py::test_enabled_source_is_downloaded_and_parsed_and_failures_are_codes -->

### Requirement: Only public identifiers are sent to OpenFIGI
Identifier mapping SHALL send only well-formed public CUSIP or ISIN values and MUST NOT send anything about a portfolio, owner or amount. The optional API key SHALL come only from the `OPENFIGI_API_KEY` environment variable, be sent only as a header, and never appear in errors, logs or storage.

#### Scenario: Malformed identifiers dropped
- **WHEN** a value is blank, "N/A" or not 9 characters (CUSIP) or 12 characters (ISIN)
- **THEN** it is not looked up

#### Scenario: Key handling
- **WHEN** a request fails
- **THEN** the error message does not contain the key

<!-- tests: tests/test_identifier_mapping.py::test_normalize_id_accepts_only_well_shaped_identifiers, tests/test_identifier_mapping.py::test_api_key_comes_from_the_environment_and_is_sent_only_as_a_header, tests/test_identifier_mapping.py::test_errors_never_carry_the_key -->

### Requirement: Identifier mappings are cached including not-found
The system SHALL cache each identifier's result in `security_identifiers`, including "not found", and look it up again only after `mapping_refresh_days` (seed 30, range 1 to 3650). A transient failure MUST NOT be cached and SHALL stop that run's lookups.

#### Scenario: Cached results
- **WHEN** an identifier was mapped or found missing within the refresh interval
- **THEN** OpenFIGI is not asked again

#### Scenario: Refresh interval passes
- **WHEN** the interval has passed
- **THEN** the identifier is looked up again

#### Scenario: Transient failure
- **WHEN** OpenFIGI answers with a rate-limit, server error, timeout or a malformed answer
- **THEN** nothing from that batch is cached and no further batches are sent in that run

#### Scenario: Preference among listings
- **WHEN** an identifier has several listings
- **THEN** the US composite listing is used, else the first listing that has a ticker

<!-- tests: tests/test_identifier_mapping.py::test_mapped_and_not_found_are_both_cached_and_not_asked_again, tests/test_identifier_mapping.py::test_a_mapping_is_looked_up_again_only_after_the_refresh_interval, tests/test_identifier_mapping.py::test_transient_failures_are_not_cached_and_stop_the_run, tests/test_identifier_mapping.py::test_malformed_answers_raise_nothing_and_cache_nothing, tests/test_identifier_mapping.py::test_pick_listing_prefers_the_us_composite_and_needs_a_ticker -->

### Requirement: Identifier lookups are bounded
Identifier mapping SHALL send at most `openfigi_max_ids_per_refresh` new identifiers per fund refresh (seed 300), largest holdings first, in batches of `openfigi_batch_size` (seed 10, range 1 to 100) capped at 10 without a key and 100 with one, spaced 2.4 seconds apart without a key and 0.24 with one. With `openfigi_enabled` at 0 it SHALL use only stored mappings and send nothing.

#### Scenario: Per-run cap
- **WHEN** a fund has more new identifiers than the cap
- **THEN** the largest holdings are looked up first and the rest wait for a later refresh

#### Scenario: Batch size without a key
- **WHEN** the batch size setting is above 10 and no key is set
- **THEN** batches contain at most 10 identifiers

#### Scenario: Disabled
- **WHEN** `openfigi_enabled` is 0
- **THEN** only stored mappings are used and nothing is sent

<!-- tests: tests/test_identifier_mapping.py::test_per_refresh_cap_leaves_the_rest_for_a_later_run, tests/test_identifier_mapping.py::test_largest_holdings_are_looked_up_first_when_capped, tests/test_identifier_mapping.py::test_batch_size_follows_the_setting_but_no_key_caps_it_at_ten, tests/test_identifier_mapping.py::test_request_spacing_differs_with_and_without_a_key, tests/test_identifier_mapping.py::test_disabled_uses_only_stored_mappings_and_sends_nothing, tests/test_identifier_mapping.py::test_the_settings_are_admin_editable_validated_and_default_sensibly -->

### Requirement: Holdings are mapped to tickers and coverage is reported two ways
Fund refresh SHALL map holdings that have only a CUSIP or ISIN to tickers before saving, retrying a not-found CUSIP as the holding's ISIN, and MUST report `holdings_coverage` (weight the holdings explain) and `mapped_coverage` (the part with a ticker). Holdings that cannot be mapped SHALL stay keyed by identifier and add `FUND_HOLDINGS_UNMAPPED`.

#### Scenario: Partly mapped fund
- **WHEN** some holdings map and others do not
- **THEN** mapped coverage is lower than holdings coverage and `FUND_HOLDINGS_UNMAPPED` is recorded

#### Scenario: CUSIP not found
- **WHEN** a CUSIP is stored as not found and the holding has an ISIN
- **THEN** the ISIN is looked up

#### Scenario: Mapping failure
- **WHEN** the mapping step raises during a refresh
- **THEN** the refresh still saves the fund with unmapped holdings

<!-- tests: tests/test_identifier_mapping.py::test_holdings_get_tickers_and_coverage_reports_explained_vs_mapped, tests/test_identifier_mapping.py::test_a_cusip_not_found_is_retried_as_isin_and_ticker_holdings_are_untouched, tests/test_identifier_mapping.py::test_a_mapping_failure_never_breaks_the_refresh, tests/test_identifier_mapping.py::test_a_fully_mapped_fund_has_equal_explained_and_mapped_coverage, tests/test_identifier_mapping.py::test_provider_data_that_already_has_tickers_needs_no_lookup -->

### Requirement: A fund's knowledge comes from its own data, then manual, then proxy
For each fund an owner holds, the system SHALL use, in priority order: the fund's own provider data, the owner's manual breakdown, the data of the instrument's proxy ticker labeled as via that proxy, and otherwise source `none`, which is UNKNOWN downstream and never guessed.

#### Scenario: Manual beats proxy
- **WHEN** a collective trust has a proxy fund with data and the owner records a manual breakdown
- **THEN** the view's source is manual with no proxy label, and clearing the breakdown returns to the proxy view

#### Scenario: Neither
- **WHEN** there is no own data, manual breakdown or proxy data
- **THEN** the view's source is `none`

#### Scenario: Own data wins
- **WHEN** a fund has its own data and a manual breakdown
- **THEN** the own data is used

<!-- tests: tests/test_fund_lookthrough.py::test_manual_breakdown_beats_proxy_and_proxy_beats_unknown_and_is_labeled_via, tests/test_fund_lookthrough.py::test_a_funds_own_data_wins_over_a_manual_breakdown -->

### Requirement: Manual breakdowns are private, validated and audited
A manual fund breakdown SHALL be readable and writable only by the owner for a fund that owner holds, with one active row per owner and fund and older rows kept as history. Asset-class keys MUST be `stock`, `bond`, `cash` or `other`, each value between 0 and 100, and each map MUST sum to 100 within 1; each change SHALL write an audit event.

#### Scenario: Other owner
- **WHEN** another owner asks for the views of the same collective trust
- **THEN** its source is `none`, and that owner's attempt to write a breakdown for it is refused as not their fund

#### Scenario: Invalid breakdowns
- **WHEN** the percents do not sum to 100, a value is out of range, a key is unknown, or the map is empty
- **THEN** the write is refused and the existing breakdown is unchanged

#### Scenario: Valid write
- **WHEN** an owner saves a valid breakdown
- **THEN** an audit event `set_fund_manual_breakdown` is written

<!-- tests: tests/test_fund_lookthrough.py::test_manual_breakdown_is_owner_scoped_validated_and_audited -->

### Requirement: Look-through is pure and deterministic
The look-through engine SHALL compute from positions and fund views alone, with no database, network or language model, so the same inputs always give the same output.

#### Scenario: Repeated analysis
- **WHEN** the same positions and views are analysed twice
- **THEN** the results are identical

<!-- tests: tests/test_fund_lookthrough.py::test_named_holdings_become_issuer_exposure_and_the_rest_stays_in_a_fund_other_bucket, tests/test_fund_lookthrough.py::test_concentration_helpers_are_normalized_by_what_is_explained -->

### Requirement: Fund holdings become issuer exposure with the rest in a bucket
The engine SHALL turn each named fund holding into issuer exposure of fund value times weight, recording the fund it came via, and MUST keep what the named holdings do not explain in a `<FUND>_other` bucket. A fund with no data SHALL be one `<FUND>_unknown` bucket with reason code `LOOKTHROUGH_NO_FUND_DATA`. Buckets MUST NOT count as issuers.

#### Scenario: Named holdings and remainder
- **WHEN** a fund worth 1000 holds two named issuers at 6 percent and 5 percent
- **THEN** their exposures are 60 and 50, the remaining 890 sits in the fund's other bucket and coverage is 11 percent

#### Scenario: Fund without data
- **WHEN** a fund has no holdings, manual breakdown or proxy
- **THEN** its value is one unknown bucket and explains nothing

#### Scenario: Same issuer reached several ways
- **WHEN** an issuer is held directly and through two funds
- **THEN** its exposures are summed and the overlap report lists each source with its value

<!-- tests: tests/test_fund_lookthrough.py::test_named_holdings_become_issuer_exposure_and_the_rest_stays_in_a_fund_other_bucket, tests/test_fund_lookthrough.py::test_a_fund_with_no_data_is_one_unknown_bucket_and_never_a_guess, tests/test_fund_lookthrough.py::test_overlap_report_lists_every_way_an_issuer_is_reached -->

### Requirement: Funds inside funds are opened with guards
The engine SHALL open a holding that is itself a fund up to `max_depth` levels, never opening a fund twice on one path, and MUST report `LOOKTHROUGH_CYCLE`, `LOOKTHROUGH_DEPTH_LIMIT` or `LOOKTHROUGH_MISSING_LEVEL` instead of guessing, leaving the unopened value in a bucket.

#### Scenario: Cycle
- **WHEN** an index fund inside a target-date fund holds that target-date fund
- **THEN** the cyclic part stays in the index fund's other bucket and `LOOKTHROUGH_CYCLE` is reported

#### Scenario: Depth limit and missing level
- **WHEN** depth is 1, or an inner fund has no stored data
- **THEN** `LOOKTHROUGH_DEPTH_LIMIT` or `LOOKTHROUGH_MISSING_LEVEL` is reported and the inner issuers are not invented

<!-- tests: tests/test_fund_lookthrough.py::test_target_date_fund_opens_its_index_funds_and_a_cycle_cannot_loop, tests/test_fund_lookthrough.py::test_depth_limit_and_missing_level_are_reason_codes_not_guesses -->

### Requirement: Look-through is computed per scope and excludes paper accounts
The engine SHALL produce one result for the household, one per account (`account:<id>`) and one per tax treatment (`tax:<TREATMENT>`), and MUST exclude paper accounts from every scope.

#### Scenario: Scopes
- **WHEN** positions sit in two accounts with different tax treatments and a third paper account
- **THEN** the household total excludes the paper account and each account and treatment shows only its own value

<!-- tests: tests/test_fund_lookthrough.py::test_scopes_account_tax_treatment_and_paper_exclusion -->

### Requirement: Sector, asset-class and international mix come from the fund
The engine SHALL derive a scope's sector, asset-class and international-share figures from each fund's own mix (or its sub-funds), reporting the unknown remainder separately and never spreading it. Missing mixes MUST give `LOOKTHROUGH_MIX_UNKNOWN`.

#### Scenario: Fund with a mix
- **WHEN** an international fund has a stock and cash mix and sector weights
- **THEN** asset-class and sector percentages follow from those mixes and the international share is that of the known equity part

<!-- tests: tests/test_fund_lookthrough.py::test_sector_asset_class_and_international_mix_come_from_the_fund_not_a_guess -->

### Requirement: Leverage scales exposure
The engine SHALL multiply issuer exposure by a position's leverage factor while keeping market value unscaled, and SHALL report the value in leveraged or inverse positions.

#### Scenario: Leveraged and inverse funds
- **WHEN** a 3x fund and a -3x fund each hold the same issuer
- **THEN** the issuer's market value is the sum of the values and its exposure applies the factors, and 20 percent of the household is counted as leveraged or inverse in a 1000 portfolio holding two 100 positions

<!-- tests: tests/test_fund_lookthrough.py::test_leveraged_and_inverse_funds_are_counted_and_scale_issuer_exposure -->

### Requirement: Concentration is normalized by what is explained
The engine SHALL report top 1, 5 and 10 percentages, HHI and effective N of named issuers over the explained value, so a fund known only in part is not made to look more concentrated.

#### Scenario: Three exposures
- **WHEN** exposures are 50, 30 and 20
- **THEN** the top-1 share is 50, the top-5 share 100, HHI 0.38 and effective N 2.63

#### Scenario: A slice of a larger base
- **WHEN** exposures sum to 10 and the explained value is 10
- **THEN** percentages are relative to 10

#### Scenario: Nothing explained
- **WHEN** there are no exposures
- **THEN** the figures are empty

<!-- tests: tests/test_fund_lookthrough.py::test_concentration_helpers_are_normalized_by_what_is_explained -->

### Requirement: Unmapped holdings are keyed by identifier and shared across funds
The engine SHALL key a holding with no ticker by its CUSIP or ISIN, so the same security reached through two funds is one exposure, and MUST report `LOOKTHROUGH_UNMAPPED_HOLDINGS` and a mapped-coverage figure no greater than the fund coverage.

#### Scenario: Same CUSIP in two funds
- **WHEN** two funds hold the same CUSIP with no ticker
- **THEN** one exposure keyed by that CUSIP is reported and the unmapped reason code is present

<!-- tests: tests/test_identifier_mapping.py::test_unmapped_holdings_are_exposures_keyed_by_cusip_and_shared_across_funds -->

### Requirement: Fund-data settings are admin-only, versioned and bounded
The fund-data settings in `portfolio_settings` SHALL be changeable only by an administrator, with an audit event and validation of range, and resettable to defaults.

#### Scenario: Defaults
- **WHEN** the settings are at their seed
- **THEN** `etf_data_ttl_hours` is 24, `mutual_fund_data_ttl_hours` 168, `lookthrough_min_coverage_pct` 80 and `lookthrough_max_depth` 3
- **AND** `sec_fund_ids_refresh_days` is 7, `nport_requests_per_second` 8 and `nport_http_timeout_seconds` 30
- **AND** `openfigi_enabled` is 1, `openfigi_batch_size` 10, `mapping_refresh_days` 30 and `openfigi_max_ids_per_refresh` 300
- **AND** `issuer_csv_max_age_days` is 3, `nport_max_age_days` 150, `yfinance_max_age_days` 7 and `fund_holdings_complete_pct` 90

#### Scenario: Non-admin change
- **WHEN** a non-admin changes a fund-data setting
- **THEN** it is refused

#### Scenario: Out of range
- **WHEN** an admin sets `openfigi_batch_size` to 0 or 101, `openfigi_enabled` to 2, or `mapping_refresh_days` to 0
- **THEN** the change is refused

#### Scenario: Reset
- **WHEN** an admin resets the settings
- **THEN** every fund-data setting returns to its default

<!-- tests: tests/test_fund_lookthrough.py::test_horizons_and_portfolio_settings_are_admin_only_audited_and_validated, tests/test_nport_provider.py::test_fund_source_settings_are_admin_editable_validated_and_capped_at_ten_per_second, tests/test_identifier_mapping.py::test_the_settings_are_admin_editable_validated_and_default_sensibly, tests/test_fund_lookthrough.py::test_new_portfolio_settings_columns_migrate_onto_the_step1_row_untouched -->
