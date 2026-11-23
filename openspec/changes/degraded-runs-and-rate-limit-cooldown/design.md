# Design

The threshold (5 attempts, 50% failed) is two constants in progress_tracker. Only the APEX phase is judged, because it is the one that writes the forecasts people read. The alert reuses `notify` with category SYSTEM, deduplicated per run id, and carries no tickers or amounts. Rate limits are matched by the text "429" or "rate limit" after the credit check, so a "rate limit ... no credits" error keeps the long credit mark.
