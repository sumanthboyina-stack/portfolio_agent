"""
APEX Prediction Validation — the user-facing trust page: "is this reliable?"

One screen, no filters: a grid of the four checks (rows) across every horizon (columns) for the viewer's own
holdings + watchlist, last 90 days, champion-model predictions only. Hover any number for a plain-English
meaning and its likely range. Then two charts for the horizon with the most evidence. Every number comes from
portfolio_agent.tools.validation_scorecard.compute_scorecard; this file only lays it out.

Everything that used to be here beyond that — the nine-tile metric grid, outcome breakdown, the follow-every-BUY index,
baselines, confusion matrix, ranking detail, model split, FLAT diagnostic, the scoring-snapshot tables — lives on the
admin-only Validation QA page.

Scope is fixed to the viewer's own holdings + watchlist, so nothing here lets one viewer infer what somebody else holds
or watches. Legacy random-model predictions and challenger shadow rows never appear (see Validation QA).
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
    build_excess_by_recommendation_chart,
    build_rank_trend_chart,
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

# ── Fixed view (no filters): the viewer's own tickers, last 90 days, champion-model predictions ──
_seg_sel = "My Tickers"
lookback = 90
_roles = VALIDATION_ROLES

# Scoped to the viewer's OWN holdings/watchlist, not the shared union across every user.
_ctx = current_context("web:validation")
_portfolio_tickers = _load_portfolio_tickers(_ctx)
_watchlist_tickers = _load_watchlist_tickers(_ctx)

# ── Data + scorecards ─────────────────────────────────────────────────────────

eval_df = _load_evaluated_predictions_df(lookback, None, _seg_sel, _portfolio_tickers, _watchlist_tickers, roles=_roles)
horizons = sorted(int(h) for h in eval_df["horizon_days"].dropna().unique()) if not eval_df.empty else []

if not horizons:
    st.info(
        "No scored predictions yet. They are scored once they mature (5 to 63 trading days after they are made).",
        icon=material("hourglass_top"),
    )
    st.stop()

# Price-based baselines need one price fetch per ticker: automatic for a normal-size list, skipped for a very large one.
_PRICE_AUTO_MAX_TICKERS = 60
_use_prices = int(eval_df["ticker"].nunique()) <= _PRICE_AUTO_MAX_TICKERS


@st.cache_data(ttl=600, show_spinner="Scoring…")
def _cached_scorecards(df: pd.DataFrame, use_prices: bool) -> dict:
    return build_scorecards(df, use_prices=use_prices)


try:
    _cards = _cached_scorecards(eval_df, _use_prices)
except Exception as _exc:   # never let the yardstick take the page down
    st.error(f"Could not compute the scorecard: {_exc}", icon=material("error"))
    st.stop()

_H_LABELS = {5: "5-Day", 21: "21-Day", 63: "63-Day", 250: "250-Day"}
# The horizon with the most evidence drives the headline and the charts; every horizon appears in the grid below.
_h_sel = max(horizons, key=lambda h: (_cards[h].evidence.n_dates, -h))
card = _cards[_h_sel]
ev = card.evidence

# ── Headline: one plain sentence ───────────────────────────────────────────────

_hl = f"{ev.n_dates} days of results so far ({_H_LABELS.get(_h_sel, f'{_h_sel}d')} calls)"
if ev.level == scorecard_mod.LEVEL_TOO_EARLY:
    st.warning(f"{_hl}. Too early to judge; treat the numbers as a preview.", icon=material("hourglass_top"))
elif ev.level == scorecard_mod.LEVEL_EARLY:
    st.info(f"{_hl}. Still early; firm conclusions need {scorecard_mod.ESTABLISHED_DATES}+ days.", icon=material("info"))
else:
    st.success(f"{_hl}.", icon=material("check_circle"))

# ── Scorecard grid: four checks (rows) x every horizon (columns) ──────────────

_ROW_TITLES = {
    "ranking": "Do BUY picks win?",
    "direction": "Is the direction right?",
    "probabilities": "Are the chances good?",
    "ranges": "Does it land in range?",
}


def _grid_html() -> str:
    cols = [(_H_LABELS.get(h, f"{h}-Day"),
             f"Predictions that looked {h} trading days ahead. {_cards[h].evidence.n_dates} days of results so far.") for h in horizons]
    rows = [(_ROW_TITLES[key], scorecard_mod.plain_tooltip(_cards[horizons[0]].items[i]).split("\n")[0], [_cards[h].items[i] for h in horizons])
            for i, key in enumerate(_ROW_TITLES)]
    return grid_html(cols, rows)


st.markdown(_grid_html(), unsafe_allow_html=True)
st.caption("Hover any number for what it means and its likely range.")

# ── Two charts ────────────────────────────────────────────────────────────────

ch1, ch2 = st.columns(2)
with ch1:
    st.markdown(f"**Average return vs SPY, by rating** · {_H_LABELS.get(_h_sel, f'{_h_sel}d')}")
    if any(r["n"] for r in card.by_recommendation):
        st.plotly_chart(build_excess_by_recommendation_chart(card.by_recommendation), use_container_width=True)
    else:
        st.info("No rated, evaluated predictions at this horizon yet.", icon=material("info"))
with ch2:
    st.markdown("**Does a higher APEX score go with beating SPY?** · day by day")
    if card.trend:
        st.plotly_chart(build_rank_trend_chart(card.trend), use_container_width=True)
    else:
        st.info("Needs at least 10 stocks scored on the same day.", icon=material("info"))

# ── Prediction history (collapsed) ────────────────────────────────────────────

with st.expander("Prediction history", expanded=False):
    from portfolio_agent.tools.validation_engine import get_recent_evaluated_predictions

    preds = get_recent_evaluated_predictions(limit=500, lookback_days=lookback, roles=_roles)
    _my_set = set(_portfolio_tickers) | set(_watchlist_tickers)
    if _seg_sel == "My Tickers" and _my_set and preds:
        preds = [p for p in preds if p.get("ticker", "").upper() in _my_set]
    elif _seg_sel == "Portfolio" and _portfolio_tickers and preds:
        _pt_set = set(_portfolio_tickers)
        preds = [p for p in preds if p.get("ticker", "").upper() in _pt_set]
    elif _seg_sel == "Watchlist" and _watchlist_tickers and preds:
        _wt_set = set(_watchlist_tickers)
        preds = [p for p in preds if p.get("ticker", "").upper() in _wt_set]
    elif _seg_sel == "New Opportunities" and preds:
        preds = [p for p in preds if p.get("trigger_type") == "trending_opportunity"]
    if not preds:
        st.info(f"No evaluated predictions yet. (scope: {_seg_sel})", icon=material("info"))
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
