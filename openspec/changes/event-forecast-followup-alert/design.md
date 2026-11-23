# Design

## Context

`detect_material_news` queues the alert at detection time; the event-driven forecast for the same ticker runs later in the same batch. Prediction rows already carry `trigger_event_id`, and the APEX phase knows each ticker's event through its trigger map.

## Decisions

- **Two messages, not one delayed.** The first alert is unchanged and immediate. A second message follows when the forecast is saved, so a stuck or failed forecast never delays the news alert.
- **Hook in the champion/fallback save path of the APEX phase.** Challenger rows are saved separately and never reach the hook. The prior label is read just before the rows are written, because the new rows become "latest" the moment they are saved.
- **The label is ticker-level.** A run's recommendation is the same for every horizon it writes, so the follow-up compares one prior label with one new label and lists the horizons re-forecast (detailed text only).
- **Shared scope only, explicitly.** `latest_shared_label` passes the shared scope, so private and challenger rows cannot be read.
- **Always sent for an event-driven forecast.** Silence on an unchanged label would be ambiguous. A changed-only or conviction threshold can be added later without changing the message shape.
- **Reuse, not new plumbing.** Same audience (holders and watchers), same `notify` call and outbox, same clearance note, de-duplicated by `event_forecast:<event id>`.
- **Wording guard.** The guard test bans buy and sell in notifications; it exempts exactly "Forecast label ..." followed by APEX's own labels.

## Risks

- One extra message per event per recipient; counts toward the hourly cap.
- If the forecast fails or the ticker is skipped (for example a non-trading day), no follow-up is sent. The first alert already went out.
