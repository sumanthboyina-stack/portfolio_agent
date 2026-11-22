# llm-model-routing Specification

## Purpose
Defines which language models serve which kind of task, in what order they are tried, and what counts as a failure that moves on to the next model. Covers the per-tier failover chains, the specialist-to-tier map, the batch-sticky session pointer, error classification, the batch failover runner and the JSON extraction helpers. The daily APEX prediction model plan (champion, fallbacks, challenger, random pool) is owned by `apex-prediction-engine`, not by this capability. Baseline at HEAD `ca450dd`.

## Requirements

### Requirement: Specialists map to task tiers
The system SHALL route each named specialist to a task tier: `news` to `triage`; `fundamentals` and `research` to `flash`; `macro` and `risk` to `analyst`; `reasoning` and `synthesis` to `reasoning`; `clearance` and `technical` to `fast`. A name not in the map MUST use the `fast` tier.

#### Scenario: Known specialist
- **WHEN** a model chain is requested for the `research` specialist
- **THEN** the `flash` chain is used

#### Scenario: Unknown specialist
- **WHEN** a model chain is requested for a name that is not in the map
- **THEN** the `fast` chain is used

<!-- tests: NONE -->

### Requirement: Low-reasoning tier chains
The `triage`, `flash` and `fast` chains SHALL start with free or cheap providers and end with paid last-resort models. They MUST contain no Claude and no Gemini model, and none of the higher-reasoning Mistral models.

#### Scenario: Triage chain order
- **WHEN** the `triage` chain is read with Cloudflare unconfigured
- **THEN** it is, in order: Groq GPT-OSS-120B, Groq GPT-OSS-20B, Mistral-Small, Ministral-8B, OR-Nemotron-Nano, GPT-4o-mini, GPT-4o

#### Scenario: Flash chain order
- **WHEN** the `flash` chain is read with Cloudflare unconfigured
- **THEN** it is the `triage` order, including OR-Nemotron-Nano

#### Scenario: Fast chain order
- **WHEN** the `fast` chain is read with Cloudflare unconfigured
- **THEN** it is the `triage` order without OR-Nemotron-Nano

#### Scenario: Paid models only at the end
- **WHEN** any of the three low-reasoning chains is read
- **THEN** GPT-4o-mini and GPT-4o are its last two entries, in that order

<!-- tests: tests/test_apex_random_pool.py::test_mistral_higher_models_are_apex_models_and_lower_ones_are_pipeline_only -->

### Requirement: High-reasoning tier chains
The `analyst` chain SHALL be, in order: GPT-5, o3, OR-Claude-Opus-4.8, OR-Qwen3.7-Max, OR-Grok-4.3, Mistral-Large, Magistral-Medium. The `reasoning` chain SHALL be the same list with Claude-Sonnet in front. Neither chain MUST contain a low-reasoning model.

#### Scenario: Analyst chain order
- **WHEN** the `analyst` chain is read
- **THEN** its entries are GPT-5, o3, OR-Claude-Opus-4.8, OR-Qwen3.7-Max, OR-Grok-4.3, Mistral-Large, Magistral-Medium

#### Scenario: Reasoning chain starts with Sonnet
- **WHEN** the `reasoning` chain is read
- **THEN** its first entry is Claude-Sonnet and the remaining entries equal the `analyst` chain in the same order

#### Scenario: Mistral Large and Magistral are last
- **WHEN** either high-reasoning chain is read
- **THEN** its last two entries are Mistral-Large then Magistral-Medium
- **AND** it contains neither Mistral-Small nor Ministral-8B

<!-- tests: tests/test_apex_random_pool.py::test_mistral_higher_models_are_apex_models_and_lower_ones_are_pipeline_only -->

### Requirement: Cloudflare models join only fully configured low tiers
Cloudflare models (`CF-Llama-3.3-70B`, `CF-Mistral-Small-3.1`) SHALL join the `triage`, `flash` and `fast` chains only when both the Cloudflare API key and the Cloudflare account id are set. They MUST never appear in the `analyst` or `reasoning` chains.

#### Scenario: Account id missing
- **WHEN** the API key is set and the account id is not
- **THEN** no chain contains a `CF-` model

