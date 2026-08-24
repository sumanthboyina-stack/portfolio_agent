"""
Shared design system for the Portfolio Agent web app.

Production-grade design language inspired by ChatGPT / Claude AI:
clean, minimal, airy — professional finance edition.
"""

from __future__ import annotations
import streamlit as st

# ── Color tokens ──────────────────────────────────────────────────────────────

PRIMARY        = "#2563EB"   # Blue-600
PRIMARY_LIGHT  = "#EFF6FF"   # Blue-50
PRIMARY_DARK   = "#1D4ED8"   # Blue-700
SUCCESS        = "#059669"   # Emerald-600
SUCCESS_LIGHT  = "#ECFDF5"   # Emerald-50
WARNING        = "#D97706"   # Amber-600
WARNING_LIGHT  = "#FFFBEB"   # Amber-50
DANGER         = "#DC2626"   # Red-600
DANGER_LIGHT   = "#FEF2F2"   # Red-50
PURPLE         = "#7C3AED"   # Violet-600
PURPLE_LIGHT   = "#F5F3FF"   # Violet-50
NEUTRAL        = "#6B7280"   # Gray-500
NEUTRAL_LIGHT  = "#F9FAFB"   # Gray-50
PAGE_BG        = "#FFFFFF"
CARD_BG        = "#FFFFFF"
BORDER         = "#E5E7EB"   # Gray-200
TEXT_PRIMARY   = "#111827"   # Gray-900
TEXT_SECONDARY = "#6B7280"   # Gray-500
SIDEBAR_BG     = "#111827"   # Near-black

# Recommendation colors
REC_STYLES: dict[str, tuple[str, str, str]] = {
    "STRONG_BUY":  (SUCCESS,  SUCCESS_LIGHT,  "🟢 STRONG BUY"),
    "BUY":         ("#16A34A","#F0FDF4",      "🟢 BUY"),
    "HOLD":        (WARNING,  WARNING_LIGHT,  "🟡 HOLD"),
    "SELL":        (DANGER,   DANGER_LIGHT,   "🔴 SELL"),
    "STRONG_SELL": ("#B91C1C","#FEE2E2",      "🔴 STRONG SELL"),
}
PRED_COLORS: dict[str, tuple[str, str]] = {
    "BULLISH": (SUCCESS, SUCCESS_LIGHT),
    "NEUTRAL": (WARNING, WARNING_LIGHT),
    "BEARISH": (DANGER,  DANGER_LIGHT),
}


# ── Shared color utilities ────────────────────────────────────────────────────

def score_color(score) -> str:
    """Return a color token for a 1–10 analyst score."""
    if score is None:
        return NEUTRAL
    try:
        v = float(score)
    except (TypeError, ValueError):
        return NEUTRAL
    if v >= 7:
        return SUCCESS
    if v >= 5:
        return WARNING
    return DANGER


def accuracy_color(pct: float) -> str:
    """Return a color token for directional accuracy (0–100)."""
    if pct > 55:
        return SUCCESS
    if pct > 45:
        return WARNING
    return DANGER


def brier_color(score: float) -> str:
    """Return a color token for Brier score (lower is better; 0.25 baseline)."""
    if score < 0.22:
        return SUCCESS
    if score < 0.25:
        return WARNING
    return DANGER


def concentration_color(weight_pct: float) -> str:
    """Return a color token for portfolio concentration %."""
    if weight_pct > 20:
        return DANGER
    if weight_pct > 15:
        return WARNING
    return PRIMARY


def freshness_color(date_str, warn_days: int = 3):
    """Return (display_text, color) for a data freshness date string."""
    from datetime import datetime as _dt
    if not date_str:
        return "missing", DANGER
    try:
        n = (_dt.now() - _dt.fromisoformat(str(date_str)[:10])).days
    except Exception:
        return str(date_str), NEUTRAL
    if n == 0:
        return "today", SUCCESS
    if n <= warn_days:
        return f"{n}d ago", WARNING
    return f"{n}d ago", DANGER


# ── Global CSS injection ───────────────────────────────────────────────────────

