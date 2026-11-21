# Spec Delta

## Purpose
Use the cheap chain to choose tools and a stronger chain to write answers that reason over the user's own data, without ever touching the premium `reasoning` chain from chat.

## ADDED Requirements

### Requirement: Tool selection stays on flash
Tool-calling rounds SHALL use the `flash` chain.

#### Scenario: Tool rounds
- **WHEN** the agent chooses and runs tools
- **THEN** those calls use the `flash` chain

### Requirement: One analyst synthesis call when private or engine data was used
If any owner-scoped or engine tool ran in the turn, exactly one final call on the `analyst` chain SHALL write the answer from the gathered tool results with no tools enabled. If that chain fails, the flash answer SHALL be used.

#### Scenario: Analyst chain unavailable
- **WHEN** every `analyst` model fails (for example out of credit)
- **THEN** the flash answer is shown, and a model marked out of credit is not retried on the same turn or again within its cool-off

### Requirement: The tier is a setting
`chat_synthesis_tier` (`flash` | `analyst`, default `analyst`) SHALL be stored with the existing engine settings, validated, versioned and audited like them. `flash` disables the extra call.

#### Scenario: Flash tier
- **WHEN** `chat_synthesis_tier` is `flash`
- **THEN** no analyst call is made and the flash answer is final; an invalid value is rejected

### Requirement: The reasoning chain is predict-only
Chat mode SHALL NOT call the `reasoning` chain.

#### Scenario: Chat code
- **WHEN** chat mode runs any turn
- **THEN** the `reasoning` chain is never used

### Requirement: The answering model is logged
The model that produced the answer SHALL be emitted in the existing `model_used` event.

#### Scenario: Event
- **WHEN** an answer is produced
- **THEN** the `model_used` event names the chain model that wrote it
