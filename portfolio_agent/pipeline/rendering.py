"""
Terminal display helpers for the interactive CLI.

Only used by runner.py / main.py for single-ticker analysis output.
Daily batch functions have their own format helpers in daily.py.
"""

from __future__ import annotations

import json

from portfolio_agent.log import get_logger as _get_logger

_log = _get_logger("cli")


def print_news(data: dict) -> None:
    """Rich terminal display for the news state key."""
    _log.info(f"\n  Sentiment : {data.get('sentiment')}  ({data.get('sentiment_score', 0):+.2f})",
              event_type="summary")
    _log.info(f"  Date      : {data.get('date', 'today')}", event_type="summary")
    if data.get("headline_1"):
        _log.info(f"\n  ► {data['headline_1']}", event_type="summary")
    if data.get("headline_2"):
        _log.info(f"  ► {data['headline_2']}", event_type="summary")
    themes = data.get("top_themes", [])
    if themes:
        _log.info(f"\n  Themes    : {', '.join(themes)}", event_type="summary")
    cats = data.get("upcoming_catalysts", [])
    if cats:
        _log.info("\n  Catalysts :", event_type="summary")
        for c in cats:
            _log.info(f"    [{c.get('impact','?')}] {c.get('event')} — {c.get('date','')}",
                      event_type="summary")
    risks = data.get("macro_risks", [])
    if risks:
        _log.info(f"\n  Macro risks: {'; '.join(risks[:3])}", event_type="summary")
    tr = data.get("trending", {})
    if isinstance(tr, dict) and tr:
        _log.info(f"\n  ┌─ TRENDING ({tr.get('days_of_history', 0)}d history) {'─' * 30}",
                  event_type="summary")
        _log.info(f"  │  Direction : {tr.get('trend_direction', 'N/A')}"
                  f"  |  7d avg score: {tr.get('sentiment_7d_avg') or 'n/a'}",
                  event_type="summary")
        new_t = tr.get("new_themes_today", [])
        if new_t:
            _log.info(f"  │  New today : {', '.join(new_t)}", event_type="summary")
        fade = tr.get("fading_themes", [])
        if fade:
            _log.info(f"  │  Fading    : {', '.join(fade)}", event_type="summary")
        if tr.get("momentum_summary"):
            _log.info(f"  │\n  │  {tr['momentum_summary']}", event_type="summary")
        _log.info("  └" + "─" * 44, event_type="summary")
    ctx = data.get("market_context")
    if ctx:
        _log.info(f"\n  Market: {ctx}", event_type="summary")
    summ = data.get("summary")
    if summ:
        _log.info(f"\n  {summ}", event_type="summary")
    saved = data.get("saved_to_db")
    if saved is not None:
        status = "✓ saved" if saved else "✗ not saved"
        _log.info(f"\n  DB: {status}", event_type="summary")


def pretty_print(ticker: str, agent_name: str, state: dict) -> None:
    """Print a human-readable summary of the analysis."""

    def _section(title: str, key: str) -> None:
        raw = state.get(key)
        if not raw:
            return
        _log.info(f"\n── {title} {'─' * (48 - len(title))}", event_type="summary")
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
            if key == "news" and isinstance(data, dict):
                print_news(data)
                return
            if isinstance(data, dict):
                summary = data.get("summary") or data.get("memo")
                if summary:
                    _log.info(f"  {summary}", event_type="summary")
                for k, v in data.items():
                    if k in ("summary", "memo", "ticker", "date"):
                        continue
                    if not isinstance(v, (dict, list)):
                        _log.info(f"  {k}: {v}", event_type="summary")
            else:
                _log.info(raw, event_type="summary")
        except Exception:
            _log.info(raw, event_type="summary")

    if agent_name == "all":
        _section("Fundamentals",   "fundamentals")
        _section("News",           "news")
        _section("Macro",          "macro")
        _section("Technical",      "technical")
        _section("Research",       "research")
        _section("Risk",           "risk")
        _section("Recommendation", "recommendation")
        _section("Clearance",      "clearance")
    else:
        key_map = {
            "fundamentals": "fundamentals",
            "news":         "news",
            "macro":        "macro",
            "technical":    "technical",
            "research":     "research",
            "risk":         "risk",
            "synthesis":    "recommendation",
            "clearance":    "clearance",
            "reasoning":    "apex_prediction",
        }
        _section(agent_name.title(), key_map.get(agent_name, agent_name))
