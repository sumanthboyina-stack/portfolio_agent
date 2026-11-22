# web-dashboard Specification

## Purpose
Defines the signed-in Streamlit screens that present APEX output to a person: the Today dashboard (attention queue, "What Fired" feed, command strip), Predictions, the Portfolio page (Overview, Holdings, Watchlist, performance), the Profile tiles, and the admin-only Schedule and Database pages. Sign-in, validation, restricted-list, opportunity, notification and ledger screens are specified in their own capabilities. Baseline at HEAD `ca450dd`.

## Requirements

### Requirement: Navigation structure and admin-only menu entries
The top navigation SHALL offer Today, Opportunity Engine, Predictions and Portfolio as primary tabs and Validation and Chat as secondary tabs. A settings menu SHALL list Profile, Schedule and Validation QA for everyone, and Database and Restricted List only to admins.

#### Scenario: Member opens the settings menu
- **WHEN** a non-admin opens the settings menu
- **THEN** Database and Restricted List are not listed

#### Scenario: Admin opens the settings menu
- **WHEN** an admin opens the settings menu
- **THEN** Database and Restricted List are listed and marked as admin-only

#### Scenario: Schedule and Validation QA shown to a member
- **WHEN** a non-admin opens the Schedule page or the Validation QA page from the menu
- **THEN** the page shows "Admin only." and stops, although the menu entry was visible

<!-- tests: NONE -->

### Requirement: Today dashboard is scoped to the signed-in owner's holdings
The Today dashboard SHALL resolve the viewer's portfolio from the verified actor and build its holdings panels, weights and risk and technical flags from that portfolio only. An owner with no portfolio MUST get an empty dashboard, never another owner's holdings.

#### Scenario: Owner with no portfolio
- **WHEN** a signed-in owner has no portfolio yet
- **THEN** the dashboard shows no holdings, no holding weights and no holding flags

#### Scenario: Flags limited to held tickers
- **WHEN** the owner has active risk flags on tickers they do not hold
- **THEN** those flags are not used by the queue or the feed

<!-- tests: NONE -->

### Requirement: Command strip states what its numbers cover
The command strip SHALL show imported securities value with the count of valued positions out of all positions, the "cash excluded" note and the oldest price date, plus unrealized P&L against recorded cost. An unpriced position MUST stay unknown and never count as zero.

#### Scenario: Partly priced portfolio
- **WHEN** 8 of 10 positions have a current value
- **THEN** the securities-value tile shows "8 of 10 positions valued" and "cash excluded"

#### Scenario: No cost basis
- **WHEN** no position has a recorded cost
- **THEN** the unrealized P&L figure is not computed

<!-- tests: NONE -->

### Requirement: Critical tier of the attention queue
The attention queue SHALL place in its Critical tier: an unprocessed severity-3 event on a held ticker, a SELL or STRONG_SELL recommendation on a held ticker, a single holding above 20% of valued securities, and an active risk flag on a held ticker. Each item MUST name the page that shows more.

#### Scenario: Unprocessed severity-3 event on a holding
- **WHEN** a severity-3 event in the last 48 hours on a held ticker has not been processed by the pipeline
- **THEN** the queue has one Critical item for that ticker that links to Chat

#### Scenario: Concentration above 20%
- **WHEN** one ticker is more than 20% of valued securities
- **THEN** a Critical item states its weight and links to Portfolio

#### Scenario: Sell recommendation
- **WHEN** the latest forecast for a held ticker is SELL or STRONG_SELL
- **THEN** a Critical item shows the recommendation and confidence and links to Predictions

<!-- tests: NONE -->

### Requirement: Watch tier of the attention queue
The Watch tier SHALL contain: bearish technical momentum on a held ticker, a recommendation change on a held ticker in the last 3 days, a 5-day accuracy below 45% over at least 30 evaluated predictions, holdings prices not synced in 10 days, a holding between 15% and 20% of valued securities, and a processed severity-3 event on a held ticker.

#### Scenario: Low 5-day accuracy
- **WHEN** at least 30 5-day predictions are evaluated and directional accuracy is below 45%
- **THEN** a Watch item says to treat 5-day signals as research prompts only, not trade signals, and does not require a decision

#### Scenario: Stale prices
- **WHEN** the oldest price observation, or the newest import when no price date exists, is more than 10 days old
- **THEN** a Watch item says the holdings prices were last synced on that date

