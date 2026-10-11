"""
Portfolio Health UI — the checkup page and the home-screen card. Layout only: every status, number and sentence comes from
portfolio_agent.tools.portfolio_health (the checks) and services.health_service (history, snapshots). Nothing here decides a
status, and there is deliberately no single overall score: the page shows how many checks are ACTION / WATCH / UNKNOWN / GOOD.
"""
from __future__ import annotations

import html
import json

import streamlit as st

from portfolio_agent.domain import HealthStatus
from portfolio_agent.services import health_service as svc
from portfolio_agent.services.account_service import NotAuthorized
from web.styles import (
    DANGER, DANGER_LIGHT, NEUTRAL, SUCCESS, SUCCESS_LIGHT, WARNING, WARNING_LIGHT, material, section_tile, stat_card_html,
)

A, W, U, G = (HealthStatus.ACTION.value, HealthStatus.WATCH.value, HealthStatus.UNKNOWN.value, HealthStatus.GOOD.value)
ORDER = (A, W, U, G)                                           # most to least attention-worthy
_LABEL = {A: "Action", W: "Watch", U: "Unknown", G: "Good"}
_STYLE = {A: (DANGER, DANGER_LIGHT), W: (WARNING, WARNING_LIGHT), U: (NEUTRAL, "#F3F4F6"), G: (SUCCESS, SUCCESS_LIGHT)}
_EXPLAIN = {A: "over a limit or far outside a band", W: "close to a limit or outside a band",
            U: "not enough data to judge — never counted as good", G: "inside your limits"}
DISCLAIMER = ("Portfolio Health compares your holdings with your own profile and limits. It is educational, not investment, "
              "tax or legal advice, and it does not tell you to buy or sell anything.")


# ── Pure html helpers (tested) ────────────────────────────────────────────────

def status_badge_html(status: str) -> str:
    color, bg = _STYLE.get(status, _STYLE[U])
    return (f'<span style="background:{bg};color:{color};font-size:0.68rem;font-weight:700;letter-spacing:0.05em;'
            f'padding:2px 9px;border-radius:20px;text-transform:uppercase">{_LABEL.get(status, status)}</span>')


def counts_chips_html(counts: dict) -> str:
    chips = []
    for s in ORDER:
        n = int((counts or {}).get(s, 0))
        color, bg = _STYLE[s]
        chips.append(f'<span title="{html.escape(_EXPLAIN[s])}" style="cursor:help;background:{bg if n else "#F9FAFB"};'
                     f'color:{color if n else "#9CA3AF"};font-weight:700;font-size:0.78rem;padding:3px 11px;border-radius:20px;'
                     f'margin-right:6px">{n} {_LABEL[s]}</span>')
    return "".join(chips)


def sorted_checks(checks) -> list:
    """ACTION first, then WATCH, UNKNOWN, GOOD; the checks' own order within a status."""
    rank = {s: i for i, s in enumerate(ORDER)}
    return sorted(checks, key=lambda c: rank.get(c.status, len(ORDER)))


def check_row_html(check) -> str:
    color, _ = _STYLE.get(check.status, _STYLE[U])
    return (f'<div style="border-left:4px solid {color};padding:6px 0 6px 12px;margin:2px 0">'
            f'<div style="display:flex;align-items:center;gap:10px">{status_badge_html(check.status)}'
            f'<span style="font-weight:700;color:#111827">{html.escape(check.title)}</span></div>'
            f'<div style="font-size:0.85rem;color:#374151;margin-top:3px">{html.escape(check.summary)}</div></div>')


_ARROW = {"worse": ("▲", DANGER), "better": ("▼", SUCCESS), "new": ("●", NEUTRAL), "removed": ("○", NEUTRAL)}


def change_line_html(ch: dict) -> str:
    arrow, color = _ARROW.get(ch["direction"], ("•", NEUTRAL))
    what = html.escape(ch["title"])
    where = "" if ch["scope"] == "household" else f' <span style="color:#6B7280">({html.escape(ch["label"])})</span>'
    move = (f'{_LABEL.get(ch["from"], "—")} → {_LABEL.get(ch["to"], "—")}' if ch.get("from") or ch.get("to") else ch["direction"])
    return f'<div style="font-size:0.85rem;padding:2px 0"><span style="color:{color};font-weight:700">{arrow}</span> {what}{where}: {move}</div>'


def scope_options(health) -> list[tuple[str, str]]:
    """[(scope key, label with counts)] — household first, then accounts, then tax buckets."""
    out = []
    for sc in health.scopes():
        c = sc.counts
        out.append((sc.scope, f"{sc.label} — {c[A]} action · {c[W]} watch · {c[U]} unknown"))
    return out


def profile_line(health) -> str:
    p = health.profile
    src = {"DEFAULT": "default", "QUIZ": "from your quiz", "PICKED": "chosen by you"}.get(str(p["source"]).upper(), str(p["source"]).lower())
    res = "your own" if str(p["reserve_source"]).upper() == "USER" else "default"
    return f"Judged against your {str(p['profile']).title()} profile ({src}) and a ${float(p['reserve_amount']):,.0f} emergency reserve ({res})."


