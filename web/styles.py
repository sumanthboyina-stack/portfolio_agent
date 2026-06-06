"""
Shared design system for the Portfolio Agent web app.

Color palette: professional slate/blue — clean without being jazzy.
"""

from __future__ import annotations
import streamlit as st

# ── Color tokens ──────────────────────────────────────────────────────────────

PRIMARY        = "#2563EB"   # Blue-600
PRIMARY_LIGHT  = "#EFF6FF"   # Blue-50
PRIMARY_DARK   = "#1D4ED8"   # Blue-700
SUCCESS        = "#059669"   # Emerald-600
SUCCESS_LIGHT  = "#D1FAE5"   # Emerald-100
WARNING        = "#D97706"   # Amber-600
WARNING_LIGHT  = "#FEF3C7"   # Amber-100
DANGER         = "#DC2626"   # Red-600
DANGER_LIGHT   = "#FEE2E2"   # Red-100
PURPLE         = "#7C3AED"   # Violet-600
PURPLE_LIGHT   = "#EDE9FE"   # Violet-100
NEUTRAL        = "#64748B"   # Slate-500
NEUTRAL_LIGHT  = "#F1F5F9"   # Slate-100
PAGE_BG        = "#F8FAFC"   # Slate-50
CARD_BG        = "#FFFFFF"
BORDER         = "#E2E8F0"   # Slate-200
TEXT_PRIMARY   = "#0F172A"   # Slate-900
TEXT_SECONDARY = "#475569"   # Slate-600
SIDEBAR_BG     = "#0F172A"   # Slate-900

# Recommendation colors
REC_STYLES: dict[str, tuple[str, str, str]] = {
    "STRONG_BUY":  (SUCCESS,  SUCCESS_LIGHT,  "🟢 STRONG BUY"),
    "BUY":         ("#16A34A","#DCFCE7",      "🟢 BUY"),
    "HOLD":        (WARNING,  WARNING_LIGHT,  "🟡 HOLD"),
    "SELL":        (DANGER,   DANGER_LIGHT,   "🔴 SELL"),
    "STRONG_SELL": ("#B91C1C","#FCA5A5",      "🔴 STRONG SELL"),
}
PRED_COLORS: dict[str, tuple[str, str]] = {
    "BULLISH": (SUCCESS, SUCCESS_LIGHT),
    "NEUTRAL": (WARNING, WARNING_LIGHT),
    "BEARISH": (DANGER,  DANGER_LIGHT),
}


# ── Global CSS injection ───────────────────────────────────────────────────────

