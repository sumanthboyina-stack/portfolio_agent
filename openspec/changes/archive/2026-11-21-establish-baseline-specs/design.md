# Design: gap report for the as-built baseline

Baseline of HEAD `ca450dd`. This document is the Phase 3 gap report for `establish-baseline-specs`: it records where README and code disagree, what is stubbed or dead, which requirements have no test, which cross-cutting invariants the code does not uphold, and what is unclear. Nothing here changed any code, test, config or data.

**Note on README edits.** At the requester's explicit request the parent session edited `README.md` after Phase 0 (the original brief said not to). Section (a) marks each discrepancy `[fixed in README]` or `[open]` against the README as it stands now. No code, test, config or data file was changed.

## Method and limits

- Specs were written from the code at HEAD `ca450dd`, one agent per capability, each reading its modules and tests. README was a secondary source; where it disagreed with code the code won.
- `pytest` was **not** run (the suite is network-guarded in `tests/conftest.py`, but the whole suite was not audited). Test names in the specs were checked mechanically: every `path::test` reference resolves to an existing file and `def` (1,290 references, 0 unresolved at the time of writing).
- File:line evidence in this document: lines I re-read while writing it are cited as-is; lines taken from agent notes are marked "per agent note" or carry `~`. Agent claims were spot-checked, not all re-verified. A claim marked UNCONFIRMED below was not traced to a conclusion.
- Counts below are recomputed from the spec files: 27 capabilities, 826 requirements, 1528 scenarios, 250 requirements marked `tests: NONE` (30%).
- Scenarios use placeholders; no emails, keys, account numbers or real amounts appear in the specs or here.

## (a) README-vs-code discrepancies

44 items: 24 `[fixed in README]`, 20 `[open]`. `[open]` means the README (or a code comment) still disagrees with the code, or the README is silent about a built behavior.