#### Scenario: Approaching the limit
- **WHEN** a holding is 17% of valued securities
- **THEN** a Watch item that needs no action says it is approaching the single-name limit

<!-- tests: NONE -->

### Requirement: Opportunity and discovery tiers of the attention queue
The Opportunity tier SHALL list held tickers with a BUY or STRONG_BUY, confidence of at least 7 and weight under 10%, and up to 3 unheld BUY or STRONG_BUY forecasts with confidence of at least 7. The Discovery tier SHALL list opportunity-engine picks whose call is BUY.

#### Scenario: Room to add
- **WHEN** a held ticker is BUY with confidence 8 and 4% weight
- **THEN** an Opportunity item says there is room to add and asks the viewer to verify the thesis independently

#### Scenario: Trending forecasts excluded
- **WHEN** an unheld BUY forecast was produced as a trending opportunity
- **THEN** it is not listed as a watchlist opportunity

<!-- tests: NONE -->

### Requirement: Queue summary separates decisions from information
The queue SHALL state how many items need a decision and how many are FYI, group items under Your Portfolio (Critical, Watch, Opportunity) and New Investment Opportunities (Discovery), and show "Nothing requires attention today." when it is empty. The Review Queue tile MUST count Critical plus Watch items.

#### Scenario: Mixed queue
- **WHEN** the queue has 2 items that need action and 3 that do not
- **THEN** the summary reads "2 items need a decision, 3 are FYI"

#### Scenario: Empty queue
- **WHEN** no rule produces an item
- **THEN** the queue shows "Nothing requires attention today."

<!-- tests: NONE -->

### Requirement: What Fired feed ranks recent changes by urgency
The "What Fired" feed SHALL merge severity-3 events, recommendation changes, risk and technical flags and screener discoveries, sort them by urgency, and show at most 12. Urgency MUST be 100 for an unprocessed event and 55 for a processed one, 92 for a change to STRONG_SELL, 85 for SELL, 58 for STRONG_BUY, 50 for BUY and 40 otherwise, 70 plus twice the capped score for a risk flag, 55 for bearish momentum and 30 plus three times the capped score for a discovery.

#### Scenario: Ordering
- **WHEN** an unprocessed severity-3 event and a change to BUY both exist
- **THEN** the event is listed above the change

#### Scenario: Feed length
- **WHEN** more than 12 items qualify
- **THEN** only the 12 most urgent are shown

<!-- tests: NONE -->

### Requirement: Severity-3 events are market events, not holdings-only
The feed SHALL include every severity-3 trigger event from the last 48 hours regardless of whether the viewer holds the ticker, showing the portfolio weight only for held tickers. The attention queue MUST use only events on held tickers.

#### Scenario: Event on an unheld ticker
- **WHEN** a severity-3 event is detected on a ticker the viewer does not hold
- **THEN** it appears in the feed without a weight and does not appear in the queue

<!-- tests: NONE -->

### Requirement: Dashboard forecasts exclude challenger rows
Every forecast read on the Today dashboard and by the critical-item lookup SHALL exclude challenger shadow rows.

#### Scenario: Challenger is newer
- **WHEN** a challenger row is the most recent for a held ticker
- **THEN** the dashboard shows the latest non-challenger forecast

<!-- tests: NONE -->

### Requirement: System health strip on the dashboard
The dashboard SHALL show 5-day directional accuracy, Brier score and evaluated count from the latest rolling metrics (90-day lookback, else any lookback on the latest date) with a link to Validation, or "No validation data yet." when none exist.

#### Scenario: No metrics
- **WHEN** no rolling metrics exist
- **THEN** the strip shows "No validation data yet."

<!-- tests: NONE -->

### Requirement: Data freshness panel
The dashboard SHALL show the latest date of News, Research, Fundamentals and Holdings data, flagging them against 1, 3, 7 and 7 days respectively, and the last pipeline run with its status.

#### Scenario: Stale news
- **WHEN** the newest news date is older than 1 day
- **THEN** the News row is flagged as stale

<!-- tests: NONE -->

### Requirement: Macro strip and regime label
The dashboard SHALL show the 10Y-2Y spread, fed funds, CPI year over year and high-yield spread, and label the regime HIGH VOL when VIX is above 25, else INVERTED when the spread is negative, else RISK-ON when VIX is below 15, else NEUTRAL. The macro data SHALL be cached for one hour, and a fetch failure MUST leave the strip empty without failing the page.

