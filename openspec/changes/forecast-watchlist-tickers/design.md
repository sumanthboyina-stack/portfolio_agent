# Design

The watchlist the batch reads is the system-wide watchlist (market-data jobs are not owner-scoped), as for news and research already. Forecasts are shared market-only rows; which viewer sees a watchlist ticker in the Validation Watchlist group is decided by that viewer's own watchlist. `select_event_tickers` is shared by both batches so the two thresholds are applied identically.