| ID | Capability | Claim | Evidence | Status |
|---|---|---|---|---|
| D1 | transaction-ledger | README said the system has no knowledge of cost basis or holding periods. | Ledger replay computes SHORT/LONG (LONG = more than 365 days): `tools/ledger_engine.py:27`, `:237`. | [fixed in README] |
| D2 | llm-model-routing | README batch failover chain was Gemini → Groq → OpenRouter → GPT-4o-mini → Claude-Haiku → GPT-4o → Claude-Sonnet. | `FAILOVER_CHAINS` (`_models.py:176`): triage/flash/fast start with Groq GPT-OSS-120B and end at GPT-4o-mini and GPT-4o; no Gemini, Haiku or Sonnet in any batch tier; `reasoning` starts with Claude-Sonnet. | [fixed in README] |
| D3 | llm-model-routing | README env table said Gemini is first in the failover chain. | No chain contains a Gemini model; `GEMINI_COUNTER` is referenced only by `pipeline/runner.py:15` and `pipeline/daily/research.py:11` (`_models.py:171`). | [fixed in README] |
| D4 | llm-model-routing | README said keys for unused providers can be omitted and those models are skipped. | Chain members are not skipped for a missing key; a missing key only fails over through the error path (agent note on `_models.py`). | [fixed in README] |
| D5 | apex-prediction-engine | Commits a90bb47 and a3fcbdf are titled "removal of claude", yet Claude is still the APEX champion default. | `DEFAULT_CHAMPION = "Claude-Sonnet"` (`tools/apex_dual_run.py:78`); the `reasoning` chain starts with Sonnet (`_models.py`). Only the batch tiers dropped Claude. README agrees with the code here, so no README text was wrong. | [open] |
| D6 | apex-prediction-engine | README said a model out of credit is skipped for the rest of the run. | Dead-model marks last `DEAD_MODEL_TTL_SECONDS = 600.0` (`apex_dual_run.py:230`) and also cover auth and subscription-tier errors. | [fixed in README] |
| D7 | apex-prediction-engine | README listed six APEX event types and seven model labels. | Code also emits `apex_skip_dead`, `apex_timeout` (`apex_dual_run.py`) and `apex_skip` (`pipeline/daily/apex.py`), and registers `OR-Grok-4.3` and `OR-Qwen3.7-Max`. | [fixed in README] |
| D8 | pipeline-orchestration | README presented batch times (06:30, 11:00/13:00/15:00, 17:30) as if enforced; its cron example used 17:00. | `config.py` defines the times but nothing reads them; real timing is external cron. | [fixed in README] |
| D9 | pipeline-orchestration | README said watchlist tickers are promoted to portfolio treatment on an event. | Morning event path and intraday Track A run APEX for portfolio tickers only (`pipeline/batch/morning.py`, `intraday.py`); `watchlist_severity_threshold` in `config.py` is never read. | [fixed in README] |
| D10 | pipeline-orchestration | README pipeline phases listed only five phases and omitted later morning and evening layers. | Morning also runs valuation, risk/technical, universe screen, score snapshot and price backfill; evening adds fund refresh, provisional marks, health snapshots and notification delivery (`pipeline/batch/morning.py`, `evening.py`). | [fixed in README] |
| D11 | pipeline-orchestration | `run_daily.sh` banner lists Fundamentals, Research, News as the phases; batch order is News, Short interest, Research, Fundamentals, APEX, then five more. | `run_daily.sh` header vs `pipeline/batch/morning.py`. Code-side text, not README. | [open] |
| D12 | pipeline-orchestration | README said 5d and 21d run every Monday. | `events/scheduler.py` (~`:86-92`) checks `weekly_monday` and `today.weekday() == 0` after the trading-day gate, so a holiday Monday skips that week. | [fixed in README] |
| D13 | prediction-composition-and-buckets | README said 5d runs every Monday; the legacy `get_scheduled_horizons` runs 5d every trading day and has no 250d. | `tools/prediction_db.py:632-650` (per agent note; seed cadence in `engine_settings_db.py` matches the README). Reached only by the legacy `--force-all` path. | [fixed in README] |
| D14 | prediction-composition-and-buckets | README presented the composite → label table as system behavior. | It is a prompt instruction (`specialists/reasoning.py:176-181`, per agent note); no code computes or enforces it. `decision_composer.py` is not a scorer. | [fixed in README] |
| D15 | prediction-composition-and-buckets | README says the conviction guardrail caps conviction; code also caps `composite_score` for the horizon where it fired. | `pipeline/daily/apex.py:386-390`; `tests/test_review_findings.py:216` (per agent note). | [open] |
| D16 | prediction-validation | README Validation methodology used fixed buckets (±1% / ±5%). | Scoring uses versioned per-horizon edges (`tools/bucket_edges.py`, `validation_engine.py:38-58` holds only the 5d v1 legacy constants). README's own bucket section already said so. | [fixed in README] |
| D17 | prediction-validation | README said Layer 3 is LLM-assisted pattern analysis and that role/instrument breakdowns are stored nightly. | `weekly_pattern_analysis` (`validation_engine.py:881`) is statistical segment mining and the model only narrates (`:955`); role and instrument-type breakdowns are computed on read. | [fixed in README] |
| D18 | prediction-validation | README says Brier/accuracy are "the only objective measure ... check them before acting". | No match for that text in `6_Validation.py` or `11_Validation_QA.py` (agent grep). README-only guidance, not a UI behavior. | [open] |
| D19 | news-ingestion | README said news comes from RSS + Finnhub into `news_daily`, scored with Haiku/Sonnet. | Per ticker: Finnhub primary, Yahoo fallback (`tools/finnhub_news.py:194-211`, `tools/news_filter.py:241-256`); RSS is market-level (`news_sources.py`); table is `news_daily_update` (`tools/news_db.py:69`); scoring uses the `triage` chain. Docstrings in `news_filter.py` and `pipeline/daily/news.py` still say Haiku/Sonnet. | [fixed in README] |
| D20 | fundamentals-and-filings | README said guidance text is capped at 2,500 characters; accession is the latest 8-K; EDGAR_USER_AGENT is optional; a raise adds 1–2 points. | Extraction caps at 3,000 (`tools/edgar.py:200`) and the prompt block truncates to 2,500 (`pipeline/daily/fundamentals.py:62`); only form exactly `8-K` is used; a placeholder contact fills the header; the score rule is a prompt instruction only. | [fixed in README] |
| D21 | fundamentals-and-filings | Stored `model_name` is always the first `flash` chain entry because persistence passes `model_idx=0`; the filing check includes 10-Qs while XBRL figures use only 10-Ks. | `pipeline/daily/fundamentals.py:165` (initial `model_idx = 0`); the rest per agent note. Not re-traced end to end. | [open] |
| D22 | research-and-short-interest | README said the yfinance fallback feeds the Research prompt, one paginated FINRA call, and "business days" for the lag. | Prompt line uses stored FINRA rows only (`pipeline/daily/research.py:24-36`); FINRA is one logical download in several pages; calendar counts weekdays only (`tools/finra.py:47-60`). | [fixed in README] |
| D23 | research-and-short-interest | `specialists/research.py:34` still says all data is from Yahoo Finance with no key. | Broker data now also includes Finnhub (`tools/broker_research.py:70`). Code-side text. | [open] |
| D24 | macro-data | README says a FRED key is "free API key required". | Without a key the macro modules degrade (`macro/fred_client.py`) and do not fail. Regime label strings also differ between snapshot (`RISK_ON`) and specialist (`RISK-ON`). | [open] |
| D25 | portfolio-health | README said health notifications use the structured-log hook and omitted account-level keys and the unvested rule. | Delivery goes through `notification_service.notify` into the outbox (`services/health_service.py:62-94`); account key `health:account:<id>:<check>:<date>` (`:81`); `health_exclude_unvested` default 1. | [fixed in README] |
| D26 | portfolio-health | README omits `health_stale_fund_data_multiple` (2.0), `statement_price_max_age_days` (100) and the compliance check merging ledger clearance into the restricted-list check. | `tools/investor_settings_db.py:69` area, `tools/portfolio_health.py` `_compliance` (per agent note). | [open] |
| D27 | notifications | README said the admin settings editor comes in a later step, the hourly-cap overflow folds into the next digest, and the blocked note appears "if clearance says so". | The admin editor exists; overflow reaches a person only if their digest is on (off by default); the note follows the clearance decision (`events/detector.py` ~`:66-70`, per agent note). | [fixed in README] |
| D28 | transaction-ledger | README said broker-specific CSV parsers are not built and omitted `OPENING_BALANCE` and saved activity history. | Fidelity and Vanguard presets exist (`tools/ledger_csv_import.py:128-189`, `:398-471`); `OPENING_BALANCE` at `tools/ledger_db.py:21`; `account_history` at `ledger_db.py:95-114`. | [fixed in README] |
| D29 | accounts-and-holdings | README called `holdings_source` and `is_paper` "reserved for later work". | Both are live (`services/account_service.py:73-83`; paper exclusion in `tools/holdings_db.py`). | [fixed in README] |
| D30 | accounts-and-holdings | README "Configure your portfolio and watchlist" tells the reader to edit `config/portfolio.yaml` and `config/watchlist.yaml`. | `config.py:1-9` says holdings moved to the database; the watchlist YAML is migrated once and renamed `.migrated` (`tools/watchlist_db.py`). This README section was not edited. | [open] |
| D31 | instrument-taxonomy-and-routing | README taxonomy list omitted `STABLE_VALUE`; leverage bounds not stated. | `domain.py:121` defines `STABLE_VALUE`; `services/account_service.py:578-581` bounds |leverage| ≤ 5 and non-zero. | [fixed in README] |
| D32 | investor-profile | README said nothing reads goals and that every operation is one transaction with an audit event. | `privacy.py:62-64` reads goal names; `set_limits_source` (`services/investor_service.py:218-221`) updates the profile and writes the audit in a separate transaction. | [fixed in README] |
| D33 | identity-and-access | README says `--owner` must be `LOCAL_OWNER`; code does not validate it. Several multi-user docstrings are stale. | `tools/auth_users_db.py:18-22`; `services/context.py:31-35`, `services/private_cache.py:14-15`, `tests/test_trusted_context_scoping.py:72-131`. | [open] |
| D34 | compliance-controls | README never says approvals and blackout windows have no UI or CLI. | `record_approval`, `revoke_approval` and blackout-window writers have no non-test callers (agent grep of `web/`, `scripts/`, `main.py`). | [open] |
| D35 | fund-data-and-lookthrough | README: N-PORT `holding_is_fund` is replaced by the classifier "whenever it has a symbol"; README: "nothing is sent to external servers except the API calls listed". | Replacement happens only for fund-of-funds types at depth < max (`services/fund_data_service.py` ~`:110-117`); held tickers from every owner are sent to Yahoo, SEC, issuer sites and OpenFIGI (`:187-193`, tickers/CUSIPs only). | [open] |
| D36 | market-prices | README says "No options" and delayed prices; omits statement-price freshness. | `tools/yfinance_tools.py` still exposes `get_options_data`; `statement_price_max_age_days` is documented only under Health; the 15–20 minute delay is a provider property, not enforced in code. | [open] |
| D37 | web-dashboard | README describes no dashboard queue rules; the home health card links to a page that no longer exists. | `web/components/portfolio_health.py:190` links `/Portfolio_Health`; the Profile "Prediction Horizons" setting (`web/pages/12_👤_Profile.py:238`) has no consumer. | [open] |
| D38 | legacy-analyst-agents | README says technical flags are rule-based with no LLM; the single-ticker CLI path still builds an LLM `technical_agent`. | Daily path: `pipeline/daily/risk_technical.py:398` `_rule_based_technical`; CLI path: `agent.py` (per agent note). | [open] |
| D39 | portfolio-risk-and-technical-flags | README had no section on risk/technical flags; `tools/technical_features.py:1-12` still describes an LLM Technical specialist. | A short paragraph was added to README; the stale docstring remains. | [fixed in README] |
| D40 | trade-plans-and-simulation | README never mentions trade plans, plan simulation or the cap guardrail. | No README section exists (agent grep). | [open] |
| D41 | long-horizon-and-calibration | README never mentions scoring snapshots or calibration. | Calibration uses 30/60/90/250 calendar days (`tools/scoring_snapshot_db.py` `HORIZONS`), long-horizon validation uses 63/250 trading days. | [open] |
| D42 | portfolio-optimization-and-scenarios | README never mentions the scenario engine or `recommend_allocation`. | No README section exists. | [open] |
| D43 | opportunities-screening-valuation | README never describes the screener, Opportunity Engine, universe sources or valuation engine. | No README section; universe sources are in `tools/universe.py:23-31`. | [open] |
| D44 | admin-and-engine-settings | README calls `bucket_edges_history` append-only; the table has no triggers. | Append-only status rests on `INSERT OR IGNORE` alone (`tools/engine_settings_db.py`, per agent note). | [open] |

