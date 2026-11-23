"""
Position view: the owner's private companion to a shared forecast. Deterministic (no LLM, no network); what is checked is the
view built from the owner's own rows plus SHARED forecasts, its relation to the owner's limits, and the batch phase that
stores it.
"""
from __future__ import annotations

import sqlite3

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.services import account_service, position_view_service as pvs
from portfolio_agent.tools import position_view as pv
from portfolio_agent.tools import position_views_db as pdb
from portfolio_agent.tools import prediction_db as pdb_pred
from portfolio_agent.tools import holdings_db as repo

# the autouse DB fixture and person helper the notification tests already use
from tests.test_notification_producers import _env, _person  # noqa: F401


def _own(ctx, ticker: str, *, shares: float = 50.0, price: float = 100.0, avg_cost: float | None = 60.0, sector: str = "Technology"):
    acct = account_service.create_account(ctx, broker="manual", display_name=f"{ctx.actor}-{ticker}", account_type="TAXABLE")
    pos = {"ticker": ticker, "shares": shares, "current_price": price, "sector": sector, "price_as_of": "2026-10-08"}
    if avg_cost is not None:
        pos["avg_cost"] = avg_cost
    account_service.add_position(ctx, acct["account_id"], pos)
    return acct


def _forecast(ticker: str, label: str, horizon: int = 5, *, scope: str = pdb_pred.SCOPE_SHARED, role: str = "champion") -> None:
    pdb_pred.insert_prediction(ticker=ticker, prediction="BULLISH", recommendation=label, horizon_days=horizon, scope=scope,
                               context_digest="test", prediction_role=role)


def test_no_view_for_a_ticker_that_is_not_held_or_an_owner_with_no_portfolio():
    alice = _person("alice")
    _own(alice, "NVDA")
    assert pv.build_position_view_for_owner("alice", "AAPL") is None
    assert pv.build_position_view_for_owner("nobody", "NVDA") is None


def test_the_view_carries_position_facts_limits_and_the_shared_forecast():
    alice = _person("alice")
    _own(alice, "NVDA")                                                    # the whole book: 100% weight, over any position limit
    _forecast("NVDA", "SELL", 5)
    _forecast("NVDA", "STRONG_SELL", 21)
    view = pv.build_position_view_for_owner("alice", "NVDA")
    assert view["position"]["shares"] == 50.0 and view["position"]["weight_pct"] == pytest.approx(100.0)
    assert view["position"]["cost_basis_total"] == 3000.0 and view["position"]["unrealized_gain_pct"] == pytest.approx(66.67, abs=0.01)
    assert view["position"]["holding_period"] is None                      # a snapshot account has no lots
    assert {l["name"]: l["status"] for l in view["limits"]}["position_concentration"] == "over"
    assert view["fit"] == "over_limit"
    assert [(f["horizon_days"], f["label"]) for f in view["forecast"]["horizons"]] == [(5, "SELL"), (21, "STRONG_SELL")]
    assert view["forecast"]["direction"] == "negative" and view["relation"] == pv.REL_NEGATIVE_OVER
    assert "Unrealized +66.7% on cost" in view["summary"] and "Shared forecast label: SELL (5d), STRONG_SELL (21d)" in view["summary"]
    assert not any(w in view["summary"].lower() for w in ("you should", "consider selling", "consider buying"))


def test_only_shared_non_challenger_forecasts_are_read():
    alice = _person("alice")
    _own(alice, "NVDA")
    _forecast("NVDA", "HOLD", 5)
    _forecast("NVDA", "STRONG_SELL", 5, scope=pdb_pred.SCOPE_PRIVATE)
    _forecast("NVDA", "STRONG_SELL", 5, role="challenger")
    view = pv.build_position_view_for_owner("alice", "NVDA")
    assert [f["label"] for f in view["forecast"]["horizons"]] == ["HOLD"] and view["relation"] == pv.REL_NEUTRAL


