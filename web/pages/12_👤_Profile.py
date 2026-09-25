"""
Profile — edit the single user profile that drives the mandate limits used by
the portfolio optimizer, the compliance gates in the clearance agent, the
personal notification filter, and which prediction horizons are generated.

All values are DB-backed (portfolio_agent.tools.user_profile_db and
portfolio_agent.tools.user_notifications_db) and edited through one form.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from web.styles import (
    section_tile,
    inject_global_css, page_header, section_title, top_nav, material, card, badge_html,
    PRIMARY, SUCCESS, WARNING, NEUTRAL, SUCCESS_LIGHT, WARNING_LIGHT,
)
from portfolio_agent.domain import ALL_HORIZON_LABELS, NOTIFICATION_CHANNELS
from portfolio_agent.tools.user_profile_db import get_user_profile, update_user_profile
from portfolio_agent.tools.user_notifications_db import (
    get_user_notifications, update_user_notifications,
)

from web.auth import current_context, current_user, require_login

st.set_page_config(
    page_title="APEX — Profile",
    page_icon=str(_ROOT / "web" / "static" / "apex_mark.png"),
    layout="wide",
    initial_sidebar_state="expanded",
)
inject_global_css()
require_login()
top_nav("profile")

with st.sidebar:
    st.markdown('<p style="font-size:0.7rem;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;color:#475569;margin:0 0 10px">Profile</p>', unsafe_allow_html=True)

page_header(
    "Profile",
    subtitle="Identity, mandate limits, compliance, notifications and horizons",
    icon="person",
)

# Same flash-after-rerun pattern the Portfolio page uses (holdings_flash).
if flash := st.session_state.pop("profile_flash", None):
    st.success(flash, icon=material("check_circle"))

profile = get_user_profile()
notif = get_user_notifications()

# ── Helpers ───────────────────────────────────────────────────────────────────

_GICS_SECTORS = [
    "Communication Services", "Consumer Discretionary", "Consumer Staples", "Energy",
    "Financials", "Health Care", "Industrials", "Information Technology", "Materials",
    "Real Estate", "Utilities",
]
_HORIZON_HELP = {
    "5d": "5 trading days", "21d": "~1 month", "63d": "~1 quarter", "250d": "~1 year",
}


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _parse_time(raw: str | None) -> time | None:
    if not raw:
        return None
    try:
        hh, mm = str(raw).split(":")[:2]
        return time(int(hh), int(mm))
    except ValueError:
        return None


def _fmt_ts(raw: str | None) -> str:
    if not raw:
        return "—"
    try:
        return datetime.fromisoformat(raw).astimezone(ZoneInfo(profile.timezone)).strftime("%b %d, %Y %H:%M %Z")
    except Exception:
        return str(raw)


def _validate(v: dict) -> list[str]:
    """Return a list of human-readable problems; empty means safe to write."""
    errors: list[str] = []
    for label, key in (("Max sector %", "max_sector_pct"), ("Max issuer %", "max_issuer_pct")):
        if not (0.0 <= float(v[key]) <= 100.0):
            errors.append(f"{label} must be between 0 and 100 (got {v[key]}).")
    for label, key in (
        ("Max per-candidate share of new cash", "max_per_candidate_pct_of_cash"),
        ("Max post-trade position", "max_post_trade_position_pct"),
    ):
        if not (0.0 <= float(v[key]) <= 1.0):
            errors.append(f"{label} must be a fraction between 0 and 1 (got {v[key]}).")
    cur = (v["base_currency"] or "").strip().upper()
    if len(cur) != 3 or not cur.isalpha():
        errors.append("Base currency must be a 3-letter ISO code (e.g. USD).")
    try:
        ZoneInfo(v["timezone"].strip())
    except Exception:
        errors.append(f"Unknown timezone '{v['timezone']}' (use an IANA name like America/Chicago).")
    if v["blackout_start"] and v["blackout_end"] and v["blackout_start"] > v["blackout_end"]:
        errors.append("Blackout start must be on or before blackout end.")
    if not v["preferred_horizons"]:
        errors.append("Select at least one prediction horizon.")
    if bool(v["quiet_start"]) != bool(v["quiet_end"]):
        errors.append("Quiet hours need both a start and an end time (or neither).")
    if not (1 <= int(v["min_severity"]) <= 5):
        errors.append("Minimum severity must be between 1 and 5.")
    if v["use_conviction"] and not (0 <= int(v["min_conviction"]) <= 10):
        errors.append("Minimum conviction must be between 0 and 10.")
    return errors


# ── Status strip ──────────────────────────────────────────────────────────────

c1, c2, c3, c4 = st.columns(4)
with c1:
    card(
        f'<div style="font-size:0.7rem;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;color:#6B7280">Pre-clearance</div>'
        f'<div style="margin-top:6px">{badge_html("Required", SUCCESS, SUCCESS_LIGHT) if profile.pre_clearance_required else badge_html("Disabled", WARNING, WARNING_LIGHT)}</div>',
    )
with c2:
    in_window = False
    _s, _e = _parse_date(profile.blackout_start), _parse_date(profile.blackout_end)
    if _s or _e:
        today = datetime.now(ZoneInfo(profile.timezone)).date() if profile.timezone else date.today()
        in_window = (not _s or today >= _s) and (not _e or today <= _e)
    window_txt = f"{profile.blackout_start or 'open'} → {profile.blackout_end or 'open'}" if (_s or _e) else "none"
    card(
        f'<div style="font-size:0.7rem;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;color:#6B7280">Blackout window</div>'
        f'<div style="margin-top:6px;font-size:0.9rem">{window_txt} '
        f'{badge_html("active today", WARNING, WARNING_LIGHT) if in_window else ""}</div>',
    )
with c3:
    card(
        f'<div style="font-size:0.7rem;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;color:#6B7280">Notifications</div>'
        f'<div style="margin-top:6px;font-size:0.9rem">{notif.channel} · severity ≥ {notif.min_severity_threshold}</div>',
    )
with c4:
    card(
        f'<div style="font-size:0.7rem;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;color:#6B7280">Last saved</div>'
        f'<div style="margin-top:6px;font-size:0.9rem">{_fmt_ts(profile.updated_at)}</div>',
    )

st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)

# ── Form ──────────────────────────────────────────────────────────────────────

with st.form("profile_form"):
    # Identity
    _tile_1 = section_tile("Identity", badge_text="display · currency · timezone", expanded=True, key="profile_1", native=True)
    if _tile_1:
        with _tile_1:
            i1, i2, i3 = st.columns([3, 1.2, 2.5])
            display_name = i1.text_input("Display name", value=profile.display_name or "", placeholder="Your name")
            base_currency = i2.text_input("Base currency", value=profile.base_currency, max_chars=3,
                                          help="3-letter ISO code used for reporting")
            tz_name = i3.text_input("Timezone", value=profile.timezone,
                                    help="IANA name, e.g. America/Chicago — used for blackout dates and quiet hours")

            # Mandate limits
    _tile_2 = section_tile("Mandate Limits", badge_text="used by the portfolio optimizer", expanded=False, key="profile_2", native=True)
    if _tile_2:
        with _tile_2:
            st.caption(
                "Concentration thresholds are percentages of total portfolio value; cash caps are fractions. "
                "The defaults (25 / 15 / 0.40 / 0.15) match the optimizer's built-in fallbacks."
            )
            m1, m2, m3, m4 = st.columns(4)
            max_sector_pct = m1.number_input(
                "Max sector concentration (%)", min_value=0.0, step=1.0, format="%.1f",
                value=float(profile.max_sector_pct),
                help="Holdings pushing a sector past this share are flagged for trimming and penalised as top-up candidates.",
            )
            max_issuer_pct = m2.number_input(
                "Max issuer concentration (%)", min_value=0.0, step=1.0, format="%.1f",
                value=float(profile.max_issuer_pct),
                help="Same as sector, but for a single issuer across share classes.",
            )
            max_per_candidate = m3.number_input(
                "Max share of new cash per candidate", min_value=0.0, step=0.05, format="%.2f",
                value=float(profile.max_per_candidate_pct_of_cash),
                help="Fraction 0–1. 0.40 = no single allocation exceeds 40% of the new cash.",
            )
            max_post_trade = m4.number_input(
                "Max post-trade position", min_value=0.0, step=0.01, format="%.2f",
                value=float(profile.max_post_trade_position_pct),
                help="Fraction 0–1 of post-trade total value any one position may reach.",
            )
            sector_options = sorted(set(_GICS_SECTORS) | set(profile.sector_exclusions or []))
            sector_exclusions = st.multiselect(
                "Sector exclusions", options=sector_options, default=list(profile.sector_exclusions or []),
                accept_new_options=True,
                help="Sectors you never want recommended. Type to add a custom name.",
            )

            # Compliance
    _tile_3 = section_tile("Compliance", badge_text="clearance agent", expanded=False, key="profile_3", native=True)
    if _tile_3:
        with _tile_3:
            k1, k2 = st.columns([3, 2])
            employer = k1.text_input("Employer", value=profile.employer or "", placeholder="Optional")
            pre_clearance_required = k2.toggle(
                "Pre-trade clearance required", value=bool(profile.pre_clearance_required),
                help="When off, the clearance step is skipped for `--agent clearance` and inside the full pipeline.",
            )
            b1, b2, b3 = st.columns([1.5, 1.5, 3])
            blackout_start = b1.date_input("Blackout start", value=_parse_date(profile.blackout_start),
                                           format="YYYY-MM-DD", help="Inclusive. Leave empty for no blackout.")
            blackout_end = b2.date_input("Blackout end", value=_parse_date(profile.blackout_end),
                                         format="YYYY-MM-DD", help="Inclusive. Leave empty for an open-ended window.")
            with b3:
                st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
                disclaimer_accepted = st.checkbox(
                    "I have read and accept the disclaimer",
                    value=bool(profile.disclaimer_accepted_at),
                    help=f"Accepted: {_fmt_ts(profile.disclaimer_accepted_at)}",
                )

            # Notifications
    _tile_4 = section_tile("Notifications", badge_text="personal filter on event alerts", expanded=False, key="profile_4", native=True)
    if _tile_4:
        with _tile_4:
            st.caption(
                "The pipeline still runs on every event that clears the global severity floor; these settings "
                "only decide whether you are told about it."
            )
            n1, n2, n3, n4 = st.columns([1.5, 1.5, 1.2, 1.5])
            channel = n1.selectbox(
                "Channel", options=list(NOTIFICATION_CHANNELS),
                index=list(NOTIFICATION_CHANNELS).index(notif.channel) if notif.channel in NOTIFICATION_CHANNELS else 0,
                help="'none' silences all notifications.",
            )
            min_severity = n2.number_input("Min severity (1–5)", min_value=1, max_value=5, step=1,
                                           value=int(notif.min_severity_threshold))
            use_conviction = n3.checkbox("Apply conviction floor", value=notif.min_conviction_threshold is not None)
            min_conviction = n4.number_input(
                "Min conviction (0–10)", min_value=0, max_value=10, step=1,
                value=int(notif.min_conviction_threshold if notif.min_conviction_threshold is not None else 6),
                help="Only enforced when a conviction is known (i.e. after a prediction) and the box is ticked.",
            )
            q1, q2, _ = st.columns([1.5, 1.5, 3])
            quiet_start = q1.time_input("Quiet hours start", value=_parse_time(notif.quiet_hours_start), step=900)
            quiet_end = q2.time_input("Quiet hours end", value=_parse_time(notif.quiet_hours_end), step=900,
                                      help="A start later than the end spans midnight (e.g. 22:00 → 07:00).")

            # Horizons
    _tile_5 = section_tile("Prediction Horizons", badge_text="generation filter", expanded=False, key="profile_5", native=True)
    if _tile_5:
        with _tile_5:
            preferred_horizons = st.multiselect(
                "Preferred horizons", options=list(ALL_HORIZON_LABELS),
                default=[h for h in profile.preferred_horizons if h in ALL_HORIZON_LABELS] or list(ALL_HORIZON_LABELS),
                format_func=lambda h: f"{h} · {_HORIZON_HELP.get(h, '')}",
                help="Only these horizons get per-horizon weights and predictions. Weighting math is unchanged.",
            )

    st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)
    submitted = st.form_submit_button("Save profile", type="primary", icon=material("save"))

if submitted:
    values = {
        "display_name": display_name.strip() or None,
        "base_currency": base_currency.strip().upper(),
        "timezone": tz_name.strip(),
        "max_sector_pct": float(max_sector_pct),
        "max_issuer_pct": float(max_issuer_pct),
        "max_per_candidate_pct_of_cash": float(max_per_candidate),
        "max_post_trade_position_pct": float(max_post_trade),
        "sector_exclusions": [s.strip() for s in sector_exclusions if s and s.strip()],
        "employer": employer.strip() or None,
        "pre_clearance_required": bool(pre_clearance_required),
        "blackout_start": blackout_start,
        "blackout_end": blackout_end,
        "preferred_horizons": list(preferred_horizons),
        "quiet_start": quiet_start,
        "quiet_end": quiet_end,
        "min_severity": int(min_severity),
        "use_conviction": bool(use_conviction),
        "min_conviction": int(min_conviction),
    }
    problems = _validate(values)
    if problems:
        for msg in problems:
            st.error(msg, icon=material("error"))
    else:
        # Disclaimer: stamp on first acceptance, keep the original stamp while it
        # stays ticked, clear it when unticked.
        if disclaimer_accepted:
            disclaimer_at = profile.disclaimer_accepted_at or datetime.now(timezone.utc).isoformat()
        else:
            disclaimer_at = None

        update_user_profile(
            display_name=values["display_name"],
            base_currency=values["base_currency"],
            timezone=values["timezone"],
            max_sector_pct=values["max_sector_pct"],
            max_issuer_pct=values["max_issuer_pct"],
            max_per_candidate_pct_of_cash=values["max_per_candidate_pct_of_cash"],
            max_post_trade_position_pct=values["max_post_trade_position_pct"],
            sector_exclusions=values["sector_exclusions"],
            employer=values["employer"],
            pre_clearance_required=values["pre_clearance_required"],
            blackout_start=blackout_start.isoformat() if blackout_start else None,
            blackout_end=blackout_end.isoformat() if blackout_end else None,
            disclaimer_accepted_at=disclaimer_at,
            preferred_horizons=values["preferred_horizons"],
        )
        update_user_notifications(
            channel=channel,
            min_severity_threshold=values["min_severity"],
            min_conviction_threshold=values["min_conviction"] if use_conviction else None,
            quiet_hours_start=quiet_start.strftime("%H:%M") if quiet_start else None,
            quiet_hours_end=quiet_end.strftime("%H:%M") if quiet_end else None,
        )
        st.session_state["profile_flash"] = "Profile saved."
        st.rerun()

# ── Admin: invited users ──────────────────────────────────────────────────────

if current_user().is_admin:
    from portfolio_agent.domain import LOCAL_OWNER
    from portfolio_agent.tools.auth_users_db import disable_user, invite_user, list_users

    st.divider()
    _tile_users = section_tile("Users", badge_text="admin", badge_color=PRIMARY, expanded=True, key="admin_users")
    if _tile_users:
        with _tile_users:
            users = list_users()
            if users:
                st.dataframe(
                    [
                        {
                            "email": u.email,
                            "owner": u.owner,
                            "role": u.role,
                            "enabled": u.enabled,
                            "status": "bound" if u.subject else "pending first login",
                            "last_login_at": u.last_login_at or "",
                        }
                        for u in users
                    ],
                    width="stretch",
                    hide_index=True,
                )
            else:
                st.caption("No invited users yet.")

            with st.form("admin_invite_user", clear_on_submit=True):
                cols = st.columns([3, 2, 1.5, 1])
                invite_email = cols[0].text_input("Email", placeholder="name@example.com")
                invite_owner = cols[1].text_input("Owner", value=LOCAL_OWNER)
                invite_role = cols[2].selectbox("Role", ["member", "admin"])
                if cols[3].form_submit_button("Invite", width="stretch"):
                    if invite_email.strip():
                        invite_user(invite_email.strip(), owner=invite_owner.strip(), role=invite_role)
                        st.session_state["profile_flash"] = f"Invited {invite_email.strip()}."
                        st.rerun()
                    else:
                        st.error("Email is required.", icon=material("error"))

            if users:
                disable_email = st.selectbox(
                    "Disable a user", [""] + [u.email for u in users if u.enabled], key="admin_disable_select"
                )
                if disable_email and st.button("Disable", key="admin_disable_btn"):
                    disable_user(disable_email)
                    st.session_state["profile_flash"] = f"Disabled {disable_email}."
                    st.rerun()

    from portfolio_agent.tools.access_requests_db import approve_request, deny_request, list_requests as list_access_requests

    pending_requests = list_access_requests(status="pending")
    _tile_requests = section_tile(
        "Access Requests", badge_text=f"{len(pending_requests)} pending" if pending_requests else "none pending",
        badge_color=WARNING if pending_requests else PRIMARY, expanded=bool(pending_requests), key="admin_requests",
    )
    if _tile_requests:
        with _tile_requests:
            if not pending_requests:
                st.caption("No pending requests from the landing page's \"Request an invite\" form.")
            for req in pending_requests:
                r_cols = st.columns([2, 2, 3, 1.3, 1, 1])
                r_cols[0].markdown(f"**{req.name}**")
                r_cols[1].caption(req.email)
                r_cols[2].caption(req.message or "—")
                req_owner = r_cols[3].text_input("Owner", value=LOCAL_OWNER, key=f"req_owner_{req.id}", label_visibility="collapsed")
                req_role = r_cols[4].selectbox("Role", ["member", "admin"], key=f"req_role_{req.id}", label_visibility="collapsed")
                if r_cols[5].button("Approve", key=f"req_approve_{req.id}", width="stretch"):
                    approve_request(req.id, owner=req_owner.strip(), role=req_role)
                    st.session_state["profile_flash"] = f"Approved {req.email} — invited as {req_role}."
                    st.rerun()
                if st.button("Deny", key=f"req_deny_{req.id}"):
                    deny_request(req.id)
                    st.session_state["profile_flash"] = f"Denied {req.email}'s request."
                    st.rerun()
