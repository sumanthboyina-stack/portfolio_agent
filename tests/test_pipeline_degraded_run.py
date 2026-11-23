"""A run whose APEX phase mostly failed finishes as 'degraded' (still counted as finished) and tells each portfolio owner once."""
from __future__ import annotations

import pytest

import portfolio_agent.tools.progress_tracker as pt
from portfolio_agent.tools import notification_outbox_db as outbox
from tests.test_notification_producers import _env, _person  # noqa: F401


def _run(run_id, completed, failed, job="morning", status="completed"):
    t = pt.PipelineProgressTracker(run_id, job)
    t.start_phase("apex", total=completed + failed)
    t.finish_phase("apex", completed, failed)
    t.finish_run(status)
    return t


def _status(run_id):
    with pt._get_conn() as c:
        return c.execute("SELECT status FROM pipeline_runs WHERE run_id = ?", (run_id,)).fetchone()["status"]


def test_a_run_where_most_forecasts_failed_is_degraded():
    _run("r1", 0, 32)
    assert _status("r1") == "degraded"
    _run("r2", 16, 16)
    assert _status("r2") == "degraded"                               # exactly at the share


def test_a_mostly_successful_or_tiny_run_stays_completed():
    _run("r3", 30, 2)
    _run("r4", 0, 4)                                                 # fewer than 5 attempts: not enough to judge
    _run("r5", 0, 0)                                                 # "no predictions needed today"
    assert [_status(r) for r in ("r3", "r4", "r5")] == ["completed"] * 3


def test_an_errored_run_keeps_its_error_status():
    _run("r6", 0, 32, status="error")
    assert _status("r6") == "error"


def test_degraded_counts_as_finished_for_the_morning_dependency():
    _run("r7", 0, 32)
    assert pt.has_completed_today("morning") is True


def test_each_portfolio_owner_is_told_once_and_a_rerun_does_not_repeat_it():
    _person("alice"); _person("bob")
    _run("r8", 0, 32)
    rows = [r for o in ("alice", "bob") for r in outbox.list_recent(o, 20) if r["dedup_key"] == "pipeline_degraded:r8"]
    assert {r["owner"] for r in rows} == {"alice", "bob"}
    assert all("0 of" not in r["body_brief"] and "32 of 32" in r["body_brief"] for r in rows)
    pt.PipelineProgressTracker("r8", "morning").finish_run("completed")      # same run id: deduplicated
    again = [r for r in outbox.list_recent("alice", 20) if r["dedup_key"] == "pipeline_degraded:r8"]
    assert len(again) == len(rows) // 2
