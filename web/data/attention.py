"""
Critical-item lookup for surfaces outside the dashboard -- currently just
Chat's hero-screen nudge.

Mirrors the CRITICAL tier of web/app.py::_build_queue but standalone: the
dashboard's full _load() + _build_queue also pulls macro, validation,
watchlist and trending-opportunity data the hero nudge doesn't need, and
importing web/app.py directly isn't safe (it calls st.set_page_config() at
import time).
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from portfolio_agent.tools.db import db_conn


def get_critical_nudge_items(limit: int = 3) -> list[dict]:
    """
    Return up to *limit* critical items across held positions: unprocessed
    severity-3 events, SELL/STRONG_SELL recommendations, >20% single-name
    concentration, and active 'risk' flags from the risk_flags table. Each
    item carries a `query` string meant to be dropped straight into Chat.
    """
    items: list[dict] = []
    with db_conn() as c:
        holdings = [dict(r) for r in c.execute("""
            SELECT ticker, SUM(COALESCE(current_value,0)) value
            FROM holdings GROUP BY ticker
        """).fetchall()]
        if not holdings:
            return []
        hset = {h["ticker"] for h in holdings}
        tv = sum(h["value"] for h in holdings) or 1.0
        weight = {h["ticker"]: h["value"] / tv * 100 for h in holdings}

        ph = ",".join("?" * len(hset))
        preds = {
            r["ticker"]: dict(r)
            for r in c.execute(f"""
                SELECT p.ticker, p.recommendation, p.confidence
                FROM predictions p
                JOIN (SELECT ticker, MAX(created_at) dt FROM predictions
                      WHERE ticker IN ({ph}) GROUP BY ticker) x
                  ON p.ticker=x.ticker AND p.created_at=x.dt
            """, list(hset)).fetchall()
        }

        ago48 = (datetime.now() - timedelta(hours=48)).isoformat()
        events = [dict(r) for r in c.execute("""
            SELECT ticker, event_type, summary
            FROM trigger_events
            WHERE detected_at >= ? AND severity = 3 AND processed = 0
            ORDER BY detected_at DESC
        """, [ago48]).fetchall()]

    for ev in events:
        if ev["ticker"] in hset:
            items.append({
                "ticker": ev["ticker"],
                "title": ev.get("summary") or f"Severity-3 {ev['event_type'].replace('_', ' ')}",
                "query": (
                    f"There's an unprocessed severity-3 event on {ev['ticker']} -- "
                    f"analyze it and tell me if it changes the thesis."
                ),
            })

    for t, p in preds.items():
        if p.get("recommendation") in ("SELL", "STRONG_SELL"):
            items.append({
                "ticker": t,
                "title": f"{p['recommendation']} on {t} ({weight.get(t, 0):.2f}% of portfolio)",
                "query": f"Why is APEX recommending {p['recommendation']} on {t}? Walk me through the reasoning.",
            })

    for t, w in weight.items():
        if w > 20:
            items.append({
                "ticker": t,
                "title": f"{t} is {w:.2f}% of your portfolio",
                "query": f"How concentrated am I in {t} and what's the risk?",
            })

    try:
        from portfolio_agent.tools.risk_flags_db import get_active_flags
        for rf in get_active_flags(None):
            if rf["source"] == "risk" and rf["ticker"] in hset:
                items.append({
                    "ticker": rf["ticker"],
                    "title": f"Risk flag on {rf['ticker']} -- score {rf.get('score', '?')}/10",
                    "query": f"What's the risk flag on {rf['ticker']} about, and should I be worried?",
                })
    except Exception:
        pass

    return items[:limit]
