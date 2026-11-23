## MODIFIED Requirements

### Requirement: Portfolio and extra tickers bypass scoring
Tickers that are held in a portfolio or passed as extras SHALL go to summarizing whenever they have any articles, whether or not they score. Those that survive triage SHALL also be scored in Stage 3 and their score recorded; the score MUST NOT gate summarizing. Their filter-log stage-3 decision SHALL stay `bypassed_portfolio`. Other tickers MUST be summarized only when their Stage 3 score is above 0.

#### Scenario: Held ticker with articles
- **WHEN** a held ticker has articles and survived triage
- **THEN** it is scored, summarized even if the score is 0, and its filter-log row has stage-3 decision `bypassed_portfolio` and the score

#### Scenario: Held ticker routine in Stage 2
- **WHEN** a held ticker has articles but was judged routine in Stage 2
- **THEN** it is summarized and its filter-log row has stage-3 decision `bypassed_portfolio` and no score

#### Scenario: Watchlist ticker scored 0
- **WHEN** a non-held ticker scores 0
- **THEN** it is dropped with skip reason `low_materiality`

<!-- tests: tests/test_held_ticker_material_events.py::test_held_tickers_that_survive_triage_are_scored_and_still_summarized, tests/test_held_ticker_material_events.py::test_held_ticker_scoring_zero_is_still_summarized -->

### Requirement: Material-news score feeds event detection
The event detector SHALL treat a ticker as having material news when its `news_filter_log` row for the date has a non-null `stage_3_score` at or above the severity threshold (default 3) and final decision `analyzed`, and SHALL record the score as the event's severity. This applies to held tickers like any other. Rows with no score, such as held tickers that did not survive triage, MUST NOT produce an event.

#### Scenario: Scored at threshold
- **WHEN** a ticker's log row has score 3 and final decision `analyzed`
- **THEN** a `material_news` event with severity 3 is recorded

#### Scenario: Held ticker scored at threshold
- **WHEN** a held ticker's log row has stage-3 decision `bypassed_portfolio`, score 3 and final decision `analyzed`
- **THEN** a `material_news` event with severity 3 is recorded for it

#### Scenario: Bypassed ticker
- **WHEN** a held ticker's log row has stage-3 decision `bypassed_portfolio` and a null score
- **THEN** no material-news event is recorded for it

<!-- tests: tests/test_notification_producers.py::test_the_events_end_to_end_through_detect_material_news, tests/test_review_findings.py::test_run_all_detectors_detects_and_persists_material_news, tests/test_held_ticker_material_events.py::test_a_scored_held_ticker_raises_a_material_news_event -->