#### Scenario: Both variables set
- **WHEN** both variables are set
- **THEN** `triage`, `flash` and `fast` contain both Cloudflare models
- **AND** `analyst` and `reasoning` contain none

<!-- tests: tests/test_apex_random_pool.py::test_cloudflare_models_join_only_the_low_tiers_and_only_when_fully_configured -->

### Requirement: Retired models stay out of the chains
No chain SHALL contain a model that has been retired for poor validation accuracy, stalls, missing credits or removal from its provider: OR-Owl-Alpha, Groq-8B, OR-Granite-4.1-8B, OR-Laguna-M.1, OR-Ring-2.6-1T, OR-Qwen3.6-Flash or OR-Mistral-Med-3.5.

#### Scenario: Retired label absent
- **WHEN** the labels of every chain are collected
- **THEN** none of the retired labels is present

<!-- tests: NONE -->

### Requirement: Primary model per tier
The primary model of a tier SHALL be the first entry of its chain: Groq GPT-OSS-120B for `triage`, `flash` and `fast`, and GPT-5 for `analyst`. A tier name that is not defined MUST resolve to the `fast` chain.

#### Scenario: Specialist primary model
- **WHEN** the primary model for the `macro` specialist is requested
- **THEN** it is GPT-5

#### Scenario: Unknown tier
- **WHEN** the primary model for an undefined tier is requested
- **THEN** the first entry of the `fast` chain is returned

<!-- tests: NONE -->

### Requirement: Provider-level errors trigger failover
An error SHALL count as a failover error when its text shows a provider-level block. That covers credit or quota exhaustion, payment required, insufficient balance, rate limiting, HTTP 429, 503 and 529, overload or unavailability, context-length overflow, a model or tier not available to the key, model not found, and bad or revoked credentials.

#### Scenario: DeepSeek out of credit
- **WHEN** a DeepSeek "Insufficient Balance" error is classified
- **THEN** it is a failover error

#### Scenario: Provider URL payment error
- **WHEN** a "402 Client Error: Payment Required" for a provider API URL is classified
- **THEN** it is a failover error

#### Scenario: Mistral tier or rate limit
- **WHEN** a Mistral "not available in your subscription tier" error, or a Mistral rate-limit error, is classified
- **THEN** each is a failover error

#### Scenario: Rate limit text
- **WHEN** an error whose text contains "429 rate limit" is classified
- **THEN** it is a failover error

<!-- tests: tests/test_apex_random_pool.py::test_deepseek_running_out_of_credit_counts_as_a_failover_error_so_the_next_model_is_tried, tests/test_apex_random_pool.py::test_mistral_tier_and_rate_limit_errors_fail_over_and_mark_the_model_unusable, tests/test_execution_trace_characterization.py::test_run_apex_primary_failure_falls_back_to_second_model_once -->

### Requirement: Data-API HTTP errors do not trigger failover
An HTTP client or server error for a URL that is not a model-provider endpoint, such as SEC or FRED, MUST NOT be a failover error. Switching model cannot fix it and it MUST NOT advance the session pointer.

#### Scenario: Data API error
- **WHEN** a "403 Client Error: Forbidden for url: https://data.sec.gov/..." error is classified
- **THEN** it is not a failover error

<!-- tests: NONE -->

### Requirement: Anthropic invalid-request errors fail over
An Anthropic error of type `invalid_request_error` SHALL be a failover error, because Anthropic reports credit exhaustion and model-not-found under that type.

#### Scenario: Anthropic credit message
- **WHEN** an Anthropic exception with type `invalid_request_error` is classified
- **THEN** it is a failover error

<!-- tests: NONE -->

### Requirement: Batch session pointer is sticky
A batch session SHALL start at the first model of its specialist's chain. After a failover error at chain index N, later calls in the batch MUST start at index N+1. A failure at an index below the current pointer MUST NOT move the pointer back. The session is exhausted when the pointer passes the last chain entry.

#### Scenario: Later tickers skip a dead model
- **WHEN** the first model fails with a failover error on one ticker
- **THEN** the next ticker in the same batch starts at the second model