### Cross-cutting invariant violations

Reported, **not fixed**. They fall into two root causes (V1, V2) plus seven smaller items. The specs record an invariant only where code or tests uphold it; the gaps are recorded here and in the notes. The `chat-portfolio-assistant` change names the same root causes as prerequisite fixes for its own paths.

**V1. Unscoped holdings read: `portfolio_tools._load_portfolio(owner=None)` returns every owner's holdings**

- What and where: `tools/portfolio_tools.py:24-28` calls `get_holdings()` with no portfolio filter when owner is None (`holdings_db.py` documents this as correct only for system-wide batch jobs). A test pins the legacy behavior: `tests/test_portfolio_tools_owner_scoping.py::test_get_portfolio_holdings_with_no_tool_context_keeps_the_legacy_unscoped_read`.
- Call sites / extent: Call sites that pass no owner or holdings: `tools/opportunity_engine.py:222-230` (`_load_conviction_and_holdings`); `tools/portfolio_optimizer.py:124-126` (`get_daily_opportunities(top_n=15)`) and `:323-326` (`holdings=None` path); `pipeline/daily/scoring_snapshot.py:74-100` (fit score from all owners' holdings stored in the shared `scoring_snapshots` table, no owner column); `web/pages/9_🎯_Opportunity_Engine.py:93` (cached by `top_n` only); `web/app.py:270-282`; `web/components/valuation_detail.py:57-58`; `web/pages/5_💼_Portfolio.py:679-682` (passes `owner` and `holdings` but not `opportunity_holdings` or `prediction_scopes`, so candidates are judged against all owners' holdings); `specialists/risk.py` tools with no tool-context owner. Chat and scenarios pass the owner's own book.
- Severity: High
- Recorded in spec(s): identity-and-access, opportunities-screening-valuation, portfolio-optimization-and-scenarios, long-horizon-and-calibration, web-dashboard
- Status: not fixed.

**V2. `DISPLAY_SCOPES` default includes private prediction rows with no owner filter**

- What and where: `tools/prediction_db.py:69` defines `DISPLAY_SCOPES = (shared, legacy, private)`; `get_latest_prediction` (`:903`) and `get_all_latest_predictions` (`:1126`) default to it. The constant's comment reads "what the local user may see of their own data", written for a single-user database.
- Call sites / extent: Defaulting callers: `tools/portfolio_risk.py:330`, `:461` (aggregate metrics and hypothetical addition, so an owner's risk figures can use another owner's private rows); `scenarios/prepare.py:97` (falls back to the default unless `prediction_scopes` is given; the Portfolio page does not give it); `web/chat/rendering.py:99` (chat is excluded from the baseline); Today dashboard queries in `web/app.py:129-194` (`hpreds`, `rec_changes`, `wl_opps` filter only `not_challenger_sql`, no scope or owner filter) and `web/data/attention.py:55-70` (same); the trending-opportunity cache `web/app.py:270` keyed by `top_n` only. Pipeline callers (`pipeline/daily/apex.py`, `decision_composer.py`) pass `SHARED_SCOPES` explicitly and are fine.
- Severity: High
- Recorded in spec(s): prediction-composition-and-buckets, portfolio-risk-and-technical-flags, portfolio-optimization-and-scenarios, web-dashboard
- Status: not fixed.

**V3. `account_service.list_import_runs_for` returns every owner's import runs**

- What and where: `services/account_service.py:749-751` authorizes the actor, then calls `repo.get_import_runs(limit)`; `import_runs` has no portfolio column (`tools/holdings_db.py`, per agent note). Any verified actor receives all owners' import receipts (broker, labels, file names).
- Call sites / extent: Single call site in the service layer; callers not enumerated.
- Severity: High
- Recorded in spec(s): accounts-and-holdings
- Status: not fixed.

**V4. Watchlist reads and writes are not owner-scoped**

- What and where: `tools/watchlist_db.py:122-148`: `add_tickers` always stamps the local owner, `remove_tickers` deletes by ticker for any owner, and the 70-ticker cap counts every owner's rows; the primary key is `ticker`. `tests/test_validation_page_data.py` documents the unscoped write path.
- Call sites / extent: Unscoped reads via `load_watchlist_tickers()` in `web/pages/9_🎯_Opportunity_Engine.py:103`, `web/app.py:219`, `web/components/valuation_detail.py:67`, `web/pages/5_💼_Portfolio.py:869`; the scoped `load_watchlist_tickers_for(ctx)` is used only by the validation pages and the Predictions page.
- Severity: Medium
- Recorded in spec(s): opportunities-screening-valuation, web-dashboard, identity-and-access
- Status: not fixed.

