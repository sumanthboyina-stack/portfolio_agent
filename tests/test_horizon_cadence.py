"""Horizon cadence in the database (Step 1.3): engine_settings.horizon_cadence_json drives the scheduler; admin-only edits."""
from __future__ import annotations

import sqlite3
from datetime import date

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.config import get_event_driven_config
from portfolio_agent.events.scheduler import get_scheduled_horizons_v2
from portfolio_agent.services import investor_service as svc
from portfolio_agent.services.context import RequestContext, _mint_identity_for_tests, local_context
from portfolio_agent.tools import auth_users_db
from portfolio_agent.tools import engine_settings_db as esdb

MON_NOT_FIRST = date(2026, 9, 21)      # Monday, mid-month
FIRST_OF_Q = date(2026, 10, 1)         # Thursday, first trading day of Q4
FIRST_OF_MONTH = date(2026, 11, 2)     # Monday, first trading day of November (not a quarter start)
TUE = date(2026, 9, 22)


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")


def _admin():
    ctx = local_context("test")
    auth_users_db.invite_user(f"{ctx.actor}@example.com", ctx.actor, "admin")
    return ctx


def test_seed_cadence_equals_the_previous_hard_coded_schedule():
    assert esdb.get_engine_settings()["horizon_cadence"] == {
        5: "weekly_monday", 21: "weekly_monday", 63: "monthly_first_trading_day", 250: "quarterly_first_trading_day"}
    assert get_event_driven_config()["scheduled_cadence"]["horizon_250d"] == "quarterly_first_trading_day"


def test_scheduler_follows_the_default_cadence():
    cfg = get_event_driven_config()
    assert get_scheduled_horizons_v2(MON_NOT_FIRST, cfg) == [5, 21]
    assert get_scheduled_horizons_v2(TUE, cfg) == []
    assert get_scheduled_horizons_v2(FIRST_OF_Q, cfg) == [63, 250]
    assert get_scheduled_horizons_v2(FIRST_OF_MONTH, cfg) == [5, 21, 63]       # Monday + first of month, not a quarter start


def test_changing_the_db_changes_the_schedule_on_the_next_read():
    ctx = _admin()
    svc.update_horizon_cadence(ctx, {250: "monthly_first_trading_day"})        # the 250d monthly option
    assert get_scheduled_horizons_v2(FIRST_OF_MONTH, get_event_driven_config()) == [5, 21, 63, 250]
    svc.update_horizon_cadence(ctx, {21: "never", 5: "monthly_first_trading_day"})
    cfg = get_event_driven_config()
    assert get_scheduled_horizons_v2(MON_NOT_FIRST, cfg) == []                  # 21d never, 5d now monthly
    assert 21 not in get_scheduled_horizons_v2(FIRST_OF_Q, cfg)


def test_never_for_every_horizon_schedules_nothing():
    svc.update_horizon_cadence(_admin(), {h: "never" for h in (5, 21, 63, 250)})
    for d in (MON_NOT_FIRST, FIRST_OF_Q, FIRST_OF_MONTH):
        assert get_scheduled_horizons_v2(d, get_event_driven_config()) == []


def test_validation_rejects_unknown_rule_and_horizon_and_empty():
    ctx = _admin()
    for bad in ({5: "daily"}, {7: "weekly_monday"}, {}, {"x": "never"}):
        with pytest.raises(ValueError):
            svc.update_horizon_cadence(ctx, bad)
    assert esdb.get_engine_settings()["horizon_cadence"][5] == "weekly_monday"      # nothing half-applied


def test_admin_only_audited_versioned_and_reset():
    ctx = _admin()
    stranger = RequestContext(identity=_mint_identity_for_tests("nobody"), source="test")
    with pytest.raises(Exception):
        svc.update_horizon_cadence(stranger, {5: "never"})
    v = esdb.get_engine_settings()["version"]
    out = svc.update_horizon_cadence(ctx, {63: "weekly_monday"}, expected_version=v)
    assert out[63] == "weekly_monday" and esdb.get_engine_settings()["version"] == v + 1
    with pytest.raises(RuntimeError):                                           # stale version
        svc.update_horizon_cadence(ctx, {63: "never"}, expected_version=v)
    from portfolio_agent.tools import holdings_db as repo
    assert "update_horizon_cadence" in [e["operation"] for e in repo.get_audit_events(limit=50)]
    assert svc.reset_horizon_cadence(ctx) == esdb.SEED_HORIZON_CADENCE
    assert esdb.get_engine_settings()["horizon_cadence"] == esdb.SEED_HORIZON_CADENCE


def test_migration_is_idempotent_and_an_unreadable_value_falls_back_to_the_seed():
    esdb.get_engine_settings()
    esdb.get_engine_settings()
    with sqlite3.connect(db_module.DB_PATH) as c:
        c.execute("UPDATE engine_settings SET horizon_cadence_json = '{\"5\": \"bogus\"}'")
    got = esdb.get_engine_settings()["horizon_cadence"]
    assert got == esdb.SEED_HORIZON_CADENCE                                     # bogus rule and missing horizons -> seed
    with sqlite3.connect(db_module.DB_PATH) as c:
        c.execute("UPDATE engine_settings SET horizon_cadence_json = 'not json'")
    assert esdb.get_engine_settings()["horizon_cadence"] == esdb.SEED_HORIZON_CADENCE


def test_scheduler_config_falls_back_to_seed_if_settings_unreadable(monkeypatch):
    monkeypatch.setattr(esdb, "scheduled_cadence", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    assert get_event_driven_config()["scheduled_cadence"]["horizon_63d"] == "monthly_first_trading_day"


def test_schedule_page_card_reads_the_db_cadence():
    """The Schedule page builds its Morning-card text from the stored cadence, not a literal."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "web" / "pages" / "2_🗓️_Schedule.py").read_text()
    assert "get_engine_settings()[\"horizon_cadence\"]" in src and "_cadence_text()" in src
    svc.update_horizon_cadence(_admin(), {250: "never"})
    assert esdb.CADENCE_LABELS["never"] in " · ".join(
        f"{h}d: {esdb.CADENCE_LABELS[r]}" for h, r in sorted(esdb.get_engine_settings()["horizon_cadence"].items()))
