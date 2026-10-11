"""
Investor profile UI — the short, optional "how do you want your money invested" section of the Profile page, and the
admin-only "Investor settings" editor. Layout only: every number, label and rule comes from
portfolio_agent.services.investor_service (writes, audited), tools.portfolio_policy (the effective policy) and
tools.investor_settings_db (the global settings). Nothing here decides an allocation or a profile.

Kept deliberately simple for the user: everyone starts as Moderate with a $3,000 emergency reserve, with zero input.
The quiz, a direct pick, a custom allocation, the limits source and goals are all optional.
"""
from __future__ import annotations

import html
import json
from datetime import date

import streamlit as st

from portfolio_agent.domain import ALLOCATION_ASSET_CLASSES, GOAL_TYPES, INSTRUMENT_TYPES, RISK_PROFILES
from portfolio_agent.services import investor_service as svc
from portfolio_agent.tools import investor_settings_db as sdb
from portfolio_agent.tools.portfolio_policy import get_effective_policy
from portfolio_agent.tools.risk_quiz import is_above_cap
from web.styles import PRIMARY, WARNING, WARNING_LIGHT, badge_html, card, material, section_tile

_CLASS_LABELS = {
    "US_EQUITY": "US stocks", "INTL_EQUITY": "International stocks", "FIXED_INCOME": "Bonds",
    "REAL_ESTATE": "Real estate", "COMMODITY": "Commodities", "CASH": "Cash",
}
_BAR_COLORS = {"US_EQUITY": "#2563EB", "INTL_EQUITY": "#7C3AED", "FIXED_INCOME": "#059669",
               "REAL_ESTATE": "#D97706", "COMMODITY": "#B45309", "CASH": "#6B7280"}


def money(v: float) -> str:
    return f"${v:,.0f}" if float(v).is_integer() else f"${v:,.2f}"


def allocation_bars_html(allocation: dict) -> str:
    """Pure: one horizontal bar per asset class, filled to its target, with the allowed band beside it."""
    rows = []
    for cls in ALLOCATION_ASSET_CLASSES:
        band = (allocation or {}).get(cls)
        if not band or (band["target"] == 0 and band["max"] == 0):
            continue
        rows.append(
            f'<div style="display:flex;align-items:center;gap:10px;margin:5px 0;font-size:0.82rem">'
            f'<div style="width:150px;color:#374151">{html.escape(_CLASS_LABELS.get(cls, cls))}</div>'
            f'<div style="flex:1;background:#F3F4F6;border-radius:6px;height:10px">'
            f'<div style="width:{band["target"]:.0f}%;background:{_BAR_COLORS.get(cls, PRIMARY)};height:10px;border-radius:6px"></div></div>'
            f'<div style="width:130px;text-align:right;color:#111827"><b>{band["target"]:.0f}%</b> '
            f'<span style="color:#9CA3AF">({band["min"]:.0f}–{band["max"]:.0f}%)</span></div></div>')
    return "".join(rows)


def _flash(msg: str) -> None:
    st.session_state["profile_flash"] = msg
    st.rerun()


def _source_note(state: dict) -> str:
    return {"DEFAULT": "default", "QUIZ": "from your quiz", "PICKED": "your choice"}.get(state["source"], "")


# ═════════════════════════════════════════════════════════════════════════════
# The user's section
# ═════════════════════════════════════════════════════════════════════════════

