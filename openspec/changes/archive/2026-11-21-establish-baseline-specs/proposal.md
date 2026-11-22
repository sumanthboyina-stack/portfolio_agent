# Proposal

## Why

APEX has no specs: `openspec/specs/` is empty, so every future change is written against the README, which is partly out of date, or against the code directly. This change records the as-built behavior at HEAD `ca450dd` as the baseline that later changes modify.

## What Changes

- Add as-built specs for every capability below. Code is the source of truth and each requirement cites the tests that exercise it, or `tests: NONE`.
- `design.md` is the gap report: README-vs-code discrepancies, stubs and not-built items, untested requirements ranked by risk, open questions, and suggested follow-up change titles.
- No code, test, config, README or data change. Stubs, TODOs and unbuilt features are recorded as gaps, not requirements.

## Capabilities

### New Capabilities
- `identity-and-access`: Entra ID login, allow-list, roles, request context and actor, privacy helpers.
- `accounts-and-holdings`: accounts, positions, snapshot import (CSV, PDF, LLM parsing), manual edits, paper accounts.
- `instrument-taxonomy-and-routing`: instrument types and vocabularies, classification, per-type pipeline routing and horizons.
- `transaction-ledger`: append-only ledger, tax lots, lot methods, switch to or from ledger, reconciliation, CSV import and export.
- `investor-profile`: risk profiles, quiz, emergency reserve, custom allocation, limits source, goals.
- `fund-data-and-lookthrough`: fund data providers and chain, N-PORT, issuer files, identifier mapping, look-through engine.
- `portfolio-health`: health checks, statuses, scopes, snapshots, notifications on ACTION.
- `portfolio-risk-and-technical-flags`: per-owner risk flags and shared technical flags with event-driven cadence.
- `news-ingestion`: news sources, filtering, severity triage, storage.
- `fundamentals-and-filings`: EDGAR XBRL, 8-K guidance extraction, fundamentals storage and routing.
- `research-and-short-interest`: sell-side consensus and FINRA short interest.
- `macro-data`: FRED series, release calendar, macro snapshot.
- `market-prices`: price acquisition, freshness rules, trading calendar.
- `apex-prediction-engine`: champion, fallback, challenger and random-pool model plan, prediction roles, failure handling.
- `prediction-composition-and-buckets`: weight engine, composite and labels, return buckets, prediction storage and evaluation dates.
- `pipeline-orchestration`: batches, event detection and scheduling, cadence, run planning, checkpoints, CLI.
- `llm-model-routing`: failover chains per tier, session pointer, failover error detection.
- `trade-plans-and-simulation`: trade plans, simulation, plan validation.
- `prediction-validation`: outcomes, rolling metrics, roles, scorecards, QA data.
- `long-horizon-and-calibration`: long-horizon benchmark accuracy, provisional marks, calibration, scoring snapshot.
- `opportunities-screening-valuation`: opportunity engine, screener, universe, watchlist, valuation.
- `portfolio-optimization-and-scenarios`: allocation recommendation and what-if scenarios.
- `compliance-controls`: restricted list, blackout windows, trade approvals, clearance, policy decisions.
- `notifications`: channels, contacts, routing, outbox, worker, senders, producers.
- `web-dashboard`: Streamlit pages, attention queue, My Wealth, performance.
- `admin-and-engine-settings`: versioned, audited admin settings and reset to defaults.
- `legacy-analyst-agents`: the ADK specialist agents and synthesis still used by the runner.

### Modified Capabilities
<!-- None: openspec/specs/ is empty. -->

## Out of scope

- Any behavior change, bug fix or refactor.
- Chat (`web/chat/*`, `4_Chat.py`, `chat_db`): owned by the in-progress change `chat-portfolio-assistant`; its specs are not duplicated here.
- Infrastructure with no behavioral contract (logging, DB connection helper, styles, one-off maintenance scripts): listed as intentionally excluded in `design.md`.

## Impact

- Adds files under `openspec/changes/establish-baseline-specs/` only. After review, archive syncs them into `openspec/specs/`.
