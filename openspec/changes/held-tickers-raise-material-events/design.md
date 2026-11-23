# Design

## Context

`pipeline/daily/news.py` builds `scorer_input` from triage survivors minus the always-run set (held and extra tickers). Held tickers therefore reach the analyst with `stage_3_decision = bypassed_portfolio` and a null score, and `events/detector.py::detect_material_news` only reads rows with a non-null score >= `intraday_severity_threshold` and `final_decision = 'analyzed'`.

## Decisions

- **Score held tickers, do not gate on the score.** `scorer_input` becomes every triage survivor. `always_run_to_agent` is unchanged, so held tickers are still summarized whatever they score. The filter-log row keeps `bypassed_portfolio` (so existing readers still see why the ticker advanced) and now also stores the score and summary when one exists. The detector needs no change.
- **Held tickers that were routine in Stage 2 stay unscored.** They were never sent to the scorer, so no event can fire for them. This keeps cost and noise down and matches how every other ticker behaves.

## Risks

- The Stage 3 batch call grows by the number of held tickers with news (already chunked at 35).
- More events now fire, so more event-driven forecasts run for held tickers; this is the intended behavior and is bounded by the existing severity threshold.
