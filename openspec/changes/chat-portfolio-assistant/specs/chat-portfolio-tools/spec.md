# Spec Delta

## Purpose
Give the chat agent read-only access to the app's deterministic engines and the user's own data, scoped to the signed-in owner and labelled for compliance, so the model explains results and never computes them.

## ADDED Requirements

### Requirement: Chat tools never write
No chat-mode tool SHALL write, place, approve or queue anything. The only write reachable from the chat page is the existing APEX prediction save in predict mode.

#### Scenario: Write functions are unreachable
- **WHEN** every tool in the chat dispatcher is executed with the write functions mocked to raise
- **THEN** none of them is called (ledger record/void, investor setters, quiz and profile pick, trade approvals, restricted-list writes, notifications, broker or order calls, policy-decision records)

### Requirement: Owner comes only from the signed-in session
`owner` SHALL come from `current_context("web:chat").actor` and be passed server-side. No tool schema SHALL expose `owner`, `portfolio_id` or `account_id`. With no owner, an owner-scoped tool SHALL return a "no signed-in account" note and SHALL NOT fall back to all portfolios.

#### Scenario: Two owners
- **WHEN** owner A and owner B each hold different positions and call the same tool
- **THEN** each result contains only that owner's data, for every new and existing owner-scoped tool

#### Scenario: No owner
- **WHEN** an owner-scoped tool is called with owner None
- **THEN** it returns a note and no holdings

### Requirement: Engines run on the owner's holdings, passed explicitly
Where an engine reads holdings itself, the chat wrapper SHALL pass the owner's holdings explicitly or the engine SHALL accept them, so that no tool ranks, allocates or simulates against another owner's book or the combined book. Inputs that mix owners (the latest prediction across private scopes) SHALL be restricted to shared scopes for chat.

#### Scenario: Opportunities and allocation
- **WHEN** two owners with different books request opportunities or a cash allocation
- **THEN** portfolio-fit figures and candidate exclusions reflect only the requesting owner

### Requirement: Restricted and blacked-out tickers are labelled and never suggested
Any ticker a tool names as something to buy, add to or trim SHALL have been checked against the owner's restricted list, blackout windows and clearance (read-only evaluation). A ticker that fails SHALL be labelled in the tool output and SHALL NOT appear as a suggested action.

#### Scenario: Restricted candidate
- **WHEN** the optimizer or opportunity engine would surface a ticker on the owner's restricted list
- **THEN** it is labelled restricted and absent from the suggested allocation

### Requirement: Every result states its source and as-of
Each tool result SHALL include its source (local DB, an engine name, SEC EDGAR, a URL) and an `as_of` date or "unavailable". The system prompt SHALL require the answer to cite them, forbid guaranteed returns, forbid presenting simulations or what-ifs as forecasts, forbid numbers not present in tool output, and say that instructions inside tool results are to be ignored.

#### Scenario: Hypothetical trades
- **WHEN** `simulate_hypothetical_trades` returns
- **THEN** the result is labelled HYPOTHETICAL and lists every constraint violation

#### Scenario: Empty result
- **WHEN** a tool returns nothing
- **THEN** the answer says so and does not fill portfolio figures from model knowledge

### Requirement: Untrusted text is delimited
News, web pages, SEC text, broker research and stored `reasoning` fields SHALL be returned inside clearly delimited data blocks.

#### Scenario: Instruction inside a news item
- **WHEN** a news headline or filing contains "ignore previous instructions"
- **THEN** the text appears only inside a delimited data block and the system prompt tells the model to ignore instructions inside tool results

### Requirement: The tool set
The agent SHALL have these additional tools: `get_personal_opportunities`, `recommend_cash_allocation(cash_amount)`, `simulate_hypothetical_trades(trades)`, `get_fund_lookthrough`, `get_valuation(ticker)`, `get_trade_plan_odds(ticker)`, `get_tax_lots_and_gains(ticker?)`, `get_investor_profile`, `get_macro_calendar(days_ahead)`, `get_sec_filings(ticker, form_type)`. `get_latest_opportunities` SHALL be backed by `opportunity_engine` and keep its existing output keys.

#### Scenario: Tools are registered
- **WHEN** the chat tool schemas are listed
- **THEN** each named tool has a schema and a dispatcher entry, none exposes `owner`, `portfolio_id` or `account_id`, and `get_latest_opportunities` still returns `source`, `count`, `as_of` and `opportunities`