def inject_global_css() -> None:
    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap');

    /* ── Reset & Base ───────────────────────────────────────── */
    html, body, [class*="css"] {
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif !important;
        -webkit-font-smoothing: antialiased !important;
        -moz-osx-font-smoothing: grayscale !important;
    }
    .stApp { background: #FFFFFF !important; }
    .block-container {
        padding-top: 0.25rem !important;
        padding-bottom: 2.5rem !important;
        max-width: 1360px !important;
    }
    [data-testid="stSidebarNav"] { display: none !important; }

    /* Custom scrollbar */
    ::-webkit-scrollbar { width: 5px; height: 5px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: #D1D5DB; border-radius: 99px; }
    ::-webkit-scrollbar-thumb:hover { background: #9CA3AF; }

    /* ── Top nav ─────────────────────────────────────────────── */
    .top-nav-wrap {
        background: rgba(255,255,255,0.95);
        backdrop-filter: blur(12px);
        -webkit-backdrop-filter: blur(12px);
        border-bottom: 1px solid #F3F4F6;
        margin: -0.25rem -1rem 1.75rem;
        padding: 0 1.5rem;
        display: flex;
        align-items: stretch;
        min-height: 58px;
        position: sticky;
        top: 0;
        z-index: 999;
        box-shadow: 0 1px 0 rgba(0,0,0,0.05);
    }
    .nav-brand {
        font-weight: 800;
        color: #111827;
        font-size: 1.05rem;
        display: flex;
        align-items: center;
        gap: 8px;
        padding-right: 24px;
        border-right: 1px solid #F3F4F6;
        margin-right: 4px;
        letter-spacing: -0.025em;
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
        padding: 0 13px;
        font-size: 1rem;
        font-weight: 500;
        color: #6B7280;
        border-bottom: 2px solid transparent;
        cursor: pointer;
        text-decoration: none;
        white-space: nowrap;
        transition: color 0.12s, border-color 0.12s;
    }
    .nav-tab:hover { color: #2563EB; border-bottom-color: #BFDBFE; }
    .nav-tab.active {
        color: #2563EB;
        border-bottom-color: #2563EB;
        font-weight: 600;
    }

    /* ── Sidebar ─────────────────────────────────────────────── */
    [data-testid="stSidebar"] {
        background: #111827 !important;
        border-right: 1px solid #1F2937 !important;
    }
    [data-testid="stSidebar"] label,
    [data-testid="stSidebar"] .stMarkdown,
    [data-testid="stSidebar"] .stCaption,
    [data-testid="stSidebar"] p,
    [data-testid="stSidebar"] span { color: #9CA3AF !important; }
    [data-testid="stSidebar"] h1,
    [data-testid="stSidebar"] h2,
    [data-testid="stSidebar"] h3 { color: #F9FAFB !important; }
    [data-testid="stSidebar"] hr { border-color: #1F2937 !important; }
    [data-testid="stSidebar"] .stDivider { border-color: #1F2937 !important; }

    /* Sidebar generic buttons */
    [data-testid="stSidebar"] .stButton > button {
        width: 100%;
        background: rgba(255,255,255,0.08) !important;
        border: 1px solid rgba(255,255,255,0.22) !important;
        color: #E5E7EB !important;
        border-radius: 8px !important;
        font-weight: 600 !important;
        text-align: center !important;
        padding: 8px 13px !important;
        font-size: 0.83rem !important;
        transition: all 0.15s ease !important;
        box-shadow: none !important;
    }
    [data-testid="stSidebar"] .stButton > button:hover {
        background: rgba(255,255,255,0.18) !important;
        border-color: rgba(255,255,255,0.4) !important;
        color: #FFFFFF !important;
        box-shadow: 0 2px 8px rgba(0,0,0,0.3) !important;
    }
    [data-testid="stSidebar"] .stButton > button[kind="primary"] {
        background: rgba(37,99,235,0.25) !important;
        border-color: rgba(37,99,235,0.55) !important;
        color: #93C5FD !important;
        font-weight: 600 !important;
    }
    [data-testid="stSidebar"] .stButton > button[kind="primary"]:hover {
        background: rgba(37,99,235,0.45) !important;
        border-color: rgba(37,99,235,0.75) !important;
        color: #FFFFFF !important;
        box-shadow: 0 2px 8px rgba(37,99,235,0.35) !important;
    }
    [data-testid="stSidebar"] .stSelectbox > div > div,
    [data-testid="stSidebar"] .stTextInput > div > div > input {
        background: #1F2937 !important;
        border-color: #374151 !important;
        color: #F9FAFB !important;
        border-radius: 8px !important;
    }

    /* Sidebar chat history buttons override (Chat page) */
    [data-testid="stSidebar"] [data-testid="stButton"] button {
        text-align: left !important;
        white-space: pre-line !important;
        line-height: 1.35 !important;
        font-size: 0.72rem !important;
        color: rgba(255,255,255,0.9) !important;
        background: rgba(255,255,255,0.05) !important;
        border: 1px solid rgba(255,255,255,0.1) !important;
        border-radius: 7px !important;
        padding: 6px 9px !important;
    }
    /* Override sidebar p-tag rule that grays out button text */
    [data-testid="stSidebar"] [data-testid="stButton"] button p,
    [data-testid="stSidebar"] [data-testid="stButton"] button span {
        color: rgba(255,255,255,0.9) !important;
        font-size: 0.72rem !important;
    }
    [data-testid="stSidebar"] [data-testid="stButton"] button:hover {
        background: rgba(255,255,255,0.12) !important;
        border-color: rgba(255,255,255,0.25) !important;
        color: rgba(255,255,255,0.9) !important;
    }
    [data-testid="stSidebar"] [data-testid="stButton"] button:hover p,
    [data-testid="stSidebar"] [data-testid="stButton"] button:hover span {
        color: #FFFFFF !important;
    }
    /* Delete button in chat history */
    [data-testid="stSidebar"] [data-testid="stHorizontalBlock"] > div:last-child [data-testid="stButton"] button {
        background: rgba(239,68,68,0.1) !important;
        border: 1px solid rgba(239,68,68,0.25) !important;
        color: #FCA5A5 !important;
        font-size: 0.85rem !important;
        padding: 6px 8px !important;
        min-height: 0 !important;
        border-radius: 7px !important;
    }
    [data-testid="stSidebar"] [data-testid="stHorizontalBlock"] > div:last-child [data-testid="stButton"] button:hover {
        background: rgba(239,68,68,0.25) !important;
        border-color: rgba(239,68,68,0.5) !important;
        color: #FEE2E2 !important;
    }

    /* ── Main buttons ────────────────────────────────────────── */
    .stButton > button,
    .stFormSubmitButton > button {
        border-radius: 8px !important;
        font-weight: 600 !important;
        font-size: 0.85rem !important;
        padding: 9px 18px !important;
        transition: all 0.15s ease !important;
        border: 1px solid #2563EB !important;
        color: white !important;
        background: #2563EB !important;
        letter-spacing: -0.01em !important;
        box-shadow: 0 1px 3px rgba(37,99,235,0.3) !important;
    }
    .stButton > button:hover,
    .stFormSubmitButton > button:hover {
        border-color: #1D4ED8 !important;
        background: #1D4ED8 !important;
        color: white !important;
        box-shadow: 0 4px 14px rgba(37,99,235,0.35) !important;
        transform: translateY(-1px) !important;
    }
    .stButton > button[kind="primary"],
    .stFormSubmitButton > button[kind="primary"] {
        background: #2563EB !important;
        border-color: #2563EB !important;
        color: white !important;
        font-weight: 600 !important;
        box-shadow: 0 1px 3px rgba(37,99,235,0.3) !important;
    }
    .stButton > button[kind="primary"]:hover,
    .stFormSubmitButton > button[kind="primary"]:hover {
        background: #1D4ED8 !important;
        border-color: #1D4ED8 !important;
        box-shadow: 0 4px 14px rgba(37,99,235,0.35) !important;
        transform: translateY(-1px) !important;
    }

    /* Buttons in main area — blue/white consistent */
    [data-testid="stMain"] [data-testid="stButton"] button {
        background: #2563EB !important;
        border: 1px solid #2563EB !important;
        color: white !important;
        font-weight: 600 !important;
        font-size: 0.85rem !important;
        border-radius: 8px !important;
        padding: 9px 18px !important;
        transition: all 0.15s ease !important;
        box-shadow: 0 1px 3px rgba(37,99,235,0.3) !important;
    }
    [data-testid="stMain"] [data-testid="stButton"] button:hover {
        background: #1D4ED8 !important;
        border-color: #1D4ED8 !important;
        color: white !important;
        transform: translateY(-1px) !important;
        box-shadow: 0 4px 14px rgba(37,99,235,0.35) !important;
    }
    [data-testid="stMain"] [data-testid="stButton"] button[kind="primary"] {
        background: #2563EB !important;
        border-color: #2563EB !important;
        color: white !important;
        font-weight: 600 !important;
        box-shadow: 0 1px 3px rgba(37,99,235,0.3) !important;
    }
    [data-testid="stMain"] [data-testid="stButton"] button[kind="primary"]:hover {
        background: #1D4ED8 !important;
        color: white !important;
        transform: translateY(-1px) !important;
        box-shadow: 0 4px 14px rgba(37,99,235,0.35) !important;
    }

    /* ── Inputs & selects ───────────────────────────────────── */
    .stTextInput > div > div > input,
    .stTextArea > div > div > textarea {
        border-radius: 8px !important;
        border: 1px solid #E5E7EB !important;
        padding: 10px 14px !important;
        font-size: 0.875rem !important;
        background: #FFFFFF !important;
        color: #111827 !important;
        transition: border-color 0.15s, box-shadow 0.15s !important;
        font-family: 'Inter', sans-serif !important;
        box-shadow: 0 1px 2px rgba(0,0,0,0.04) !important;
    }
    .stTextInput > div > div > input:focus,
    .stTextArea > div > div > textarea:focus {
        border-color: #2563EB !important;
        box-shadow: 0 0 0 3px rgba(37,99,235,0.1) !important;
        outline: none !important;
    }
    .stTextInput > div > div > input::placeholder,
    .stTextArea > div > div > textarea::placeholder {
        color: #9CA3AF !important;
    }
    .stSelectbox > div > div,
    .stMultiSelect > div > div {
        border-radius: 8px !important;
        border: 1px solid #E5E7EB !important;
        background: #FFFFFF !important;
        box-shadow: 0 1px 2px rgba(0,0,0,0.04) !important;
    }
    .stNumberInput input {
        border-radius: 8px !important;
        border: 1px solid #E5E7EB !important;
    }

    /* ── Section tabs ────────────────────────────────────────── */
    .stTabs [data-baseweb="tab-list"] {
        gap: 2px;
        background: #F9FAFB;
        padding: 4px;
        border-radius: 10px;
        border: 1px solid #F3F4F6;
    }
    .stTabs [data-baseweb="tab"] {
        border-radius: 7px !important;
        padding: 7px 20px !important;
        font-weight: 500 !important;
        font-size: 0.82rem !important;
        color: #6B7280 !important;
        transition: all 0.12s !important;
    }
    .stTabs [aria-selected="true"] {
        background: white !important;
        color: #111827 !important;
        font-weight: 600 !important;
        box-shadow: 0 1px 4px rgba(0,0,0,0.07) !important;
    }
    .stTabs [data-baseweb="tab"]:hover:not([aria-selected="true"]) {
        background: rgba(37,99,235,0.06) !important;
        color: #2563EB !important;
    }

    /* ── Metrics ─────────────────────────────────────────────── */
    [data-testid="stMetric"] {
        background: #FFFFFF;
        border: 1px solid #F3F4F6;
        border-radius: 12px;
        padding: 18px 20px !important;
        box-shadow: 0 1px 3px rgba(0,0,0,0.04), 0 1px 2px rgba(0,0,0,0.03);
        transition: box-shadow 0.2s, transform 0.2s;
    }
    [data-testid="stMetric"]:hover {
        box-shadow: 0 4px 16px rgba(0,0,0,0.07);
        transform: translateY(-1px);
    }
    [data-testid="metric-container"] > div:first-child {
        font-size: 0.7rem !important;
        font-weight: 700 !important;
        text-transform: uppercase !important;
        letter-spacing: 0.08em !important;
        color: #9CA3AF !important;
    }
    [data-testid="metric-container"] [data-testid="stMetricValue"] {
        font-size: 1.8rem !important;
        font-weight: 800 !important;
        color: #111827 !important;
        letter-spacing: -0.04em !important;
        line-height: 1.1 !important;
    }
    [data-testid="metric-container"] [data-testid="stMetricDelta"] {
        font-size: 0.78rem !important;
        font-weight: 600 !important;
    }

    /* ── DataFrames & tables ─────────────────────────────────── */
    .stDataFrame {
        border-radius: 10px !important;
        border: 1px solid #F3F4F6 !important;
        overflow: hidden !important;
        box-shadow: 0 1px 3px rgba(0,0,0,0.04) !important;
    }
    .stDataFrame thead tr th {
        background: #F9FAFB !important;
        font-size: 0.72rem !important;
        font-weight: 700 !important;
        text-transform: uppercase !important;
        letter-spacing: 0.06em !important;
        color: #6B7280 !important;
        border-bottom: 1px solid #E5E7EB !important;
        padding: 10px 14px !important;
    }
    .stDataFrame tbody tr:nth-child(even) { background: #FAFAFA !important; }
    .stDataFrame tbody tr:hover { background: #EFF6FF !important; }
    .stDataFrame table {
        font-size: 0.84rem !important;
        color: #111827 !important;
    }

    /* ── Expander ────────────────────────────────────────────── */
    .streamlit-expanderHeader {
        border-radius: 9px !important;
        font-weight: 600 !important;
        font-size: 0.875rem !important;
        background: #F9FAFB !important;
        border: 1px solid #F3F4F6 !important;
        padding: 10px 16px !important;
        color: #111827 !important;
        transition: background 0.12s !important;
    }
    .streamlit-expanderHeader:hover { background: #F3F4F6 !important; }
    .streamlit-expanderContent {
        border: 1px solid #F3F4F6 !important;
        border-top: none !important;
        border-radius: 0 0 9px 9px !important;
        padding: 14px 16px !important;
    }

    /* ── Alerts ──────────────────────────────────────────────── */
    .stAlert {
        border-radius: 10px !important;
        font-size: 0.875rem !important;
        border: 1px solid transparent !important;
        padding: 12px 16px !important;
    }
    [data-testid="stAlert"][data-type="success"] { background: #F0FDF4 !important; border-color: #BBF7D0 !important; color: #15803D !important; }
    [data-testid="stAlert"][data-type="warning"] { background: #FFFBEB !important; border-color: #FDE68A !important; color: #92400E !important; }
    [data-testid="stAlert"][data-type="error"]   { background: #FEF2F2 !important; border-color: #FECACA !important; color: #991B1B !important; }
    [data-testid="stAlert"][data-type="info"]    { background: #EFF6FF !important; border-color: #BFDBFE !important; color: #1E40AF !important; }

    /* ── Divider ─────────────────────────────────────────────── */
    hr { border-color: #F3F4F6 !important; margin: 20px 0 !important; }
    [data-testid="stDivider"] { border-color: #F3F4F6 !important; }

    /* ── Spinner & progress ──────────────────────────────────── */
    .stSpinner > div { border-color: #2563EB transparent transparent transparent !important; }
    .stProgress > div > div { background: #2563EB !important; border-radius: 99px !important; }
    .stProgress > div { background: #F3F4F6 !important; border-radius: 99px !important; }

    /* ── Code blocks ─────────────────────────────────────────── */
    code {
        background: #F3F4F6 !important;
        color: #1D4ED8 !important;
        padding: 2px 6px !important;
        border-radius: 4px !important;
        font-size: 0.82em !important;
        font-family: 'Fira Code', 'JetBrains Mono', 'Consolas', monospace !important;
    }
    pre code {
        background: #111827 !important;
        color: #E5E7EB !important;
        padding: 14px 18px !important;
        border-radius: 10px !important;
        display: block !important;
        font-size: 0.83rem !important;
        line-height: 1.6 !important;
    }

    /* ── Chat ────────────────────────────────────────────────── */
    [data-testid="stChatMessage"] {
        border-radius: 12px !important;
        border: 1px solid #F3F4F6 !important;
        padding: 14px 18px !important;
    }
    [data-testid="stChatMessage"][data-testid*="user"] {
        background: #F9FAFB !important;
    }
    .stChatInput > div {
        border-radius: 12px !important;
        border: 1.5px solid #E5E7EB !important;
        background: #FFFFFF !important;
        box-shadow: 0 2px 10px rgba(0,0,0,0.06) !important;
        transition: border-color 0.15s, box-shadow 0.15s !important;
    }
    .stChatInput > div:focus-within {
        border-color: #2563EB !important;
        box-shadow: 0 0 0 3px rgba(37,99,235,0.1), 0 2px 10px rgba(0,0,0,0.06) !important;
    }

    /* ── Page links (nav) ────────────────────────────────────── */
    [data-testid="stPageLink"] a { text-decoration: none !important; }
    [data-testid="stPageLink"] p {
        background: transparent !important;
        border: none !important;
        border-radius: 0 !important;
        padding: 0 13px !important;
        font-size: 1rem !important;
        font-weight: 500 !important;
        color: #6B7280 !important;
        height: 58px !important;
        display: flex !important;
        align-items: center !important;
        gap: 5px !important;
        border-bottom: 2px solid transparent !important;
        transition: color 0.12s, border-color 0.12s !important;
        white-space: nowrap !important;
        margin: 0 !important;
    }
    [data-testid="stPageLink"] p:hover {
        background: transparent !important;
        color: #2563EB !important;
        border-bottom-color: #BFDBFE !important;
    }

    /* ── Checkbox & radio (main area) ───────────────────────── */
    .stCheckbox label span:first-child,
    .stRadio label span:first-child {
        border-color: #D1D5DB !important;
        border-radius: 4px !important;
    }

    /* ── Sidebar radio — visible on dark background ──────────── */
    [data-testid="stSidebar"] .stRadio label {
        color: #D1D5DB !important;
        font-size: 0.83rem !important;
        padding: 4px 0 !important;
        cursor: pointer !important;
    }
    [data-testid="stSidebar"] .stRadio label:hover {
        color: #F9FAFB !important;
    }
    [data-testid="stSidebar"] .stRadio [data-baseweb="radio"] div:first-child {
        border-color: #4B5563 !important;
        background: transparent !important;
    }
    [data-testid="stSidebar"] .stRadio [aria-checked="true"] div:first-child {
        border-color: #3B82F6 !important;
        background: #3B82F6 !important;
    }
    [data-testid="stSidebar"] .stRadio [aria-checked="true"] + div {
        color: #93C5FD !important;
        font-weight: 600 !important;
    }

    /* ── Slider ──────────────────────────────────────────────── */
    .stSlider [data-baseweb="slider"] [role="slider"] {
        background: #2563EB !important;
        border-color: #2563EB !important;
    }
    .stSlider [data-baseweb="slider"] [data-testid="stThumbValue"] { color: #2563EB !important; }

    /* ── Date group in chat sidebar ──────────────────────────── */
    .date-group {
        font-size: 0.68rem; font-weight: 700; text-transform: uppercase;
        letter-spacing: 0.08em; color: #6B7280; padding: 10px 4px 4px;
    }
    </style>
    """, unsafe_allow_html=True)


# ── Top navigation bar ────────────────────────────────────────────────────────

_NAV_PRIMARY = [
    ("dashboard",         "🏠", "Today",             "app.py"),
    ("opportunity_engine", "🚀", "Opportunity Engine", "pages/9_🎯_Opportunity_Engine.py"),
    ("opportunities",     "🧭", "Opportunities",     "pages/8_🧭_Opportunities.py"),
    ("predictions",       "🔮", "Predictions",       "pages/7_🔮_Predictions.py"),
    ("portfolio",         "💼", "Portfolio",         "pages/5_💼_Portfolio.py"),
]
_NAV_SECONDARY = [
    ("watchlist",   "📋", "Watchlist",     "pages/3_📋_Watchlist.py"),
    ("validation",  "🎯", "Validation",    "pages/6_🎯_Validation.py"),
    ("chat",        "🤖", "Chat",          "pages/4_🤖_Chat.py"),
    ("valuation",   "📐", "Valuation",     "pages/10_📐_Valuation.py"),
]
_NAV_ADMIN = [
    ("schedule",    "🗓️", "Schedule",    "pages/2_🗓️_Schedule.py"),
    ("database",    "📊", "Database",      "pages/1_📊_Database.py"),
]

def top_nav(active: str = "dashboard") -> None:
    """Render a sticky top nav: primary tabs, secondary tabs, and an admin drawer toggle."""
    st.markdown(
        '<div class="top-nav-wrap">'
        '<div class="nav-brand">📈 Portfolio Intelligence</div>'
        '<div class="nav-tabs-row">',
        unsafe_allow_html=True,
    )

    # primary (wider) | thin divider | secondary (narrower) | thin divider | admin gear
    n_primary, n_secondary = len(_NAV_PRIMARY), len(_NAV_SECONDARY)
    widths = [1.8] * n_primary + [0.3] + [1.4] * n_secondary + [0.3, 0.6]
    cols = st.columns(widths, gap="small")
    divider_1 = n_primary
    secondary_start = n_primary + 1
    divider_2 = secondary_start + n_secondary
    admin_col = divider_2 + 1

    for i, (key, icon, label, path) in enumerate(_NAV_PRIMARY):
        with cols[i]:
            if key == active:
                st.markdown(
                    f'<div class="nav-tab active">{icon} <span>{label}</span></div>',
                    unsafe_allow_html=True,
                )
            else:
                st.page_link(path, label=f"{icon} {label}", use_container_width=True)

    with cols[divider_1]:
        st.markdown(
            '<div style="width:1px;background:#E5E7EB;height:30px;margin:14px auto"></div>',
            unsafe_allow_html=True,
        )

    for i, (key, icon, label, path) in enumerate(_NAV_SECONDARY):
        with cols[secondary_start + i]:
            if key == active:
                st.markdown(
                    f'<div class="nav-tab active" style="font-size:0.875rem">'
                    f'{icon} <span>{label}</span></div>',
                    unsafe_allow_html=True,
                )
            else:
                st.page_link(path, label=f"{icon} {label}", use_container_width=True)

    with cols[divider_2]:
        st.markdown(
            '<div style="width:1px;background:#E5E7EB;height:30px;margin:14px auto"></div>',
            unsafe_allow_html=True,
        )

    with cols[admin_col]:
        if st.button("⚙️", key="admin_drawer_toggle", help="Admin: Schedule & Database",
                     use_container_width=True):
            st.session_state["_admin_drawer_open"] = not st.session_state.get("_admin_drawer_open", False)

    st.markdown('</div></div>', unsafe_allow_html=True)

    if st.session_state.get("_admin_drawer_open", False) or active in ("schedule", "database"):
        st.markdown(
            '<div style="padding:6px 24px 0;display:flex;gap:6px;align-items:center">'
            '<span style="font-size:0.7rem;font-weight:700;text-transform:uppercase;'
            'letter-spacing:0.08em;color:#9CA3AF;margin-right:4px">Admin</span></div>',
            unsafe_allow_html=True,
        )
        admin_cols = st.columns([1, 1, 6])
        for i, (key, icon, label, path) in enumerate(_NAV_ADMIN):
            with admin_cols[i]:
                if key == active:
                    st.markdown(
                        f'<div class="nav-tab active" style="font-size:0.85rem">'
                        f'{icon} <span>{label}</span></div>',
                        unsafe_allow_html=True,
                    )
                else:
                    st.page_link(path, label=f"{icon} {label}", use_container_width=True)


# ── HTML component helpers ─────────────────────────────────────────────────────

def card(html: str, padding: str = "20px 24px", extra_style: str = "") -> None:
    st.markdown(
        f'<div style="background:#FFFFFF;border:1px solid #F3F4F6;border-radius:12px;'
        f'padding:{padding};margin-bottom:10px;'
        f'box-shadow:0 1px 3px rgba(0,0,0,0.04),0 1px 2px rgba(0,0,0,0.03);{extra_style}">'
        f'{html}</div>',
        unsafe_allow_html=True,
    )


def page_header(title: str, subtitle: str = "", icon: str = "") -> None:
    icon_html = f'<span style="font-size:1.5rem;margin-right:10px;opacity:0.85">{icon}</span>' if icon else ""
    sub_html  = (
        f'<p style="margin:5px 0 0;color:#6B7280;font-size:0.9rem;font-weight:400">{subtitle}</p>'
        if subtitle else ""
    )
    st.markdown(
        f'<div style="margin-bottom:28px;padding-bottom:18px;border-bottom:1px solid #F3F4F6">'
        f'<h1 style="margin:0;color:#111827;font-size:1.65rem;font-weight:700;'
        f'letter-spacing:-0.03em;display:flex;align-items:center">{icon_html}{title}</h1>'
        f'{sub_html}</div>',
        unsafe_allow_html=True,
    )


def section_title(text: str, badge_text: str = "", badge_color: str = PRIMARY) -> None:
    badge_html_str = (
        f'<span style="background:{badge_color}18;color:{badge_color};font-size:0.7rem;'
        f'font-weight:600;padding:2px 9px;border-radius:20px;margin-left:8px;'
        f'letter-spacing:0.01em">{badge_text}</span>'
        if badge_text else ""
    )
    st.markdown(
        f'<h3 style="margin:0 0 14px;color:#111827;font-size:0.95rem;font-weight:700;'
        f'letter-spacing:-0.01em">{text}{badge_html_str}</h3>',
        unsafe_allow_html=True,
    )


def ticker_label(ticker: str, max_len: int = 40) -> str:
    """
    Return "TICKER — Company Name" when the name is known (SEC-registered
    companies), else just "TICKER" (ETFs, foreign ADRs, delisted tickers).
    """
    from portfolio_agent.tools.company_names import get_company_name

    name = get_company_name(ticker)
    if not name:
        return ticker
    if len(name) > max_len:
        name = name[: max_len - 1].rstrip() + "…"
    return f"{ticker} — {name}"


def badge_html(text: str, color: str = PRIMARY, bg: str = PRIMARY_LIGHT,
               size: str = "0.78rem") -> str:
    return (
        f'<span style="background:{bg};color:{color};padding:3px 10px;border-radius:99px;'
        f'font-size:{size};font-weight:600;white-space:nowrap;letter-spacing:0.01em">{text}</span>'
    )


def rec_badge_html(rec: str) -> str:
    col, bg, label = REC_STYLES.get(rec, (NEUTRAL, NEUTRAL_LIGHT, rec))
    return badge_html(label, col, bg)


def score_bar_html(score: int | None, max_score: int = 10, color: str = PRIMARY) -> str:
    if score is None:
        return '<span style="color:#9CA3AF;font-size:0.8rem">N/A</span>'
    pct = max(0, min(100, (score / max_score) * 100))
    return (
        f'<div style="display:flex;align-items:center;gap:8px">'
        f'<div style="flex:1;height:5px;background:#F3F4F6;border-radius:99px">'
        f'<div style="width:{pct}%;height:100%;background:{color};border-radius:99px;'
        f'transition:width 0.3s"></div></div>'
        f'<span style="font-weight:700;font-size:0.82rem;color:#111827;min-width:32px;'
        f'text-align:right">{score}/{max_score}</span>'
        f'</div>'
    )


def stat_card_html(value: str, label: str, icon: str = "", color: str = PRIMARY,
                   delta: str = "") -> str:
    delta_html = (
        f'<p style="margin:6px 0 0;font-size:0.75rem;font-weight:600;color:#059669;'
        f'display:flex;align-items:center;gap:3px">▲ {delta}</p>'
        if delta else ""
    )
    return (
        f'<div style="background:#FFFFFF;border:1px solid #F3F4F6;border-radius:14px;'
        f'padding:18px 20px;box-shadow:0 1px 3px rgba(0,0,0,0.04),'
        f'0 1px 2px rgba(0,0,0,0.03);transition:box-shadow 0.2s,transform 0.2s;'
        f'border-top:3px solid {color}">'
        f'<div style="display:flex;justify-content:space-between;align-items:flex-start">'
        f'<div style="flex:1">'
        f'<p style="margin:0;font-size:0.68rem;font-weight:700;text-transform:uppercase;'
        f'letter-spacing:0.08em;color:#9CA3AF">{label}</p>'
        f'<p style="margin:6px 0 0;font-size:1.85rem;font-weight:800;color:#111827;'
        f'letter-spacing:-0.04em;line-height:1">{value}</p>'
        f'{delta_html}</div>'
        f'<div style="width:38px;height:38px;border-radius:10px;background:{color}12;'
        f'display:flex;align-items:center;justify-content:center;font-size:1.15rem;'
        f'flex-shrink:0">{icon}</div></div></div>'
    )
