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
    inject_global_css, top_nav, section_title,
    REC_STYLES, SUCCESS, WARNING, DANGER, PRIMARY, NEUTRAL, PURPLE,
    SUCCESS_LIGHT, WARNING_LIGHT, DANGER_LIGHT, PRIMARY_LIGHT,
)

_DB = _ROOT / "data" / "portfolio.db"

st.set_page_config(
    page_title="Portfolio Intelligence",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)
inject_global_css()
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
        rows = c.execute("""
            SELECT ticker,
                   SUM(shares) shares,
                   SUM(COALESCE(current_value,0)) value,
                   SUM(COALESCE(cost_basis_total, shares*avg_cost, 0)) cost_basis,
                   MIN(synced_at) oldest_sync
            FROM holdings GROUP BY ticker ORDER BY value DESC
        """).fetchall()
        holdings = [dict(r) for r in rows]
        tv = sum(h["value"] for h in holdings)
        tc = sum(h["cost_basis"] for h in holdings)
        for h in holdings:
            h["weight_pct"] = h["value"] / tv * 100 if tv else 0
        d["holdings"]      = holdings
        d["total_value"]   = tv
        d["total_cost"]    = tc
        d["unreal_pct"]    = (tv - tc) / tc * 100 if tc else None
        d["holding_set"]   = {h["ticker"] for h in holdings}
        d["holding_map"]   = {h["ticker"]: h for h in holdings}
        oldest = min((h["oldest_sync"] or "") for h in holdings) if holdings else ""
        d["data_stale"]    = oldest < stale10 if oldest else True
        d["sync_date"]     = oldest[:10] if oldest else None

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

        # ── Trending opportunity discoveries ────────────────────────────────
        rows = c.execute("""
            SELECT p.ticker, p.recommendation, p.confidence, p.composite_score,
                   p.news_score, p.research_score, p.fundamental_score, p.created_at
            FROM predictions p
            JOIN (
                SELECT ticker, MAX(created_at) dt
                FROM predictions WHERE trigger_type='trending_opportunity'
                GROUP BY ticker
            ) x ON p.ticker=x.ticker AND p.created_at=x.dt
            WHERE p.trigger_type='trending_opportunity'
            ORDER BY p.composite_score DESC NULLS LAST LIMIT 8
        """).fetchall()
        d["trending_opps"] = [dict(r) for r in rows]

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
        hyv  = round(hy.get("value") * 100) if ("error" not in hy and hy.get("value")) else None
        spr  = round(t10v - t2v, 2) if (t10v and t2v) else None

        regime = "NEUTRAL"
        if vix and vix > 25:              regime = "HIGH VOL"
        elif spr is not None and spr < 0: regime = "INVERTED"
        elif vix and vix < 15:            regime = "RISK-ON"

        return {"vix": vix, "t10": t10v, "t2": t2v, "ff": ffv,
                "cpi": cpiv, "hy": hyv, "spread": spr, "regime": regime}
    except Exception:
        return {}


# ── Attention queue builder ───────────────────────────────────────────────────