#### Scenario: Elevated volatility
- **WHEN** VIX is 28
- **THEN** the regime label is HIGH VOL

#### Scenario: Macro source failure
- **WHEN** the macro fetch raises an error
- **THEN** the dashboard still renders and the regime shows a dash

<!-- tests: NONE -->

### Requirement: Critical-item lookup for other surfaces
The critical-item lookup SHALL return at most 3 items across the owner's own held positions: unprocessed severity-3 events, SELL or STRONG_SELL recommendations, single holdings above 20% and active risk flags, each with a ready-to-ask question. An owner with no portfolio or no holdings MUST get an empty list.

#### Scenario: No holdings
- **WHEN** the owner has a portfolio with no holdings
- **THEN** the lookup returns an empty list

#### Scenario: Limit
- **WHEN** five critical items exist
- **THEN** only the first three are returned

<!-- tests: NONE -->

### Requirement: Predictions page shows shared forecasts and the viewer's own private forecasts only
The Predictions page SHALL load the latest shared forecast per ticker and horizon, plus the viewer's own private forecasts marked as such, deduplicated separately so a private forecast never replaces the shared one. Another owner's private forecast MUST NOT appear.

#### Scenario: Other owner's private forecast
- **WHEN** another owner has a newer private forecast for a ticker
- **THEN** the viewer sees only the shared forecast for it

#### Scenario: Own private forecast
- **WHEN** the viewer has a private chat forecast for a ticker that also has a shared forecast
- **THEN** both rows appear and the private row carries the "your chat forecast" badge

<!-- tests: tests/test_predictions_page_data.py::test_load_predictions_excludes_other_users_private_forecasts_by_default, tests/test_predictions_page_data.py::test_load_predictions_never_lets_a_private_forecast_replace_the_shared_one, tests/test_predictions_page_data.py::test_load_predictions_includes_the_viewers_own_private_forecast_when_owner_given, tests/test_predictions_page_data.py::test_load_predictions_marks_is_own_private_false_for_shared_rows -->

### Requirement: Predictions page never shows challenger rows
The Predictions page loader SHALL exclude challenger shadow rows.

#### Scenario: Challenger on the same ticker
- **WHEN** a challenger forecast exists for a ticker and horizon
- **THEN** it is not returned to the page

<!-- tests: NONE -->

### Requirement: Predictions page scope and filters
The Predictions page SHALL list the viewer's holdings, watchlist and own private forecasts, keep trending-discovery forecasts off the page, and offer filters for date, horizon (All, 5d, 21d, 63d), trigger (scheduled or event), minimum conviction and hiding segments whose validation accuracy is under 52% with at least 10 evaluated predictions.

#### Scenario: Unrelated ticker
- **WHEN** a shared forecast exists for a ticker the viewer neither holds nor watches
- **THEN** it is not listed under Your holdings and watchlist

#### Scenario: Low-edge filter
- **WHEN** the hide-low-edge option is on and a segment has accuracy 48% over 20 predictions
- **THEN** its rows are hidden, and rows with fewer than 10 evaluated predictions are kept

<!-- tests: NONE -->

### Requirement: Personal context badges never change a forecast
Each listed forecast SHALL carry read-only badges for held and weight, restricted (the viewer's own list only), active blackout window and needs pre-clearance (from the viewer's own profile). The recommendation MUST NOT be altered by a badge.

#### Scenario: Another owner's restricted list
- **WHEN** only another owner has restricted a ticker
- **THEN** the viewer's forecast for it shows no restricted badge

#### Scenario: Restricted ticker still shows its call
- **WHEN** a ticker is on the viewer's restricted list and the shared call is BUY
- **THEN** the card shows BUY with a restricted badge

<!-- tests: tests/test_predictions_page_data.py::test_personal_overlay_reports_held_and_weight, tests/test_predictions_page_data.py::test_personal_overlay_reports_restricted_from_the_viewers_own_list, tests/test_predictions_page_data.py::test_personal_overlay_does_not_leak_a_different_owners_restricted_list, tests/test_predictions_page_data.py::test_personal_overlay_reports_active_blackout_window, tests/test_predictions_page_data.py::test_personal_overlay_reports_needs_pre_clearance_from_the_viewers_own_profile -->

