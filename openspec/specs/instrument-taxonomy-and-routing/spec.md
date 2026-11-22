# instrument-taxonomy-and-routing Specification

## Purpose
Defines what kind of security each ticker is (type, asset class, leverage), how that is decided and overridden, and how the type routes a ticker through the pipeline: filings, fundamentals, valuation and forecast horizons. The vocabularies are the contract shared by holdings, health, look-through and validation.

## Requirements

### Requirement: Instrument type vocabulary
The system SHALL classify every instrument with exactly one `instrument_type` from: `STOCK`, `ETF`, `ETN`, `MUTUAL_FUND`, `MONEY_MARKET`, `TARGET_DATE_FUND`, `BOND`, `CIT`, `STABLE_VALUE`, `CASH`, `OTHER`, `UNKNOWN`. The README omits `STABLE_VALUE`; the code defines it.

#### Scenario: Unknown type is refused on manual override
- **WHEN** an owner sets an instrument's type to a value outside the vocabulary (for example `STONK`)
- **THEN** the change is refused with a validation error
- **AND** the instrument keeps its previous classification and its `auto` source

<!-- tests: tests/test_instrument_taxonomy.py::test_override_validation -->

### Requirement: Asset class vocabulary
The system SHALL classify every instrument with one `asset_class` from: `US_EQUITY`, `INTL_EQUITY`, `FIXED_INCOME`, `REAL_ESTATE`, `COMMODITY`, `CASH`, `MULTI_ASSET`, `UNKNOWN`. Target allocations MAY use only the classes other than `MULTI_ASSET` and `UNKNOWN`, which are looked through.

#### Scenario: Unknown asset class is refused on manual override
- **WHEN** an owner sets an instrument's asset class to a value outside the vocabulary (for example `MOON`)
- **THEN** the change is refused with a validation error

<!-- tests: tests/test_instrument_taxonomy.py::test_override_validation -->

### Requirement: Leverage factor
Each instrument SHALL carry a non-zero `leverage_factor` within ±5, where 1.0 is normal, 2.0 and 3.0 are leveraged, and negative values are inverse. A manual override outside that range, or equal to 0, MUST be refused.

#### Scenario: Out-of-range leverage refused
- **WHEN** an owner overrides leverage to 0 or to 9
- **THEN** the change is refused and nothing is stored

#### Scenario: Valid leverage stored
- **WHEN** an owner overrides leverage to 2
- **THEN** the instrument's `leverage_factor` is 2.0

<!-- tests: tests/test_instrument_taxonomy.py::test_override_validation -->

### Requirement: Automatic classification source order
The system SHALL classify a security from Yahoo Finance's `quoteType` first, with name and category keywords refining the type. `EQUITY` is `STOCK`, `ETF` is `ETF`, `MUTUALFUND` is `MUTUAL_FUND` and `MONEYMARKET` is `MONEY_MARKET`. A money-market keyword in the name or category MUST win over `MUTUALFUND`. A target-date keyword turns an ETF or mutual fund into `TARGET_DATE_FUND`. An ETN keyword turns an ETF into `ETN`.

#### Scenario: Money-market keyword beats mutual-fund quote type
- **WHEN** a security with quote type `MUTUALFUND` is named "Government Cash Reserves Money Market Fund"
- **THEN** its type is `MONEY_MARKET`

#### Scenario: Target-date fund by name
- **WHEN** a `MUTUALFUND` is named "Target Retirement 2050 Fund"
- **THEN** its type is `TARGET_DATE_FUND` and its asset class is `MULTI_ASSET`

#### Scenario: Non-US stock
- **WHEN** a `EQUITY` security reports a country other than the United States
- **THEN** its asset class is `INTL_EQUITY`; a US or unreported country gives `US_EQUITY`

<!-- tests: tests/test_instrument_taxonomy.py::test_classifier_uses_quote_type_then_name_and_category -->

### Requirement: Sub-asset class and fund details
Automatic classification SHALL set a `sub_asset_class` from the fund's category or name using first-match rules, with these values: `MONEY_MARKET`, `HIGH_YIELD`, `TREASURY`, `MUNICIPAL`, `CORPORATE`, `EMERGING_MARKETS`, `LARGE_CAP_GROWTH`, `LARGE_CAP_VALUE`, `LARGE_CAP_BLEND`, `MID_CAP`, `SMALL_CAP`, `SECTOR`, `TARGET_DATE`, `STABLE_VALUE`. For funds it SHALL also record `expense_ratio` as a fraction, `fund_family` and `category`.

