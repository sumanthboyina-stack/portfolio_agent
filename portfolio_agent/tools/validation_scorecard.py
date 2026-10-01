"""
Validation scorecard -- the four numbers a user should look at, with one plain verdict each.

Pure: no Streamlit, no DB, no network (price history is injected). This module is the VIEW MODEL
over validation_metrics.py: every statistic comes from that module's functions; this file only
picks them, formats them and applies one verdict rule. Nothing is recomputed here.

    compute_scorecard(df, horizon) -> Scorecard      (one horizon at a time, never mixed)

Items:
    ranking        do BUY-rated names beat the rest?            (spread, date-block bootstrap CI)
    direction      is APEX's up/flat/down call right more often than a naive baseline?
    probabilities  are APEX's stated chances better than long-run averages?
    ranges         how often does the actual return land inside the stated range? (info only)

The verdict rule lives in ONE place: verdict(). User-facing strings (title, question, display,
baseline_label, one_line_reason, subtitle) avoid statistical jargon; the technical name is only in
help_text.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from portfolio_agent.log import get_logger as _get_logger
from portfolio_agent.tools import validation_metrics as vm

_log = _get_logger("validation_scorecard")

# ── configurable constants ────────────────────────────────────────────────────

MIN_DATES_FOR_VERDICT = 10      # fewer distinct prediction dates than this -> "Too early"
ESTABLISHED_DATES = 30          # this many or more -> evidence level "Established"
N_BOOT = 1000
SEED = 0
BUY_RECS = ("BUY", "STRONG_BUY")
RECOMMENDATION_ORDER = ["STRONG_BUY", "BUY", "HOLD", "SELL", "STRONG_SELL"]

WORKING, NOT_WORKING, UNPROVEN, TOO_EARLY, INFO_ONLY = "Working", "Not working", "Unproven", "Too early", "Info only"
LEVEL_TOO_EARLY, LEVEL_EARLY, LEVEL_ESTABLISHED = "Too early", "Early", "Established"

PLAIN_BASELINE = {
    "always_up": "always guessing UP",
    "always_down": "always guessing DOWN",
    "always_flat": "always guessing FLAT",
    "majority_class": "always guessing the most common outcome",
    "class_frequency_random": "guessing at random in proportion to how often each outcome happens",
    "uniform_random": "guessing UP, FLAT or DOWN at random",
    "spy_prior_window": "repeating the market's last move",
    "momentum": "repeating each stock's last move",
}


# ── data classes ──────────────────────────────────────────────────────────────

@dataclass
class ScoreItem:
    key: str
    title: str
    plain_question: str
    value: Optional[float]
    display: str
    ci_low: Optional[float]
    ci_high: Optional[float]
    baseline: Optional[float]
    baseline_label: str
    n: int
    n_dates: int
    verdict: str
    one_line_reason: str
    help_text: str
    subtitle: str = ""                                  # secondary number, plain English
    extras: dict = field(default_factory=dict)          # raw values for QA / tests


@dataclass
class Evidence:
    n_raw: int
    n_streaks: Optional[int]
    n_nonoverlap: int
    n_dates: int
    first_date: Optional[str]
    last_date: Optional[str]
    level: str


@dataclass
class Scorecard:
    horizon: int
    items: list[ScoreItem]
    evidence: Evidence
    trend: list[tuple[str, float]] = field(default_factory=list)       # (date, daily rank IC of composite score)
    by_recommendation: list[dict] = field(default_factory=list)        # mean excess return per rating

    def item(self, key: str) -> ScoreItem:
        return next(i for i in self.items if i.key == key)


# ── the one verdict rule ──────────────────────────────────────────────────────

def evidence_level(n_dates: int) -> str:
    if n_dates < MIN_DATES_FOR_VERDICT:
        return LEVEL_TOO_EARLY
    return LEVEL_ESTABLISHED if n_dates >= ESTABLISHED_DATES else LEVEL_EARLY


def verdict(n_dates: int, ci_low: Optional[float], ci_high: Optional[float],
            baseline: float = 0.0, higher_is_better: bool = True) -> str:
    """
    The only place a verdict is decided.
      n_dates < MIN_DATES_FOR_VERDICT            -> "Too early"   (at exactly MIN it is allowed to judge)
      no interval                                -> "Unproven"
      interval entirely better than `baseline`   -> "Working"
      interval entirely worse                    -> "Not working"
      otherwise (straddles, or touches, baseline) -> "Unproven"
    "Better" is strictly above `baseline` when higher_is_better (spreads and skill use baseline 0),
    strictly below otherwise.
    """
    if n_dates < MIN_DATES_FOR_VERDICT:
        return TOO_EARLY
    if ci_low is None or ci_high is None:
        return UNPROVEN
    if not higher_is_better:
        ci_low, ci_high = -ci_high, -ci_low
        baseline = -baseline
    if ci_low > baseline:
        return WORKING
    if ci_high < baseline:
        return NOT_WORKING
    return UNPROVEN


# ── formatting / plain-English reasons ───────────────────────────────────────

def _pts(v: Optional[float], digits: int = 1) -> str:
    """fraction -> signed percent, e.g. 0.0123 -> '+1.2%'"""
    return "—" if v is None else f"{v * 100:+.{digits}f}%"


def _pct(v: Optional[float], digits: int = 0) -> str:
    return "—" if v is None else f"{v * 100:.{digits}f}%"


def _too_early_reason(n_dates: int) -> str:
    return (f"Only {n_dates} prediction date{'s' if n_dates != 1 else ''} so far; at least {MIN_DATES_FOR_VERDICT} "
            "are needed before this can be judged.")


def _empty_item(key, title, question, help_text, reason="Not enough data yet.", verdict_=TOO_EARLY, n=0, n_dates=0) -> ScoreItem:
    return ScoreItem(key=key, title=title, plain_question=question, value=None, display="—", ci_low=None, ci_high=None,
                     baseline=None, baseline_label="", n=n, n_dates=n_dates, verdict=verdict_,
                     one_line_reason=reason, help_text=help_text)


_DATE_NOTE = ("Ranges are 95% intervals from resampling whole prediction dates, because stocks predicted on the same day "
              "share one market move.")


# ── items ─────────────────────────────────────────────────────────────────────

_RANK_HELP = ("Mean excess return vs SPY of BUY / STRONG_BUY rated rows minus all other rated rows, with a date-block bootstrap "
              "95% CI. Secondary line: the daily cross-sectional Spearman rank IC of composite_score vs excess return, averaged "
              "over prediction dates. " + _DATE_NOTE)


def _ranking_item(d: pd.DataFrame, n_dates: int, rng) -> tuple[ScoreItem, pd.Series]:
    title, q = "Does the rating pick winners?", "Do the stocks APEX rates BUY do better than the rest, compared with the market?"
    comp = d["composite_score"].astype(float) if "composite_score" in d.columns else pd.Series(np.nan, index=d.index)
    ic_by_date = vm.rank_ic_by_date(d, comp)
    ic = vm._mean_over_dates(ic_by_date, N_BOOT, rng)

    rated = d[d["recommendation"].notna() & d["excess_return"].notna()] if {"recommendation", "excess_return"} <= set(d.columns) else d.iloc[0:0]
    sub = ""
    if ic["mean"] is not None:
        sub = f"Ordering by APEX's overall score lined up with results by {ic['mean']:+.2f} on average (0 = no relationship)."
    if rated.empty:
        return _empty_item("ranking", title, q, _RANK_HELP, "No rated, evaluated predictions yet.", n_dates=n_dates), pd.Series(dtype=float)
    is_buy = rated["recommendation"].astype(str).str.upper().isin(BUY_RECS)
    c = vm.group_excess_contrast(rated, is_buy, N_BOOT, rng)
    nd = c["n_dates"]
    extras = {"contrast": c, "rank_ic": ic}
    if c["diff"] is None:
        side = "BUY-rated" if c["n_a"] == 0 else "non-BUY"
        item = ScoreItem("ranking", title, q, None, "—", None, None, 0.0, "no difference", len(rated), nd, UNPROVEN,
                         f"There are no {side} rows to compare against yet, so this can't be judged.", _RANK_HELP,
                         subtitle=sub, extras=extras)
        return item, ic_by_date
    v = verdict(nd, c["ci_low"], c["ci_high"], 0.0)
    x = _pts(c["diff"])
    reason = {
        TOO_EARLY: _too_early_reason(nd),
        WORKING: f"BUY-rated stocks beat the others by {x} on average, and even the pessimistic end of the range is above zero.",
        NOT_WORKING: f"BUY-rated stocks trailed the others by {_pts(abs(c['diff']))} on average, and even the optimistic end is below zero.",
        UNPROVEN: f"BUY-rated stocks beat the others by {x} on average, but the plausible range ({_pts(c['ci_low'])} to {_pts(c['ci_high'])}) includes zero.",
    }[v]
    return ScoreItem("ranking", title, q, c["diff"], x, c["ci_low"], c["ci_high"], 0.0, "0% = BUY-rated no better than the rest",
                     len(rated), nd, v, reason, _RANK_HELP, subtitle=sub, extras=extras), ic_by_date


_DIR_HELP = ("APEX 3-class directional accuracy (UP / FLAT / DOWN, FLAT = within ±1%) on the NON-OVERLAPPING rows, with a Wilson 95% CI, "
             "against the best naive baseline scored on those same rows (always-UP/DOWN/FLAT, in-sample majority class, class-frequency "
             "and uniform random expectations, SPY prior window, ticker momentum; the look-ahead SPY-same-window diagnostic is excluded).")


def _direction_item(d: pd.DataFrame, closes, streak_n, n_dates: int) -> ScoreItem:
    title, q = "Is the call direction right?", "How often is APEX right about whether a stock goes up, stays flat or goes down?"
    eff = vm.effective_sample(d, streak_n)
    kept = vm.nonoverlap_rows(d) if len(d) else d
    comp = vm.compare_predictors(kept, closes)
    best = comp.get("best_baseline")
    if not eff["nonoverlap_n"] or eff["nonoverlap_accuracy"] is None or not best:
        return _empty_item("direction", title, q, _DIR_HELP, "Not enough non-overlapping predictions yet.", n=eff["nonoverlap_n"], n_dates=n_dates)
    acc, lo, hi, base = eff["nonoverlap_accuracy"], eff["ci_low"], eff["ci_high"], best["accuracy"]
    blabel = PLAIN_BASELINE.get(best["key"], best["name"])
    v = verdict(n_dates, lo, hi, base)
    reason = {
        TOO_EARLY: _too_early_reason(n_dates),
        WORKING: f"APEX was right {_pct(acc)} of the time versus {_pct(base)} for {blabel}, and even the pessimistic end of the range beats that.",
        NOT_WORKING: f"APEX was right {_pct(acc)} of the time versus {_pct(base)} for {blabel}, and even the optimistic end falls short.",
        UNPROVEN: f"APEX was right {_pct(acc)} of the time versus {_pct(base)} for {blabel}; the plausible range ({_pct(lo)} to {_pct(hi)}) includes that.",
    }[v]
    return ScoreItem("direction", title, q, acc, _pct(acc), lo, hi, base, f"{_pct(base)} by {blabel}", eff["nonoverlap_n"], n_dates, v, reason,
                     _DIR_HELP, extras={"effective_sample": {k: x for k, x in eff.items() if k != "kept"}, "comparison": comp, "best_baseline": best})


_PROB_HELP = ("5-bucket Brier skill score = 1 − APEX Brier / climatology Brier, where climatology forecasts the sample's own bucket "
              "frequencies every time (strong down / moderate down / flat / moderate up / strong up). Above 0 beats climatology. "
              "Date-block bootstrap 95% CI, climatology recomputed per resample. " + _DATE_NOTE)


def _probabilities_item(d: pd.DataFrame, n_dates: int, rng) -> ScoreItem:
    title, q = "Are the stated chances any good?", "Are APEX's stated chances better than simply using the long-run average chances?"
    pb = vm.probability_baselines(d)
    five = pb.get("five")
    if not five or five["brier_skill"] is None:
        return _empty_item("probabilities", title, q, _PROB_HELP, "No predictions with a full set of stated chances have been evaluated yet.", n_dates=n_dates)
    ci = vm.five_bucket_skill_ci(d, N_BOOT, rng)
    skill, lo, hi = five["brier_skill"], ci["ci_low"], ci["ci_high"]
    nd = ci["n_dates"]
    v = verdict(nd, lo, hi, 0.0)
    x = f"{skill * 100:+.1f}%"
    size = f"{abs(skill) * 100:.0f}%"
    if lo is None or hi is None:
        reason = _too_early_reason(nd) if v == TOO_EARLY else "The plausible range could not be estimated yet."
    else:
        reason = {
            TOO_EARLY: _too_early_reason(nd),
            WORKING: f"APEX's stated chances were {size} more accurate than the long-run averages, and even the pessimistic end is better.",
            NOT_WORKING: f"APEX's stated chances were {size} less accurate than the long-run averages, and even the optimistic end is worse.",
            UNPROVEN: f"APEX's stated chances were {size} {'more' if skill >= 0 else 'less'} accurate than the long-run averages, but the plausible range ({lo * 100:+.0f}% to {hi * 100:+.0f}%) includes no difference.",
        }[v]
    return ScoreItem("probabilities", title, q, skill, x, lo, hi, 0.0, "0% = no better than long-run averages", five_n(pb), nd, v, reason, _PROB_HELP,
                     extras={"probability": pb, "ci": ci})


def five_n(pb: dict) -> int:
    return int(pb.get("n") or 0)


_RANGE_HELP = ("Share of evaluated predictions whose actual return fell inside the stated [low, high] return range (the stored "
               "in_predicted_range flag). The system states no coverage target (for example 80%), so there is nothing to judge it against.")


def _ranges_item(d: pd.DataFrame, n_dates: int) -> ScoreItem:
    title, q = "Does the return land in the stated range?", "How often does the actual return land inside the range APEX gave?"
    if "in_predicted_range" not in d.columns or d["in_predicted_range"].notna().sum() == 0:
        return _empty_item("ranges", title, q, _RANGE_HELP, "Ranges have no stated coverage target, so this is for information only.", INFO_ONLY, n_dates=n_dates)
    flags = d["in_predicted_range"].dropna().astype(float)
    pct = float(flags.mean())
    return ScoreItem("ranges", title, q, pct, _pct(pct), None, None, None, "no coverage target", int(len(flags)), n_dates, INFO_ONLY,
                     "Ranges have no stated coverage target, so this is for information only.", _RANGE_HELP)


# ── by-recommendation + evidence ──────────────────────────────────────────────

def _by_recommendation(d: pd.DataFrame) -> list[dict]:
    if not {"recommendation", "excess_return"} <= set(d.columns):
        return []
    x = d[d["recommendation"].notna() & d["excess_return"].notna()].copy()
    if x.empty:
        return []
    x["_rec"] = x["recommendation"].astype(str).str.upper()
    x["_k"] = vm._date_keys(x)
    out = []
    for rec in RECOMMENDATION_ORDER:
        g = x[x["_rec"] == rec]
        out.append({"recommendation": rec, "mean_excess": float(g["excess_return"].mean()) if len(g) else None,
                    "n": int(len(g)), "n_dates": int(g["_k"].nunique())})
    return out


def _evidence(d: pd.DataFrame, streak_n: Optional[int]) -> Evidence:
    dates = sorted(vm._date_keys(d).unique()) if len(d) and "as_of_date" in d.columns else []
    try:
        nonoverlap = int(len(vm.nonoverlap_rows(d))) if len(d) else 0
    except Exception as exc:                                    # e.g. no ticker / date columns
        _log.warning(f"scorecard: could not count non-overlapping rows: {exc}")
        nonoverlap = 0
    return Evidence(n_raw=len(d), n_streaks=streak_n, n_nonoverlap=nonoverlap, n_dates=len(dates),
                    first_date=dates[0] if dates else None, last_date=dates[-1] if dates else None,
                    level=evidence_level(len(dates)))


# ── entry point ───────────────────────────────────────────────────────────────

def compute_scorecard(df: Optional[pd.DataFrame], horizon: int, closes_by_symbol: Optional[dict] = None,
                      streak_n: Optional[int] = None) -> Scorecard:
    """
    Scorecard for ONE horizon from an evaluated-predictions frame (see web.data.validation.
    _load_evaluated_predictions_df). Rows of other horizons are ignored. `closes_by_symbol` feeds the
    prior-window baselines; `streak_n` is the rating-streak count from the existing streak logic.
    Empty frames, short samples and missing columns yield "Too early" items; this never raises.
    """
    base = pd.DataFrame() if df is None else df
    try:
        d = vm.prepare_rows(base[base["horizon_days"] == horizon]) if (len(base) and "horizon_days" in base.columns) else vm.prepare_rows(pd.DataFrame())
    except Exception as exc:                                   # malformed input must not take the page down
        _log.warning(f"scorecard: could not prepare rows: {exc}")
        d = vm.prepare_rows(pd.DataFrame())
    try:
        evidence = _evidence(d, streak_n) if len(d) else Evidence(0, streak_n, 0, 0, None, None, LEVEL_TOO_EARLY)
    except Exception as exc:
        _log.warning(f"scorecard: could not build evidence: {exc}")
        evidence = Evidence(len(d), streak_n, 0, 0, None, None, LEVEL_TOO_EARLY)
    rng = np.random.default_rng(SEED)

    builders = [
        ("ranking", lambda: _ranking_item(d, evidence.n_dates, rng)),
        ("direction", lambda: _direction_item(d, closes_by_symbol, streak_n, evidence.n_dates)),
        ("probabilities", lambda: _probabilities_item(d, evidence.n_dates, rng)),
        ("ranges", lambda: _ranges_item(d, evidence.n_dates)),
    ]
    items: list[ScoreItem] = []
    trend: list[tuple[str, float]] = []
    for key, build in builders:
        try:
            res = build() if len(d) else None
            if res is None:
                raise ValueError("no rows")
            if key == "ranking":
                item, ic_by_date = res
                trend = sorted(ic_by_date.items())
            else:
                item = res
        except Exception as exc:
            if len(d):
                _log.warning(f"scorecard: {key} failed: {exc}")
            meta = {"ranking": ("Does the rating pick winners?", "Do the stocks APEX rates BUY do better than the rest, compared with the market?", _RANK_HELP),
                    "direction": ("Is the call direction right?", "How often is APEX right about whether a stock goes up, stays flat or goes down?", _DIR_HELP),
                    "probabilities": ("Are the stated chances any good?", "Are APEX's stated chances better than simply using the long-run average chances?", _PROB_HELP),
                    "ranges": ("Does the return land in the stated range?", "How often does the actual return land inside the range APEX gave?", _RANGE_HELP)}[key]
            item = _empty_item(key, *meta[:2], meta[2], reason=_too_early_reason(evidence.n_dates) if key != "ranges" else "Ranges have no stated coverage target, so this is for information only.",
                               verdict_=INFO_ONLY if key == "ranges" else TOO_EARLY, n_dates=evidence.n_dates)
        items.append(item)
    return Scorecard(horizon=horizon, items=items, evidence=evidence, trend=trend, by_recommendation=_by_recommendation(d) if len(d) else [])
