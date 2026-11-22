## Purpose

Describes the interactive Google ADK analyst pipeline that `main.py TICKER` and `adk run` still use: extractors, six specialist agents, a synthesis agent and a deterministic clearance step. Its output is session state printed to the terminal. It is separate from the daily APEX prediction path, which is covered by `apex-prediction-engine`. Baseline at HEAD `ca450dd`.

## ADDED Requirements

### Requirement: CLI runs the full pipeline per ticker
`main.py` SHALL run the full analyst pipeline once for each ticker given on the command line, from `--watchlist`, or both (de-duplicated), using `--query` as the user's focus when supplied. With no ticker and no `--watchlist` it MUST exit with an error. Only `--agent news` may run with no ticker, using `MARKET`.

#### Scenario: Two tickers
- **WHEN** `main.py TICKER_A TICKER_B` is run
- **THEN** the pipeline runs once for each ticker, in the order given

#### Scenario: No ticker
- **WHEN** `main.py` is run with no ticker, no `--watchlist` and no daily or batch flag
- **THEN** it logs an error asking for at least one ticker and exits non-zero

#### Scenario: News with no ticker
- **WHEN** `main.py --agent news` is run with no ticker
- **THEN** the news agent runs for `MARKET`

<!-- tests: NONE -->

### Requirement: Agent selection
`--agent` SHALL accept `fundamentals`, `news`, `macro`, `technical`, `research`, `risk`, `synthesis`, `clearance`, `reasoning` or `all` (the default). Any other value MUST log an error listing the valid names and exit non-zero without running anything.

#### Scenario: Single specialist
- **WHEN** `main.py TICKER --agent macro` is run
- **THEN** only the macro agent runs and only its state key is printed

#### Scenario: Unknown agent
- **WHEN** `--agent` is given an unrecognized name
- **THEN** the run exits non-zero with the list of valid names

<!-- tests: NONE -->

### Requirement: Pipeline stages and order
The `all` pipeline SHALL run in four sequential stages: a preparation stage in which ticker and query extraction run in parallel, a research stage in which the fundamentals, news, macro, technical, research and risk agents run in parallel, the synthesis agent, and the clearance agent.

#### Scenario: Stage order
- **WHEN** the full pipeline runs
- **THEN** synthesis starts only after all six specialists have finished
- **AND** clearance runs after synthesis

<!-- tests: NONE -->

### Requirement: Session state seeding
Each run SHALL start from a fresh in-memory session whose `current_ticker` is the upper-cased ticker and whose `user_query` is the supplied query, or `Full analysis` when none is given. The user message is the query, or `Analyze <TICKER>` when none is given.

#### Scenario: No query
- **WHEN** a run is started for a ticker with an empty query
- **THEN** the session state has `user_query` equal to `Full analysis`
- **AND** the user message is `Analyze` followed by the upper-cased ticker

<!-- tests: NONE -->

### Requirement: Recommendation is session-only, clearance is recorded
The synthesis recommendation SHALL exist only as session state printed for the run and MUST NOT be stored as a forecast, prediction or shared artifact. The clearance decision computed in the same run MUST be recorded as a private policy decision.

#### Scenario: Run completes
- **WHEN** the full pipeline completes for a ticker
- **THEN** no prediction row is written
- **AND** one policy decision row is recorded for the ticker under the run's owner scope

<!-- tests: NONE -->

### Requirement: Synthesis instruction contract
The synthesis agent's instruction SHALL tell the model to weight Fundamentals 30%, Technical 20%, News 15%, Macro 15%, Research 10% and Risk 10%, to let high risk only reduce conviction, to score a missing specialist 5/10, and to return one JSON object with `action` of `STRONG BUY`, `BUY`, `HOLD`, `SELL` or `STRONG SELL`. These rules are prompt instructions; the code neither computes nor validates them.

#### Scenario: Missing specialist output
- **WHEN** a specialist's state key is empty at synthesis time
- **THEN** the instruction directs the model to score that dimension conservatively at 5/10

<!-- tests: NONE -->