#### Scenario: Large-blend ETF
- **WHEN** an ETF with category "Large Blend" and an annual expense ratio of 0.000945 is classified
- **THEN** it is `ETF`, `US_EQUITY`, `LARGE_CAP_BLEND`, with expense ratio 0.000945 and its fund family recorded

#### Scenario: Net expense ratio reported in percent
- **WHEN** the source gives a net expense ratio of 0.2 (percent)
- **THEN** the stored expense ratio is 0.002

<!-- tests: tests/test_instrument_taxonomy.py::test_classifier_uses_quote_type_then_name_and_category -->

### Requirement: Leveraged and inverse detection
The system SHALL derive `leverage_factor` from a fund's name: "3x", "UltraPro" or "triple" give 3; "2x", "double" or "Ultra" give 2; "Inverse", "Bear", "UltraShort", "-1x/-2x/-3x" or a bare "Short" make it negative. "Short-Term", "Ultra-Short" and "Short Duration" funds MUST NOT be flagged inverse. Only ETF, ETN and mutual-fund types are read this way; a stock's name never makes it leveraged.

#### Scenario: Leveraged and inverse names
- **WHEN** ETFs named "UltraPro QQQ", "UltraShort S&P500" and "Short QQQ" are classified
- **THEN** their leverage factors are 3.0, -2.0 and -1.0

#### Scenario: Short-maturity bond funds are ordinary
- **WHEN** ETFs named "Short-Term Treasury ETF" and "Ultra-Short Bond ETF" are classified
- **THEN** both have leverage factor 1.0

#### Scenario: Stock with a leveraged-sounding name
- **WHEN** an `EQUITY` security's name contains "2x Ultra Bear"
- **THEN** its leverage factor is 1.0

<!-- tests: tests/test_instrument_taxonomy.py::test_leveraged_and_inverse_detection_does_not_flag_ordinary_short_bond_funds, tests/test_instrument_taxonomy.py::test_stock_names_are_never_read_as_leveraged -->

### Requirement: Failed lookups never raise or erase
A failed or empty provider lookup SHALL classify as `UNKNOWN` with no classification time recorded, so the next run retries. It MUST NOT raise into an import, an add-position call or the pipeline, and it MUST NOT overwrite an existing good classification.

#### Scenario: Provider down on first look
- **WHEN** the provider returns nothing for a new holding
- **THEN** the instrument stays `UNKNOWN` with no `classified_at`, and the next run looks it up again

#### Scenario: Provider down on re-check
- **WHEN** a previously classified instrument is re-checked and the provider fails
- **THEN** its stored type is unchanged

<!-- tests: tests/test_instrument_taxonomy.py::test_failed_lookup_is_unknown_and_never_raises, tests/test_instrument_taxonomy.py::test_fetch_info_swallows_provider_errors_and_empty_answers, tests/test_instrument_taxonomy.py::test_failed_lookup_stores_nothing_wipes_nothing_and_retries_next_time -->

### Requirement: Re-check cadence and timing
An automatic classification SHALL be re-checked once it is older than 30 days. Lookups MUST happen outside any database transaction, after an import or add-position commits. A change in classification MUST be audited and an unchanged result MUST NOT add an audit event.

#### Scenario: Fresh classification not re-fetched
- **WHEN** classification runs again for an instrument classified less than 30 days ago
- **THEN** no lookup is made

#### Scenario: Stale classification re-fetched
- **WHEN** an instrument was classified 31 days ago
- **THEN** only that instrument is looked up, and an identical result adds no audit event

<!-- tests: tests/test_instrument_taxonomy.py::test_classify_new_holdings_stores_audits_and_rechecks_only_after_30_days, tests/test_instrument_taxonomy.py::test_add_position_classifies_after_commit_but_not_inside_a_caller_transaction, tests/test_instrument_taxonomy.py::test_import_commit_classifies_new_securities -->

### Requirement: Manual override is permanent
A classification an owner sets (`classification_source = manual`) SHALL never be overwritten or looked up by the automatic classifier. The override MAY set type, asset class, sub-asset class (stored upper-cased with underscores), leverage and a proxy ticker, and is versioned (`expected_version`) and audited. A proxy ticker MUST be a real market symbol, not another ticker-less fund. Because the instrument row is shared, an override applies in every account holding that security.