# ── Page ──────────────────────────────────────────────────────────────────────

def _load(ctx):
    try:
        return svc.get_health_view(ctx), None
    except NotAuthorized:
        return None, "You don't have a portfolio here yet. Add an account on the Accounts page and your checkup will appear."


def render_health_page(ctx) -> None:
    view, problem = _load(ctx)
    if problem:
        st.info(problem, icon=material("info"))
        return
    health = view["health"]
    hh = health.household
    cols = st.columns(4)
    for col, (status, icon) in zip(cols, ((A, "error"), (W, "warning"), (U, "help"), (G, "check_circle"))):
        col.markdown(stat_card_html(str(hh.counts[status]), _LABEL[status], icon=icon, color=_STYLE[status][0]), unsafe_allow_html=True)
    st.caption("There is no single overall score on purpose: a number can hide the one thing that needs your attention. "
               "Unknown means the data to judge is missing — it is never counted as good.")
    st.caption(profile_line(health) + " Change these on the Profile page.")
    snap = view["snapshot"]
    if snap["status"] == "disabled":
        st.caption("Daily snapshots are turned off, so there is no history below — this is today's live checkup.")

    _render_changes(view)

    options = scope_options(health)
    key_of = {label: k for k, label in options}
    pick = key_of[st.selectbox("Look at", list(key_of), key="health_scope")]
    scope = next(s for s in health.scopes() if s.scope == pick)
    if scope.scope != "household":
        st.caption("This slice is judged on its own value against the same limits. Your allocation targets, emergency reserve, "
                   "profile and restricted list apply to the household as a whole.")
    st.markdown(f'<div style="margin:6px 0 10px">{counts_chips_html(scope.counts)}'
                f'<span style="color:#6B7280;font-size:0.8rem">  of ${scope.total_value:,.0f}</span></div>', unsafe_allow_html=True)
    for c in sorted_checks(scope.checks):
        st.markdown(check_row_html(c), unsafe_allow_html=True)
        if c.notes or c.reason_codes or c.metrics:
            with st.expander("Details", expanded=False):
                for n in c.notes:
                    st.write("• " + n)
                if c.reason_codes:
                    st.caption("Reason codes: " + ", ".join(c.reason_codes))
                if c.metrics:
                    st.code(json.dumps(c.metrics, indent=2, default=str), language="json")
    st.caption(DISCLAIMER)


def _render_changes(view: dict) -> None:
    last, old = view["vs_last"], view["vs_30d"]
    n = len(last["changes"]) if last["has_previous"] else 0
    badge = f"{n} change(s) since {view['last_date']}" if last["has_previous"] else "first snapshot"
    tile = section_tile("What changed", badge_text=badge, icon="history", expanded=bool(n), key="health_changes")
    if not tile:
        return
    with tile:
        if not last["has_previous"]:
            st.caption("This is your first snapshot, so there is nothing to compare with yet. Come back tomorrow.")
        elif not last["changes"]:
            st.caption(f"No check changed status since {view['last_date']}.")
        else:
            st.markdown(f"**Since {view['last_date']}**")
            st.markdown("".join(change_line_html(ch) for ch in last["changes"]), unsafe_allow_html=True)
        if old is None:
            st.caption("No snapshot from about 30 days ago yet.")
        elif not old["changes"]:
            st.caption(f"Nothing changed status since {view['thirty_days_date']} (about 30 days ago).")
        else:
            st.markdown(f"**Since {view['thirty_days_date']} (about 30 days ago)**")
            st.markdown("".join(change_line_html(ch) for ch in old["changes"]), unsafe_allow_html=True)


# ── Home card ─────────────────────────────────────────────────────────────────

def render_health_home_card(ctx) -> None:
    """A compact tile for the home screen. Never raises: the home page must load even if the checkup cannot."""
    try:
        view = svc.get_health_view(ctx)
    except Exception:
        return
    hh = view["health"].household
    tile = section_tile("Portfolio Health", badge_text=f"{hh.counts[A]} action · {hh.counts[W]} watch", icon="monitor_heart",
                        expanded=True, key="home_portfolio_health")
    if not tile:
        return
    with tile:
        st.markdown(f'<div style="margin-bottom:8px">{counts_chips_html(hh.counts)}</div>', unsafe_allow_html=True)
        top = [c for c in hh.checks if c.status == A][:3]
        for c in top:
            st.markdown(check_row_html(c), unsafe_allow_html=True)
        if not top:
            st.caption("Nothing needs action right now." if hh.counts[U] == 0 else
                       "Nothing needs action, but some checks have too little data to judge.")
        st.markdown('<div style="font-size:0.78rem;margin-top:6px">→ <a href="/Portfolio_Health" target="_self">Full checkup</a></div>',
                    unsafe_allow_html=True)
