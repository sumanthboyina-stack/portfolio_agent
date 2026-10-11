"""
Portfolio Health — a checkup of your own portfolio against your own profile and limits: allocation, concentration (looking
through funds), diversification, emergency reserve, cash, fees, leveraged / inverse funds, restricted list and data quality.
Each check is GOOD / WATCH / ACTION / UNKNOWN; there is no single score. Everything is computed in
portfolio_agent.tools.portfolio_health; this file only lays it out.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from web.auth import current_context, require_login
from web.components.portfolio_health import render_health_page
from web.styles import inject_global_css, page_header, top_nav

st.set_page_config(
    page_title="APEX — Portfolio Health",
    page_icon=str(_ROOT / "web" / "static" / "apex_mark.png"),
    layout="wide",
    initial_sidebar_state="expanded",
)
inject_global_css()
require_login()
top_nav("health")

ctx = current_context("web:health")

page_header("Portfolio Health", subtitle="How your portfolio sits against your own profile and limits", icon="monitor_heart")
render_health_page(ctx)
