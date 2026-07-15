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
    ticker_label,
    REC_STYLES, SUCCESS, WARNING, DANGER, PRIMARY, NEUTRAL, NEUTRAL_LIGHT,
    SUCCESS_LIGHT, WARNING_LIGHT, DANGER_LIGHT, PRIMARY_LIGHT,
)
from web.chat.tools import _CHAT_TOOLS, _execute_chat_tool, _search_ticker_by_name
from web.chat.rendering import _build_plan, _render_plan_card, _render_prediction_card
from web.chat.orchestration import (
    _build_chat_history, _classify_intent,
    _run_chat_agent_thread, _run_apex_thread, _extract_json,
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


_COMMON_INDEX_TICKERS = {
    "SPY", "QQQ", "DIA", "VOO", "IWM", "VIX", "VTI", "IVV",
}


@st.cache_data(ttl=300)
def _known_tickers() -> frozenset[str]:
    """
    Universe of tickers this app actually knows about: the watchlist, portfolio
    holdings, the EDGAR-verified fundamentals table, the S&P1500/Nasdaq screener
    universe, plus a handful of common index/ETF symbols.

    Used to validate a bare regex match before treating it as a ticker --
    without this, any 1-5 letter English word not in the hand-maintained
    stopword list (e.g. "LOWER", "TREND", "GAINS") gets misread as a symbol.

    Deliberately does NOT read the `predictions` table: it's append-only and can
    already contain bogus "tickers" from a bad extraction (this exact bug) or a
    typo'd company name -- using it here would let a past false positive vouch
    for a future one.
    """
    known: set[str] = set(_COMMON_INDEX_TICKERS) | set(_load_watchlist())

    db_path = _ROOT / "data" / "portfolio.db"
    if db_path.exists():
        try:
            import sqlite3
            with sqlite3.connect(str(db_path)) as conn:
                rows = conn.execute("SELECT DISTINCT ticker FROM fundamentals").fetchall()
                known.update(r[0].upper() for r in rows if r[0])
        except Exception:
            pass

    try:
        from portfolio_agent.tools.universe_db import get_tickers
        known.update(t.upper() for t in get_tickers())
    except Exception:
        pass

    try:
        import yaml
        pdata = yaml.safe_load((_ROOT / "config" / "portfolio.yaml").read_text()) or {}
        known.update(
            h["ticker"].upper() for h in pdata.get("holdings", []) if h.get("ticker")
        )
    except Exception:
        pass

    return frozenset(known)


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

    # 2. Then match bare uppercase ticker-like tokens -- only accept ones we
    # actually recognize, so ordinary English words (e.g. "LOWER" in "trading
    # lower") can't get misread as ticker symbols.
    known = _known_tickers()
    for t in dict.fromkeys(_TICKER_RE.findall(upper)):
        if t not in _STOP and t not in found and t in known:
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


# ── Session state defaults ────────────────────────────────────────────────────


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

    # ── Attention Queue nudge -- same critical-tier logic as the dashboard ────
    from web.data.attention import get_critical_nudge_items
    _nudges = get_critical_nudge_items(limit=3)
    if _nudges:
        _n = len(_nudges)
        st.markdown(
            f'<div style="max-width:760px;margin:0 auto 10px;background:{DANGER_LIGHT};'
            f'border:1px solid {DANGER}33;border-radius:12px;padding:14px 18px 4px">'
            f'<div style="font-size:0.78rem;font-weight:700;color:{DANGER};'
            f'text-transform:uppercase;letter-spacing:0.05em;margin-bottom:2px">'
            f'⚠️ {_n} item{"s" if _n != 1 else ""} need your attention</div>'
            f'</div>',
            unsafe_allow_html=True,
        )
        _nudge_cols = st.columns(len(_nudges))
        for _ni, _nudge in enumerate(_nudges):
            with _nudge_cols[_ni]:
                if st.button(_nudge["title"], use_container_width=True, key=f"nudge_{_ni}"):
                    if not st.session_state.session_id:
                        _new_chat()
                    st.session_state.messages.append({
                        "role": "user", "content": _nudge["query"],
                        "type": "text", "metadata": {},
                    })
                    _nudge_tickers = _extract_tickers(_nudge["query"])
                    if not _nudge_tickers and _nudge.get("ticker"):
                        _nudge_tickers = [_nudge["ticker"]]
                    st.session_state.current_tickers = _nudge_tickers
                    st.session_state.chat_no_ticker  = not _nudge_tickers
                    st.rerun()
        st.markdown('<div style="margin-bottom:8px"></div>', unsafe_allow_html=True)

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

            st.markdown(f"#### 🧠 Panel Deliberation -- `{ticker_label(ticker)}`")

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
