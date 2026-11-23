"""Watchlist tickers are forecast like held tickers: on the scheduled cadence and on a material-news event (with their own
severity threshold), in both the morning and the intraday batch. Everything the batches call out to is stubbed."""
from __future__ import annotations

import asyncio
import json
from datetime import date

import pytest

from portfolio_agent.events.scheduler import select_event_tickers


def _ev(ticker, severity, id_=1):
    return {"ticker": ticker, "severity": severity, "id": id_}


# ── the selector ──────────────────────────────────────────────────────────────

def test_a_watchlist_ticker_uses_the_watchlist_threshold_and_others_the_default():
    pending = [_ev("HELD", 3, 1), _ev("WATCH", 2, 2), _ev("OTHER", 2, 3)]
    got = select_event_tickers(pending, {"WATCH"}, threshold=3, watchlist_threshold=2)
    assert set(got) == {"HELD", "WATCH"}                                  # OTHER (2 < 3) is out; WATCH (2 >= 2) is in


def test_the_most_severe_event_per_ticker_wins_and_weak_events_are_dropped():
    pending = [_ev("A", 5, 10), _ev("A", 3, 11), _ev("W", 1, 12)]        # ordered most severe first, as get_pending_events does
    got = select_event_tickers(pending, {"W"}, 3, 3)
    assert got["A"]["id"] == 10 and "W" not in got


# ── the morning batch ─────────────────────────────────────────────────────────

def _morning(monkeypatch, *, scheduled, pending, watchlist_only=frozenset({"WATCH"}), tickers=("HELD", "WATCH")):
    import portfolio_agent.events.db as events_db
    import portfolio_agent.events.detector as detector
    import portfolio_agent.events.scheduler as sched
    import portfolio_agent.pipeline.daily.apex as apex
    import portfolio_agent.tools.prediction_db as pdb
    from portfolio_agent.pipeline.batch import morning
    from portfolio_agent.log import get_logger

    calls = []

    async def _apex(tickers, **kw):
        calls.append({"tickers": list(tickers), **kw})

    monkeypatch.setattr(apex, "_run_daily_apex", _apex)
    monkeypatch.setattr(detector, "run_all_detectors", lambda *a, **k: None)
    monkeypatch.setattr(events_db, "get_pending_events", lambda today, min_severity=1: list(pending))
    monkeypatch.setattr(pdb, "is_trading_day", lambda d: True)
    monkeypatch.setattr(sched, "get_scheduled_horizons_v2", lambda today, config: list(scheduled))
    asyncio.run(morning._run_morning_apex_event_driven(list(tickers), list(tickers), [], None, get_logger("t"),
                                                       watchlist_only=set(watchlist_only)))
    return calls


def test_a_scheduled_day_forecasts_watchlist_tickers_too(monkeypatch):
    calls = _morning(monkeypatch, scheduled=[5, 21], pending=[])
    assert calls and set(calls[0]["tickers"]) == {"HELD", "WATCH"}
    assert calls[0]["trigger_map"]["WATCH"][0].startswith("scheduled")


def test_a_watchlist_event_triggers_a_forecast_off_schedule(monkeypatch):
    calls = _morning(monkeypatch, scheduled=[], pending=[_ev("WATCH", 3, 7)])
    assert [c["tickers"] for c in calls] == [["WATCH"]]
    assert calls[0]["trigger_map"]["WATCH"] == ("event_material_news", 7)


def test_a_weak_event_on_a_watchlist_ticker_does_nothing_off_schedule(monkeypatch):
    assert _morning(monkeypatch, scheduled=[], pending=[_ev("WATCH", 2, 7)]) == []


# ── the intraday batch (Track A) ──────────────────────────────────────────────

def test_the_intraday_event_track_forecasts_a_watchlist_ticker_and_does_not_also_treat_it_as_a_trending_discovery(monkeypatch):
    import portfolio_agent.events.db as events_db
    import portfolio_agent.events.detector as detector
    import portfolio_agent.pipeline.batch.intraday as intraday
    import portfolio_agent.pipeline.batch.morning as morning
    import portfolio_agent.pipeline.daily.apex as apex
    import portfolio_agent.pipeline.daily.news as news
    import portfolio_agent.pipeline.daily.research as research
    import portfolio_agent.pipeline.daily.fundamentals as fundamentals
    import portfolio_agent.tools.holdings_db as holdings_db
    import portfolio_agent.tools.holdings_prices as holdings_prices
    import portfolio_agent.tools.news_sources as news_sources
    import portfolio_agent.tools.prediction_db as pdb
    import portfolio_agent.tools.watchlist_db as watchlist_db

    apex_calls, news_calls = [], []

    async def _apex(tickers, **kw):
        apex_calls.append(list(tickers))

    async def _news(*a, **kw):
        news_calls.append(list(kw.get("all_tickers") or a[0]))
        return True

    async def _noop(*a, **kw):
        return True

    monkeypatch.setattr(holdings_db, "get_portfolio_tickers", lambda: ["HELD"])
    monkeypatch.setattr(watchlist_db, "load_watchlist_tickers", lambda: ["WATCH", "HELD"])
    monkeypatch.setattr(news_sources, "get_trending_tickers", lambda n=30: json.dumps({"tickers": ["WATCH", "TRND"]}))
    monkeypatch.setattr(pdb, "is_trading_day", lambda d: True)
    monkeypatch.setattr(morning, "ensure_morning_ran_today", _noop)
    monkeypatch.setattr(detector, "run_all_detectors", lambda *a, **k: None)
    monkeypatch.setattr(events_db, "get_pending_events", lambda today, min_severity=1: [_ev("WATCH", 3, 9)])
    monkeypatch.setattr(apex, "_run_daily_apex", _apex)
    monkeypatch.setattr(news, "_run_news_phase", _news)
    monkeypatch.setattr(research, "_run_daily_research", _noop)
    monkeypatch.setattr(fundamentals, "_run_daily_fundamentals", _noop)
    monkeypatch.setattr(holdings_prices, "backfill_missing_price_snapshots", lambda: {})
    monkeypatch.delenv("PIPELINE_RUN_ID", raising=False)
    asyncio.run(intraday.run_batch_intraday())
    assert apex_calls == [["WATCH"]]                                        # Track A forecast for the watchlist ticker, once
    assert not any("WATCH" in c for c in news_calls)                        # not re-run as a trending discovery
