# Proposal

## Why

APEX Chat can answer questions about the user's portfolio and the pipeline's opportunities, but it is routed badly and only reaches a fraction of the deterministic engines the app already has. "Analyze my portfolio" triggers a "Did you mean <random company>?" prompt (or, with a ticker already in the session, a full paid APEX run on the *old* ticker), and "NVDA news" launches the paid APEX panel. Questions like "where should $5,000 go?", "what if I sell half my AAPL and buy VOO?" or "how much NVDA do I really own including funds?" have working engines behind them but no chat tool. The final answer is written by the free `flash` chain, follow-ups lose context, and the "at most 6 tool calls" rule lives only in the prompt.

## What Changes

- **Phase 1 – Router.** Extract the routing decision (today inline in the page plus `_classify_intent`) into one pure, tested function. Personal/portfolio phrasing never reaches `_search_ticker_by_name`, never inherits a stale session ticker, and never starts APEX. The "≤3 words → predict" rule is removed; a bare ticker goes to chat with an "Analyze NVDA" suggestion.
- **Phase 2 – Read-only tools.** Ten new chat tools, each a thin wrapper over an existing engine (opportunities, cash allocation, hypothetical trades, fund look-through, stored valuation, trade-plan odds, tax lots and gains, investor profile, macro calendar, SEC filings), plus `get_latest_opportunities` re-based on `opportunity_engine`. Every tool is owner-scoped server-side, labels restricted/blacked-out tickers, and returns an `as_of` and a source.
- **Phase 3 – Context and loop.** Persist a compact digest of each tool result in `chat_messages.metadata` and replay it; enforce 6 tool executions / 8 rounds in code; cap a tool result at 12 KB; stream the final answer.
- **Phase 4 – Model tiering.** Tool selection stays on `flash`; when an owner-scoped or engine tool ran, one tool-less synthesis call on the `analyst` chain (setting `chat_synthesis_tier`, default `analyst`), falling back to the flash answer. `reasoning` stays predict-only.
- **Phase 5 – Web research.** A `web_fetch(url)` tool limited to URLs returned by `web_search` in the same turn, with SSRF, size, time and content limits; answers list the URLs used.
- **Prerequisite fixes found while verifying** (see design): the opportunity engine and cash-allocation path read every owner's holdings when no holdings are passed; the scenario inputs read the latest prediction across private scopes; seven existing chat tools hard-code `data/portfolio.db`, which defeats test isolation.
- No change to guardrails, clearance, restricted-list logic, trade-plan math, prediction schemas, the batch pipeline, `.env`, secrets or `data/portfolio.db`.

## Capabilities

### New Capabilities
- `chat-routing`: how a message becomes chat mode or predict mode, and when a ticker is resolved or confirmed.
- `chat-portfolio-tools`: the read-only, owner-scoped, compliance-labelled tool set the chat agent can call, and the rules every tool result must satisfy.
- `chat-context-and-loop`: persisted tool digests, replayed history, the enforced tool/round budget, the result-size cap and answer streaming.
- `chat-synthesis-tiering`: which model chain selects tools and which writes the final answer, and the setting that controls it.
- `chat-web-research`: `web_fetch` and its safety limits, and citation of web sources.

### Modified Capabilities
<!-- None: openspec/specs/ is empty, so there is no existing spec to modify. -->

## Impact

- **Code:** `web/pages/4_🤖_Chat.py`, `web/chat/orchestration.py`, `web/chat/tools.py`, new `web/chat/routing.py`; small optional-parameter changes in `portfolio_agent/tools/opportunity_engine.py`, `portfolio_agent/scenarios/prepare.py`, `portfolio_agent/tools/portfolio_optimizer.py`; `portfolio_agent/tools/engine_settings_db.py` (one setting); tests under `tests/`.
- **Database:** one idempotent column, `engine_settings.chat_synthesis_tier` (existing `migrate_columns` pattern). No new table. Tool digests use the existing `chat_messages.metadata` JSON column.
- **Dependencies:** none new (`requests` and `ddgs` are already used; HTML-to-text uses the standard library).
- **Cost:** default paths add no paid calls except the single Phase 4 synthesis call.
- **Depends on:** the out-of-credit model memory on branch `fix/apex-dead-model-memory` (unmerged), so a dead `analyst` chain does not add a failing walk to every chat turn.
