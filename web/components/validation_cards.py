"""
Validation dashboard Streamlit card/tile renderers.

Contains:
  - _render_scorecard_tiles      : horizontal metric tiles with CSS hover tooltips, per horizon
  - render_baselines_section     : "Baselines & sample size" body for one horizon
                                   (comparison table, confusion matrix, beat-market, effective N)
"""

from __future__ import annotations

import html
import sys
from pathlib import Path
from typing import Callable, Optional

import pandas as pd
import streamlit as st

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from web.components.validation_charts import (
    _METRIC_CFG, _H_COLORS, _TILE_CSS, _get_tier, relative_accuracy_tier, skill_tier,
)
from web.styles import icon_html, WARNING


def _pct(v: Optional[float], digits: int = 1) -> str:
    return "—" if v is None else f"{v * 100:.{digits}f}%"


def _best_baseline(report: Optional[dict]) -> Optional[dict]:
    return ((report or {}).get("comparison") or {}).get("best_baseline")


def _apex_common_accuracy(report: Optional[dict]) -> Optional[float]:
    """APEX accuracy on the same rows the baselines were scored on (like for like)."""
    rows = ((report or {}).get("comparison") or {}).get("rows") or []
    return rows[0]["accuracy"] if rows else None


def _tile_state(key: str, cfg: dict, row: dict, report: Optional[dict]) -> dict:
    """value / color / tier text / tooltip lines for one tile. Baseline-relative
    tiles (cfg["relative"]) read their context from `report`; the rest use the
    fixed tier tables."""
    rel = cfg.get("relative")
    val = row.get(key)
    extra: list[str] = []

    if rel == "skill":
        blk = ((report or {}).get("probability") or {}).get(cfg["prob"])
        if blk:
            val = blk[cfg["metric"]]
            base = blk[f"baseline_{cfg['metric']}"]
            skill = blk[f"{cfg['metric']}_skill"]
            tier_lbl, color = skill_tier(skill)
            sub = f"{skill:+.1%} vs base {base:.3f}" if skill is not None else "no baseline"
            n = report["probability"]["n"]
            extra = [f"Baseline (always forecast this sample's base rate): {base:.3f}",
                     f"Skill score = 1 − score/baseline: {skill:+.1%}" if skill is not None else "Skill score: n/a",
                     f"Rows with a full distribution: {n}"]
            if cfg["prob"] == "binary":
                extra.append(f"UP rate in these rows: {_pct(blk['base_rate'])}")
            return dict(val=val, color=color, tier=tier_lbl, sub=sub, extra=extra)
        if val is None:
            return dict(val=None, color="#475569", tier="No data", sub="No data", extra=[])
        return dict(val=val, color="#64748B", tier="No baseline", sub="no baseline", extra=[])

    if rel == "accuracy":
        if val is None:
            return dict(val=None, color="#475569", tier="No data", sub="No data", extra=[])
        best = _best_baseline(report)
        comp = (report or {}).get("comparison") or {}
        if not best or not comp.get("n_common"):
            return dict(val=val, color="#64748B", tier="No baseline", sub="no baseline", extra=[])
        # directional_accuracy: compare like for like (APEX on the baselines' common rows);
        # hi/lo-conviction subsets are compared against the same horizon-wide baseline.
        ref_acc = _apex_common_accuracy(report) if key == "directional_accuracy" else val
        tier_lbl, color = relative_accuracy_tier(ref_acc, best["accuracy"])
        top = sorted((r for r in comp["rows"] if r["key"] != "apex" and r["accuracy"] is not None),
                     key=lambda r: -r["accuracy"])[:3]
        extra = [f"Best naive baseline: {html.escape(best['name'])} {_pct(best['accuracy'])}",
                 f"APEX on the same {comp['n_common']} rows: {_pct(_apex_common_accuracy(report))}"]
        extra += [f"{html.escape(r['name'])}: {_pct(r['accuracy'])}" for r in top if r["key"] != best["key"]]
        return dict(val=val, color=color, tier=tier_lbl,
                    sub=f"{tier_lbl.split('(')[0].strip()} · best {_pct(best['accuracy'])}", extra=extra)

    if val is None:
        return dict(val=None, color="#475569", tier="No data", sub="No data", extra=[])
    tier_lbl, color = _get_tier(key, val)
    return dict(val=val, color=color, tier=tier_lbl, sub=tier_lbl.split("(")[0].strip(), extra=[])


