## ADDED Requirements

### Requirement: Prompts about markets carry today's date
Every prompt sent to a model to answer about markets, rates, news or timing SHALL begin with today's date in US Central, computed when the prompt is built, and a statement that the model's training knowledge ends before that date. This covers the chat system prompt, the chat prediction prompts, the daily APEX prompt, the fundamentals, research, risk and news batch prompts, and the news scorer prompt.

#### Scenario: Chat
- **WHEN** the chat sends a question to a model
- **THEN** the system message contains "Today's date:" with the current weekday and date

#### Scenario: Evening in Central time
- **WHEN** it is 03:30 UTC on 23 November
- **THEN** the date line reads Sunday, November 22 (US Central)

#### Scenario: Pipeline prompts
- **WHEN** the daily APEX or the news scorer prompt is built
- **THEN** it starts with the date line

<!-- tests: tests/test_prompt_date.py::test_the_chat_system_prompt_carries_the_date, tests/test_prompt_date.py::test_the_line_names_the_central_date_and_tells_the_model_not_to_assume, tests/test_prompt_date.py::test_the_daily_apex_prompt_carries_the_date, tests/test_prompt_date.py::test_the_news_scorer_prompt_carries_the_date, tests/test_prompt_date.py::test_no_pipeline_prompt_is_built_without_the_date -->
