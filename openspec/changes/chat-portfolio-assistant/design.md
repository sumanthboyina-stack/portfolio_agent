# Design

## Context

Every statement below was checked against the code on 2026-11-21 (branch `main` at `33f5af7`), not taken from the task description.

**Chat today.** `web/pages/4_🤖_Chat.py` extracts tickers (`_extract_tickers`: company map + known-ticker regex), keeps them in `st.session_state.current_tickers`, then calls `_classify_intent(query, tickers)` (`web/chat/orchestration.py:163`). Chat mode is `_run_chat_agent_thread` (LiteLLM loop, `flash` chain, `range(8)` rounds, all tool calls of a round executed, 20 tool schemas). Predict mode is `_run_apex_thread` (`reasoning` chain) and the page saves the result with `write_private_forecast` (origin `chat.ticker_prediction`). `chat_messages` already has a `metadata TEXT DEFAULT '{}'` column, written by `chat_db.add_message(..., metadata=)`.

**What the verification found that differs from the task description or adds to it:**

1. **Router: a second bug.** `current_tickers` is sticky for the whole session and is only reset when a new ticker is found. After one NVDA question, "Analyze my portfolio" reaches `_classify_intent` with `["NVDA"]`, matches "analyze", and runs a full paid APEX panel on **NVDA**. With no earlier ticker it takes the `_search_ticker_by_name` ("Did you mean…") path. The page's routing is inline code, not a function, so "page-level" tests need it extracted.
2. **Opportunity engine reads the wrong portfolio.** `opportunity_engine._load_conviction_and_holdings` calls `portfolio_tools.get_portfolio_holdings()` with no tool context. With no owner, `_load_portfolio(None)` calls `holdings_db.get_holdings()` with no `portfolio_id`: **every owner's holdings, combined.** So `portfolio_impact`, the portfolio-fit bonus and "why" text describe the combined book for everyone. Callers: `scoring_snapshot` (batch), `scenarios.prepare.prepare_inputs`, `portfolio_optimizer`.
3. **`recommend_allocation` has the same default.** `recommend_allocation(cash_amount, top_n_candidates, holdings=None, scope="all", owner=None)`: when `holdings` is None it reads the combined book. `owner` is used only for the restricted list and profile. It then calls `prepare_inputs`, which calls `get_daily_opportunities(top_n)` (item 2).
4. **`prepare_inputs` reads predictions across private scopes.** `get_all_latest_predictions()` defaults to `DISPLAY_SCOPES = (shared, legacy, private)` with no owner filter: the latest row per ticker can be another owner's private chat forecast, and it feeds `expected_returns`. It also does not check blackout windows or clearance, only the owner's restricted list.
5. **`evaluate_and_record_clearance` writes** a private `PolicyDecision` row. The prediction card already uses it (predict mode). Chat-mode tools must use the read-only `clearance.evaluate_clearance`.
6. **Seven existing chat tools hard-code `_ROOT/data/portfolio.db`** (`sqlite3.connect`) instead of `tools.db`. Monkeypatching `DB_PATH` does not isolate them, so any test of them reads the **live** database. New tools use `db_conn`/services; the seven are a one-line fix each (item D9).
7. **No SEC rate limiter exists.** `edgar.py` sets a `User-Agent` from `EDGAR_USER_AGENT` with a placeholder default (`PortfolioAgent contact@example.com`) and has no throttle. The task says to respect "the existing rate limiter"; there is none.
8. **Valuation cannot be computed on demand without touching the batch.** `_run_daily_valuation` inlines fundamentals fetch, peers, multiples and the engine calls, then `upsert_valuation` (a write). There is no standalone compute function.
9. **`lookthrough.analyze` needs positions and fund views.** `portfolio_assessment.get_portfolio_exposures(ctx)` already builds both for the owner (and caches), and exposes issuer percentages, overlap and coverage.
10. **`Trade` needs `account_id`.** What-ifs from natural language ("sell half my AAPL") must be mapped to the owner's accounts server-side; the model must not supply account ids.
11. **The chat loop restarts its chain every message** and has its own `_FAILOVER_ERRORS` list (separate from `_models._is_failover_error`), so a dead flash model costs a failed call on every turn. The `analyst` chain is exactly the one that is out of credit today.
12. **`get_latest_opportunities`** runs raw SQL without a shared-scope filter; the engine version filters shared scopes.

## Goals / Non-Goals

**Goals:** a chat that answers portfolio and opportunity questions by calling the existing engines for the signed-in owner; correct routing; bounded, cheap loops; answers with sources and as-of dates; no new write path; no cross-owner leakage.

