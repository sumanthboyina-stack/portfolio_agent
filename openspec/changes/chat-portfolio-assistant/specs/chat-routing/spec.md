# Spec Delta

## Purpose
Decide, for each chat message, whether it is answered conversationally or by the paid APEX panel, without guessing tickers from personal or portfolio wording.

## ADDED Requirements

### Requirement: Personal and portfolio phrasing always routes to chat
A message containing personal or portfolio phrasing ("my portfolio", "my holdings", "what I own", "my account", "should I trim", "where should I put") SHALL be answered by chat mode. It SHALL NOT call `_search_ticker_by_name`, SHALL NOT ask the user to confirm a company, and SHALL NOT inherit a ticker from earlier in the session.

#### Scenario: Analyze my portfolio
- **WHEN** the user sends "Analyze my portfolio" in a session that earlier discussed NVDA
- **THEN** the route is chat, no ticker confirmation is shown, no APEX run starts, and the session ticker is not used

### Requirement: Predict mode needs an explicit phrase and a resolved ticker
Predict mode SHALL start only when the message contains an explicit predict phrase AND names a ticker or company that resolves without guessing. A message of three words or fewer SHALL NOT route to predict by length alone.

#### Scenario: Bare ticker
- **WHEN** the user sends "NVDA"
- **THEN** the route is chat and the answer suggests typing "Analyze NVDA"

#### Scenario: Ticker plus a short request
- **WHEN** the user sends "NVDA news"
- **THEN** the route is chat, no APEX run starts and no prediction row is written

#### Scenario: Explicit request
- **WHEN** the user sends "Analyze NVDA" or "Should I buy AAPL?"
- **THEN** the route is predict for that ticker

### Requirement: Broad and comparison questions go to chat
Screening, comparison and exploratory questions SHALL route to chat even when they mention tickers.

#### Scenario: Comparison and screen
- **WHEN** the user sends "Compare MSFT vs GOOGL" or "what AI stocks look good"
- **THEN** the route is chat

### Requirement: One routing function, tested
The routing decision SHALL be one pure function used by the page, so the tests exercise the same code the page runs.

#### Scenario: Page and tests agree
- **WHEN** the page routes a message
- **THEN** it calls the same function the unit tests call, and no routing branch remains inline in the page
