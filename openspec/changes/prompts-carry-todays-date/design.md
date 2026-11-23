# Design

- **Computed per prompt, not per import.** The chat system prompt was a module constant built once, so the date comes from `_chat_system()` called when each message list is built; pipeline prompts get it where they are formatted.
- **US Central.** The zone the batches, schedule and event dates already use; the line shows the weekday, long date and ISO date.
- **Prefix outside the templates.** The date is added around the formatted prompt, so prompt-policy versioning and the templates' own tests are unaffected.
- **Not everywhere.** Parsing and narration prompts are left alone (nothing in them depends on today).
- **A guard test** reads the daily pipeline sources and fails if a batch or APEX template is formatted without `with_today(`.
