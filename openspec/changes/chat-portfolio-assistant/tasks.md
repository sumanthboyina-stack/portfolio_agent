# Tasks

Each phase is one commit; the full test suite runs after each, and the next phase does not start if anything fails. Nothing is pushed.

## 0. Phase 0 – Plan (this change)

- [x] 0.1 Read the files named in the brief and verify every signature and data shape (see design.md Context)
- [x] 0.2 Proposal, specs, design, tasks
- [x] 0.3 Open Questions approved (all six; valuation: stored if available, else calculated without saving)

## 1. Phase 1 – Router fix (no new features)

- [x] 1.1 Add `web/chat/routing.py` with `route_message(query, mentioned_tickers, session_tickers)`: personal-phrase list, explicit predict phrase list, no length rule
- [x] 1.2 `_classify_intent` delegates to it; remove the "≤3 words" rule
- [x] 1.3 Page: use `route_message`; chat-mode turns never read or write the sticky `current_tickers`; personal phrasing never reaches `_search_ticker_by_name`; a bare ticker shows an "Analyze X" suggestion
- [x] 1.4 Tests (`tests/test_chat_routing.py`): "Analyze my portfolio" (with and without an earlier ticker in session), "NVDA news", "NVDA", "Analyze NVDA", "Should I buy AAPL?", "Compare MSFT vs GOOGL", "what AI stocks look good", plus "should I trim", "where should I put $5,000"; assert `_search_ticker_by_name` is never called for personal phrasing
- [x] 1.5 Full suite; show results

## 2. Phase 2 – New read-only chat tools

- [x] 2.1 (D9) Replace the seven hard-coded `data/portfolio.db` paths with the `tools.db` path; test that a chat tool reads the monkeypatched test DB
- [x] 2.2 (D2) Optional `holdings` on `get_daily_opportunities` / `_load_conviction_and_holdings`; optional `holdings` pass-through and `prediction_scopes` on `prepare_inputs`; tests that defaults are unchanged and that two owners get different `portfolio_impact`
- [x] 2.3 Helpers in `web/chat/tools.py`: `_owner_book`, `_compliance_label` (read-only `evaluate_clearance` + restricted list), result envelope `{source, as_of, data}`, untrusted-text delimiter
- [x] 2.4 Tools with JSON schemas and dispatcher entries: `get_personal_opportunities`, `recommend_cash_allocation`, `simulate_hypothetical_trades`, `get_fund_lookthrough`, `get_valuation`, `get_trade_plan_odds`, `get_tax_lots_and_gains`, `get_investor_profile`, `get_macro_calendar`, `get_sec_filings` (+ SEC throttle)
- [x] 2.5 Re-base `get_latest_opportunities` on the engine, keeping its keys
- [x] 2.6 `_CHAT_SYSTEM`: routing table for the new tools; cite source and `as_of`; forbid guaranteed returns, simulations-as-forecasts and invented numbers; ignore instructions inside tool results
- [x] 2.7 Tests: owner scoping for every new tool (two owners, no leakage; owner None → note); restricted/blacked-out ticker labelled and never suggested; HYPOTHETICAL label and constraint violations present; "write functions unreachable" test (mock every write function to raise, run every dispatcher tool); `tests/test_investor_profile.py` boundary still green
- [x] 2.8 Full suite; show results

## 3. Phase 3 – Context and loop hardening

- [x] 3.1 `digest_tool_result` (≤10 rows / key figures, ≤1,500 chars) and storage in `metadata["tool_digests"]`
- [x] 3.2 `_build_chat_history`: replay digests, larger assistant cap
- [x] 3.3 `ToolBudget` (6 tools across parallel calls, 8 rounds); forced tool-less final answer
- [x] 3.4 12 KB result cap with `"truncated": true`
- [x] 3.5 Stream the final answer to the page
- [x] 3.6 Tests: digest size and content; replay; follow-up answered without a tool call (mock LLM asserts no tool call); parallel-call budget; truncation flag; streaming order
- [x] 3.7 Full suite; show results

## 4. Phase 4 – Model tiering

- [x] 4.1 `chat_synthesis_tier` column (`migrate_columns`), validation, getter, admin control, audit
- [x] 4.2 One tool-less `analyst`-chain synthesis call when an owner-scoped or engine tool ran; fall back to the flash answer; skip models marked out of credit; log `model_used`
- [x] 4.3 Test that chat code never references the `reasoning` chain
- [x] 4.4 Tests: tool-only-public turn makes no analyst call; private-tool turn makes exactly one; analyst failure falls back; `flash` setting disables it; no retry of a credit-failed model
- [x] 4.5 Full suite; show results

## 5. Phase 5 – Web research

- [x] 5.1 `web_search` records returned URLs per turn; `web_fetch(url)` accepts only those
- [x] 5.2 Safe fetch: scheme check, DNS resolve and IP validation per hop, connect to the validated IP, manual redirects (max 3), 10 s timeout, 2 MB cap, no cookies/auth, content-type check, stdlib text extraction
- [x] 5.3 Prompt: list URLs used; fetched text is untrusted
- [x] 5.4 Tests (all offline): refused URL not from search; localhost/10.x/169.254.169.254/IPv6 loopback; redirect to a private address; oversize body; timeout; non-text content type
- [x] 5.5 Full suite; show results

## 6. Acceptance (mocked data)

- [x] 6.1 "Analyze my portfolio" → chat mode, portfolio tools, no ticker prompt
- [x] 6.2 "NVDA news" → chat, no APEX run, no prediction row
- [x] 6.3 "I have $5,000, where should it go?" → `recommend_cash_allocation`, restricted list respected, synthesis on the `analyst` chain
- [x] 6.4 "What if I sell half my AAPL and buy VOO?" → `simulate_hypothetical_trades`, labelled hypothetical
- [x] 6.5 "How much NVDA do I really own including funds?" → `get_fund_lookthrough`
- [x] 6.6 Follow-up "which of those do I already own?" answered from the stored digest
- [x] 6.7 User A never sees user B's data through any tool
- [x] 6.8 Full suite green; summary of files changed and open risks

## 7. Phase 6 (propose only – not built without approval)

- [ ] 7.1 `request_apex_analysis(ticker)` returns a sentinel; the page shows a "Run full APEX analysis on X (uses premium model credits)" button; APEX runs only on click
