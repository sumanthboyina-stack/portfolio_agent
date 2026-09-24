"""
Web login gate — Entra ID sign-in plus the auth_users allow-list.

require_login() must run at the top of every page, right after
st.set_page_config(), before any portfolio_agent data access:

    st.set_page_config(...)
    require_login()
    top_nav("...")
    render_account_menu()

st.login("entra") / st.user / st.logout() are Streamlit's own OIDC client —
it runs the authorization-code flow, checks state/nonce, verifies the id
token's signature, and manages the session cookie. Nothing here reimplements
any of that; this module only (1) makes sure a login happened at all, and
(2) checks the resulting identity against the auth_users allow-list.

Nothing in this file, or anything it calls, logs a token, a client secret,
or the full st.user mapping — only the owner id and a short hash of the
OIDC subject ever reach a log line.
"""

from __future__ import annotations

import hashlib
import time

import streamlit as st

from portfolio_agent.domain import AuthUser
from portfolio_agent.services.context import (
    RequestContext,
    identity_from_verified_login,
    web_context,
)
from portfolio_agent.tools.auth_users_db import resolve_login

_RECHECK_SECONDS = 300
_WELL_KNOWN_SUFFIX = "/.well-known/openid-configuration"


def _hash_subject(subject: str) -> str:
    return hashlib.sha256(subject.encode()).hexdigest()[:12]


@st.cache_resource
def _expected_issuer() -> str:
    """
    Derive the issuer Entra ID will put in `iss` from the configured
    server_metadata_url — for both a workforce tenant (login.microsoftonline.
    com/<tenant>/v2.0/...) and a CIAM external tenant (<sub>.ciamlogin.com/
    <tenant>/v2.0/...), Entra's metadata URL is always `<issuer>` + the
    well-known suffix, so stripping that suffix is exactly the issuer for
    either shape. Don't hardcode one tenant format here.
    """
    metadata_url = st.secrets["auth"]["entra"]["server_metadata_url"]
    if not metadata_url.endswith(_WELL_KNOWN_SUFFIX):
        raise RuntimeError(
            "auth.entra.server_metadata_url doesn't end with "
            f"{_WELL_KNOWN_SUFFIX!r} — can't derive the expected issuer from it."
        )
    return metadata_url[: -len(_WELL_KNOWN_SUFFIX)]


def _render_login_screen() -> None:
    st.title("Portfolio Intelligence")
    st.write("Sign in with your invited account to continue.")
    if st.button("Sign in", type="primary"):
        st.login("entra")


def _logout_button(label: str = "Sign out") -> None:
    if st.button(label, key=f"auth_logout_{label}"):
        st.logout()


def require_login() -> None:
    """Gate the current page. Renders a login/error screen and st.stop()s if
    the visitor isn't a signed-in, invited, enabled user."""
    if not st.user.is_logged_in:
        _render_login_screen()
        st.stop()

    iss = st.user.get("iss")
    sub = st.user.get("sub")
    email = st.user.get("email")
    if not iss or not sub:
        st.error("Sign-in didn't return the expected identity claims.")
        _logout_button()
        st.stop()

    if iss != _expected_issuer():
        st.error("Sign-in came from an unexpected identity provider.")
        _logout_button()
        st.stop()

    cached = st.session_state.get("auth_user")
    now = time.time()
    if (
        cached is not None
        and cached["sub"] == sub
        and now - cached["checked_at"] < _RECHECK_SECONDS
    ):
        if not cached["user"].enabled:
            st.error("Your access has been disabled.")
            _logout_button()
            st.stop()
        return

    auth_user = resolve_login(iss, sub, email)
    if auth_user is None:
        # Same message whether the email was never invited, is disabled, or
        # is already bound elsewhere — never reveal which.
        st.error("Your account isn't invited. Ask the admin.")
        _logout_button()
        st.stop()

    st.session_state["auth_user"] = {"sub": sub, "user": auth_user, "checked_at": now}


def current_user() -> AuthUser:
    """The signed-in, allow-listed user for this session. Call only after
    require_login() has run (and not st.stop()'d) on this page."""
    cached = st.session_state.get("auth_user")
    if cached is None:
        raise RuntimeError("current_user() called before require_login() succeeded")
    return cached["user"]


def current_context(source: str) -> RequestContext:
    """The authorized RequestContext for this request — see services.context.web_context."""
    identity = identity_from_verified_login(current_user().owner)
    return web_context(identity, source)


def render_account_menu() -> None:
    """Signed-in-as + sign-out control. This app has no st.sidebar (nav is
    the top bar in web/styles.py), so this renders as a small popover next
    to it — call right after top_nav() on every page."""
    from web.styles import material

    user = current_user()
    _, menu_col = st.columns([20, 1])
    with menu_col:
        with st.popover("", icon=material("account_circle"), help=user.email):
            st.caption(user.email)
            st.caption(f"Role: {user.role}")
            if st.button("Sign out", key="auth_sign_out", width="stretch"):
                st.logout()