#### Scenario: Older failure does not rewind
- **WHEN** a failure is reported for an index lower than the current pointer
- **THEN** the pointer is unchanged

#### Scenario: Last model fails
- **WHEN** a failover error occurs at the last chain index
- **THEN** the session is exhausted

<!-- tests: NONE -->

### Requirement: Failover runner tries each model once in order
The batch failover runner SHALL try chain models from the session pointer in order, one attempt each, and stop at the first success. A successful result MUST carry the display name and provider of the model that produced it.

#### Scenario: First model succeeds
- **WHEN** the first model from the start index succeeds
- **THEN** the result carries that model's label as `_model_used` and its provider as `_model_provider`
- **AND** no other model is tried

#### Scenario: First model fails over
- **WHEN** the first model raises a failover error and the second succeeds
- **THEN** the result carries the second model's label

<!-- tests: NONE -->

### Requirement: Ticker-only fallback does not advance the session
For tool-call formatting failures (`tool_use_failed`, "failed to call a function", `failed_generation`), the runner SHALL fall back to the next model for that ticker only and MUST NOT advance the session pointer. Any other non-failover error MUST be raised to the caller.

#### Scenario: Tool-call format failure
- **WHEN** a model raises a `tool_use_failed` error for one ticker
- **THEN** the next model is tried for that ticker
- **AND** the next ticker in the batch starts at the same model as before

#### Scenario: Other error
- **WHEN** a model raises an error that is neither a failover error nor a ticker-only error
- **THEN** it is raised and no further model is tried

<!-- tests: NONE -->

### Requirement: Chain exhaustion is a distinct error
The runner SHALL raise `CreditExhaustedError` when the last model in the chain fails with a failover error, when a batch session is already exhausted before the call, and when the chain is empty. The batch pipeline phases rely on this error to checkpoint.

#### Scenario: All models fail
- **WHEN** every model from the start index raises a failover error
- **THEN** `CreditExhaustedError` is raised naming the specialist and the last error

#### Scenario: Exhausted session
- **WHEN** a call is made with an exhausted session
- **THEN** `CreditExhaustedError` is raised without any model call

<!-- tests: NONE -->

### Requirement: Groq rate-limit wait is bounded
When every model is exhausted and the last failure was a Groq per-minute rate limit, the pipeline phases SHALL wait the suggested retry delay plus 2 seconds and retry from the first Groq entry. The delay is parsed from forms such as `22.37s`, `35m51.3s` and `2h30m3s`, and is 65 seconds when none is given. A delay above 300 seconds MUST NOT be waited out and the phase checkpoints instead.

#### Scenario: Short delay
- **WHEN** a Groq rate-limit error suggests a 22.37 second retry
- **THEN** the wait is 24.37 seconds

#### Scenario: No delay in the message
- **WHEN** a Groq rate-limit error gives no retry time
- **THEN** the wait is 65 seconds

#### Scenario: Long delay
- **WHEN** the suggested retry time is above 300 seconds including the extra 2 seconds
- **THEN** no wait happens and the phase checkpoints

<!-- tests: NONE -->

### Requirement: Unsupported call parameters are dropped
The system SHALL drop request parameters a model does not support, such as temperature for OpenAI o-series models, instead of raising an error.

#### Scenario: o3 with temperature
- **WHEN** a call to o3 sets a temperature other than 1
- **THEN** the temperature is dropped and the call is made

<!-- tests: NONE -->

### Requirement: JSON extraction from model replies
The JSON helpers SHALL extract a JSON array or object from a reply that wraps it in a fenced `json` block, a plain fenced block, or surrounding prose. When nothing parses, the array helper MUST return an empty list and the object helpers MUST return `None`. The APEX helper requires the object to contain a `recommendation` key when it is not fenced.

#### Scenario: Fenced array
- **WHEN** a reply contains a fenced `json` block holding an array
- **THEN** the parsed list is returned

#### Scenario: Prose around an object
- **WHEN** a reply has narrative text followed by a JSON object
- **THEN** the object is returned

#### Scenario: Unparseable reply
- **WHEN** a reply contains no valid JSON
- **THEN** an empty list is returned for arrays and `None` for objects

<!-- tests: NONE -->
