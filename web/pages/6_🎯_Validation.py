"""
APEX Prediction Validation — the user-facing trust page: "is this reliable?"

One screen: pick a horizon, see how much evidence there is, then four score cards
(each a plain question, one number, a verdict, the thing it is compared against, and a
one-line reason) and two charts. Every number comes from
portfolio_agent.tools.validation_scorecard.compute_scorecard; this file only lays it out.

Everything that used to be here beyond that — the nine-tile metric grid, outcome
breakdown, the follow-every-BUY index, baselines, confusion matrix, ranking detail, model
split, FLAT diagnostic, the scoring-snapshot tables — lives on the admin-only Validation QA
page.

Scope defaults to the viewer's own holdings + watchlist, with an explicit switch to the full
shared prediction universe. Every filter narrows by *criteria*, never by an enumerable list of
other people's specific tickers, so nothing here lets one viewer infer what somebody else
holds or watches.
"""
from __future__ import annotations

import html
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
    initial_sidebar_state="expanded",
)
inject_global_css()
require_login()
top_nav("validation")

from portfolio_agent.tools.prediction_db import CURRENT_SYSTEM_VERSION
from portfolio_agent.tools import validation_scorecard as scorecard_mod
from web.data.validation import (
    _load_portfolio_tickers,
    _load_watchlist_tickers,
    _load_evaluated_predictions_df,
    build_scorecards,
)
from web.components.validation_charts import (
    build_excess_by_recommendation_chart,
    build_rank_trend_chart,
    OUTCOME_LABELS,
)

# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown(
        '<p style="font-size:0.7rem;font-weight:700;text-transform:uppercase;'
        'letter-spacing:0.08em;color:#475569;margin:0 0 10px">Filters</p>',
        unsafe_allow_html=True,
    )
    st.caption(f"Run Evening (evaluation) and Weekly Analysis from the {material('calendar_month')} Schedule page.")

    st.divider()
    lookback = st.selectbox(
        "Lookback period",
        [30, 90, 365],
        index=1,
        format_func=lambda x: f"{x} days",
    )

    st.divider()

    st.markdown(
        '<p style="font-size:0.72rem;font-weight:600;color:#9CA3AF;margin:0 0 8px">'
        'Scope</p>',
        unsafe_allow_html=True,
    )
    _seg_sel = st.radio(
        "segment",
        ["My Tickers", "All", "Portfolio", "Watchlist", "New Opportunities"],
        index=0,
        label_visibility="collapsed",
        key="val_segment",
        help=(
            "My Tickers (default): your holdings + watchlist combined  ·  "
            "All: everyone's shared prediction universe, for context  ·  "
            "Portfolio: your holdings only  ·  "
            "Watchlist: tracked but not held  ·  "
            "New Opportunities: trending tickers discovered by the scanner"
        ),
    )
    if _seg_sel == "All":
        st.caption(f"{material('public')} Widened to the full shared universe.")

# Scoped to the viewer's OWN holdings/watchlist, not the shared union across every user.
_ctx = current_context("web:validation")
_portfolio_tickers = _load_portfolio_tickers(_ctx)
_watchlist_tickers = _load_watchlist_tickers(_ctx)

# ── Page header ───────────────────────────────────────────────────────────────

st.title(f"{material('track_changes')} APEX Prediction Validation")
st.caption(f"System version: `{CURRENT_SYSTEM_VERSION}` · Scores predictions after they mature · Detailed diagnostics: Validation QA (admin)")

if _seg_sel != "My Tickers":
    st.info(f"Scope: **{_seg_sel}** · All numbers recalculated for this scope.", icon=material("search"))

# ── Data + scorecards ─────────────────────────────────────────────────────────

eval_df = _load_evaluated_predictions_df(lookback, None, _seg_sel, _portfolio_tickers, _watchlist_tickers)
horizons = sorted(int(h) for h in eval_df["horizon_days"].dropna().unique()) if not eval_df.empty else []

if not horizons:
    st.info(
        "No evaluated predictions yet. "
        f"Click {material('nights_stay')} **Evening** on the {material('calendar_month')} "
        "Schedule page to score matured predictions.",
        icon=material("info"),
    )
    st.stop()

# Momentum / SPY-prior-window baselines need one price fetch per ticker, so a big scope asks first.
_PRICE_AUTO_MAX_TICKERS = 60
_n_tickers = int(eval_df["ticker"].nunique())
_use_prices = _n_tickers <= _PRICE_AUTO_MAX_TICKERS
if not _use_prices:
    _use_prices = st.sidebar.toggle(
        "Price-based baselines",
        value=False,
        key="val_price_baselines",
        help=f"Needs price history for each of the {_n_tickers} tickers in this scope (cached 30 min). "
             "Off = the 'repeat the last move' comparisons are left out of the direction check.",
    )


@st.cache_data(ttl=600, show_spinner="Scoring…")
def _cached_scorecards(df: pd.DataFrame, use_prices: bool) -> dict:
    return build_scorecards(df, use_prices=use_prices)


try:
    _cards = _cached_scorecards(eval_df, _use_prices)
except Exception as _exc:   # never let the yardstick take the page down
    st.error(f"Could not compute the scorecard: {_exc}", icon=material("error"))
    st.stop()

