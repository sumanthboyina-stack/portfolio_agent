# Proposal

## Why

The shared APEX forecast is market-only by design: it never sees what a person holds, so it cannot say how a forecast relates to their own position. Today a held ticker shows the forecast label, a clearance banner and a weight caption, but never the owner's cost basis or gain, their limits against the forecast direction, or a daily record of how that relation changed. The owner asked for a second view, not a second forecast: an independent market forecast plus a deterministic position view for held tickers, in chat and in the daily pipeline.

## What Changes

- A deterministic, private **position view** for a held ticker: the latest shared forecast labels by horizon with their age, the owner's shares, value, weight, cost basis, unrealized gain and short/long holding period (ledger lots only), their own concentration limits (over / within / unknown), their action permission, and a neutral relation between the forecast direction and the limits, with a one-line summary. No LLM, no network. Cost basis and holding period are facts shown beside the forecast and never change the relation.
- **Chat:** the prediction card is followed by the position view for held tickers (also when a past card is replayed), and a new chat tool answers "how does the forecast look for my X".
- **Morning batch:** a new phase stores each owner's view of every ticker they hold, once per day, in a private owner-scoped table, with one audit event per owner per day.
- The assessment builder gains an owner-string entry point (the context-checked one wraps it) so a batch phase can compute per owner, as the health snapshots do.

## Capabilities

### New Capabilities
- `position-view`: the view, its stored daily record, the chat card and the chat tool.

### Modified Capabilities
- `pipeline-orchestration`: the morning batch runs the position-view phase after the score snapshot (ADDED requirement only).

## Out of scope

- Any recommendation to buy, sell, increase or reduce, and any use of cost basis, gain or holding period to steer the relation.
- A notification when the relation changes, and a dashboard or Portfolio-page display (the stored views make both possible later).
- Changing the market forecast, its prompt, or what it sees.

## Impact

- New: `portfolio_agent/tools/position_view.py`, `position_views_db.py`, `portfolio_agent/services/position_view_service.py`.
- Changed: `portfolio_agent/tools/portfolio_assessment.py` (owner entry point), `portfolio_agent/pipeline/batch/morning.py`, `web/chat/rendering.py`, `web/chat/engine_tools.py`, `web/chat/orchestration.py` (tool table), `web/pages/4_Chat.py`.
- Tests: new `tests/test_position_view.py`; the chat tool catalogue test names the new tool.
