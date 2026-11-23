"""
APEX Prediction Validation — the user-facing trust page: "is this reliable?"

One screen, no filters: Portfolio, Watchlist and Opportunities side by side so they can be compared. For each horizon a grid of
the four checks (rows) by group (columns), then two comparison charts. Last 90 days, champion/fallback predictions only. Hover any number for a plain-English
meaning and its likely range. Then two charts for the horizon with the most evidence. Every number comes from
portfolio_agent.tools.validation_scorecard.compute_scorecard; this file only lays it out.

Everything that used to be here beyond that — the nine-tile metric grid, outcome breakdown, the follow-every-BUY index,
baselines, confusion matrix, ranking detail, model split, FLAT diagnostic, the scoring-snapshot tables — lives on the
admin-only Validation QA page.

Portfolio and Watchlist are the viewer's own tickers, so nothing here lets one viewer infer what somebody else holds or
watches. Opportunities are market-only shared forecasts on tickers APEX discovered, identical for every viewer. Legacy random-model predictions and challenger shadow rows never appear (see Validation QA).
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

import pandas as pd
import streamlit as st
from web.styles import inject_global_css, top_nav, material, fmt_pct, fmt_money

from web.auth import current_context, require_login

st.set_page_config(
    page_title="APEX — Validation",
    page_icon=str(_ROOT / "web" / "static" / "apex_mark.png"),
    layout="wide",
    initial_sidebar_state="collapsed",
)
inject_global_css()
require_login()
top_nav("validation")

from portfolio_agent.tools.prediction_db import (
    CURRENT_SYSTEM_VERSION, ROLE_LABELS, VALIDATION_ROLES,
)
from portfolio_agent.tools import validation_scorecard as scorecard_mod
from web.data.validation import (
    _load_portfolio_tickers,
    _load_watchlist_tickers,
    _load_evaluated_predictions_df,
    build_scorecards,
)
from web.components.validation_grid import grid_html
from web.components.validation_charts import (
    build_excess_by_group_chart,
    build_rank_trend_compare_chart,
)

# ── Page header ───────────────────────────────────────────────────────────────

st.title(f"{material('track_changes')} APEX Prediction Validation")
st.caption(f"System version: `{CURRENT_SYSTEM_VERSION}` · Scores predictions after they mature · Detailed diagnostics: Validation QA (admin)")

from web.components.plan_validation_view import render_cap_recommendation_banner, render_plans_simulation

render_cap_recommendation_banner(current_context("web:validation"))
_view = st.segmented_control("Section", ["Predictions", "Plans & Simulation"], default="Predictions", key="val_section",
                             label_visibility="collapsed")
if _view == "Plans & Simulation":
    render_plans_simulation()
    st.stop()

# ── Three sections: the viewer's portfolio, the viewer's watchlist, and the opportunities APEX found on its own ──
# Last 90 days, champion/fallback predictions. Portfolio and watchlist are the viewer's OWN tickers; opportunities are
# market-only shared forecasts on tickers APEX discovered (trending headlines, the morning screen), the same for everyone.
lookback = 90
_roles = VALIDATION_ROLES

# Scoped to the viewer's OWN holdings/watchlist, not the shared union across every user.
_ctx = current_context("web:validation")
_portfolio_tickers = _load_portfolio_tickers(_ctx)
_watchlist_tickers = _load_watchlist_tickers(_ctx)

# ── Data + scorecards ─────────────────────────────────────────────────────────

# Price-based baselines need one price fetch per ticker: automatic for a normal-size list, skipped for a very large one.
_PRICE_AUTO_MAX_TICKERS = 60


@st.cache_data(ttl=600, show_spinner="Scoring…")
def _cached_scorecards(df: pd.DataFrame, use_prices: bool) -> dict:
    return build_scorecards(df, use_prices=use_prices)


_H_LABELS = {5: "5-Day", 21: "21-Day", 63: "63-Day", 250: "250-Day"}
_ROW_TITLES = {
    "ranking": "Do BUY picks win?",
    "direction": "Is the direction right?",
    "probabilities": "Are the chances good?",
    "ranges": "Does it land in range?",
}
# (group label, loader segment, one-line description)
_GROUPS = [
    ("Portfolio", "Portfolio", "the tickers you hold"),
    ("Watchlist", "Watchlist", "the tickers on your watchlist"),
    ("Opportunities", "New Opportunities",
     "tickers APEX found by itself (trending headlines, the morning screen) that nobody holds or watches"),
]


def _group_cards() -> dict[str, dict]:
    """{group: {"cards": {horizon: Scorecard}, "tickers": n, "prices": bool}} for every group that has scored predictions."""
    out: dict[str, dict] = {}
    for label, seg, _desc in _GROUPS:
        if (seg == "Portfolio" and not _portfolio_tickers) or (seg == "Watchlist" and not _watchlist_tickers):
            continue          # an empty list is an empty group, never "everything" (the loader applies no filter to an empty list)
        df = _load_evaluated_predictions_df(lookback, None, seg, _portfolio_tickers, _watchlist_tickers, roles=_roles)
        if df.empty or not df["horizon_days"].notna().any():
            continue
        use_prices = int(df["ticker"].nunique()) <= _PRICE_AUTO_MAX_TICKERS
        try:
            out[label] = {"cards": _cached_scorecards(df, use_prices), "tickers": int(df["ticker"].nunique()), "prices": use_prices}
        except Exception as exc:   # never let the yardstick take the page down
            st.error(f"Could not compute the {label.lower()} scorecard: {exc}", icon=material("error"))
    return out


_groups = _group_cards()
if not _groups:
    st.info(
        "No scored predictions yet. They are scored once they mature (5 to 63 trading days after they are made).",
        icon=material("hourglass_top"),
    )
    st.stop()

_horizons = sorted({h for g in _groups.values() for h in g["cards"]})
# The horizon with the most evidence (dates, summed over groups) drives the charts; every horizon appears in a grid below.
_h_sel = max(_horizons, key=lambda h: (sum(g["cards"][h].evidence.n_dates for g in _groups.values() if h in g["cards"]), -h))

# ── One block per group: how much evidence each has ───────────────────────────

_cols = st.columns(len(_GROUPS))
for _col, (_label, _seg, _desc) in zip(_cols, _GROUPS):
    with _col:
        _g = _groups.get(_label)
        if _g is None:
            st.metric(_label, "no results yet", help=f"Scored predictions on {_desc}: none in the last {lookback} days.")
            continue
        _best = max(_g["cards"], key=lambda h: (_g["cards"][h].evidence.n_dates, -h))
        _ev = _g["cards"][_best].evidence
        _level = {scorecard_mod.LEVEL_TOO_EARLY: "too early to judge", scorecard_mod.LEVEL_EARLY: "still early"}.get(_ev.level, "established")
        st.metric(_label, f"{_ev.n_dates} days of results",
                  delta=f"{_H_LABELS.get(_best, f'{_best}d')} calls · {_g['tickers']} tickers · {_level}", delta_color="off",
                  help=f"Scored predictions on {_desc}. Firm conclusions need {scorecard_mod.ESTABLISHED_DATES}+ days.")

# ── Scorecard grids: four checks (rows) x the groups (columns), one grid per horizon ──

for _h in _horizons:
    _have = [(l, _groups[l]["cards"][_h]) for l, _s, _d in _GROUPS if l in _groups and _h in _groups[l]["cards"]]
    if not _have:
        continue
    st.markdown(f"**{_H_LABELS.get(_h, f'{_h}-Day')} calls** · predictions that looked {_h} trading days ahead")
    _cols_h = [(l, f"{l}: {c.evidence.n_dates} days of results so far at this horizon.") for l, c in _have]
    _rows_h = []
    for _i, _key in enumerate(_ROW_TITLES):
        _tip = scorecard_mod.plain_tooltip(_have[0][1].items[_i]).split("\n")[0]
        _rows_h.append((_ROW_TITLES[_key], _tip, [c.items[_i] for _l, c in _have]))
    st.markdown(grid_html(_cols_h, _rows_h), unsafe_allow_html=True)
st.caption("Hover any number for what it means and its likely range. Opportunities are market-only forecasts, the same for every viewer."
           + ("" if all(g["prices"] for g in _groups.values()) else " Price-based baselines are skipped for a list this long."))

# ── Two comparison charts ─────────────────────────────────────────────────────

_rec = {l: g["cards"][_h_sel].by_recommendation for l, g in _groups.items() if _h_sel in g["cards"]}
_trend = {l: g["cards"][_h_sel].trend for l, g in _groups.items() if _h_sel in g["cards"]}
ch1, ch2 = st.columns(2)
with ch1:
    st.markdown(f"**Average return vs SPY, by rating** · {_H_LABELS.get(_h_sel, f'{_h_sel}d')}")
    if any(r["n"] for rows in _rec.values() for r in rows):
        st.plotly_chart(build_excess_by_group_chart(_rec), use_container_width=True, key="val_excess")
    else:
        st.info("No rated, evaluated predictions at this horizon yet.", icon=material("info"))
with ch2:
    st.markdown("**Does a higher APEX score go with beating SPY?** · day by day")
    if any(_trend.values()):
        st.plotly_chart(build_rank_trend_compare_chart(_trend), use_container_width=True, key="val_trend")
    else:
        st.info("Needs at least 10 stocks scored on the same day.", icon=material("info"))

# ── Prediction history (collapsed): one table, a Group column says which section each row is in ──

with st.expander("Prediction history", expanded=False):
    from portfolio_agent.tools.validation_engine import get_recent_evaluated_predictions

    _pt_set, _wt_set = set(_portfolio_tickers), set(_watchlist_tickers)

    def _groups_of(p: dict) -> list[str]:
        t = str(p.get("ticker") or "").upper()
        return [n for n, hit in (("Portfolio", t in _pt_set), ("Watchlist", t in _wt_set),
                                 ("Opportunities", p.get("trigger_type") == "trending_opportunity")) if hit]

    preds = [dict(p, _group=" + ".join(g)) for p in get_recent_evaluated_predictions(limit=5000, lookback_days=lookback, roles=_roles)
             if (g := _groups_of(p))]
    if not preds:
        st.info("No evaluated predictions yet.", icon=material("info"))
    else:
        _wrong_only = st.toggle("Wrong calls only", value=False, key="pred_tab_wrong")
        filtered = sorted(
            [p for p in preds if not _wrong_only or str(p.get("outcome") or "").startswith("wrong")],
            key=lambda p: p.get("as_of_date") or p.get("prediction_date") or "",
            reverse=True,
        )

        rows = []
        for p in filtered:
            ar    = p.get("actual_return")        # decimal  e.g. -0.0063
            rlo   = p.get("predicted_return_low")  # percent  e.g. 1.5
            rhi   = p.get("predicted_return_high") # percent  e.g. 4.0
            sp    = p.get("start_price")           # dollars
            oc    = p.get("outcome") or p.get("evaluation_status") or "—"
            er    = p.get("excess_return")

            rng_pct = f"{fmt_pct(rlo, signed=True)} – {fmt_pct(rhi, signed=True)}" if rlo is not None and rhi is not None else "—"
            act_pct = fmt_pct(ar*100, signed=True) if ar is not None else "—"

            if sp and sp > 0:
                if rlo is not None and rhi is not None:
                    rng_price = f"{fmt_money(sp * (1 + rlo / 100))} – {fmt_money(sp * (1 + rhi / 100))}"
                else:
                    rng_price = "—"
                act_price = fmt_money(sp * (1 + ar)) if ar is not None else "—"
                entry_price = fmt_money(sp)
            else:
                rng_price = act_price = entry_price = "—"

            dist_parts = []
            for col, label in [("p_strong_down","▼▼"),("p_moderate_down","▼"),
                                ("p_flat","→"),("p_moderate_up","▲"),("p_strong_up","▲▲")]:
                v = p.get(col)
                if v is not None:
                    dist_parts.append(f"{label}{int(v)}%")

            rows.append({
                "Date":            p.get("as_of_date") or p.get("prediction_date") or "",
                "Group":           p["_group"],
                "Ticker":          p.get("ticker") or "",
                "Horizon":         _H_LABELS.get(p.get("horizon_days"), f"{p.get('horizon_days')}-Day"),
                "Predicted":       p.get("predicted_direction") or "—",
                "Entry Price":     entry_price,
                "Pred Range %":    rng_pct,
                "Pred Price Range":rng_price,
                "Actual %":        act_pct,
                "Actual Price":    act_price,
                "Distribution":    " ".join(dist_parts) if dist_parts else "—",
                "Bucket":          p.get("actual_bucket") or "—",
                "Outcome":         oc.replace("_", " ").title() if isinstance(oc, str) else oc,
                "Conviction":      p.get("conviction_score"),
                "Excess vs SPY":   fmt_pct(er*100, signed=True) if er is not None else "—",
                "Source":          ROLE_LABELS.get(p.get("prediction_role"), p.get("prediction_role") or "—"),
            })

        st.dataframe(
            pd.DataFrame(rows),
            use_container_width=True,
            hide_index=True,
            column_config={
                "Date":             st.column_config.Column(help="Date the prediction was made."),
            "Group":            st.column_config.Column(help="Portfolio: you hold it · Watchlist: on your watchlist · Opportunities: found by APEX on its own. A ticker can be in more than one."),
                "Horizon":          st.column_config.Column(help="How many trading days ahead the prediction looked."),
                "Predicted":        st.column_config.Column(help="Predicted direction: UP, DOWN, or FLAT."),
                "Entry Price":      st.column_config.Column(help="Price at the time the prediction was made."),
                "Pred Range %":     st.column_config.Column(help="Predicted return range (low – high) over the horizon."),
                "Pred Price Range": st.column_config.Column(help="Predicted return range converted to a dollar price window off the entry price."),
                "Actual %":         st.column_config.Column(help="Actual realized return over the horizon."),
                "Actual Price":     st.column_config.Column(help="Realized price at the end of the horizon."),
                "Distribution":     st.column_config.Column(help="Model's stated probability across 5 return buckets: ▼▼ strong down · ▼ moderate down · → flat · ▲ moderate up · ▲▲ strong up."),
                "Bucket":           st.column_config.Column(help="Which of the 5 return buckets the actual return landed in."),
                "Outcome":          st.column_config.Column(help="Whether the call was scored correct or wrong, and by how much."),
                "Conviction":       st.column_config.Column(help="Model's self-reported confidence in this call, 1 (low) – 10 (high)."),
                "Excess vs SPY":    st.column_config.Column(help="Actual return minus the S&P 500's return over the same window. Positive = beat the market."),
                "Source":           st.column_config.Column(help="Champion: the configured APEX model · Fallback: a backup model used because the champion failed that run · Legacy (random model): made before champion selection."),
            },
        )
        st.caption(f"{len(filtered)} of {len(preds)} evaluated predictions shown.")
