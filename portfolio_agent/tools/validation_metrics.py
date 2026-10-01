"""
Validation yardstick -- baselines, effective sample size and probability-skill
metrics for evaluated predictions. Pure functions: no DB, no network, no
Streamlit. Price history is passed in as {symbol: {date_iso: close}}; the web
data layer owns fetching and caching it.

Every function works on ONE horizon at a time (a frame mixing horizons raises
ValueError) so a 5d and a 63d number are never blended.

Expected columns on the evaluated-rows frame (extra columns are ignored):
    ticker, horizon_days, as_of_date, evaluation_date,
    predicted_direction, actual_return, benchmark_return, excess_return,
    p_strong_down, p_moderate_down, p_flat, p_moderate_up, p_strong_up

Class labels are 3-way UP / DOWN / FLAT, with FLAT = |return| <= FLAT_THRESHOLD.
"""
from __future__ import annotations

import math
from bisect import bisect_right
from datetime import date, timedelta
from typing import Optional

import pandas as pd

from portfolio_agent.tools.validation_engine import (
    FLAT_THRESHOLD,
    _BUCKET_NAMES,
    actual_return_bucket,
    brier_5bucket,
    compute_distribution_metrics,
    logloss_5bucket,
)

DIRECTIONS = ("UP", "DOWN", "FLAT")
P_COLUMNS = ["p_strong_down", "p_moderate_down", "p_flat", "p_moderate_up", "p_strong_up"]

MIN_NONOVERLAP_N = 30          # below this, UI shows a low-sample warning
_STALE_PRICE_DAYS = 4          # a trailing return is undefined if the series ends >4 days before as_of

# Baselines that predict a label per row.
LABEL_BASELINES = [
    "always_up", "always_down", "always_flat", "majority_class",
    "spy_same_window", "spy_prior_window", "momentum",
]
# Baselines with an analytic expected score (no per-row prediction).
EXPECTED_BASELINES = ["class_frequency_random", "uniform_random"]

BASELINE_LABELS = {
    "apex": "APEX",
    "always_up": "Always UP",
    "always_down": "Always DOWN",
    "always_flat": "Always FLAT",
    "majority_class": "Majority class (in-sample oracle)",
    "class_frequency_random": "Random, class-frequency (expected)",
    "uniform_random": "Random, uniform 1/3 (expected)",
    "spy_same_window": "SPY same window (look-ahead diagnostic, not tradable)",
    "spy_prior_window": "SPY prior window (tradable)",
    "momentum": "Ticker momentum (prior window, tradable)",
}
# Excluded from "best baseline": it peeks at the outcome window's own market move.
LOOKAHEAD_BASELINES = {"spy_same_window"}


# ── helpers ───────────────────────────────────────────────────────────────────

def direction_from_return(ret: Optional[float]) -> Optional[str]:
    if ret is None or (isinstance(ret, float) and math.isnan(ret)):
        return None
    return "UP" if ret > FLAT_THRESHOLD else "DOWN" if ret < -FLAT_THRESHOLD else "FLAT"


def _require_single_horizon(df: pd.DataFrame) -> Optional[int]:
    if df is None or df.empty or "horizon_days" not in df.columns:
        return None
    hs = df["horizon_days"].dropna().unique()
    if len(hs) > 1:
        raise ValueError(f"validation_metrics works per horizon; got horizons {sorted(hs.tolist())}")
    return int(hs[0]) if len(hs) else None


def prepare_rows(df: pd.DataFrame) -> pd.DataFrame:
    """Copy of df restricted to rows with a usable actual_return and an
    UP/DOWN/FLAT predicted_direction, plus `pred_dir` / `actual_dir` columns."""
    if df is None or df.empty:
        return pd.DataFrame(columns=["pred_dir", "actual_dir"])
    d = df.copy()
    d = d[d["actual_return"].notna()]
    d["pred_dir"] = d["predicted_direction"].fillna("").astype(str).str.upper()
    d = d[d["pred_dir"].isin(DIRECTIONS)].copy()
    d["actual_dir"] = d["actual_return"].map(direction_from_return)
    return d.reset_index(drop=True)