### Requirement: Predictions grouping and action items
The page SHALL list Action Items as STRONG_BUY and STRONG_SELL forecasts with conviction of at least 7, and group all forecasts by recommendation in the order STRONG_BUY, BUY, HOLD, SELL, STRONG_SELL, with one multi-horizon card per ticker that has more than one horizon.

#### Scenario: Conviction below 7
- **WHEN** a STRONG_BUY forecast has conviction 6
- **THEN** it is not an Action Item but is listed under STRONG BUY

#### Scenario: Several horizons
- **WHEN** a ticker has 5-day and 21-day forecasts with the same recommendation
- **THEN** one multi-horizon card is shown under that recommendation

<!-- tests: NONE -->

### Requirement: Portfolio page sections
The Portfolio page SHALL offer the sections Overview, Holdings, Watchlist, Accounts & Imports and Health, open on Holdings by default, and render only the selected section.

#### Scenario: Default
- **WHEN** a person opens the Portfolio page for the first time in a session
- **THEN** the Holdings section is shown and the other sections are not rendered

<!-- tests: NONE -->

### Requirement: Holdings scope selector and onboarding
The Holdings section SHALL read the viewer's holdings and cash through the service layer with the viewer's context, offer all accounts or one account selected by a stable account id, and show onboarding when there are no holdings. Two accounts with the same nickname MUST keep distinct ids.

#### Scenario: Same nickname
- **WHEN** two accounts of one owner have the same display name
- **THEN** each has its own selection id and its own holdings group

#### Scenario: No holdings
- **WHEN** the viewer has no holdings
- **THEN** the page shows onboarding instead of the tiles

<!-- tests: tests/test_performance_and_summary.py::test_accounts_with_same_nickname_keep_distinct_ids -->

### Requirement: Holdings summary keeps unknown values unknown
The Holdings summary SHALL exclude unpriced positions from value totals and count them, and MUST show gain only where both value and cost basis are known.

#### Scenario: Unpriced position
- **WHEN** one position has no current value
- **THEN** it is counted as unvalued, excluded from the total, and its gain is empty

<!-- tests: tests/test_performance_and_summary.py::test_summary_keeps_unknown_values_unknown -->

### Requirement: Sector allocation labelling
The allocation view SHALL take a holding's sector from the holding row, else from cached universe data; a fund with no sector SHALL be labelled "Funds & ETFs" and any other missing sector "Unknown". Unvalued positions MUST be excluded and counted, and small slices folded into "Other" after the 7 largest.

#### Scenario: Fund without sector
- **WHEN** a held ETF has no sector anywhere
- **THEN** its value is grouped under "Funds & ETFs"

#### Scenario: Many small sectors
- **WHEN** more than 7 sectors exist
- **THEN** the smallest are combined into one "Other" slice

<!-- tests: tests/test_portfolio_charts.py::test_sector_breakdown_resolves_sectors_and_labels_funds, tests/test_portfolio_charts.py::test_sector_pie_folds_small_sectors_into_other -->

### Requirement: Allocation can be grouped by asset class or instrument type
The allocation view SHALL also group value by any holding field such as asset class or instrument type, labelling a missing value "Unclassified" and excluding and counting unvalued positions.

#### Scenario: Unclassified holding
- **WHEN** a valued holding has no asset class
- **THEN** it is grouped under "Unclassified"

<!-- tests: NONE -->

### Requirement: Value chart colours
The value stack chart SHALL use eight distinct hues plus an "Other" bucket with totals on top, and each ticker MUST keep the same colour in the stack and in the line views.

#### Scenario: More than eight tickers
- **WHEN** nine tickers have value
- **THEN** the eight largest have their own colours and the rest are grouped under "Other"

<!-- tests: tests/test_portfolio_charts.py::test_value_stack_has_eight_hues_an_other_bucket_and_totals_on_top, tests/test_portfolio_charts.py::test_line_views_keep_the_same_colour_per_ticker_as_the_stack, tests/test_portfolio_charts.py::test_rank_and_colour_assignment_never_cycle -->

### Requirement: Performance windows and security-type filter
The Performance tile SHALL offer the windows 1M, 3M, 6M, 1Y and All (30, 90, 182 and 365 days back from the latest date, or all history), default to 3M, and filter by All, Stocks only or Funds only.

#### Scenario: Default window
- **WHEN** the Performance tile is first opened
- **THEN** the 3M window is selected

#### Scenario: No rows
- **WHEN** the chosen filter leaves no history rows
- **THEN** the tile says no history rows match

<!-- tests: NONE -->