def _render_scorecard_tiles(
    by_horizon: dict,
    all_horizons: list,
    horizon_labels: dict,
    reports: Optional[dict] = None,
    after_horizon: Optional[Callable[[int], None]] = None,
) -> None:
    """Render one row of metric tiles per horizon (CSS hover tooltips).
    `reports` is {horizon: validation_metrics.horizon_report} for the
    baseline-relative tiles; `after_horizon(h)` is called right after each
    horizon's row so the page can attach the "Baselines & sample size" section."""
    reports = reports or {}
    metric_keys = list(_METRIC_CFG.keys())

    for i, h_days in enumerate(all_horizons):
        row    = by_horizon[h_days]
        report = reports.get(h_days)
        n      = row.get("num_predictions") or 0
        hlbl   = horizon_labels.get(h_days, f"{h_days}d")
        hcolor = _H_COLORS.get(h_days, "#64748B")
        if n == 0:
            n_label = "no evaluated predictions yet"
            n_warn  = ""
        elif n < 30:
            n_label = f"{n} evaluated predictions"
            n_warn  = f"  {icon_html('warning', 12, color=WARNING)} fewer than 30 — metrics unreliable"
        else:
            n_label = f"{n} evaluated predictions"
            n_warn  = ""

        out = _TILE_CSS if i == 0 else ""
        out += (
            f'<div class="sc-wrap">'
            f'<div class="sc-horizon-hdr" style="color:{hcolor};background:{hcolor}18;border-left:3px solid {hcolor}">'
            f'{hlbl} Horizon &nbsp;·&nbsp; {n_label}{n_warn}</div>'
            f'<div class="sc-row">'
        )

        for key in metric_keys:
            cfg = _METRIC_CFG[key]
            short_label = cfg["label"].split("  ↓")[0].split("  (")[0]
            st_ = _tile_state(key, cfg, row, report)
            color = st_["color"]
            val_text = "—" if st_["val"] is None else cfg["fmt"](st_["val"])

            range_rows = "".join(
                f'<div class="tip-tier{" cur" if lbl == st_["tier"] else ""}">'
                f'<span style="color:{tc}">●</span> {lbl}</div>'
                for _, tc, lbl in cfg["tiers"]
            )
            lower_note = " &nbsp;↓ lower is better" if not cfg["higher"] else ""
            extra_html = "".join(f'<div class="tip-tier cur">{line}</div>' for line in st_["extra"])
            ref_html = (
                f'<div class="tip-ref">{cfg["ref_label"]}</div>' if cfg.get("ref_label") else ""
            )
            tip = (
                f'<div class="sc-tip">'
                f'<div class="tip-title">{cfg["label"]}</div>'
                f'<div class="tip-val" style="color:{color}">{val_text}{lower_note}</div>'
                f'<div class="tip-defn">{cfg["defn"]}</div>'
                f'{extra_html}'
                f'<div class="tip-ranges-hdr" style="margin-top:6px">Tiers:</div>'
                f'{range_rows}{ref_html}</div>'
            )
            out += (
                f'<div class="sc-tile">'
                f'<div class="sc-tile-bar" style="background:{color}"></div>'
                f'<div class="sc-tile-label">{short_label}</div>'
                f'<div class="sc-tile-value" style="color:{color}">{val_text}</div>'
                f'<div class="sc-tile-tier">{st_["sub"]}</div>'
                f'{tip}</div>'
            )

        out += '</div></div>'
        st.markdown(out, unsafe_allow_html=True)
        if after_horizon is not None:
            after_horizon(h_days)


# ── Baselines & sample size ───────────────────────────────────────────────────

_COMPARISON_COLUMNS = {"name": "Predictor", "accuracy": "Accuracy", "balanced_accuracy": "Balanced acc",
                       "macro_f1": "Macro-F1", "n": "n"}


def _comparison_df(comp: dict) -> pd.DataFrame:
    rows = [{
        "Predictor": r["name"],
        "Accuracy": _pct(r["accuracy"]), "Balanced acc": _pct(r["balanced_accuracy"]),
        "Macro-F1": "—" if r["macro_f1"] is None else f"{r['macro_f1']:.3f}",
        "n": r["n"],
    } for r in comp.get("rows", [])]
    return pd.DataFrame(rows, columns=list(_COMPARISON_COLUMNS.values()))