def test_relation_without_a_forecast_or_with_mixed_horizons():
    alice = _person("alice")
    _own(alice, "NVDA")
    assert pv.build_position_view_for_owner("alice", "NVDA")["relation"] == pv.REL_NO_FORECAST
    _forecast("NVDA", "BUY", 5)
    _forecast("NVDA", "SELL", 21)
    view = pv.build_position_view_for_owner("alice", "NVDA")
    assert view["forecast"]["direction"] == "mixed" and view["relation"] == pv.REL_MIXED


def test_cost_basis_unknown_is_reported_as_unknown_not_zero():
    alice = _person("alice")
    _own(alice, "NVDA", avg_cost=None)
    view = pv.build_position_view_for_owner("alice", "NVDA")
    assert view["position"]["cost_basis_total"] is None and view["position"]["unrealized_gain_pct"] is None
    assert "Cost basis is not known." in view["summary"]


def test_each_owner_sees_only_their_own_position():
    alice, bob = _person("alice"), _person("bob")
    _own(alice, "NVDA", shares=50.0)
    _own(bob, "NVDA", shares=7.0, avg_cost=90.0)
    assert pv.build_position_view_for_owner("alice", "NVDA")["position"]["shares"] == 50.0
    assert pv.build_position_view_for_owner("bob", "NVDA")["position"]["shares"] == 7.0
    assert pv.build_position_view(bob, "NVDA")["position"]["cost_basis_total"] == 630.0


def test_the_checked_entry_point_refuses_a_stranger():
    from portfolio_agent.services.context import RequestContext, _mint_identity_for_tests
    stranger = RequestContext(identity=_mint_identity_for_tests("stranger"), source="test")
    with pytest.raises(account_service.NotAuthorized):
        pv.build_position_view(stranger, "NVDA")


# ── The morning-batch phase ───────────────────────────────────────────────────

def _rows() -> list[sqlite3.Row]:
    with sqlite3.connect(str(db_module.DB_PATH)) as c:
        c.row_factory = sqlite3.Row
        return c.execute("SELECT * FROM position_views ORDER BY owner, ticker").fetchall()


def test_the_batch_stores_each_owners_views_with_one_audit_event_and_refreshes_on_rerun():
    from datetime import date
    alice, bob = _person("alice"), _person("bob")
    _own(alice, "NVDA"); _own(alice, "AAPL", sector="Technology")
    _own(bob, "NVDA", shares=7.0)
    _forecast("NVDA", "BUY", 5)
    r = pvs.snapshot_all_owners(today=date(2026, 10, 12))
    assert (r["created"], r["failed"]) == (3, 0) and r["owners"] >= 2          # the default local owner has a portfolio but no holdings
    assert [(x["owner"], x["ticker"]) for x in _rows()] == [("alice", "AAPL"), ("alice", "NVDA"), ("bob", "NVDA")]
    r2 = pvs.snapshot_all_owners(today=date(2026, 10, 12))
    assert (r2["created"], r2["updated"]) == (0, 3) and len(_rows()) == 3
    audits = [e for e in repo.get_audit_events() if e["operation"] == "save_position_views"]
    assert len(audits) == 2                                                  # one per owner, none for the same-day refresh
    assert all(e["actor"] == "system:position_views" for e in audits)


def test_a_stored_view_is_readable_only_by_its_owner():
    from datetime import date
    from portfolio_agent.services.context import RequestContext, _mint_identity_for_tests
    alice = _person("alice")
    _own(alice, "NVDA")
    pvs.snapshot_all_owners(today=date(2026, 10, 12))
    assert pvs.get_latest_view(alice, "NVDA")["view"]["position"]["shares"] == 50.0
    bob = _person("bob")                                                     # a portfolio, but no NVDA
    assert pvs.get_latest_view(bob, "NVDA") is None
    stranger = RequestContext(identity=_mint_identity_for_tests("stranger"), source="test")
    with pytest.raises(account_service.NotAuthorized):
        pvs.get_latest_view(stranger, "NVDA")
    assert pdb.get_latest("bob", "NVDA") is None and pdb.latest_before("alice", "NVDA", "2026-10-12") is None