**V5. Legacy blackout dates become one `global` window applying to every owner**

- What and where: `clearance.py:339` calls `migrate_legacy_profile_blackout` with the evaluating owner's profile dates; `tools/blackout_windows_db.py:117-141` inserts one `global` row once system-wide (gated on a marker row). A second owner's legacy dates are dropped. A test asserts the global window (`tests/test_clearance_gate.py`, ~`:519-521`), so this may be intended.
- Call sites / extent: One migration path; the effect is on every later `evaluate_clearance` call.
- Severity: Medium
- Recorded in spec(s): compliance-controls
- Status: not fixed.

**V6. `scenarios/prepare.py` reads accounts and cash by id without an ownership check**

- What and where: `scenarios/prepare.py:62-73` lists all accounts and cash balances; `scope="account:<id>"` adds an id parsed from the scope string without verifying the account belongs to the owner, so a scope naming another owner's account would pick up its label and cash. No test.
- Call sites / extent: `recommend_allocation` callers; normal use passes the owner's own rows.
- Severity: Medium
- Recorded in spec(s): portfolio-optimization-and-scenarios
- Status: not fixed.

**V7. Admin Database page shows all owners' holdings and challenger rows**

- What and where: `web/pages/1_📊_Database.py` reads `holdings` and `predictions` for all owners without `not_challenger_sql` (agent note). The page is admin-only, so this may be intended, but challenger rows are meant never to be shown to users.
- Call sites / extent: One page.
- Severity: Low (intent decision)
- Recorded in spec(s): web-dashboard
- Status: not fixed.

**V8. Fund-data, look-through, identifier-mapping and health helpers take a plain owner string**

- What and where: `load_fund_views(owner, ...)`, `views_version(owner, ...)`, `list_manual_breakdowns(owner)`, `get_effective_policy_for_owner(owner)` (`tools/portfolio_policy.py:169`), `get_health_view_for_owner(owner)` and `list_restricted_by_owner(owner)` (`tools/restricted_list_db.py:235-250`) trust the caller to pass the verified actor. Callers found pass a trusted context path; none were audited exhaustively.
- Call sites / extent: Design point, not a confirmed violation.
- Severity: Low
- Recorded in spec(s): fund-data-and-lookthrough, investor-profile, portfolio-health, compliance-controls
- Status: not fixed.

**V9. System-context cross-owner reads (price refresh, held-fund tickers, shared ticker universe)**

- What and where: `refresh_holding_prices` and `snapshot_portfolio_prices` read all owners' holdings under a system context and write prices only; `held_fund_tickers()` (`services/fund_data_service.py:~187-193`) and `pipeline/watchlist.py` `get_portfolio_tickers()` union every owner's tickers, which are then sent to Yahoo, Finnhub, SEC, issuer sites and OpenFIGI. Tickers and CUSIPs only, no owner ids or amounts.
- Call sites / extent: Deliberate shared-market-data design; README's statement that nothing is sent beyond listed APIs is incomplete (see D35).
- Severity: Low (disclosure design point)
- Recorded in spec(s): market-prices, fund-data-and-lookthrough, research-and-short-interest, news-ingestion
- Status: not fixed.

Wording risk in the specs: `identity-and-access` states that "a missing owner SHALL NOT fall back to all portfolios" and every private read and write is scoped to the verified actor. Its scenarios and tests cover the account-service layer, but V1 and V2 show other layers that do not meet that wording. Consider adding a known-gap note to that requirement, or narrowing it to the service layer, before archive.

## (b) Stubs and not-built items

These are **not** requirements in any spec.

| Item | Detail | Capability |
|---|---|---|
| Event detectors | `detect_earnings`, `detect_sec_8k`, `detect_rating_changes`, `detect_macro_events` return `[]` (`events/detector.py:182-207`). Only `detect_material_news` is real. README states this correctly. | pipeline-orchestration |
| Notification channels | `WhatsAppSender` and `SmsSender` return "skipped: channel not implemented" (`services/notification_senders.py:297`, `:318`); `SLACK` is a legacy read-only value. One-click POST unsubscribe is not possible in Streamlit. | notifications |
| Ledger | IRR, paper-trading UI, wash-sale adjustment, choosing SPECIFIC lots in the form, broker sync are TODO hooks (`web/data/ledger.py:8-10`, `web/components/ledger_panel.py:8`); average-cost method and cash in lieu not modelled (`tools/ledger_engine.py:19`); SPLIT is not CSV-importable by design. | transaction-ledger |
| Compliance UI | No UI or CLI records or revokes approvals or manages blackout windows; the holding-period check is permanently UNKNOWN; `decision_composer._generate_private_explanation` raises `NotImplementedError` (`tools/decision_composer.py:283`). | compliance-controls, prediction-composition-and-buckets |
| Run planner | `run_generation_unit`, `plan_batch`, `plan_ticker_horizon` are called by no batch; only `plan_reuse` is used (`tools/decision_composer.py`). `generation_jobs_db` is used in production only by morning recovery; `generation_jobs` rows are never pruned. | pipeline-orchestration |
| Scenarios | No UI or CLI saves, re-evaluates or compares scenarios; FX conversion is recorded but not applied. | portfolio-optimization-and-scenarios |
| Country weights | The fund provider interface has `country_weights` but no provider populates them. | fund-data-and-lookthrough |
| Goals | Goals feed nothing (retirement planning TODO, `tools/investor_profile_db.py:14`). | investor-profile |
| Trade plans | Plans cover only UP trades; no 250d plans by design. | trade-plans-and-simulation |
| Preferred horizons | Profile "Prediction Horizons" stores `preferred_horizons`; `weight_engine.filter_preferred_horizons` has no caller. | web-dashboard, prediction-composition-and-buckets |

### Dead or unreachable code

| Code | Why dead |
|---|---|
| `run_analysis_with_failover` (`pipeline/runner.py:169`), `GEMINI_COUNTER` (`_models.py:171`) and the `google` branch in `runner.py` | no callers / no chain with provider google |
| `pipeline/daily/__init__.py` `run_daily` | unreachable from the CLI (`--daily` routes to the morning batch, `main.py`) |
| `needs_research_refresh` (`tools/broker_research.py:146`), `batch_check_research` (`:184`), `upsert_short_interest` | no callers outside their module |
| `macro_calendar` table, `consensus_value` columns | created or declared, never written |
| `events/db.py` `mark_event_processed`, `mark_event_checked` | no callers; `trigger_events.processed` is always 0 |
| `should_notify`, `should_notify_trigger` (`events/scheduler.py:156-176`) | no production caller |
| `research.raw_fetched_at` | legacy, no longer written |
| The "access has been disabled" branch in `web/auth.py:340-343` | unreachable; revocation takes effect after the 300 s recheck |
| Legacy decision values CLEARED / SKIPPED / INSUFFICIENT_DATA | may exist in old rows, never written now |