def trailing_return(closes: dict[str, float], as_of: str, n_trading_days: int) -> Optional[float]:
    """
    Return over the `n_trading_days` TRADING days up to and including the last
    trading day on or before `as_of` (position-based, so weekends/holidays
    don't count). None if the series is empty, too short, or stale.
    """
    if not closes or n_trading_days <= 0:
        return None
    dates = sorted(closes)
    idx = bisect_right(dates, as_of) - 1
    if idx - n_trading_days < 0:
        return None
    try:
        if (date.fromisoformat(as_of) - date.fromisoformat(dates[idx])).days > _STALE_PRICE_DAYS:
            return None
    except ValueError:
        return None
    start, end = closes[dates[idx - n_trading_days]], closes[dates[idx]]
    if not start or not end:
        return None
    return end / start - 1


def price_requests(df: pd.DataFrame) -> dict[str, tuple[str, str]]:
    """{symbol: (start_iso, end_iso)} of price history the prior-window
    baselines need for these rows (one fetch per ticker, one for SPY). The
    start is padded generously because the window is counted in trading days."""
    d = prepare_rows(df)
    h = _require_single_horizon(d)
    if d.empty or h is None or "as_of_date" not in d.columns:
        return {}
    pad = int(math.ceil(h * 1.6)) + 10
    out: dict[str, tuple[str, str]] = {}
    for sym, grp in list(d.groupby(d["ticker"].str.upper())) + [("SPY", d)]:
        dates = grp["as_of_date"].dropna()
        if dates.empty:
            continue
        start = (date.fromisoformat(str(dates.min())[:10]) - timedelta(days=pad)).isoformat()
        out[sym] = (start, str(dates.max())[:10])
    return out


# ── classification metrics ────────────────────────────────────────────────────

def classification_metrics(actual: pd.Series, pred: pd.Series) -> dict:
    """accuracy, balanced accuracy (mean per-class recall over classes present
    in `actual`), macro-F1 (over classes present in actual or pred; a class
    with no true positives scores F1 = 0), and n."""
    n = len(actual)
    if n == 0:
        return {"accuracy": None, "balanced_accuracy": None, "macro_f1": None, "n": 0}
    a, p = actual.tolist(), pred.tolist()
    acc = sum(x == y for x, y in zip(a, p)) / n
    present = sorted(set(a))
    recalls, f1s = [], []
    for c in sorted(set(a) | set(p)):
        tp = sum(1 for x, y in zip(a, p) if x == c and y == c)
        n_act, n_pred = a.count(c), p.count(c)
        rec = tp / n_act if n_act else 0.0
        prec = tp / n_pred if n_pred else 0.0
        f1s.append(2 * prec * rec / (prec + rec) if (prec + rec) else 0.0)
        if c in present:
            recalls.append(rec)
    return {
        "accuracy": acc,
        "balanced_accuracy": sum(recalls) / len(recalls),
        "macro_f1": sum(f1s) / len(f1s),
        "n": n,
    }


def expected_random_metrics(actual: pd.Series, kind: str) -> dict:
    """Analytic expected scores of a predictor that ignores the inputs:
    'class_frequency_random' picks each class with its own frequency in these
    rows, 'uniform_random' picks UP/DOWN/FLAT with probability 1/3 each."""
    n = len(actual)
    if n == 0:
        return {"accuracy": None, "balanced_accuracy": None, "macro_f1": None, "n": 0}
    freq = {c: (actual == c).sum() / n for c in DIRECTIONS if (actual == c).any()}
    k = len(freq)
    if kind == "class_frequency_random":
        # recall_c = precision_c = F1_c = f_c
        return {"accuracy": sum(f * f for f in freq.values()), "balanced_accuracy": 1 / k,
                "macro_f1": 1 / k, "n": n}
    if kind == "uniform_random":
        f1_present = sum(2 * f * (1 / 3) / (f + 1 / 3) for f in freq.values())
        return {"accuracy": 1 / 3, "balanced_accuracy": 1 / 3, "macro_f1": f1_present / 3, "n": n}
    raise ValueError(kind)