# ── The chat: tool and card ───────────────────────────────────────────────────

def _run_tool(owner, ctx, **args):
    from web.chat.engine_tools import run_engine_tool
    return run_engine_tool("get_position_view", args, owner, ctx)


def test_the_chat_tool_returns_the_owners_view_and_the_previous_stored_one():
    from datetime import date, timedelta
    alice = _person("alice")
    _own(alice, "NVDA")
    _forecast("NVDA", "HOLD", 5)
    pvs.snapshot_all_owners(today=date.today() - timedelta(days=1))          # yesterday's stored view
    out = _run_tool("alice", alice, ticker="nvda")
    assert out["held"] is True and out["view"]["position"]["shares"] == 50.0
    assert out["previous_stored_view"]["forecast_labels"] == ["HOLD"]
    assert "Information only" in out["note"]


def test_the_chat_tool_says_so_for_a_ticker_not_held_and_refuses_another_actor():
    alice, bob = _person("alice"), _person("bob")
    _own(alice, "NVDA")
    assert _run_tool("alice", alice, ticker="AAPL")["held"] is False
    assert _run_tool("alice", bob, ticker="NVDA").get("held") is None         # a context that is not the owner's: refused
    assert "no_account" in str(_run_tool(None, alice, ticker="NVDA")).lower() or "account" in str(_run_tool(None, alice, ticker="NVDA")).lower()
    assert _run_tool("alice", alice, ticker="")["note"] == "A ticker is required."


def test_the_model_cannot_pick_another_owner_through_arguments():
    alice, bob = _person("alice"), _person("bob")
    _own(alice, "NVDA", shares=50.0); _own(bob, "NVDA", shares=7.0)
    from web.chat.engine_tools import run_engine_tool
    out = run_engine_tool("get_position_view", {"ticker": "NVDA", "owner": "bob", "portfolio_id": 99}, "alice", alice)
    assert out["view"]["position"]["shares"] == 50.0                          # the server-side owner wins; model-supplied ids are dropped


def _capture_card(monkeypatch, ctx):
    import web.auth as auth
    import web.chat.rendering as rendering
    monkeypatch.setattr(auth, "current_context", lambda source: ctx)
    seen = {"write": [], "warning": [], "caption": [], "metric": []}

    class _Col:
        def metric(self, label, value):
            seen["metric"].append((label, value))

    monkeypatch.setattr(rendering.st, "markdown", lambda *a, **k: None)
    monkeypatch.setattr(rendering.st, "columns", lambda n: [_Col() for _ in range(n)])
    for kind in ("write", "warning", "caption"):
        monkeypatch.setattr(rendering.st, kind, lambda msg, _k=kind, **kw: seen[_k].append(msg))
    return rendering, seen


def test_the_card_shows_the_position_next_to_the_forecast_and_flags_an_exceeded_limit(monkeypatch):
    alice = _person("alice")
    _own(alice, "NVDA")
    _forecast("NVDA", "SELL", 5)
    rendering, seen = _capture_card(monkeypatch, alice)
    rendering.render_position_view("NVDA")
    assert "Shared forecast label: SELL (5d)" in seen["write"][0]
    assert ("Shares", "50") in seen["metric"] and ("Unrealized", "+66.7%") in seen["metric"]
    assert seen["warning"] and "position concentration" in seen["warning"][0]
    assert "never sees your holdings" in seen["caption"][0] and "not investment advice" in seen["caption"][0]


def test_the_card_shows_nothing_for_a_ticker_not_held(monkeypatch):
    alice = _person("alice")
    _own(alice, "NVDA")
    rendering, seen = _capture_card(monkeypatch, alice)
    rendering.render_position_view("AAPL")
    assert seen == {"write": [], "warning": [], "caption": [], "metric": []}
