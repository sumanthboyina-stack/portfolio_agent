## ADDED Requirements

### Requirement: Event-driven forecasts send a follow-up with the forecast label
When an event-driven forecast for a ticker is saved, the system SHALL send everyone who holds or watches that ticker a `MARKET_EVENT` notification with dedup key `event_forecast:<event id>` stating APEX's label after the news: "Forecast label: <PRIOR> -> <NEW>." when it changed, "Forecast label unchanged: <LABEL>." when not, and "Forecast label: <NEW>." when there was no prior label. The brief MUST carry no amounts or position sizes and MUST NOT be phrased as an instruction.

#### Scenario: Label changed
- **WHEN** the shared label for a held ticker was HOLD and the event-driven forecast saves SELL
- **THEN** each holder and watcher gets "forecast updated after material news. Forecast label: HOLD -> SELL." with the event's severity

#### Scenario: Label unchanged or first
- **WHEN** the new label equals the prior label, or no shared forecast existed before
- **THEN** the message reads "Forecast label unchanged: <LABEL>." or "Forecast label: <NEW>." respectively

#### Scenario: Sent once per event
- **WHEN** the follow-up for the same event is offered again
- **THEN** nothing further is queued

#### Scenario: Audience and settings
- **WHEN** a person neither holds nor watches the ticker
- **THEN** they receive nothing
- **AND** each recipient is judged by their own routing, severity filter, quiet hours, content level and clearance note

<!-- tests: tests/test_event_forecast_followup.py::test_a_changed_label_is_reported_as_prior_to_new_with_the_horizons, tests/test_event_forecast_followup.py::test_unchanged_and_first_labels_have_their_own_wording, tests/test_event_forecast_followup.py::test_the_follow_up_is_sent_once_per_event_and_only_to_holders_and_watchers, tests/test_event_forecast_followup.py::test_each_person_is_judged_by_their_own_clearance_note, tests/test_event_forecast_followup.py::test_an_event_driven_forecast_sends_the_follow_up_with_the_prior_label -->

### Requirement: The prior label reads only shared forecasts
The label compared against SHALL be the latest shared, non-challenger recommendation for the ticker read before the event-driven rows are written; private and challenger forecasts MUST NOT be read or quoted. A scheduled forecast with no event, and a forecast that saves no rows, MUST send no follow-up.

#### Scenario: Private and challenger rows ignored
- **WHEN** newer private or challenger forecasts exist for the ticker
- **THEN** the prior label is the latest shared non-challenger label, or none

#### Scenario: No event
- **WHEN** a scheduled forecast saves with no triggering event
- **THEN** no follow-up is queued

<!-- tests: tests/test_event_forecast_followup.py::test_the_prior_label_reads_only_shared_non_challenger_forecasts, tests/test_event_forecast_followup.py::test_a_scheduled_forecast_with_no_event_sends_no_follow_up, tests/test_event_forecast_followup.py::test_no_message_without_an_event_or_a_label -->
