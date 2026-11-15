"""
Trade plan display — one small block per forecast row: entry / stop / target / time stop / reward:risk, or the reason there
is no plan. The numbers are the rules' (tools.trade_plan), never the model's; the model's own upper target is shown beside the
plan's only for comparison. Private sizing ("Max loss $X -> N shares") is passed in by a caller that has the logged-in owner;
this module never looks up a portfolio itself.
"""

from __future__ import annotations

import html as _html

import streamlit as st

from portfolio_agent.tools.trade_plan import NO_PLAN_REASONS


def _money(v) -> str:
    return f"${v:,.2f}" if isinstance(v, (int, float)) else "—"


def _pct(v) -> str:
    return f"{v * 100:.0f}%" if isinstance(v, (int, float)) else "—"


def stop_rule_words(plan: dict) -> str:
    """The stop rule in words, e.g. '10-day range low' (a row written before v2 recorded no parameter: 'ATR stop')."""
    rule, p = plan.get("stop_rule") or "atr", plan.get("stop_param")
    num = isinstance(p, (int, float))
    if rule == "pct":
        return f"{p * 100:g}% below entry" if num else "percent stop"
    if rule == "range_low":
        return f"{int(p)}-day range low" if num else "recent range low"
    if rule == "range_mid":
        return f"{int(p)}-day range midpoint" if num else "recent range midpoint"
    if rule == "price":
        return "your price"
    return f"{p:g}× ATR14 below entry" if num else "ATR stop"


def target_rule_words(plan: dict) -> str:
    rule, p = plan.get("target_rule") or "r_multiple", plan.get("target_param")
    if rule == "price":
        return "your price"
    return f"{p:g}× the risk above entry" if isinstance(p, (int, float)) else "multiple of the risk"


def _rule_line(plan: dict) -> str:
    """'Stop: 10-day range low, $182.40, rounded down · Target: 2× the risk above entry, $190.00'. Rows from before v2 say no rounding."""
    rounded = ", rounded down" if plan.get("rules_version") not in (None, "trade-plan-v1") else ""
    parts = [f"Stop: {stop_rule_words(plan)}, {_money(plan.get('stop'))}{rounded}"]
    if plan.get("target") is not None:
        parts.append(f"Target: {target_rule_words(plan)}, {_money(plan.get('target'))}")
    return (f'<div style="font-size:0.68rem;color:#64748B;margin-top:2px">{_html.escape(" · ".join(parts))}</div>')


def _no_plan_text(plan: dict) -> str:
    """The reason sentence; a freshly refused owner plan carries its own specific `message` (e.g. the too-tight dollars)."""
    return plan.get("message") or NO_PLAN_REASONS.get(plan.get("no_plan_reason"), plan.get("no_plan_reason") or "no plan")


def protective_block_html(plan: dict | None) -> str:
    """
    Protective exit for a position the owner HOLDS when the forecast is DOWN: a stop to cap the damage if the call is right, and the
    model's own trim level (display only). Never a short. Not simulated. "" when there is nothing to show.
    """
    if not plan:
        return ""
    head = ('<span style="font-size:0.7rem;font-weight:700;color:#B45309">PROTECTIVE EXIT</span>'
            '<span style="font-size:0.7rem;color:#92400E">for a position you hold: a forecast of DOWN, not a short</span>')
    box = lambda inner: (f'<div style="margin:3px 0 2px;padding:4px 8px;border-left:3px solid #F59E0B;background:#FFFBEB;'    # noqa: E731
                         f'border-radius:4px"><div style="display:flex;gap:10px;flex-wrap:wrap;align-items:baseline">{head}</div>{inner}</div>')
    if plan.get("plan_status") != "planned":
        return box(f'<div style="font-size:0.72rem;color:#92400E;margin-top:2px">No protective exit: '
                   f'{_html.escape(_no_plan_text(plan))}</div>')
    cells = [("Reference", _money(plan.get("entry"))), ("Sell if it falls to", _money(plan.get("stop"))),
             ("Time stop", _html.escape(str(plan.get("time_stop_date") or "—")))]
    if plan.get("trim_level"):
        cells.insert(2, ("Model's trim level", f'{_money(plan["trim_level"])} <span style="font-weight:400;color:#94A3B8">(model\'s number, display only)</span>'))
    body = "".join(f'<span style="font-size:0.72rem;color:#475569">{k} <b style="color:#0F172A">{v}</b></span>' for k, v in cells)
    src = "your own stop" if plan.get("source") == "custom" else "default rule"
    return box(f'<div style="display:flex;gap:12px;flex-wrap:wrap;margin-top:2px">{body}</div>' + _rule_line({**plan, "target": None}) +
               f'<div style="font-size:0.66rem;color:#94A3B8">{src} · Not simulated. No order is placed and nothing here is a short sale.</div>')