def baseline_labels(df: pd.DataFrame, closes_by_symbol: Optional[dict[str, dict[str, float]]] = None) -> pd.DataFrame:
    """
    Per-row label predictions for each LABEL_BASELINE, aligned to
    prepare_rows(df)'s index; None where a baseline is undefined for that row.

    majority_class is the most frequent ACTUAL class in these rows -- an
    in-sample oracle. spy_same_window reads the row's own benchmark_return over
    the outcome window (look-ahead; "how much does the market explain").
    spy_prior_window / momentum use the SPY / ticker return over the
    horizon_days trading days before as_of_date.
    """
    d = prepare_rows(df)
    h = _require_single_horizon(d)
    closes_by_symbol = closes_by_symbol or {}
    out = pd.DataFrame(index=d.index)
    if d.empty:
        return out
    out["always_up"], out["always_down"], out["always_flat"] = "UP", "DOWN", "FLAT"
    out["majority_class"] = d["actual_dir"].value_counts().idxmax()

    bench = d["benchmark_return"] if "benchmark_return" in d.columns else pd.Series(index=d.index, dtype=float)
    out["spy_same_window"] = bench.map(direction_from_return)

    spy = closes_by_symbol.get("SPY") or {}

    def _prior(sym_closes, as_of):
        r = trailing_return(sym_closes, str(as_of)[:10], h) if (h and as_of is not None) else None
        return direction_from_return(r)

    out["spy_prior_window"] = [_prior(spy, a) for a in d["as_of_date"]]
    out["momentum"] = [
        _prior(closes_by_symbol.get(str(t).upper()) or {}, a)
        for t, a in zip(d["ticker"], d["as_of_date"])
    ]
    return out.astype(object).where(out.notna(), None)


def compare_predictors(df: pd.DataFrame, closes_by_symbol: Optional[dict[str, dict[str, float]]] = None) -> dict:
    """
    APEX vs every baseline on the SAME rows: those where every available
    label baseline is defined (a baseline with zero defined rows -- e.g. no
    price data -- is dropped and listed in `unavailable` instead of emptying
    the table). Returns:
        n_total      rows with a usable direction and return
        n_common     rows in the comparison
        unavailable  baselines dropped for lack of data
        rows         [{key, name, accuracy, balanced_accuracy, macro_f1, n, n_defined}]
        apex_all_rows  APEX metrics on all n_total rows
        best_baseline  {key, name, accuracy} -- best naive baseline accuracy
                       (look-ahead diagnostics excluded), or None
    """
    d = prepare_rows(df)
    _require_single_horizon(d)
    empty = {"n_total": 0, "n_common": 0, "unavailable": [], "rows": [], "apex_all_rows": None, "best_baseline": None}
    if d.empty:
        return empty
    labels = baseline_labels(d, closes_by_symbol)
    unavailable = [b for b in LABEL_BASELINES if labels[b].notna().sum() == 0]
    required = [b for b in LABEL_BASELINES if b not in unavailable]
    mask = labels[required].notna().all(axis=1)
    common, lab = d[mask], labels[mask]

    rows = [{"key": "apex", "name": BASELINE_LABELS["apex"],
             **classification_metrics(common["actual_dir"], common["pred_dir"]),
             "n_defined": len(d)}]
    for b in required:
        rows.append({"key": b, "name": BASELINE_LABELS[b],
                     **classification_metrics(common["actual_dir"], lab[b]),
                     "n_defined": int(labels[b].notna().sum())})
    for b in EXPECTED_BASELINES:
        rows.append({"key": b, "name": BASELINE_LABELS[b],
                     **expected_random_metrics(common["actual_dir"], b), "n_defined": len(d)})

    candidates = [r for r in rows if r["key"] != "apex" and r["key"] not in LOOKAHEAD_BASELINES
                  and r["accuracy"] is not None]
    best = max(candidates, key=lambda r: r["accuracy"]) if candidates else None
    return {
        "n_total": len(d), "n_common": int(mask.sum()), "unavailable": unavailable, "rows": rows,
        "apex_all_rows": classification_metrics(d["actual_dir"], d["pred_dir"]),
        "best_baseline": ({"key": best["key"], "name": best["name"], "accuracy": best["accuracy"]} if best else None),
    }


# ── confusion matrix ──────────────────────────────────────────────────────────

def confusion_matrix(df: pd.DataFrame) -> dict:
    """3x3 counts, rows = predicted, columns = actual, plus the predicted and
    actual direction mix (share of rows)."""
    d = prepare_rows(df)
    _require_single_horizon(d)
    n = len(d)
    matrix = {p: {a: int(((d["pred_dir"] == p) & (d["actual_dir"] == a)).sum()) for a in DIRECTIONS}
              for p in DIRECTIONS}
    return {
        "labels": list(DIRECTIONS), "matrix": matrix, "n": n,
        "predicted_mix": {c: ((d["pred_dir"] == c).sum() / n if n else None) for c in DIRECTIONS},
        "actual_mix": {c: ((d["actual_dir"] == c).sum() / n if n else None) for c in DIRECTIONS},
    }