## (c) Requirements with no test coverage, ranked by risk

250 of 826 requirements are marked `tests: NONE`. Ranking: (1) privacy and owner scoping, (2) money, (3) compliance, admin and disclosure, (4) the rest. Names below are the requirement headers in the spec files.

### Tier 1: privacy, owner scoping, access

| Capability | Requirement |
|---|---|
| compliance-controls | Pipeline skip list is shared admin configuration |
| compliance-controls | Restricted List page edits only the signed-in member's list |
| fundamentals-and-filings | Fundamentals are shared and deterministic where possible |
| identity-and-access | Allow-list status is re-checked periodically |
| identity-and-access | Sign-in must return expected identity claims from the expected issuer |
| identity-and-access | Sign-out clears the session |
| identity-and-access | User management screens are shown only to admins |
| legacy-analyst-agents | Clearance owner identity in the pipeline |
| market-prices | Market price data carries no private content |
| market-prices | Shared price series are best-effort and carry no private data |
| news-ingestion | Restricted skip list applies to the news phase |
| portfolio-optimization-and-scenarios | Restricted list applies only for a known owner |
| portfolio-optimization-and-scenarios | Restricted-list check is the caller's own |
| portfolio-risk-and-technical-flags | Active flags are read per owner from the latest date |
| portfolio-risk-and-technical-flags | Active risk flags feed each owner's attention queue |
| portfolio-risk-and-technical-flags | Flags never enter predictions |
| portfolio-risk-and-technical-flags | Legacy flag rows are migrated once to the owner-keyed table |
| prediction-validation | Rolling metrics exclude challengers and private rows |
| research-and-short-interest | Research is shared market data |
| trade-plans-and-simulation | Plan validation reads shared forecasts in validation roles |
| web-dashboard | Dashboard forecasts exclude challenger rows |
| web-dashboard | Predictions page never shows challenger rows |
| web-dashboard | Predictions page scope and filters |
| web-dashboard | Today dashboard is scoped to the signed-in owner's holdings |

### Tier 2: money (ledger, valuation, health reserve, scenarios, performance)

| Capability | Requirement |
|---|---|
| long-horizon-and-calibration | Final score blend |
| opportunities-screening-valuation | DCF projection |
| opportunities-screening-valuation | Screener outcome evaluation |
| opportunities-screening-valuation | Valuation is deterministic |
| opportunities-screening-valuation | Valuation screen |
| opportunities-screening-valuation | Valuation storage |
| portfolio-health | Unvested employer money is excluded by default |
| portfolio-optimization-and-scenarios | A sell by amount larger than the position sells the position |
| portfolio-optimization-and-scenarios | A trade states exactly one of quantity or amount |
| portfolio-optimization-and-scenarios | Allocation entry point returns evaluated trades |
| portfolio-optimization-and-scenarios | Concentration flag |
| portfolio-optimization-and-scenarios | Funding account for a contribution |
| portfolio-optimization-and-scenarios | No language model in scenarios |
| portfolio-optimization-and-scenarios | Preparation pins inputs and versions |
| portfolio-optimization-and-scenarios | Price pinning order |
| portfolio-optimization-and-scenarios | Risk impact is a best-effort market estimate |
| portfolio-optimization-and-scenarios | Scope needs at least one account |
| portfolio-optimization-and-scenarios | Score floor for new cash |
| portfolio-optimization-and-scenarios | Sector resolution order |
| portfolio-optimization-and-scenarios | Starting cash and overrides |
| web-dashboard | Allocation can be grouped by asset class or instrument type |
| web-dashboard | Command strip states what its numbers cover |
| web-dashboard | Critical tier of the attention queue |
| web-dashboard | Opportunity and discovery tiers of the attention queue |
| web-dashboard | Performance states its limits |
| web-dashboard | Performance windows and security-type filter |
| web-dashboard | Queue summary separates decisions from information |
| web-dashboard | Recent activity and yearly summary |
| web-dashboard | Watch tier of the attention queue |

### Tier 3: compliance, order-execution guarantee, admin, disclaimers

| Capability | Requirement |
|---|---|
| admin-and-engine-settings | Admin form errors are shown, not raised |
| admin-and-engine-settings | Command-line model changes are validated but not admin-gated |
| admin-and-engine-settings | Quiz version bumps on change |
| admin-and-engine-settings | Weight and guardrail settings |
| apex-prediction-engine | No order execution |
| compliance-controls | Approval lookup failure is pending, not allowed |
| compliance-controls | Compliance records are information, never order execution |
| identity-and-access | No order execution or broker connectivity |
| investor-profile | Profile page states it is not investment advice |
| portfolio-optimization-and-scenarios | Research only, no execution |
| prediction-validation | Validation computes no order or advice output |
| trade-plans-and-simulation | Plans are research output and never execute trades |
| web-dashboard | Database viewer is admin-only and read-only |
| web-dashboard | Disclaimer acceptance is stamped once |
| web-dashboard | Navigation structure and admin-only menu entries |
| web-dashboard | Profile form validation |
| web-dashboard | Profile limits are read-only when they come from the investor profile |
| web-dashboard | Schedule page is admin-only and launches runs as background jobs |

### Tier 4: everything else (counts; names are in the spec files)

| Capability | Untested requirements | Of total |
|---|---|---|
| apex-prediction-engine | 5 | 33 |
| fund-data-and-lookthrough | 1 | 38 |
| fundamentals-and-filings | 20 | 22 |
| identity-and-access | 1 | 30 |
| legacy-analyst-agents | 9 | 12 |
| llm-model-routing | 12 | 16 |
| long-horizon-and-calibration | 3 | 13 |
| macro-data | 19 | 25 |
| market-prices | 5 | 19 |
| news-ingestion | 19 | 22 |
| opportunities-screening-valuation | 15 | 37 |
| pipeline-orchestration | 19 | 34 |
| portfolio-risk-and-technical-flags | 10 | 37 |
| prediction-composition-and-buckets | 6 | 37 |
| prediction-validation | 8 | 33 |
| research-and-short-interest | 17 | 18 |
| web-dashboard | 10 | 41 |

Tier counts: tier 1 = 24, tier 2 = 29, tier 3 = 18, tier 4 = 179; total 250 (matches 250).

