# Spec Delta

## Purpose
Keep chat follow-ups grounded in what was actually fetched, and bound a turn's cost and size in code instead of in the prompt.

## ADDED Requirements

### Requirement: Tool digests persist and replay
For each tool call the assistant message SHALL store a digest in `chat_messages.metadata`: tool name, args, as_of, the top 10 rows or key figures, at most 1,500 characters. `_build_chat_history` SHALL replay the digests as assistant context.

#### Scenario: Follow-up from a digest
- **WHEN** the user asks "which of those do I already own?" after an opportunities answer
- **THEN** the agent answers from the stored digest without calling the same tool again

### Requirement: The budget is enforced in code
A turn SHALL execute at most 6 tools in total, counted across parallel calls, and at most 8 rounds. On reaching either limit the loop SHALL force a final answer with tools disabled.

#### Scenario: Parallel calls
- **WHEN** a round requests 5 parallel tool calls after 3 have run
- **THEN** only 3 execute, the rest are refused with a budget note, and the next call has no tools

### Requirement: Tool output is capped
A single tool result sent to the model SHALL be at most 12 KB; larger results are truncated and carry `"truncated": true`.

#### Scenario: Oversized result
- **WHEN** a tool returns more than 12 KB
- **THEN** the model receives at most 12 KB, cut at a row or field boundary, with `"truncated": true`

### Requirement: The final answer streams
The final answer text SHALL reach the page incrementally.

#### Scenario: Streaming
- **WHEN** the final answer is generated
- **THEN** the page receives it as ordered chunks before the call completes, not as one block at the end