# ── direction vs SPY ──────────────────────────────────────────────────────────

def beat_market(df: pd.DataFrame) -> dict:
    """
    Among APEX UP / DOWN calls, how often did the call's sign match the sign of
    excess_return (UP: excess > 0, DOWN: excess < 0)? `always_up_hit_rate` is
    the share of ALL rows with excess_return > 0 -- what an always-UP policy
    scores. FLAT calls are reported (n, mean excess) but not scored.
    """
    d = prepare_rows(df)
    _require_single_horizon(d)
    d = d[d["excess_return"].notna()] if (not d.empty and "excess_return" in d.columns) else d.iloc[0:0]
    out = {"n_rows": len(d), "n_calls": 0, "hit_rate": None, "always_up_hit_rate": None,
           "up": {"n": 0, "hit_rate": None, "mean_excess": None},
           "down": {"n": 0, "hit_rate": None, "mean_excess": None},
           "flat": {"n": 0, "mean_excess": None}}
    if d.empty:
        return out
    out["always_up_hit_rate"] = float((d["excess_return"] > 0).mean())
    up, down, flat = (d[d["pred_dir"] == c] for c in ("UP", "DOWN", "FLAT"))
    if len(up):
        out["up"] = {"n": len(up), "hit_rate": float((up["excess_return"] > 0).mean()),
                     "mean_excess": float(up["excess_return"].mean())}
    if len(down):
        out["down"] = {"n": len(down), "hit_rate": float((down["excess_return"] < 0).mean()),
                       "mean_excess": float(down["excess_return"].mean())}
    if len(flat):
        out["flat"] = {"n": len(flat), "mean_excess": float(flat["excess_return"].mean())}
    hits = int((up["excess_return"] > 0).sum() + (down["excess_return"] < 0).sum())
    out["n_calls"] = len(up) + len(down)
    out["hit_rate"] = hits / out["n_calls"] if out["n_calls"] else None
    return out


# ── probability metrics vs sample-specific baselines ──────────────────────────

def _entropy(probs) -> float:
    return -sum(p * math.log(p) for p in probs if p > 0)


def _skill(score: Optional[float], baseline: Optional[float]) -> Optional[float]:
    return 1 - score / baseline if (score is not None and baseline) else None


def probability_baselines(df: pd.DataFrame) -> dict:
    """
    APEX Brier / log-loss against the baseline of always forecasting the
    sample's own base rates, on rows that carry a full p_* distribution:

      binary   UP vs not-UP (actual_up = ret > FLAT_THRESHOLD); matches the
               stored brier_score / log_loss columns.
               baseline Brier = p(1-p), baseline log-loss = binary entropy,
               p = empirical UP rate.
      five     5-bucket multiclass; baseline Brier = sum q_k(1-q_k),
               baseline log-loss = entropy(q), q = empirical bucket frequencies.

    brier_skill = 1 - APEX Brier / baseline Brier (> 0 beats the base rate).
    Baselines are in-sample, so skill is mildly flattering to the baseline.
    """
    out: dict = {"n": 0, "binary": None, "five": None}
    if df is None or df.empty or not set(P_COLUMNS) <= set(df.columns) or "actual_return" not in df.columns:
        return out
    d = df[df["actual_return"].notna() & df[P_COLUMNS].notna().all(axis=1)]
    _require_single_horizon(d)
    if d.empty:
        return out
    bin_b, bin_l, five_b, five_l, ups, buckets = [], [], [], [], [], []
    for rec in d[P_COLUMNS + ["actual_return"]].to_dict("records"):
        ret = float(rec["actual_return"])
        b, l = compute_distribution_metrics(rec, ret)
        fb, fl = brier_5bucket(rec, ret), logloss_5bucket(rec, ret)
        if b is None or fb is None:      # all-zero distribution
            continue
        bin_b.append(b); bin_l.append(l); five_b.append(fb); five_l.append(fl)
        ups.append(1.0 if ret > FLAT_THRESHOLD else 0.0)
        buckets.append(actual_return_bucket(ret))
    n = len(ups)
    if not n:
        return out
    p_up = sum(ups) / n
    q = [buckets.count(name) / n for name in _BUCKET_NAMES]
    mean = lambda xs: sum(xs) / len(xs)
    bb0, bl0 = p_up * (1 - p_up), _entropy([p_up, 1 - p_up])
    fb0, fl0 = sum(x * (1 - x) for x in q), _entropy(q)
    out["n"] = n
    out["binary"] = {"brier": mean(bin_b), "baseline_brier": bb0, "brier_skill": _skill(mean(bin_b), bb0),
                     "log_loss": mean(bin_l), "baseline_log_loss": bl0, "log_loss_skill": _skill(mean(bin_l), bl0),
                     "base_rate": p_up}
    out["five"] = {"brier": mean(five_b), "baseline_brier": fb0, "brier_skill": _skill(mean(five_b), fb0),
                   "log_loss": mean(five_l), "baseline_log_loss": fl0, "log_loss_skill": _skill(mean(five_l), fl0),
                   "bucket_freq": dict(zip(_BUCKET_NAMES, q))}
    return out