def _build_queue(d: dict, macro: dict) -> list[dict]:
    items: list[dict] = []
    hmap = d.get("holding_map", {})
    hset = d.get("holding_set", set())
    hp   = d.get("hpreds", {})

    def _item(priority, ticker, title, why, action_page):
        items.append(dict(priority=priority, ticker=ticker,
                          title=title, why=why, action_page=action_page))

    # ── CRITICAL ─────────────────────────────────────────────────────────────
    seen_ev: set = set()
    for ev in d.get("events", []):
        if (ev["severity"] == 3 and ev["ticker"] in hset
                and not ev.get("processed") and ev["ticker"] not in seen_ev):
            seen_ev.add(ev["ticker"])
            w = hmap.get(ev["ticker"], {}).get("weight_pct", 0)
            _item("critical", ev["ticker"],
                  ev.get("summary") or f"Severity-3 {ev['event_type'].replace('_',' ')}",
                  f"Portfolio weight {w:.1f}% — pipeline has not yet processed this event. "
                  f"Open Chat and ask APEX to analyze {ev['ticker']}.",
                  "chat")

    for t, p in hp.items():
        if p.get("recommendation") in ("SELL", "STRONG_SELL"):
            w = hmap.get(t, {}).get("weight_pct", 0)
            _item("critical", t,
                  f"{p['recommendation']} — confidence {p.get('confidence','?')}/10",
                  f"Portfolio weight {w:.1f}%. Review the reasoning before holding further. "
                  f"Check the Predictions page for the full analyst breakdown.",
                  "predictions")

    for h in d.get("holdings", []):
        if h["weight_pct"] > 20:
            _item("critical", h["ticker"],
                  f"Concentration {h['weight_pct']:.1f}% of portfolio",
                  "Single name above 20%. Consider whether the position sizing still matches "
                  "your original thesis. See the Portfolio page for full breakdown.",
                  "portfolio")

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
              f"5-day accuracy {acc5*100:.0f}% over {n5} evaluated predictions",
              f"Below coin-flip baseline (50%). Brier score {v5.get('brier_score',0):.3f} "
              f"vs 0.250 baseline. Treat 5-day signals as research prompts only — "
              f"not trade signals. See Validation for breakdown by ticker and horizon.",
              "validation")

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
                  f"Concentration {h['weight_pct']:.1f}% — approaching single-name limit",
                  "No immediate action required. Monitor — further appreciation will push "
                  "this past 20%.",
                  "portfolio")

    for ev in d.get("events", []):
        if (ev["severity"] == 3 and ev["ticker"] in hset
                and ev.get("processed") and ev["ticker"] not in seen_ev):
            seen_ev.add(ev["ticker"])
            w = hmap.get(ev["ticker"], {}).get("weight_pct", 0)
            _item("watch", ev["ticker"],
                  ev.get("summary") or f"Material event: {ev['event_type'].replace('_',' ')}",
                  f"Portfolio weight {w:.1f}%. APEX already processed this — check the updated "
                  f"prediction on the Predictions page.",
                  "predictions")

    # ── OPPORTUNITY ───────────────────────────────────────────────────────────
    for t, p in hp.items():
        if p.get("recommendation") in ("BUY", "STRONG_BUY"):
            h = hmap.get(t, {})
            w = h.get("weight_pct", 0)
            if w < 10 and (p.get("confidence") or 0) >= 7:
                _item("opportunity", t,
                      f"{p['recommendation']} — {w:.1f}% weight, room to add",
                      f"Confidence {p.get('confidence','?')}/10. Candidate for adding to an "
                      f"existing position. Verify the thesis independently before acting.",
                      "predictions")

    for wl in d.get("wl_opps", [])[:3]:
        sc = wl.get("composite_score")
        sc_str = f"{sc:.1f}" if sc is not None else "?"
        _item("opportunity", wl["ticker"],
              f"Watchlist {wl['recommendation']} — score {sc_str}, conf {wl.get('confidence','?')}/10",
              "Not currently owned. Review setup, check valuation independently, "
              "and consider whether it fits your portfolio before entry.",
              "chat")

    # ── DISCOVERY (trending opportunity) ─────────────────────────────────────
    for td in d.get("trending_opps", []):
        rec = td.get("recommendation", "")
        if rec not in ("BUY", "STRONG_BUY"):
            continue
        sc  = td.get("composite_score")
        sc_str = f"{sc:.1f}" if sc is not None else "?"
        ns  = td.get("news_score")
        rs  = td.get("research_score")
        fs  = td.get("fundamental_score")
        score_parts = []
        if ns is not None: score_parts.append(f"News {ns:.1f}")
        if rs is not None: score_parts.append(f"Research {rs:.1f}")
        if fs is not None: score_parts.append(f"Fundamentals {fs:.1f}")
        score_detail = " · ".join(score_parts) if score_parts else ""
        _item("discovery", td["ticker"],
              f"Trending {rec} — composite score {sc_str}, conf {td.get('confidence','?')}/10",
              f"Discovered via trending analysis. Full pipeline run: {score_detail}. "
              f"Not in current portfolio — see Predictions page for full APEX breakdown.",
              "predictions")

    return items


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt_val(v):
    if v is None: return "—"
    if v >= 1_000_000: return f"${v/1_000_000:.2f}M"
    if v >= 1_000:     return f"${v:,.0f}"
    return f"${v:.2f}"