def render_investor_profile(ctx, user_profile) -> None:
    """Profile card, reserve, quiz, pick, custom allocation, limits source and goals — all optional."""
    state = svc.get_investor_state(ctx)
    policy = get_effective_policy(ctx)
    profiles = sdb.get_risk_profile_settings()
    current = profiles[state["profile"]]

    # ── Your profile ──
    flag = ""
    if state["above_cap"] and state["above_cap_acknowledged"]:
        flag = " " + badge_html("above your quiz result", WARNING, WARNING_LIGHT)
    alloc_note = "your own allocation" if policy.allocation_source == "CUSTOM" else "profile default"
    card(
        f'<div style="font-size:0.7rem;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;color:#6B7280">Your profile</div>'
        f'<div style="margin:6px 0 2px;font-size:1.15rem;font-weight:800;color:#111827">'
        f'{html.escape(current["display_name"])} <span style="font-weight:500;color:#9CA3AF;font-size:0.9rem">'
        f'({html.escape(_source_note(state))})</span>{flag}</div>'
        f'<div style="font-size:0.85rem;color:#4B5563;margin-bottom:10px">{html.escape(current["description"])}</div>'
        f'<div style="font-size:0.7rem;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;color:#6B7280;margin-bottom:2px">'
        f'Target allocation · {alloc_note}</div>{allocation_bars_html(policy.target_allocation)}',
    )

    # ── Emergency reserve ──
    own = state["emergency_reserve_source"] == "USER"
    default_amount = sdb.get_portfolio_settings()["default_emergency_reserve_amount"]
    r1, r2, r3 = st.columns([2.2, 1, 1.4], vertical_alignment="bottom")
    amount = r1.number_input(
        f"Emergency cash reserve — {money(state['emergency_reserve_amount'])} "
        f"({'your amount' if own else 'default'})", min_value=0.0, step=100.0,
        value=float(state["emergency_reserve_amount"]), key="reserve_amount",
        help="Cash you want to keep available for emergencies, in dollars. Only cash in taxable and cash accounts counts "
             "toward it — money inside a 401(k) or IRA isn't available without penalties or taxes.")
    if r2.button("Save", key="reserve_save", icon=material("save")):
        try:
            svc.set_emergency_reserve(ctx, amount, reason="set on Profile page")
        except ValueError as exc:
            st.error(str(exc), icon=material("error"))
        else:
            _flash(f"Emergency reserve set to {money(amount)}.")
    if own and r3.button(f"Reset to default ({money(default_amount)})", key="reserve_reset"):
        svc.set_emergency_reserve(ctx, None, reason="reset on Profile page")
        _flash(f"Emergency reserve is back to the default ({money(default_amount)}).")

    # ── Quiz ──
    quiz = sdb.get_quiz_settings()
    _t_quiz = section_tile("Take the 3-question quiz", badge_text="optional", expanded=False, key="inv_quiz", native=True)
    if _t_quiz:
        with _t_quiz:
            st.caption("Skip any question — it counts as the middle answer. The first question sets a ceiling on what "
                       "your situation can handle; the others show your comfort with risk. You get the more careful of the two.")
            answers = {}
            for q in quiz["questions"]:
                opts = [o["key"] for o in q["options"]]
                labels = {o["key"]: o["label"] for o in q["options"]}
                answers[q["id"]] = st.radio(q["text"], opts, index=None, key=f"quiz_{q['id']}",
                                            format_func=lambda k, labels=labels: labels[k])
            if st.button("Score my answers", key="quiz_submit", type="primary"):
                result = svc.take_quiz(ctx, answers)
                capped = result.preference_profile != result.profile
                _flash(f"Your answers point to {profiles[result.profile]['display_name']}"
                       + (" — your time horizon is the limiting factor." if capped else "."))

    # ── Pick a profile ──
    _t_pick = section_tile("Pick a profile", badge_text="skip the quiz", expanded=False, key="inv_pick", native=True)
    if _t_pick:
        with _t_pick:
            pick = st.radio("Profile", list(RISK_PROFILES), index=list(RISK_PROFILES).index(state["profile"]),
                            key="pick_profile", horizontal=True,
                            format_func=lambda p: profiles[p]["display_name"], label_visibility="collapsed")
            st.caption(profiles[pick]["description"])
            above = is_above_cap(pick, state["cap_profile"])
            ack = False
            if above:
                st.warning(f"{profiles[pick]['display_name']} is riskier than the "
                           f"{profiles[state['cap_profile']]['display_name']} your time horizon suggested.",
                           icon=material("warning"))
                ack = st.checkbox("I understand — use it anyway", key="pick_ack")
            if st.button("Use this profile", key="pick_submit", disabled=(above and not ack)):
                try:
                    svc.pick_profile(ctx, pick, acknowledge_above_cap=ack, reason="picked on Profile page")
                except svc.AcknowledgmentRequired as exc:
                    st.error(str(exc), icon=material("error"))
                else:
                    _flash(f"Profile set to {profiles[pick]['display_name']}.")

    # ── Custom allocation ──
    _t_alloc = section_tile("Custom target allocation", badge_text="optional", expanded=False, key="inv_alloc", native=True)
    if _t_alloc:
        with _t_alloc:
            st.caption("Replace the profile's targets with your own. Targets must add up to 100%, and each band must "
                       "contain its target.")
            base = policy.target_allocation
            new_alloc, total = {}, 0.0
            hdr = st.columns([2, 1, 1, 1])
            for h, c in zip(("Asset class", "Target %", "Min %", "Max %"), hdr):
                c.markdown(f"**{h}**")
            for cls in ALLOCATION_ASSET_CLASSES:
                b = base.get(cls, {"target": 0.0, "min": 0.0, "max": 0.0})
                c0, c1, c2, c3 = st.columns([2, 1, 1, 1], vertical_alignment="center")
                c0.write(_CLASS_LABELS.get(cls, cls))
                t = c1.number_input(f"{cls} target", 0.0, 100.0, float(b["target"]), 1.0, key=f"ca_t_{cls}", label_visibility="collapsed")
                lo = c2.number_input(f"{cls} min", 0.0, 100.0, float(b["min"]), 1.0, key=f"ca_lo_{cls}", label_visibility="collapsed")
                hi = c3.number_input(f"{cls} max", 0.0, 100.0, float(b["max"]), 1.0, key=f"ca_hi_{cls}", label_visibility="collapsed")
                total += t
                if t or hi:
                    new_alloc[cls] = {"target": t, "min": lo, "max": hi}
            st.caption(f"Targets add up to {total:g}%.")
            a1, a2, _ = st.columns([1.2, 1.4, 3])
            if a1.button("Save my allocation", key="ca_save", type="primary"):
                try:
                    svc.set_custom_allocation(ctx, new_alloc, reason="set on Profile page")
                except ValueError as exc:
                    st.error(str(exc), icon=material("error"))
                else:
                    _flash("Custom allocation saved.")
            if state["custom_allocation"] and a2.button("Use profile default", key="ca_clear"):
                svc.clear_custom_allocation(ctx, reason="reset on Profile page")
                _flash("Back to the profile's target allocation.")

    # ── Limits source ──
    _t_lim = section_tile("Concentration limits", badge_text=f"using {user_profile.limits_source}", expanded=False,
                          key="inv_limits", native=True)
    if _t_lim:
        with _t_lim:
            ps = profiles[state["profile"]]
            rows = [("Max sector", ps["max_sector_pct"], user_profile.max_sector_pct),
                    ("Max issuer", ps["max_issuer_pct"], user_profile.max_issuer_pct),
                    ("Max position", ps["max_position_pct"], user_profile.max_post_trade_position_pct * 100),
                    ("Max share of new cash per candidate", ps["max_candidate_pct_of_cash"],
                     user_profile.max_per_candidate_pct_of_cash * 100)]
            st.dataframe([{"Limit": n, f"{ps['display_name']} profile": f"{p:g}%", "Your own numbers": f"{c:g}%"}
                          for n, p, c in rows], hide_index=True, width="stretch")
            choice = st.radio("Use", ["profile", "custom"], index=0 if user_profile.limits_source == "profile" else 1,
                              horizontal=True, key="limits_source_choice",
                              format_func=lambda v: "My profile's limits" if v == "profile" else "My own numbers")
            if st.button("Save", key="limits_source_save"):
                svc.set_limits_source(ctx, choice, reason="set on Profile page")
                _flash("Limits source updated.")

    # ── Goals ──
    goals = svc.list_goals(ctx)
    _t_goals = section_tile("Goals", badge_text=f"{len(goals)} saved" if goals else "optional", expanded=False,
                            key="inv_goals", native=True)
    if _t_goals:
        with _t_goals:
            st.caption("Optional notes about what the money is for. Nothing depends on them yet.")
            for g in goals:
                g1, g2, g3 = st.columns([5, 1, 1], vertical_alignment="center")
                g1.markdown(f"**{html.escape(g['name'])}** · {g['goal_type'].title()}"
                            + (f" · {money(g['target_amount'])}" if g["target_amount"] else "")
                            + (f" · by {g['target_date']}" if g["target_date"] else "")
                            + (" · done" if g["status"] == "DONE" else ""))
                if g["status"] == "ACTIVE" and g2.button("Done", key=f"goal_done_{g['goal_id']}"):
                    svc.update_goal(ctx, g["goal_id"], status="DONE")
                    st.rerun()
                if g3.button("Remove", key=f"goal_rm_{g['goal_id']}"):
                    svc.remove_goal(ctx, g["goal_id"])
                    st.rerun()
            with st.form("goal_add", clear_on_submit=True):
                f1, f2, f3, f4 = st.columns([1.3, 2.2, 1.3, 1.4])
                gtype = f1.selectbox("Type", sorted(GOAL_TYPES), format_func=str.title)
                gname = f2.text_input("Name", placeholder="e.g. House down payment")
                gamt = f3.number_input("Target ($, optional)", min_value=0.0, step=1000.0, value=0.0)
                gdate = f4.date_input("By (optional)", value=None, format="YYYY-MM-DD")
                if st.form_submit_button("Add goal"):
                    try:
                        svc.add_goal(ctx, goal_type=gtype, name=gname, target_amount=gamt or None,
                                     target_date=gdate.isoformat() if gdate else None)
                    except ValueError as exc:
                        st.error(str(exc), icon=material("error"))
                    else:
                        st.rerun()

    st.caption("APEX is an educational tool, not investment advice. Profiles, targets and reserves are starting points "
               "you can change — they do not take into account your full financial situation.")