Observations: `research-and-short-interest` is untested end to end (no test references FINRA, short interest, broker research or `research_db`); `fundamentals-and-filings`, `news-ingestion`, `macro-data` and `pipeline-orchestration` are mostly untested because the batch code has few direct tests; `no order execution` is asserted in several specs but has no test, only a code-read scan that found no broker or order-placement code.

## (d) Open questions (intent unclear)

- Should held (portfolio) and extra tickers bypass news scoring? They carry a NULL score and `detect_material_news` requires a non-null `stage_3_score` (`pipeline/daily/news.py:227`, `events/detector.py:138-142`), so a held ticker can never raise a material-news event. The README implies it can.
- Is the global effect of the legacy blackout migration (V5) intended in a multi-owner deployment? Who may grant approvals, given `trade_approvals_db` has no authorization and nothing in the app calls it?
- Should the admin Database page (V7) filter challenger rows and other owners' holdings?
- Is `DISPLAY_SCOPES` (V2) meant to be "shared plus the viewer's own" rather than all private rows? Is the all-owner fallback in `portfolio_tools` (V1) meant for batch jobs only?
- UNCONFIRMED: `run_daily.sh` does not set `PIPELINE_RUN_ID` (only `run_validate.sh:10` and the Schedule page, `2_🗓️_Schedule.py:165`, do), and the morning batch creates its progress tracker only when it is set (`pipeline/batch/morning.py:122`, `:133`). The agent concluded a cron-launched morning run records no `pipeline_runs` row, so intraday and evening would re-run morning. `pipeline/daily/__init__.py:33` falls back to a timestamp run id, and `has_completed_today` was not traced; confirm against how the morning job is really scheduled.
- Should `weekly_monday` on a holiday Monday move to the next trading day? Code skips the week; README now says so, but intent is unclear.
- Should `portfolio-health` per-ticker assessment live under another capability? Should `set_limits_source` write its audit in the same transaction?
- Should weights, guardrails and APEX model settings get an admin-gated, audited service path (`engine_settings_db.update_engine_settings` and `scripts/apex_models.py` are ungated and unaudited)? Should one `engine_settings.version` per group replace the single version that makes other groups' forms stale?
- Should `invite_user`, `approve_request` and other user-management calls enforce admin in the service layer as investor settings do, or is page gating the contract?
- Should `model_name` in fundamentals reflect the answering model rather than the first chain entry? Should BOND skip EDGAR? Should 10-Q figures feed the analysis?
- Layer 3 validation is statistical with model narration (not a stub); is the dashed aggregate on dashboards meant to include legacy random-model rows?
- Is the credit-exhausted 600 s dead mark acceptable when an auth blip removes a model for ten minutes? Is the challenger sample percentage meant to compare the champion label or the drawn pick in pool mode?
- Should hourly-cap overflow fold into a digest that is off by default? Should multi-recipient sends be per-recipient rows so a retry cannot duplicate (notifications)?
- Is the placeholder EDGAR User-Agent fallback acceptable under SEC fair-access guidance?
- `universe_db.get_peers` subtracts `market_cap_category` values that may be strings from the Nasdaq API (agent note, not verified); `macro/releases.py:18` resolves `data/portfolio.db` one directory above the repo (defect, see (e)); failed FRED fetches are cached for an hour.
- Is the Profile "Prediction Horizons" setting meant to filter generation (which would break the shared-forecast boundary) or be removed? Should Schedule and Validation QA be hidden from members in the navigation?
- Should the single-ticker CLI pipeline (`main.py TICKER`) and its LLM technical agent be retired now that APEX and chat cover the same ground?

## (e) Suggested follow-up changes (proposal titles only)

- require-owner-for-holdings-reads (V1: portfolio_tools, opportunity_engine, portfolio_optimizer, scoring snapshot, valuation screen, Portfolio page)
- replace-display-scopes-default-with-owner-aware-scopes (V2: prediction_db, portfolio_risk, scenarios, Today dashboard, attention queue)
- scope-import-run-listing-to-the-owner (V3)
- make-watchlist-owner-scoped (V4: composite key, scoped add/remove/cap/reads)
- decide-and-document-legacy-blackout-ownership (V5)
- verify-account-ownership-in-scenario-scope (V6)
- decide-admin-database-page-visibility (V7)
- key-trending-opportunity-and-opportunity-page-caches-by-owner
- audit-owner-string-helpers-for-verified-actor-use (V8)
- add-server-side-admin-checks-to-user-and-access-request-management
- add-admin-gate-and-audit-for-weights-guardrails-and-apex-settings
- add-append-only-triggers-to-bucket-edges-history-and-plan-tables
- fix-macro-releases-database-path
- confirm-and-fix-morning-run-recording-for-cron-launches
- unify-batch-schedule-times-across-readme-cron-wrapper-and-config
- stamp-the-answering-model-name-in-fundamentals
- decide-held-ticker-news-scoring-and-material-news-events
- cache-failed-fred-fetches-for-less-than-an-hour
- per-recipient-notification-rows-to-avoid-duplicate-retries
- add-tests-for-research-and-short-interest
- add-tests-for-fundamentals-news-and-macro-ingestion
- add-tests-for-web-login-gate-recheck-and-signout
- add-tests-for-opportunity-scoring-screener-and-peer-selection
- add-end-to-end-tests-for-scenario-preparation-and-allocation
- add-tests-for-dashboard-owner-scoping-and-challenger-exclusion
- add-readme-sections-for-trade-plans-scenarios-screener-and-calibration
- refresh-stale-multi-user-docstrings-and-llm-technical-comments
- remove-dead-gemini-path-and-unused-run-daily
- fix-dead-portfolio-health-link-and-unwired-preferred-horizons
- retire-or-align-single-ticker-cli-technical-agent

## Coverage of modules

Every `.py` file under `portfolio_agent/`, `web/` and `scripts/` (211 files, excluding `__init__.py`) was mapped by `find portfolio_agent web scripts -name '*.py'`. **Unmapped: none.** `main.py` (CLI flags) and `run_*.sh` map to `pipeline-orchestration`. Mapping is by module group; a file used by several capabilities is listed with its owning capability.