def _comparison_block(title: str, comp: dict) -> None:
    st.markdown(f"**{title}**")
    if not comp.get("n_common"):
        st.caption("No rows where every available baseline is defined.")
        return
    st.dataframe(_comparison_df(comp), hide_index=True, use_container_width=True)
    note = f"Common subset: n = {comp['n_common']} of {comp['n_total']} rows."
    if comp.get("unavailable"):
        note += " Unavailable (no price data): " + ", ".join(comp["unavailable"]) + "."
    st.caption(note)


def render_baselines_section(report: Optional[dict], horizon_label: str) -> None:
    """Body of the per-horizon "Baselines & sample size" expander."""
    with st.expander(f"Baselines & sample size — {horizon_label}", expanded=False):
        if not report or not report.get("comparison", {}).get("n_total"):
            st.caption("No evaluated predictions with a usable direction and return at this horizon yet.")
            return

        eff = report["effective_sample"]
        streak = "—" if eff.get("streak_n") is None else eff["streak_n"]
        line = (f"**Effective sample:** raw N = {eff['raw_n']} / streak N = {streak} / "
                f"non-overlapping N = {eff['nonoverlap_n']}")
        if eff["nonoverlap_accuracy"] is not None:
            line += (f" — directional accuracy on the non-overlapping rows {_pct(eff['nonoverlap_accuracy'])} "
                     f"(Wilson 95% CI {_pct(eff['ci_low'])} – {_pct(eff['ci_high'])})")
        st.markdown(line)
        if eff["low_sample"]:
            st.warning(
                f"Only {eff['nonoverlap_n']} non-overlapping predictions (< 30): treat every number here as "
                "anecdotal. Overlapping windows on the same ticker are one observation counted many times, and "
                "different tickers still share market moves.",
                icon=":material/warning:",
            )

        c1, c2 = st.columns(2)
        with c1:
            _comparison_block("All evaluated rows", report["comparison"])
        with c2:
            _comparison_block("Non-overlapping rows only", report["nonoverlap_comparison"])
        st.caption(
            "Baselines are scored on exactly the rows shown in n. Majority class is an in-sample oracle (it peeks "
            "at the most common actual class). SPY same window is a look-ahead diagnostic of how much the market "
            "explains — not tradable and excluded from the 'best baseline' used for tile tiers. SPY prior window "
            "and ticker momentum use the return over the prior horizon-length of trading days."
        )

        cm = report["confusion"]
        st.markdown("**Predicted vs actual direction** (rows = predicted, columns = actual)")
        cm_df = pd.DataFrame(cm["matrix"]).T[cm["labels"]]
        cm_df.index = [f"Pred {c}" for c in cm_df.index]
        cm_df.columns = [f"Actual {c}" for c in cm_df.columns]
        mix = pd.DataFrame({
            "Predicted mix": {c: _pct(cm["predicted_mix"][c]) for c in cm["labels"]},
            "Actual mix": {c: _pct(cm["actual_mix"][c]) for c in cm["labels"]},
        })
        m1, m2 = st.columns([3, 2])
        m1.dataframe(cm_df, use_container_width=True)
        m2.dataframe(mix, use_container_width=True)

        bm = report["beat_market"]
        st.markdown("**Direction vs SPY** — did the call's sign match excess return over SPY?")
        if not bm["n_rows"]:
            st.caption("No rows with an excess return.")
        else:
            def _row(name, n, hit, mean_exc):
                return {"Calls": name, "n": n, "Beat-market hit rate": _pct(hit),
                        "Mean excess return": "—" if mean_exc is None else f"{mean_exc * 100:+.2f}%"}
            bm_rows = [
                _row("UP calls (excess > 0)", bm["up"]["n"], bm["up"]["hit_rate"], bm["up"]["mean_excess"]),
                _row("DOWN calls (excess < 0)", bm["down"]["n"], bm["down"]["hit_rate"], bm["down"]["mean_excess"]),
                _row("UP + DOWN calls", bm["n_calls"], bm["hit_rate"], None),
                _row("Baseline: always UP (all rows)", bm["n_rows"], bm["always_up_hit_rate"], None),
            ]
            st.dataframe(pd.DataFrame(bm_rows), hide_index=True, use_container_width=True)
            if bm["flat"]["n"]:
                st.caption(f"FLAT calls (not scored): n = {bm['flat']['n']}, mean excess "
                           f"{bm['flat']['mean_excess'] * 100:+.2f}%.")