**Non-Goals:** any write, order, approval or notification from chat; changes to `_apply_conviction_guardrails`, `apply_conviction_guardrails_to_horizons`, `clearance.py`, restricted-list logic, trade-plan math or prediction schemas; touching the batch pipeline, `.env`, secrets, `run_*.sh` or `data/portfolio.db`; Phase 6 (`request_apex_analysis`) is proposed only.

## Decisions

**D1 Routing is one pure function.** New `web/chat/routing.py: route_message(query, mentioned_tickers, session_tickers) -> Route(mode, tickers, suggest)`. Order: (a) personal/portfolio phrase → chat, tickers = mentioned only (never `session_tickers`); (b) explicit predict phrase + a ticker resolved from this message → predict; (c) predict phrase with no ticker and no personal phrase → existing "which stock?" prompt, resolved only from the message itself; (d) otherwise chat. A bare ticker adds a "type Analyze X" suggestion. `_classify_intent` keeps its name and delegates. The page calls `route_message` and stops mutating `current_tickers` for chat-mode turns. Chosen over patching the inline branches because tests must run the page's real decision.

**D2 Owner scoping by explicit holdings.** `get_daily_opportunities(top_n, holdings=None)` and `_load_conviction_and_holdings(holdings=None)`: when `holdings` is given they are used; when not, behaviour is exactly today's (batch and `scoring_snapshot` unchanged). `prepare_inputs(..., prediction_scopes=None)` and a `holdings` pass-through to `get_daily_opportunities`; chat passes the owner's holdings and `prediction_scopes=SHARED_SCOPES`. No default changes. Alternative (change the defaults to require an owner) rejected: it would break the batch.

**D3 Chat tool wrappers live in `web/chat/tools.py`** (thin, JSON in/out, `owner` injected by `_execute_chat_tool`, never in a schema). Shared helper `_owner_book(owner)` returns the owner's portfolio id, holdings and a `no signed-in account` note when owner is None. Every candidate ticker passes `_compliance_label(owner, ticker)` using `clearance.evaluate_clearance` (read-only) plus the owner's restricted list; labelled tickers are excluded from suggested actions and shown in a `restricted_or_blocked` list.