| Module group | Files | Capabilities covering it |
|---|---|---|
| portfolio_agent/ (top level) | 8 | EXCL: infrastructure (1); compliance-controls (1); identity-and-access (1); instrument-taxonomy-and-routing (1); legacy-analyst-agents (2); llm-model-routing (1); pipeline-orchestration (1) |
| portfolio_agent/tools/ | 96 | EXCL: chat (change chat-portfolio-assistant) (1); EXCL: infrastructure (1); accounts-and-holdings (2); accounts-and-holdings (holdings_prices also market-prices) (4); admin-and-engine-settings (1); admin-and-engine-settings (+ investor-profile, portfolio-health keys) (1); apex-prediction-engine (2); compliance-controls (5); fund-data-and-lookthrough (9); fundamentals-and-filings (3); identity-and-access (2); instrument-taxonomy-and-routing (1); investor-profile (4); long-horizon-and-calibration (2); macro-data (2); market-prices (4); news-ingestion (5); notifications (6); opportunities-screening-valuation (7); pipeline-orchestration (4); portfolio-health (3); portfolio-optimization-and-scenarios (1); portfolio-optimization-and-scenarios (holdings read; owner-scoping gap) (1); portfolio-risk-and-technical-flags (3); prediction-composition-and-buckets (5); prediction-validation (+ long-horizon-and-calibration for long-horizon parts) (4); research-and-short-interest (5); trade-plans-and-simulation (5); transaction-ledger (3) |
| portfolio_agent/services/ | 13 | accounts-and-holdings (1); compliance-controls (1); fund-data-and-lookthrough (1); identity-and-access (2); instrument-taxonomy-and-routing (1); investor-profile (1); notifications (4); portfolio-health (1); transaction-ledger (1) |
| portfolio_agent/pipeline/ | 16 | apex-prediction-engine (1); fundamentals-and-filings (1); legacy-analyst-agents (1); legacy-analyst-agents + pipeline-orchestration (1); llm-model-routing (2); llm-model-routing + fundamentals/news (prompt contract) (1); long-horizon-and-calibration (1); news-ingestion (1); opportunities-screening-valuation (2); pipeline-orchestration (3); portfolio-risk-and-technical-flags (1); research-and-short-interest (1) |
| portfolio_agent/specialists/ | 9 | apex-prediction-engine (1); fundamentals-and-filings (1); legacy-analyst-agents (2); macro-data (1); news-ingestion + legacy-analyst-agents (1); portfolio-risk-and-technical-flags (2); research-and-short-interest (1) |
| portfolio_agent/events/ | 3 | pipeline-orchestration (3) |
| portfolio_agent/macro/ | 4 | macro-data (4) |
| portfolio_agent/scenarios/ | 3 | portfolio-optimization-and-scenarios (3) |
| web/ (top level) | 4 | EXCL: infrastructure (2); identity-and-access (1); web-dashboard (1) |
| web/pages/ | 11 | EXCL: chat (change chat-portfolio-assistant) (1); compliance-controls (1); notifications (1); opportunities-screening-valuation (1); prediction-validation (2); web-dashboard (4); web-dashboard (tiles) + admin-and-engine-settings + investor-profile (1) |
| web/components/ | 19 | accounts-and-holdings (2); investor-profile + admin-and-engine-settings (1); notifications (1); opportunities-screening-valuation (1); portfolio-health (1); prediction-validation (3); prediction-validation + long-horizon-and-calibration (1); trade-plans-and-simulation (2); transaction-ledger (1); web-dashboard (6) |
| web/data/ | 9 | notifications (1); prediction-validation (1); transaction-ledger (1); web-dashboard (6) |
| web/chat/ | 7 | EXCL: chat (change chat-portfolio-assistant) (7) |
| scripts/ | 9 | EXCL: deployment secret rendering (not read) (1); EXCL: one-off maintenance (5); apex-prediction-engine (1); notifications (1); opportunities-screening-valuation (1) |

### Intentionally excluded

| File | Reason |
|---|---|
| portfolio_agent/log.py | infrastructure |
| portfolio_agent/tools/chat_db.py | chat (change chat-portfolio-assistant) |
| portfolio_agent/tools/db.py | infrastructure |
| scripts/backfill_flat_threshold_fix.py | one-off maintenance |
| scripts/backfill_instrument_classification.py | one-off maintenance |
| scripts/check_recency_bias.py | one-off maintenance |
| scripts/cleanup_test_debris_2026-10-13.py | one-off maintenance |
| scripts/recompute_outcome_labels.py | one-off maintenance |
| scripts/write_secrets.py | deployment secret rendering (not read) |
| web/chat/engine_tools.py | chat (change chat-portfolio-assistant) |
| web/chat/loop_support.py | chat (change chat-portfolio-assistant) |
| web/chat/orchestration.py | chat (change chat-portfolio-assistant) |
| web/chat/rendering.py | chat (change chat-portfolio-assistant) |
| web/chat/routing.py | chat (change chat-portfolio-assistant) |
| web/chat/tools.py | chat (change chat-portfolio-assistant) |
| web/chat/web_fetch.py | chat (change chat-portfolio-assistant) |
| web/lib.py | infrastructure |
| web/pages/4_🤖_Chat.py | chat (change chat-portfolio-assistant) |
| web/styles.py | infrastructure |

Chat files (`web/chat/*`, `web/pages/4_🤖_Chat.py`, `tools/chat_db.py`) are owned by the in-progress change `chat-portfolio-assistant`. `company_names.py` and `pipeline/rendering.py`, listed as infrastructure in the Phase 0 plan, turned out to carry behavior (a company-name lookup and the CLI output path) and are covered by `news-ingestion` and `legacy-analyst-agents` respectively. `scripts/write_secrets.py` was not opened.

## Cross-cutting invariants

