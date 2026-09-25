"""
Shared design system for the Portfolio Agent web app.

Production-grade design language inspired by ChatGPT / Claude AI:
clean, minimal, airy — professional finance edition.
"""

from __future__ import annotations
import re
from pathlib import Path

import streamlit as st

_STATIC_DIR = Path(__file__).resolve().parent / "static"

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

# Recommendation colors — color carries the signal; labels are plain text (see #4: clean pills, not colorful cards)
REC_STYLES: dict[str, tuple[str, str, str]] = {
    "STRONG_BUY":  (SUCCESS,  SUCCESS_LIGHT,  "STRONG BUY"),
    "BUY":         ("#16A34A","#F0FDF4",      "BUY"),
    "HOLD":        (WARNING,  WARNING_LIGHT,  "HOLD"),
    "SELL":        (DANGER,   DANGER_LIGHT,   "SELL"),
    "STRONG_SELL": ("#B91C1C","#FEE2E2",      "STRONG SELL"),
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


def fmt_money(value, decimals: int = 2, signed: bool = False, dash: str = "—") -> str:
    """The one shared dollar formatter — always $X,XXX.XX (or .XX decimals given).
    `signed=True` adds a leading + for positive values (for gain/loss amounts)."""
    if value is None:
        return dash
    try:
        v = float(value)
    except (TypeError, ValueError):
        return dash
    sign = "-" if v < 0 else ("+" if signed and v > 0 else "")
    return f"{sign}${abs(v):,.{decimals}f}"


def fmt_pct(value, decimals: int = 2, signed: bool = False, dash: str = "—") -> str:
    """The one shared percent formatter — always X.XX% (optionally with a leading +/-)."""
    if value is None:
        return dash
    try:
        v = float(value)
    except (TypeError, ValueError):
        return dash
    return f"{v:+.{decimals}f}%" if signed else f"{v:.{decimals}f}%"


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
    @import url('https://fonts.googleapis.com/css2?family=Material+Symbols+Rounded:opsz,wght,FILL,GRAD@20..48,400,0,0&display=block');

    /* ── Reset & Base ───────────────────────────────────────── */
    html, body, [class*="css"] {
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif !important;
        -webkit-font-smoothing: antialiased !important;
        -moz-osx-font-smoothing: grayscale !important;
    }
    /* Financial figures: digits always align in columns, never jitter */
    html, body, [class*="css"] {
        font-variant-numeric: tabular-nums !important;
        font-feature-settings: "tnum" 1 !important;
    }

    /* ── Icons — Material Symbols (matches Streamlit's own :material/ icons) ── */
    .material-symbols-rounded {
        font-family: 'Material Symbols Rounded';
        font-weight: normal;
        font-style: normal;
        line-height: 1;
        letter-spacing: normal;
        text-transform: none;
        display: inline-block;
        white-space: nowrap;
        word-wrap: normal;
        direction: ltr;
        -webkit-font-smoothing: antialiased;
        font-variation-settings: 'FILL' 0, 'wght' 400, 'GRAD' 0, 'opsz' 24;
        vertical-align: middle;
    }
    /* Native Streamlit :material/ icons (buttons, page_link, alerts, expanders, tabs)
       should read as the same restrained, monochrome icon language as our own —
       never let them inherit alert/status colors decoratively. */
    [data-testid="stIconMaterial"] {
        font-variation-settings: 'FILL' 0, 'wght' 400, 'GRAD' 0, 'opsz' 20 !important;
    }
    .stApp { background: #FFFFFF !important; }
    /* Streamlit's fixed header is 60px tall; keep the nav row below it and let the header show through. */
    [data-testid="stHeader"] { background: transparent !important; }
    .block-container {
        padding-top: 4.4rem !important;
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
        font-size: 1.02rem;
        display: flex;
        align-items: center;
        gap: 8px;
        height: 58px;
        padding-right: 16px;
        border-right: 1px solid #E5E7EB;
        letter-spacing: -0.025em;
        white-space: nowrap;
        overflow: visible;
    }
    .nav-underline { height: 1px; background: #F3F4F6; margin: -6px 0 18px; }
    @media (max-width: 1450px) {
        /* tight row: labels only, icons dropped so nothing overlaps */
        [data-testid="stPageLink"] p, .nav-tab { font-size: 0.84rem !important; padding: 0 5px !important; }
        [data-testid="stPageLink"] [data-testid="stIconMaterial"], .nav-tab .material-symbols-rounded { display: none !important; }
        .nav-brand { font-size: 0.92rem; padding-right: 10px; }
    }
    @media (max-width: 1400px) {
        .nav-brand .brand-text { display: none; }
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
        justify-content: center;
        gap: 5px;
        padding: 0 9px;
        font-size: 0.92rem;
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
    /* Delete button in chat history — scoped to its own keyed container
       (st.container(key="chat_del_wrap_...")) rather than a positional
       :last-child guess, which used to also catch unrelated sidebar
       button pairs (e.g. Database's Today/7-days quick-select grid). */
    [data-testid="stSidebar"] [class*="st-key-chat_del_wrap_"] [data-testid="stButton"] button {
        background: rgba(239,68,68,0.1) !important;
        border: 1px solid rgba(239,68,68,0.25) !important;
        color: #FCA5A5 !important;
        font-size: 0.85rem !important;
        padding: 6px 8px !important;
        min-height: 0 !important;
        border-radius: 7px !important;
    }
    [data-testid="stSidebar"] [class*="st-key-chat_del_wrap_"] [data-testid="stButton"] button:hover {
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
    [data-testid="stMain"] [data-testid="stButton"] button p,
    [data-testid="stMain"] [data-testid="stPopoverButton"] p { white-space: nowrap !important; }
    [data-testid="stMain"] [data-testid="stButton"] button {
        background: #2563EB !important;
        border: 1px solid #2563EB !important;
        color: white !important;
        font-weight: 600 !important;
        font-size: 0.85rem !important;
        border-radius: 8px !important;
        padding: 9px 14px !important;
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

    /* Trash icons on Portfolio broker / account tiles
       (st.container(key="tile_del_...")). Declared AFTER the blue main-area
       rules with higher specificity so they win: no background, no border,
       no shadow — the icon sits directly on the tile's own colour. */
    [data-testid="stMain"] [class*="st-key-tile_del_"] [data-testid="stButton"] button,
    [data-testid="stMain"] [class*="st-key-tile_del_"] [data-testid="stButton"] button:hover,
    [data-testid="stMain"] [class*="st-key-tile_del_"] [data-testid="stButton"] button:active {
        background: transparent !important;
        border: none !important;
        box-shadow: none !important;
        transform: none !important;
        color: #9CA3AF !important;
        padding: 2px 4px !important;
        min-height: 0 !important;
        float: right;
    }
    /* Keyboard users must still see where focus is. */
    [data-testid="stMain"] [class*="st-key-tile_del_"] [data-testid="stButton"] button:focus-visible {
        outline: 2px solid #2563EB !important;
        outline-offset: 2px !important;
    }
    [data-testid="stMain"] [class*="st-key-tile_del_"] [data-testid="stButton"] button:hover {
        color: #DC2626 !important;
    }
    [data-testid="stMain"] [class*="st-key-tile_del_"] [data-testid="stButton"] button span,
    [data-testid="stMain"] [class*="st-key-tile_del_"] [data-testid="stButton"] button p {
        color: inherit !important;
        background: transparent !important;
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
    /* icon and label are siblings inside the link container: centre them together, as one unit */
    [data-testid="stPageLink"] {
        display: flex !important; justify-content: center !important; align-items: center !important; gap: 0 !important;
    }
    [data-testid="stPageLink"] a {
        display: flex !important; align-items: center !important; justify-content: center !important;
        gap: 4px !important; width: max-content !important; max-width: none !important; overflow: visible !important;
        margin: 0 !important;
    }
    [data-testid="stPageLink"] [data-testid="stMarkdownContainer"] { overflow: visible !important; max-width: none !important; }
    [data-testid="stPageLink"] p {
        background: transparent !important;
        border: none !important;
        border-radius: 0 !important;
        padding: 0 8px 0 3px !important;
        font-size: 0.92rem !important;
        overflow: visible !important;
        text-overflow: clip !important;
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

    /* ── Top-right settings menu (gear icon + dropdown) ─────── */
    [class*="st-key-settings_menu"] { display:flex; justify-content:flex-end; }
    [class*="st-key-settings_menu"] [data-testid="stPopover"] { display: flex; justify-content: flex-end; }
    [class*="st-key-settings_menu"] button,
    [class*="st-key-settings_menu"] button:hover,
    [class*="st-key-settings_menu"] button:active,
    [class*="st-key-settings_menu"] button:focus,
    [class*="st-key-settings_menu"] button[aria-expanded="true"] {
        background: transparent !important;
        border: none !important;
        outline: none !important;
        box-shadow: none !important;
        color: #6B7280 !important;
        min-height: 0 !important;
        height: 58px;                       /* same row height as the nav tabs */
        width: auto !important;
        padding: 0 6px !important;
        transform: none !important;
    }
    [class*="st-key-settings_menu"] button:hover,
    [class*="st-key-settings_menu_active"] button { color: #2563EB !important; }
    [class*="st-key-settings_menu"] button:focus-visible {
        outline: 2px solid #2563EB !important; outline-offset: 2px !important;
    }
    [class*="st-key-settings_menu"] button [data-testid="stIconMaterial"] {
        font-size: 22px !important; color: inherit !important;
    }
    /* hide the popover's caret so it reads as a plain icon */
    [class*="st-key-settings_menu"] button svg,
    [class*="st-key-settings_menu"] button > div > div[aria-hidden="true"] { display:none !important; }

    /* ── Admin-only entries in the account/settings dropdown ─── */
    [class*="st-key-nav_admin_only_"] [data-testid="stPageLink"] p,
    [class*="st-key-nav_admin_only_"] [data-testid="stIconMaterial"] {
        color: #DC2626 !important;
    }
    [class*="st-key-nav_admin_only_"] [data-testid="stPageLink"]:hover p {
        color: #B91C1C !important;
    }
    .settings-active-dot {
        width: 6px; height: 6px; border-radius: 50%; background: #2563EB;
        margin: -8px 14px 0 auto;
    }
    /* dropdown body: vertical rows */
    [data-testid="stPopoverBody"] .side-nav-title { padding: 0 4px 6px; }
    [data-testid="stPopoverBody"] [data-testid="stPageLink"] p {
        height: auto !important; padding: 8px 10px !important; margin: 2px 0 !important;
        border-bottom: none !important; border-left: 3px solid transparent !important;
        border-radius: 8px !important; font-size: 0.9rem !important; gap: 8px !important;
        color: #374151 !important; min-width: 220px;
    }
    [data-testid="stPopoverBody"] [data-testid="stPageLink"] p:hover {
        background: #EFF6FF !important; color: #2563EB !important; border-left-color: #93C5FD !important;
    }
    .nav-menu-item {
        display:flex; align-items:center; gap:8px; padding:8px 10px; margin:2px 0; min-width:220px;
        border-radius:8px; font-size:0.9rem; font-weight:600; color:#1D4ED8;
        background:#EFF6FF; border-left:3px solid #2563EB;
    }
    /* ── Sidebar settings nav (vertical) ────────────────────── */
    .side-nav-title {
        display: flex; align-items: center; gap: 6px;
        font-size: 0.7rem; font-weight: 700; text-transform: uppercase;
        letter-spacing: 0.08em; color: #9CA3AF !important;
        padding: 4px 10px 8px; margin: 0;
    }
    .side-nav-title span { color: #9CA3AF !important; }
    .side-nav-item {
        display: flex; align-items: center; gap: 8px;
        padding: 8px 10px; margin: 2px 0;
        border-radius: 8px; font-size: 0.9rem; font-weight: 600;
        color: #F9FAFB !important; background: rgba(37,99,235,0.28);
        border-left: 3px solid #60A5FA;
    }
    .side-nav-item span { color: #F9FAFB !important; }
    .side-nav-end {
        height: 1px; background: #1F2937; margin: 10px 0 14px;
    }
    /* Sidebar page links: vertical rows, not the 58px underline tabs used in the top nav */
    [data-testid="stSidebar"] [data-testid="stPageLink"] p {
        height: auto !important;
        padding: 8px 10px !important;
        margin: 2px 0 !important;
        border-bottom: none !important;
        border-left: 3px solid transparent !important;
        border-radius: 8px !important;
        font-size: 0.9rem !important;
        gap: 8px !important;
        color: #D1D5DB !important;
        transition: background 0.12s, color 0.12s !important;
    }
    [data-testid="stSidebar"] [data-testid="stPageLink"] p:hover {
        background: rgba(255,255,255,0.06) !important;
        color: #FFFFFF !important;
        border-bottom: none !important;
    }
    [data-testid="stSidebar"] [data-testid="stPageLink"] span,
    [data-testid="stSidebar"] [data-testid="stPageLink"] [data-testid="stIconMaterial"] {
        color: inherit !important;
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

    /* ── Section tiles (section_tile): a disclosure row, not a button ──
       Declared after the blue main-area button rules and more specific than
       them, so the header keeps the tile's own white background. */
    [data-testid="stMain"] [class*="st-key-tilebox_"] {
        background: #FFFFFF; border: 1px solid #E2E8F0 !important; border-radius: 12px !important;
        box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04); padding: 0 18px 4px !important;
    }
    /* Streamlit puts its 1rem container padding one level down; the tile sets its own */
    [data-testid="stMain"] [class*="st-key-tilebox_"] > div,
    [data-testid="stMain"] [class*="st-key-tilebox_"] > div > div { padding: 0 !important; border: none !important; }
    [data-testid="stMain"] [class*="st-key-tilehdr_"] { margin: 0 -18px; }
    [data-testid="stMain"] [class*="st-key-tilebox_"] > div > [data-testid="stVerticalBlock"] { gap: 0.5rem; }
    [data-testid="stMain"] [class*="st-key-tilehdr_"] button,
    [data-testid="stMain"] [class*="st-key-tilehdr_"] button[kind="tertiary"],
    [data-testid="stMain"] [class*="st-key-tilehdr_"] button:hover,
    [data-testid="stMain"] [class*="st-key-tilehdr_"] button:focus,
    [data-testid="stMain"] [class*="st-key-tilehdr_"] button:focus-visible,
    [data-testid="stMain"] [class*="st-key-tilehdr_"] button:active {
        display: flex !important; flex-direction: row !important; justify-content: flex-start !important;
        align-items: center !important; width: 100% !important; min-height: 0 !important; text-align: left !important;
        padding: 11px 18px !important; border-radius: 12px !important; cursor: pointer;
        background: #FFFFFF !important; border: none !important; box-shadow: none !important; outline: none !important;
        color: #0F172A !important; font-weight: 700 !important; font-size: 0.95rem !important;
        transform: none !important; transition: none !important;
    }
    /* Streamlit nests the label as  button > div > span > (icon-wrapper span, markdown div).
       Make every wrapper a full-width row, put the title first and the chevron last. */
    [data-testid="stMain"] [class*="st-key-tilehdr_"] button > div,
    [data-testid="stMain"] [class*="st-key-tilehdr_"] button > div > span {
        display: flex !important; align-items: center !important; width: 100% !important;
        justify-content: flex-start !important; text-align: left !important; margin: 0 !important; gap: 0 !important;
    }
    [data-testid="stMain"] [class*="st-key-tilehdr_"] button [data-testid="stMarkdownContainer"] {
        order: 1 !important; flex: 1 1 auto !important; width: auto !important; text-align: left !important; margin: 0 !important;
    }
    [data-testid="stMain"] [class*="st-key-tilehdr_"] button p,
    [data-testid="stMain"] [class*="st-key-tilehdr_"] button:hover p {
        font-size: 0.95rem !important; color: #0F172A !important; text-align: left !important; margin: 0 !important;
        letter-spacing: -0.01em; width: 100%;
    }
    [data-testid="stMain"] [class*="st-key-tilehdr_"] button > div > span > span {
        order: 2 !important; margin: 0 0 0 auto !important;
    }
    [data-testid="stMain"] [class*="st-key-tilehdr_"] button span[data-testid="stIconMaterial"],
    [data-testid="stMain"] [class*="st-key-tilehdr_"] button:hover span[data-testid="stIconMaterial"] {
        color: #94A3B8 !important; font-size: 1.35rem !important;
    }
    /* Metrics: values sized to fit four across; labels may wrap instead of truncating */
    [data-testid="stMetricValue"], [data-testid="stMetricValue"] > div {
        font-size: clamp(0.95rem, 1.3vw, 1.35rem) !important; overflow: visible !important; text-overflow: clip !important;
    }
    [data-testid="stMetricLabel"] p { white-space: normal !important; overflow: visible !important; text-overflow: clip !important; line-height: 1.25; }
    /* Native expanders (used inside forms) share the same tile look */
    div[data-testid="stExpander"] details {
        border: 1px solid #E2E8F0; border-radius: 12px; background: #FFFFFF;
        box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
    }
    div[data-testid="stExpander"] summary { padding: 13px 16px; }
    div[data-testid="stExpander"] summary:hover { background: #F8FAFC; border-radius: 12px; }
    div[data-testid="stExpander"] summary p { font-size: 0.95rem; color: #0F172A; }
    </style>
    """, unsafe_allow_html=True)


# ── Top navigation bar ────────────────────────────────────────────────────────

_NAV_PRIMARY = [
    ("dashboard",         "home",          "Today",             "app.py"),
    ("opportunity_engine", "rocket_launch", "Opportunity Engine", "pages/9_🎯_Opportunity_Engine.py"),
    ("predictions",       "insights",      "Predictions",       "pages/7_🔮_Predictions.py"),
    ("portfolio",         "work",          "Portfolio",         "pages/5_💼_Portfolio.py"),
]
_NAV_SECONDARY = [
    ("validation",  "track_changes", "Validation",    "pages/6_🎯_Validation.py"),
    ("chat",        "smart_toy",     "Chat",          "pages/4_🤖_Chat.py"),
    ("valuation",   "calculate",     "Valuation",     "pages/10_📐_Valuation.py"),
]
# (key, icon, label, path, admin_only) — ordered by how often each is actually
# used, not alphabetically. admin_only entries are hidden from the menu
# entirely for non-admins, and color-coded (DANGER) for admins.
_NAV_ADMIN = [
    ("profile",       "person",          "Profile",             "pages/12_👤_Profile.py",     False),
    ("schedule",      "calendar_month",  "Schedule",            "pages/2_🗓️_Schedule.py",     False),
    ("accounts",      "account_balance", "Accounts & Imports",  "pages/13_🗂️_Accounts.py",    False),
    ("validation_qa", "science",         "Validation QA",       "pages/11_🔬_Validation_QA.py", False),
    ("database",      "database",        "Database",            "pages/1_📊_Database.py",      True),
    ("restricted",    "block",           "Restricted List",     "pages/8_🚫_Restricted_List.py", True),
]

def top_nav(active: str = "dashboard") -> None:
    """Render a sticky top nav: primary + secondary tabs on the left, and one
    merged account/settings icon in the top-right corner — who's signed in,
    the settings pages (usage-ordered; admin-only ones grouped, colored, and
    hidden entirely from non-admins), and sign out, all in one dropdown."""
    # column width scales with label length so longer labels (e.g. "Opportunity
    # Engine") don't get clipped next to short ones (e.g. "Chat") in a uniform grid
    def _w(label: str) -> float:
        return 0.9 + 0.12 * len(label)

    n_primary = len(_NAV_PRIMARY)
    widths = (
        [4.0]                  # brand
        + [_w(label) for _, _, label, _ in _NAV_PRIMARY] + [0.3]
        + [_w(label) for _, _, label, _ in _NAV_SECONDARY]
        + [0.8, 0.7]           # flexible spacer, then the settings icon pinned right
    )
    cols = st.columns(widths, gap="small", vertical_alignment="center")
    divider_1 = 1 + n_primary
    secondary_start = 2 + n_primary
    settings_col = cols[-1]

    with cols[0]:
        st.markdown(
            f'<div class="nav-brand"><span style="display:inline-flex;width:19px;height:19px;'
            f'vertical-align:middle">{brand_mark_svg()}</span> <span class="brand-text">APEX</span></div>',
            unsafe_allow_html=True,
        )

    for i, (key, icon, label, path) in enumerate(_NAV_PRIMARY):
        with cols[1 + i]:
            if key == active:
                st.markdown(
                    f'<div class="nav-tab active">{icon_html(icon, 17)} <span>{label}</span></div>',
                    unsafe_allow_html=True,
                )
            else:
                st.page_link(path, label=label, icon=material(icon), width="stretch")

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
                    f'{icon_html(icon, 16)} <span>{label}</span></div>',
                    unsafe_allow_html=True,
                )
            else:
                st.page_link(path, label=label, icon=material(icon), width="stretch")

    on_settings_page = any(key == active for key, *_ in _NAV_ADMIN)
    with settings_col, st.container(key="settings_menu_active" if on_settings_page else "settings_menu"):
        _account_menu(active)

    st.markdown('<div class="nav-underline"></div>', unsafe_allow_html=True)


def _account_menu(active: str) -> None:
    """Merged account + settings dropdown in the nav's top-right corner: who's
    signed in, the settings pages, and sign out — one popover instead of the
    separate settings-gear and account-circle ones this replaced."""
    from web.auth import current_user, sign_out

    user = current_user()

    with st.popover("", icon=material("account_circle"), help=user.email):
        st.caption(user.email)
        st.caption(f"Role: {user.role}")
        st.markdown(
            '<div style="border-top:1px solid #E5E7EB;margin:6px 0 8px"></div>',
            unsafe_allow_html=True,
        )
        for key, icon, label, path, admin_only in _NAV_ADMIN:
            if admin_only and not user.is_admin:
                continue
            color = DANGER if admin_only else None
            if key == active:
                style = f'style="color:{color}"' if color else ""
                st.markdown(
                    f'<div class="nav-menu-item active" {style}>'
                    f'{icon_html(icon, 17, color=color or "currentColor")} <span>{label}</span></div>',
                    unsafe_allow_html=True,
                )
            elif admin_only:
                with st.container(key=f"nav_admin_only_{key}"):
                    st.page_link(path, label=label, icon=material(icon), width="stretch")
            else:
                st.page_link(path, label=label, icon=material(icon), width="stretch")

        st.markdown(
            '<div style="border-top:1px solid #E5E7EB;margin:8px 0 8px"></div>',
            unsafe_allow_html=True,
        )
        if st.button("Sign out", key="auth_sign_out", width="stretch"):
            sign_out()


# ── Icon system (Material Symbols Rounded — same font Streamlit's own
#    `:material/name:` icon params render, so custom spans match native
#    widgets exactly). Use icon_html() inside raw HTML/markdown strings;
#    use the plain "name" string with Streamlit's own icon= kwargs
#    (st.button, st.page_link, st.info/warning/success/error, st.expander,
#    st.tabs) via f":material/{name}:". ───────────────────────────────────

def icon_html(name: str, size: int = 18, color: str = "currentColor", extra_style: str = "") -> str:
    """Inline Material Symbols glyph for embedding inside raw HTML/markdown."""
    return (
        f'<span class="material-symbols-rounded" '
        f'style="font-size:{size}px;color:{color};{extra_style}">{name}</span>'
    )


def material(name: str) -> str:
    """Format an icon name for Streamlit's native icon= kwargs, e.g. st.button(icon=material("refresh"))."""
    return f":material/{name}:"


@st.cache_data(show_spinner=False)
def brand_mark_svg() -> str:
    """Inline contents of the APEX brand mark (web/static/apex_mark.svg) — four
    analyst dots converging into one solid apex point. Hardcoded fill colors
    (not currentColor), since the same file also serves as the standalone
    favicon; embed this string directly (unsafe_allow_html) rather than an
    <img> tag so it sizes via a wrapping span like the other nav icons."""
    return (_STATIC_DIR / "apex_mark.svg").read_text()


def status_dot_html(color: str, size: int = 8) -> str:
    """A small solid dot for status/quality indicators — no emoji circles."""
    return (
        f'<span style="display:inline-block;width:{size}px;height:{size}px;'
        f'border-radius:50%;background:{color};flex-shrink:0"></span>'
    )


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
    """`icon` is a Material Symbols icon name (e.g. "work"), not an emoji."""
    icon_span = icon_html(icon, 22, color="#111827", extra_style="margin-right:10px;opacity:0.85") if icon else ""
    sub_html  = (
        f'<p style="margin:5px 0 0;color:#6B7280;font-size:0.9rem;font-weight:400">{subtitle}</p>'
        if subtitle else ""
    )
    st.markdown(
        f'<div style="margin-bottom:28px;padding-bottom:18px;border-bottom:1px solid #F3F4F6">'
        f'<h1 style="margin:0;color:#111827;font-size:1.65rem;font-weight:700;'
        f'letter-spacing:-0.03em;display:flex;align-items:center">{icon_span}{title}</h1>'
        f'{sub_html}</div>',
        unsafe_allow_html=True,
    )


class _Tile:
    """Handle returned by section_tile(): truthy when open; `with tile:` renders into its body."""

    def __init__(self, is_open: bool, body):
        self.open = is_open
        self._body = body

    def __bool__(self) -> bool:
        return self.open

    def __enter__(self):
        if self._body is None:
            raise RuntimeError("section_tile is collapsed — guard the body with `if tile:`")
        self._body.__enter__()
        return self

    def __exit__(self, *exc):
        return self._body.__exit__(*exc)


def _tile_slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48]


def section_tile(text: str, badge_text: str = "", badge_color: str = PRIMARY, expanded: bool = False,
                 icon: str | None = None, key: str | None = None, native: bool = False):
    """
    Collapsible section tile. Same vocabulary as section_title() (title + badge),
    but the section body only renders while the tile is open, so collapsed
    sections cost nothing. Use as

        tile = section_tile("Holdings", badge_text=scope, expanded=True, key="portfolio_holdings")
        if tile:
            with tile:
                ...

    Open/closed state lives in st.session_state under the key, so it survives
    reruns. native=True returns a styled st.expander instead — for bodies inside
    an st.form, where the header button is not allowed (expanders cannot nest).
    """
    plain = re.sub(r"<[^>]+>", "", str(text)).strip()
    badge = re.sub(r"<[^>]+>", "", str(badge_text)).strip()
    if native:
        return st.expander(f"**{plain}**" + (f"  ·  {badge}" if badge else ""), expanded=expanded,
                           icon=material(icon) if icon else None)
    slug = key or _tile_slug(plain)
    state_key = f"tile_open_{slug}"
    if state_key not in st.session_state:
        st.session_state[state_key] = bool(expanded)
    is_open = bool(st.session_state[state_key])
    outer = st.container(border=True, key=f"tilebox_{slug}")   # class st-key-tilebox_* is what the CSS targets
    with outer:
        label = f"**{plain}**" + (f"  ·  {badge}" if badge else "")
        if st.button(label, key=f"tilehdr_{slug}", type="tertiary", width="stretch",
                     icon=material("keyboard_arrow_down" if is_open else "keyboard_arrow_right")):
            st.session_state[state_key] = not is_open
            st.rerun()
        body = st.container() if is_open else None
    return _Tile(is_open, body)


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
                   delta: str = "", delta_positive: bool | None = None) -> str:
    """`icon` is a Material Symbols icon name (e.g. "work"), not an emoji.
    `delta_positive` picks the up/down glyph and gain/loss color; omit for a neutral delta."""
    if delta:
        if delta_positive is None:
            d_color, d_arrow = NEUTRAL, "•"
        else:
            d_color, d_arrow = (SUCCESS, "▲") if delta_positive else (DANGER, "▼")
        delta_html = (
            f'<p style="margin:6px 0 0;font-size:0.75rem;font-weight:600;color:{d_color};'
            f'display:flex;align-items:center;gap:3px">{d_arrow} {delta}</p>'
        )
    else:
        delta_html = ""
    icon_span = icon_html(icon, 18, color=color) if icon else ""
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
        f'display:flex;align-items:center;justify-content:center;'
        f'flex-shrink:0">{icon_span}</div></div></div>'
    )
