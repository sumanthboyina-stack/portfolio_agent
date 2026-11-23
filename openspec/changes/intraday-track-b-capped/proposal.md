# Proposal

## Why

The intraday batch ran the full news, research, fundamentals and forecast pipeline for up to 30 headline-scraped "trending" symbols at each of three daily runs. Symbols were not checked, filter-dropped tickers looked new at every run, and research and fundamentals were forced to re-run. This was slow and spent most of the batch's model credits.

## What Changes

- A trending ticker outside the portfolio enters the pipeline only with a signal: an unforecast severity-3 event (every run), or a math-screen score at or above the minimum that has not been examined today (first intraday run of the day only).
- Trending symbols must exist in the loaded universe when one is loaded.
- At most 5 tickers per pass, ranked by event severity then screen score; the rest are deferred and logged.
- "Examined today" counts a news row or a news-filter-log row.
- Forced re-run of research and fundamentals applies only to event-triggered tickers.
- A trending ticker whose event already produced a forecast is skipped.

## Capabilities

### Modified Capabilities
- `pipeline-orchestration`: Intraday trending track requirement.

## Out of scope

- Track A (held tickers), model choice, per-run budget guards, cheaper models for discovery.

## Impact

- `portfolio_agent/pipeline/batch/intraday.py` (new `select_track_b`, `_examined_today`), two keys in `config.py`, new `tests/test_intraday_track_b.py`.
- Behaviour change: a trending ticker with no event and no screen signal is no longer researched or forecast.
