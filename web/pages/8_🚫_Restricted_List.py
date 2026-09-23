"""Restricted List Manager — compliance blocklist + pipeline phase-skip list."""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from web.styles import inject_global_css, page_header, section_title, top_nav, material, DANGER, NEUTRAL, section_tile
from portfolio_agent.tools.restricted_list_db import (
    list_restricted, add_restricted, remove_restricted,
    list_pipeline_skip, add_pipeline_skip, remove_pipeline_skip,
)

st.set_page_config(
    page_title="Restricted List — Portfolio Intelligence",
    page_icon=material("block"),
    layout="wide",
    initial_sidebar_state="expanded",
)
inject_global_css()
top_nav("restricted")

with st.sidebar:
    st.markdown('<p style="font-size:0.7rem;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;color:#475569;margin:0 0 10px">Restricted List</p>', unsafe_allow_html=True)

page_header("Restricted List Manager", icon="block")

# ── Compliance restricted tickers ─────────────────────────────────────────────

restricted = list_restricted()
_tile_1 = section_tile("Restricted Tickers", badge_text=f"{len(restricted)} blocked", badge_color=DANGER, expanded=True, key="restricted_1")
if _tile_1:
    with _tile_1:
        st.caption("Tickers here are blocked from new recommendations (checked by the clearance agent before every trade call).")

        if restricted:
            for entry in restricted:
                c1, c2, c3 = st.columns([1.5, 6, 1])
                c1.markdown(f"**{entry['ticker']}**")
                c2.write(entry.get("reason") or "—")
                if c3.button("", icon=material("delete"), key=f"rm_restricted_{entry['ticker']}", help=f"Remove {entry['ticker']}"):
                    remove_restricted(entry["ticker"])
                    st.success(f"Removed {entry['ticker']} from restricted list.", icon=material("check_circle"))
                    st.rerun()
        else:
            st.info("No tickers currently restricted.", icon=material("check_circle"))

        with st.form("add_restricted_form", clear_on_submit=True):
            fc1, fc2, fc3 = st.columns([1.5, 6, 1])
            new_ticker = fc1.text_input("Ticker", placeholder="XYZ", label_visibility="collapsed")
            new_reason = fc2.text_input("Reason", placeholder="Reason for restriction…", label_visibility="collapsed")
            submitted = fc3.form_submit_button("Add", icon=material("add"))
            if submitted:
                if not new_ticker.strip():
                    st.warning("Enter a ticker.")
                else:
                    add_restricted(new_ticker.strip().upper(), new_reason.strip() or None)
                    st.success(f"Added {new_ticker.strip().upper()} to restricted list.", icon=material("check_circle"))
                    st.rerun()


        # ── Pipeline phase-skip list ───────────────────────────────────────────────────

        skip = list_pipeline_skip()
_tile_2 = section_tile("Pipeline Skip", badge_text=f"{len(skip)} tickers", badge_color=NEUTRAL, expanded=False, key="restricted_2")
if _tile_2:
    with _tile_2:
        st.caption(
            "Tickers here are silently skipped in the listed pipeline phases (e.g. ETFs with no "
            "fundamentals data, or delisted tickers). `all` skips every phase — treated as complete for the day."
        )

        _PHASE_OPTIONS = ["news", "research", "fundamentals", "all"]

        if skip:
            for ticker in sorted(skip):
                cfg = skip[ticker]
                c1, c2, c3, c4 = st.columns([1.5, 2.5, 4, 1])
                c1.markdown(f"**{ticker}**")
                c2.write(", ".join(cfg.get("phases", [])) or "—")
                c3.write(cfg.get("reason") or "—")
                if c4.button("", icon=material("delete"), key=f"rm_skip_{ticker}", help=f"Remove {ticker}"):
                    remove_pipeline_skip(ticker)
                    st.success(f"Removed {ticker} from pipeline skip list.", icon=material("check_circle"))
                    st.rerun()
        else:
            st.info("No tickers currently skipped in the pipeline.", icon=material("check_circle"))

        with st.form("add_skip_form", clear_on_submit=True):
            fc1, fc2, fc3, fc4 = st.columns([1.5, 2.5, 4, 1])
            skip_ticker = fc1.text_input("Ticker", placeholder="IVV", label_visibility="collapsed", key="skip_ticker")
            skip_phases = fc2.multiselect("Phases", _PHASE_OPTIONS, label_visibility="collapsed", key="skip_phases")
            skip_reason = fc3.text_input("Reason", placeholder="Reason for skipping…", label_visibility="collapsed", key="skip_reason")
            skip_submitted = fc4.form_submit_button("Add", icon=material("add"))
            if skip_submitted:
                if not skip_ticker.strip():
                    st.warning("Enter a ticker.")
                elif not skip_phases:
                    st.warning("Select at least one phase.")
                else:
                    add_pipeline_skip(skip_ticker.strip().upper(), skip_phases, skip_reason.strip() or None)
                    st.success(f"Added {skip_ticker.strip().upper()} to pipeline skip list.", icon=material("check_circle"))
                    st.rerun()