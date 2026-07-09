"""
APEX Chat -- AI-powered financial assistant with persistent conversation history.

Capabilities:
  • Full stock analysis -- 4-analyst weighted panel (Fundamentals · Research · Macro · News)
  • General financial Q&A -- economy, Fed, rates, inflation, sectors, IPOs, market news
  • Conversational tool-calling -- fetches live data before answering every question
  • Every stock prediction auto-saved to the predictions DB

UX pattern:
  • Left sidebar: all chat sessions grouped by date
  • New Chat button creates a fresh session
  • Messages streamed live with plan card + execution steps
  • Session titles auto-generated from first user message
"""

from __future__ import annotations

import asyncio
import html as _html
import json
import re
import sys
import time
from datetime import datetime, date, timedelta
from pathlib import Path
from threading import Thread
from queue import Queue, Empty

import streamlit as st

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from web.styles import (
    inject_global_css, top_nav, badge_html, rec_badge_html, score_bar_html,
    REC_STYLES, SUCCESS, WARNING, DANGER, PRIMARY, NEUTRAL, NEUTRAL_LIGHT,
    SUCCESS_LIGHT, WARNING_LIGHT, DANGER_LIGHT, PRIMARY_LIGHT,
)

st.set_page_config(
    page_title="APEX Chat · Financial AI",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)
inject_global_css()
top_nav("chat")

# ── Chat-specific CSS (supplements global styles.py) ──────────────────────────
st.markdown("""
<style>
/* User message bubble */
.user-bubble {
    background: #2563EB;
    color: white;
    border-radius: 18px 18px 4px 18px;
    padding: 11px 16px;
    margin: 4px 0;
    max-width: 80%;
    margin-left: auto;
    font-size: 0.88rem;
    line-height: 1.55;
    box-shadow: 0 2px 8px rgba(37,99,235,0.2);
}

/* Prediction card */
.pred-card {
    background: #FFFFFF;
    border: 1px solid #F3F4F6;
    border-radius: 14px;
    overflow: hidden;
    margin: 8px 0;
    box-shadow: 0 1px 4px rgba(0,0,0,0.06), 0 1px 2px rgba(0,0,0,0.04);
}
.pred-header {
    padding: 16px 20px 12px;
    border-bottom: 1px solid #F9FAFB;
}
.pred-body { padding: 16px 20px; }
.pred-scores { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.pred-score-item label {
    font-size: 0.7rem; font-weight: 700; text-transform: uppercase;
    letter-spacing: 0.06em; color: #9CA3AF; display: block; margin-bottom: 4px;
}
</style>
""", unsafe_allow_html=True)


# ── Utilities ─────────────────────────────────────────────────────────────────

_STOP = {
    "I","A","AN","THE","AND","OR","IN","ON","AT","TO","FOR","OF","BY","AS","IS",
    "IT","BE","DO","IF","MY","ME","US","BUY","SELL","HOLD","ASK","GET","RUN","SET",
    "NOT","NO","YES","ALL","ANY","ARE","WAS","HAS","HAD","NEW","OLD","WHAT","HOW",
    "WHY","WHEN","WHO","WILL","FROM","WITH","THAN","THAT","THIS","GIVE","TELL",
    "SHOW","MAKE","TAKE","LOOK","FIND","GOOD","NEXT","LAST","BEST","MORE","MOST",
    "ALSO","JUST","ONLY","BOTH","EACH","EVEN","OVER","MUCH","SOME","ABOUT","INTO",
    "STRONG","WEAK","LONG","SHORT","PUT","CAN","TOP","HIGH","LOW","BIG","SOME",
    "VS","VIA","INC","LTD","ETF","CEO","CFO","CTO","COO","IPO","FED","GDP","CPI",
    "PEG","EPS","ROE","ROI","YOY","QOQ","TTM","DCF","FCF","ARR","MRR","EBIT",
    "STOCK","SHARE","PRICE","MARKET","SECTOR","QUARTER","ANNUAL","REPORT","TODAY",
    "WEEK","MONTH","YEAR","LATEST","RECENT","CURRENT","OUTLOOK","VIEW","THESIS",
}
_TICKER_RE = re.compile(r'\b([A-Z]{1,5})\b')

# Company name → ticker mapping for common queries like "analyze Microsoft"
_COMPANY_MAP: dict[str, str] = {
    "MICROSOFT": "MSFT", "APPLE": "AAPL", "GOOGLE": "GOOGL", "ALPHABET": "GOOGL",
    "AMAZON": "AMZN", "META": "META", "FACEBOOK": "META", "NVIDIA": "NVDA",
    "TESLA": "TSLA", "NETFLIX": "NFLX", "SALESFORCE": "CRM", "ORACLE": "ORCL",
    "INTEL": "INTC", "AMD": "AMD", "QUALCOMM": "QCOM", "BROADCOM": "AVGO",
    "CISCO": "CSCO", "IBM": "IBM", "SAP": "SAP", "ADOBE": "ADBE",
    "PAYPAL": "PYPL", "SHOPIFY": "SHOP", "SPOTIFY": "SPOT", "UBER": "UBER",
    "LYFT": "LYFT", "AIRBNB": "ABNB", "SNAPCHAT": "SNAP", "TWITTER": "X",
    "JPMORGAN": "JPM", "MORGAN": "MS", "GOLDMAN": "GS", "BERKSHIRE": "BRK",
    "BERKSHIRE HATHAWAY": "BRK-B", "JOHNSON": "JNJ", "PFIZER": "PFE",
    "MODERNA": "MRNA", "ABBVIE": "ABBV", "UNITEDHEALTH": "UNH",
    "VISA": "V", "MASTERCARD": "MA", "AMEX": "AXP", "AMERICAN EXPRESS": "AXP",
    "WALMART": "WMT", "TARGET": "TGT", "COSTCO": "COST", "HOME DEPOT": "HD",
    "BOEING": "BA", "LOCKHEED": "LMT", "RAYTHEON": "RTX", "EXXON": "XOM",
    "CHEVRON": "CVX", "SHELL": "SHEL", "BP": "BP", "CATERPILLAR": "CAT",
    "DEERE": "DE", "3M": "MMM", "GENERAL ELECTRIC": "GE", "FORD": "F",
    "GM": "GM", "GENERAL MOTORS": "GM", "STELLANTIS": "STLA", "RIVIAN": "RIVN",
    "LUCID": "LCID", "PALANTIR": "PLTR", "SNOWFLAKE": "SNOW", "DATADOG": "DDOG",
    "CROWDSTRIKE": "CRWD", "PALO ALTO": "PANW", "FORTINET": "FTNT",
    "CLOUDFLARE": "NET", "TWILIO": "TWLO", "ZOOM": "ZM", "DOCUSIGN": "DOCU",
    "COINBASE": "COIN", "ROBINHOOD": "HOOD", "BLOCK": "SQ", "SQUARE": "SQ",
}


def _extract_tickers(text: str) -> list[str]:
    found: list[str] = []
    upper = text.upper()

    # 1. Check multi-word company names first (longest match wins)
    for name in sorted(_COMPANY_MAP, key=len, reverse=True):
        if name in upper:
            ticker = _COMPANY_MAP[name]
            if ticker not in found:
                found.append(ticker)
            # Remove matched region to avoid double-matching sub-strings
            upper = upper.replace(name, " ", 1)

    # 2. Then match bare uppercase ticker-like tokens
    for t in dict.fromkeys(_TICKER_RE.findall(upper)):
        if t not in _STOP and t not in found:
            found.append(t)

    return found


def _load_watchlist() -> list[str]:
    try:
        import yaml
        d = yaml.safe_load((_ROOT / "config" / "watchlist.yaml").read_text()) or {}
        return [str(t).upper() for t in d.get("tickers", [])]
    except Exception:
        return []


def _group_sessions_by_date(sessions: list[dict]) -> dict[str, list[dict]]:
    """Group sessions into Today / Yesterday / Last 7 days / Older."""
    today     = date.today()
    yesterday = today - timedelta(days=1)
    week_ago  = today - timedelta(days=7)
    groups: dict[str, list[dict]] = {
        "Today": [], "Yesterday": [], "Last 7 days": [], "Older": [],
    }
    for s in sessions:
        try:
            d = date.fromisoformat(s["updated_at"][:10])
        except Exception:
            d = date.min
        if d == today:
            groups["Today"].append(s)
        elif d == yesterday:
            groups["Yesterday"].append(s)
        elif d >= week_ago:
            groups["Last 7 days"].append(s)
        else:
            groups["Older"].append(s)
    return groups


# ── Plan builder ──────────────────────────────────────────────────────────────

def _build_plan(ticker: str) -> dict:
    from portfolio_agent.tools.fundamentals_db import get_stored_fundamentals
    from portfolio_agent.tools.research_db import get_stored_research
    from portfolio_agent.tools.prediction_db import get_latest_prediction
    import sqlite3

    plan: dict = {"ticker": ticker, "sources": [], "gaps": []}

    fund = get_stored_fundamentals(ticker)
    if fund:
        plan["sources"].append({
            "icon": "✅", "label": "Fundamentals",
            "detail": f"As of {fund.get('as_of_date','?')} · Score {fund.get('fundamental_score','?')}/10",
            "live": False,
        })
    else:
        plan["gaps"].append("fundamentals")
        plan["sources"].append({"icon": "⚡", "label": "Fundamentals",
                                "detail": "Not in DB -- live agent will run", "live": True})

    res = get_stored_research(ticker)
    if res:
        fetched = (res.get("raw_fetched_at") or res.get("as_of_date") or "?")[:10]
        stale_7d = fetched < (date.today() - timedelta(days=7)).isoformat() if fetched != "?" else False
        plan["sources"].append({
            "icon": "⚠️" if stale_7d else "✅",
            "label": "Broker Research",
            "detail": (
                f"Last fetched {fetched} (>7d old -- using cached) · {res.get('consensus','?')}"
                if stale_7d else
                f"Fetched {fetched} · {res.get('consensus','?')}"
            ),
            "live": False,
        })
    else:
        plan["gaps"].append("research")
        plan["sources"].append({"icon": "⚡", "label": "Broker Research",
                                "detail": "Not in DB -- live agent will run", "live": True})

    news_data = []
    try:
        cutoff = (date.today() - timedelta(days=7)).isoformat()
        conn = sqlite3.connect(str(_ROOT / "data" / "portfolio.db"))
        conn.row_factory = sqlite3.Row
        rows_news = conn.execute(
            """SELECT date, headline_1, headline_2, sentiment, sentiment_score, top_themes, trending
               FROM news_daily_update WHERE ticker=? AND date>=? AND row_type='ticker'
               ORDER BY date DESC""",
            [ticker, cutoff],
        ).fetchall()
        news_data = [dict(r) for r in rows_news]
        conn.close()
        cnt = len(news_data)
        if cnt > 0:
            plan["sources"].append({"icon": "✅", "label": "News (7d)",
                                    "detail": f"{cnt} records found", "live": False})
        else:
            plan["gaps"].append("news")
            plan["sources"].append({"icon": "⚡", "label": "News (7d)",
                                    "detail": "No recent records -- live agent will run", "live": True})
    except Exception:
        plan["gaps"].append("news")
        plan["sources"].append({"icon": "⚡", "label": "News (7d)",
                                "detail": "DB unavailable -- live agent will run", "live": True})

    plan["sources"].append({"icon": "📡", "label": "Macro Snapshot",
                            "detail": "VIX · 10Y yield · S&P trend (yfinance)", "live": True})

    prior = get_latest_prediction(ticker)
    if prior:
        plan["prior"] = prior
        plan["sources"].append({
            "icon": "📜", "label": "Prior Prediction",
            "detail": f"{prior.get('recommendation','?')} on {prior.get('created_at','')[:10]} "
                      f"· Confidence {prior.get('confidence','?')}/10",
            "live": False,
        })
    else:
        plan["sources"].append({"icon": "🆕", "label": "Prior Prediction",
                                "detail": "First analysis for this ticker", "live": False})

    # Compute dynamic weights (best-effort -- use defaults if macro fetch fails)
    try:
        from portfolio_agent.tools.reasoning_tools import get_macro_snapshot
        from portfolio_agent.tools.weight_engine import compute_dynamic_weights
        import json as _json
        macro = _json.loads(get_macro_snapshot())
        weight_data = compute_dynamic_weights(
            ticker=ticker,
            news_data=news_data,
            research_data=res or {},
            macro_snapshot=macro,
            fundamentals_data=fund or {},
        )
        plan["weight_data"] = weight_data
    except Exception:
        plan["weight_data"] = None

    return plan