def sim_block_html(sim: dict | None, *, flagged: bool = False, blend_p: float | None = None) -> str:
    """
    The stock's own history replayed against the plan: a three-segment bar (target first / stop first / neither), expected R, and how
    this rule did on this stock's real past. It is NOT a prediction and says so. "" when there is no simulation to show.
    """
    if not sim:
        return ""
    if sim.get("sim_status") != "ok":
        why = {"insufficient_history": "Not enough price history to replay this stock's past.",
               "error": "The history replay could not be run."}.get(sim.get("sim_status"), "")
        return f'<div style="font-size:0.7rem;color:#94A3B8;margin:1px 0 3px">{why}</div>' if why else ""
    t, s_, n = (sim.get("p_target_first") or 0.0), (sim.get("p_stop_first") or 0.0), (sim.get("p_timeout") or 0.0)
    seg = lambda w, col: f'<div style="width:{max(w, 0) * 100:.1f}%;background:{col}"></div>'          # noqa: E731
    bar = (f'<div style="display:flex;height:8px;border-radius:4px;overflow:hidden;background:#E2E8F0;margin:3px 0" '
           f'title="Of {sim.get("paths", 0):,} replayed futures: target first / stop first / neither by the time stop">'
           f'{seg(t, "#059669")}{seg(n, "#94A3B8")}{seg(s_, "#DC2626")}</div>')
    er = sim.get("expected_r")
    hist = ""
    if sim.get("hist_n"):
        hist = (f'<div style="font-size:0.7rem;color:#475569">This rule on this stock historically: '
                f'<b>{sim["hist_target"]}</b> target, <b>{sim["hist_stop"]}</b> stop, <b>{sim["hist_timeout"]}</b> neither '
                f'(overlapping days, not independent trials)</div>')
    chip = ('<span style="background:#FEE2E2;color:#B91C1C;padding:1px 7px;border-radius:4px;font-size:0.66rem;font-weight:700" '
            'title="The model is confident, but replaying this stock\'s own past reaches the target first only rarely.">Sim disagrees</span>'
            if flagged else "")
    blend = (f'<span style="font-size:0.72rem;color:#1D4ED8" title="Learned from matured calls: combines the replay probability with the '
             f'model\'s conviction. Shown only because it has beaten the replay alone.">blended target odds <b>{_pct(blend_p)}</b></span>'
             if blend_p is not None else "")
    return (f'<div style="margin:2px 0 4px;padding:4px 8px;border-left:3px solid #CBD5E1;background:#F8FAFC;border-radius:4px">'
            f'<div style="display:flex;gap:10px;flex-wrap:wrap;align-items:baseline"><span style="font-size:0.7rem;font-weight:700;'
            f'color:#64748B">HISTORY REPLAY</span><span style="font-size:0.72rem;color:#059669">target first <b>{_pct(t)}</b></span>'
            f'<span style="font-size:0.72rem;color:#DC2626">stop first <b>{_pct(s_)}</b></span>'
            f'<span style="font-size:0.72rem;color:#64748B">neither <b>{_pct(n)}</b></span>'
            f'<span style="font-size:0.72rem;color:#475569">expected R <b>{er:+.2f}</b></span>{blend}{chip}</div>{bar}{hist}'
            f'<div style="font-size:0.66rem;color:#94A3B8">The stock\'s own past days replayed {sim.get("paths", 0):,} ways. '
            f'Not a prediction.</div></div>')