### Requirement: Clearance is decided without a model
The clearance step SHALL resolve its decision in Python before any model runs, record it as a policy decision, store its JSON in session state under `clearance`, and short-circuit the agent's model for every ticker and profile combination.

#### Scenario: Any profile
- **WHEN** the gate runs for a ticker with a default profile, a profile with pre-clearance off, or a profile in an active blackout
- **THEN** it returns a decision without any model call
- **AND** the decision JSON is in session state under `clearance`

<!-- tests: tests/test_clearance_gate.py::test_profile_gate_resolves_every_ticker_profile_combination_without_a_model_call, tests/test_clearance_gate.py::test_profile_gate_writes_state_and_never_falls_through_to_the_llm -->

### Requirement: Clearance owner identity in the pipeline
The clearance gate SHALL evaluate for `state["owner"]` when the caller set one server-side, and otherwise for the local operator identity. When the owner's profile cannot be read it MUST evaluate with default profile values and never skip the gate.

#### Scenario: Owner supplied
- **WHEN** the session state has an owner
- **THEN** the decision is recorded under that owner's scope, using that owner's profile when one exists

#### Scenario: No owner (CLI)
- **WHEN** the session state has no owner
- **THEN** the decision is recorded under the local operator's scope

#### Scenario: Profile store unavailable
- **WHEN** reading the profile raises an error
- **THEN** the gate evaluates with a default profile and still records a decision

<!-- tests: NONE -->

### Requirement: Action-permission qualifier on CLI output
After a full-pipeline run that produced both a recommendation and a clearance, the terminal output SHALL print one qualifier line. For `ALLOWED_BY_RULES` it MUST say this is this system's rules only, not external compliance approval. For any other status it MUST say the action is NOT actionable and print the clearance memo. With either value missing it MUST print nothing and not raise.

#### Scenario: Blocked
- **WHEN** the recommendation is `STRONG BUY` and the clearance status is `BLOCKED`
- **THEN** the output says the action is NOT actionable, names `BLOCKED` and prints the memo

#### Scenario: Allowed
- **WHEN** the clearance status is `ALLOWED_BY_RULES`
- **THEN** the output says it is not external compliance approval

#### Scenario: Missing clearance
- **WHEN** the state has a recommendation but no clearance
- **THEN** nothing is printed and no error is raised

<!-- tests: tests/test_rendering.py::test_blocked_recommendation_prints_not_actionable, tests/test_rendering.py::test_allowed_recommendation_prints_allowed_by_rules_qualifier, tests/test_rendering.py::test_missing_recommendation_or_clearance_prints_nothing_and_does_not_raise -->

### Requirement: Terminal and JSON output
With `all`, the CLI SHALL print the Fundamentals, News, Macro, Technical, Research, Risk, Recommendation and Clearance sections in that order, skipping any empty one. With a single agent it MUST print that agent's state key (`synthesis` prints `recommendation`, `reasoning` prints `apex_prediction`). With `--json` it MUST print the final state as JSON without `current_ticker`.

#### Scenario: JSON output
- **WHEN** a run is made with `--json`
- **THEN** the printed object holds the session state keys other than `current_ticker`

<!-- tests: NONE -->

### Requirement: Provider failure surfaces as exhausted credit
When a run fails with a model-provider failover-class error, the runner SHALL raise a credit-exhausted error and MUST NOT retry another model.

#### Scenario: Provider quota error
- **WHEN** the model provider returns a quota or authentication error during a run
- **THEN** the run raises the credit-exhausted error and the CLI stops for that ticker

<!-- tests: NONE -->

### Requirement: Developer entry points
The package SHALL expose the full pipeline as `root_agent` for `adk run` and `adk web`. Each specialist module, the synthesis module and the clearance module MUST be runnable directly as `python <module> TICKER [query]`, defaulting to `AAPL` and `Full analysis`, and print its tool calls and its state key.

#### Scenario: Standalone specialist
- **WHEN** a specialist module is run with a ticker and no query
- **THEN** it runs for that ticker with query `Full analysis` and prints its result

<!-- tests: NONE -->
