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


def render_plans_simulation() -> None:
    st.markdown("**How the plans actually turned out, and whether the history replay or the model's conviction predicted it better.** "
                "A plan ends at its TARGET, its STOP (a day touching both counts as the stop) or its time stop (TIMEOUT).")
    try:
        report = _cached_report()
    except Exception as exc:
        st.error(f"Could not score the plans: {exc}")
        return
    if not report:
        st.info("No plans have matured yet. A plan matures when its time stop passes (5, 21 or 63 trading days after the forecast); the "
                "evening batch then records what happened.", icon=":material/hourglass_top:")
        return
    horizons = sorted(report)
    default_h = max(horizons, key=lambda h: report[h]["n"])
    h = st.radio("Horizon", horizons, index=horizons.index(default_h), horizontal=True, key="plan_val_horizon",
                 format_func=lambda x: _H_LABELS.get(x, f"{x}d"))
    r = report[h]
    st.caption(f"{r['n']} matured plans across {r['n_dates']} prediction dates. Horizons are never mixed.")

    # c) the key number
    edge = r["edge"]
    headline, reading = edge_headline(edge)
    st.markdown("##### Does the model add edge beyond the simulator?")
    c1, c2, c3 = st.columns([1.4, 1, 1])
    c1.metric("Edge over the simulator", headline, help="Mean of (realized TARGET hit minus the simulator's P(target first)) over every matured plan.")
    c1.caption(f"95% interval {_ci(edge.get('lo'), edge.get('hi'), _pts)} · n={edge['n']}")
    c2.metric("Realized TARGET rate", _pct(edge.get("realized"), 1), help="How often the target really came first.")
    c3.metric("Simulator said", _pct(edge.get("mean_sim"), 1), help="The average P(target first) the simulator gave.")
    st.info(reading, icon=":material/insights:")
    zd = edge.get("vs_zero_drift") or {}
    st.caption(f"Against the no-edge baseline (the stock just wiggles, no drift): {_pts(zd.get('edge'))}, interval "
               f"{_ci(zd.get('lo'), zd.get('hi'), _pts)}.")
    st.markdown("**Edge by conviction band**")
    _table([{**b, "edge_pts": None if b["edge"] is None else round(b["edge"] * 100, 1),
             "ci": _ci(b["lo"], b["hi"], _pts) if b["n"] >= _MIN_N else None} for b in edge["by_band"]],
           [("band", "Conviction"), ("n", "n"), ("edge_pts", "Edge (pts)"), ("ci", "95% interval")])

    # a) simulator calibration
    st.markdown("##### Is the simulator calibrated?")
    left, right = st.columns([1.6, 1])
    with left:
        st.plotly_chart(_reliability_chart(r["reliability"]), use_container_width=True)
        st.caption("Dots on the dashed line mean 'when it said 30%, the target came first about 30% of the time'. Bars are 95% intervals; "
                   f"bins with fewer than {_MIN_N} plans are hidden.")
    with right:
        b = r["brier"]
        st.metric("Brier score (3 outcomes)", f"{b['brier_sim']:.3f}" if b.get("brier_sim") is not None else "—",
                  help="Lower is better. 0 is perfect; the base-rate forecast below is the yardstick.")
        st.caption(f"Base-rate Brier {b['brier_base']:.3f} · skill {_pct(b.get('skill'))} · n={b['n']}" if b.get("brier_base") is not None else "")
        if b.get("gain_lo") is not None:
            st.caption(f"Gain over base rate, 95% interval: {b['gain_lo']:+.3f} to {b['gain_hi']:+.3f}")

    # b) conviction calibration
    st.markdown("##### Is the model's conviction calibrated?")
    _table([{**c, "target_pct": None if c["target_rate"] is None else round(c["target_rate"] * 100, 1),
             "mean_r": None if c["mean_r"] is None else round(c["mean_r"], 2),
             "ci": _ci(c["lo"], c["hi"]) if c["n"] >= _MIN_N else None} for c in r["conviction"]],
           [("band", "Conviction"), ("n", "n"), ("target_pct", "TARGET rate (%)"), ("ci", "95% interval"), ("mean_r", "Mean realized R")])

    # d) agreement
    st.markdown("##### When they disagree, who was right?")
    dg = r["disagreement"]
    d1, d2, d3 = st.columns(3)
    d1.metric("Sim disagrees", _pct(dg["rate_disagree"]) if dg["n_disagree"] >= _MIN_N else "n < 10",
              help="Confident calls where the simulator said the target is rare: how often the target actually came first.")
    d1.caption(f"n={dg['n_disagree']}")
    d2.metric("Both confident", _pct(dg["rate_confident"]) if dg["n_confident"] >= _MIN_N else "n < 10",
              help="Confident calls where the simulator also gave the target a decent chance.")
    d2.caption(f"n={dg['n_confident']}")
    d3.metric("Difference p-value", f"{dg['p_value']:.3f}" if dg["p_value"] is not None else "—",
              help="Two-proportion test. Rows from the same day are correlated, so treat this as generous.")
    d3.caption("significantly worse" if dg["significantly_worse"] else "not significant")
    g1, g2 = st.columns([1.2, 1])
    with g1:
        st.plotly_chart(_grid_heatmap(r["grid4"]), use_container_width=True)
        st.caption(f"Realized TARGET rate by conviction band and simulated probability band. Cells with n < {_MIN_N} are blank.")
    with g2:
        cells = r["grid2"]
        rows2 = []
        for key, label in (("confident|sim_agrees", "Confident · sim agrees"), ("confident|sim_disagrees", "Confident · sim disagrees"),
                           ("not_confident|sim_agrees", "Not confident · sim agrees"), ("not_confident|sim_disagrees", "Not confident · sim disagrees")):
            c = cells[key]
            ok = c["n"] >= _MIN_N
            rows2.append({"Cell": label, "n": c["n"], "TARGET rate": _pct(c["target_rate"]) if ok else "—",
                          "Mean R": round(c["mean_r"], 2) if ok and c["mean_r"] is not None else None})
        st.dataframe(pd.DataFrame(rows2), use_container_width=True, hide_index=True)

    # e) blend
    st.markdown("##### Blended probability")
    bl = r["blend"]
    if bl["status"] != "ok":
        st.info(f"Not enough matured plans yet ({bl['n']} of {bl['min_n']}) to learn a blend for this horizon.", icon=":material/hourglass_top:")
    else:
        st.caption(f"Fitted on {bl['n']} matured plans. Brier (out of sample) blend {bl['brier_blend']:.4f} vs simulator alone {bl['brier_sim_only']:.4f}.")
        if not bl["valid"]:
            st.warning("The blend has not beaten the simulator alone, so it is not shown on prediction cards.", icon=":material/block:")
        else:
            st.success("The blend beats the simulator alone, so it is shown next to the replay on prediction cards.", icon=":material/check_circle:")
        conv, sim = bl["pts_per_conviction"], bl["pts_per_10pts_sim"]
        st.markdown(f"- Conviction adds **{conv:+.1f} points** of target probability per point of conviction, beyond what the simulator already knew."
                    f"{' (about nothing)' if abs(conv) < 0.5 else ''}\n"
                    f"- Every +10 points of simulated probability adds **{sim:+.1f} points** beyond what conviction already said."
                    f"{' (about nothing)' if abs(sim) < 1.0 else ''}")


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