#### Scenario: Override survives forced reclassification
- **WHEN** an owner overrides a ticker to `ETF` and a forced automatic re-classification later runs
- **THEN** that instrument is not looked up and still reads `ETF` with source `manual`

#### Scenario: Stale version refused
- **WHEN** an override is submitted with an `expected_version` that is not current
- **THEN** it is refused with a version conflict

#### Scenario: CIT as proxy refused
- **WHEN** a proxy ticker is another ticker-less (`CIT:`) key
- **THEN** the change is refused

<!-- tests: tests/test_instrument_taxonomy.py::test_manual_override_sticks_against_automatic_reclassification, tests/test_instrument_taxonomy.py::test_override_validation -->

### Requirement: Ticker-less funds
A fund with no ticker SHALL be keyed `CIT:<slug>` derived stably and case-insensitively from plan and fund names, typed `CIT`, classified manually, and MUST NOT be sent to a price provider, the classifier or the prediction pipeline. An optional proxy ticker is for analytics only and MUST NOT be shown as the holding.

#### Scenario: Stable key
- **WHEN** the same plan and fund names are given with different case and spacing
- **THEN** the same `CIT:` key results; an empty fund name is refused

#### Scenario: Implicit CIT creation
- **WHEN** a position is added directly under a `CIT:` key
- **THEN** its instrument is typed `CIT` and is never auto-classified

#### Scenario: Holding is the CIT, not the proxy
- **WHEN** a CIT has proxy ticker VOO
- **THEN** the holding is listed under the CIT key and the proxy appears only as an analytics attribute

<!-- tests: tests/test_instrument_taxonomy.py::test_synthetic_ticker_helpers, tests/test_instrument_taxonomy.py::test_cit_instrument_shape_and_proxy_rules, tests/test_instrument_taxonomy.py::test_cit_is_excluded_from_pipeline_universe_and_provider_calls, tests/test_instrument_taxonomy.py::test_classifier_never_looks_up_a_cit -->

### Requirement: Fund-name ticker suggestions
For ticker-less statement rows that look like public mutual funds, the system SHALL propose up to 3 ticker candidates whose normalized-name overlap is at least 0.6 and whose quote type is mutual fund, ETF or fund. It MUST NOT apply a suggestion unless the person picks it, and MUST give none for collective trusts, stable value funds, separate accounts or company stock.

#### Scenario: Collective trust gets no suggestion
- **WHEN** a ticker-less row is named like a collective investment trust
- **THEN** no candidates are produced and no search is made

#### Scenario: Close match offered
- **WHEN** a retail fund row matches a candidate at score 0.6 or more
- **THEN** the candidate is listed for the person to choose; nothing is written

<!-- tests: tests/test_statement_import.py::test_only_funds_that_look_public_get_a_lookup_and_only_close_fund_matches_are_suggested, tests/test_statement_import.py::test_a_suggestion_changes_nothing_until_it_is_confirmed -->

### Requirement: Types that are never forecast
The prediction pipeline SHALL skip `CASH`, `MONEY_MARKET`, `CIT` and `STABLE_VALUE` instruments, logging a `[skip] <ticker> — <type> — nothing to forecast` reason and storing no prediction. A ticker whose type is still `UNKNOWN` (or unset) and that has no price MUST be skipped with "type unknown and no price available".

#### Scenario: Cash-like holdings skipped
- **WHEN** a run includes a money-market fund, a cash line and a CIT
- **THEN** none is sent to the model and each is logged as skipped

#### Scenario: Unpriceable unknown skipped
- **WHEN** a ticker of unknown type has no price
- **THEN** it is skipped with "type unknown and no price available"

#### Scenario: Unclassified but priced stays on the stock path
- **WHEN** a ticker of unknown type has a price
- **THEN** it is forecast on the unchanged stock path and stored with type `UNKNOWN`

<!-- tests: tests/test_etf_routing.py::test_apex_routes_funds_stores_no_placeholder_and_skips_cash_ciT_and_unpriceable, tests/test_fund_lookthrough.py::test_stable_value_is_classified_cash_like_and_never_predicted -->

