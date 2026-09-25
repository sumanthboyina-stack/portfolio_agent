"""Portfolio Intelligence — Daily Command Center."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import streamlit as st

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
load_dotenv()

from web.styles import (
    section_tile,
    inject_global_css, top_nav, section_title, ticker_label,
    REC_STYLES, SUCCESS, WARNING, DANGER, PRIMARY, NEUTRAL, PURPLE,
    SUCCESS_LIGHT, WARNING_LIGHT, DANGER_LIGHT, PRIMARY_LIGHT,
    freshness_color, accuracy_color, brier_color,
    icon_html, status_dot_html, fmt_money, fmt_pct,
)

_DB = _ROOT / "data" / "portfolio.db"

from web.auth import current_context, require_login

st.set_page_config(
    page_title="APEX — Dashboard",
    page_icon=str(_ROOT / "web" / "static" / "apex_mark.png"),
    layout="wide",
    initial_sidebar_state="collapsed",
)
inject_global_css()
require_login()
top_nav("dashboard")


# ── DB load (single pass, 120 s cache) ───────────────────────────────────────

@st.cache_data(ttl=120, show_spinner=False)
def _load() -> dict:
    if not _DB.exists():
        return {"no_db": True}
    from portfolio_agent.tools.db import db_conn

    ago2d   = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
    ago3d   = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
    stale10 = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")

    d: dict = {}
    with db_conn(_DB) as c:

        # ── Holdings ────────────────────────────────────────────────────────
        # SUM() skips NULL current_value on its own — an unpriced position stays
        # unknown (counted in n_unvalued) instead of silently becoming $0.
        # Cash is not in the holdings table at all, so this is securities only.
        cols = {r[1] for r in c.execute("PRAGMA table_info(holdings)").fetchall()}
        price_col = "MIN(price_as_of)" if "price_as_of" in cols else "NULL"
        rows = c.execute(f"""
            SELECT ticker,
                   SUM(shares) shares,
                   SUM(current_value) value,
                   SUM(CASE WHEN current_value IS NULL THEN 1 ELSE 0 END) n_unvalued,
                   COUNT(*) n_rows,
                   SUM(COALESCE(cost_basis_total, shares*avg_cost, 0)) cost_basis,
                   MIN(synced_at) oldest_sync,
                   MAX(synced_at) newest_sync,
                   {price_col} oldest_price
            FROM holdings GROUP BY ticker ORDER BY value DESC
        """).fetchall()
        holdings = [dict(r) for r in rows]
        tv = sum(h["value"] or 0 for h in holdings)
        tc = sum(h["cost_basis"] for h in holdings)
        for h in holdings:
            h["weight_pct"] = (h["value"] or 0) / tv * 100 if tv else 0
        d["holdings"]      = holdings
        d["total_value"]   = tv                      # securities only, valued rows only
        d["total_cost"]    = tc
        d["n_positions"]   = sum(h["n_rows"] for h in holdings)
        d["n_unvalued"]    = sum(h["n_unvalued"] for h in holdings)
        d["unreal_pct"]    = (tv - tc) / tc * 100 if tc else None
        d["holding_set"]   = {h["ticker"] for h in holdings}
        d["holding_map"]   = {h["ticker"]: h for h in holdings}
        # Freshness: "imported at" (newest import) and "prices as of" (oldest
        # price observation) are different questions — keep both.
        newest_import = max((h["newest_sync"] or "") for h in holdings) if holdings else ""
        oldest_price  = min((h["oldest_price"] or "") for h in holdings) if holdings else ""
        d["imported_at"]   = newest_import[:16].replace("T", " ") if newest_import else None
        d["price_as_of"]   = oldest_price[:10] if oldest_price else None
        stale_basis        = oldest_price or newest_import
        d["data_stale"]    = stale_basis < stale10 if stale_basis else True
        d["sync_date"]     = newest_import[:10] if newest_import else None

        # ── Risk/Technical flags (latest available) ─────────────────────────
        try:
            from portfolio_agent.tools.risk_flags_db import get_active_flags
            # Latest available date, not strictly "today" — the dashboard may be
            # opened before the morning batch finishes; yesterday's flags are
            # still meaningfully current for something that changes this slowly.
            d["risk_flags"] = [
                f for f in get_active_flags(None) if f["ticker"] in d["holding_set"]
            ]
        except Exception:
            d["risk_flags"] = []

        # ── Trigger events last 48 h ────────────────────────────────────────
        rows = c.execute("""
            SELECT ticker, event_type, severity, summary, detected_at, processed
            FROM trigger_events
            WHERE detected_at >= datetime('now', '-48 hours')
            ORDER BY severity DESC, detected_at DESC LIMIT 25
        """).fetchall()
        d["events"] = [dict(r) for r in rows]

        # ── Latest prediction per holding ───────────────────────────────────
        ht = list(d["holding_set"])
        ph = ",".join("?" * len(ht)) if ht else "NULL"
        if ht:
            rows = c.execute(f"""
                SELECT p.ticker, p.recommendation, p.previous_prediction,
                       p.changed_from_previous, p.confidence, p.composite_score,
                       p.created_at, p.horizon_days, p.reasoning,
                       p.fundamental_score, p.research_score, p.macro_score, p.news_score
                FROM predictions p
                JOIN (
                    SELECT ticker, MAX(created_at) dt
                    FROM predictions WHERE ticker IN ({ph}) GROUP BY ticker
                ) x ON p.ticker=x.ticker AND p.created_at=x.dt
            """, ht).fetchall()
            d["hpreds"] = {r[0]: dict(r) for r in rows}
        else:
            d["hpreds"] = {}

        # ── Rec changes on holdings last 3 d ───────────────────────────────
        if ht:
            rows = c.execute(f"""
                SELECT ticker, recommendation, previous_prediction, confidence,
                       composite_score, created_at, reasoning
                FROM predictions
                WHERE changed_from_previous=1 AND created_at>=? AND ticker IN ({ph})
                ORDER BY created_at DESC LIMIT 10
            """, [ago3d] + ht).fetchall()
            d["rec_changes"] = [dict(r) for r in rows]
        else:
            d["rec_changes"] = []

        # ── Validation latest per horizon (90-day window; fall back to any) ──
        rows = c.execute("""
            SELECT horizon_days, directional_accuracy, brier_score,
                   num_predictions, high_conviction_accuracy
            FROM metrics_rolling
            WHERE metric_date=(SELECT MAX(metric_date) FROM metrics_rolling)
              AND lookback_days=90
            ORDER BY horizon_days
        """).fetchall()
        if not rows:
            rows = c.execute("""
                SELECT horizon_days, directional_accuracy, brier_score,
                       num_predictions, high_conviction_accuracy
                FROM metrics_rolling
                WHERE metric_date=(SELECT MAX(metric_date) FROM metrics_rolling)
                GROUP BY horizon_days ORDER BY horizon_days
            """).fetchall()
        d["validation"] = {r[0]: dict(r) for r in rows}

        # ── Watchlist opportunities (not holdings) ──────────────────────────
        if ht:
            rows = c.execute(f"""
                SELECT p.ticker, p.recommendation, p.confidence, p.composite_score, p.created_at
                FROM predictions p
                JOIN (SELECT ticker, MAX(created_at) dt FROM predictions GROUP BY ticker) x
                  ON p.ticker=x.ticker AND p.created_at=x.dt
                WHERE p.ticker NOT IN ({ph})
                  AND p.recommendation IN ('BUY','STRONG_BUY') AND p.confidence>=7
                  AND (p.trigger_type IS NULL OR p.trigger_type != 'trending_opportunity')
                ORDER BY p.composite_score DESC NULLS LAST LIMIT 5
            """, ht).fetchall()
            d["wl_opps"] = [dict(r) for r in rows]
        else:
            d["wl_opps"] = []

        # ── Freshness ────────────────────────────────────────────────────────
        def _latest(tbl, col="updated_at"):
            try:
                return (c.execute(f"SELECT MAX({col}) FROM {tbl}").fetchone()[0] or "")[:10]
            except Exception:
                return ""

        d["news_latest"]   = _latest("news_daily_update", "as_of_date")
        d["res_latest"]    = _latest("research")
        d["fund_latest"]   = _latest("fundamentals")
        d["val_latest"]    = _latest("metrics_rolling", "metric_date")

        # ── Last pipeline run ────────────────────────────────────────────────
        row = c.execute("""
            SELECT job_type, started_at, finished_at, status, trigger_source
            FROM pipeline_runs ORDER BY started_at DESC LIMIT 1
        """).fetchone()
        d["last_run"] = dict(row) if row else None

    # ── Newly promoted screener candidates (today's Discovered signals) ────────
    try:
        from portfolio_agent.tools.universe_db import get_latest_signal_date, get_signals
        from portfolio_agent.tools.watchlist_db import load_watchlist_tickers
        wl_tickers = set(load_watchlist_tickers())
        latest_sig_date = get_latest_signal_date()
        if latest_sig_date:
            sigs = get_signals(latest_sig_date, min_score=2)
            d["discovered_signals"] = [s for s in sigs if s["ticker"] not in wl_tickers][:10]
        else:
            d["discovered_signals"] = []
    except Exception:
        d["discovered_signals"] = []

    return d


@st.cache_data(ttl=3600, show_spinner=False)
def _macro() -> dict:
    try:
        from portfolio_agent.macro.fred_client import get_latest_value, get_yoy_change
        import yfinance as yf

        def _yf(sym):
            try:
                return yf.Ticker(sym).info.get("regularMarketPrice")
            except Exception:
                return None

        vix  = _yf("^VIX")
        t10  = get_latest_value("DGS10")
        t2   = get_latest_value("DGS2")
        ff   = get_latest_value("FEDFUNDS")
        cpi  = get_yoy_change("CPIAUCSL")
        hy   = get_latest_value("BAMLH0A0HYM2")

        t10v = t10.get("value")   if "error" not in t10 else None
        t2v  = t2.get("value")    if "error" not in t2  else None
        ffv  = ff.get("value")    if "error" not in ff  else None
        cpiv = cpi.get("yoy_pct") if "error" not in cpi else None
        hyv  = round(hy.get("value") * 100, 2) if ("error" not in hy and hy.get("value")) else None
        spr  = round(t10v - t2v, 2) if (t10v and t2v) else None

        regime = "NEUTRAL"
        if vix and vix > 25:              regime = "HIGH VOL"
        elif spr is not None and spr < 0: regime = "INVERTED"
        elif vix and vix < 15:            regime = "RISK-ON"

        return {"vix": vix, "t10": t10v, "t2": t2v, "ff": ffv,
                "cpi": cpiv, "hy": hyv, "spread": spr, "regime": regime}
    except Exception:
        return {}


@st.cache_data(ttl=1800, show_spinner=False)
def _trending_opportunities(top_n: int = 8) -> list[dict]:
    """
    Portfolio-fit-aware ranking from opportunity_engine.py — same source data
    the old raw composite_score query used (trigger_type='trending_opportunity'
    predictions), but with the actual portfolio-fit math applied, not just a
    re-sort. Cached separately from _load() (30min, not 2min) since this does
    live yfinance correlation/concentration calls per candidate, matching the
    Opportunity Engine page's own cache TTL rather than hammering it every
    dashboard refresh.
    """
    try:
        from portfolio_agent.tools.opportunity_engine import get_daily_opportunities
        return get_daily_opportunities(top_n=top_n)
    except Exception:
        return []


# ── Attention queue builder ───────────────────────────────────────────────────

def _build_queue(d: dict, macro: dict) -> list[dict]:
    items: list[dict] = []
    hmap = d.get("holding_map", {})
    hset = d.get("holding_set", set())
    hp   = d.get("hpreds", {})

    def _item(priority, ticker, title, why, action_page, requires_action=True):
        items.append(dict(priority=priority, ticker=ticker,
                          title=title, why=why, action_page=action_page,
                          requires_action=requires_action))

    # ── CRITICAL ─────────────────────────────────────────────────────────────
    seen_ev: set = set()
    for ev in d.get("events", []):
        if (ev["severity"] == 3 and ev["ticker"] in hset
                and not ev.get("processed") and ev["ticker"] not in seen_ev):
            seen_ev.add(ev["ticker"])
            w = hmap.get(ev["ticker"], {}).get("weight_pct", 0)
            _item("critical", ev["ticker"],
                  ev.get("summary") or f"Severity-3 {ev['event_type'].replace('_',' ')}",
                  f"Portfolio weight {fmt_pct(w)} — pipeline has not yet processed this event. "
                  f"Open Chat and ask APEX to analyze {ev['ticker']}.",
                  "chat")

    for t, p in hp.items():
        if p.get("recommendation") in ("SELL", "STRONG_SELL"):
            w = hmap.get(t, {}).get("weight_pct", 0)
            _item("critical", t,
                  f"{p['recommendation']} — confidence {p.get('confidence','?')}/10",
                  f"Portfolio weight {fmt_pct(w)}. Review the reasoning before holding further. "
                  f"Check the Predictions page for the full analyst breakdown.",
                  "predictions")

    for h in d.get("holdings", []):
        if h["weight_pct"] > 20:
            _item("critical", h["ticker"],
                  f"Concentration {fmt_pct(h['weight_pct'])} of portfolio",
                  "Single name above 20%. Consider whether the position sizing still matches "
                  "your original thesis. See the Portfolio page for full breakdown.",
                  "portfolio")

    # Risk specialist flags — sector/issuer/correlation/beta dimensions the
    # inline concentration check above doesn't cover (see risk_flags table,
    # populated daily alongside APEX; decoupled from the weight engine).
    seen_risk: set = set()
    for rf in d.get("risk_flags", []):
        if rf["source"] != "risk" or rf["ticker"] in seen_risk:
            continue
        seen_risk.add(rf["ticker"])
        try:
            rd = json.loads(rf.get("raw_json") or "{}")
        except Exception:
            rd = {}
        bits = []
        if (rd.get("sector_concentration_pct") or 0) > 25:
            bits.append(f"{rd.get('sector','sector')} at {fmt_pct(rd['sector_concentration_pct'])} of portfolio")
        if (rd.get("issuer_concentration_pct") or 0) > 15:
            peers = ", ".join(rd.get("issuer_peers") or [])
            bits.append(f"issuer concentration {fmt_pct(rd['issuer_concentration_pct'])}" + (f" (with {peers})" if peers else ""))
        if abs(rd.get("beta_vs_spy") or 0) > 1.5:
            bits.append(f"beta {rd['beta_vs_spy']:.2f} vs SPY")
        detail = "; ".join(bits) if bits else (rf.get("summary") or "Risk specialist flagged this position")
        _item("critical", rf["ticker"],
              f"Risk flag — score {rf.get('score','?')}/10",
              f"{detail}. {rd.get('summary','')}",
              "chat")

    # Technical specialist — bearish momentum on a held position
    for rf in d.get("risk_flags", []):
        if rf["source"] != "technical":
            continue
        try:
            td = json.loads(rf.get("raw_json") or "{}")
        except Exception:
            td = {}
        if td.get("momentum") == "BEARISH":
            _item("watch", rf["ticker"],
                  f"Bearish momentum — technical score {rf.get('score','?')}/10",
                  f"{td.get('summary', 'Technical specialist flagged bearish momentum.')} "
                  f"Ask Chat for the full technical breakdown before acting.",
                  "chat")

    # ── WATCH ─────────────────────────────────────────────────────────────────
    seen_rc: set = set()
    for rc in d.get("rec_changes", []):
        if rc["ticker"] not in seen_rc:
            seen_rc.add(rc["ticker"])
            prev = rc.get("previous_prediction") or "—"
            curr = rc.get("recommendation") or "—"
            rsn  = (rc.get("reasoning") or "")[:120]
            _item("watch", rc["ticker"],
                  f"Recommendation changed: {prev} → {curr}  (conf {rc.get('confidence','?')}/10)",
                  f"{rsn}{'…' if len(rc.get('reasoning','') or '') > 120 else ''} "
                  f"Read the full reasoning on the Predictions page before acting.",
                  "predictions")

    v5   = d.get("validation", {}).get(5, {})
    acc5 = v5.get("directional_accuracy") or 0
    n5   = v5.get("num_predictions") or 0
    if n5 >= 30 and acc5 < 0.45:
        _item("watch", None,
              f"5-day accuracy {fmt_pct(acc5*100)} over {n5} evaluated predictions",
              f"Below coin-flip baseline (50%). Brier score {v5.get('brier_score',0):.2f} "
              f"vs 0.250 baseline. Treat 5-day signals as research prompts only — "
              f"not trade signals. See Validation for breakdown by ticker and horizon.",
              "validation", requires_action=False)

    if d.get("data_stale"):
        sd = d.get("sync_date", "unknown")
        _item("watch", None,
              f"Holdings prices last synced {sd}",
              "Portfolio weights and P&L may not reflect current prices. "
              "Run the pipeline or sync holdings from the Schedule page.",
              "portfolio")

    for h in d.get("holdings", []):
        if 15 <= h["weight_pct"] < 20:
            _item("watch", h["ticker"],
                  f"Concentration {fmt_pct(h['weight_pct'])} — approaching single-name limit",
                  "No immediate action required. Monitor — further appreciation will push "
                  "this past 20%.",
                  "portfolio", requires_action=False)

    for ev in d.get("events", []):
        if (ev["severity"] == 3 and ev["ticker"] in hset
                and ev.get("processed") and ev["ticker"] not in seen_ev):
            seen_ev.add(ev["ticker"])
            w = hmap.get(ev["ticker"], {}).get("weight_pct", 0)
            _item("watch", ev["ticker"],
                  ev.get("summary") or f"Material event: {ev['event_type'].replace('_',' ')}",
                  f"Portfolio weight {fmt_pct(w)}. APEX already processed this — check the updated "
                  f"prediction on the Predictions page.",
                  "predictions", requires_action=False)

    # ── OPPORTUNITY ───────────────────────────────────────────────────────────
    for t, p in hp.items():
        if p.get("recommendation") in ("BUY", "STRONG_BUY"):
            h = hmap.get(t, {})
            w = h.get("weight_pct", 0)
            if w < 10 and (p.get("confidence") or 0) >= 7:
                _item("opportunity", t,
                      f"{p['recommendation']} — {fmt_pct(w)} weight, room to add",
                      f"Confidence {p.get('confidence','?')}/10. Candidate for adding to an "
                      f"existing position. Verify the thesis independently before acting.",
                      "predictions")

    for wl in d.get("wl_opps", [])[:3]:
        sc = wl.get("composite_score")
        sc_str = f"{sc:.2f}" if sc is not None else "?"
        _item("opportunity", wl["ticker"],
              f"Watchlist {wl['recommendation']} — score {sc_str}, conf {wl.get('confidence','?')}/10",
              "Not currently owned. Review setup, check valuation independently, "
              "and consider whether it fits your portfolio before entry.",
              "chat")

    # ── DISCOVERY (trending opportunity) ─────────────────────────────────────
    # Sourced from opportunity_engine.get_daily_opportunities() — the
    # portfolio-fit-aware ranking (correlation, sector-concentration impact,
    # conviction), not a raw composite_score re-sort.
    for td in d.get("trending_opps", []):
        if td.get("call") != "BUY":
            continue
        sc = td.get("opportunity_score")
        sc_str = f"{sc:.2f}" if sc is not None else "?"
        _item("discovery", td["ticker"],
              f"Opportunity Engine BUY — score {sc_str}/100",
              td.get("why") or "Discovered via trending analysis — not in current portfolio.",
              "opportunity_engine")

    return items


def _build_fired_feed(d: dict, limit: int = 12) -> list[dict]:
    """
    One ranked-by-urgency list of everything that changed, pulled from the
    four places that used to scatter this: trigger events (dashboard),
    recommendation changes (Predictions), screener discoveries (Watchlist),
    and risk/technical flags. Meant to sit above everything else — the
    "what fired, what's different, what needs a decision" glance.
    """
    hset = d.get("holding_set", set())
    hmap = d.get("holding_map", {})
    feed: list[dict] = []

    def _add(urgency, icon, ticker, title, action_page):
        feed.append(dict(urgency=urgency, icon=icon, ticker=ticker,
                         title=title, action_page=action_page))

    # ── Severity-3 news events ──────────────────────────────────────────────
    for ev in d.get("events", []):
        if ev["severity"] != 3:
            continue
        w = hmap.get(ev["ticker"], {}).get("weight_pct")
        held_tag = f" · {fmt_pct(w)} of portfolio" if w else ""
        if not ev.get("processed"):
            _add(100, status_dot_html(DANGER), ev["ticker"],
                 f"{ev.get('summary') or ev['event_type'].replace('_',' ')}{held_tag} — not yet processed",
                 "chat")
        else:
            _add(55, status_dot_html(WARNING), ev["ticker"],
                 f"{ev.get('summary') or ev['event_type'].replace('_',' ')}{held_tag} — processed",
                 "predictions")

    # ── Recommendation changes (rating flips) ───────────────────────────────
    for rc in d.get("rec_changes", []):
        prev = rc.get("previous_prediction") or "—"
        curr = rc.get("recommendation") or "—"
        if curr in ("SELL", "STRONG_SELL"):
            urgency = 92 if curr == "STRONG_SELL" else 85
            icon = status_dot_html(DANGER)
        elif curr in ("BUY", "STRONG_BUY"):
            urgency = 58 if curr == "STRONG_BUY" else 50
            icon = status_dot_html(SUCCESS)
        else:
            urgency = 40
            icon = status_dot_html(WARNING)
        _add(urgency, icon, rc["ticker"],
             f"{prev} → {curr} (conf {rc.get('confidence','?')}/10)",
             "predictions")

    # ── Risk / technical flags ──────────────────────────────────────────────
    for rf in d.get("risk_flags", []):
        try:
            rd = json.loads(rf.get("raw_json") or "{}")
        except Exception:
            rd = {}
        score = rf.get("score") or 0
        if rf["source"] == "risk":
            _add(70 + min(score, 10) * 2, status_dot_html(WARNING), rf["ticker"],
                 f"Risk flag — {rd.get('summary', rf.get('summary',''))[:90]}",
                 "chat")
        else:
            _add(55, status_dot_html(WARNING), rf["ticker"],
                 f"Bearish momentum — {rd.get('summary', rf.get('summary',''))[:90]}",
                 "chat")

    # ── Newly promoted screener candidates ──────────────────────────────────
    for sig in d.get("discovered_signals", []):
        try:
            fired = json.loads(sig.get("signals_json") or "[]")
        except Exception:
            fired = []
        _add(30 + min(sig.get("score") or 0, 10) * 3, icon_html("star", 14, color=PURPLE), sig["ticker"],
             f"Discovered — score {sig.get('score','?')} ({', '.join(fired) or 'screener hit'})",
             "chat")

    feed.sort(key=lambda i: i["urgency"], reverse=True)
    return feed[:limit]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt_val(v):
    if v is None: return "—"
    if v >= 1_000_000: return f"${v/1_000_000:.2f}M"
    return fmt_money(v)

# (color, label) — the status dot itself is rendered at each call site via
# status_dot_html(color), not stored here, so the dot always tracks the color.
PRIORITY_STYLE = {
    "critical":    (DANGER,  "CRITICAL"),
    "watch":       (WARNING, "WATCH"),
    "opportunity": (SUCCESS, "PORTFOLIO · ADD SIGNAL"),
    "discovery":   (PURPLE,  "NEW OPPORTUNITIES · TRENDING"),
}

PAGE_MAP = {
    "chat":               "pages/4_🤖_Chat.py",
    "predictions":        "pages/7_🔮_Predictions.py",
    "portfolio":          "pages/5_💼_Portfolio.py",
    "validation":         "pages/6_🎯_Validation.py",
    "opportunity_engine": "pages/9_🎯_Opportunity_Engine.py",
}

# Streamlit's actual page URL is the filename with the leading number, the
# emoji, and their separators all stripped — NOT "emoji + space + Name" and
# NOT "emoji_Name". e.g. "6_🎯_Validation.py" resolves to "/Validation".
PAGE_URL = {
    "chat":               "Chat",
    "predictions":        "Predictions",
    "portfolio":          "Portfolio",
    "validation":         "Validation",
    "opportunity_engine": "Opportunity_Engine",
}


# ── Sticky right-column CSS ───────────────────────────────────────────────────
st.markdown("""
<style>
div[data-testid="stExpander"] details summary p {
    font-size: 0.85rem !important;
    font-weight: 600 !important;
}
div[data-testid="stExpander"] details > div > div {
    padding-top: 6px !important;
    padding-bottom: 8px !important;
}
div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:nth-child(2) {
    position: sticky;
    top: 62px;
    align-self: flex-start;
    max-height: calc(100vh - 80px);
    overflow-y: auto;
}
</style>
""", unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════════════════
# RENDER
# ═══════════════════════════════════════════════════════════════════════════════

d     = _load()
d["trending_opps"] = _trending_opportunities()
macro = _macro()
queue = _build_queue(d, macro)

n_critical    = sum(1 for i in queue if i["priority"] == "critical")
n_watch       = sum(1 for i in queue if i["priority"] == "watch")
n_opportunity = sum(1 for i in queue if i["priority"] == "opportunity")
n_discovery   = sum(1 for i in queue if i["priority"] == "discovery")
n_review      = n_critical + n_watch

tv  = d.get("total_value", 0)
tc  = d.get("total_cost", 0)
upr = d.get("unreal_pct")


# ── Command Strip ─────────────────────────────────────────────────────────────

def _tile(label, value, color="#F9FAFB", sub=""):
    s = f'<div style="font-size:0.66rem;color:#9CA3AF;margin-top:2px">{sub}</div>' if sub else ""
    return (
        f'<div style="flex:1;padding:0 18px;min-width:0;border-right:1px solid #374151;'
        f'last-child:border:none">'
        f'<div style="font-size:0.6rem;font-weight:700;text-transform:uppercase;'
        f'letter-spacing:0.08em;color:#6B7280;margin-bottom:3px">{label}</div>'
        f'<div style="font-size:1rem;font-weight:800;color:{color};white-space:nowrap">{value}</div>'
        f'{s}</div>'
    )

run    = d.get("last_run") or {}
run_st = run.get("status", "")
run_sc = SUCCESS if run_st == "completed" else (DANGER if run_st == "error" else WARNING)
run_la = (run.get("started_at") or "")[:16].replace("T", " ")
run_icon_name = "check_circle" if run_st == "completed" else "cancel" if run_st == "error" else "hourglass_empty"
run_tx = (
    f'<span style="display:inline-flex;align-items:center;gap:4px">'
    f'{icon_html(run_icon_name, 13, color=run_sc)}{run_la}</span>'
)

regime     = macro.get("regime", "—")
regime_col = DANGER if regime in ("HIGH VOL","INVERTED") else (SUCCESS if regime == "RISK-ON" else WARNING)
rev_color  = DANGER if n_critical > 0 else (WARNING if n_watch > 0 else SUCCESS)
upr_col    = SUCCESS if (upr or 0) >= 0 else DANGER
upr_str    = fmt_pct(upr, signed=True)
rev_sub    = (f"{n_critical} critical" if n_critical else "") + (f", {n_watch} watch" if n_watch else "")
disc_col   = PURPLE if n_discovery > 0 else NEUTRAL
disc_sub   = f"{n_discovery} BUY/STRONG_BUY" if n_discovery > 0 else "none today"

st.markdown(
    '<div style="background:#111827;border-radius:12px;padding:14px 4px;'
    'margin-bottom:18px;display:flex;align-items:stretch">'
    + _tile("Imported securities value", _fmt_val(tv), sub=(
            f"{d.get('n_positions', 0) - d.get('n_unvalued', 0)} of {d.get('n_positions', 0)} positions valued"
            + " · cash excluded"
            + (f" · prices as of {d['price_as_of']}" if d.get("price_as_of") else "")))
    + _tile("Unrealized P&L",   upr_str, upr_col,
            f"vs {fmt_money(tc)} cost" if tc else "")
    + _tile("Review Queue",     f"{n_review} items", rev_color, rev_sub or "nothing urgent")
    + _tile("New Discoveries",  f"{n_discovery}", disc_col, disc_sub)
    + _tile("Regime",           regime, regime_col,
            f"VIX {macro['vix']:.2f}" if macro.get("vix") else "")
    + _tile("Last Run",         run_tx, run_sc, run.get("job_type","").title())
    + '</div>',
    unsafe_allow_html=True,
)


# ── Unified "What Fired" feed — one ranked list, above everything else ────────
# Replaces four scattered surfaces (dashboard events, Predictions rec changes,
# Watchlist screener hits, risk flags) with one urgency-sorted list so the
# "what fired, what's different, what needs a decision" glance takes one look,
# not four page visits.

_fired = _build_fired_feed(d)
if _fired:
    _fired_html = ""
    for _f in _fired:
        _url = PAGE_URL.get(_f["action_page"])
        _link = (
            f'<a href="/{_url}" target="_self" style="font-size:0.7rem;color:{PRIMARY};'
            f'margin-left:auto;white-space:nowrap">→ {_f["action_page"].title()}</a>'
            if _url else ""
        )
        _fired_html += (
            f'<div style="display:flex;align-items:baseline;gap:10px;padding:7px 0;'
            f'border-bottom:1px solid #F1F5F9">'
            f'<span style="display:inline-flex;align-items:center">{_f["icon"]}</span>'
            f'<span style="font-weight:800;color:#111827;min-width:110px">'
            f'{ticker_label(_f["ticker"]) if _f["ticker"] else "—"}</span>'
            f'<span style="font-size:0.82rem;color:#374151;flex:1">{_f["title"]}</span>'
            f'{_link}'
            f'</div>'
        )
    _tile_fired = section_tile("What Fired", badge_text=f"{len(_fired)} item(s), ranked by urgency", icon="bolt",
                               expanded=True, key="home_fired")
    if _tile_fired:
        with _tile_fired:
            st.markdown(f'<div style="padding:2px 4px 4px">{_fired_html}</div>', unsafe_allow_html=True)


# ── Two-column layout ─────────────────────────────────────────────────────────

left, right = st.columns([3, 2], gap="large")


# ══════════════════════════════════════════════════════════
# LEFT — Attention Queue (expanders)
# ══════════════════════════════════════════════════════════

with left:
    _t = section_tile("Today's Attention Queue", badge_text="ranked by urgency", icon="notifications", key="home_queue")
    if _t:
        with _t:

            if not queue:
                st.success("Nothing requires attention today.")
            else:
                n_action = sum(1 for i in queue if i["requires_action"])
                n_fyi    = len(queue) - n_action
                st.markdown(
                    f'<div style="font-size:0.8rem;font-weight:600;color:#111827;'
                    f'margin-bottom:14px">'
                    f'{n_action} item{"s" if n_action != 1 else ""} need a decision, '
                    f'{n_fyi} {"are" if n_fyi != 1 else "is"} FYI</div>',
                    unsafe_allow_html=True,
                )

                # ── Portfolio section ─────────────────────────────────────────────────
                st.markdown(
                    '<div style="font-size:0.72rem;font-weight:700;text-transform:uppercase;'
                    'letter-spacing:0.08em;color:#374151;background:#F9FAFB;border-radius:8px;'
                    'padding:6px 10px;margin-bottom:4px;border-left:3px solid #2563EB">'
                    f'{icon_html("work", 14, extra_style="margin-right:6px;vertical-align:-2px")} YOUR PORTFOLIO</div>',
                    unsafe_allow_html=True,
                )
                _portfolio_rendered = False
                for group_key in ("critical", "watch", "opportunity"):
                    group = [i for i in queue if i["priority"] == group_key]
                    if not group:
                        continue
                    _portfolio_rendered = True
                    color, label = PRIORITY_STYLE[group_key]
                    st.markdown(
                        f'<div style="font-size:0.68rem;font-weight:700;text-transform:uppercase;'
                        f'letter-spacing:0.08em;color:{color};margin:12px 0 6px;display:flex;'
                        f'align-items:center;gap:6px">'
                        f'{status_dot_html(color, 7)} {label} ({len(group)})</div>',
                        unsafe_allow_html=True,
                    )
                    for item in group:
                        t = item.get("ticker")
                        ticker_tag = f"[{ticker_label(t)}] " if t else ""
                        exp_label  = f"{ticker_tag}{item['title']}"
                        with st.expander(exp_label, expanded=(group_key == "critical")):
                            st.markdown(
                                f'<div style="font-size:0.82rem;color:#374151;line-height:1.6">'
                                f'{item["why"]}</div>',
                                unsafe_allow_html=True,
                            )
                            url = PAGE_URL.get(item["action_page"])
                            if url:
                                st.markdown(
                                    f'<div style="font-size:0.75rem;color:#9CA3AF;margin-top:6px">'
                                    f'→ See: <a href="/{url}" '
                                    f'target="_self">{item["action_page"].title()}</a></div>',
                                    unsafe_allow_html=True,
                                )
                if not _portfolio_rendered:
                    st.markdown(
                        '<div style="font-size:0.8rem;color:#6B7280;padding:8px 4px">'
                        'No portfolio alerts today.</div>',
                        unsafe_allow_html=True,
                    )

                # ── New Opportunities section ─────────────────────────────────────────
                st.markdown(
                    '<div style="font-size:0.72rem;font-weight:700;text-transform:uppercase;'
                    'letter-spacing:0.08em;color:#374151;background:#F5F3FF;border-radius:8px;'
                    'padding:6px 10px;margin:20px 0 4px;border-left:3px solid #7C3AED">'
                    f'{icon_html("auto_awesome", 14, color=PURPLE, extra_style="margin-right:6px;vertical-align:-2px")} '
                    'NEW INVESTMENT OPPORTUNITIES</div>',
                    unsafe_allow_html=True,
                )
                disc_group = [i for i in queue if i["priority"] == "discovery"]
                if disc_group:
                    color, label = PRIORITY_STYLE["discovery"]
                    st.markdown(
                        f'<div style="font-size:0.68rem;font-weight:700;text-transform:uppercase;'
                        f'letter-spacing:0.08em;color:{color};margin:12px 0 6px;display:flex;'
                        f'align-items:center;gap:6px">'
                        f'{status_dot_html(color, 7)} {label} ({len(disc_group)})</div>',
                        unsafe_allow_html=True,
                    )
                    for idx, item in enumerate(disc_group):
                        t = item.get("ticker")
                        ticker_tag = f"[{ticker_label(t)}] " if t else ""
                        exp_label  = f"{ticker_tag}{item['title']}"
                        with st.expander(exp_label, expanded=(idx == 0)):
                            st.markdown(
                                f'<div style="font-size:0.82rem;color:#374151;line-height:1.6">'
                                f'{item["why"]}</div>',
                                unsafe_allow_html=True,
                            )
                            url = PAGE_URL.get(item["action_page"])
                            if url:
                                st.markdown(
                                    f'<div style="font-size:0.75rem;color:#9CA3AF;margin-top:6px">'
                                    f'→ See: <a href="/{url}" '
                                    f'target="_self">{item["action_page"].title()}</a></div>',
                                    unsafe_allow_html=True,
                                )
                else:
                    st.markdown(
                        '<div style="font-size:0.8rem;color:#6B7280;padding:8px 4px">'
                        'No trending opportunity signals today. The pipeline discovers new tickers '
                        'during morning and intraday batch runs.</div>',
                        unsafe_allow_html=True,
                    )


# ══════════════════════════════════════════════════════════
# RIGHT — Sticky sidebar (one HTML block)
# ══════════════════════════════════════════════════════════

with right:
    _t = section_tile("Health, Opportunities & Macro", icon="monitor_heart", key="home_health")
    if _t:
        with _t:
            # ── Compact health strip (replaces full portfolio + validation panels) ─────
            v5   = d.get("validation", {}).get(5, {})
            acc5 = v5.get("directional_accuracy")
            br5  = v5.get("brier_score")
            n5   = v5.get("num_predictions", 0)

            if acc5 is not None:
                _acc_col = accuracy_color(acc5 * 100)
                _br_col  = brier_color(br5 or 1.0)
                _health_strip = (
                    f'<div style="background:#fff;border:1px solid #E5E7EB;border-radius:12px;'
                    f'padding:12px 16px;margin-bottom:12px">'
                    f'<div style="font-size:0.65rem;font-weight:700;text-transform:uppercase;'
                    f'letter-spacing:0.08em;color:#9CA3AF;margin-bottom:8px">System Health</div>'
                    f'<div style="display:flex;gap:20px">'
                    f'<div><div style="font-size:1.05rem;font-weight:800;color:{_acc_col}">'
                    f'{fmt_pct(acc5*100)}</div>'
                    f'<div style="font-size:0.63rem;color:#9CA3AF">5d accuracy</div></div>'
                    f'<div><div style="font-size:1.05rem;font-weight:800;color:{_br_col}">'
                    f'{br5:.2f}</div>'
                    f'<div style="font-size:0.63rem;color:#9CA3AF">Brier</div></div>'
                    f'<div><div style="font-size:1.05rem;font-weight:800;color:#374151">{n5}</div>'
                    f'<div style="font-size:0.63rem;color:#9CA3AF">Evaluated</div></div>'
                    f'</div>'
                    f'<div style="font-size:0.72rem;color:#9CA3AF;margin-top:8px">'
                    f'→ <a href="/Validation" target="_self">Full validation report</a></div>'
                    f'</div>'
                )
            else:
                _health_strip = (
                    f'<div style="background:#fff;border:1px solid #E5E7EB;border-radius:12px;'
                    f'padding:12px 16px;margin-bottom:12px">'
                    f'<div style="font-size:0.65rem;font-weight:700;text-transform:uppercase;'
                    f'letter-spacing:0.08em;color:#9CA3AF;margin-bottom:6px">System Health</div>'
                    f'<div style="font-size:0.8rem;color:#9CA3AF">No validation data yet.</div>'
                    f'<div style="font-size:0.72rem;color:#9CA3AF;margin-top:8px">'
                    f'→ <a href="/Validation" target="_self">Full validation report</a></div>'
                    f'</div>'
                )

            # ── Freshness rows ────────────────────────────────────────────────────────
            fresh_rows = ""
            for label, date_str, warn_days in [
                ("News",         d.get("news_latest"),  1),
                ("Research",     d.get("res_latest"),   3),
                ("Fundamentals", d.get("fund_latest"),  7),
                ("Holdings",     d.get("sync_date"),    7),
            ]:
                ftxt, fcol = freshness_color(date_str or "", warn_days)
                _dot_icon = "check_circle" if fcol == SUCCESS else ("warning" if fcol == WARNING else "cancel")
                dot = icon_html(_dot_icon, 13, color=fcol)
                fresh_rows += (
                    f'<div style="display:flex;justify-content:space-between;padding:5px 0;'
                    f'border-bottom:1px solid #F3F4F6">'
                    f'<span style="font-size:0.78rem;color:#374151;display:inline-flex;align-items:center;gap:5px">{dot} {label}</span>'
                    f'<span style="font-size:0.78rem;font-weight:600;color:{fcol}">{ftxt}</span>'
                    f'</div>'
                )

            # ── Macro strip ───────────────────────────────────────────────────────────
            def _mv(val, fmt=".2f", suffix=""):
                return f"{val:{fmt}}{suffix}" if val is not None else "—"

            spr = macro.get("spread")
            spr_col = DANGER if (spr is not None and spr < 0) else SUCCESS
            macro_rows = (
                f'<div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 16px">'
                f'<div style="font-size:0.76rem;color:#6B7280">10Y–2Y Spread</div>'
                f'<div style="font-size:0.76rem;font-weight:700;color:{spr_col};text-align:right">'
                f'{fmt_pct(spr, signed=True)}</div>'
                f'<div style="font-size:0.76rem;color:#6B7280">Fed Funds</div>'
                f'<div style="font-size:0.76rem;font-weight:700;color:#374151;text-align:right">'
                f'{fmt_pct(macro.get("ff"))}</div>'
                f'<div style="font-size:0.76rem;color:#6B7280">CPI YoY</div>'
                f'<div style="font-size:0.76rem;font-weight:700;'
                f'color:{DANGER if (macro.get("cpi") or 0)>3.5 else WARNING if (macro.get("cpi") or 0)>2.5 else SUCCESS};'
                f'text-align:right">{fmt_pct(macro.get("cpi"))}</div>'
                f'<div style="font-size:0.76rem;color:#6B7280">HY Spread</div>'
                f'<div style="font-size:0.76rem;font-weight:700;'
                f'color:{DANGER if (macro.get("hy") or 0)>450 else "#374151"};text-align:right">'
                f'{_mv(macro.get("hy"), ".2f", "bps")}</div>'
                f'</div>'
            )

            # ── New Opportunities panel ───────────────────────────────────────────────
            # Sourced from opportunity_engine.get_daily_opportunities() — same
            # portfolio-fit-aware ranking the Opportunity Engine page shows, not a
            # raw composite_score re-sort.
            _topp = d.get("trending_opps", [])
            if _topp:
                opp_rows = ""
                for _op in _topp[:5]:
                    _call = _op.get("call", "")
                    _call_col = {"BUY": SUCCESS}.get(_call, WARNING)
                    _sc = _op.get("opportunity_score")
                    _sc_str = f"{_sc:.2f}" if _sc is not None else "?"
                    _impact = _op.get("portfolio_impact") or {}
                    _corr = _impact.get("correlation_with_portfolio")
                    _sub_parts = []
                    if _op.get("composite_score") is not None:
                        _sub_parts.append(f"APEX {_op['composite_score']:.2f}")
                    if _corr is not None:
                        _sub_parts.append(f"corr {_corr:.2f}")
                    _sub = " · ".join(_sub_parts)
                    opp_rows += (
                        f'<div style="display:flex;justify-content:space-between;align-items:center;'
                        f'padding:7px 0;border-bottom:1px solid #EDE9FE">'
                        f'<div>'
                        f'<span style="font-size:0.82rem;font-weight:700;color:#111827">{_op["ticker"]}</span>'
                        f'<span style="font-size:0.72rem;color:{_call_col};font-weight:600;margin-left:6px">'
                        f'{_call}</span>'
                        f'<div style="font-size:0.68rem;color:#9CA3AF;margin-top:1px">{_sub}</div>'
                        f'</div>'
                        f'<span style="font-size:0.88rem;font-weight:800;color:{_call_col}">'
                        f'{_sc_str}<span style="font-size:0.65rem;color:#9CA3AF">/100</span></span>'
                        f'</div>'
                    )
                opp_panel = (
                    f'<div style="background:#F5F3FF;border:1px solid #DDD6FE;border-radius:12px;'
                    f'padding:14px 16px;margin-bottom:12px">'
                    f'<div style="font-size:0.68rem;font-weight:700;text-transform:uppercase;'
                    f'letter-spacing:0.08em;color:#7C3AED;margin-bottom:10px">'
                    f'{icon_html("auto_awesome", 13, color=PURPLE, extra_style="margin-right:5px;vertical-align:-2px")} '
                    f'New Opportunities · Trending</div>'
                    f'{opp_rows}'
                    f'<div style="font-size:0.72rem;color:#9CA3AF;margin-top:8px">'
                    f'→ <a href="/Opportunity_Engine" target="_self">Full ranking on Opportunity Engine</a></div>'
                    f'</div>'
                )
            else:
                opp_panel = (
                    f'<div style="background:#F5F3FF;border:1px dashed #C4B5FD;border-radius:12px;'
                    f'padding:12px 16px;margin-bottom:12px">'
                    f'<div style="font-size:0.68rem;font-weight:700;text-transform:uppercase;'
                    f'letter-spacing:0.08em;color:#7C3AED;margin-bottom:6px">'
                    f'{icon_html("auto_awesome", 13, color=PURPLE, extra_style="margin-right:5px;vertical-align:-2px")} '
                    f'New Opportunities · Trending</div>'
                    f'<div style="font-size:0.8rem;color:#6B7280">'
                    f'No trending signals yet today. Discoveries are added during morning '
                    f'&amp; intraday pipeline runs.</div>'
                    f'</div>'
                )

            # ── Render all as sticky block ────────────────────────────────────────────
            _fresh_panel = (
                f'<div style="background:#fff;border:1px solid #E5E7EB;border-radius:12px;'
                f'padding:14px 16px;margin-bottom:12px">'
                f'<div style="font-size:0.68rem;font-weight:700;text-transform:uppercase;'
                f'letter-spacing:0.08em;color:#9CA3AF;margin-bottom:8px">Data Freshness</div>'
                f'{fresh_rows}'
                f'</div>'
            )
            _macro_panel = (
                f'<div style="background:#fff;border:1px solid #E5E7EB;border-radius:12px;'
                f'padding:14px 16px">'
                f'<div style="font-size:0.68rem;font-weight:700;text-transform:uppercase;'
                f'letter-spacing:0.08em;color:#9CA3AF;margin-bottom:10px">'
                f'Macro Context · Regime: <span style="color:{regime_col}">{regime}</span></div>'
                f'{macro_rows}'
                f'</div>'
            )
            st.markdown(
                '<div style="position:sticky;top:62px">'
                + _health_strip
                + opp_panel
                + _fresh_panel
                + _macro_panel
                + '</div>',
                unsafe_allow_html=True,
            )
