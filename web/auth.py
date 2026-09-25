"""
Web login gate — Entra ID sign-in plus the auth_users allow-list.

require_login() must run at the top of every page, right after
st.set_page_config(), before any portfolio_agent data access:

    st.set_page_config(...)
    require_login()
    top_nav("...")

top_nav() itself renders the merged account/settings/sign-out dropdown
(web.styles._account_menu) — nothing else needs to call back into this
module for that.

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
from portfolio_agent.tools.access_requests_db import submit_request
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


def _preview_card(title: str, body_html: str) -> str:
    return (
        '<div style="border:1px solid #E5E7EB;border-radius:14px;background:#FFFFFF;'
        'box-shadow:0 8px 24px rgba(17,24,39,0.06);overflow:hidden;height:100%">'
        '<div style="height:34px;display:flex;align-items:center;gap:6px;padding:0 12px;'
        'background:#F3F4F6;border-bottom:1px solid #E5E7EB">'
        '<span style="width:8px;height:8px;border-radius:50%;background:#D1D5DB"></span>'
        '<span style="width:8px;height:8px;border-radius:50%;background:#D1D5DB"></span>'
        '<span style="width:8px;height:8px;border-radius:50%;background:#D1D5DB"></span>'
        '</div>'
        f'<div style="padding:18px;display:flex;flex-direction:column;gap:12px">'
        f'<div style="font-size:13px;font-weight:700;color:#111827">{title}</div>'
        f'{body_html}</div></div>'
    )


def _render_request_access() -> None:
    from web.styles import material

    with st.expander("First time user? Register", icon=material("person_add")):
        with st.form("landing_access_request", clear_on_submit=True):
            name = st.text_input("Name")
            email = st.text_input("Email")
            message = st.text_area("What would you like access for? (optional)", height=80)
            if st.form_submit_button("Request access", type="primary"):
                if not name.strip() or not email.strip():
                    st.error("Name and email are required.")
                else:
                    submit_request(name, email, message)
                    st.success("Request sent — an admin will follow up once it's reviewed.")


def _render_login_screen() -> None:
    """
    The pre-login landing page — brand, pitch, stylized (non-live) product
    previews, and a request-access form. Deliberately describes what APEX
    does in terms of the signals it looks at (market data, valuation, macro,
    news, risk limits) — never the internal multi-role/agent architecture
    that produces them.
    """
    from web.styles import PRIMARY, WARNING, brand_mark_svg, icon_html, material

    st.markdown(
        '<style>'
        'body{background:#F9FAFB}'
        '.apx-badge{display:inline-flex;align-items:center;gap:8px;padding:6px 14px;'
        f'background:#EFF6FF;border-radius:999px;font-size:13px;font-weight:600;color:{PRIMARY}}}'
        '</style>',
        unsafe_allow_html=True,
    )

    # ── Top bar — brand only; the one Sign in CTA lives in the hero ────────
    st.markdown(
        f'<div style="display:flex;align-items:center;gap:10px;padding-top:6px">'
        f'<span style="display:inline-flex;width:26px;height:26px">{brand_mark_svg()}</span>'
        f'<span style="font-size:20px;font-weight:800;letter-spacing:-0.01em;color:{PRIMARY}">APEX</span>'
        f'</div>',
        unsafe_allow_html=True,
    )
    st.markdown('<div style="border-bottom:1px solid #E5E7EB;margin:14px 0 36px"></div>', unsafe_allow_html=True)

    # ── Hero ───────────────────────────────────────────────────────────────
    _, hero_col, _ = st.columns([1, 5, 1])
    with hero_col:
        st.markdown(
            '<div style="text-align:center">'
            '<span class="apx-badge">One conviction score, built from many signals</span>'
            '</div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            '<h1 style="text-align:center;margin:22px 0 0;font-size:44px;line-height:1.1;'
            'font-weight:800;letter-spacing:-0.02em;color:#111827">'
            'Portfolio intelligence that reasons<br>deeper than one model.</h1>',
            unsafe_allow_html=True,
        )
        st.markdown(
            '<p style="text-align:center;margin:18px auto 0;font-size:17px;line-height:1.6;'
            'color:#4B5563;max-width:640px">'
            'APEX scores every position across market data, valuation, macro conditions, and news '
            '&mdash; then blends them into one composite call per time horizon, checks it against '
            'your risk limits, and clears it against your restricted list before it ever reaches you.'
            '</p>',
            unsafe_allow_html=True,
        )
        st.markdown('<div style="height:22px"></div>', unsafe_allow_html=True)
        _, cta_col, _ = st.columns([1, 2, 1])
        with cta_col:
            if st.button(
                "Sign in to view your portfolio", type="primary",
                key="landing_signin_hero", width="stretch",
            ):
                st.login("entra")
        st.markdown(
            '<p style="text-align:center;margin:10px 0 0;font-size:13px;color:#9CA3AF">'
            'Private workspace — access is by invitation only</p>',
            unsafe_allow_html=True,
        )
        st.markdown('<div style="height:14px"></div>', unsafe_allow_html=True)
        _render_request_access()

    st.markdown('<div style="height:48px"></div>', unsafe_allow_html=True)

    # ── Product previews — stylized, not live data. Dashboard, Portfolio,
    #    Predictions, Opportunities, in that order. ─────────────────────────
    row1a, row1b = st.columns(2, gap="medium")
    with row1a:
        st.markdown(
            _preview_card(
                "Today's Priorities",
                '<div style="display:flex;align-items:center;justify-content:space-between;'
                'padding:10px 12px;background:#FEF2F2;border-radius:8px">'
                '<span style="font-size:12px;font-weight:600;color:#991B1B">NVDA — sector concentration &gt;25%</span>'
                '<span style="font-size:11px;font-weight:700;color:#B91C1C">TRIM</span></div>'
                '<div style="display:flex;align-items:center;justify-content:space-between;'
                'padding:10px 12px;background:#ECFDF5;border-radius:8px">'
                '<span style="font-size:12px;font-weight:600;color:#065F46">MSFT — high conviction</span>'
                '<span style="font-size:11px;font-weight:700;color:#047857">BUY</span></div>'
                '<div style="display:flex;align-items:center;justify-content:space-between;'
                'padding:10px 12px;background:#F9FAFB;border-radius:8px">'
                '<span style="font-size:12px;font-weight:600;color:#374151">New cash available</span>'
                f'<span style="font-size:11px;font-weight:700;color:{PRIMARY}">$4,200</span></div>',
            ),
            unsafe_allow_html=True,
        )
    with row1b:
        st.markdown(
            _preview_card(
                "Pre-Trade Clearance",
                '<div style="display:flex;align-items:center;justify-content:space-between;'
                'padding:10px 12px;border:1px solid #E5E7EB;border-radius:8px">'
                '<span style="font-size:12px;font-weight:600;color:#374151">TSLA — restricted list check</span>'
                '<span style="display:inline-flex;align-items:center;gap:5px;font-size:11px;'
                'font-weight:700;color:#047857"><span style="width:6px;height:6px;border-radius:50%;'
                'background:#10B981"></span>CLEARED</span></div>'
                '<div style="font-size:11px;font-weight:700;color:#9CA3AF;text-transform:uppercase;'
                'letter-spacing:0.04em;margin-top:4px">Sector concentration</div>'
                '<div style="height:8px;border-radius:999px;background:#F3F4F6;overflow:hidden">'
                f'<div style="width:62%;height:100%;background:{PRIMARY}"></div></div>'
                '<div style="font-size:11px;color:#6B7280">Technology 62% of portfolio — 12pt below your limit</div>',
            ),
            unsafe_allow_html=True,
        )

    st.markdown('<div style="height:20px"></div>', unsafe_allow_html=True)
    row2a, row2b = st.columns(2, gap="medium")
    with row2a:
        st.markdown(
            _preview_card(
                "Predictions — AAPL",
                '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;font-size:10px;'
                'font-weight:700;color:#9CA3AF;text-transform:uppercase;letter-spacing:0.04em">'
                '<span>5D</span><span>21D</span><span>63D</span><span>250D</span></div>'
                '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px">'
                '<div style="text-align:center;padding:10px 0;background:#F3F4F6;border-radius:8px;'
                'font-size:12px;font-weight:700;color:#6B7280">HOLD</div>'
                '<div style="text-align:center;padding:10px 0;background:#ECFDF5;border-radius:8px;'
                'font-size:12px;font-weight:700;color:#047857">BUY</div>'
                '<div style="text-align:center;padding:10px 0;background:#ECFDF5;border-radius:8px;'
                'font-size:12px;font-weight:700;color:#047857">BUY</div>'
                f'<div style="text-align:center;padding:10px 0;background:#EFF6FF;border-radius:8px;'
                f'font-size:12px;font-weight:700;color:{PRIMARY}">STRONG BUY</div></div>'
                '<div style="font-size:11px;line-height:1.5;color:#6B7280">One blended score per '
                'horizon — confidence rises when independent signals agree, not from one static weight.</div>',
            ),
            unsafe_allow_html=True,
        )
    with row2b:
        st.markdown(
            _preview_card(
                "Today's Opportunities",
                '<div style="display:flex;flex-direction:column;gap:8px">'
                '<div style="display:flex;align-items:center;gap:10px;padding:10px 12px;'
                'background:#F9FAFB;border-radius:8px">'
                '<span style="font-size:12px;font-weight:800;color:#94A3B8">#1</span>'
                '<span style="font-size:12px;font-weight:700;color:#111827;flex:1">AAPL</span>'
                '<span style="font-size:11px;font-weight:700;color:#047857;background:#ECFDF5;'
                'padding:3px 8px;border-radius:6px">BUY</span></div>'
                '<div style="display:flex;align-items:center;gap:10px;padding:10px 12px;'
                'background:#F9FAFB;border-radius:8px">'
                '<span style="font-size:12px;font-weight:800;color:#94A3B8">#2</span>'
                '<span style="font-size:12px;font-weight:700;color:#111827;flex:1">XOM</span>'
                f'<span style="font-size:11px;font-weight:700;color:{WARNING};background:#FFFBEB;'
                'padding:3px 8px;border-radius:6px">WATCH</span></div></div>'
                '<div style="font-size:11px;line-height:1.5;color:#6B7280">Ranked against your actual '
                'holdings — correlation and concentration fit, not just what\'s trending today.</div>',
            ),
            unsafe_allow_html=True,
        )

    st.markdown('<div style="height:56px"></div>', unsafe_allow_html=True)

    # ── Feature strip — what it weighs, never how it's built internally ───
    feat_cols = st.columns(4, gap="medium")
    _FEATURES = [
        ("insights", "Multi-signal scoring",
         "Market data, valuation, macro conditions, and news are each weighed "
         "independently, then blended into one call."),
        ("trending_up", "Horizon-aware weighting",
         "Signal weights shift across 5, 21, 63, and 250-day horizons instead "
         "of one static blend."),
        ("bar_chart", "Risk-aware sizing",
         "Allocations respect your sector, issuer, and position-size limits "
         "automatically."),
        ("shield", "Pre-trade clearance",
         "Every recommendation is checked against your restricted list before "
         "it reaches you."),
    ]
    for col, (icon, title, desc) in zip(feat_cols, _FEATURES):
        with col:
            st.markdown(
                f'<div style="display:flex;flex-direction:column;gap:10px">'
                f'{icon_html(icon, 24, color=PRIMARY)}'
                f'<div style="font-size:14px;font-weight:700;color:#111827">{title}</div>'
                f'<div style="font-size:13px;line-height:1.5;color:#6B7280">{desc}</div></div>',
                unsafe_allow_html=True,
            )

    st.markdown('<div style="height:12px;border-top:1px solid #E5E7EB;margin-top:44px"></div>', unsafe_allow_html=True)

    # ── Footer ───────────────────────────────────────────────────────────────
    st.markdown('<div style="height:8px"></div>', unsafe_allow_html=True)
    st.markdown(
        '<div style="text-align:center">'
        '<span style="font-size:12px;color:#9CA3AF">APEX — internal use only. Not investment advice.</span>'
        '</div>',
        unsafe_allow_html=True,
    )


def sign_out() -> None:
    """Clear our own cached session state and end the Streamlit auth session."""
    st.session_state.pop("auth_user", None)
    st.logout()


def _logout_button(label: str = "Sign out") -> None:
    if st.button(label, key=f"auth_logout_{label}"):
        sign_out()


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