# ═════════════════════════════════════════════════════════════════════════════
# Admin: Investor settings
# ═════════════════════════════════════════════════════════════════════════════

def render_investor_admin(ctx) -> None:
    """Admin-only editor for risk_quiz_settings, risk_profile_settings and portfolio_settings, with reset to defaults.
    Every write goes through investor_service, which re-checks admin server-side."""
    _tile = section_tile("Investor settings", badge_text="admin", badge_color=PRIMARY, expanded=False, key="admin_investor")
    if not _tile:
        return
    with _tile:
        t_res, t_prof, t_quiz, t_fund, t_health, t_hz = st.tabs(
            ["Default emergency reserve", "Profiles", "Quiz", "Fund data & snapshots", "Health thresholds", "Horizons by type"])

        with t_res:
            ps = sdb.get_portfolio_settings()
            with st.form("admin_portfolio_settings"):
                amt = st.number_input("Default emergency reserve ($)", min_value=0.0, step=100.0,
                                      value=float(ps["default_emergency_reserve_amount"]),
                                      help="Used for every user who hasn't set their own amount.")
                st.caption(f"Version {ps['version']} · updated {ps['updated_at'][:16].replace('T', ' ')}"
                           + (f" by {ps['updated_by']}" if ps.get("updated_by") else ""))
                c1, c2 = st.columns(2)
                save = c1.form_submit_button("Save", type="primary")
                reset = c2.form_submit_button("Reset to default")
            if save or reset:
                # "Reset" here puts back the default RESERVE only; the fund-data settings have their own reset
                _admin_call(lambda: svc.update_portfolio_settings(
                    ctx, expected_version=ps["version"],
                    default_emergency_reserve_amount=sdb.DEFAULT_EMERGENCY_RESERVE_AMOUNT if reset else amt),
                            "Default emergency reserve " + ("reset." if reset else "saved."))

        with t_prof:
            all_profiles = sdb.get_risk_profile_settings()
            name = st.selectbox("Profile", list(RISK_PROFILES), key="admin_profile_pick",
                                format_func=lambda p: all_profiles[p]["display_name"])
            cur = all_profiles[name]
            with st.form(f"admin_profile_{name}"):
                d1, d2 = st.columns([1, 3])
                dn = d1.text_input("Display name", cur["display_name"])
                desc = d2.text_area("Description", cur["description"], height=70)
                n1, n2, n3, n4, n5 = st.columns(5)
                pos = n1.number_input("Max position %", 0.0, 100.0, float(cur["max_position_pct"]), 1.0)
                iss = n2.number_input("Max issuer %", 0.0, 100.0, float(cur["max_issuer_pct"]), 1.0)
                sec = n3.number_input("Max sector %", 0.0, 100.0, float(cur["max_sector_pct"]), 1.0)
                cand = n4.number_input("Max candidate % of cash", 0.0, 100.0, float(cur["max_candidate_pct_of_cash"]), 1.0)
                lev = n5.number_input("Max leveraged/inverse %", 0.0, 100.0, float(cur["max_leveraged_inverse_pct"]), 1.0)
                st.markdown("**Target allocation (%)**")
                alloc = {}
                total = 0.0
                for cls in ALLOCATION_ASSET_CLASSES:
                    b = cur["allocation"].get(cls, {"target": 0.0, "min": 0.0, "max": 0.0})
                    c0, c1, c2, c3 = st.columns([2, 1, 1, 1], vertical_alignment="center")
                    c0.write(_CLASS_LABELS.get(cls, cls))
                    t = c1.number_input("target", 0.0, 100.0, float(b["target"]), 1.0, key=f"ap_t_{name}_{cls}", label_visibility="collapsed")
                    lo = c2.number_input("min", 0.0, 100.0, float(b["min"]), 1.0, key=f"ap_lo_{name}_{cls}", label_visibility="collapsed")
                    hi = c3.number_input("max", 0.0, 100.0, float(b["max"]), 1.0, key=f"ap_hi_{name}_{cls}", label_visibility="collapsed")
                    total += t
                    if t or hi:
                        alloc[cls] = {"target": t, "min": lo, "max": hi}
                st.caption(f"Targets add up to {total:g}% (must be 100).")
                allowed = st.multiselect("Allowed instrument types", sorted(INSTRUMENT_TYPES),
                                         default=[t for t in cur["allowed_instrument_types"] if t in INSTRUMENT_TYPES])
                st.caption(f"Version {cur['version']} · updated {cur['updated_at'][:16].replace('T', ' ')}"
                           + (f" by {cur['updated_by']}" if cur.get("updated_by") else ""))
                b1, b2, b3 = st.columns([1, 1.4, 1.6])
                save = b1.form_submit_button("Save", type="primary")
                reset_one = b2.form_submit_button(f"Reset {all_profiles[name]['display_name']}")
                reset_all = b3.form_submit_button("Reset all three profiles")
            if save:
                _admin_call(lambda: svc.update_risk_profile_settings(
                    ctx, name, expected_version=cur["version"], display_name=dn, description=desc, allocation=alloc,
                    max_position_pct=pos, max_issuer_pct=iss, max_sector_pct=sec, max_candidate_pct_of_cash=cand,
                    max_leveraged_inverse_pct=lev, allowed_instrument_types=allowed), f"{dn} saved.")
            elif reset_one or reset_all:
                _admin_call(lambda: svc.reset_risk_profile_settings(ctx, None if reset_all else name),
                            "All three profiles reset." if reset_all else f"{all_profiles[name]['display_name']} reset.")

        with t_quiz:
            q = sdb.get_quiz_settings()
            with st.form("admin_quiz"):
                st.caption("Question text, options, point values, the cap rule and the preference thresholds are stored "
                           "as JSON. They are validated together before saving.")
                qj = st.text_area("Questions", json.dumps(q["questions"], indent=2), height=260)
                cj = st.text_area("Cap rule", json.dumps(q["cap_rules"], indent=2), height=120)
                tj = st.text_area("Preference thresholds", json.dumps(q["preference_thresholds"], indent=2), height=160)
                st.caption(f"Quiz version {q['quiz_version']} · settings version {q['version']}")
                c1, c2 = st.columns(2)
                save = c1.form_submit_button("Save quiz", type="primary")
                reset = c2.form_submit_button("Reset quiz to default")
            if save or reset:
                def _go():
                    if reset:
                        return svc.reset_quiz_settings(ctx)
                    try:
                        return svc.update_quiz_settings(ctx, expected_version=q["version"], questions=json.loads(qj),
                                                        cap_rules=json.loads(cj), preference_thresholds=json.loads(tj))
                    except json.JSONDecodeError as exc:
                        raise ValueError(f"not valid JSON: {exc}")
                _admin_call(_go, "Quiz reset." if reset else "Quiz saved.")

        with t_fund:
            ps = sdb.get_portfolio_settings()
            with st.form("admin_fund_settings"):
                f1, f2 = st.columns(2)
                etf_ttl = f1.number_input("ETF data refresh (hours)", 0.5, 8760.0, float(ps["etf_data_ttl_hours"]), 1.0,
                                          help="How long an ETF's holdings / mix / expense ratio is reused before it is fetched again.")
                mf_ttl = f2.number_input("Mutual-fund data refresh (hours)", 0.5, 8760.0, float(ps["mutual_fund_data_ttl_hours"]), 1.0,
                                         help="Mutual funds publish holdings monthly or quarterly, so this is longer.")
                g1, g2, g3 = st.columns(3)
                cov = g1.number_input("Look-through minimum coverage (%)", 0.0, 100.0, float(ps["lookthrough_min_coverage_pct"]), 5.0,
                                      help="Below this share of your money explained, concentration checks use the funds "
                                           "themselves and say that fund contents are only partly known.")
                depth = g2.number_input("Look-through max depth", 1, 6, int(ps["lookthrough_max_depth"]), 1,
                                        help="How many fund-in-fund levels to open (a target-date fund holding index funds is 2).")
                exp = g3.number_input("High expense ratio (% a year)", 0.0, 10.0, float(ps["high_expense_ratio_pct"]), 0.05,
                                      help="Funds above this are flagged on the Portfolio Health page.")
                snap = st.checkbox("Save a daily Portfolio Health snapshot", value=bool(ps["health_snapshot_enabled"]))
                st.caption(f"Version {ps['version']} · updated {ps['updated_at'][:16].replace('T', ' ')}"
                           + (f" by {ps['updated_by']}" if ps.get("updated_by") else ""))
                b1, b2 = st.columns(2)
                save = b1.form_submit_button("Save", type="primary")
                reset = b2.form_submit_button("Reset all portfolio settings (incl. reserve)")
            if save or reset:
                _admin_call(lambda: svc.reset_portfolio_settings(ctx) if reset else svc.update_portfolio_settings(
                    ctx, expected_version=ps["version"], etf_data_ttl_hours=etf_ttl, mutual_fund_data_ttl_hours=mf_ttl,
                    lookthrough_min_coverage_pct=cov, lookthrough_max_depth=int(depth), high_expense_ratio_pct=exp,
                    health_snapshot_enabled=snap), "Portfolio settings " + ("reset." if reset else "saved."))

        with t_health:
            ps = sdb.get_portfolio_settings()
            st.caption("The numbers behind GOOD / WATCH / ACTION on the Portfolio Health page. Each person's own limits and "
                       "targets come from their profile; these are the shared rules for comparing against them.")
            spec = sdb.HEALTH_SETTINGS_SPEC
            with st.form("admin_health_thresholds"):
                h1, h2, h3 = st.columns(3)
                mult = h1.number_input("ACTION when outside a band by (× band width)", spec["health_action_band_multiple"][1],
                                       spec["health_action_band_multiple"][2], float(ps["health_action_band_multiple"]), 0.5,
                                       help="A mix is WATCH outside its band and ACTION beyond this many band-widths from the target.")
                frac = h2.number_input("WATCH from (fraction of a limit)", 0.0, 1.0, float(ps["health_watch_limit_fraction"]), 0.05,
                                       help="A concentration or leveraged share at or above this fraction of its limit is WATCH.")
                tol = h3.number_input("Unknown share that blocks GOOD (%)", 0.0, 100.0, float(ps["health_unknown_share_tolerance_pct"]), 1.0,
                                      help="If more than this much of a mix could not be classified, the check cannot be GOOD.")
                e1, e2, e3, e4 = st.columns(4)
                n_w = e1.number_input("Effective holdings: WATCH below", 1.0, 1000.0, float(ps["health_min_effective_n_watch"]), 1.0)
                n_a = e2.number_input("Effective holdings: ACTION below", 1.0, 1000.0, float(ps["health_min_effective_n_action"]), 1.0)
                c_w = e3.number_input("Avg correlation: WATCH above", 0.0, 1.0, float(ps["health_max_avg_correlation_watch"]), 0.05)
                c_a = e4.number_input("Avg correlation: ACTION above", 0.0, 1.0, float(ps["health_max_avg_correlation_action"]), 0.05)
                d1, d2 = st.columns(2)
                unp = d1.number_input("Unpriced value: ACTION above (%)", 0.0, 100.0, float(ps["health_unpriced_action_pct"]), 5.0)
                stale = d2.number_input("Fund data stale after (× refresh interval)", 1.0, 100.0, float(ps["health_stale_fund_data_multiple"]), 0.5)
                st.caption(f"Version {ps['version']} · updated {ps['updated_at'][:16].replace('T', ' ')}"
                           + (f" by {ps['updated_by']}" if ps.get("updated_by") else ""))
                b1, b2 = st.columns(2)
                save = b1.form_submit_button("Save", type="primary")
                reset = b2.form_submit_button("Reset all portfolio settings (incl. reserve)")
            if save or reset:
                _admin_call(lambda: svc.reset_portfolio_settings(ctx) if reset else svc.update_portfolio_settings(
                    ctx, expected_version=ps["version"], health_action_band_multiple=mult, health_watch_limit_fraction=frac,
                    health_unknown_share_tolerance_pct=tol, health_min_effective_n_watch=n_w, health_min_effective_n_action=n_a,
                    health_max_avg_correlation_watch=c_w, health_max_avg_correlation_action=c_a, health_unpriced_action_pct=unp,
                    health_stale_fund_data_multiple=stale), "Health thresholds " + ("reset." if reset else "saved."))

        with t_hz:
            from portfolio_agent.tools import engine_settings_db as esdb
            es = esdb.get_engine_settings()
            cur = es["instrument_type_horizons"]
            st.caption("Which forecast horizons (days) each kind of security gets. A type with none is never forecast. "
                       "A horizon only runs if it is also on the daily schedule.")
            with st.form("admin_type_horizons"):
                picked: dict[str, list[int]] = {}
                for itype in sorted(cur):
                    picked[itype] = st.multiselect(itype.replace("_", " ").title(), [5, 21, 63, 250],
                                                   default=[h for h in cur[itype] if h in (5, 21, 63, 250)], key=f"hz_{itype}")
                st.caption(f"Engine settings version {es['version']}")
                b1, b2 = st.columns(2)
                save = b1.form_submit_button("Save", type="primary")
                reset = b2.form_submit_button("Reset to defaults")
            if save or reset:
                _admin_call(lambda: svc.reset_instrument_type_horizons(ctx) if reset else
                            svc.update_instrument_type_horizons(ctx, picked, expected_version=es["version"]),
                            "Horizons " + ("reset." if reset else "saved."))


def _admin_call(fn, ok_message: str) -> None:
    try:
        fn()
    except (ValueError, RuntimeError, svc.NotAdmin, sdb.SettingsVersionConflict) as exc:
        st.error(str(exc), icon=material("error"))
    else:
        _flash(ok_message)