# ── Rendering helpers ─────────────────────────────────────────────────────────

def _render_plan_card(plan: dict) -> None:
    ticker = plan["ticker"]
    gaps   = plan.get("gaps", [])
    wd     = plan.get("weight_data")

    rows = ""
    for s in plan["sources"]:
        dot_color = "#DC2626" if s["live"] else "#059669"
        rows += (
            f'<div style="display:flex;gap:10px;padding:8px 0;border-bottom:1px solid #F1F5F9">'
            f'<span style="font-size:1rem;width:20px;text-align:center">{s["icon"]}</span>'
            f'<div style="flex:1">'
            f'<span style="font-weight:600;font-size:0.875rem;color:#0F172A">{s["label"]}</span>'
            f'<br><span style="font-size:0.8rem;color:#64748B">{s["detail"]}</span></div>'
            f'<span style="width:8px;height:8px;border-radius:50%;background:{dot_color};'
            f'margin-top:6px;flex-shrink:0;display:inline-block"></span>'
            f'</div>'
        )

    live_banner = ""
    if gaps:
        live_banner = (
            f'<div style="background:#EFF6FF;border:1px solid #BFDBFE;border-radius:8px;'
            f'padding:8px 12px;margin-bottom:12px;font-size:0.83rem;color:#1E40AF">'
            f'⚡ <strong>{len(gaps)} live agent(s)</strong> will run to fill data gaps: '
            f'{", ".join(gaps)}. Results stored for future use.</div>'
        )

    # Dynamic weight badge -- show regime + per-source weights
    BASE = {"fundamentals": 0.40, "research": 0.30, "macro": 0.20, "news": 0.10}
    if wd and wd.get("weights"):
        w = wd["weights"]
        regime = wd.get("regime", "QUIET_DAY")
        regime_colors = {
            "EARNINGS_RELEASE": ("#DC2626", "#FEF2F2"),
            "LEADERSHIP_CHANGE": ("#DC2626", "#FEF2F2"),
            "MA_EVENT": ("#D97706", "#FFFBEB"),
            "EXTREME_VOLATILITY": ("#DC2626", "#FEF2F2"),
            "HIGH_VOLATILITY": ("#D97706", "#FFFBEB"),
            "RISK_OFF": ("#D97706", "#FFFBEB"),
            "RISK_ON": ("#059669", "#F0FDF4"),
            "SECTOR_ROTATION": ("#7C3AED", "#F5F3FF"),
            "RESEARCH_BOOST": ("#2563EB", "#EFF6FF"),
            "QUIET_DAY": ("#64748B", "#F8FAFC"),
        }
        rc, rb = regime_colors.get(regime, ("#64748B", "#F8FAFC"))

        def _delta(key: str) -> str:
            diff = round((w.get(key, 0) - BASE.get(key, 0)) * 100)
            if diff > 0:
                return f'<span style="color:#059669;font-size:0.7rem"> +{diff}%</span>'
            elif diff < 0:
                return f'<span style="color:#DC2626;font-size:0.7rem"> {diff}%</span>'
            return ""

        weight_badge = (
            f'<div style="background:{rb};border:1px solid {rc}44;border-radius:8px;'
            f'padding:8px 12px;margin-bottom:12px">'
            f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">'
            f'<span style="background:{rc};color:white;padding:2px 8px;border-radius:4px;'
            f'font-size:0.72rem;font-weight:700;letter-spacing:0.04em">{regime.replace("_"," ")}</span>'
            f'<span style="font-size:0.78rem;color:{rc};font-weight:600">Dynamic weight regime</span>'
            f'</div>'
            f'<div style="display:flex;gap:16px;flex-wrap:wrap">'
            + "".join(
                f'<span style="font-size:0.8rem;color:#374151">'
                f'<strong style="color:#0F172A">{lbl}</strong> '
                f'{round(w.get(key,0)*100)}%{_delta(key)}</span>'
                for lbl, key in [
                    ("📋 Fundamentals", "fundamentals"),
                    ("🔬 Research", "research"),
                    ("🌐 Macro", "macro"),
                    ("📰 News", "news"),
                ]
            )
            + f'</div>'
            + (
                f'<div style="margin-top:4px;font-size:0.75rem;color:#64748B">'
                f'{wd.get("weight_summary","")}</div>'
                if wd.get("weight_summary") else ""
            )
            + f'</div>'
        )
    else:
        weight_badge = (
            f'<div style="background:#F8FAFC;border:1px solid #E2E8F0;border-radius:8px;'
            f'padding:6px 12px;margin-bottom:12px;font-size:0.78rem;color:#64748B">'
            f'📋 Fundamentals 40% · 🔬 Research 30% · 🌐 Macro 20% · 📰 News 10% '
            f'<em>(default weights -- macro fetch pending)</em></div>'
        )

    steps_html = "".join(
        f'<div style="display:flex;gap:8px;padding:5px 0;font-size:0.83rem;color:#475569">'
        f'<span style="font-weight:700;color:#2563EB;min-width:20px">{i}.</span>'
        f'<span>{s}</span></div>'
        for i, s in enumerate([
            "Load all stored context + live macro snapshot",
            "Panel opening: each analyst states score & verdict",
            "Cross-examination: analysts challenge divergent views",
            "History check: compare with prior recommendations",
            "Weighted synthesis using the dynamic weights above → final recommendation",
        ], 1)
    )

    st.markdown(
        f'<div style="background:white;border:1px solid #E2E8F0;border-radius:12px;'
        f'padding:16px 20px;margin-bottom:12px;box-shadow:0 1px 3px rgba(0,0,0,0.05)">'
        f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">'
        f'<h4 style="margin:0;color:#0F172A;font-size:1rem">🗺️ Execution Plan -- <code>{ticker}</code></h4>'
        f'</div>'
        f'{weight_badge}'
        f'{live_banner}{rows}'
        f'<details style="margin-top:10px"><summary style="cursor:pointer;color:#64748B;'
        f'font-size:0.8rem;font-weight:600">Deliberation steps</summary>'
        f'<div style="margin-top:8px">{steps_html}</div></details>'
        f'</div>',
        unsafe_allow_html=True,
    )