# ── effective sample size ─────────────────────────────────────────────────────

def nonoverlap_rows(df: pd.DataFrame) -> pd.DataFrame:
    """
    Per (ticker, horizon): sort by as_of_date (then id, when present) and
    greedily keep a row only if its as_of_date >= the previously kept row's
    evaluation_date -- so no kept row's outcome window overlaps another's.
    A missing evaluation_date falls back to as_of_date + ceil(horizon*7/5)
    calendar days. Cross-ticker correlation (a common market factor) remains.
    """
    if df is None or df.empty:
        return df if df is not None else pd.DataFrame()
    d = df.copy()
    d["_asof"] = pd.to_datetime(d["as_of_date"])
    end = pd.to_datetime(d["evaluation_date"]) if "evaluation_date" in d.columns else pd.Series(pd.NaT, index=d.index)
    fallback = d["_asof"] + pd.to_timedelta(((d["horizon_days"].fillna(0) * 7 / 5).apply(math.ceil)).astype(int), unit="D")
    d["_end"] = end.fillna(fallback)
    sort_cols = ["ticker", "horizon_days", "_asof"] + (["id"] if "id" in d.columns else [])
    d = d.sort_values(sort_cols, kind="stable")
    keep: list = []
    for _, grp in d.groupby(["ticker", "horizon_days"], sort=False):
        last_end = None
        for idx, asof, e in zip(grp.index, grp["_asof"], grp["_end"]):
            if last_end is None or asof >= last_end:
                keep.append(idx)
                last_end = e
    return d.loc[keep].drop(columns=["_asof", "_end"]).sort_index()


def wilson_ci(successes: int, n: int, z: float = 1.96) -> tuple[Optional[float], Optional[float]]:
    """Wilson score interval for a proportion (95% by default)."""
    if n <= 0:
        return None, None
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def effective_sample(df: pd.DataFrame, streak_n: Optional[int] = None) -> dict:
    """
    raw N / streak N / non-overlapping N for one horizon, plus directional
    accuracy and its Wilson 95% CI on the non-overlapping subset. `streak_n`
    comes from web.data.validation._add_streak_ids (the existing streak
    logic); it is passed in rather than reimplemented here. Also returns the
    kept (non-overlapping) rows so baselines can be recomputed on them.
    """
    d = prepare_rows(df)
    _require_single_horizon(d)
    kept = nonoverlap_rows(d) if not d.empty else d
    k = int((kept["pred_dir"] == kept["actual_dir"]).sum()) if len(kept) else 0
    lo, hi = wilson_ci(k, len(kept))
    return {
        "raw_n": len(d), "streak_n": streak_n, "nonoverlap_n": len(kept),
        "nonoverlap_accuracy": (k / len(kept)) if len(kept) else None,
        "ci_low": lo, "ci_high": hi,
        "low_sample": len(kept) < MIN_NONOVERLAP_N,
        "kept": kept,
    }


def horizon_report(df: pd.DataFrame, closes_by_symbol: Optional[dict[str, dict[str, float]]] = None,
                   streak_n: Optional[int] = None) -> dict:
    """Everything the Validation page shows for one horizon, in one call."""
    d = prepare_rows(df)
    _require_single_horizon(d)
    eff = effective_sample(d, streak_n)
    return {
        "comparison": compare_predictors(d, closes_by_symbol),
        "nonoverlap_comparison": compare_predictors(eff["kept"], closes_by_symbol),
        "confusion": confusion_matrix(d),
        "beat_market": beat_market(d),
        "probability": probability_baselines(d),
        "effective_sample": {k: v for k, v in eff.items() if k != "kept"},
    }
