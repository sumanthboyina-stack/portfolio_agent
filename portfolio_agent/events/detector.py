"""
Event detector — identifies market events that should trigger predictions.

Each detector reads existing data sources (news_filter_log, SEC filings, etc.)
and writes rows to trigger_events via events/db.py.  No new LLM calls are made
here — detectors are pure data readers + writers.

Detectors:
  detect_material_news(tickers, today, config)  — reads news_filter_log stage_3_score
  detect_earnings(tickers, today, config)        — stub (future: earnings calendar API)
  detect_sec_8k(tickers, today, config)          — stub (future: SEC EDGAR RSS)
  detect_rating_changes(tickers, today, config)  — stub (future: Finnhub upgrades)
  detect_macro_events(today, config)             — stub (future: macro calendar API)

run_all_detectors(tickers, today, config) — convenience wrapper.
"""

from __future__ import annotations

from portfolio_agent.log import get_logger as _get_logger

_log = _get_logger("events.detector")


def detect_material_news(
    tickers: list[str],
    today: str,
    config: dict,
) -> list[dict]:
    """
    Read today's news_filter_log rows and emit trigger_events for any ticker
    whose stage_3_score >= intraday_severity_threshold.

    Returns list of event dicts with keys: ticker, event_type, severity, source, summary.
    """
    from portfolio_agent.tools.db import db_conn
    from portfolio_agent.events.db import insert_event

    threshold = config.get("intraday_severity_threshold", 3)
    if not config.get("detectors", {}).get("material_news", True):
        return []

    events: list[dict] = []
    try:
        with db_conn() as c:
            rows = c.execute(
                """SELECT ticker, stage_3_score, stage_3_summary, final_decision
                   FROM news_filter_log
                   WHERE as_of_date = ?
                   AND stage_3_score IS NOT NULL
                   AND stage_3_score >= ?
                   AND final_decision = 'analyzed'""",
                [today, threshold],
            ).fetchall()
    except Exception as exc:
        _log.warning(f"  [events] news_filter_log query failed: {exc}", event_type="warning")
        return []

    for row in rows:
        ticker = row["ticker"]
        if ticker not in {t.upper() for t in tickers}:
            continue
        score = row["stage_3_score"]
        summary = row["stage_3_summary"] or f"Material news score={score}"
        ev_id = insert_event(
            ticker=ticker,
            event_type="material_news",
            severity=int(score),
            source="news_filter_log",
            summary=summary,
        )
        events.append({
            "id": ev_id,
            "ticker": ticker,
            "event_type": "material_news",
            "severity": int(score),
            "source": "news_filter_log",
            "summary": summary,
        })
        _log.info(
            f"  [event] {ticker} material_news severity={score}: {summary[:60]}",
            event_type="event_detected", ticker=ticker,
        )

    return events


def detect_earnings(tickers: list[str], today: str, config: dict) -> list[dict]:
    """Stub — future: query earnings calendar API for surprise announcements."""
    if not config.get("detectors", {}).get("earnings", True):
        return []
    return []


def detect_sec_8k(tickers: list[str], today: str, config: dict) -> list[dict]:
    """Stub — future: poll SEC EDGAR RSS feed for 8-K filings."""
    if not config.get("detectors", {}).get("sec_8k", True):
        return []
    return []


def detect_rating_changes(tickers: list[str], today: str, config: dict) -> list[dict]:
    """Stub — future: Finnhub upgrade/downgrade endpoint."""
    if not config.get("detectors", {}).get("rating_changes", True):
        return []
    return []


def detect_macro_events(today: str, config: dict) -> list[dict]:
    """Stub — future: macro economic calendar (FOMC, CPI, NFP)."""
    if not config.get("detectors", {}).get("macro_events", True):
        return []
    return []


def run_all_detectors(
    tickers: list[str],
    today: str,
    config: dict,
) -> list[dict]:
    """
    Run all enabled detectors and return the combined list of detected events.
    Events are also persisted to trigger_events via insert_event().
    """
    if not config.get("enabled", True):
        _log.info("  [events] event_driven disabled in config — skipping detectors",
                  event_type="info")
        return []

    all_events: list[dict] = []
    all_events.extend(detect_material_news(tickers, today, config))
    all_events.extend(detect_earnings(tickers, today, config))
    all_events.extend(detect_sec_8k(tickers, today, config))
    all_events.extend(detect_rating_changes(tickers, today, config))
    all_events.extend(detect_macro_events(today, config))

    _log.info(
        f"  [events] Detected {len(all_events)} event(s) for {len(tickers)} tickers",
        event_type="summary",
    )
    return all_events
