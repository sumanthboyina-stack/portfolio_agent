# Design

Portfolio and Watchlist are the viewer's own tickers; Opportunities are shared market-only forecasts (trigger `trending_opportunity`), identical for every viewer. A ticker can be in more than one group and is then counted in each; the history table lists all its groups. Groups longer than 60 tickers skip price-based baselines, as before. The single headline sentence became one block per group because the groups have different amounts of evidence.
