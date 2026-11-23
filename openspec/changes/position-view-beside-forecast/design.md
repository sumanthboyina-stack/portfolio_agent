# Design

## Decisions

- **A second view, not a second prediction.** The forecast stays market-only and validated; the position view is rule-derived from the stored shared forecast and the owner's own rows. A per-person model forecast would be unvalidated, would cross the privacy boundary, cost a call per held ticker and could contradict the shared one.
- **Owner-string core, context-checked wrapper.** Batch jobs cannot mint a verified context for an owner (only the web login can), so `build_position_view_for_owner(owner, ticker, exposures, policy)` does the work and `build_position_view(ctx, ticker)` authorizes then delegates, as the health snapshots do. `build_assessment_for_owner` is the same split of the existing assessment; the original function now delegates to it unchanged.
- **Facts beside the forecast, never steering.** Cost basis, gain and holding period are shown; the `relation` field uses only the forecast direction (positive / neutral / negative / mixed / none) and the limit status (over / within / unknown). The text never says buy, sell, increase or reduce.
- **Shared scope only, explicitly.** Forecasts are read with the shared scope, so private and challenger rows cannot appear.
- **Stored once a day per owner and ticker.** Written in the morning batch (after forecasts and event runs), refreshed on a same-day re-run, one audit event per owner per day. Reads always filter by owner.
- **Value fallback.** A position's value is its stored `current_value` when present, otherwise shares x last price, as the exposures value it.

## Risks

- Views reflect the morning batch's forecasts; an event-driven forecast later in the day updates the chat card (live) but not the stored row until the next run.
- One more phase in the morning batch; a failure is logged and never stops the batch.
