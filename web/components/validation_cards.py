"""
Validation dashboard Streamlit card/tile renderers.

Contains:
  - _render_scorecard_tiles      : horizontal metric tiles with CSS hover tooltips, per horizon
  - _headline_tiles_html         : date-aware ranking-skill tiles (AUC, rank IC, excess by label) -- the headline row
  - render_ranking_section       : "Ranking detail & model split" expander for one horizon
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
    _METRIC_CFG, _H_COLORS, _TILE_CSS, _get_tier, relative_accuracy_tier, skill_tier, interval_tier,
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


def _sgn(v: Optional[float], digits: int = 2) -> str:
    return "—" if v is None else f"{v * 100:+.{digits}f}%"


def _ci_txt(lo: Optional[float], hi: Optional[float], fmt) -> str:
    return "CI n/a" if lo is None or hi is None else f"95% CI {fmt(lo)} to {fmt(hi)}"


def _tile_html(label: str, val_text: str, color: str, sub: str, title: str, defn: str, lines: list[str], tiers: str = "") -> str:
    lines_html = "".join(f'<div class="tip-tier cur">{html.escape(x)}</div>' for x in lines)
    tip = (f'<div class="sc-tip"><div class="tip-title">{html.escape(title)}</div>'
           f'<div class="tip-val" style="color:{color}">{val_text}</div>'
           f'<div class="tip-defn">{defn}</div>{lines_html}{tiers}</div>')
    return (f'<div class="sc-tile"><div class="sc-tile-bar" style="background:{color}"></div>'
            f'<div class="sc-tile-label">{label}</div>'
            f'<div class="sc-tile-value" style="color:{color}">{val_text}</div>'
            f'<div class="sc-tile-tier">{html.escape(sub)}</div>{tip}</div>')


_INTERVAL_TIERS_HTML = (
    '<div class="tip-ranges-hdr" style="margin-top:6px">Reading the interval:</div>'
    + "".join(f'<div class="tip-tier"><span style="color:{c}">●</span> {t}</div>' for c, t in (
        ("#059669", "Above chance: the whole 95% CI is on the good side of no-skill"),
        ("#EAB308", "Not distinguishable: the CI contains the no-skill value"),
        ("#EF4444", "Below chance: the whole CI is on the bad side"),
    ))
    + '<div class="tip-ref">Intervals resample whole dates (date-block bootstrap), because names scored on the '
      'same day share one market move.</div>'
)


def _headline_tiles_html(report: Optional[dict], hlbl: str, hcolor: str) -> str:
    """The ranking-skill row: does a higher p_up / composite go with beating SPY, and do UP-labelled names beat
    FLAT-labelled ones? Every interval is a date-block bootstrap; every tile says how many distinct dates stand behind it."""
    rk = (report or {}).get("ranking") or {}
    head = (f'<div class="sc-wrap"><div class="sc-horizon-hdr" style="color:{hcolor};background:{hcolor}18;'
            f'border-left:3px solid {hcolor}">{hlbl} · Headline: ranking skill vs SPY (date-aware)</div>')
    if not rk or not rk.get("n_rows"):
        return head + '<div class="tip-defn" style="padding:6px 2px">No evaluated predictions with an excess return yet.</div></div>'
    warn = ""
    if rk.get("low_dates"):
        warn = (f'<div style="font-size:.74rem;color:#B45309;margin:0 0 8px">⚠ Only {rk["n_dates"]} distinct prediction '
                f'dates — a date-block bootstrap over so few dates gives crude intervals. Read the sign and size, not the decimals.</div>')

    tiles = []
    auc_defn = ('AUC of the score against "beat SPY" (excess return &gt; 0): the chance that a name that beat SPY was '
                'scored higher than one that did not. 0.5 = no skill, 1 = perfect.')
    for key, name in (("p_up", "p_up"), ("composite", "composite")):
        a = (rk.get("auc") or {}).get(key) or {}
        if a.get("auc") is None:
            tiles.append(_tile_html(f"AUC · {name}", "—", "#475569", "no data", f"AUC · {name} vs beat SPY", auc_defn, []))
            continue
        wd = a.get("within_date") or {}
        # Both views must agree: the pooled AUC can be flattered by dates differing in base rate or score level;
        # the within-date AUC cannot. The tier is only as good as the weaker of the two.
        t_pool = interval_tier(a["ci_low"], a["ci_high"], 0.5)
        t_within = interval_tier(wd.get("ci_low"), wd.get("ci_high"), 0.5) if wd.get("mean") is not None else t_pool
        lbl, col = t_pool if t_pool == t_within else ("Not distinguishable from chance", "#EAB308")
        lines = [lbl + " — needs the pooled AND within-date intervals to agree",
                 f"Pooled over {a['n']} rows, {a['n_pos']} beat SPY, {a['n_dates']} dates",
                 _ci_txt(a["ci_low"], a["ci_high"], lambda v: f"{v:.3f}")]
        if wd.get("mean") is not None:
            lines.append(f"Within-date mean AUC {wd['mean']:.3f} ({_ci_txt(wd['ci_low'], wd['ci_high'], lambda v: f'{v:.3f}')}, "
                         f"{wd['n_dates']} dates) — removes date-to-date differences in how many names beat SPY")
        tiles.append(_tile_html(f"AUC · {name}", f"{a['auc']:.3f}", col, f"{lbl.split('(')[0].strip()} · {a['n_dates']} dates",
                                f"AUC · {name} vs beat SPY", auc_defn, lines, _INTERVAL_TIERS_HTML))

    ic_defn = ('Spearman rank correlation between the score and excess return, computed across all names predicted on the same '
               'date, then averaged over dates. 0 = no skill. Dates with fewer than 10 names are dropped.')
    for key, name in (("p_up", "p_up"), ("composite", "composite")):
        ic = (rk.get("rank_ic") or {}).get(key) or {}
        if ic.get("mean") is None:
            tiles.append(_tile_html(f"Rank IC · {name}", "—", "#475569", "no data", f"Rank IC · {name}", ic_defn, []))
            continue
        lbl, col = interval_tier(ic["ci_low"], ic["ci_high"], 0.0)
        lines = [f"Averaged over {ic['n_dates']} dates ({ic['n_rows']} rows); {ic['n_dates_dropped']} thin date(s) dropped",
                 _ci_txt(ic["ci_low"], ic["ci_high"], lambda v: f"{v:+.3f}"),
                 f"Positive on {ic['share_positive'] * 100:.0f}% of dates"]
        tiles.append(_tile_html(f"Rank IC · {name}", f"{ic['mean']:+.3f}", col, f"{lbl.split('(')[0].strip()} · {ic['n_dates']} dates",
                                f"Rank IC · {name} vs excess return", ic_defn, lines, _INTERVAL_TIERS_HTML))

    le = rk.get("label_excess") or {}
    ex_defn = "Mean return minus SPY's return over the same window, for names APEX labelled this way."
    for lab in ("UP", "FLAT", "DOWN"):
        x = (le.get("labels") or {}).get(lab) or {}
        if not x.get("n"):
            tiles.append(_tile_html(f"Excess · {lab} calls", "—", "#475569", "no calls", f"Mean excess · {lab} calls", ex_defn, []))
            continue
        lbl, col = interval_tier(x["ci_low"], x["ci_high"], 0.0)
        lines = [lbl, f"n = {x['n']} calls on {x['n_dates']} distinct dates", _ci_txt(x["ci_low"], x["ci_high"], _sgn)]
        tiles.append(_tile_html(f"Excess · {lab} calls", _sgn(x["mean"]), col, f"n={x['n']} · {x['n_dates']} dates",
                                f"Mean excess · {lab} calls", ex_defn, lines, _INTERVAL_TIERS_HTML))

    d = (le.get("diffs") or {}).get("UP-FLAT") or {}
    if d.get("diff") is not None:
        lbl, col = interval_tier(d["ci_low"], d["ci_high"], 0.0)
        tiles.append(_tile_html("UP − FLAT excess", _sgn(d["diff"]), col, f"{lbl.split('(')[0].strip()} · {le['n_dates']} dates",
                                "UP minus FLAT mean excess", "Pooled: how much better UP-labelled names did than FLAT-labelled ones.",
                                [lbl, _ci_txt(d["ci_low"], d["ci_high"], _sgn)], _INTERVAL_TIERS_HTML))
    wt = rk.get("within_ticker") or {}
    if wt.get("mean_diff") is not None:
        lbl, col = interval_tier(wt["ci_low"], wt["ci_high"], 0.0)
        tiles.append(_tile_html("UP − FLAT, same ticker", _sgn(wt["mean_diff"]), col, f"{wt['n_tickers']} tickers · {lbl.split('(')[0].strip()}",
                                "UP minus FLAT, within ticker",
                                "Only tickers that had both an UP and a FLAT call: each ticker's UP mean minus its own FLAT mean, "
                                "averaged. A stock that is simply a steady winner or loser cancels out, which removes the composition effect.",
                                [lbl, f"{wt['n_tickers']} tickers, {wt['n_obs']} calls", _ci_txt(wt["ci_low"], wt["ci_high"], _sgn)],
                                _INTERVAL_TIERS_HTML))
    else:
        tiles.append(_tile_html("UP − FLAT, same ticker", "—", "#475569", "no tickers with both", "UP minus FLAT, within ticker",
                                "Needs tickers that had both an UP and a FLAT call.", []))
    return head + warn + '<div class="sc-row">' + "".join(tiles) + '</div></div>'


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
        out += _headline_tiles_html(report, hlbl, hcolor)
        out += (
            f'<div class="sc-wrap">'
            f'<div class="sc-horizon-hdr" style="color:{hcolor};background:{hcolor}18;border-left:3px solid {hcolor}">'
            f'{hlbl} Horizon &nbsp;·&nbsp; Secondary: 3-class accuracy, calibration &nbsp;·&nbsp; {n_label}{n_warn}</div>'
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


# ── Ranking detail & model split ──────────────────────────────────────────────

def _fmt3(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:.3f}"


def render_ranking_section(report: Optional[dict], horizon_label: str) -> None:
    """Body of the per-horizon "Ranking detail & model split" expander."""
    with st.expander(f"Ranking detail, model split & FLAT diagnostic — {horizon_label}", expanded=False):
        rk = (report or {}).get("ranking") or {}
        if not rk.get("n_rows"):
            st.caption("No evaluated predictions with an excess return at this horizon yet.")
            return
        if rk.get("low_dates"):
            st.warning(f"Only {rk['n_dates']} distinct prediction dates behind these numbers. Intervals resample whole dates, "
                       "so with this few they are crude.", icon=":material/warning:")

        def _line(name, m, fmt):
            return {"Metric": name, "Value": fmt(m.get("mean", m.get("auc"))),
                    "95% CI": "—" if m.get("ci_low") is None else f"{fmt(m['ci_low'])} to {fmt(m['ci_high'])}",
                    "Dates": m.get("n_dates", "—")}
        rows = []
        for key in ("p_up", "composite"):
            a = rk["auc"].get(key) or {}
            if a.get("auc") is not None:
                rows.append(_line(f"AUC {key} vs beat SPY (pooled)", {"auc": a["auc"], "ci_low": a["ci_low"], "ci_high": a["ci_high"], "n_dates": a["n_dates"]}, _fmt3))
                if (a.get("within_date") or {}).get("mean") is not None:
                    rows.append(_line(f"AUC {key}, within-date mean", a["within_date"], _fmt3))
            ic = rk["rank_ic"].get(key) or {}
            if ic.get("mean") is not None:
                rows.append(_line(f"Rank IC {key} (daily, averaged)", ic, lambda v: f"{v:+.3f}"))
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

        le = rk["label_excess"]
        lab_rows = [{"Label": lab, "n": x["n"], "Dates": x["n_dates"], "Mean excess vs SPY": _sgn(x["mean"]),
                     "95% CI": "—" if x["ci_low"] is None else f"{_sgn(x['ci_low'])} to {_sgn(x['ci_high'])}"}
                    for lab, x in le["labels"].items()]
        lab_rows += [{"Label": k.replace("-", " − "), "n": None, "Dates": le["n_dates"], "Mean excess vs SPY": _sgn(v["diff"]),
                      "95% CI": "—" if v["ci_low"] is None else f"{_sgn(v['ci_low'])} to {_sgn(v['ci_high'])}"}
                     for k, v in le["diffs"].items()]
        st.markdown("**Mean excess return by APEX label** (date-block bootstrap)")
        st.dataframe(pd.DataFrame(lab_rows), hide_index=True, use_container_width=True)

        wt = rk.get("within_ticker") or {}
        if wt.get("mean_diff") is not None:
            st.caption(f"Within ticker, UP − FLAT: {_sgn(wt['mean_diff'])} ({_ci_txt(wt['ci_low'], wt['ci_high'], _sgn)}) "
                       f"across {wt['n_tickers']} tickers that had both calls ({wt['n_obs']} calls).")
        else:
            st.caption("Within-ticker UP − FLAT: no ticker has both an UP and a FLAT call yet.")

        bm = rk.get("by_model") or {}
        if bm:
            st.markdown("**By model**")
            mrows = []
            for name, m in sorted(bm.items(), key=lambda kv: -kv[1]["n_rows"]):
                ex = m["label_excess"]["labels"]
                mrows.append({
                    "Model": name, "n": m["n_rows"], "Dates": m["n_dates"],
                    "AUC p_up": _fmt3(m["auc"]["p_up"].get("auc")),
                    "Rank IC p_up": "—" if m["rank_ic"]["p_up"].get("mean") is None else f"{m['rank_ic']['p_up']['mean']:+.3f}",
                    "Rank IC composite": "—" if m["rank_ic"]["composite"].get("mean") is None else f"{m['rank_ic']['composite']['mean']:+.3f}",
                    "UP excess": _sgn(ex["UP"]["mean"]), "FLAT excess": _sgn(ex["FLAT"]["mean"]), "DOWN excess": _sgn(ex["DOWN"]["mean"]),
                })
            st.dataframe(pd.DataFrame(mrows), hide_index=True, use_container_width=True)
            st.caption("Models with a handful of rows (or a single date) cannot support a rank correlation — their cells are empty or noisy by construction.")

        fd = (report or {}).get("flat_diagnostic") or {}
        fc = fd.get("flat_calls")
        if fc and fc["n"]:
            st.markdown("**FLAT diagnostic — does the FLAT label agree with APEX's own distribution?**")
            st.markdown(
                f"Of {fc['n']} FLAT calls, p_flat is the largest of the five buckets in {fc['p_flat_strict_max']} "
                f"({fc['p_flat_strict_max'] / fc['n'] * 100:.0f}%), tied for largest in {fc['p_flat_tied_max']}, and not the largest in "
                f"{fc['p_flat_not_max']}. Mean p_flat on those calls is {_pct(fc['mean_p_flat'])} against {_pct(fc['mean_p_up'])} up mass "
                f"and {_pct(fc['mean_p_down'])} down mass.")
            rr = [{"Direction derived from the probabilities": {"argmax5": "Largest of the 5 buckets", "mass3": "Largest of down / flat / up mass"}[k],
                   "Rows (ties excluded)": v["n"], "Agrees with APEX label": _pct(v["agrees_with_label"]),
                   "Derived accuracy": _pct(v["derived_accuracy"]), "APEX accuracy, same rows": _pct(v["apex_accuracy_same_rows"])}
                  for k, v in fd["derived_rules"].items() if v["n"]]
            st.dataframe(pd.DataFrame(rr), hide_index=True, use_container_width=True)
            cal = fd["calibration"]
            st.caption(f"Calibration of the FLAT bucket: APEX puts a mean {_pct(cal['mean_p_flat_all_rows'])} on p_flat across all rows; "
                       f"{_pct(cal['actual_flat_rate'])} of outcomes actually landed within ±1%.")


