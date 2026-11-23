"""
Pipeline health notifications — category SYSTEM.

A batch run whose forecast phase mostly failed (see progress_tracker.degraded_phase) is recorded as "degraded" and announced to
every person who has a portfolio, once per run (dedup key pipeline_degraded:<run_id>).  The text says what failed and where to
look; it carries no tickers, amounts or positions.  Best-effort: never raises into the batch that called it.
"""
from __future__ import annotations

from portfolio_agent.log import get_logger as _get_logger

_log = _get_logger("services.pipeline_health")

_SEVERITY = 4


def notify_degraded_run(run_id: str, job_type: str, *, phase: str, failed: int, total: int) -> dict:
    """Enqueue one SYSTEM notification per portfolio owner. Returns {"owners": n, "enqueued": n}."""
    out = {"owners": 0, "enqueued": 0}
    try:
        from portfolio_agent.services.notification_service import notify
        from portfolio_agent.tools import holdings_db as repo
        title = f"APEX {job_type} batch finished with failures"
        brief = (f"APEX: the {job_type} batch finished, but {failed} of {total} forecasts failed. "
                 f"This is usually a model provider out of credit or rate-limited - check provider balances.")
        detailed = (brief + f" Run {run_id}. Failed forecasts were not saved, so the affected tickers have no new forecast "
                    f"from this run. Models marked out of credit are skipped for 10 minutes and then tried again.")
        for owner in repo.list_portfolio_owners():
            out["owners"] += 1
            r = notify(owner, "SYSTEM", dedup_key=f"pipeline_degraded:{run_id}", title=title, brief=brief,
                       detailed=detailed, severity=_SEVERITY)
            if r.get("enqueued"):
                out["enqueued"] += 1
    except Exception as exc:
        _log.warning(f"  [pipeline-health] degraded-run alert failed: {type(exc).__name__}: {exc}", event_type="warning")
    return out