### Requirement: Investment return is time-weighted across observed snapshots
The investment return SHALL be a Modified Dietz return per interval between consecutive observed snapshots, chain-linked into a time-weighted return. A date containing any estimated row MUST break the chain, and the return needs at least two consecutive observed snapshots.

#### Scenario: Estimated day in the middle
- **WHEN** a date in the window is estimated, not observed
- **THEN** the chain restarts at the next observed pair and the estimated date still counts toward value

#### Scenario: Single snapshot
- **WHEN** fewer than two consecutive observed snapshots exist
- **THEN** the tile says investment return needs at least two consecutive observed snapshots

<!-- tests: tests/test_performance_and_summary.py::test_estimated_days_break_the_return_chain_but_count_in_value, tests/test_performance_and_summary.py::test_external_flow_timing_uses_modified_dietz_weight_between_snapshots, tests/test_performance_and_summary.py::test_multi_account_rows_sum_per_date -->

### Requirement: Buying or selling is not investment return
The chart SHALL show recorded value change separately from investment return, labelled as including buys and sells, and a price move with a cash flow MUST separate the flow from performance.

#### Scenario: Buying more shares
- **WHEN** shares are added between two snapshots with no price change
- **THEN** the investment return is zero while the recorded value rises

<!-- tests: tests/test_performance_and_summary.py::test_buying_more_shares_is_not_investment_return, tests/test_performance_and_summary.py::test_price_move_with_flow_is_separated -->

### Requirement: Ledger and statement accounts are treated differently in returns
Ledger-managed accounts SHALL include ledger cash and use recorded deposits, withdrawals, employer matches, opening balances, distributions and transaction fees as flows. Statement-managed accounts SHALL infer flows from share changes valued at the close. A Stocks only or Funds only view MUST exclude account cash and ledger fees.

#### Scenario: Ledger deposit
- **WHEN** a ledger account records a deposit between two snapshots
- **THEN** the deposit is an external flow and does not count as return

#### Scenario: Security-only view
- **WHEN** the Stocks only filter is selected
- **THEN** ledger cash and fees are excluded and share changes are the flows

<!-- tests: tests/test_performance_and_summary.py::test_ledger_performance_includes_cash_and_separates_external_deposits, tests/test_performance_and_summary.py::test_ledger_performance_nets_transaction_fees_and_reports_cash_costs, tests/test_performance_and_summary.py::test_security_only_view_excludes_ledger_cash_and_uses_share_changes_as_flows -->

### Requirement: Benchmark comparison over matched dates
The Performance tile SHALL compare the portfolio return with the S&P 500, Nasdaq and Dow Jones over the selected window using the closes actually available inside it, state the actual benchmark start and end dates, and show the portfolio return minus the S&P 500 return.

#### Scenario: Partial benchmark history
- **WHEN** benchmark closes begin after the window start
- **THEN** the benchmark return uses the first available close and the stated start date is that date

#### Scenario: S&P 500 unavailable
- **WHEN** no S&P 500 closes exist in the window
- **THEN** the tile says the S&P 500 comparison is unavailable and "Vs. S&P 500" is empty

<!-- tests: tests/test_performance_and_summary.py::test_benchmark_returns_use_the_requested_window_and_report_actual_close_dates, tests/test_performance_and_summary.py::test_normalize_index_series -->

### Requirement: Performance states its limits
The Performance tile SHALL state the return method, the number of estimated dates excluded, that snapshot-managed accounts lack historical cash and complete fees, and that benchmarks use auto-adjusted closes as a total-return proxy.

#### Scenario: Estimated dates present
- **WHEN** three dates in the window are estimated
- **THEN** the caption says three estimated dates are excluded from returns

<!-- tests: NONE -->

### Requirement: Watchlist section
The Watchlist section SHALL show a capacity meter against 70 tickers, count the tickers with fundamentals and with predictions, filter by text and by missing fundamentals, and include an add-ticker tile in the ticker grid.

#### Scenario: Missing fundamentals filter
- **WHEN** the missing-fundamentals filter is on
- **THEN** only tickers without stored fundamentals are shown

<!-- tests: NONE -->

### Requirement: Overview shows tracked brokerage assets, not net worth
The Overview SHALL state that it covers tracked brokerage assets only and is not a complete net-worth statement, and MUST total only the viewer's accounts, positions and cash read through the service layer with the viewer's context.

