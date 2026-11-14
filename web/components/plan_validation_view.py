"""
"Plans & Simulation" on the Validation page: how the plan simulator and the model's conviction did against what actually happened to
each plan (TARGET / STOP / TIMEOUT). Every number carries its n; anything with fewer than 10 rows is hidden. All intervals resample
whole prediction dates (same-day calls are correlated), so they are wider than a per-row interval would be.
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from portfolio_agent.tools import plan_validation as pv
from portfolio_agent.tools import validation_scorecard as sc
from web.components.validation_grid import grid_html

_MIN_N = pv.MIN_CELL_N
_H_LABELS = {5: "5-Day", 21: "21-Day", 63: "63-Day"}


def _pct(v, digits: int = 0) -> str:
    return f"{v * 100:.{digits}f}%" if isinstance(v, (int, float)) else "—"


def _pts(v) -> str:
    return f"{v * 100:+.1f} pts" if isinstance(v, (int, float)) else "—"


def _ci(lo, hi, fmt=_pct) -> str:
    return f"{fmt(lo)} to {fmt(hi)}" if lo is not None and hi is not None else "interval needs 3+ dates"


@st.cache_data(ttl=600, show_spinner="Scoring plans…")
def _cached_report() -> dict:
    return pv.plan_report()


def edge_headline(edge: dict) -> tuple[str, str]:
    """(headline, plain-words reading) for the edge number."""
    if not edge.get("n") or edge.get("edge") is None:
        return "—", "No matured plans yet."
    lo, hi, e = edge["lo"], edge["hi"], edge["edge"]
    if lo is None:
        return _pts(e), "Too few prediction dates for an interval yet, so this is a preview, not a finding."
    if lo > 0:
        return _pts(e), "The model's calls beat the stock's own odds: the interval is above zero."
    if hi < 0:
        return _pts(e), "The model's calls did worse than the stock's own odds: the interval is below zero."
    return _pts(e), "Indistinguishable from zero: the simulator alone would have been about as good."


def _reliability_chart(rows: list[dict]) -> go.Figure:
    rows = [r for r in rows if r["n"] >= _MIN_N]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=[0, 1], y=[0, 1], mode="lines", line=dict(dash="dash", color="#94A3B8"), name="Perfectly calibrated"))
    if rows:
        fig.add_trace(go.Scatter(
            x=[r["mean_pred"] for r in rows], y=[r["realized"] for r in rows], mode="markers+lines", name="Realized",
            marker=dict(size=[min(8 + r["n"] ** 0.5 * 2, 30) for r in rows], color="#2563EB"),
            error_y=dict(type="data", symmetric=False, array=[(r["hi"] - r["realized"]) if r["hi"] is not None else 0 for r in rows],
                         arrayminus=[(r["realized"] - r["lo"]) if r["lo"] is not None else 0 for r in rows], color="#93C5FD"),
            text=[f"n={r['n']}" for r in rows], hovertemplate="predicted %{x:.0%}<br>realized %{y:.0%}<br>%{text}<extra></extra>"))
    fig.update_layout(height=320, margin=dict(l=10, r=10, t=10, b=10), xaxis_title="Simulated P(target first)",
                      yaxis_title="Realized TARGET rate", xaxis=dict(tickformat=".0%", range=[0, 1]), yaxis=dict(tickformat=".0%", range=[0, 1]),
                      legend=dict(orientation="h", y=-0.25))
    return fig


def _grid_heatmap(grid: dict, value: str = "target_rate") -> go.Figure:
    rows, cols = grid["rows"], grid["cols"]
    cell = {(c["conviction"], c["sim_p"]): c for c in grid["cells"]}
    z, text = [], []
    for r in rows:
        zr, tr = [], []
        for c in cols:
            x = cell[(r, c)]
            show = x["n"] >= _MIN_N and x[value] is not None
            zr.append(x[value] if show else None)
            tr.append(f"{_pct(x['target_rate'])}<br>n={x['n']}" if show else (f"n={x['n']}" if x["n"] else ""))
        z.append(zr)
        text.append(tr)
    fig = go.Figure(go.Heatmap(z=z, x=cols, y=rows, text=text, texttemplate="%{text}", colorscale="RdYlGn", zmin=0, zmax=0.6,
                               hoverongaps=False, colorbar=dict(title="TARGET rate", tickformat=".0%")))
    fig.update_layout(height=300, margin=dict(l=10, r=10, t=10, b=10), xaxis_title="Simulated P(target first)",
                      yaxis_title="Conviction", yaxis=dict(autorange="reversed"))
    return fig


def _table(rows: list[dict], cols: list[tuple[str, str]]) -> None:
    st.dataframe(pd.DataFrame([{label: r.get(key) if r.get("n", _MIN_N) >= _MIN_N else None for key, label in cols} for r in rows]),
                 use_container_width=True, hide_index=True)


def _item(key: str, title: str, value, display: str, lo, hi, verdict_: str, n: int, n_dates: int, reason: str = "") -> sc.ScoreItem:
    return sc.ScoreItem(key=key, title=title, plain_question="", value=value, display=display, ci_low=lo, ci_high=hi, baseline=0.0,
                        baseline_label="", n=n, n_dates=n_dates, verdict=verdict_, one_line_reason=reason, help_text="")


def plan_items(r: dict) -> list[sc.ScoreItem]:
    """The three plan checks for one horizon, judged by the SAME rule as the Predictions grid: no verdict before
    MIN_DATES_FOR_VERDICT prediction dates, and 'Working' / 'Not working' only when the whole 95% range is on one side of zero."""
    nd = r["n_dates"]
    e = r["edge"]
    if not e.get("n") or e.get("edge") is None:
        edge_item = _item("plan_edge", "", None, "—", None, None, sc.TOO_EARLY, 0, nd)
    else:
        edge_item = _item("plan_edge", "", e["edge"], _pts(e["edge"]), e.get("lo"), e.get("hi"),
                          sc.verdict(nd, e.get("lo"), e.get("hi"), 0.0), e["n"], nd)
    b = r["brier"]
    if b.get("skill") is None:
        cal_item = _item("plan_calibration", "", None, "—", None, None, sc.TOO_EARLY, b.get("n", 0), nd)
    else:
        cal_item = _item("plan_calibration", "", b["skill"], f"{b['skill'] * 100:+.0f}%", None, None,
                         sc.verdict(nd, b.get("gain_lo"), b.get("gain_hi"), 0.0), b["n"], nd)
    dg = r["disagreement"]
    if dg["n_disagree"] < _MIN_N or dg["n_confident"] < _MIN_N or dg.get("rate_disagree") is None or dg.get("rate_confident") is None:
        dis_item = _item("plan_disagree", "", None, "—", None, None, sc.TOO_EARLY, dg["n_disagree"], nd)
    else:
        diff = dg["rate_disagree"] - dg["rate_confident"]
        v = sc.TOO_EARLY if nd < sc.MIN_DATES_FOR_VERDICT else (sc.NOT_WORKING if dg["significantly_worse"] else sc.UNPROVEN)
        dis_item = _item("plan_disagree", "", diff, _pts(diff), None, None, v, dg["n_disagree"], nd)
    return [edge_item, cal_item, dis_item]


_PLAN_ROWS = [("plan_edge", "Does the model add edge?"), ("plan_calibration", "Is the simulator's chance right?"),
              ("plan_disagree", "Do doubted calls do worse?")]


def render_plans_simulation() -> None:
    st.markdown("**Plans** are the target / stop levels APEX proposes for a stock, and the **simulator** replays history to estimate the chance "
                "each plan reaches its target. This page checks, once plans have played out, whether those chances and the model's conviction held up.")
    try:
        report = _cached_report()
    except Exception as exc:
        st.error(f"Could not score the plans: {exc}")
        return
    if not report:
        st.info("No plans have played out yet. They are scored after their time stop (5, 21 or 63 trading days).", icon=":material/hourglass_top:")
        return
    horizons = sorted(report)
    best = max(horizons, key=lambda h: (report[h]["n_dates"], report[h]["n"]))
    rb = report[best]

    hl = f"{rb['n_dates']} days of results so far ({_H_LABELS.get(best, f'{best}d')} plans)"
    if rb["n_dates"] < sc.MIN_DATES_FOR_VERDICT:
        st.warning(f"{hl}. Too early to judge; treat the numbers as a preview.", icon=":material/hourglass_top:")
    elif rb["n_dates"] < sc.ESTABLISHED_DATES:
        st.info(f"{hl}. Still early; firm conclusions need {sc.ESTABLISHED_DATES}+ days.", icon=":material/info:")
    else:
        st.success(f"{hl}.", icon=":material/check_circle:")

    items = {h: plan_items(report[h]) for h in horizons}
    cols = [(_H_LABELS.get(h, f"{h}-Day"), f"Plans with a {h}-day time stop. {report[h]['n']} played out so far, over {report[h]['n_dates']} days.")
            for h in horizons]
    rows = [(title, sc.plain_tooltip(items[horizons[0]][i]).split("\n")[0], [items[h][i] for h in horizons]) for i, (_, title) in enumerate(_PLAN_ROWS)]
    st.markdown(grid_html(cols, rows), unsafe_allow_html=True)
    st.caption("Hover any number for what it means and its likely range.")

    with st.expander(f"Details ({_H_LABELS.get(best, f'{best}d')} plans)", expanded=False):
        _details(rb)


def _details(r: dict) -> None:
    edge = r["edge"]
    headline, reading = edge_headline(edge)
    c1, c2, c3 = st.columns(3)
    c1.metric("Edge over the simulator", headline, help="Realized target hits minus the simulator's chance, averaged over plans.")
    c2.metric("Target actually hit", _pct(edge.get("realized"), 1))
    c3.metric("Simulator said", _pct(edge.get("mean_sim"), 1))
    st.caption(f"{reading} 95% range {_ci(edge.get('lo'), edge.get('hi'), _pts)} · {edge['n']} plans.")

    st.markdown("**Is the simulator's chance right?** Dots on the dashed line mean it was.")
    st.plotly_chart(_reliability_chart(r["reliability"]), use_container_width=True)

    st.markdown("**Does higher conviction hit targets more often?**")
    _table([{**c, "target_pct": None if c["target_rate"] is None else round(c["target_rate"] * 100, 1),
             "ci": _ci(c["lo"], c["hi"]) if c["n"] >= _MIN_N else None} for c in r["conviction"]],
           [("band", "Conviction"), ("n", "Plans"), ("target_pct", "Target hit (%)"), ("ci", "95% range")])

    bl = r["blend"]
    if bl["status"] != "ok":
        st.caption(f"Blended chance: not enough played-out plans yet ({bl['n']} of {bl['min_n']}).")
    elif bl["valid"]:
        st.caption("Blended chance: it beats the simulator alone, so it is shown next to the replay on prediction cards.")
    else:
        st.caption("Blended chance: it has not beaten the simulator alone, so it is not shown on prediction cards.")


def render_cap_recommendation_banner(ctx) -> None:
    """The weekly sim check recommends it, a person flips it: one click sets sim_guardrail_mode = cap (admin only)."""
    try:
        from portfolio_agent.tools.engine_settings_db import get_sim_settings
        if get_sim_settings()["sim_guardrail_mode"] == "cap":
            return
        rec = pv.sim_cap_recommendation()
    except Exception:
        return
    if not rec["recommend"]:
        return
    latest = (rec.get("latest") or {}).get("per_horizon", {})
    detail = "; ".join(f"{h}d: {_pct(v['rate_disagree'])} vs {_pct(v['rate_confident'])} (n={v['n_disagree']}/{v['n_confident']}, p={v['p_value']:.3f})"
                       for h, v in sorted(latest.items()) if v.get("significantly_worse") and v.get("p_value") is not None)
    st.warning(f"**Recommend sim_guardrail_mode = cap.** Calls the simulation disagreed with reached their target significantly less often than "
               f"confident calls it agreed with, in {rec['weeks_significant']} of the last {rec['weeks_checked']} weekly reports"
               f"{' (' + detail + ')' if detail else ''}. In cap mode such calls are limited to conviction 5; their raw conviction is kept.",
               icon=":material/policy:")
    if st.button("Switch to cap mode", key="sim_cap_switch"):
        from portfolio_agent.services import investor_service as svc
        try:
            svc.update_sim_settings(ctx, {"sim_guardrail_mode": "cap"}, reason="recommended by the weekly plan validation")
        except (ValueError, RuntimeError, svc.NotAdmin) as exc:
            st.error(str(exc))
        else:
            st.success("sim_guardrail_mode is now cap. New forecasts with a flagged disagreement will be capped.")
            st.rerun()
