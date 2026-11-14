"""Risk analyst cadence: re-run only on a change (price move, severity-3 event, new filing,
holdings change), otherwise weekly; batched 10 tickers per LLM call."""
import json
from datetime import date, timedelta

from portfolio_agent.tools.risk_flags_db import risk_refresh_reason, upsert_risk_flag

TODAY = "2026-11-14"


def _seed(days_ago, sig="sigA", owner="alice", ticker="AAPL"):
    d = (date.fromisoformat(TODAY) - timedelta(days=days_ago)).isoformat()
    upsert_risk_flag(ticker, d, "risk", False, 3, "x", json.dumps({"_holdings_sig": sig}), "m", owner=owner)


def test_never_run_then_skip_when_nothing_changed():
    assert risk_refresh_reason("NEVR", TODAY, "alice") == "no_risk_flag"
    _seed(2, ticker="NEVR")
    assert risk_refresh_reason("NEVR", TODAY, "alice", holdings_sig="sigA", price_move_pct=1.0,
                               event_tickers_since=lambda s: {}) is None


def test_weekly_fallback():
    _seed(7, ticker="WKLY")
    assert risk_refresh_reason("WKLY", TODAY, "alice", holdings_sig="sigA").startswith("weekly_")


def test_each_trigger_fires():
    _seed(2, ticker="TRIG")
    assert risk_refresh_reason("TRIG", TODAY, "alice", holdings_sig="sigB") == "holdings_changed"
    assert risk_refresh_reason("TRIG", TODAY, "alice", holdings_sig="sigA", price_move_pct=-6.2).startswith("price_move")
    assert risk_refresh_reason("TRIG", TODAY, "alice", holdings_sig="sigA",
                               event_tickers_since=lambda s: {"TRIG": "sec_8k"}) == "event_sec_8k"
    assert risk_refresh_reason("TRIG", TODAY, "alice", holdings_sig="sigA",
                               new_filing_date=TODAY).startswith("new_filing_")


def test_already_done_today_and_owner_isolation():
    _seed(0, ticker="DONE")
    assert risk_refresh_reason("DONE", TODAY, "alice", price_move_pct=9) is None
    assert risk_refresh_reason("DONE", TODAY, "bob") == "no_risk_flag"


def test_batches_of_ten(monkeypatch):
    import asyncio
    from portfolio_agent.pipeline.daily import risk_technical as rt
    import portfolio_agent.pipeline.runner as runner_mod
    tickers = [f"T{i:02d}" for i in range(23)]
    monkeypatch.setattr(rt, "_owners_and_their_tickers", lambda: {"alice": tickers})
    monkeypatch.setattr(rt, "_price_stats", lambda t: {})
    monkeypatch.setattr(rt, "_latest_filing_dates", lambda t: {})
    import portfolio_agent.tools.holdings_db as repo
    monkeypatch.setattr(repo, "get_portfolio_by_owner", lambda o: {"portfolio_id": 1})
    class _H(dict):
        def to_dict(self): return dict(self)
    monkeypatch.setattr(repo, "get_holdings", lambda portfolio_id=None: [_H(ticker=t, shares=1, sector="Tech", current_value=100) for t in tickers])
    import portfolio_agent.tools.portfolio_risk as pr
    monkeypatch.setattr(pr, "compute_portfolio_risk_context", lambda h: {})
    monkeypatch.setattr(rt, "needs_technical_refresh", lambda *a, **k: False, raising=False)
    import portfolio_agent.tools.risk_flags_db as rdb
    monkeypatch.setattr(rdb, "needs_technical_refresh", lambda *a, **k: False)
    sizes = []
    async def _fake(prompt, chain, idx):
        got = [l.split("=== ")[1].split(" ")[0] for l in prompt.splitlines() if l.startswith("=== ")]
        sizes.append(len(got))
        return [{"ticker": t, "portfolio_risk_score": 2, "summary": "ok"} for t in got], idx
    monkeypatch.setattr(rt, "_call_risk_batch", _fake)
    asyncio.run(rt._run_daily_risk_technical(tickers))
    assert sizes == [10, 10, 3]
