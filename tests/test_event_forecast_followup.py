"""
The second message of a material event: once the event-driven forecast is saved, everyone who holds or watches the ticker is
told what APEX's label became. No network, no LLM: the model is a stub, and what is checked is what was QUEUED in the outbox.
"""
from __future__ import annotations

import asyncio

import portfolio_agent.pipeline.daily.apex as apex_mod
from portfolio_agent.events import detector
from portfolio_agent.events.db import insert_event
from portfolio_agent.tools import prediction_db as pdb

# the autouse DB fixture and helpers the notification tests already use, and the APEX-phase stand-ins from the routing tests
from tests.test_notification_producers import _env, _hold, _person, _rows, _watch  # noqa: F401
from tests.test_etf_routing import _full_loaders, _model_result, _typed  # noqa: F401


def _forecast(ticker: str, label: str, horizon: int = 5, *, scope: str = pdb.SCOPE_SHARED, role: str = "champion") -> None:
    pdb.insert_prediction(ticker=ticker, prediction="BULLISH", recommendation=label, horizon_days=horizon, scope=scope,
                          context_digest="test", prediction_role=role)


def _event() -> int:
    return insert_event("NVDA", "material_news", 4, source="news_filter_log", summary="Nvidia posted a guidance surprise.")


def _followups(**where) -> list[dict]:
    return [r for r in _rows(**where) if (r["dedup_key"] or "").startswith("event_forecast:")]


# ── The message ───────────────────────────────────────────────────────────────

def test_a_changed_label_is_reported_as_prior_to_new_with_the_horizons():
    alice = _person("alice")
    _hold(alice, "NVDA")
    ev = _event()
    assert detector.notify_event_forecast(ev, "NVDA", "HOLD", "SELL", [21, 5]) is True
    row = _followups(owner="alice")[0]
    assert row["dedup_key"] == f"event_forecast:{ev}" and row["category"] == "MARKET_EVENT" and row["severity"] == 4
    assert "forecast updated after material news. Forecast label: HOLD -> SELL." in row["body_brief"]
    assert row["body_brief"].startswith("APEX: NVDA - ") and row["body_brief"].endswith("Open APEX for details.")
    assert "$" not in row["body_brief"]


def test_unchanged_and_first_labels_have_their_own_wording():
    alice = _person("alice")
    _hold(alice, "NVDA")
    ev1 = _event()
    detector.notify_event_forecast(ev1, "NVDA", "HOLD", "HOLD", [5])
    ev2 = insert_event("NVDA", "macro_event", 3)
    detector.notify_event_forecast(ev2, "NVDA", None, "BUY", [5])
    briefs = {r["dedup_key"]: r["body_brief"] for r in _followups(owner="alice")}
    assert "Forecast label unchanged: HOLD." in briefs[f"event_forecast:{ev1}"]
    assert "Forecast label: BUY." in briefs[f"event_forecast:{ev2}"] and "->" not in briefs[f"event_forecast:{ev2}"]


def test_the_follow_up_is_sent_once_per_event_and_only_to_holders_and_watchers():
    alice, bob = _person("alice"), _person("bob")
    _hold(alice, "NVDA")
    _watch("bob", "NVDA")
    _person("carol")                                                      # neither holds nor watches it
    ev = _event()
    assert detector.notify_event_forecast(ev, "NVDA", "HOLD", "SELL", [5]) is True
    assert detector.notify_event_forecast(ev, "NVDA", "HOLD", "SELL", [5]) is False      # the outbox de-duplicates
    owners = sorted(r["owner"] for r in _followups())
    assert owners == ["alice", "bob"]


def test_each_person_is_judged_by_their_own_clearance_note():
    from portfolio_agent.tools import restricted_list_db
    alice, bob = _person("alice"), _person("bob")
    _hold(alice, "NVDA"); _hold(bob, "NVDA")
    restricted_list_db.add_restricted("NVDA", "r", "alice")
    ev = _event()
    detector.notify_event_forecast(ev, "NVDA", "HOLD", "BUY", [5])
    a, b = _followups(owner="alice")[0], _followups(owner="bob")[0]
    assert "no new trade currently permitted" in a["body_brief"]          # information only, never a filter
    assert a["status"] == "PENDING" and "BLOCKED" not in b["body_brief"]


def test_no_message_without_an_event_or_a_label():
    alice = _person("alice")
    _hold(alice, "NVDA")
    assert detector.notify_event_forecast(None, "NVDA", "HOLD", "BUY", [5]) is False
    assert detector.notify_event_forecast(_event(), "NVDA", "HOLD", None, [5]) is False
    assert _followups() == []


def test_the_prior_label_reads_only_shared_non_challenger_forecasts():
    assert detector.latest_shared_label("NVDA") is None
    _forecast("NVDA", "HOLD")
    _forecast("NVDA", "SELL", scope=pdb.SCOPE_PRIVATE)                    # private: never read
    _forecast("NVDA", "STRONG_SELL", role="challenger")                  # challenger: never read
    assert detector.latest_shared_label("NVDA") == "HOLD"


# ── Through the real APEX phase ───────────────────────────────────────────────

def _run_event_apex(monkeypatch, ticker: str, trigger_map):
    from portfolio_agent.tools import apex_dual_run as adr
    from portfolio_agent.tools import market_context as market_context_mod
    from portfolio_agent.tools import reasoning_tools as rt_mod
    from portfolio_agent.tools import yfinance_tools as yft
    monkeypatch.setattr(market_context_mod, "default_loaders", _full_loaders)
    monkeypatch.setattr(rt_mod, "get_macro_snapshot", lambda: '{"vix": 20.0}')
    monkeypatch.setattr(pdb, "is_trading_day", lambda d: True)
    monkeypatch.setattr(yft, "get_close", lambda t, d: 100.0)
    monkeypatch.setattr(yft, "get_trailing_return", lambda t, d: 0.01)

    async def _fake_run_apex(t, prompt, **kw):
        return _model_result(t)                                           # recommends BUY on the 5-day horizon
    monkeypatch.setattr(adr, "run_apex", _fake_run_apex)
    asyncio.run(apex_mod._run_daily_apex([ticker], trigger_map=trigger_map, scheduled_horizons_override=[5]))


def test_an_event_driven_forecast_sends_the_follow_up_with_the_prior_label(monkeypatch):
    alice = _person("alice")
    _hold(alice, "NVDA")
    _typed("NVDA", "STOCK")
    _forecast("NVDA", "HOLD", 21)                                         # what APEX said before the news
    ev = _event()
    _run_event_apex(monkeypatch, "NVDA", {"NVDA": ("event_material_news", ev)})
    row = _followups(owner="alice")[0]
    assert row["dedup_key"] == f"event_forecast:{ev}"
    assert "Forecast label: HOLD -> BUY." in row["body_brief"]


def test_a_scheduled_forecast_with_no_event_sends_no_follow_up(monkeypatch):
    alice = _person("alice")
    _hold(alice, "NVDA")
    _typed("NVDA", "STOCK")
    _run_event_apex(monkeypatch, "NVDA", None)
    assert _followups() == []
