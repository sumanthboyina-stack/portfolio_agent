## ADDED Requirements

### Requirement: Rate-limited models cool down
A model whose call fails with a rate limit (HTTP 429 or "rate limit" in the error) SHALL be skipped for a 90-second cool-down and then tried again. A credit or quota refusal MUST keep the longer 10-minute mark, and other failures such as invalid JSON MUST NOT mark the model.

#### Scenario: Rate limit then recovery
- **WHEN** a model returns a rate-limit error for one ticker
- **THEN** later tickers within 90 seconds do not call it, and after the cool-down it is called again

<!-- tests: tests/test_apex_dead_models.py::test_a_rate_limited_model_is_skipped_for_a_short_cooldown_then_retried -->
