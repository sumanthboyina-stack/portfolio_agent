"""
Held tickers raise material-news events.

No network and no LLM: Stage 1+2 triage and the Stage 3 scorer are replaced with canned data; everything checked is what the
news phase logs and what the detector queues in the outbox.
"""
from __future__ import annotations

import json
import sqlite3

import portfolio_agent.tools.db as db_module
from portfolio_agent.events import detector
from portfolio_agent.pipeline.daily import news

# the autouse DB fixture and the helpers the notification tests already use
from tests.test_notification_producers import _env, _hold, _person, _rows  # noqa: F401


class _Log:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass


def _interesting(ticker: str) -> dict:
    return {"ticker": ticker, "articles": [{"title": f"{ticker} headline"}], "reason": "keywords", "article_count": 1,
            "matched_keywords": ["guidance"]}


def _run_triage(monkeypatch, *, held: str, other: str, scores: dict[str, int], held_interesting: bool = True):
    """Run _triage_news_tickers with canned triage and scorer; returns (tickers_with_news, skipped, logged decisions, scorer input)."""
    from portfolio_agent.tools import checkpoint, news_db, news_filter
    interesting = ([_interesting(held)] if held_interesting else []) + [_interesting(other)]
    skipped = [] if held_interesting else [{"ticker": held, "article_count": 2, "reason": "routine"}]
    articles = {t["ticker"]: t["articles"] for t in interesting}
    if not held_interesting:
        articles[held] = [{"title": "routine item"}]
    triage = {"interesting": interesting, "skipped": skipped, "interesting_count": len(interesting),
              "skipped_count": len(skipped), "articles_by_ticker": articles,
              "source_by_ticker": {t: "finnhub" for t in articles}}
    seen: dict = {}
    logged: list[dict] = []
    monkeypatch.setattr(news_filter, "batch_fetch_and_triage", lambda tickers: json.dumps(triage))

    def _score(items):
        seen["scored"] = [i["ticker"] for i in items]
        return {i["ticker"]: {"score": scores[i["ticker"]], "summary": f"{i['ticker']} summary"} for i in items}

    monkeypatch.setattr(news_filter, "batch_score_materiality", _score)
    monkeypatch.setattr(checkpoint, "pending_from_yesterday", lambda *a, **k: [])
    monkeypatch.setattr(news_db, "load_seen_hashes", lambda tickers: {})
    monkeypatch.setattr(news_db, "log_filter_decisions", lambda decisions: logged.extend(decisions))
    result = news._triage_news_tickers([held, other], {held}, _Log())
    return result[0], result[1], {d["ticker"]: d for d in logged}, seen.get("scored", [])


def test_held_tickers_that_survive_triage_are_scored_and_still_summarized(monkeypatch):
    with_news, skipped, rows, scored = _run_triage(monkeypatch, held="HELD", other="WATCH", scores={"HELD": 3, "WATCH": 2})
    assert sorted(scored) == ["HELD", "WATCH"]                         # the held ticker now goes to the scorer
    assert "HELD" in with_news and "WATCH" in with_news
    assert rows["HELD"]["stage_3_decision"] == "bypassed_portfolio"
    assert (rows["HELD"]["stage_3_score"], rows["HELD"]["stage_3_summary"]) == (3, "HELD summary")
    assert rows["HELD"]["final_decision"] == "analyzed"


def test_held_ticker_scoring_zero_is_still_summarized(monkeypatch):
    with_news, skipped, rows, _ = _run_triage(monkeypatch, held="HELD", other="WATCH", scores={"HELD": 0, "WATCH": 0})
    assert "HELD" in with_news                                         # the score never gates a held ticker
    assert skipped == {"WATCH": "low_materiality"}                    # a non-held ticker scoring 0 is still dropped
    assert rows["HELD"]["stage_3_score"] == 0


def test_held_ticker_routine_in_stage_2_is_summarized_but_unscored(monkeypatch):
    with_news, _, rows, scored = _run_triage(monkeypatch, held="HELD", other="WATCH", scores={"WATCH": 1}, held_interesting=False)
    assert "HELD" in with_news and scored == ["WATCH"]
    assert rows["HELD"]["stage_3_decision"] == "bypassed_portfolio" and rows["HELD"]["stage_3_score"] is None


def _filter_log(rows: list[tuple]) -> None:
    with sqlite3.connect(str(db_module.DB_PATH)) as c:
        c.execute("CREATE TABLE IF NOT EXISTS news_filter_log (as_of_date TEXT, ticker TEXT, stage_2_decision TEXT, stage_3_decision TEXT, "
                  "stage_3_score INTEGER, stage_3_summary TEXT, final_decision TEXT)")
        c.executemany("INSERT INTO news_filter_log VALUES (?, ?, ?, ?, ?, ?, ?)", rows)


def test_a_scored_held_ticker_raises_a_material_news_event():
    alice = _person("alice")
    _hold(alice, "NVDA")
    _filter_log([("2026-10-12", "NVDA", "interesting", "bypassed_portfolio", 3, "Big news", "analyzed"),
                 ("2026-10-12", "AAPL", "interesting", "bypassed_portfolio", None, None, "analyzed")])
    events = detector.detect_material_news(["NVDA", "AAPL"], "2026-10-12", {"intraday_severity_threshold": 3})
    assert [(e["ticker"], e["severity"]) for e in events] == [("NVDA", 3)]   # the unscored held ticker raises nothing
    assert any(r["dedup_key"] == f"event:{events[0]['id']}" for r in _rows(owner="alice"))