| # | Invariant | Owning capability | Requirement (as named in the spec) | Scenario present | Tests | Status |
|---|---|---|---|---|---|---|
| 1 | No order execution or broker connectivity | identity-and-access (plus six more) | No order execution or broker connectivity; also apex-prediction-engine "No order execution", compliance-controls "Compliance records are information, never order execution", trade-plans-and-simulation "Plans are research output and never execute trades", portfolio-optimization-and-scenarios "Research only, no execution", prediction-validation "Validation computes no order or advice output" | Yes | None (all NONE) | Met in code (no broker or order-placement code found by agent scans); untested |
| 2 | Owner scoping: private reads/writes use the verified actor; a missing owner never falls back to all portfolios | identity-and-access | Private reads and writes are scoped to the verified actor; also accounts-and-holdings, investor-profile, transaction-ledger, portfolio-health, portfolio-risk-and-technical-flags requirements | Yes | tests/test_trusted_context_scoping.py, test_privacy_multi_owner.py, test_portfolio_tools_owner_scoping.py | **Partially met**: holds in the service layer, violated by V1–V4, V6 (see (a)) |
| 3 | Privacy boundary: profile and holdings never reach shared forecasts, the prediction pipeline or prediction rows | identity-and-access, prediction-composition-and-buckets, macro-data, investor-profile | Private terms from every owner are detected before content is shared; Shared forecasts refuse private text; Shared reads exclude private and challenger rows; Market context refuses private text; Flags never enter predictions | Yes | tests/test_investor_profile.py, test_market_context_boundary.py, test_privacy_multi_owner.py; "Flags never enter predictions" is NONE | **Partially met**: forecast path is tested; holdings reach the shared `scoring_snapshots` table (V1) and risk batches send owner-derived numbers to model providers (design point) |
| 4 | Ledger integrity: append-only, VOID corrections, lots and positions derived by replay, reconciliation never writes | transaction-ledger | Ledger is append-only; Corrections are VOID rows; Replay is deterministic and lots are derived; Positions and cash of a ledger account are derived; Reconciliation never writes | Yes | tests/test_ledger*.py (all covered) | Met |
| 5 | Clearance never blocks a recorded trade; records a status and notifies without amounts | transaction-ledger, compliance-controls | Clearance never blocks a recorded trade; Missing clearance notification has no amounts (and the compliance-controls equivalents) | Yes | tests/test_ledger_compliance_import.py, test_clearance_gate.py | Met |
| 6 | Deterministic engines use no LLM | fund-data-and-lookthrough, transaction-ledger, portfolio-optimization-and-scenarios, opportunities-screening-valuation, trade-plans-and-simulation, portfolio-risk-and-technical-flags, portfolio-health, long-horizon-and-calibration, prediction-validation | Look-through is pure and deterministic; Replay is deterministic and lots are derived; No language model in scenarios; Valuation is deterministic; Simulation is deterministic; Technical flags are computed without a model; Health uses no language model | Yes | Mixed; "No language model in scenarios", "Valuation is deterministic", "Long-horizon and calibration computations are deterministic" are NONE | Met by construction; untested for scenarios, valuation and long-horizon. Note: risk flags use a model for narrative while numbers are computed in code; the CLI `technical_agent` is an LLM (D38) |
| 7 | Challenger predictions are never shown to users and are excluded from default validation | apex-prediction-engine, prediction-validation, web-dashboard | Challenger predictions are never shown to users; Roles in default validation; Rolling metrics exclude challengers and private rows; Predictions page never shows challenger rows; Dashboard forecasts exclude challenger rows | Yes | Role rules tested in validation; dashboard and Predictions page requirements are NONE | **Partially met**: V7 (admin Database page shows challenger rows); queries in `web/app.py` apply `not_challenger_sql` but are otherwise unscoped (V2) |
| 8 | Audit: every admin setting change and every service-layer write is audited, and versioned where the code does so | admin-and-engine-settings, accounts-and-holdings, investor-profile, transaction-ledger | Admin changes are audited with the change; Audit operation names; Account and position writes are audited atomically; Every private investor write is audited; Ledger access is owner-scoped and audited | Yes | Tested for investor, account, ledger and admin settings; "Weight and guardrail settings" and "Command-line model changes are validated but not admin-gated" are NONE | **Partially met**: weights, guardrails and APEX model settings have no admin gate or audit; `bucket_edges_history` has no triggers (append-only rests on `INSERT OR IGNORE`); `set_limits_source` audit is not atomic with its write |

All eight invariants appear as a requirement with at least one scenario. Invariants 2, 3, 7 and 8 are only partially met by the code and are recorded as gaps here, not as passing behavior.

## Summary by capability

`Discrepancies` counts section (a) README items for that capability (open + fixed). `Invariant gaps` counts the section (a) violation groups that list the capability.

| Capability | Requirements | Scenarios | Untested | Discrepancies (open / fixed) | Invariant gaps |
|---|---|---|---|---|---|
| accounts-and-holdings | 39 | 60 | 0 | 2 (1 / 1) | 1 |
| admin-and-engine-settings | 34 | 74 | 4 | 1 (1 / 0) | 0 |
| apex-prediction-engine | 33 | 47 | 6 | 3 (1 / 2) | 0 |
| compliance-controls | 36 | 70 | 4 | 1 (1 / 0) | 2 |
| fund-data-and-lookthrough | 38 | 85 | 1 | 1 (1 / 0) | 2 |
| fundamentals-and-filings | 22 | 39 | 21 | 2 (1 / 1) | 0 |
| identity-and-access | 30 | 52 | 6 | 1 (1 / 0) | 2 |
| instrument-taxonomy-and-routing | 19 | 43 | 0 | 1 (0 / 1) | 0 |
| investor-profile | 32 | 60 | 1 | 1 (0 / 1) | 1 |
| legacy-analyst-agents | 12 | 19 | 10 | 1 (1 / 0) | 0 |
| llm-model-routing | 16 | 36 | 12 | 3 (0 / 3) | 0 |
| long-horizon-and-calibration | 13 | 41 | 4 | 1 (1 / 0) | 1 |
| macro-data | 25 | 47 | 19 | 1 (1 / 0) | 0 |
| market-prices | 19 | 40 | 7 | 1 (1 / 0) | 1 |
| news-ingestion | 22 | 42 | 20 | 1 (0 / 1) | 1 |
| notifications | 43 | 78 | 0 | 1 (0 / 1) | 0 |
| opportunities-screening-valuation | 37 | 63 | 20 | 1 (1 / 0) | 2 |
| pipeline-orchestration | 34 | 61 | 19 | 5 (1 / 4) | 0 |
| portfolio-health | 42 | 63 | 1 | 2 (1 / 1) | 1 |
| portfolio-optimization-and-scenarios | 34 | 53 | 16 | 1 (1 / 0) | 3 |
| portfolio-risk-and-technical-flags | 37 | 55 | 14 | 1 (0 / 1) | 1 |
| prediction-composition-and-buckets | 37 | 88 | 6 | 3 (1 / 2) | 1 |
| prediction-validation | 33 | 49 | 10 | 3 (1 / 2) | 0 |
| research-and-short-interest | 18 | 39 | 18 | 2 (1 / 1) | 1 |
| trade-plans-and-simulation | 40 | 84 | 2 | 1 (1 / 0) | 0 |
| transaction-ledger | 40 | 71 | 0 | 2 (0 / 2) | 0 |
| web-dashboard | 41 | 69 | 29 | 1 (1 / 0) | 4 |
| **Total** | **826** | **1528** | **250** | **44 (20 / 24)** | |
