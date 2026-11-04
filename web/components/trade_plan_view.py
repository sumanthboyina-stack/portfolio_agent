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
                    flagged: bool = False, blend_p: float | None = None) -> str:
    """HTML for one row's plan (and its history replay); "" when the row has no plan record at all (older than the feature, or a private forecast)."""
    if not plan:
        return ""
    label = f"{horizon_days}d · " if horizon_days else ""
    if plan.get("plan_status") != "planned":
        reason = NO_PLAN_REASONS.get(plan.get("no_plan_reason"), plan.get("no_plan_reason") or "no plan")
        return (f'<div style="font-size:0.72rem;color:#94A3B8;margin:2px 0">{label}No trade plan: '
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
            f'color:#64748B">{label}TRADE PLAN</span>{body}{llm}</div>{size}</div>' + sim_block_html(sim, flagged=flagged, blend_p=blend_p))


def blended_odds(horizon_days, sim: dict | None, conviction) -> float | None:
    """The learned blend of the replay probability and conviction, only when a stored blend for this horizon has beaten the replay alone."""
    if not sim or sim.get("sim_status") != "ok" or horizon_days is None:
        return None
    try:
        from portfolio_agent.tools.plan_validation import p_blend
        return p_blend(int(horizon_days), sim.get("p_target_first"), conviction)
    except Exception:
        return None


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