### Requirement: Funds forecast without company fundamentals
`ETF`, `ETN`, `MUTUAL_FUND` and `TARGET_DATE_FUND` SHALL be forecast from Research, Macro and News only. Fundamentals and Valuation weights MUST be exactly 0 on every horizon, the remaining weights renormalized to sum to 1.0 subject to the floor, and the stored fundamental and valuation scores MUST be empty, never a 5/10 placeholder. A fund's missing fundamentals MUST NOT count as a data gap. Stocks are unaffected.

#### Scenario: ETF prediction row
- **WHEN** an ETF is forecast
- **THEN** its stored fundamental and valuation scores are empty, its weights show fundamentals 0.0 and valuation 0.0 summing to 1.0, and no fundamentals or valuation call is made

#### Scenario: Stock unchanged
- **WHEN** a stock is forecast
- **THEN** its fundamental and valuation scores are stored as before

<!-- tests: tests/test_etf_routing.py::test_fund_weights_zero_fundamentals_and_valuation_and_sum_to_one_on_every_horizon, tests/test_etf_routing.py::test_fund_weights_respect_the_floor_for_the_remaining_sources, tests/test_etf_routing.py::test_etf_context_runs_no_fundamentals_or_valuation_call_and_adds_no_placeholder, tests/test_etf_routing.py::test_fund_with_missing_research_and_news_reports_only_those_gaps, tests/test_etf_routing.py::test_stock_path_is_numerically_unchanged_whatever_the_non_fund_type -->

### Requirement: Leverage and type recorded on predictions
Each stored prediction SHALL record the `instrument_type` and `leverage_factor` it was made under, and leveraged or inverse funds MUST be flagged in the forecast prompt and context. Rows stored before this existed remain empty until a stamping backfill is run.

#### Scenario: Leveraged ETF stored
- **WHEN** a 3x ETF and a -3x ETF are forecast
- **THEN** their rows store leverage 3.0 and -3.0 and a normal ETF stores 1.0

<!-- tests: tests/test_etf_routing.py::test_insert_prediction_stores_type_and_leverage_and_defaults_to_null, tests/test_etf_routing.py::test_leveraged_and_inverse_funds_are_flagged_in_the_prompt_and_context, tests/test_etf_routing.py::test_prediction_columns_migrate_idempotently_and_old_rows_stay_null_until_backfilled -->

### Requirement: No filings routing for non-company types
EDGAR checks, fundamentals prefetch, 8-K guidance extraction and DCF valuation SHALL skip `ETF`, `ETN`, `MUTUAL_FUND`, `TARGET_DATE_FUND`, `CASH`, `MONEY_MARKET`, `CIT` and `STABLE_VALUE` cleanly, without error noise. `STOCK`, `BOND`, `OTHER` and unclassified tickers keep the filer path.

#### Scenario: Mixed ticker list
- **WHEN** a list of a stock, ETF, mutual fund, money market, CIT and an unclassified ticker is split
- **THEN** the filers are the stock and the unclassified ticker, and the others are returned with their types

#### Scenario: Fundamentals phase with funds
- **WHEN** the fundamentals phase runs over a stock and two funds
- **THEN** EDGAR check, prefetch and guidance are called only for the stock and no "no EDGAR data" warning names a fund

<!-- tests: tests/test_etf_routing.py::test_split_company_filers_separates_funds_cash_and_ciT_from_filers, tests/test_etf_routing.py::test_fundamentals_phase_never_reaches_edgar_or_guidance_for_fund_tickers, tests/test_etf_routing.py::test_valuation_phase_skips_fund_tickers_before_any_edgar_call -->

### Requirement: Unheld tickers are classified before analysis
At the start of a phase, any ticker about to be analysed (watchlist, trending, extras) without an instrument row SHALL get one and be classified. A manual classification is never looked up, and a failed lookup is remembered within the process so it is not retried on every call.

#### Scenario: Watchlist ETF
- **WHEN** an unheld watchlist ticker is analysed for the first time
- **THEN** it gets an instrument row typed by the classifier with source `auto`

#### Scenario: Repeat call
- **WHEN** classification is requested again for already classified and failed tickers
- **THEN** no further lookups are made

<!-- tests: tests/test_etf_routing.py::test_pipeline_gives_unheld_watchlist_tickers_an_instrument_row_and_type, tests/test_etf_routing.py::test_ensure_classified_creates_rows_leaves_manual_alone_and_remembers_failures -->