# ── Horizon selector ──────────────────────────────────────────────────────────

_H_LABELS = {5: "5-Day", 21: "21-Day", 63: "63-Day", 250: "250-Day"}
_h_sel = st.radio(
    "Horizon", horizons, index=0, horizontal=True, key="val_horizon",
    format_func=lambda h: _H_LABELS.get(h, f"{h}d"),
    help="How far ahead the prediction looked. Horizons are never mixed in one number.",
)
card = _cards[_h_sel]
ev = card.evidence

# ── Evidence banner ───────────────────────────────────────────────────────────

_span = f"{ev.first_date} → {ev.last_date}" if ev.first_date else "—"
_streaks = f", {ev.n_streaks} rating streaks" if ev.n_streaks is not None else ""
_evidence_text = (
    f"**Evidence: {ev.level}** · {ev.n_dates} distinct prediction dates ({_span}) · "
    f"{ev.n_nonoverlap} non-overlapping predictions ({ev.n_raw} total{_streaks})"
)
if ev.level == scorecard_mod.LEVEL_TOO_EARLY:
    st.warning(
        f"{_evidence_text}. Verdicts switch on at {scorecard_mod.MIN_DATES_FOR_VERDICT} dates; until then the numbers "
        "below are shown but should be read as a preview, not a result.",
        icon=material("hourglass_top"),
    )
elif ev.level == scorecard_mod.LEVEL_EARLY:
    st.info(f"{_evidence_text}. Verdicts are allowed, but firm conclusions need {scorecard_mod.ESTABLISHED_DATES}+ dates.", icon=material("info"))
else:
    st.success(_evidence_text, icon=material("check_circle"))

# ── Four score cards ──────────────────────────────────────────────────────────

_VERDICT_COLORS = {
    scorecard_mod.WORKING: "#059669",
    scorecard_mod.UNPROVEN: "#D97706",
    scorecard_mod.NOT_WORKING: "#DC2626",
    scorecard_mod.TOO_EARLY: "#64748B",
    scorecard_mod.INFO_ONLY: "#2563EB",
}


def _esc(text: str) -> str:
    return html.escape(text or "", quote=True)


def _card_html(item: scorecard_mod.ScoreItem) -> str:
    color = _VERDICT_COLORS.get(item.verdict, "#64748B")
    baseline = f'<div style="font-size:.78rem;opacity:.75;margin:6px 0 2px">Compared with: {_esc(item.baseline_label)}</div>' if item.baseline_label else ""
    sub = f'<div style="font-size:.76rem;opacity:.7;margin-bottom:6px">{_esc(item.subtitle)}</div>' if item.subtitle else ""
    return (
        f'<div title="{_esc(item.help_text)}" style="cursor:help">'
        f'<div style="font-size:.84rem;font-weight:600;margin-bottom:2px">{_esc(item.title)} '
        f'<span style="opacity:.55;font-weight:400">ⓘ</span></div>'
        f'<div style="font-size:.76rem;opacity:.7;min-height:2.4em">{_esc(item.plain_question)}</div>'
        f'<div style="font-size:2.1rem;font-weight:800;line-height:1.15;margin:6px 0">{_esc(item.display)}</div>'
        f'<span style="background:{color}22;color:{color};border:1px solid {color}66;border-radius:20px;'
        f'padding:2px 11px;font-size:.74rem;font-weight:700">{_esc(item.verdict)}</span>'
        f'{baseline}{sub}'
        f'<div style="font-size:.8rem;margin-top:8px;line-height:1.4">{_esc(item.one_line_reason)}</div>'
        f'</div>'
    )


_cols = st.columns(len(card.items))
for _col, _item in zip(_cols, card.items):
    with _col:
        with st.container(border=True):
            st.markdown(_card_html(_item), unsafe_allow_html=True)

st.caption("Hover a card for the technical definition. Ranges are 95% intervals that resample whole prediction dates, "
           "because stocks predicted on the same day share one market move.")

# ── Two charts ────────────────────────────────────────────────────────────────

ch1, ch2 = st.columns(2)
with ch1:
    st.markdown(f"**Average return vs SPY, by rating** · {_H_LABELS.get(_h_sel, f'{_h_sel}d')} predictions only")
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

    preds = [p for p in get_recent_evaluated_predictions(limit=500, lookback_days=lookback)
             if p.get("horizon_days") == _h_sel]
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
        o_opts = sorted({p["outcome"] for p in preds if p.get("outcome")})
        o_filter = st.multiselect(
            "Outcome", o_opts, default=[],
            format_func=lambda o: OUTCOME_LABELS.get(o, o), key="pred_tab_o",
            placeholder="All outcomes — pick to narrow",
        )
        filtered = sorted(
            [p for p in preds if not o_filter or p.get("outcome") in o_filter],
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
            })

        st.dataframe(
            pd.DataFrame(rows),
            use_container_width=True,
            hide_index=True,
            column_config={
                "Date":             st.column_config.Column(help="Date the prediction was made."),
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
            },
        )
        st.caption(f"{len(filtered)} of {len(preds)} evaluated {_H_LABELS.get(_h_sel, f'{_h_sel}d')} predictions shown.")