def _freshness(date_str, warn_days=3):
    if not date_str: return "missing", DANGER
    try:
        n = (datetime.now() - datetime.fromisoformat(date_str[:10])).days
    except Exception:
        return date_str, NEUTRAL
    if n == 0:          return "today", SUCCESS
    if n <= warn_days:  return f"{n}d ago", WARNING
    return f"{n}d ago", DANGER

PRIORITY_STYLE = {
    "critical":    (DANGER,  "🔴", "CRITICAL"),
    "watch":       (WARNING, "🟡", "WATCH"),
    "opportunity": (SUCCESS, "🟢", "PORTFOLIO · ADD SIGNAL"),
    "discovery":   (PURPLE,  "🌟", "NEW OPPORTUNITIES · TRENDING"),
}

PAGE_MAP = {
    "chat":        "pages/4_🤖_Chat.py",
    "predictions": "pages/7_🔮_Predictions.py",
    "portfolio":   "pages/5_💼_Portfolio.py",
    "validation":  "pages/6_🎯_Validation.py",
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
run_tx = ("✓ " if run_st == "completed" else "✗ " if run_st == "error" else "… ") + run_la

regime     = macro.get("regime", "—")
regime_col = DANGER if regime in ("HIGH VOL","INVERTED") else (SUCCESS if regime == "RISK-ON" else WARNING)
rev_color  = DANGER if n_critical > 0 else (WARNING if n_watch > 0 else SUCCESS)
upr_col    = SUCCESS if (upr or 0) >= 0 else DANGER
upr_str    = f"{upr:+.1f}%" if upr is not None else "—"
rev_sub    = (f"{n_critical} critical" if n_critical else "") + (f", {n_watch} watch" if n_watch else "")
disc_col   = PURPLE if n_discovery > 0 else NEUTRAL
disc_sub   = f"{n_discovery} BUY/STRONG_BUY" if n_discovery > 0 else "none today"

st.markdown(
    '<div style="background:#111827;border-radius:12px;padding:14px 4px;'
    'margin-bottom:18px;display:flex;align-items:stretch">'
    + _tile("Portfolio",        _fmt_val(tv))
    + _tile("Unrealized P&L",   upr_str, upr_col,
            f"vs ${tc:,.0f} cost" if tc else "")
    + _tile("Review Queue",     f"{n_review} items", rev_color, rev_sub or "nothing urgent")
    + _tile("New Discoveries",  f"{n_discovery}", disc_col, disc_sub)
    + _tile("Regime",           regime, regime_col,
            f"VIX {macro['vix']:.0f}" if macro.get("vix") else "")
    + _tile("Last Run",         run_tx, run_sc, run.get("job_type","").title())
    + '</div>',
    unsafe_allow_html=True,
)


# ── Two-column layout ─────────────────────────────────────────────────────────

left, right = st.columns([3, 2], gap="large")


# ══════════════════════════════════════════════════════════
# LEFT — Attention Queue (expanders)
# ══════════════════════════════════════════════════════════

with left:
    st.markdown(
        '<div style="font-size:0.92rem;font-weight:700;color:#111827;margin-bottom:2px">'
        "Today's Attention Queue</div>"
        '<div style="font-size:0.75rem;color:#9CA3AF;margin-bottom:14px">'
        "Click any item to see detail and suggested action.</div>",
        unsafe_allow_html=True,
    )

    if not queue:
        st.success("Nothing requires attention today.")
    else:
        # ── Portfolio section ─────────────────────────────────────────────────
        st.markdown(
            '<div style="font-size:0.72rem;font-weight:700;text-transform:uppercase;'
            'letter-spacing:0.08em;color:#374151;background:#F9FAFB;border-radius:8px;'
            'padding:6px 10px;margin-bottom:4px;border-left:3px solid #2563EB">'
            '💼 YOUR PORTFOLIO</div>',
            unsafe_allow_html=True,
        )
        _portfolio_rendered = False
        for group_key in ("critical", "watch", "opportunity"):
            group = [i for i in queue if i["priority"] == group_key]
            if not group:
                continue
            _portfolio_rendered = True
            color, dot, label = PRIORITY_STYLE[group_key]
            st.markdown(
                f'<div style="font-size:0.68rem;font-weight:700;text-transform:uppercase;'
                f'letter-spacing:0.08em;color:{color};margin:12px 0 6px">'
                f'{dot} {label} ({len(group)})</div>',
                unsafe_allow_html=True,
            )
            for idx, item in enumerate(group):
                t = item.get("ticker")
                ticker_tag = f"[{t}] " if t else ""
                exp_label  = f"{ticker_tag}{item['title']}"
                with st.expander(exp_label, expanded=(group_key == "critical" and idx == 0)):
                    st.markdown(
                        f'<div style="font-size:0.82rem;color:#374151;line-height:1.6">'
                        f'{item["why"]}</div>',
                        unsafe_allow_html=True,
                    )
                    page = PAGE_MAP.get(item["action_page"])
                    if page:
                        st.markdown(
                            f'<div style="font-size:0.75rem;color:#9CA3AF;margin-top:6px">'
                            f'→ See: <a href="/{page.split("/")[-1].split("_",1)[-1].replace(".py","").replace("_"," ")}" '
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
            '🌟 NEW INVESTMENT OPPORTUNITIES</div>',
            unsafe_allow_html=True,
        )
        disc_group = [i for i in queue if i["priority"] == "discovery"]
        if disc_group:
            color, dot, label = PRIORITY_STYLE["discovery"]
            st.markdown(
                f'<div style="font-size:0.68rem;font-weight:700;text-transform:uppercase;'
                f'letter-spacing:0.08em;color:{color};margin:12px 0 6px">'
                f'{dot} {label} ({len(disc_group)})</div>',
                unsafe_allow_html=True,
            )
            for idx, item in enumerate(disc_group):
                t = item.get("ticker")
                ticker_tag = f"[{t}] " if t else ""
                exp_label  = f"{ticker_tag}{item['title']}"
                with st.expander(exp_label, expanded=(idx == 0)):
                    st.markdown(
                        f'<div style="font-size:0.82rem;color:#374151;line-height:1.6">'
                        f'{item["why"]}</div>',
                        unsafe_allow_html=True,
                    )
                    page = PAGE_MAP.get(item["action_page"])
                    if page:
                        st.markdown(
                            f'<div style="font-size:0.75rem;color:#9CA3AF;margin-top:6px">'
                            f'→ See: <a href="/{page.split("/")[-1].split("_",1)[-1].replace(".py","").replace("_"," ")}" '
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
    holdings_sorted = sorted(d.get("holdings", []), key=lambda h: h["weight_pct"], reverse=True)
    hp = d.get("hpreds", {})

    # ── Portfolio concentration bars ──────────────────────────────────────────
    risk_rows = ""
    for h in holdings_sorted[:5]:
        w    = h["weight_pct"]
        bar  = min(100, w * 4)
        col  = DANGER if w > 20 else (WARNING if w > 15 else PRIMARY)
        pred = hp.get(h["ticker"])
        rec  = pred.get("recommendation", "") if pred else ""
        dot  = {"STRONG_BUY":"🟢","BUY":"🟢","HOLD":"🟡","SELL":"🔴","STRONG_SELL":"🔴"}.get(rec, "")
        risk_rows += (
            f'<div style="margin-bottom:8px">'
            f'<div style="display:flex;justify-content:space-between;margin-bottom:2px">'
            f'<span style="font-size:0.8rem;font-weight:700;color:#111827">{dot} {h["ticker"]}</span>'
            f'<span style="font-size:0.8rem;font-weight:800;color:{col}">{w:.1f}%</span>'
            f'</div>'
            f'<div style="height:4px;background:#F3F4F6;border-radius:99px">'
            f'<div style="width:{bar:.0f}%;height:100%;background:{col};border-radius:99px"></div>'
            f'</div></div>'
        )

    # ── Validation panel ──────────────────────────────────────────────────────
    v5    = d.get("validation", {}).get(5, {})
    acc5  = v5.get("directional_accuracy")
    br5   = v5.get("brier_score")
    n5    = v5.get("num_predictions", 0)
    if acc5 is not None:
        acc_pct = acc5 * 100
        acc_col = SUCCESS if acc_pct > 55 else (WARNING if acc_pct > 45 else DANGER)
        br_col  = SUCCESS if (br5 or 1) < 0.22 else (WARNING if (br5 or 1) < 0.25 else DANGER)
        warn    = ("⚠ Below coin-flip. Use as research, not signals."
                   if acc_pct < 50 else "Limited edge — verify fundamentals first.")
        val_html = (
            f'<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:8px">'
            f'<div style="text-align:center">'
            f'<div style="font-size:1.15rem;font-weight:800;color:{acc_col}">{acc_pct:.0f}%</div>'
            f'<div style="font-size:0.63rem;color:#9CA3AF">5d Accuracy</div></div>'
            f'<div style="text-align:center">'
            f'<div style="font-size:1.15rem;font-weight:800;color:{br_col}">{br5:.3f}</div>'
            f'<div style="font-size:0.63rem;color:#9CA3AF">Brier</div></div>'
            f'<div style="text-align:center">'
            f'<div style="font-size:1.15rem;font-weight:800;color:#374151">{n5}</div>'
            f'<div style="font-size:0.63rem;color:#9CA3AF">Evaluated</div></div>'
            f'</div>'
            f'<div style="font-size:0.72rem;color:#991B1B">{warn}</div>'
        )
    else:
        val_html = '<div style="font-size:0.8rem;color:#9CA3AF">No validation data yet.</div>'

    # ── Freshness rows ────────────────────────────────────────────────────────
    fresh_rows = ""
    for label, date_str, warn_days in [
        ("News",         d.get("news_latest"),  1),
        ("Research",     d.get("res_latest"),   3),
        ("Fundamentals", d.get("fund_latest"),  7),
        ("Holdings",     d.get("sync_date"),    7),
    ]:
        ftxt, fcol = _freshness(date_str or "", warn_days)
        dot = "✅" if fcol == SUCCESS else ("⚠️" if fcol == WARNING else "❌")
        fresh_rows += (
            f'<div style="display:flex;justify-content:space-between;padding:5px 0;'
            f'border-bottom:1px solid #F3F4F6">'
            f'<span style="font-size:0.78rem;color:#374151">{dot} {label}</span>'
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
        f'{_mv(spr, "+.2f", "%")}</div>'
        f'<div style="font-size:0.76rem;color:#6B7280">Fed Funds</div>'
        f'<div style="font-size:0.76rem;font-weight:700;color:#374151;text-align:right">'
        f'{_mv(macro.get("ff"), ".2f", "%")}</div>'
        f'<div style="font-size:0.76rem;color:#6B7280">CPI YoY</div>'
        f'<div style="font-size:0.76rem;font-weight:700;'
        f'color:{DANGER if (macro.get("cpi") or 0)>3.5 else WARNING if (macro.get("cpi") or 0)>2.5 else SUCCESS};'
        f'text-align:right">{_mv(macro.get("cpi"), ".1f", "%")}</div>'
        f'<div style="font-size:0.76rem;color:#6B7280">HY Spread</div>'
        f'<div style="font-size:0.76rem;font-weight:700;'
        f'color:{DANGER if (macro.get("hy") or 0)>450 else "#374151"};text-align:right">'
        f'{_mv(macro.get("hy"), ".0f", "bps")}</div>'
        f'</div>'
    )

    # ── New Opportunities panel ───────────────────────────────────────────────
    _topp = d.get("trending_opps", [])
    if _topp:
        opp_rows = ""
        for _op in _topp[:5]:
            _rec  = _op.get("recommendation", "")
            _rec_col = {"STRONG_BUY": SUCCESS, "BUY": SUCCESS}.get(_rec, WARNING)
            _sc   = _op.get("composite_score")
            _sc_str = f"{_sc:.1f}" if _sc is not None else "?"
            _ns   = _op.get("news_score")
            _rs   = _op.get("research_score")
            _fs   = _op.get("fundamental_score")
            _sub_parts = []
            if _fs is not None: _sub_parts.append(f"F:{_fs:.0f}")
            if _rs is not None: _sub_parts.append(f"R:{_rs:.0f}")
            if _ns is not None: _sub_parts.append(f"N:{_ns:.0f}")
            _sub = " · ".join(_sub_parts)
            opp_rows += (
                f'<div style="display:flex;justify-content:space-between;align-items:center;'
                f'padding:7px 0;border-bottom:1px solid #EDE9FE">'
                f'<div>'
                f'<span style="font-size:0.82rem;font-weight:700;color:#111827">{_op["ticker"]}</span>'
                f'<span style="font-size:0.72rem;color:#7C3AED;font-weight:600;margin-left:6px">'
                f'{_rec.replace("_"," ")}</span>'
                f'<div style="font-size:0.68rem;color:#9CA3AF;margin-top:1px">{_sub}</div>'
                f'</div>'
                f'<span style="font-size:0.88rem;font-weight:800;color:{_rec_col}">'
                f'{_sc_str}<span style="font-size:0.65rem;color:#9CA3AF">/10</span></span>'
                f'</div>'
            )
        opp_panel = (
            f'<div style="background:#F5F3FF;border:1px solid #DDD6FE;border-radius:12px;'
            f'padding:14px 16px;margin-bottom:12px">'
            f'<div style="font-size:0.68rem;font-weight:700;text-transform:uppercase;'
            f'letter-spacing:0.08em;color:#7C3AED;margin-bottom:10px">'
            f'🌟 New Opportunities · Trending</div>'
            f'{opp_rows}'
            f'<div style="font-size:0.72rem;color:#9CA3AF;margin-top:8px">'
            f'→ <a href="/Predictions" target="_self">Full analysis on Predictions page</a></div>'
            f'</div>'
        )
    else:
        opp_panel = (
            f'<div style="background:#F5F3FF;border:1px dashed #C4B5FD;border-radius:12px;'
            f'padding:12px 16px;margin-bottom:12px">'
            f'<div style="font-size:0.68rem;font-weight:700;text-transform:uppercase;'
            f'letter-spacing:0.08em;color:#7C3AED;margin-bottom:6px">'
            f'🌟 New Opportunities · Trending</div>'
            f'<div style="font-size:0.8rem;color:#6B7280">'
            f'No trending signals yet today. Discoveries are added during morning '
            f'&amp; intraday pipeline runs.</div>'
            f'</div>'
        )

    # ── Render all as sticky block ────────────────────────────────────────────
    _portfolio_panel = (
        f'<div style="background:#fff;border:1px solid #E5E7EB;border-radius:12px;'
        f'padding:14px 16px;margin-bottom:12px">'
        f'<div style="font-size:0.68rem;font-weight:700;text-transform:uppercase;'
        f'letter-spacing:0.08em;color:#2563EB;margin-bottom:10px">'
        f'💼 Portfolio Concentration</div>'
        f'{risk_rows}'
        f'<div style="font-size:0.72rem;color:#9CA3AF;margin-top:8px">'
        f'→ <a href="/Portfolio" target="_self">Full portfolio breakdown</a></div>'
        f'</div>'
    )
    _val_panel = (
        f'<div style="background:#FEF2F2;border:1px solid #FECACA;border-radius:12px;'
        f'padding:14px 16px;margin-bottom:12px">'
        f'<div style="font-size:0.68rem;font-weight:700;text-transform:uppercase;'
        f'letter-spacing:0.08em;color:#991B1B;margin-bottom:10px">Prediction Reliability</div>'
        f'{val_html}'
        f'<div style="font-size:0.72rem;color:#9CA3AF;margin-top:8px">'
        f'→ <a href="/Validation" target="_self">Full validation report</a></div>'
        f'</div>'
    )
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
        + _portfolio_panel
        + opp_panel
        + _val_panel
        + _fresh_panel
        + _macro_panel
        + '</div>',
        unsafe_allow_html=True,
    )