### Requirement: Horizons by instrument type
The system SHALL keep a per-type list of forecast horizons in `engine_settings.instrument_type_horizons_json`. For each ticker the daily phase MUST use the intersection of the day's scheduled horizons and the type's enabled horizons, and skip the ticker with a logged reason when nothing remains. An unset type uses the `UNKNOWN` entry.

#### Scenario: Seeded horizons
- **WHEN** the setting is at its seed
- **THEN** `STOCK`, `ETF`, `ETN`, `BOND`, `OTHER` and `UNKNOWN` have horizons 5, 21, 63, 250
- **AND** `MUTUAL_FUND` and `TARGET_DATE_FUND` have 63, 250
- **AND** `CIT`, `STABLE_VALUE`, `CASH` and `MONEY_MARKET` have none

#### Scenario: Mutual fund on a weekly-only day
- **WHEN** only horizons 5 and 21 are scheduled today and the ticker is a mutual fund
- **THEN** the ticker is skipped with "no scheduled horizon is enabled for mutual fund (scheduled [5, 21], enabled [63, 250])"

#### Scenario: Unclassified ticker
- **WHEN** a ticker has no type
- **THEN** it gets the `UNKNOWN` horizons 5, 21, 63, 250

<!-- tests: tests/test_fund_lookthrough.py::test_horizons_per_instrument_type_are_seeded_and_gate_the_pipeline, tests/test_fund_lookthrough.py::test_a_type_with_no_enabled_scheduled_horizon_is_skipped_and_the_setting_is_editable, tests/test_etf_routing.py::test_apex_routes_funds_stores_no_placeholder_and_skips_cash_ciT_and_unpriceable -->

### Requirement: Editing horizons by type
Changing the per-type horizons SHALL be admin-only, versioned with `expected_version`, audited as `update_instrument_type_horizons`, and resettable to the seed. Unknown instrument types, unsupported horizons (anything other than 5, 21, 63, 250) and a non-list value MUST be refused. An empty list means never predicted.

#### Scenario: Unsupported horizon refused
- **WHEN** an admin sets ETF horizons to [7]
- **THEN** the change is refused and the stored table is unchanged

#### Scenario: Stale version refused
- **WHEN** an update is submitted with an out-of-date `expected_version`
- **THEN** it is refused and exactly one audit event exists for the earlier successful change

#### Scenario: Reset to defaults
- **WHEN** an admin resets horizons after changing ETF to [21]
- **THEN** ETF reads 5, 21, 63, 250 again

#### Scenario: Non-admin refused
- **WHEN** a non-admin calls the update
- **THEN** it is refused

<!-- tests: tests/test_fund_lookthrough.py::test_horizons_and_portfolio_settings_are_admin_only_audited_and_validated, tests/test_fund_lookthrough.py::test_a_type_with_no_enabled_scheduled_horizon_is_skipped_and_the_setting_is_editable, tests/test_gap_fixes.py::test_horizons_change_and_audit_land_together_and_a_stale_version_leaves_neither -->

### Requirement: Shared instrument reference is market-only
Code on the shared forecast path SHALL read instrument type, asset class and leverage only through the instrument-reference module, which exposes the instruments table and shared fund tables and nothing about positions, accounts, cash, manual breakdowns or proxies. A fund's context SHALL be a compact summary of shared data and, with no stored data, MUST be `{"status": "UNKNOWN", "reason_code": "FUND_DATA_UNAVAILABLE"}`, including for a CIT.

#### Scenario: Fund with stored data
- **WHEN** a fund has stored shared data
- **THEN** its context lists the top 10 holdings, up to 6 sectors, the asset-class mix, the expense ratio and the concentration of the known holdings
- **AND** it repeats identically on a second call

#### Scenario: Fund with no data
- **WHEN** a ticker, or a CIT with a proxy or manual breakdown, has no shared fund data
- **THEN** its context is UNKNOWN with reason code `FUND_DATA_UNAVAILABLE`

<!-- tests: tests/test_etf_routing.py::test_default_loader_reads_the_instrument_row_from_the_instruments_table_only, tests/test_fund_lookthrough.py::test_fund_context_summarises_shared_fund_data_deterministically, tests/test_fund_lookthrough.py::test_fund_context_never_carries_anything_private_proxy_manual_or_holdings, tests/test_portfolio_health.py::test_no_model_decides_anything_and_the_shared_forecast_path_does_not_reach_health -->
