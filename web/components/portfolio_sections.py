"""
Sections of the Portfolio page. Overview (the old My Wealth page), Holdings, Watchlist,
Accounts & Imports (the old Accounts page) and Health (the old Portfolio Health page) share one
screen; only the selected section is rendered, so a rerun never pays for the other four.
"""

from __future__ import annotations

import streamlit as st

SECTION_KEY = "portfolio_section"            # the segmented control's own state
_REQUEST_KEY = "portfolio_section_request"   # "open the page on this section", consumed before the control is drawn
DEFAULT_SECTION = "holdings"

SECTIONS: dict[str, str] = {
    "overview":  "Overview",
    "holdings":  "Holdings",
    "watchlist": "Watchlist",
    "accounts":  "Accounts & Imports",
    "health":    "Health",
}


def request_section(section: str) -> None:
    """Make the Portfolio page open on `section` the next time it renders (from this or any other page)."""
    if section in SECTIONS:
        st.session_state[_REQUEST_KEY] = section


def go_to_section_button(label: str, section: str, *, key: str, icon: str | None = None, width: str = "content") -> None:
    """A button that switches the Portfolio page to another section (the old cross-page links)."""
    if st.button(label, key=key, icon=icon, width=width):
        request_section(section)
        st.rerun()


def pick_section() -> str:
    """Draw the section selector (call once, at the top of the Portfolio page) and return the chosen key."""
    if (wanted := st.session_state.pop(_REQUEST_KEY, None)) in SECTIONS:
        st.session_state[SECTION_KEY] = wanted
    if st.session_state.get(SECTION_KEY) not in SECTIONS:
        st.session_state[SECTION_KEY] = DEFAULT_SECTION
    return st.segmented_control(
        "Portfolio section", list(SECTIONS), format_func=SECTIONS.get, key=SECTION_KEY,
        required=True, label_visibility="collapsed",
    ) or DEFAULT_SECTION