def plan_block_html(plan: dict | None, *, horizon_days=None, sizing: dict | None = None, sim: dict | None = None,
                    flagged: bool = False, blend_p: float | None = None, title: str = "TRADE PLAN") -> str:
    """HTML for one row's plan (and its history replay); "" when the row has no plan record at all (older than the feature, or a private forecast)."""
    if not plan:
        return ""
    label = f"{horizon_days}d · " if horizon_days else ""
    if plan.get("plan_status") != "planned":
        reason = _no_plan_text(plan)
        lead = "No trade plan" if title == "TRADE PLAN" else "Your plan was not created"
        return (f'<div style="font-size:0.72rem;color:#94A3B8;margin:2px 0">{label}{lead}: '
                f'{_html.escape(reason)}</div>')
    cells = [("Entry", _money(plan.get("entry"))), ("Stop", _money(plan.get("stop"))), ("Target", _money(plan.get("target"))),
             ("Time stop", _html.escape(str(plan.get("time_stop_date") or "—"))),
             ("R:R", f"{plan['reward_risk']:.2f}" if isinstance(plan.get("reward_risk"), (int, float)) else "—")]
    body = "".join(f'<span style="font-size:0.72rem;color:#475569">{k} <b style="color:#0F172A">{v}</b></span>' for k, v in cells)
    llm = (f'<span style="font-size:0.68rem;color:#94A3B8">model upper target {_money(plan.get("llm_target"))} (not used)</span>'
           if plan.get("llm_target") else "")
    size = ""
    if sizing and sizing.get("shares"):
        size = (f'<div style="font-size:0.72rem;color:#475569;margin-top:2px">Max loss <b>{_money(sizing["max_loss_usd"])}</b> → '
                f'<b>{sizing["shares"]:,} shares</b> ({_money(sizing["position_value"])}; loss if stopped '
                f'{_money(sizing["loss_if_stopped"])}; limited by {"the loss budget" if sizing["binding_cap"] == "loss" else "your position limit"})</div>')
    elif sizing is not None:
        size = ('<div style="font-size:0.72rem;color:#94A3B8;margin-top:2px">Your loss budget or position limit '
                'is too small for one share at this stop.</div>')
    return (f'<div style="margin:3px 0 2px;padding:4px 8px;border-left:3px solid #CBD5E1;background:#F8FAFC;border-radius:4px">'
            f'<div style="display:flex;gap:12px;flex-wrap:wrap;align-items:baseline"><span style="font-size:0.7rem;font-weight:700;'
            f'color:#64748B">{label}{title}</span>{body}{llm}</div>{_rule_line(plan)}{size}</div>' + sim_block_html(sim, flagged=flagged, blend_p=blend_p))


def blended_odds(horizon_days, sim: dict | None, conviction) -> float | None:
    """The learned blend of the replay probability and conviction, only when a stored blend for this horizon has beaten the replay alone."""
    if not sim or sim.get("sim_status") != "ok" or horizon_days is None:
        return None
    try:
        from portfolio_agent.tools.plan_validation import p_blend
        return p_blend(int(horizon_days), sim.get("p_target_first"), conviction)
    except Exception:
        return None


_STOP_CHOICES = {"atr": "ATR multiple (× ATR14 below entry)", "pct": "Percent below entry", "range_low": "Recent range low (days)",
                 "range_mid": "Recent range midpoint (days)", "price": "My price ($)"}
_TARGET_CHOICES = {"r_multiple": "Multiple of the risk", "price": "My price ($)"}
_PARAM_HELP = {"atr": ("ATR multiple", 0.1, 20.0, 0.25), "pct": ("Percent below entry", 0.1, 50.0, 0.5),
               "range_low": ("Days", 2, 60, 1), "range_mid": ("Days", 2, 60, 1), "price": ("Price", 0.01, 1_000_000.0, 0.01),
               "r_multiple": ("Multiple of the risk", 0.1, 20.0, 0.25)}


def to_service_args(stop_rule: str, stop_value: float, target_rule: str | None, target_value: float | None) -> dict:
    """Form values -> create_custom_plan arguments (the percent field is a percent; the rules take a fraction)."""
    stop_param = stop_value / 100.0 if stop_rule == "pct" else (int(stop_value) if stop_rule in ("range_low", "range_mid") else stop_value)
    return {"stop_rule": stop_rule, "stop_param": stop_param, "target_rule": target_rule, "target_param": target_value}


