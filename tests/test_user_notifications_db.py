from __future__ import annotations

from datetime import datetime, time

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.domain import UserNotificationPrefs
from portfolio_agent.events.scheduler import should_notify_trigger
from portfolio_agent.tools.user_notifications_db import (
    _db,
    get_user_notifications,
    in_quiet_hours,
    should_notify,
    update_user_notifications,
)


def test_defaults_seeded_on_first_access(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")

    prefs = get_user_notifications()
    assert isinstance(prefs, UserNotificationPrefs)
    assert prefs.channel == "none"
    assert prefs.enabled is False
    # matches config._DEFAULT_EVENT_DRIVEN["intraday_severity_threshold"]
    from portfolio_agent.config import get_event_driven_config
    assert prefs.min_severity_threshold == get_event_driven_config()["intraday_severity_threshold"]
    assert prefs.min_conviction_threshold is None
    assert prefs.quiet_hours_start is None and prefs.quiet_hours_end is None

    with _db() as c:
        assert c.execute("SELECT COUNT(*) FROM user_notifications").fetchone()[0] == 1


def test_update_round_trips_and_never_reseeds(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")

    before = get_user_notifications()
    prefs = update_user_notifications(
        channel="slack", min_severity_threshold=4, min_conviction_threshold=6,
        quiet_hours_start="22:00", quiet_hours_end="07:00",
    )
    assert prefs.channel == "slack" and prefs.enabled
    assert prefs.min_severity_threshold == 4
    assert prefs.min_conviction_threshold == 6
    assert (prefs.quiet_hours_start, prefs.quiet_hours_end) == ("22:00", "07:00")
    assert prefs.created_at == before.created_at

    for _ in range(3):
        again = get_user_notifications()
    assert again.to_dict() == prefs.to_dict()

    # clearing quiet hours with "" stores NULL
    cleared = update_user_notifications(quiet_hours_start="", quiet_hours_end="")
    assert cleared.quiet_hours_start is None and cleared.quiet_hours_end is None


def test_update_rejects_bad_input(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")

    with pytest.raises(ValueError, match="nope"):
        update_user_notifications(nope=1)
    with pytest.raises(ValueError, match="channel"):
        update_user_notifications(channel="pager")
    with pytest.raises(ValueError, match="HH:MM"):
        update_user_notifications(quiet_hours_start="ten pm")
    assert get_user_notifications().channel == "none"


def test_in_quiet_hours_same_day_overnight_and_disabled():
    assert in_quiet_hours(time(23, 0), "22:00", "07:00")
    assert in_quiet_hours(time(3, 0), "22:00", "07:00")
    assert not in_quiet_hours(time(12, 0), "22:00", "07:00")
    assert in_quiet_hours(time(13, 0), "12:00", "14:00")
    assert not in_quiet_hours(time(14, 0), "12:00", "14:00")   # end is exclusive
    assert not in_quiet_hours(time(13, 0), None, "14:00")
    assert not in_quiet_hours(time(13, 0), "12:00", "12:00")   # start == end → disabled
    assert not in_quiet_hours(time(13, 0), "bad", "14:00")


def _prefs(**kw) -> UserNotificationPrefs:
    base = dict(channel="email", min_severity_threshold=3)
    base.update(kw)
    return UserNotificationPrefs(**base)


def test_should_notify_channel_none_never_fires():
    ok, reason = should_notify(5, prefs=_prefs(channel="none"))
    assert not ok and "none" in reason


def test_should_notify_severity_threshold():
    noon = datetime(2026, 9, 21, 12, 0)
    assert should_notify(3, now=noon, prefs=_prefs()) == (True, "email")
    ok, reason = should_notify(2, now=noon, prefs=_prefs())
    assert not ok and "severity" in reason
    ok, _ = should_notify(None, now=noon, prefs=_prefs())
    assert not ok
    assert should_notify(4, now=noon, prefs=_prefs(min_severity_threshold=4))[0]
    assert not should_notify(3, now=noon, prefs=_prefs(min_severity_threshold=4))[0]


def test_should_notify_conviction_threshold_only_when_known():
    noon = datetime(2026, 9, 21, 12, 0)
    p = _prefs(min_conviction_threshold=7)
    assert should_notify(4, conviction=8, now=noon, prefs=p)[0]
    ok, reason = should_notify(4, conviction=5, now=noon, prefs=p)
    assert not ok and "conviction" in reason
    # conviction unknown at detection time → check skipped
    assert should_notify(4, conviction=None, now=noon, prefs=p)[0]


def test_should_notify_quiet_hours():
    p = _prefs(quiet_hours_start="22:00", quiet_hours_end="07:00")
    ok, reason = should_notify(5, now=datetime(2026, 9, 21, 23, 30), prefs=p)
    assert not ok and "quiet hours" in reason
    assert should_notify(5, now=datetime(2026, 9, 21, 9, 0), prefs=p) == (True, "email")


def test_should_notify_trigger_only_for_event_triggers(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    update_user_notifications(channel="slack", min_severity_threshold=3)
    noon = datetime(2026, 9, 21, 12, 0)

    assert should_notify_trigger("scheduled_weekly", severity=5, now=noon)[0] is False
    assert should_notify_trigger(None, severity=5, now=noon)[0] is False
    assert should_notify_trigger("event_material_news", severity=3, now=noon) == (True, "slack")
    assert should_notify_trigger("event_material_news", severity=2, now=noon)[0] is False


def test_detector_notification_hook_respects_filter(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    from portfolio_agent.events import detector

    emitted: list[tuple[str, dict]] = []
    monkeypatch.setattr(detector, "_emit", lambda ch, ev: emitted.append((ch, ev)))
    event = {"id": 1, "ticker": "AAPL", "event_type": "material_news", "severity": 4, "summary": "x"}

    # channel 'none' by default → suppressed
    assert detector._maybe_notify(event) is False
    assert emitted == []

    update_user_notifications(channel="email", min_severity_threshold=3,
                              quiet_hours_start=None, quiet_hours_end=None)
    assert detector._maybe_notify(event) is True
    assert emitted[-1][0] == "email"

    update_user_notifications(min_severity_threshold=5)
    assert detector._maybe_notify(event) is False
    assert len(emitted) == 1
