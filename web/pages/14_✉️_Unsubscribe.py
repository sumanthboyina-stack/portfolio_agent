"""
Unsubscribe — the landing page of the link at the bottom of every APEX email. It needs NO login: the signed token in the link
is the proof of who may stop which address (see services.notification_senders.sign_unsubscribe_token), and it can stop emails
to that one address and nothing else.

It asks for ONE click to confirm instead of acting the moment the page loads: mail scanners and link previewers fetch links
automatically, and must not be able to opt someone out by accident. (A true RFC 8058 one-click POST is not something Streamlit
can serve; this page is the closest, and the List-Unsubscribe header carries the same link.)

What it says never depends on why a link was bad, and it shows no address, name or anything else about the person.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from portfolio_agent.services.notification_service import unsubscribe_with_token
from web.styles import inject_global_css, material, page_header

st.set_page_config(
    page_title="APEX — Unsubscribe",
    page_icon=str(_ROOT / "web" / "static" / "apex_mark.png"),
    layout="centered",
    initial_sidebar_state="collapsed",
)
inject_global_css()

page_header("Stop these emails", subtitle="You can turn them back on any time in your APEX profile", icon="mail")

token = st.query_params.get("token", "")
if isinstance(token, list):
    token = token[0] if token else ""

if not token:
    st.error("This link isn't valid.", icon=material("error"))
elif st.session_state.get("unsub_done") == token:
    st.success("Done — you won't get APEX emails at that address any more. You can opt back in any time from your APEX profile "
               "(Profile → Notifications).", icon=material("check_circle"))
else:
    st.write("Stop APEX from sending emails to the address this link was sent to? Your other contacts, and your APEX account, "
             "are not affected.")
    if st.button("Yes, stop these emails", type="primary", key="unsub_confirm"):
        if unsubscribe_with_token(token).get("ok"):
            st.session_state["unsub_done"] = token
            st.rerun()
        else:
            st.error("This link isn't valid, or it has expired.", icon=material("error"))