def _render_prediction_card(data: dict, elapsed: float = 0.0,
                             show_history: bool = True) -> None:
    rec   = data.get("recommendation", "HOLD")
    pred  = data.get("prediction", "NEUTRAL")
    col, bg, label = REC_STYLES.get(rec, (NEUTRAL, NEUTRAL_LIGHT, rec))
    conf  = data.get("confidence")
    comp  = data.get("composite_score")

    wu = data.get("weights_used") or {}
    def _wpct(key: str, fallback: float) -> str:
        v = wu.get(key)
        return f"{round(v*100)}%" if v is not None else f"{round(fallback*100)}%"

    scores_html = "".join(
        f'<div class="pred-score-item">'
        f'<label>{lbl} ({_wpct(wkey, fb)})</label>'
        f'{score_bar_html(data.get(key), color=bar_col)}'
        f'</div>'
        for lbl, key, wkey, fb, bar_col in [
            ("Fundamentals", "fundamental_score", "fundamentals", 0.40, SUCCESS),
            ("Research",     "research_score",    "research",     0.30, PRIMARY),
            ("Macro",        "macro_score",        "macro",        0.20, WARNING),
            ("News",         "news_score",         "news",         0.10, "#7C3AED"),
        ]
    )

    panel = data.get("panel_summary") or {}
    panel_html = ""
    if panel:
        names = [
            ("Fundamentals Analyst",  "chen_verdict",  "Fundamentals"),
            ("Research Analyst",      "webb_verdict",  "Research"),
            ("Macro Analyst",         "varga_verdict", "Macro"),
            ("News Analyst",          "park_verdict",  "News"),
        ]
        panel_html = '<div style="margin-top:14px;border-top:1px solid #F1F5F9;padding-top:12px">'
        panel_html += '<div style="margin:0 0 8px;font-size:0.8rem;font-weight:700;color:#64748B;text-transform:uppercase;letter-spacing:0.05em">Panel Verdicts</div>'
        for name, key, role in names:
            verdict = _html.escape(str(panel.get(key, "--")))
            panel_html += (
                f'<div style="display:flex;gap:8px;margin-bottom:6px;font-size:0.83rem">'
                f'<span style="font-weight:600;color:#475569;min-width:130px">{name}</span>'
                f'<span style="color:#64748B">{role} ·</span>'
                f'<span style="color:#0F172A">{verdict}</span></div>'
            )
        if panel.get("key_debate"):
            panel_html += (
                f'<div style="background:#FEF9C3;border:1px solid #FDE68A;border-radius:6px;'
                f'padding:6px 10px;margin-top:8px;font-size:0.8rem;color:#92400E">'
                f'💬 <strong>Key debate:</strong> {_html.escape(str(panel["key_debate"]))}</div>'
            )
        panel_html += "</div>"

    # ── Analyst Price Target table ─────────────────────────────────────────────
    pt_html = ""
    _pt_mean    = data.get("pt_mean")
    _pt_median  = data.get("pt_median")
    _pt_high    = data.get("pt_high")
    _pt_low     = data.get("pt_low")
    _pt_n       = data.get("pt_num_analysts")
    _pt_cur     = data.get("pt_current_price")
    if any(v is not None for v in [_pt_mean, _pt_median, _pt_high, _pt_low]):
        def _pt_fmt(v, cur=None):
            if v is None:
                return '<span style="color:#94A3B8">--</span>'
            s = f"${v:,.2f}"
            if cur:
                pct = (v - cur) / cur * 100
                sign = "+" if pct >= 0 else ""
                color = "#059669" if pct >= 0 else "#DC2626"
                s += f' <span style="color:{color};font-size:0.75rem">({sign}{pct:.1f}%)</span>'
            return s
        _n_str = f"{_pt_n} analyst{'s' if _pt_n and _pt_n != 1 else ''}" if _pt_n else ""
        _n_chip = (
            f' &nbsp;&middot;&nbsp; <span style="font-weight:400">{_n_str}</span>'
            if _n_str else ""
        )
        _cur_chip = (
            f' &nbsp;&middot;&nbsp; <span style="color:#64748B;font-weight:400">'
            f'Current ${_pt_cur:,.2f}</span>'
            if _pt_cur else ""
        )
        pt_html = (
            f'<div style="margin-top:14px;border-top:1px solid #F1F5F9;padding-top:12px">'
            f'<div style="font-size:0.8rem;font-weight:700;color:#64748B;text-transform:uppercase;'
            f'letter-spacing:0.05em;margin-bottom:8px">Analyst Price Targets'
            f'{_n_chip}{_cur_chip}'
            f'</div>'
            f'<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px">'
        )
        for lbl, val, show_pct in [
            ("Mean",   _pt_mean,   True),
            ("Median", _pt_median, True),
            ("High",   _pt_high,   True),
            ("Low",    _pt_low,    True),
        ]:
            pt_html += (
                f'<div style="background:#F8FAFC;border:1px solid #E2E8F0;border-radius:8px;'
                f'padding:8px 10px;text-align:center">'
                f'<div style="font-size:0.72rem;font-weight:600;color:#64748B;'
                f'text-transform:uppercase;margin-bottom:4px">{lbl}</div>'
                f'<div style="font-size:0.9rem;font-weight:700;color:#0F172A">'
                f'{_pt_fmt(val, _pt_cur if show_pct else None)}</div>'
                f'</div>'
            )
        pt_html += '</div></div>'

    # ── Multi-horizon forecast table ───────────────────────────────────────────
    horizons_html = ""
    _horizons = data.get("horizons") or []
    if _horizons and isinstance(_horizons, list):
        _dir_color = {"UP": "#059669", "DOWN": "#DC2626", "FLAT": "#64748B"}
        _dir_icon  = {"UP": "↑", "DOWN": "↓", "FLAT": "→"}
        _hz_label  = {5: "5d · Week", 21: "21d · Month", 63: "63d · Quarter"}
        rows_html = ""
        for _hrow in sorted(_horizons, key=lambda x: x.get("horizon_days", 99)):
            _hd   = _hrow.get("horizon_days", "?")
            _dir  = (_hrow.get("predicted_direction") or "").upper()
            _rlo  = _hrow.get("predicted_return_low")
            _rhi  = _hrow.get("predicted_return_high")
            _conv = _hrow.get("conviction_score")
            _rtxt = _hrow.get("reasoning_text", "")
            _dcol = _dir_color.get(_dir, "#64748B")
            _dico = _dir_icon.get(_dir, "?")
            _range_str = (
                f'<span style="color:{_dcol};font-weight:700">'
                f'{_dico} {_rlo:+.1f}% to {_rhi:+.1f}%</span>'
                if _rlo is not None and _rhi is not None
                else f'<span style="color:{_dcol};font-weight:700">{_dico} {_dir}</span>'
                if _dir else '<span style="color:#94A3B8">--</span>'
            )
            _conv_str = (
                f'<span style="font-size:0.8rem;color:#475569">{_conv}/10</span>'
                if _conv is not None else '<span style="color:#94A3B8">--</span>'
            )
            _lbl = _hz_label.get(_hd, f"{_hd}d")
            rows_html += (
                f'<tr>'
                f'<td style="padding:6px 10px;font-weight:600;color:#0F172A;'
                f'white-space:nowrap">{_lbl}</td>'
                f'<td style="padding:6px 10px">{_range_str}</td>'
                f'<td style="padding:6px 10px;text-align:center">{_conv_str}</td>'
                f'<td style="padding:6px 10px;font-size:0.78rem;color:#475569">'
                f'{_html.escape(_rtxt[:120])}</td>'
                f'</tr>'
            )
        if rows_html:
            horizons_html = (
                f'<div style="margin-top:14px;border-top:1px solid #F1F5F9;padding-top:12px">'
                f'<div style="font-size:0.8rem;font-weight:700;color:#64748B;'
                f'text-transform:uppercase;letter-spacing:0.05em;margin-bottom:8px">'
                f'Horizon Forecasts</div>'
                f'<table style="width:100%;border-collapse:collapse;font-size:0.83rem">'
                f'<thead><tr style="background:#F8FAFC">'
                f'<th style="padding:5px 10px;text-align:left;color:#64748B;'
                f'font-weight:600;font-size:0.75rem">Horizon</th>'
                f'<th style="padding:5px 10px;text-align:left;color:#64748B;'
                f'font-weight:600;font-size:0.75rem">Return Range</th>'
                f'<th style="padding:5px 10px;text-align:center;color:#64748B;'
                f'font-weight:600;font-size:0.75rem">Conviction</th>'
                f'<th style="padding:5px 10px;text-align:left;color:#64748B;'
                f'font-weight:600;font-size:0.75rem">Rationale</th>'
                f'</tr></thead><tbody>{rows_html}</tbody></table></div>'
            )

    changed_html = ""
    if data.get("changed_from_previous"):
        changed_html = (
            f'<div style="background:#FEF3C7;border:1px solid #FDE68A;border-radius:6px;'
            f'padding:6px 10px;margin-top:8px;font-size:0.8rem;color:#92400E">'
            f'🔄 Recommendation changed from '
            f'<strong>{data.get("previous_recommendation","?")}</strong> → '
            f'<strong>{rec.replace("_"," ")}</strong></div>'
        )

    target_html = ""
    if data.get("target_price"):
        target_html = (
            f'<span style="background:#F0FDF4;color:#166534;border:1px solid #BBF7D0;'
            f'border-radius:6px;padding:3px 10px;font-size:0.8rem;font-weight:600;margin-right:6px">'
            f'🎯 Target ${data["target_price"]:.2f}</span>'
        )
    horizon_html = (
        f'<span style="background:#F1F5F9;color:#475569;border-radius:6px;'
        f'padding:3px 10px;font-size:0.8rem;font-weight:600">⏱ {data.get("horizon","1m")}</span>'
    )

    data_sources_html = (
        "&nbsp;· " + " · ".join(data.get("data_sources", []))
        if data.get("data_sources") else ""
    )
    reasoning_html = (
        f'<div style="margin:12px 0 0;font-size:0.875rem;color:#0F172A;line-height:1.6;'
        f'border-top:1px solid #F1F5F9;padding-top:12px">'
        f'{_html.escape(data.get("reasoning", ""))}</div>'
        if data.get("reasoning") else ""
    )

    # Build as one compact string -- CommonMark terminates an HTML block on a blank line,
    # so any blank line inside st.markdown HTML causes the rest to render as raw text.
    card_html = (
        f'<div class="pred-card">'
        f'<div class="pred-header" style="background:{bg}18;border-bottom:2px solid {col}22">'
        f'<div style="display:flex;justify-content:space-between;align-items:flex-start">'
        f'<div>'
        f'<span style="font-size:1.5rem;font-weight:800;color:{col}">{label}</span>'
        f'<span style="font-size:0.85rem;color:{NEUTRAL};margin-left:10px">'
        f'{pred} · {_html.escape(str(data.get("ticker","")))}</span>'
        f'<div style="margin-top:6px">{target_html}{horizon_html}</div>'
        f'</div>'
        f'<div style="text-align:right">'
        f'<div style="font-size:0.78rem;color:#64748B;font-weight:600;text-transform:uppercase;letter-spacing:0.04em">Composite</div>'
        f'<div style="font-size:1.8rem;font-weight:800;color:#0F172A;line-height:1.1">{comp or "--"}'
        f'<span style="font-size:1rem;color:#94A3B8">/10</span></div>'
        f'<div style="font-size:0.78rem;color:#64748B">Confidence: {conf or "--"}/10</div>'
        f'</div></div></div>'
        f'<div class="pred-body">'
        f'<div class="pred-scores">{scores_html}</div>'
        f'{pt_html}'
        f'{horizons_html}'
        f'{panel_html}'
        f'{changed_html}'
        f'{reasoning_html}'
        f'<div style="margin:10px 0 0;font-size:0.75rem;color:#94A3B8">'
        f'Generated in {elapsed:.1f}s{data_sources_html}</div>'
        f'</div></div>'
    )
    st.markdown(card_html, unsafe_allow_html=True)

    if show_history:
        from portfolio_agent.tools.prediction_db import get_prediction_history
        hist = get_prediction_history(data.get("ticker",""), limit=6)
        if len(hist) > 1:
            with st.expander("📜 Prediction history for this ticker"):
                rows = [
                    {"Date": h.get("created_at","")[:10],
                     "Horizon": f"{h['horizon_days']}d" if h.get("horizon_days") else h.get("horizon","--"),
                     "Recommendation": h.get("recommendation",""),
                     "Direction": h.get("predicted_direction","--"),
                     "Return Range": (
                         f"{h['predicted_return_low']:+.1f}% to {h['predicted_return_high']:+.1f}%"
                         if h.get("predicted_return_low") is not None and h.get("predicted_return_high") is not None
                         else "--"
                     ),
                     "Conviction": h.get("conviction_score","--"),
                     "Confidence": h.get("confidence",""),
                     "Composite": h.get("composite_score",""),
                     "Changed": "↑↓" if h.get("changed_from_previous") else "--"}
                    for h in hist
                ]
                st.dataframe(rows, hide_index=True, use_container_width=True)


# ── Conversational chat agent ─────────────────────────────────────────────────