def inject_global_css() -> None:
    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

    /* ── Base ────────────────────────────────────────────── */
    html, body, [class*="css"] { font-family: 'Inter', -apple-system, sans-serif !important; }
    .stApp { background: #F8FAFC !important; }
    .block-container {
        padding-top: 0.5rem !important;
        padding-bottom: 2rem !important;
        max-width: 1320px !important;
    }
    [data-testid="stSidebarNav"] { display: none !important; }

    /* ── Top nav ─────────────────────────────────────────── */
    .top-nav-wrap {
        background: white;
        border-bottom: 1px solid #E2E8F0;
        margin: -0.5rem -1rem 1.5rem;
        padding: 0 1.25rem;
        display: flex;
        align-items: stretch;
        min-height: 52px;
        position: sticky;
        top: 0;
        z-index: 999;
        box-shadow: 0 1px 3px rgba(0,0,0,0.06);
    }
    .nav-brand {
        font-weight: 800;
        color: #0F172A;
        font-size: 0.95rem;
        display: flex;
        align-items: center;
        gap: 8px;
        padding-right: 28px;
        border-right: 1px solid #E2E8F0;
        margin-right: 8px;
        letter-spacing: -0.02em;
        white-space: nowrap;
    }
    .nav-tabs-row {
        display: flex;
        align-items: stretch;
        gap: 0;
        flex: 1;
    }
    .nav-tab {
        display: flex;
        align-items: center;
        gap: 5px;
        padding: 0 14px;
        font-size: 0.82rem;
        font-weight: 500;
        color: #64748B;
        border-bottom: 2px solid transparent;
        cursor: pointer;
        text-decoration: none;
        white-space: nowrap;
        transition: color 0.15s, border-color 0.15s;
    }
    .nav-tab:hover { color: #2563EB; border-bottom-color: #BFDBFE; }
    .nav-tab.active {
        color: #2563EB;
        border-bottom-color: #2563EB;
        font-weight: 600;
    }

    /* ── Sidebar ─────────────────────────────────────────── */
    [data-testid="stSidebar"] {
        background: #0F172A !important;
        border-right: 1px solid #1E293B !important;
    }
    [data-testid="stSidebar"] label,
    [data-testid="stSidebar"] .stMarkdown,
    [data-testid="stSidebar"] .stCaption,
    [data-testid="stSidebar"] p,
    [data-testid="stSidebar"] span { color: #CBD5E1 !important; }
    [data-testid="stSidebar"] h1,
    [data-testid="stSidebar"] h2,
    [data-testid="stSidebar"] h3 { color: #F1F5F9 !important; }
    [data-testid="stSidebar"] hr { border-color: #1E293B !important; }
    [data-testid="stSidebar"] .stButton > button {
        width: 100%;
        background: #1E293B !important;
        border: 1px solid #334155 !important;
        color: #CBD5E1 !important;
        border-radius: 8px !important;
        font-weight: 500 !important;
        text-align: left !important;
        padding: 8px 14px !important;
        font-size: 0.85rem !important;
    }
    [data-testid="stSidebar"] .stButton > button:hover {
        background: #2563EB !important;
        border-color: #2563EB !important;
        color: #FFFFFF !important;
    }
    [data-testid="stSidebar"] .stButton > button[kind="primary"] {
        background: #2563EB !important;
        border-color: #2563EB !important;
        color: white !important;
    }
    [data-testid="stSidebar"] .stSelectbox > div > div,
    [data-testid="stSidebar"] .stTextInput > div > div > input {
        background: #1E293B !important;
        border-color: #334155 !important;
        color: #F1F5F9 !important;
        border-radius: 8px !important;
    }

    /* ── Main buttons ────────────────────────────────────── */
    .stButton > button {
        border-radius: 8px !important;
        font-weight: 500 !important;
        font-size: 0.875rem !important;
        padding: 8px 18px !important;
        transition: all 0.15s ease !important;
        border: 1px solid #E2E8F0 !important;
        letter-spacing: -0.01em !important;
    }
    .stButton > button[kind="primary"] {
        background: #2563EB !important;
        border-color: #2563EB !important;
        color: white !important;
    }
    .stButton > button[kind="primary"]:hover {
        background: #1D4ED8 !important;
        border-color: #1D4ED8 !important;
        box-shadow: 0 4px 12px rgba(37,99,235,0.25) !important;
    }
    .stButton > button:hover {
        border-color: #2563EB !important;
        color: #2563EB !important;
    }

    /* ── Inputs ──────────────────────────────────────────── */
    .stTextInput > div > div > input,
    .stTextArea > div > div > textarea {
        border-radius: 8px !important;
        border: 1px solid #E2E8F0 !important;
        padding: 10px 14px !important;
        font-size: 0.9rem !important;
        transition: border-color 0.15s !important;
        font-family: 'Inter', sans-serif !important;
    }
    .stTextInput > div > div > input:focus,
    .stTextArea > div > div > textarea:focus {
        border-color: #2563EB !important;
        box-shadow: 0 0 0 3px rgba(37,99,235,0.1) !important;
    }
    .stSelectbox > div > div,
    .stMultiSelect > div > div {
        border-radius: 8px !important;
        border: 1px solid #E2E8F0 !important;
    }

    /* ── Streamlit inner tabs (section tabs, not nav) ─────── */
    .stTabs [data-baseweb="tab-list"] {
        gap: 2px;
        background: #F1F5F9;
        padding: 4px;
        border-radius: 10px;
        border: 1px solid #E2E8F0;
    }
    .stTabs [data-baseweb="tab"] {
        border-radius: 7px !important;
        padding: 6px 18px !important;
        font-weight: 500 !important;
        font-size: 0.82rem !important;
        color: #64748B !important;
    }
    .stTabs [aria-selected="true"] {
        background: white !important;
        color: #0F172A !important;
        box-shadow: 0 1px 3px rgba(0,0,0,0.08) !important;
    }

    /* ── Metrics ─────────────────────────────────────────── */
    [data-testid="stMetric"] {
        background: white;
        border: 1px solid #E2E8F0;
        border-radius: 12px;
        padding: 16px 20px !important;
        box-shadow: 0 1px 3px rgba(0,0,0,0.05);
        transition: box-shadow 0.15s;
    }
    [data-testid="stMetric"]:hover { box-shadow: 0 4px 12px rgba(0,0,0,0.08); }
    [data-testid="metric-container"] > div:first-child {
        font-size: 0.72rem !important;
        font-weight: 700 !important;
        text-transform: uppercase !important;
        letter-spacing: 0.07em !important;
        color: #94A3B8 !important;
    }
    [data-testid="metric-container"] [data-testid="stMetricValue"] {
        font-size: 1.75rem !important;
        font-weight: 800 !important;
        color: #0F172A !important;
        letter-spacing: -0.03em !important;
    }

    /* ── DataFrames ──────────────────────────────────────── */
    .stDataFrame {
        border-radius: 10px !important;
        border: 1px solid #E2E8F0 !important;
        overflow: hidden !important;
        box-shadow: 0 1px 3px rgba(0,0,0,0.04) !important;
    }
    .stDataFrame thead tr th {
        background: #F8FAFC !important;
        font-size: 0.75rem !important;
        font-weight: 700 !important;
        text-transform: uppercase !important;
        letter-spacing: 0.05em !important;
        color: #64748B !important;
    }
    .stDataFrame table { font-size: 0.86rem !important; }

    /* ── Expander ────────────────────────────────────────── */
    .streamlit-expanderHeader {
        border-radius: 8px !important;
        font-weight: 600 !important;
        background: #F8FAFC !important;
        font-size: 0.9rem !important;
    }

    /* ── Alerts ──────────────────────────────────────────── */
    .stAlert { border-radius: 10px !important; font-size: 0.875rem !important; }

    /* ── Divider ─────────────────────────────────────────── */
    hr { border-color: #E2E8F0 !important; margin: 16px 0 !important; }

    /* ── Chat ────────────────────────────────────────────── */
    [data-testid="stChatMessage"] { border-radius: 12px !important; }
    .stChatInput > div {
        border-radius: 12px !important;
        border: 1.5px solid #E2E8F0 !important;
        box-shadow: 0 2px 8px rgba(0,0,0,0.06) !important;
    }
    .stChatInput > div:focus-within {
        border-color: #2563EB !important;
        box-shadow: 0 0 0 3px rgba(37,99,235,0.1) !important;
    }

    /* ── Page links (nav context — transparent tab style) ─── */
    [data-testid="stPageLink"] a { text-decoration: none !important; }
    [data-testid="stPageLink"] p {
        background: transparent !important;
        border: none !important;
        border-radius: 0 !important;
        padding: 0 14px !important;
        font-size: 0.82rem !important;
        font-weight: 500 !important;
        color: #64748B !important;
        height: 52px !important;
        display: flex !important;
        align-items: center !important;
        gap: 5px !important;
        border-bottom: 2px solid transparent !important;
        transition: color 0.15s, border-color 0.15s !important;
        white-space: nowrap !important;
        margin: 0 !important;
    }
    [data-testid="stPageLink"] p:hover {
        background: transparent !important;
        color: #2563EB !important;
        border-bottom-color: #BFDBFE !important;
    }
    </style>
    """, unsafe_allow_html=True)


# ── Top navigation bar ────────────────────────────────────────────────────────

_NAV_ITEMS = [
    ("dashboard",   "🏠", "Dashboard",    "app.py"),
    ("chat",        "🤖", "APEX Chat",    "pages/4_🤖_Chat.py"),
    ("predictions", "🔮", "Predictions",  "pages/7_🔮_Predictions.py"),
    ("database",    "📊", "Database",     "pages/1_📊_Database.py"),
    ("schedule",    "🗓️", "Schedule",   "pages/2_🗓️_Schedule.py"),
    ("watchlist",   "📋", "Watchlist",    "pages/3_📋_Watchlist.py"),
    ("portfolio",   "💼", "Portfolio",    "pages/5_💼_Portfolio.py"),
    ("validation",  "🎯", "Validation",   "pages/6_🎯_Validation.py"),
]


def top_nav(active: str = "dashboard") -> None:
    """Render a sticky top navigation bar with tab-style links."""
    # Build the HTML brand + active-tab portion; inactive tabs use st.page_link
    st.markdown(
        '<div class="top-nav-wrap">'
        '<div class="nav-brand">📈 Portfolio Intelligence</div>'
        '<div class="nav-tabs-row">',
        unsafe_allow_html=True,
    )

    # Render each nav item: active as styled HTML span, inactive as page_link
    # We use a zero-gap columns trick to pack them horizontally
    cols = st.columns([1.4] * len(_NAV_ITEMS), gap="small")
    for col, (key, icon, label, path) in zip(cols, _NAV_ITEMS):
        with col:
            if key == active:
                st.markdown(
                    f'<div class="nav-tab active">{icon} <span>{label}</span></div>',
                    unsafe_allow_html=True,
                )
            else:
                st.page_link(path, label=f"{icon} {label}", use_container_width=True)

    st.markdown('</div></div>', unsafe_allow_html=True)


# ── HTML component helpers ─────────────────────────────────────────────────────

def card(html: str, padding: str = "20px 24px", extra_style: str = "") -> None:
    st.markdown(
        f'<div style="background:white;border:1px solid #E2E8F0;border-radius:12px;'
        f'padding:{padding};margin-bottom:12px;box-shadow:0 1px 3px rgba(0,0,0,0.05);{extra_style}">'
        f'{html}</div>',
        unsafe_allow_html=True,
    )


def page_header(title: str, subtitle: str = "", icon: str = "") -> None:
    icon_html = f'<span style="font-size:1.6rem;margin-right:10px">{icon}</span>' if icon else ""
    sub_html  = (f'<p style="margin:4px 0 0;color:#64748B;font-size:0.95rem">{subtitle}</p>'
                 if subtitle else "")
    st.markdown(
        f'<div style="margin-bottom:24px;padding-bottom:16px;border-bottom:1px solid #E2E8F0">'
        f'<h1 style="margin:0;color:#0F172A;font-size:1.75rem;font-weight:700;display:flex;'
        f'align-items:center">{icon_html}{title}</h1>{sub_html}</div>',
        unsafe_allow_html=True,
    )


def section_title(text: str, badge_text: str = "", badge_color: str = PRIMARY) -> None:
    badge_html = (
        f'<span style="background:{badge_color}1A;color:{badge_color};font-size:0.75rem;'
        f'font-weight:600;padding:2px 10px;border-radius:20px;margin-left:10px">{badge_text}</span>'
        if badge_text else ""
    )
    st.markdown(
        f'<h3 style="margin:0 0 12px;color:#0F172A;font-size:1rem;font-weight:700">'
        f'{text}{badge_html}</h3>',
        unsafe_allow_html=True,
    )


def badge_html(text: str, color: str = PRIMARY, bg: str = PRIMARY_LIGHT,
               size: str = "0.8rem") -> str:
    return (
        f'<span style="background:{bg};color:{color};padding:3px 10px;border-radius:20px;'
        f'font-size:{size};font-weight:600;white-space:nowrap">{text}</span>'
    )


def rec_badge_html(rec: str) -> str:
    col, bg, label = REC_STYLES.get(rec, (NEUTRAL, NEUTRAL_LIGHT, rec))
    return badge_html(label, col, bg)


def score_bar_html(score: int | None, max_score: int = 10, color: str = PRIMARY) -> str:
    if score is None:
        return '<span style="color:#94A3B8;font-size:0.8rem">N/A</span>'
    pct = max(0, min(100, (score / max_score) * 100))
    return (
        f'<div style="display:flex;align-items:center;gap:8px">'
        f'<div style="flex:1;height:6px;background:#E2E8F0;border-radius:3px">'
        f'<div style="width:{pct}%;height:100%;background:{color};border-radius:3px"></div></div>'
        f'<span style="font-weight:700;font-size:0.85rem;color:#0F172A;min-width:28px">{score}/{max_score}</span>'
        f'</div>'
    )


def stat_card_html(value: str, label: str, icon: str = "", color: str = PRIMARY,
                   delta: str = "") -> str:
    delta_html = (
        f'<p style="margin:4px 0 0;font-size:0.78rem;color:#059669">{delta}</p>'
        if delta else ""
    )
    return (
        f'<div style="background:white;border:1px solid #E2E8F0;border-radius:12px;'
        f'padding:18px 20px;box-shadow:0 1px 2px rgba(0,0,0,0.04)">'
        f'<div style="display:flex;justify-content:space-between;align-items:flex-start">'
        f'<div>'
        f'<p style="margin:0;font-size:0.75rem;font-weight:600;text-transform:uppercase;'
        f'letter-spacing:0.05em;color:#64748B">{label}</p>'
        f'<p style="margin:4px 0 0;font-size:1.9rem;font-weight:700;color:#0F172A;'
        f'line-height:1">{value}</p>{delta_html}</div>'
        f'<span style="font-size:1.5rem;opacity:0.6">{icon}</span></div></div>'
    )
