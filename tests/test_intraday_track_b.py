"""Intraday Track B selection: capped, signal-driven, once-a-day discovery (pure selector + DB-backed 'examined' check)."""
from __future__ import annotations

from portfolio_agent.pipeline.batch.intraday import _examined_today, select_track_b

KW = dict(discovery_allowed=True, cap=5, min_screen_score=2, known_universe=set())


def _sel(trending=(), events=None, screen=None, examined=(), done=(), **over):
    return select_track_b(list(trending), events or {}, screen or {}, set(examined), set(done), **{**KW, **over})


def test_a_trending_ticker_with_no_signal_is_never_run():
    assert _sel(trending=["ZZZ", "YYY"])["tickers"] == []


def test_event_triggered_trending_runs_on_every_pass_until_its_event_has_a_forecast():
    ev = {"AAA": {"id": 1, "severity": 3}}
    assert _sel(trending=["AAA"], events=ev, discovery_allowed=False)["tickers"] == ["AAA"]
    assert _sel(trending=["AAA"], events=ev, done={"AAA"})["tickers"] == []


def test_screen_promoted_runs_only_on_the_first_pass_and_only_if_not_examined():
    assert _sel(screen={"BBB": 3.0})["tickers"] == ["BBB"]
    assert _sel(screen={"BBB": 3.0}, discovery_allowed=False)["tickers"] == []
    assert _sel(screen={"BBB": 3.0}, examined={"BBB"})["tickers"] == []
    assert _sel(screen={"BBB": 1.0})["tickers"] == []                      # below the minimum score


def test_unknown_symbols_are_ignored_when_a_universe_is_loaded():
    ev = {"AAA": {"id": 1, "severity": 3}, "JUNK": {"id": 2, "severity": 3}}
    assert _sel(trending=["AAA", "JUNK"], events=ev, known_universe={"AAA"})["tickers"] == ["AAA"]


def test_ranking_is_events_first_then_screen_score_and_the_cap_defers_the_rest():
    ev = {"E1": {"id": 1, "severity": 3}, "E2": {"id": 2, "severity": 4}}
    out = _sel(trending=["E1", "E2"], events=ev, screen={"S1": 5.0, "S2": 9.0, "S3": 2.0}, cap=3)
    assert out["tickers"] == ["E2", "E1", "S2"] and out["dropped_for_cap"] == ["S1", "S3"]


def test_a_ticker_the_news_filter_dropped_counts_as_examined():
    from portfolio_agent.tools import news_db
    with news_db._db() as c:                                # creates the news tables
        c.execute("INSERT INTO news_filter_log (as_of_date, ticker, stage_2_decision, final_decision) "
                  "VALUES ('2026-11-23', 'DROP', 'routine', 'dropped')")
        c.commit()
    assert _examined_today(["DROP", "NEW"], "2026-11-23") == {"DROP"}
