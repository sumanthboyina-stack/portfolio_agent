"""
web/data/predictions.py — repro tests for the Predictions-page review finding:

  #1 (critical) _load_predictions() had no scope filter at all, so a private
     chat forecast (scope='private', owner_scope='user:<owner>') showed up on
     every viewer's Predictions page — and, since "latest per ticker+horizon"
     deduped by created_at across ALL rows, a private forecast could silently
     replace the shared call for everyone if it happened to be more recent.
     Fixed: scope_clause(SHARED_SCOPES) always applies; owner's own private
     rows are additionally included (and deduped separately, never against
     shared rows) only when `owner` is passed.
  #2 _personal_overlay() computes the deterministic per-viewer badges (held +
     weight, restricted, blackout, needs pre-clearance) the Action Items fix
     is built on, reusing clearance.py's own evaluate_blackout_windows and
     restricted_list_db — never re-deriving or rewriting the recommendation.
"""
from __future__ import annotations

import pandas as pd
import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.services import account_service
from portfolio_agent.services.context import RequestContext, _mint_identity_for_tests, local_context
from portfolio_agent.tools.prediction_db import SCOPE_PRIVATE, SCOPE_SHARED
from web.data.predictions import _load_predictions, _personal_overlay


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    import web.lib as weblib
    monkeypatch.setattr(weblib, "_DB", db_path)


def _insert_prediction(scope: str, *, ticker: str = "AAPL", owner_scope: str | None = None,
                       horizon_days: int = 5, created_at: str | None = None, **overrides) -> None:
    from portfolio_agent.tools.prediction_db import _db as prediction_db_conn
    row = {
        "ticker": ticker, "prediction": "BULLISH", "horizon_days": horizon_days,
        "as_of_date": "2026-09-28", "evaluation_status": "pending",
        "recommendation": "BUY", "scope": scope, "owner_scope": owner_scope,
    }
    row.update(overrides)
    with prediction_db_conn() as conn:
        cols = list(row)
        created_clause = "?" if created_at else "datetime('now')"
        params = list(row.values()) + ([created_at] if created_at else [])
        conn.execute(
            f"INSERT INTO predictions ({', '.join(cols)}, created_at) VALUES "
            f"({', '.join('?' for _ in cols)}, {created_clause})",
            params,
        )
        conn.commit()


# ── #1: scope filtering ───────────────────────────────────────────────────────

def test_load_predictions_excludes_other_users_private_forecasts_by_default():
    _insert_prediction(SCOPE_SHARED, ticker="AAPL")
    _insert_prediction(SCOPE_PRIVATE, ticker="TSLA", owner_scope="user:bob")

    df = _load_predictions()
    assert set(df["ticker"]) == {"AAPL"}


def test_load_predictions_never_lets_a_private_forecast_replace_the_shared_one():
    """The bug this guards against: 'latest per ticker+horizon' deduped by
    created_at across every scope, so a more recent private chat forecast
    could silently hide the shared call for that ticker/horizon from
    everyone else looking at the Predictions page."""
    _insert_prediction(SCOPE_SHARED, ticker="AAPL", recommendation="BUY", created_at="2026-09-28T09:00:00")
    _insert_prediction(SCOPE_PRIVATE, ticker="AAPL", owner_scope="user:bob",
                       recommendation="SELL", created_at="2026-09-28T14:00:00")   # later, but private

    df = _load_predictions()
    assert len(df) == 1
    assert df.iloc[0]["recommendation"] == "BUY"   # the shared call, untouched