_CHAT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web for current financial news, events, data, or facts. "
                "Use this as a FALLBACK when internal tools (get_market_news, get_ticker_news, "
                "get_macro_snapshot, etc.) return no relevant information, or when the question "
                "requires real-time data that may not be in the local database -- "
                "e.g. breaking news, recent earnings, new IPO filings, policy changes, "
                "live price moves, or any event from the last few days."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query. Be specific -- include company name, ticker, date, or event type.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Number of results to return (default 5, max 10).",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_ticker",
            "description": "Look up the stock ticker symbol for a company by name. Use this when the user mentions a company name and you need to find its ticker before calling other tools.",
            "parameters": {
                "type": "object",
                "properties": {
                    "company_name": {
                        "type": "string",
                        "description": "Company name or search query, e.g. 'Duolingo', 'Palantir Technologies'",
                    }
                },
                "required": ["company_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_ticker_news",
            "description": "Get recent news headlines, sentiment, and themes for a specific stock ticker (7-day lookback from DB + live fallback).",
            "parameters": {
                "type": "object",
                "properties": {"ticker": {"type": "string", "description": "Stock ticker symbol"}},
                "required": ["ticker"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_market_news",
            "description": "Get the latest general financial/market news headlines from Reuters, Yahoo Finance, CNBC, MarketWatch. Use for questions about market trends, economy, macro events, IPOs, M&A, or any broad financial news.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Number of headlines to return (default 20, max 30)",
                        "default": 20,
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_ipo_info",
            "description": "Get information about recent or upcoming IPOs, or company listing details for a given ticker. Use for IPO-related questions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {
                        "type": "string",
                        "description": "Optional ticker of a recently IPO'd company. Leave empty for a general recent IPO list.",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_sector_performance",
            "description": "Get performance data for major market sectors (Technology, Healthcare, Finance, Energy, etc.) and broad indices (S&P 500, Nasdaq, Dow Jones). Use for sector rotation questions or broad market performance.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_fundamentals",
            "description": "Get financial fundamentals for a specific stock: revenue growth, net margin, FCF, debt/equity, fundamental score, key strengths and risks.",
            "parameters": {
                "type": "object",
                "properties": {"ticker": {"type": "string"}},
                "required": ["ticker"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_research",
            "description": "Get broker/analyst research for a specific stock: consensus rating, price targets (mean/median/high/low), recent upgrades and downgrades.",
            "parameters": {
                "type": "object",
                "properties": {"ticker": {"type": "string"}},
                "required": ["ticker"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_macro_snapshot",
            "description": "Get current macro environment: VIX, 10-year treasury yield, S&P 500 1-month trend, yield curve spread, macro regime.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_prediction_history",
            "description": "Get APEX prediction history for a ticker (last 10 predictions with scores and reasoning).",
            "parameters": {
                "type": "object",
                "properties": {"ticker": {"type": "string"}},
                "required": ["ticker"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_latest_opportunities",
            "description": (
                "Query the local database for the latest trending/new investment opportunities "
                "discovered by the pipeline. These are tickers analyzed as 'trending_opportunity' "
                "with BUY or STRONG_BUY signals. Use this FIRST when the user asks about: "
                "new opportunities, trending stocks, what stocks look good, latest discoveries, "
                "what should I invest in, top picks, opportunity stocks, trending investments."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "min_confidence": {
                        "type": "integer",
                        "description": "Minimum confidence score (1-10, default 6)",
                        "default": 6,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results to return (default 10)",
                        "default": 10,
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_portfolio_summary",
            "description": (
                "Get current portfolio holdings from the local database: tickers, shares, "
                "current value, cost basis, weight%, and latest APEX recommendation for each. "
                "Use when user asks about their portfolio, what they own, their holdings, "
                "portfolio performance, or portfolio composition."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_predictions_summary",
            "description": (
                "Get the latest APEX predictions for all tickers in the database — both portfolio "
                "holdings and any trending tickers analyzed today. Returns recommendation, "
                "confidence, composite score, and scores breakdown per ticker. "
                "Use when user asks: what are today's predictions, latest signals, "
                "what does APEX say, summary of all recommendations, or any broad question "
                "about the system's current views."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "segment": {
                        "type": "string",
                        "description": "Filter: 'all' (default), 'portfolio', or 'opportunities'",
                        "default": "all",
                    },
                    "recommendation": {
                        "type": "string",
                        "description": "Optional filter: 'BUY', 'SELL', 'HOLD', 'STRONG_BUY', 'STRONG_SELL'",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_price_history",
            "description": "Get price performance for a specific stock: latest close, period change %, 52-week high/low, average volume.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "period": {
                        "type": "string",
                        "description": "Time period: 1mo, 3mo, 6mo, 1y, 2y",
                        "default": "3mo",
                    },
                },
                "required": ["ticker"],
            },
        },
    },
]


def _chat_tool_get_news(ticker: str) -> dict:
    from portfolio_agent.tools.reasoning_tools import _get_recent_news
    news = _get_recent_news(ticker.upper(), days=7)
    if news:
        return {"source": "db", "ticker": ticker.upper(), "news": news}
    try:
        from portfolio_agent.tools.news_sources import get_ticker_news_merged
        arts = json.loads(get_ticker_news_merged(ticker, limit=12, max_age_hours=168))
        return {"source": "live", "ticker": ticker.upper(),
                "articles": arts.get("articles", [])[:8]}
    except Exception:
        return {"ticker": ticker.upper(), "note": "No news data available in the last 7 days."}


def _chat_tool_get_fundamentals(ticker: str) -> dict:
    from portfolio_agent.tools.fundamentals_db import get_stored_fundamentals
    data = get_stored_fundamentals(ticker.upper())
    if data:
        return {"source": "db", "ticker": ticker.upper(), "fundamentals": data}
    return {"ticker": ticker.upper(),
            "note": "No stored fundamentals. Run a full analysis to populate."}


def _chat_tool_get_research(ticker: str) -> dict:
    from portfolio_agent.tools.research_db import get_stored_research
    data = get_stored_research(ticker.upper())
    if data and (data.get("as_of_date") or data.get("raw_fetched_at")):
        return {"source": "db", "ticker": ticker.upper(), "research": data}
    try:
        from portfolio_agent.tools.broker_research import get_broker_research
        d = json.loads(get_broker_research(ticker))
        return {"source": "live", "ticker": ticker.upper(), "research": d}
    except Exception:
        return {"ticker": ticker.upper(), "note": "Research data unavailable."}


def _chat_tool_get_macro() -> dict:
    from portfolio_agent.tools.reasoning_tools import get_macro_snapshot
    try:
        return json.loads(get_macro_snapshot())
    except Exception:
        return {"note": "Macro data temporarily unavailable."}


def _chat_tool_get_predictions(ticker: str) -> dict:
    from portfolio_agent.tools.prediction_db import get_prediction_history
    hist = get_prediction_history(ticker.upper(), limit=10)
    return {"ticker": ticker.upper(), "prediction_history": hist}


def _chat_tool_get_price_history(ticker: str, period: str = "3mo") -> dict:
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period=period)
        if hist.empty:
            return {"ticker": ticker.upper(), "note": "No price data available."}
        yr = yf.Ticker(ticker).history(period="1y")
        return {
            "ticker":       ticker.upper(),
            "period":       period,
            "latest_close": round(float(hist["Close"].iloc[-1]), 2),
            "period_start": round(float(hist["Close"].iloc[0]), 2),
            "change_pct":   round((hist["Close"].iloc[-1] / hist["Close"].iloc[0] - 1) * 100, 2),
            "period_high":  round(float(hist["High"].max()), 2),
            "period_low":   round(float(hist["Low"].min()), 2),
            "52w_high":     round(float(yr["High"].max()), 2) if not yr.empty else None,
            "52w_low":      round(float(yr["Low"].min()), 2) if not yr.empty else None,
            "avg_volume":   int(hist["Volume"].mean()),
        }
    except Exception as exc:
        return {"ticker": ticker.upper(), "note": f"Price data unavailable: {exc}"}


def _chat_tool_get_market_news(limit: int = 20) -> dict:
    """Fetch general market news from RSS feeds (Reuters, Yahoo Finance, CNBC, MarketWatch)."""
    # Try DB-stored market news first (today's items)
    try:
        from portfolio_agent.tools.news_db import get_todays_market_news
        db_result = json.loads(get_todays_market_news())
        if db_result.get("count", 0) >= 5:
            return {"source": "db", **db_result}
    except Exception:
        pass
    # Fall back to live RSS
    try:
        from portfolio_agent.tools.news_sources import get_market_news
        return {"source": "live", **json.loads(get_market_news(limit=min(limit, 30)))}
    except Exception as exc:
        return {"note": f"Market news temporarily unavailable: {exc}"}


def _chat_tool_get_ipo_info(ticker: str = "") -> dict:
    """Fetch IPO details for a specific ticker, or recent IPO activity from yfinance."""
    try:
        import yfinance as yf
        if ticker:
            t = yf.Ticker(ticker.upper())
            info = t.info or {}
            return {
                "ticker": ticker.upper(),
                "company_name": info.get("longName"),
                "sector": info.get("sector"),
                "industry": info.get("industry"),
                "ipo_date": None,  # yfinance doesn't directly expose this
                "market_cap": info.get("marketCap"),
                "shares_outstanding": info.get("sharesOutstanding"),
                "float_shares": info.get("floatShares"),
                "current_price": info.get("currentPrice") or info.get("regularMarketPrice"),
                "52w_high": info.get("fiftyTwoWeekHigh"),
                "52w_low": info.get("fiftyTwoWeekLow"),
                "exchange": info.get("exchange"),
                "description": (info.get("longBusinessSummary") or "")[:400],
            }
        # No ticker -- pull recent IPO news from RSS
        from portfolio_agent.tools.news_sources import get_market_news
        raw = json.loads(get_market_news(limit=30))
        ipo_articles = [
            a for a in raw.get("articles", [])
            if any(kw in (a.get("title", "") + a.get("summary", "")).lower()
                   for kw in ("ipo", "initial public offering", "goes public", "listing", "debut"))
        ]
        return {"source": "rss", "ipo_news": ipo_articles[:10],
                "note": "Filtered IPO-related headlines from financial RSS feeds."}
    except Exception as exc:
        return {"note": f"IPO data unavailable: {exc}"}


def _chat_tool_get_sector_performance() -> dict:
    """Fetch performance for major sector ETFs and broad indices."""
    try:
        import yfinance as yf
        symbols = {
            "S&P 500": "^GSPC",
            "Nasdaq": "^IXIC",
            "Dow Jones": "^DJI",
            "Russell 2000": "^RUT",
            "Technology (XLK)": "XLK",
            "Healthcare (XLV)": "XLV",
            "Financials (XLF)": "XLF",
            "Energy (XLE)": "XLE",
            "Consumer Disc (XLY)": "XLY",
            "Consumer Staples (XLP)": "XLP",
            "Industrials (XLI)": "XLI",
            "Materials (XLB)": "XLB",
            "Real Estate (XLRE)": "XLRE",
            "Utilities (XLU)": "XLU",
            "Communication (XLC)": "XLC",
        }
        results = {}
        tickers_str = " ".join(symbols.values())
        data = yf.download(tickers_str, period="1mo", auto_adjust=True, progress=False)
        closes = data["Close"] if "Close" in data.columns else data
        for name, sym in symbols.items():
            try:
                col = closes[sym] if sym in closes.columns else None
                if col is not None and len(col.dropna()) >= 2:
                    start = float(col.dropna().iloc[0])
                    end = float(col.dropna().iloc[-1])
                    results[name] = {
                        "symbol": sym,
                        "latest": round(end, 2),
                        "1mo_change_pct": round((end / start - 1) * 100, 2),
                    }
            except Exception:
                continue
        return {"period": "1 month", "sectors": results}
    except Exception as exc:
        return {"note": f"Sector data unavailable: {exc}"}


def _chat_tool_web_search(query: str, max_results: int = 5) -> dict:
    """Search the web via DuckDuckGo -- no API key required."""
    try:
        from ddgs import DDGS
        results = DDGS().text(query, max_results=min(max_results, 10))
        if not results:
            return {"query": query, "results": [], "note": "No results found."}
        return {
            "query": query,
            "results": [
                {
                    "title": r.get("title", ""),
                    "url":   r.get("href", ""),
                    "snippet": r.get("body", "")[:400],
                }
                for r in results
            ],
        }
    except Exception as exc:
        return {"query": query, "error": str(exc), "note": "Web search unavailable."}


def _chat_tool_search_ticker(company_name: str) -> dict:
    ticker, display = _search_ticker_by_name(company_name)
    if ticker:
        return {"company": company_name, "ticker": ticker, "name": display,
                "note": f"Use '{ticker}' as the ticker symbol for subsequent tool calls."}
    return {"company": company_name, "ticker": None,
            "note": "Could not find a ticker for this company. Ask the user to provide the ticker symbol."}


def _chat_tool_get_latest_opportunities(min_confidence: int = 6, limit: int = 10) -> dict:
    """Query DB for trending_opportunity predictions with BUY/STRONG_BUY."""
    import sqlite3
    db_path = _ROOT / "data" / "portfolio.db"
    if not db_path.exists():
        return {"note": "Database not found. Run the pipeline first to populate data."}
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT p.ticker, p.recommendation, p.confidence, p.composite_score,
                   p.news_score, p.research_score, p.fundamental_score,
                   p.reasoning, p.created_at
            FROM predictions p
            JOIN (
                SELECT ticker, MAX(created_at) dt
                FROM predictions WHERE trigger_type='trending_opportunity'
                GROUP BY ticker
            ) x ON p.ticker=x.ticker AND p.created_at=x.dt
            WHERE p.trigger_type='trending_opportunity'
              AND p.recommendation IN ('BUY','STRONG_BUY')
              AND COALESCE(p.confidence, 0) >= ?
            ORDER BY p.composite_score DESC NULLS LAST
            LIMIT ?
        """, [min_confidence, limit]).fetchall()
        conn.close()
        opportunities = [dict(r) for r in rows]
        if not opportunities:
            # Fall back to ANY trending_opportunity regardless of recommendation
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT p.ticker, p.recommendation, p.confidence, p.composite_score,
                       p.news_score, p.research_score, p.fundamental_score,
                       p.reasoning, p.created_at
                FROM predictions p
                JOIN (
                    SELECT ticker, MAX(created_at) dt
                    FROM predictions WHERE trigger_type='trending_opportunity'
                    GROUP BY ticker
                ) x ON p.ticker=x.ticker AND p.created_at=x.dt
                WHERE p.trigger_type='trending_opportunity'
                ORDER BY p.created_at DESC, p.composite_score DESC NULLS LAST
                LIMIT ?
            """, [limit]).fetchall()
            conn.close()
            opportunities = [dict(r) for r in rows]
            if not opportunities:
                return {
                    "source": "db",
                    "count": 0,
                    "note": "No trending opportunity predictions found in the database yet. "
                            "The pipeline discovers trending tickers during intraday and morning "
                            "batch runs. Check back after the next scheduled run.",
                }
        return {
            "source": "db",
            "count": len(opportunities),
            "as_of": opportunities[0]["created_at"][:10] if opportunities else None,
            "opportunities": opportunities,
        }
    except Exception as exc:
        return {"note": f"DB query failed: {exc}"}


def _chat_tool_get_portfolio_summary() -> dict:
    """Query DB for current portfolio holdings + latest APEX rec per ticker."""
    import sqlite3
    db_path = _ROOT / "data" / "portfolio.db"
    if not db_path.exists():
        return {"note": "Database not found. Run the pipeline first."}
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        holdings = [dict(r) for r in conn.execute("""
            SELECT ticker,
                   SUM(shares) shares,
                   SUM(COALESCE(current_value,0)) current_value,
                   SUM(COALESCE(cost_basis_total, shares*avg_cost, 0)) cost_basis
            FROM holdings GROUP BY ticker ORDER BY current_value DESC
        """).fetchall()]
        tv = sum(h["current_value"] for h in holdings)
        tc = sum(h["cost_basis"] for h in holdings)
        for h in holdings:
            h["weight_pct"] = round(h["current_value"] / tv * 100, 1) if tv else 0
            h["unrealized_pct"] = (
                round((h["current_value"] - h["cost_basis"]) / h["cost_basis"] * 100, 1)
                if h["cost_basis"] else None
            )
        # Latest recommendation per holding
        ht = [h["ticker"] for h in holdings]
        if ht:
            ph = ",".join("?" * len(ht))
            preds = {r[0]: {"recommendation": r[1], "confidence": r[2], "composite_score": r[3]}
                     for r in conn.execute(f"""
                SELECT p.ticker, p.recommendation, p.confidence, p.composite_score
                FROM predictions p
                JOIN (SELECT ticker, MAX(created_at) dt FROM predictions
                      WHERE ticker IN ({ph}) GROUP BY ticker) x
                  ON p.ticker=x.ticker AND p.created_at=x.dt
            """, ht).fetchall()}
            for h in holdings:
                h["apex"] = preds.get(h["ticker"])
        conn.close()
        return {
            "source": "db",
            "total_value": round(tv, 2),
            "total_cost": round(tc, 2),
            "unrealized_pct": round((tv - tc) / tc * 100, 1) if tc else None,
            "holdings": holdings,
        }
    except Exception as exc:
        return {"note": f"DB query failed: {exc}"}


def _chat_tool_get_predictions_summary(segment: str = "all",
                                        recommendation: str | None = None) -> dict:
    """Query DB for latest APEX prediction per ticker."""
    import sqlite3
    db_path = _ROOT / "data" / "portfolio.db"
    if not db_path.exists():
        return {"note": "Database not found."}
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        # Build segment filter
        seg_clause = ""
        seg_params: list = []
        if segment == "portfolio":
            # Load portfolio tickers from yaml
            try:
                import yaml as _yaml
                pdata = _yaml.safe_load((_ROOT / "config" / "portfolio.yaml").read_text()) or {}
                ptickers = [h["ticker"].upper() for h in pdata.get("holdings", []) if "ticker" in h]
                if ptickers:
                    ph = ",".join("?" * len(ptickers))
                    seg_clause = f"AND p.ticker IN ({ph})"
                    seg_params = ptickers
            except Exception:
                pass
        elif segment == "opportunities":
            seg_clause = "AND p.trigger_type='trending_opportunity'"

        rec_clause = ""
        rec_params: list = []
        if recommendation:
            rec_clause = "AND p.recommendation=?"
            rec_params = [recommendation.upper()]

        rows = conn.execute(f"""
            SELECT p.ticker, p.recommendation, p.confidence, p.composite_score,
                   p.fundamental_score, p.research_score, p.news_score,
                   p.trigger_type, p.created_at
            FROM predictions p
            JOIN (SELECT ticker, MAX(created_at) dt FROM predictions GROUP BY ticker) x
              ON p.ticker=x.ticker AND p.created_at=x.dt
            WHERE 1=1 {seg_clause} {rec_clause}
            ORDER BY p.composite_score DESC NULLS LAST
            LIMIT 30
        """, seg_params + rec_params).fetchall()
        conn.close()
        preds = [dict(r) for r in rows]
        return {
            "source": "db",
            "segment": segment,
            "count": len(preds),
            "predictions": preds,
        }
    except Exception as exc:
        return {"note": f"DB query failed: {exc}"}


def _execute_chat_tool(name: str, args: dict) -> str:
    try:
        if name == "web_search":
            result = _chat_tool_web_search(args.get("query", ""), args.get("max_results", 5))
        elif name == "search_ticker":
            result = _chat_tool_search_ticker(args.get("company_name", ""))
        elif name == "get_ticker_news":
            result = _chat_tool_get_news(args.get("ticker", ""))
        elif name == "get_market_news":
            result = _chat_tool_get_market_news(args.get("limit", 20))
        elif name == "get_ipo_info":
            result = _chat_tool_get_ipo_info(args.get("ticker", ""))
        elif name == "get_sector_performance":
            result = _chat_tool_get_sector_performance()
        elif name == "get_fundamentals":
            result = _chat_tool_get_fundamentals(args.get("ticker", ""))
        elif name == "get_research":
            result = _chat_tool_get_research(args.get("ticker", ""))
        elif name == "get_macro_snapshot":
            result = _chat_tool_get_macro()
        elif name == "get_prediction_history":
            result = _chat_tool_get_predictions(args.get("ticker", ""))
        elif name == "get_price_history":
            result = _chat_tool_get_price_history(
                args.get("ticker", ""), args.get("period", "3mo")
            )
        elif name == "get_latest_opportunities":
            result = _chat_tool_get_latest_opportunities(
                min_confidence=args.get("min_confidence", 6),
                limit=args.get("limit", 10),
            )
        elif name == "get_portfolio_summary":
            result = _chat_tool_get_portfolio_summary()
        elif name == "get_predictions_summary":
            result = _chat_tool_get_predictions_summary(
                segment=args.get("segment", "all"),
                recommendation=args.get("recommendation"),
            )
        else:
            result = {"error": f"Unknown tool: {name}"}
        return json.dumps(result, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def _build_chat_history(messages: list[dict], limit: int = 12) -> list[dict]:
    """Convert session messages to LLM-compatible history (last N exchanges)."""
    result = []
    for msg in messages[-limit:]:
        if msg["role"] == "user":
            result.append({"role": "user", "content": msg["content"]})
        elif msg.get("type") == "prediction":
            meta = msg.get("metadata", {})
            summary = (
                f"[APEX Prediction: {meta.get('recommendation','?')} on {meta.get('ticker','?')}, "
                f"composite {meta.get('composite_score','?')}/10, "
                f"confidence {meta.get('confidence','?')}/10] "
                f"{(msg.get('content') or '')[:200]}"
            )
            result.append({"role": "assistant", "content": summary})
        else:
            content = (msg.get("content") or "")[:600]
            if content:
                result.append({"role": "assistant", "content": content})
    return result


_CHAT_SYSTEM = """\
You are APEX Chat, an AI financial assistant with access to a local investment database \
(holdings, predictions, opportunities) plus live market data, news, and web search.

You can answer questions about:
- **Individual stocks** -- fundamentals, broker research, price history, news, APEX predictions
- **Portfolio** -- current holdings, weights, P&L, APEX recommendations
- **New opportunities** -- trending stocks discovered and analyzed by the pipeline
- **Today's predictions** -- latest APEX signals across all tickers
- **Market news** -- latest financial headlines from Reuters, CNBC, Yahoo Finance, MarketWatch
- **Economy & macro** -- VIX, interest rates, yield curve, Fed policy, inflation, GDP trends
- **IPOs & listings** -- recent IPOs, company debut details, upcoming listings
- **Sectors & indices** -- sector ETF performance, S&P 500, Nasdaq, Dow Jones, Russell 2000
- **General finance** -- bonds, currencies, commodities, crypto market trends, regulatory issues

## Tool Priority (CRITICAL -- follow this exact order)

### 1. LOCAL DATABASE FIRST (always try these before anything else)
These query the local portfolio database and return real pipeline results:

| Question type | Tool to call FIRST |
|---|---|
| "latest opportunities", "trending stocks", "what looks good", "new discoveries", "top picks" | `get_latest_opportunities` |
| "my portfolio", "what do I own", "holdings", "portfolio performance" | `get_portfolio_summary` |
| "today's predictions", "latest signals", "what does APEX say", "all recommendations" | `get_predictions_summary` |
| Specific ticker prediction history | `get_prediction_history` |
| Specific ticker fundamentals | `get_fundamentals` |
| Specific ticker broker research | `get_research` |
| Specific ticker news | `get_ticker_news` |
| Specific ticker price | `get_price_history` |

### 2. LIVE MARKET DATA (when DB doesn't have what's needed)
- Market headlines → `get_market_news`
- Macro snapshot → `get_macro_snapshot`
- Sector performance → `get_sector_performance`
- IPO info → `get_ipo_info`
- Unknown company → `search_ticker`

### 3. WEB SEARCH (last resort only)
Use `web_search` ONLY when:
- DB and live tools returned empty / "unavailable" / clearly stale data
- Question is about a very recent event (last 1-2 days) not yet in the DB
- Question asks for something no internal tool covers (e.g. regulatory filing details, \
  specific earnings call quotes from today)

**Never fabricate**: If both DB and web search return nothing useful, say so clearly.

## Response discipline (IMPORTANT)
- You have **at most 6 tool calls** per response. Budget them wisely.
- For opportunity/portfolio/prediction questions: call the DB tool, then answer immediately.
- For stock analysis: call 2–3 tools max then answer -- do not over-chain.
- Do not call the same tool twice for the same ticker in one response.
- If `get_latest_opportunities` returns data, present it directly -- do not also call web search.

## Presenting opportunities
When showing results from `get_latest_opportunities` or `get_predictions_summary`:
- Lead with the ticker, recommendation, and composite score
- Include news/research/fundamental sub-scores when available
- Add a 1-sentence reasoning summary if the DB returned one
- Suggest "Type **Analyze [TICKER]** for the full 4-analyst APEX panel" for any ticker of interest

## Format rules
- Use **bold** for key figures, bullet lists for multi-point summaries.
- Cite your source: "(from local DB)" or "(web search)" or "(yfinance)".
- For full stock investment analysis, suggest: "Type **Analyze [TICKER]** for the full \
  4-analyst APEX panel."
"""


def _classify_intent(query: str, tickers: list[str]) -> str:
    """
    Classify the query as 'predict' (full APEX panel) or 'chat' (conversational).

    'predict' triggers when the user clearly wants an investment recommendation.
    Everything else routes to the conversational tool-calling agent.
    """
    q = query.lower()
    predict_phrases = {
        "analyze", "analysis", "full analysis", "predict", "prediction",
        "should i buy", "should i sell", "buy or sell", "worth buying",
        "investment thesis", "investment recommendation",
        "full report", "deep dive", "apex analysis",
        "give me a report", "run apex", "run analysis",
    }
    if any(ph in q for ph in predict_phrases):
        return "predict"
    # Short one-word queries with a ticker default to prediction
    if tickers and len(query.split()) <= 3:
        return "predict"
    return "chat"


def _run_chat_agent_thread(
    tickers: list[str], query: str, history: list[dict], q: Queue
) -> None:
    """
    LLM-orchestrated conversational agent with tool-calling.
    Runs in a daemon thread; pushes events to *q* like the APEX thread.
    """
    async def _inner():
        from dotenv import load_dotenv
        load_dotenv(_ROOT / ".env")
        import litellm
        from portfolio_agent._models import FAILOVER_CHAINS, _is_failover_error

        # Use flash chain (cheap + capable for conversational tasks)
        chat_chain = FAILOVER_CHAINS.get("flash", [])

        # Prepend ticker context to query if tickers are present
        ticker_hint = (
            f"[Tickers in question: {', '.join(tickers)}]\n\n" if tickers else ""
        )
        user_content = ticker_hint + query

        messages = [{"role": "system", "content": _CHAT_SYSTEM}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_content})

        for model_id, provider, label in chat_chain:
            q.put(("info", f"💬 {label} ({provider})"))
            try:
                for _round in range(8):   # max 8 tool-call rounds
                    resp = await litellm.acompletion(
                        model=model_id,
                        messages=messages,
                        tools=_CHAT_TOOLS,
                        tool_choice="auto",
                        temperature=0.2,
                        max_tokens=2500,
                    )
                    msg = resp.choices[0].message

                    if msg.tool_calls:
                        # Execute each tool call
                        tool_results = []
                        for tc in msg.tool_calls:
                            fn_name = tc.function.name
                            fn_args = json.loads(tc.function.arguments or "{}")
                            q.put(("tool", f"{fn_name}({json.dumps(fn_args)})"))
                            result_str = _execute_chat_tool(fn_name, fn_args)
                            q.put(("result", f"{fn_name} → {result_str[:250]}"))
                            tool_results.append({
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": result_str,
                            })
                        # Add assistant message with tool_calls, then results
                        messages.append({
                            "role": "assistant",
                            "content": msg.content or "",
                            "tool_calls": [
                                {
                                    "id": tc.id,
                                    "type": "function",
                                    "function": {
                                        "name": tc.function.name,
                                        "arguments": tc.function.arguments,
                                    },
                                }
                                for tc in msg.tool_calls
                            ],
                        })
                        messages.extend(tool_results)
                    else:
                        # Final answer -- stream it
                        text = msg.content or "_No response generated._"
                        q.put(("text", text))
                        q.put(("model_used", (label, provider)))
                        q.put(("done", "chat"))
                        return

                # Exceeded max rounds without final answer
                q.put(("text", "_Agent did not produce a final answer within tool limits._"))
                q.put(("done", "chat"))
                return

            except Exception as exc:
                if _is_failover_error(exc) and (model_id, provider, label) != chat_chain[-1]:
                    q.put(("warn", f"⚠ {label} failed -- trying next model…"))
                    continue
                q.put(("error", str(exc)))
                q.put(("done", ""))
                return

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_inner())
    finally:
        loop.close()


# ── APEX streaming runner ─────────────────────────────────────────────────────

_FAILOVER_ERRORS = (
    "429", "rate limit", "quota", "resource_exhausted",
    "credit", "billing", "529", "overloaded",
    "not_found", "is not found for api",
    "tool_use_failed", "failed_generation", "bad_request",
    "invalid_request_error",
)

def _is_apex_transient(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in _FAILOVER_ERRORS)


async def _apex_adk_run(ticker: str, query: str, model_id: str, label: str,
                        provider: str, chain_pos: str, q: Queue) -> bool:
    """Run APEX via ADK Runner (Claude / Gemini). Returns True on success."""
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types
    from portfolio_agent._models import _lite
    from portfolio_agent.specialists.reasoning import make_reasoning_agent

    agent  = make_reasoning_agent(model=_lite(model_id))
    svc    = InMemorySessionService()
    runner = Runner(agent=agent, app_name="apex_chat", session_service=svc)
    sess   = await svc.create_session(
        app_name="apex_chat", user_id="chat_user",
        state={"current_ticker": ticker.upper(), "user_query": query},
    )
    msg = types.Content(role="user",
                        parts=[types.Part(text=f"Analyze {ticker.upper()}: {query}")])

    q.put(("info", f"🤖 Reasoning with {label} ({provider})  [{chain_pos}]"))
    async for ev in runner.run_async(user_id="chat_user",
                                     session_id=sess.id, new_message=msg):
        if not (hasattr(ev, "content") and ev.content):
            continue
        for part in (ev.content.parts or []):
            if hasattr(part, "text") and part.text:
                q.put(("text", part.text))
            elif hasattr(part, "function_call") and part.function_call:
                fc = part.function_call
                q.put(("tool", f"{fc.name}({json.dumps(fc.args) if fc.args else ''})"))
            elif hasattr(part, "function_response") and part.function_response:
                fr = part.function_response
                q.put(("result", f"{fr.name} → {str(fr.response)[:300]}"))

    final = await svc.get_session(app_name="apex_chat", user_id="chat_user",
                                   session_id=sess.id)
    state = dict(final.state) if final else {}
    q.put(("model_used", (label, provider)))
    q.put(("done", state.get("apex_prediction", "")))
    return True


async def _apex_direct_run(ticker: str, query: str, model_id: str, label: str,
                           provider: str, chain_pos: str, q: Queue) -> bool:
    """
    Groq / no-tools fallback: pre-fetch context in Python, embed in prompt,
    call model directly via LiteLLM without ADK tool use.
    Returns True on success.
    """
    import litellm
    from portfolio_agent.tools.reasoning_tools import get_full_analysis_context

    q.put(("info", f"🤖 Reasoning with {label} ({provider}) [no-tools]  [{chain_pos}]"))
    q.put(("tool", "get_full_analysis_context -- pre-fetching context…"))

    ctx_json = get_full_analysis_context(ticker.upper())
    q.put(("result", f"get_full_analysis_context → {str(ctx_json)[:200]}…"))

    # Extract score caps from pre-loaded context to inject into the prompt
    try:
        ctx_obj = json.loads(ctx_json)
        _caps = ctx_obj.get("dynamic_weights", {}).get("data_caps", {})
        _gaps = ctx_obj.get("data_gaps", [])
    except Exception:
        _caps, _gaps = {}, []

    _cap_lines = []
    for _domain, _cap in _caps.items():
        if _cap < 10:
            _cap_lines.append(f"  • {_domain} score ≤ {_cap} -- no data in DB")
    _cap_block = (
        "\nSCORE CAPS (mandatory -- exceeding these is an error):\n" + "\n".join(_cap_lines) + "\n"
        if _cap_lines else ""
    )

    from portfolio_agent.tools.prediction_db import get_scheduled_horizons as _get_horizons
    _today_date = date.today()
    _sched_horizons = _get_horizons(_today_date) or [5]
    _hz_guidance = {
        5:  "5d  (~1 week)  -- focus on news flow, momentum, short-term catalysts",
        21: "21d (~1 month) -- focus on earnings drift, monthly themes, near catalysts",
        63: "63d (~1 quarter) -- focus on fundamentals, valuation, full earnings cycle",
    }
    _hz_lines = "\n".join(f"  • {_hz_guidance.get(h, f'{h}d')}" for h in sorted(_sched_horizons))

    # Build example horizons JSON for the prompt
    _hz_example_items = []
    for _h in sorted(_sched_horizons):
        _hz_example_items.append(
            f'    {{"horizon_days": {_h}, "predicted_direction": "UP|DOWN|FLAT", '
            f'"predicted_return_low": 1.5, "predicted_return_high": 4.0, '
            f'"conviction_score": 7, "reasoning_text": "..."}}'
        )
    _hz_example = "[\n" + ",\n".join(_hz_example_items) + "\n  ]"

    _caps_f = _caps.get("fundamentals", 10)
    _caps_r = _caps.get("research", 10)
    _caps_m = _caps.get("macro", 10)
    _caps_n = _caps.get("news", 10)

    prompt = f"""You are APEX (Adaptive Portfolio EXpert), a multi-perspective investment reasoning system.

Ticker  : {ticker.upper()}
Question: {query}

=== FULL ANALYSIS CONTEXT (pre-loaded) ===
{ctx_json}
=== END CONTEXT ===
{_cap_block}
Using the context above (fundamentals, research, news, macro, dynamic_weights, prediction_history):

1. State the weight regime and dynamic weights (from dynamic_weights in context).
2. Have each analyst score their domain -- respect any SCORE CAPS above: if the domain has no data the analyst must state "No <domain> data available" and assign a score ≤ that cap.
   FUNDAMENTAL ANALYST, RESEARCH ANALYST, MACRO ANALYST, NEWS ANALYST.
3. Cross-examine if scores diverge > 3 pts.
4. Compare to prior prediction if one exists.
5. Compute composite = fund_w×fund_score + res_w×res_score + mac_w×mac_score + news_w×news_score.
6. Map composite to STRONG_BUY/BUY/HOLD/SELL/STRONG_SELL.
7. For EACH scheduled horizon below, provide direction, return range, and per-horizon conviction:
{_hz_lines}

End your response with EXACTLY this JSON block (no text after):

```json
{{
  "ticker": "{ticker.upper()}",
  "prediction": "BULLISH|BEARISH|NEUTRAL",
  "recommendation": "STRONG_BUY|BUY|HOLD|SELL|STRONG_SELL",
  "confidence": 7,
  "target_price": null,
  "fundamental_score": 7,
  "research_score": 7,
  "macro_score": 6,
  "news_score": 6,
  "composite_score": 6.5,
  "score_caps_applied": {{"fundamentals": {_caps_f}, "research": {_caps_r}, "macro": {_caps_m}, "news": {_caps_n}}},
  "weight_regime": "QUIET_DAY",
  "weights_used": {{"fundamentals": 0.35, "research": 0.30, "macro": 0.20, "news": 0.15}},
  "panel_summary": {{
    "chen_verdict": "BULLISH (7/10) -- Fundamental Analyst one sentence",
    "webb_verdict": "BULLISH (7/10) -- Research Analyst one sentence",
    "varga_verdict": "NEUTRAL (6/10) -- Macro Analyst one sentence",
    "park_verdict":  "NEUTRAL (6/10) -- News Analyst one sentence",
    "key_debate": "Panel consensus or main disagreement"
  }},
  "weight_rationale": {{
    "fundamentals": "why this weight",
    "research": "why this weight",
    "macro": "why this weight",
    "news": "why this weight"
  }},
  "reasoning": "3-5 sentence narrative",
  "changed_from_previous": false,
  "previous_recommendation": null,
  "horizons": {_hz_example}
}}
```"""

    response = await litellm.acompletion(
        model=model_id,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        max_tokens=4096,
    )
    text = response.choices[0].message.content or ""
    q.put(("text", text))
    q.put(("model_used", (label, provider)))
    q.put(("done", text))
    return True


def _run_apex_thread(ticker: str, query: str, q: Queue) -> None:
    async def _inner():
        from dotenv import load_dotenv
        load_dotenv(_ROOT / ".env")
        from portfolio_agent._models import FAILOVER_CHAINS

        reasoning_chain = FAILOVER_CHAINS.get("reasoning", [])

        for i, (model_id, provider, label) in enumerate(reasoning_chain):
            chain_pos = f"{i+1}/{len(reasoning_chain)}"
            # Groq models don't support ADK tool calling -- use direct LiteLLM path
            use_direct = (provider == "groq")
            try:
                if use_direct:
                    await _apex_direct_run(ticker, query, model_id, label,
                                           provider, chain_pos, q)
                else:
                    await _apex_adk_run(ticker, query, model_id, label,
                                        provider, chain_pos, q)
                return  # success

            except Exception as exc:
                if _is_apex_transient(exc) and i < len(reasoning_chain) - 1:
                    next_label = reasoning_chain[i + 1][2]
                    q.put(("warn", f"⚠ {label} failed -- falling back to {next_label}…"))
                    continue
                q.put(("error", str(exc)))
                q.put(("done", ""))
                return

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_inner())
    finally:
        loop.close()


def _extract_json(text: str) -> dict:
    for pattern in [
        r'```json\s*(\{.*?\})\s*```',
        r'```\s*(\{.*?\})\s*```',
        r'(\{[^{}]*"recommendation"[^{}]*\})',
    ]:
        m = re.search(pattern, text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
    return {}


# ── Session state defaults ────────────────────────────────────────────────────

def _search_ticker_by_name(text: str) -> tuple[str, str] | tuple[None, None]:
    """
    Try to resolve a free-text company name to a ticker via yfinance Search.
    Returns (ticker, display_name) or (None, None) if nothing found.
    Only matches EQUITY quotes on major exchanges.
    """
    try:
        import yfinance as yf
        results = yf.Search(text, max_results=5).quotes
        for r in results:
            if r.get("quoteType") == "EQUITY" and r.get("symbol"):
                return r["symbol"], r.get("longname") or r.get("shortname") or r["symbol"]
    except Exception:
        pass
    return None, None


def _init_state():
    defaults = {
        "session_id":            None,
        "messages":              [],
        "current_tickers":       [],
        "awaiting_ticker":       False,
        "pending_query":         "",
        "session_tickers":       [],
        "last_rec":              "",
        "chat_no_ticker":        False,
        "ticker_confirm":        None,  # {"ticker": str, "name": str, "query": str, "intent": str}
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

_init_state()


def _new_chat():
    from portfolio_agent.tools.chat_db import new_session
    sid = new_session("New conversation")
    st.session_state.session_id      = sid
    st.session_state.messages        = []
    st.session_state.current_tickers = []
    st.session_state.awaiting_ticker = False
    st.session_state.pending_query   = ""
    st.session_state.session_tickers = []
    st.session_state.last_rec        = ""
    st.session_state.chat_no_ticker  = False


def _load_chat(session_id: str):
    from portfolio_agent.tools.chat_db import get_messages
    msgs = get_messages(session_id)
    st.session_state.session_id = session_id
    st.session_state.messages   = [
        {"role": m["role"], "content": m["content"],
         "type": m["msg_type"], "metadata": m["metadata"]}
        for m in msgs
    ]
    st.session_state.current_tickers = []
    st.session_state.awaiting_ticker = False


# ── Sidebar: chat history ─────────────────────────────────────────────────────

with st.sidebar:
    st.markdown(
        '<div style="padding:10px 0 16px">'
        '<h2 style="margin:0;font-size:1rem;font-weight:700;color:#F9FAFB;'
        'letter-spacing:-0.02em">🤖 APEX Chat</h2>'
        '<p style="margin:4px 0 0;font-size:0.72rem;color:#4B5563;font-weight:500">'
        'Financial AI · stocks · markets · news</p>'
        '</div>',
        unsafe_allow_html=True,
    )

    if st.button("✏️  New conversation", type="primary", use_container_width=True):
        _new_chat()
        st.rerun()

    st.divider()

    # Session history
    from portfolio_agent.tools.chat_db import list_sessions
    sessions = list_sessions(limit=80)
    groups   = _group_sessions_by_date(sessions)

    from portfolio_agent.tools.chat_db import delete_session

    for group_name, group_sessions in groups.items():
        if not group_sessions:
            continue
        st.markdown(f'<div class="date-group">{group_name}</div>', unsafe_allow_html=True)
        for s in group_sessions:
            is_active = s["id"] == st.session_state.get("session_id")
            tickers   = s.get("tickers", [])
            last_rec  = s.get("last_rec", "")
            rec_chip  = f' · {last_rec.replace("_"," ")}' if last_rec else ""

            # Prefer the first user question over the auto-title
            raw_label = s.get("first_question") or s.get("title") or "New conversation"
            label = raw_label[:38] + ("…" if len(raw_label) > 38 else "")
            meta  = ", ".join(tickers[:3]) + rec_chip if (tickers or last_rec) else \
                    s.get("updated_at","")[:16].replace("T"," ")

            btn_col, del_col = st.columns([5, 1], gap="small")
            with btn_col:
                if st.button(
                    f"{'▸ ' if is_active else ''}{label}\n{meta}",
                    key=f"sess_{s['id']}",
                    use_container_width=True,
                    help=raw_label,
                ):
                    _load_chat(s["id"])
                    st.rerun()
            with del_col:
                if st.button("🗑", key=f"del_{s['id']}", help="Delete this conversation"):
                    delete_session(s["id"])
                    if st.session_state.get("session_id") == s["id"]:
                        _new_chat()
                    st.rerun()

    if not sessions:
        st.markdown(
            '<p style="font-size:0.8rem;color:#475569;padding:8px 4px">'
            'No conversations yet. Start by typing a question.</p>',
            unsafe_allow_html=True,
        )


# ── Main area ─────────────────────────────────────────────────────────────────

# Hero (shown only when no session)
if not st.session_state.session_id and not st.session_state.messages:
    _hero_html = (
        '<div style="text-align:center;padding:52px 20px 16px">'
        '<div style="display:inline-flex;align-items:center;justify-content:center;'
        'width:64px;height:64px;background:#EFF6FF;border-radius:18px;'
        'font-size:2rem;margin-bottom:16px">🤖</div>'
        '<h1 style="font-size:2rem;font-weight:800;color:#111827;margin:0;'
        'letter-spacing:-0.04em">APEX Chat</h1>'
        '<p style="font-size:0.95rem;color:#6B7280;margin:8px 0 0;font-weight:400;'
        'line-height:1.5">Your AI financial assistant -- stocks, markets, economy &amp; more'
        '</p></div>'
        '<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;'
        'max-width:760px;margin:28px auto 20px">'
        '<div style="background:#FFFFFF;border:1px solid #E5E7EB;border-radius:14px;'
        'padding:20px 22px;border-top:3px solid #2563EB;'
        'box-shadow:0 1px 3px rgba(0,0,0,0.04)">'
        '<div style="font-size:1.15rem;margin-bottom:8px">📊</div>'
        '<div style="font-size:0.88rem;font-weight:700;color:#111827;margin-bottom:6px">'
        'Stock Analysis</div>'
        '<div style="font-size:0.79rem;color:#6B7280;line-height:1.65">'
        'Full 4-analyst APEX panel -- Fundamentals 40% · Research 30% · Macro 20% · News 10%. '
        'Dynamic weights on earnings days &amp; Fed meetings.'
        '</div>'
        '<div style="margin-top:10px;font-size:0.74rem;color:#2563EB;font-weight:500">'
        '"Analyze NVDA" · "Should I buy AAPL?" · "Full report on MSFT"'
        '</div></div>'
        '<div style="background:#FFFFFF;border:1px solid #E5E7EB;border-radius:14px;'
        'padding:20px 22px;border-top:3px solid #059669;'
        'box-shadow:0 1px 3px rgba(0,0,0,0.04)">'
        '<div style="font-size:1.15rem;margin-bottom:8px">💬</div>'
        '<div style="font-size:0.88rem;font-weight:700;color:#111827;margin-bottom:6px">'
        'Financial Chat</div>'
        '<div style="font-size:0.79rem;color:#6B7280;line-height:1.65">'
        'Live-data Q&amp;A. Calls market news, macro snapshot, sector performance, '
        'IPO data &amp; broker research before answering.'
        '</div>'
        '<div style="margin-top:10px;font-size:0.74rem;color:#059669;font-weight:500">'
        '"What\'s happening in markets?" · "Latest IPOs" · "Fed rate outlook"'
        '</div></div>'
        '</div>'
        '<div style="max-width:760px;margin:0 auto 24px;'
        'display:flex;flex-wrap:wrap;gap:7px;justify-content:center">'
        '<span style="display:inline-flex;align-items:center;gap:5px;'
        'background:#F9FAFB;border-radius:999px;padding:5px 13px;'
        'font-size:0.75rem;font-weight:600;color:#6B7280;border:1px solid #E5E7EB">'
        '📰 Market News</span>'
        '<span style="display:inline-flex;align-items:center;gap:5px;'
        'background:#F9FAFB;border-radius:999px;padding:5px 13px;'
        'font-size:0.75rem;font-weight:600;color:#6B7280;border:1px solid #E5E7EB">'
        '🌐 Economy &amp; Macro</span>'
        '<span style="display:inline-flex;align-items:center;gap:5px;'
        'background:#F9FAFB;border-radius:999px;padding:5px 13px;'
        'font-size:0.75rem;font-weight:600;color:#6B7280;border:1px solid #E5E7EB">'
        '🚀 IPOs &amp; Listings</span>'
        '<span style="display:inline-flex;align-items:center;gap:5px;'
        'background:#F9FAFB;border-radius:999px;padding:5px 13px;'
        'font-size:0.75rem;font-weight:600;color:#6B7280;border:1px solid #E5E7EB">'
        '📈 Sectors &amp; Indices</span>'
        '<span style="display:inline-flex;align-items:center;gap:5px;'
        'background:#F9FAFB;border-radius:999px;padding:5px 13px;'
        'font-size:0.75rem;font-weight:600;color:#6B7280;border:1px solid #E5E7EB">'
        '🔍 Web Search</span>'
        '</div>'
    )
    st.markdown(_hero_html, unsafe_allow_html=True)

    # Suggested prompts
    st.markdown(
        '<p style="text-align:center;font-size:0.72rem;font-weight:700;'
        'text-transform:uppercase;letter-spacing:0.08em;color:#9CA3AF;margin:4px 0 12px">'
        'Try asking</p>',
        unsafe_allow_html=True,
    )
    _hero_prompts = [
        ("Analyze NVDA",                    "📊", True),
        ("What's happening in markets?",    "📰", False),
        ("Latest IPOs this week",           "🚀", False),
        ("Compare MSFT vs GOOGL",           "⚖️", True),
        ("Fed rate outlook",                "🌐", False),
        ("How is tech sector performing?",  "📈", False),
    ]
    _hp_cols = st.columns(3)
    for _hi, (_hp_prompt, _hp_icon, _hp_has_ticker) in enumerate(_hero_prompts):
        with _hp_cols[_hi % 3]:
            if st.button(f"{_hp_icon}  {_hp_prompt}", use_container_width=True, key=f"hero_p_{_hi}"):
                if not st.session_state.session_id:
                    _new_chat()
                st.session_state.messages.append({"role": "user", "content": _hp_prompt,
                                                   "type": "text", "metadata": {}})
                if _hp_has_ticker:
                    st.session_state.current_tickers = _extract_tickers(_hp_prompt)
                    st.session_state.chat_no_ticker  = False
                else:
                    st.session_state.current_tickers = []
                    st.session_state.chat_no_ticker  = True
                st.rerun()

# Replay messages
for msg in st.session_state.messages:
    if msg["role"] == "user":
        with st.chat_message("user"):
            st.markdown(msg["content"])
    else:
        with st.chat_message("assistant"):
            if msg.get("type") == "prediction" and msg.get("metadata"):
                _render_prediction_card(msg["metadata"], elapsed=msg.get("elapsed", 0),
                                        show_history=False)
            else:
                st.markdown(msg.get("content", ""))

# ── Ticker confirmation prompt ("Did you mean X?") ───────────────────────────

if st.session_state.get("ticker_confirm"):
    _tc = st.session_state.ticker_confirm
    with st.chat_message("assistant"):
        st.markdown(
            f"I found **{_tc['name']}** (`{_tc['ticker']}`) -- is that the company you meant?"
        )
        _c1, _c2, _c3 = st.columns([1, 1, 2])
        with _c1:
            if st.button(f"✅ Yes, analyze {_tc['ticker']}", type="primary", key="tc_yes"):
                st.session_state.current_tickers = [_tc["ticker"]]
                st.session_state.ticker_confirm  = None
                st.session_state.awaiting_ticker = False
                st.rerun()
        with _c2:
            if st.button("🔍 Different company", key="tc_no"):
                st.session_state.ticker_confirm  = None
                st.session_state.awaiting_ticker = True
                st.rerun()

# ── Ticker selection prompt (fallback when auto-resolve fails) ────────────────

elif st.session_state.awaiting_ticker:
    with st.chat_message("assistant"):
        st.markdown(
            "**Which stock would you like me to analyze?** "
            "You can type a ticker (e.g. NVDA) or a company name (e.g. Nvidia):"
        )
        wl = _load_watchlist()
        col_sel, col_text = st.columns([2, 1])
        with col_sel:
            selected = st.multiselect("Watchlist tickers", wl,
                                      label_visibility="collapsed", key="ticker_select")
        with col_text:
            manual = st.text_input("Ticker or company name",
                                   label_visibility="collapsed", key="ticker_text",
                                   placeholder="e.g. NVDA or Nvidia")

        if st.button("▶ Analyze", type="primary", key="confirm_tickers"):
            manual_list = [t.strip() for t in manual.split(",") if t.strip()]
            # Try to resolve any company names in the manual input
            resolved = []
            for item in manual_list:
                found = _extract_tickers(item)
                if found:
                    resolved.extend(found)
                else:
                    tk, _ = _search_ticker_by_name(item)
                    if tk:
                        resolved.append(tk)
                    else:
                        resolved.append(item.upper())
            chosen = list(dict.fromkeys(selected + resolved))
            if chosen:
                st.session_state.current_tickers = chosen
                st.session_state.awaiting_ticker  = False
                st.rerun()
            else:
                st.warning("Please select or enter at least one ticker or company name.")

# ── Chat input ────────────────────────────────────────────────────────────────

user_input = st.chat_input(
    "Ask anything financial -- 'Analyze NVDA', 'What's happening in markets?', 'Latest IPOs', 'Fed rate outlook'…"
)

if user_input:
    if not st.session_state.session_id:
        _new_chat()

    st.session_state.messages.append(
        {"role": "user", "content": user_input, "type": "text", "metadata": {}}
    )

    # Persist to DB
    from portfolio_agent.tools.chat_db import add_message, rename_session
    add_message(st.session_state.session_id, "user", user_input)

    # Auto-title from first user message
    if len(st.session_state.messages) == 1:
        title = user_input[:60]
        rename_session(st.session_state.session_id, title)

    tickers = _extract_tickers(user_input)
    if tickers:
        st.session_state.current_tickers = tickers
        st.session_state.chat_no_ticker  = False
        st.session_state.ticker_confirm  = None
    elif not st.session_state.current_tickers:
        _q_lower = user_input.lower()
        _predict_kw = {
            "analyze", "full analysis", "should i buy", "should i sell",
            "buy or sell", "worth buying", "investment thesis",
            "full report", "deep dive", "apex analysis", "run apex",
        }
        _wants_prediction = any(kw in _q_lower for kw in _predict_kw)

        if _wants_prediction:
            # Try to resolve a company name in the query before asking the user
            _candidate_tk, _candidate_name = _search_ticker_by_name(user_input)
            if _candidate_tk:
                # Found a match -- confirm with user instead of silently assuming
                st.session_state.ticker_confirm = {
                    "ticker": _candidate_tk,
                    "name":   _candidate_name,
                    "query":  user_input,
                    "intent": "predict",
                }
            else:
                st.session_state.awaiting_ticker = True
        else:
            # General financial question -- route to chat agent; it can call
            # search_ticker itself if it needs to resolve a company name.
            st.session_state.chat_no_ticker = True
            st.session_state.ticker_confirm = None

    st.rerun()

# ── Route: predict (full APEX) vs chat (conversational agent) ─────────────────

_has_ticker  = bool(st.session_state.current_tickers)
_no_ticker   = st.session_state.get("chat_no_ticker", False)
_has_message = (
    st.session_state.messages
    and st.session_state.messages[-1]["role"] == "user"
    and not st.session_state.awaiting_ticker
    and not st.session_state.get("ticker_confirm")
)

if _has_message and (_has_ticker or _no_ticker):
    tickers = st.session_state.current_tickers
    query   = st.session_state.messages[-1]["content"]
    intent  = _classify_intent(query, tickers)

    # ── Conversational chat agent (non-predict) ───────────────────────────────
    if intent == "chat" or _no_ticker:
        with st.chat_message("assistant"):
            q_chat: Queue = Queue()
            ct = Thread(
                target=_run_chat_agent_thread,
                args=(tickers, query, _build_chat_history(st.session_state.messages[:-1]), q_chat),
                daemon=True,
            )
            ct.start()
            start_chat = time.time()
            chat_text = ""
            used_label_chat = None

            # Tool log lives inside the collapsible status; answer is rendered below it
            with st.status("🔧 Fetching data…", expanded=True) as chat_st:
                tool_ph_c  = st.empty()
                tool_log_c: list[str] = []

                while True:
                    try:
                        ev_type, ev_val = q_chat.get(timeout=0.25)
                    except Empty:
                        if not ct.is_alive():
                            break
                        continue

                    if ev_type == "info":
                        tool_log_c.append(
                            f'<span style="color:#3B82F6;font-size:0.8rem">ℹ {ev_val}</span>'
                        )
                    elif ev_type in ("tool", "result", "warn"):
                        icon  = {"tool": "▶", "result": "◀", "warn": "⚠"}[ev_type]
                        color = {"tool": "#0F172A", "result": "#64748B", "warn": "#F59E0B"}[ev_type]
                        tool_log_c.append(
                            f'<span style="color:{color};font-size:0.78rem">'
                            f'{icon} {ev_val[:220]}</span>'
                        )
                    elif ev_type == "text":
                        chat_text += ev_val   # collected; rendered below after status closes
                    elif ev_type == "model_used":
                        used_label_chat = ev_val[0]
                    elif ev_type == "error":
                        chat_st.update(label=f"❌ {ev_val}", state="error")
                        break
                    elif ev_type == "done":
                        label_txt = f" ({used_label_chat})" if used_label_chat else ""
                        chat_st.update(
                            label=f"✅ Tools done{label_txt}", state="complete",
                            expanded=False,   # collapse after done -- answer shows below
                        )
                        break

                    if tool_log_c:
                        tool_ph_c.markdown(
                            '<div style="background:#F8FAFC;border:1px solid #E2E8F0;'
                            'border-radius:8px;padding:8px 12px;font-family:monospace">'
                            + "<br>".join(tool_log_c[-6:]) + "</div>",
                            unsafe_allow_html=True,
                        )

            # Answer rendered directly in the chat message, outside the status block
            if chat_text:
                st.markdown(chat_text)

            # Persist to chat DB
            final_text = chat_text or "_No response._"
            from portfolio_agent.tools.chat_db import add_message as _add_msg
            _add_msg(st.session_state.session_id, "assistant", final_text)
            st.session_state.messages.append({
                "role": "assistant", "type": "text",
                "content": final_text, "metadata": {},
            })

        st.session_state.current_tickers = []
        st.session_state.chat_no_ticker  = False

    # ── Full APEX prediction ──────────────────────────────────────────────────
    else:
      for ticker in tickers:
        ticker = ticker.upper()

        with st.chat_message("assistant"):

            # ── Plan card ─────────────────────────────────────────────────────
            plan = _build_plan(ticker)
            _render_plan_card(plan)

            # Fill gaps via live agents
            if plan["gaps"]:
                with st.status(
                    f"⚡ Running live agents for: {', '.join(plan['gaps'])}…",
                    expanded=True,
                ) as gap_st:
                    from portfolio_agent.tools.reasoning_tools import fill_data_gaps
                    from portfolio_agent.pipeline.failover import CreditExhaustedError
                    try:
                        _, agents_run = fill_data_gaps(ticker, {"data_gaps": plan["gaps"]})
                        for a in agents_run:
                            st.write(f"✅ {a.title()} agent completed")
                        gap_st.update(label="✅ Data gaps filled", state="complete")
                    except CreditExhaustedError as exc:
                        gap_st.update(label="⚠️ Could not fill all data gaps", state="error")
                        st.warning(
                            f"**All models exhausted** -- could not fetch live data for "
                            f"`{', '.join(plan['gaps'])}`. "
                            f"APEX will reason with whatever is already in the database.\n\n"
                            f"_{str(exc).split('Last error:')[0].strip()}_",
                            icon="⚠️",
                        )
                    except Exception as exc:
                        gap_st.update(label="⚠️ Data gap fill failed", state="error")
                        st.warning(f"Live agent error (continuing with cached data): {exc}", icon="⚠️")

            st.markdown(f"#### 🧠 Panel Deliberation -- `{ticker}`")

            # ── Stream execution ──────────────────────────────────────────────
            q: Queue = Queue()
            t = Thread(target=_run_apex_thread, args=(ticker, query, q), daemon=True)
            t.start()
            start = time.time()
            deliberation   = ""
            raw_output     = ""
            used_model_label    = None
            used_model_provider = None

            with st.status("🔄 Analysts deliberating…", expanded=True) as run_st:
                tool_ph = st.empty()
                text_ph = st.empty()
                tool_log: list[str] = []

                while True:
                    try:
                        ev_type, ev_val = q.get(timeout=0.25)
                    except Empty:
                        if not t.is_alive():
                            break
                        continue

                    if ev_type == "info":
                        tool_log.append(f'<span style="color:#3B82F6;font-size:0.8rem">ℹ {ev_val}</span>')
                        tool_ph.markdown(
                            f'<div style="background:#F8FAFC;border:1px solid #E2E8F0;'
                            f'border-radius:8px;padding:10px 14px;font-family:monospace">'
                            + "<br>".join(tool_log[-4:]) + "</div>",
                            unsafe_allow_html=True,
                        )

                    elif ev_type == "warn":
                        tool_log.append(f'<span style="color:#F59E0B;font-size:0.8rem">⚠ {ev_val}</span>')
                        tool_ph.markdown(
                            f'<div style="background:#F8FAFC;border:1px solid #E2E8F0;'
                            f'border-radius:8px;padding:10px 14px;font-family:monospace">'
                            + "<br>".join(tool_log[-4:]) + "</div>",
                            unsafe_allow_html=True,
                        )

                    elif ev_type == "tool":
                        tool_log.append(f'<code style="font-size:0.8rem">▶ {ev_val}</code>')
                        tool_ph.markdown(
                            f'<div style="background:#F8FAFC;border:1px solid #E2E8F0;'
                            f'border-radius:8px;padding:10px 14px;font-family:monospace">'
                            + "<br>".join(tool_log[-4:]) + "</div>",
                            unsafe_allow_html=True,
                        )

                    elif ev_type == "result":
                        tool_log.append(f'<span style="color:#64748B;font-size:0.78rem">◀ {ev_val[:200]}</span>')
                        tool_ph.markdown(
                            f'<div style="background:#F8FAFC;border:1px solid #E2E8F0;'
                            f'border-radius:8px;padding:10px 14px;font-family:monospace">'
                            + "<br>".join(tool_log[-4:]) + "</div>",
                            unsafe_allow_html=True,
                        )

                    elif ev_type == "text":
                        deliberation += ev_val
                        text_ph.markdown(deliberation)

                    elif ev_type == "model_used":
                        used_model_label, used_model_provider = ev_val

                    elif ev_type == "error":
                        run_st.update(label=f"❌ Error: {ev_val}", state="error")
                        st.error(ev_val)
                        break

                    elif ev_type == "done":
                        raw_output = ev_val
                        label_txt = f" ({used_model_label})" if used_model_label else ""
                        run_st.update(label=f"✅ Deliberation complete{label_txt}", state="complete")
                        break

            elapsed = time.time() - start

            # ── Parse prediction JSON ─────────────────────────────────────────
            pred_data = _extract_json(raw_output) or _extract_json(deliberation)
            if pred_data:
                pred_data["ticker"] = ticker

                # Pull analyst PT snapshot from stored research BEFORE rendering
                from portfolio_agent.tools.research_db import get_stored_research as _gsr
                _res = _gsr(ticker) or {}
                pred_data.setdefault("pt_mean",          _res.get("price_target_avg"))
                pred_data.setdefault("pt_median",        _res.get("price_target_median"))
                pred_data.setdefault("pt_high",          _res.get("price_target_high"))
                pred_data.setdefault("pt_low",           _res.get("price_target_low"))
                pred_data.setdefault("pt_num_analysts",  _res.get("num_analysts"))
                pred_data.setdefault("pt_current_price", _res.get("current_price"))

                st.divider()
                _render_prediction_card(pred_data, elapsed, show_history=True)

                # Save prediction to predictions DB -- one row per horizon
                from portfolio_agent.tools.prediction_db import (
                    insert_prediction, get_scheduled_horizons as _gs_h,
                    get_today_horizons as _gth,
                )
                from portfolio_agent.tools.yfinance_tools import get_close as _get_close
                _today_save = date.today()
                _sched_h = _gs_h(_today_save) or [5]
                _done_h  = _gth(ticker, _today_save.isoformat())
                _start_price = _get_close(ticker, _today_save.isoformat())
                _horizons_data = pred_data.get("horizons") or []
                _h_by_days = {h.get("horizon_days"): h for h in _horizons_data
                              if isinstance(h, dict) and h.get("horizon_days")}
                _common_save = dict(
                    ticker=ticker,
                    prediction=pred_data.get("prediction", "NEUTRAL"),
                    recommendation=pred_data.get("recommendation", "HOLD"),
                    confidence=pred_data.get("confidence"),
                    target_price=pred_data.get("target_price"),
                    fundamental_score=pred_data.get("fundamental_score"),
                    research_score=pred_data.get("research_score"),
                    macro_score=pred_data.get("macro_score"),
                    news_score=pred_data.get("news_score"),
                    composite_score=pred_data.get("composite_score"),
                    reasoning=pred_data.get("reasoning", ""),
                    panel_summary=pred_data.get("panel_summary", {}),
                    data_sources=pred_data.get("data_sources", []),
                    model_name=used_model_label,
                    model_provider=used_model_provider,
                    pt_mean=pred_data.get("pt_mean"),
                    pt_median=pred_data.get("pt_median"),
                    pt_high=pred_data.get("pt_high"),
                    pt_low=pred_data.get("pt_low"),
                    pt_num_analysts=pred_data.get("pt_num_analysts"),
                    pt_current_price=pred_data.get("pt_current_price"),
                )
                save_res = None
                _saved_h_count = 0
                for _h_days in _sched_h:
                    if _h_days in _done_h:
                        continue
                    _hd = _h_by_days.get(_h_days, {})
                    _dist = _hd.get("distribution") or {}
                    _r = insert_prediction(
                        **_common_save,
                        horizon_days=_h_days,
                        predicted_direction=_hd.get("predicted_direction"),
                        predicted_return_low=_hd.get("predicted_return_low"),
                        predicted_return_high=_hd.get("predicted_return_high"),
                        conviction_score=_hd.get("conviction_score"),
                        reasoning_text=_hd.get("reasoning_text") or pred_data.get("reasoning",""),
                        start_price=_start_price,
                        p_strong_down=_dist.get("strong_down"),
                        p_moderate_down=_dist.get("moderate_down"),
                        p_flat=_dist.get("flat"),
                        p_moderate_up=_dist.get("moderate_up"),
                        p_strong_up=_dist.get("strong_up"),
                    )
                    if save_res is None:
                        save_res = _r
                    if _r.get("saved"):
                        _saved_h_count += 1
                if save_res and save_res.get("saved"):
                    _hz_label = f"{_saved_h_count} horizon{'s' if _saved_h_count != 1 else ''}"
                    st.caption(
                        f"💾 Prediction saved ({_hz_label})"
                        + (f" · changed from **{save_res['previous']}**"
                           if save_res.get("changed") else "")
                    )

                # Update session metadata
                tickers_so_far = list(dict.fromkeys(
                    st.session_state.session_tickers + [ticker]
                ))
                st.session_state.session_tickers = tickers_so_far
                st.session_state.last_rec = pred_data.get("recommendation","")

                from portfolio_agent.tools.chat_db import touch_session
                touch_session(
                    st.session_state.session_id,
                    tickers=tickers_so_far,
                    last_rec=pred_data.get("recommendation",""),
                )

                # Save to chat DB
                from portfolio_agent.tools.chat_db import add_message
                add_message(
                    st.session_state.session_id,
                    "assistant",
                    pred_data.get("reasoning", "Prediction generated."),
                    msg_type="prediction",
                    metadata={**pred_data, "elapsed": elapsed},
                )
                st.session_state.messages.append({
                    "role": "assistant", "type": "prediction",
                    "content": pred_data.get("reasoning",""),
                    "metadata": {**pred_data, "elapsed": elapsed},
                })
            else:
                # Fallback: plain text
                st.markdown(deliberation or raw_output or "_No output received._")
                from portfolio_agent.tools.chat_db import add_message
                content = deliberation or raw_output or "Analysis completed."
                add_message(st.session_state.session_id, "assistant", content)
                st.session_state.messages.append({
                    "role": "assistant", "type": "text",
                    "content": content, "metadata": {},
                })

      st.session_state.current_tickers = []
