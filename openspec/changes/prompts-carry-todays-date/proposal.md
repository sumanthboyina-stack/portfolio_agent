# Proposal

## Why

A model knows nothing after its training data. The chat's system prompt and prediction prompts carried no date, so the chat answered a Fed-timing question as if it were late 2023 ("next meeting December 18, 2023"). The daily pipeline prompts had no date either.

## What Changes

- One helper builds a "Today's date" paragraph (US Central, computed when the prompt is built) that also tells the model not to state dates, rates, prices or market conditions from memory and to rely on supplied data.
- It is put at the front of: the chat system prompt (Q&A and synthesis), both chat prediction prompts, the daily APEX prompt, the fundamentals, research, risk and news batch prompts, and the news scorer prompt.

## Capabilities

### Modified Capabilities
- `llm-model-routing`: prompts sent to models carry today's date (ADDED requirement).

## Out of scope

- Prompts that only parse or narrate supplied text (holdings file parsing, validation narration, clearance memos), where the date changes nothing.
- Changing which models answer, or the prompts' other content.

## Impact

- New `portfolio_agent/pipeline/prompt_date.py`; call sites in `web/chat/orchestration.py`, `pipeline/daily/{apex,fundamentals,research,news,risk_technical}.py`, `tools/news_filter.py`.
- New `tests/test_prompt_date.py`, including a guard that fails when a pipeline prompt template is formatted without the date.