def render_owner_plan_form(ctx, prediction_id: int, direction: str, *, key: str) -> None:
    """Owner-only form: your own stop (and target, on an UP forecast). Calls the service, which authorises, audits and keeps history.
    On a DOWN forecast it sets a protective stop for a position you hold; there is no target and never a short."""
    from portfolio_agent.services import investor_service as svc
    up = str(direction).upper() == "UP"
    with st.expander("Set my own stop" + (" and target" if up else " (protective exit)"), expanded=False):
        with st.form(f"own_plan_{key}"):
            c1, c2 = st.columns(2)
            srule = c1.selectbox("Stop rule", list(_STOP_CHOICES), format_func=_STOP_CHOICES.get, key=f"{key}_sr")
            lab, lo, hi, step = _PARAM_HELP[srule]
            sval = c2.number_input(f"Stop: {lab}", float(lo), float(hi), float(lo), float(step), key=f"{key}_sv")
            trule = tval = None
            if up:
                d1, d2 = st.columns(2)
                trule = d1.selectbox("Target rule", list(_TARGET_CHOICES), format_func=_TARGET_CHOICES.get, key=f"{key}_tr")
                tl, tlo, thi, tstep = _PARAM_HELP[trule]
                tval = d2.number_input(f"Target: {tl}", float(tlo), float(thi), float(max(tlo, 2.0)), float(tstep), key=f"{key}_tv")
            st.caption("Only you can see this. It is checked by the same rules as every plan (too tight, too wide, minimum reward:risk). "
                       "It never places an order.")
            go = st.form_submit_button("Save my plan", type="primary")
        if go:
            try:
                res = svc.create_custom_plan(ctx, int(prediction_id), **to_service_args(srule, sval, trule, tval), reason="own plan from the UI")
                if res["plan_status"] == "planned":
                    st.success("Saved.")
                else:
                    st.warning(f"No plan: {res.get('message') or NO_PLAN_REASONS.get(res.get('no_plan_reason'), '')}")
            except svc.NotHeld as exc:
                st.warning(str(exc))
            except Exception as exc:
                st.error(f"Could not save: {exc}")


def owner_plan_html(ctx, prediction_id: int, direction: str, shared_plan: dict | None, *, horizon_days=None, sizing=None,
                    fetch_history=None) -> str:
    """The viewer's OWN plan block for one shared forecast: their custom plan on an UP forecast, their protective exit on a DOWN one
    (only if they hold the stock). "" for everyone else. Best-effort."""
    from portfolio_agent.services import investor_service as svc
    try:
        if str(direction).upper() == "DOWN":
            return protective_block_html(svc.get_protective_plan(ctx, prediction_id, fetch_history=fetch_history))
        mine = svc.get_custom_plans(ctx, [prediction_id]).get(int(prediction_id), {}).get("custom_long")
        return plan_block_html(mine, horizon_days=horizon_days, sizing=sizing, title="YOUR PLAN") if mine else ""
    except Exception:
        return ""


def render_row_plans(rows: list[dict]) -> None:
    """Plan blocks for shared forecast rows (those carrying an `id`); a private forecast has none and shows nothing."""
    from portfolio_agent.tools.trade_plan_db import get_plans
    shared = [r for r in rows if r.get("id") is not None and r.get("scope", "shared_market") == "shared_market"
              and not r.get("is_own_private")]
    from portfolio_agent.tools.plan_sim_db import get_flagged_ids, get_raw_convictions, get_sims
    try:
        ids = [r["id"] for r in shared]
        plans, sims, flagged, convs = get_plans(ids), get_sims(ids), get_flagged_ids(ids), get_raw_convictions(ids)
    except Exception:
        return                        # a plan is additive: never break the card it sits on
    for r in shared:
        pid = int(r["id"])
        block = plan_block_html(plans.get(pid), horizon_days=r.get("horizon_days") if len(shared) > 1 else None,
                                sim=sims.get(pid), flagged=pid in flagged, blend_p=blended_odds(r.get("horizon_days"), sims.get(pid), convs.get(pid)))
        if block:
            st.markdown(block, unsafe_allow_html=True)
        _render_owner_plans(r, pid, plans.get(pid))


def _render_owner_plans(row: dict, pid: int, shared_plan: dict | None) -> None:
    """The signed-in viewer's own plan (and its form) under a shared row. Silent when there is no login or nothing applies."""
    direction = str(row.get("predicted_direction") or "").upper()
    if direction not in ("UP", "DOWN"):
        return
    try:
        from web.auth import current_context
        ctx = current_context("web:plans")
    except Exception:
        return
    mine = owner_plan_html(ctx, pid, direction, shared_plan, horizon_days=row.get("horizon_days"))
    if mine:
        st.markdown(mine, unsafe_allow_html=True)
    if direction == "UP" or mine:                      # a protective form only for someone who holds it (the block shows then)
        render_owner_plan_form(ctx, pid, direction, key=f"p{pid}")