#### Scenario: Caption
- **WHEN** the Overview is rendered
- **THEN** it states that debts, property and accounts not added are excluded

#### Scenario: Aggregation by account
- **WHEN** the viewer has two accounts with positions
- **THEN** tracked assets equal securities value plus cash on file across those accounts only

<!-- tests: tests/test_wealth_overview.py::test_overview_aggregates_tracked_assets_by_account_without_guessing_unknown_values -->

### Requirement: Overview does not guess missing values
The Overview SHALL show cash only when an account reports it ("Not reported" per account, a dash in total), show unrealized gain only when every valued position has a cost basis, and warn about positions with no current value.

#### Scenario: Missing cost basis on one position
- **WHEN** one valued position has no cost basis
- **THEN** unrealized gain is not shown

#### Scenario: Unvalued position
- **WHEN** a position has no current value
- **THEN** it is excluded from totals and a warning counts it

<!-- tests: tests/test_wealth_overview.py::test_overview_shows_gain_only_when_all_valued_positions_have_cost_basis, tests/test_wealth_overview.py::test_unvalued_positions_do_not_change_gain_or_cost_basis_totals -->

### Requirement: Goals are notes, not funded progress
The Overview SHALL list active goals with type, target amount and date and state that targets are planning notes not linked to account balances.

#### Scenario: Active goal
- **WHEN** a goal is active
- **THEN** it is listed without any funded-progress indicator

<!-- tests: NONE -->

### Requirement: Recent activity and yearly summary
The Overview SHALL list the 8 most recent non-voided transactions of the viewer's ledger-managed accounts, and show the current year's dividends, interest, deposits, sale proceeds and purchases from saved history files and the ledger when any exist. A failure of the yearly summary MUST NOT break the Overview.

#### Scenario: Snapshot account
- **WHEN** an account is snapshot-managed
- **THEN** its transactions are not listed as recent activity

#### Scenario: Summary failure
- **WHEN** the yearly summary cannot be loaded
- **THEN** the rest of the Overview renders without it

<!-- tests: NONE -->

### Requirement: Profile form validation
The Profile form SHALL refuse to save and list every problem when a sector or issuer limit is outside 0 to 100, a per-candidate or post-trade fraction is outside 0 to 1, the currency is not a 3-letter code, the timezone is not an IANA name, the blackout start is after its end, or no prediction horizon is chosen.

#### Scenario: Invalid fraction
- **WHEN** the maximum post-trade position is entered as 1.5
- **THEN** an error is shown and nothing is saved

#### Scenario: Valid save
- **WHEN** every value is valid
- **THEN** the profile is saved through the service layer with the viewer's context and "Profile saved." is shown

<!-- tests: NONE -->

### Requirement: Profile limits are read-only when they come from the investor profile
When the viewer's limits source is the investor profile, the Mandate Limits fields SHALL show the profile's limits and be disabled.

#### Scenario: Limits source is profile
- **WHEN** the limits source is "profile"
- **THEN** the limit inputs are disabled and show the effective policy values

<!-- tests: NONE -->

### Requirement: Disclaimer acceptance is stamped once
Ticking the disclaimer acceptance SHALL stamp the time on first acceptance and keep the original stamp while it stays ticked, and unticking it MUST clear the stamp.

#### Scenario: Re-save while accepted
- **WHEN** the profile is saved again with the box still ticked
- **THEN** the original acceptance time is kept

<!-- tests: NONE -->

### Requirement: Schedule page is admin-only and launches runs as background jobs
The Schedule page SHALL be refused to non-admins and, for admins, launch the Morning, Intraday, Evening, Full Daily (force-all) and Weekly Analysis jobs as background processes of the main program with a run id, a log file and a web trigger marker, allow only one job at a time, and offer a stop control.

#### Scenario: Non-admin
- **WHEN** a non-admin opens the Schedule page
- **THEN** "Admin only." is shown and no control is rendered

#### Scenario: Job already running
- **WHEN** one job is running
- **THEN** the buttons of the other jobs are disabled

<!-- tests: NONE -->

### Requirement: Database viewer is admin-only and read-only
The Database page SHALL be refused to non-admins and, for admins, let them browse stored fundamentals, news, research, predictions, holdings, validation and trigger events by date range without editing them.

#### Scenario: Non-admin
- **WHEN** a non-admin opens the Database page
- **THEN** "Admin only." is shown and no data is queried

<!-- tests: NONE -->