**D4 Tool-by-tool wiring (verified signatures).**
- `get_personal_opportunities`: `opportunity_engine.get_daily_opportunities(top_n, holdings=owner_holdings)`; adds `already_held`, `weight_pct`, and `portfolio_impact` (correlation/overlap already in the engine's impact).
- `recommend_cash_allocation(cash_amount)`: `portfolio_optimizer.recommend_allocation(cash, top_n, holdings=owner_holdings, scope="all", owner=owner)` with D2; result post-filtered by compliance labels.
- `simulate_hypothetical_trades(trades)`: model supplies `{ticker, side, fraction|quantity|amount}`; the wrapper maps to the owner's account(s) (pro-rata across accounts holding it for a sell; the funding account `prepare_inputs` chooses for a buy), builds `scenarios.engine.Trade`, calls `prepare_inputs` + `evaluate` (not `propose_trades`), returns `status`, `trades`, `rejected`, `constraints`, `warnings`, `before`/`after`; labelled HYPOTHETICAL.
- `get_fund_lookthrough`: `portfolio_assessment.get_portfolio_exposures(ctx)` (which runs `lookthrough.analyze` on the owner's positions and the owner's fund views) → issuer %, via-fund %, overlap, coverage. This deviates from "wrap `lookthrough.analyze`" because `analyze` needs positions and views that this function already assembles correctly per owner.
- `get_valuation(ticker)`: `valuation_db.get_stored_valuation` only, with every assumption and `as_of_date` returned. **No on-demand compute** (finding 8): "none stored" is reported. Needs your decision (Open Questions).
- `get_trade_plan_odds(ticker)`: latest shared forecast via `get_latest_prediction(..., scopes=SHARED_SCOPES)` → stored `trade_plans` row and `plan_simulations` row (the simulation is already stored for planned rows); only for a shared UP forecast; labelled "historical resampling, not a forecast". No new simulation is run in chat.
- `get_tax_lots_and_gains(ticker?)`: `ledger_service.get_lots` / `get_closures` / `get_ledger_summary`, which already authorise the owner through `_owned_account`; iterates `account_service.list_accounts_for(ctx)`. Snapshot accounts report "no lots (snapshot account)".
- `get_investor_profile`: `investor_service.get_investor_state(ctx)`.
- `get_macro_calendar(days_ahead)`: `macro.calendar.get_upcoming_events` (returns at most 10 events; Finnhub optional).
- `get_sec_filings(ticker, form_type)`: `edgar.get_sec_filings` (+ `get_earnings_guidance` for 8-K). New: a small per-process throttle (≤ 5 requests/second, below SEC's 10) and a warning note when `EDGAR_USER_AGENT` is unset. Text from filings is wrapped as untrusted data.
- `get_latest_opportunities`: keeps its keys (`source`, `count`, `as_of`, `opportunities[...]` with `ticker, recommendation, confidence, composite_score, news_score, research_score, fundamental_score, reasoning, created_at`) but is fed by the engine ranking, with `opportunity_score`, `call`, `why`, `already_held` added.

**D5 Context.** Digest = `{tool, args, as_of, rows|figures, truncated}` capped at 1,500 chars, built by a pure `digest_tool_result`. Stored in `metadata["tool_digests"]` of the assistant message through the existing `add_message(metadata=)`; no migration. `_build_chat_history` raises the assistant text cap, replays digests as `[tool digest]` blocks, and keeps the 12-message window.

**D6 Loop.** `ToolBudget(max_tools=6, max_rounds=8)` object in code; calls beyond the budget in a parallel round are refused with a note; when the budget is spent, one more call with `tools` omitted. Any tool result over 12 KB is truncated at a row/field boundary and flagged `truncated`. The final answer is streamed with `stream=True` and pushed as `text` chunks (the page appends to a placeholder).

**D7 Tiering.** After the tool loop, if any tool in `{owner-scoped, engine}` ran, one tool-less call on `FAILOVER_CHAINS["analyst"]` with the system prompt, the question and the tool results. It reuses `apex_dual_run`'s out-of-credit memory (branch `fix/apex-dead-model-memory`): marked models are skipped, not retried. On failure the flash answer is used. `chat_synthesis_tier` is a new column on `engine_settings` (`migrate_columns`, default `'analyst'`), validated in `update_engine_settings`, with an admin control. `reasoning` is never referenced from chat mode (a test asserts it).

**D8 Web fetch.** `requests` with `allow_redirects=False` and a manual redirect loop (max 3) so each hop is re-validated; every hop resolves DNS and rejects loopback, private, link-local, multicast, reserved and metadata (169.254.169.254, fd00::/8) addresses and connects to the **validated IP** (with the Host header) to close the DNS-rebinding gap; 10 s timeout; streamed read stopped at 2 MB; no cookies/auth; `Content-Type` must be text/html, text/plain or application/xhtml; text extracted with the standard-library `html.parser` (scripts/styles dropped). A per-turn allow-list is filled from `web_search` results.

**D9 Test isolation fix for the seven hard-coded DB paths.** Replace `_ROOT / "data" / "portfolio.db"` with the path from `portfolio_agent.tools.db` at call time. Behaviour in production is identical.

## Risks / Trade-offs

- **Credits.** The `analyst` chain is currently mostly out of credit; with D7 the cost is one fast failure per turn at most, then a cool-off, and the answer falls back to flash. Setting `chat_synthesis_tier = flash` turns it off.
- **Touching engines (D2).** Small optional parameters on `opportunity_engine` and `prepare_inputs`; defaults unchanged, covered by new tests and the existing suite. If you would rather not touch them, those two tools (`get_personal_opportunities`, `recommend_cash_allocation`) are left out (per "owner scoping can't be guaranteed → leave the tool out").
- **Router strictness.** Fewer messages reach the paid panel; users who relied on short messages must say "Analyze X". Intended.
- **DNS pinning** adds complexity to `web_fetch`; the alternative (resolve then connect by name) leaves a rebinding window.
- **Sticky session tickers** are still used for chat answers that reference "it"/"that stock" (no personal phrase), which keeps follow-ups natural but can mis-attach; chat mode only, never predict.

## Migration Plan

One idempotent column (`engine_settings.chat_synthesis_tier`) created on first open by the existing migration helper; the live database file is not edited by hand. Rollback: the column is ignored by older code.

## Open Questions (need your answer before Phase 1)

1. **D2:** OK to add optional `holdings` / `prediction_scopes` parameters to `opportunity_engine` and `scenarios.prepare_inputs`? (Otherwise the two tools are left out.)
2. **D4 valuation:** accept "stored valuation only, no on-demand compute" (computing needs the batch's inlined code, which is off-limits)?
3. **D4 look-through:** accept wrapping `get_portfolio_exposures` instead of calling `lookthrough.analyze` directly?
4. **D7:** may chat depend on the out-of-credit memory from `fix/apex-dead-model-memory` (needs that branch merged first, or cherry-picked)?
5. **D9:** OK to change the seven hard-coded DB paths in `web/chat/tools.py` (needed so owner-scoping tests never read the live database)?
6. **D4 SEC:** add a small throttle and an unset-`EDGAR_USER_AGENT` warning to the chat path (there is no existing limiter to reuse)?