def test_load_predictions_includes_the_viewers_own_private_forecast_when_owner_given():
    _insert_prediction(SCOPE_SHARED, ticker="AAPL", recommendation="BUY")
    _insert_prediction(SCOPE_PRIVATE, ticker="AAPL", owner_scope="user:alice", recommendation="SELL")
    _insert_prediction(SCOPE_PRIVATE, ticker="TSLA", owner_scope="user:bob", recommendation="HOLD")

    df = _load_predictions(owner="alice")

    # Both alice's own private AAPL row AND the shared AAPL row appear --
    # neither displaces the other.
    aapl = df[df["ticker"] == "AAPL"]
    assert len(aapl) == 2
    assert set(aapl["recommendation"]) == {"BUY", "SELL"}
    assert sorted(aapl["is_own_private"]) == [False, True]

    # Bob's private TSLA row never shows up for alice.
    assert "TSLA" not in set(df["ticker"])


def test_load_predictions_marks_is_own_private_false_for_shared_rows():
    _insert_prediction(SCOPE_SHARED, ticker="AAPL")
    df = _load_predictions(owner="alice")
    assert df.iloc[0]["is_own_private"] == False  # noqa: E712


# ── #2: personal overlay (held/weight, restricted, blackout, pre-clearance) ──

def _seed_holding(ctx, ticker: str, current_value: float) -> None:
    acct = account_service.create_account(ctx, broker="manual", display_name=f"{ticker} account")
    account_service.add_position(ctx, acct["account_id"], {
        "ticker": ticker, "shares": 1, "avg_cost": current_value, "cost_basis_total": current_value,
        "current_price": current_value, "current_value": current_value,
    })


def test_personal_overlay_reports_held_and_weight():
    ctx = local_context("test")
    _seed_holding(ctx, "AAPL", 800.0)
    _seed_holding(ctx, "TSLA", 200.0)

    overlay = _personal_overlay(ctx, ["AAPL", "TSLA", "NVDA"])

    assert overlay["AAPL"]["held"] is True
    assert overlay["AAPL"]["weight_pct"] == pytest.approx(80.0)
    assert overlay["TSLA"]["weight_pct"] == pytest.approx(20.0)
    assert overlay["NVDA"]["held"] is False
    assert overlay["NVDA"]["weight_pct"] is None


def test_personal_overlay_reports_restricted_from_the_viewers_own_list():
    from portfolio_agent.tools.restricted_list_db import add_restricted
    ctx = local_context("test")
    add_restricted("AAPL", "employer conflict", owner=ctx.actor)

    overlay = _personal_overlay(ctx, ["AAPL", "MSFT"])
    assert overlay["AAPL"]["restricted"] is True
    assert overlay["AAPL"]["restricted_reason"] == "employer conflict"
    assert overlay["MSFT"]["restricted"] is False


def test_personal_overlay_does_not_leak_a_different_owners_restricted_list():
    from portfolio_agent.tools.restricted_list_db import add_restricted
    add_restricted("AAPL", "bob's own reason", owner="bob")

    ctx = local_context("test")
    overlay = _personal_overlay(ctx, ["AAPL"])
    assert overlay["AAPL"]["restricted"] is False


def test_personal_overlay_reports_active_blackout_window():
    from portfolio_agent.tools.blackout_windows_db import add_blackout_window
    add_blackout_window(scope="ticker:AAPL", start_date=None, end_date=None, source="manual:test")

    ctx = local_context("test")
    overlay = _personal_overlay(ctx, ["AAPL", "MSFT"])
    assert overlay["AAPL"]["blackout"] is True
    assert overlay["MSFT"]["blackout"] is False


def test_personal_overlay_reports_needs_pre_clearance_from_the_viewers_own_profile():
    from portfolio_agent.tools.user_profile_db import update_user_profile_for
    ctx = local_context("test")
    update_user_profile_for(ctx, pre_clearance_required=True)

    overlay = _personal_overlay(ctx, ["AAPL"])
    assert overlay["AAPL"]["needs_pre_clearance"] is True

    update_user_profile_for(ctx, pre_clearance_required=False)
    overlay = _personal_overlay(ctx, ["AAPL"])
    assert overlay["AAPL"]["needs_pre_clearance"] is False
