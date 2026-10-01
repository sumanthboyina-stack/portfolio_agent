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

import numpy as np
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


MIN_CS_ROWS = 10               # a date needs this many rows to give a cross-sectional rank correlation
MIN_DATES_WARN = 10            # a date-block bootstrap over fewer distinct dates is not trustworthy
N_BOOT = 1000


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


# ── date-aware ranking metrics ────────────────────────────────────────────────
#
# 3-class accuracy answers "did the label match", which depends on the class
# mix and on how often APEX says FLAT. The questions that matter for use are
# ranking questions: does a higher p_up / composite go with beating SPY, and
# do UP-labelled names beat FLAT-labelled ones? Predictions made on the same
# date share one market move, so every interval here is a DATE-BLOCK
# bootstrap: whole as_of_dates are resampled with replacement, never rows.

def _rank(a) -> np.ndarray:
    return pd.Series(np.asarray(a, dtype=float)).rank(method="average").to_numpy()


def auc_score(scores, positives) -> Optional[float]:
    """P(score of a random positive > score of a random negative), ties = 1/2
    (Mann-Whitney). None if either class is empty."""
    y = np.asarray(positives, dtype=bool)
    n_pos, n_neg = int(y.sum()), int((~y).sum())
    if n_pos == 0 or n_neg == 0:
        return None
    r = _rank(scores)
    return float((r[y].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def spearman(a, b) -> Optional[float]:
    """Spearman rank correlation (ties averaged). None if either side is constant or n < 3."""
    if len(a) < 3:
        return None
    ra, rb = _rank(a), _rank(b)
    if ra.std() == 0 or rb.std() == 0:
        return None
    return float(np.corrcoef(ra, rb)[0, 1])


def p_up_series(df: pd.DataFrame) -> pd.Series:
    """P(UP) = (p_moderate_up + p_strong_up) / total, NaN where any p_* is missing or total <= 0."""
    if not set(P_COLUMNS) <= set(df.columns):
        return pd.Series(np.nan, index=df.index)
    p = df[P_COLUMNS].astype(float)
    total = p.sum(axis=1).where(lambda t: t > 0)
    out = (p["p_moderate_up"] + p["p_strong_up"]) / total
    return out.where(p.notna().all(axis=1))


def _ci(draws: np.ndarray) -> tuple[Optional[float], Optional[float]]:
    draws = draws[~np.isnan(draws)]
    if len(draws) == 0:
        return None, None
    lo, hi = np.percentile(draws, [2.5, 97.5])
    return float(lo), float(hi)


def _date_keys(d: pd.DataFrame) -> pd.Series:
    return d["as_of_date"].astype(str).str[:10]


def _per_date_stat(d: pd.DataFrame, fn, min_rows: int, keys: Optional[pd.Series] = None) -> dict[str, float]:
    """{date: fn(group)} for dates with >= min_rows rows where fn is defined."""
    out = {}
    for k, g in d.groupby(_date_keys(d) if keys is None else keys):
        if len(g) >= min_rows:
            v = fn(g)
            if v is not None and not math.isnan(v):
                out[k] = v
    return out


def _mean_over_dates(per_date: dict[str, float], n_boot: int, rng) -> dict:
    """Equal-weight mean of per-date values + date-block bootstrap CI."""
    vals = np.array(list(per_date.values()), dtype=float)
    if len(vals) == 0:
        return {"mean": None, "ci_low": None, "ci_high": None, "n_dates": 0, "share_positive": None}
    draws = vals[rng.integers(0, len(vals), size=(n_boot, len(vals)))].mean(axis=1)
    lo, hi = _ci(draws)
    return {"mean": float(vals.mean()), "ci_low": lo, "ci_high": hi, "n_dates": len(vals),
            "share_positive": float((vals > 0).mean())}


def auc_vs_beat_spy(d: pd.DataFrame, score: pd.Series, n_boot: int, rng, min_rows: int = MIN_CS_ROWS) -> dict:
    """
    AUC of `score` against "beat SPY" (excess_return > 0), two ways:
      auc         pooled over all rows (date-block bootstrap CI)
      within_date mean over dates of the per-date AUC (dates with >= min_rows
                  rows and both outcomes), CI from resampling those dates --
                  immune to dates differing in how many names beat SPY.
    """
    x = pd.DataFrame({"s": score, "y": d["excess_return"] > 0, "k": _date_keys(d)})
    x = x[score.notna() & d["excess_return"].notna()]
    out = {"auc": None, "ci_low": None, "ci_high": None, "n": len(x), "n_pos": int(x["y"].sum()) if len(x) else 0,
           "n_dates": int(x["k"].nunique()) if len(x) else 0, "within_date": None}
    if not len(x):
        return out
    s, y = x["s"].to_numpy(), x["y"].to_numpy()
    out["auc"] = auc_score(s, y)
    if out["auc"] is None:
        return out
    groups = [np.flatnonzero((x["k"] == k).to_numpy()) for k in x["k"].unique()]
    draws = np.empty(n_boot)
    for b in range(n_boot):
        idx = np.concatenate([groups[i] for i in rng.integers(0, len(groups), len(groups))])
        a = auc_score(s[idx], y[idx])
        draws[b] = np.nan if a is None else a
    out["ci_low"], out["ci_high"] = _ci(draws)
    per_date = _per_date_stat(x, lambda g: auc_score(g["s"].to_numpy(), g["y"].to_numpy()), min_rows, keys=x["k"])
    out["within_date"] = _mean_over_dates(per_date, n_boot, rng)
    return out


def rank_ic(d: pd.DataFrame, score: pd.Series, n_boot: int, rng, min_rows: int = MIN_CS_ROWS) -> dict:
    """Daily cross-sectional Spearman correlation between `score` and
    excess_return, averaged over dates (equal weight; dates with fewer than
    min_rows rows are dropped and counted)."""
    x = d.assign(_s=score)
    x = x[x["_s"].notna() & x["excess_return"].notna()]
    n_dates_all = int(_date_keys(x).nunique()) if len(x) else 0
    per_date = _per_date_stat(x, lambda g: spearman(g["_s"].to_numpy(), g["excess_return"].to_numpy()), min_rows)
    out = _mean_over_dates(per_date, n_boot, rng)
    out["n_dates_dropped"] = n_dates_all - out["n_dates"]
    out["n_rows"] = int(sum(len(g) for k, g in x.groupby(_date_keys(x)) if k in per_date))
    return out


def label_excess(d: pd.DataFrame, n_boot: int, rng) -> dict:
    """
    Mean excess_return (vs SPY) by predicted label with date-block bootstrap CIs, the number of
    distinct dates behind each, and the pairwise differences UP-FLAT, UP-DOWN, FLAT-DOWN. All
    labels and differences share the same bootstrap draws, so the intervals are comparable.
    """
    x = d[d["excess_return"].notna()]
    keys = sorted(_date_keys(x).unique()) if len(x) else []
    out: dict = {"n_dates": len(keys), "labels": {}, "diffs": {}}
    if not keys:
        return out
    kidx = {k: i for i, k in enumerate(keys)}
    di = _date_keys(x).map(kidx).to_numpy()
    ex = x["excess_return"].to_numpy(dtype=float)
    w = rng.multinomial(len(keys), [1 / len(keys)] * len(keys), size=n_boot).astype(float)   # B x D
    sums, cnts, means = {}, {}, {}
    for lab in DIRECTIONS:
        m = (x["pred_dir"] == lab).to_numpy()
        sums[lab] = np.bincount(di[m], weights=ex[m], minlength=len(keys))
        cnts[lab] = np.bincount(di[m], minlength=len(keys)).astype(float)
        with np.errstate(invalid="ignore", divide="ignore"):
            means[lab] = (w @ sums[lab]) / (w @ cnts[lab])
        n = int(m.sum())
        lo, hi = _ci(means[lab])
        out["labels"][lab] = {"n": n, "n_dates": int((cnts[lab] > 0).sum()),
                              "mean": float(ex[m].mean()) if n else None, "ci_low": lo, "ci_high": hi}
    for a, b in (("UP", "FLAT"), ("UP", "DOWN"), ("FLAT", "DOWN")):
        ma, mb = out["labels"][a]["mean"], out["labels"][b]["mean"]
        lo, hi = _ci(means[a] - means[b])
        out["diffs"][f"{a}-{b}"] = {"diff": None if ma is None or mb is None else ma - mb, "ci_low": lo, "ci_high": hi}
    return out


def within_ticker_contrast(d: pd.DataFrame, n_boot: int, rng, a: str = "UP", b: str = "FLAT") -> dict:
    """
    Composition-free version of the label comparison: for every ticker that
    has BOTH an `a` call and a `b` call, mean excess under `a` minus mean
    excess under `b` (so a stock that is simply always a good or bad
    performer cancels out); then the equal-weight mean across such tickers.
    Date-block bootstrap CI (tickers are re-evaluated per draw).
    """
    x = d[d["excess_return"].notna() & d["pred_dir"].isin([a, b])]
    empty = {"pair": f"{a}-{b}", "mean_diff": None, "ci_low": None, "ci_high": None,
             "n_tickers": 0, "n_obs": 0, "n_dates": 0}
    if x.empty:
        return empty
    keys = sorted(_date_keys(x).unique())
    tick = x["ticker"].astype(str).str.upper()
    tk = {t: i for i, t in enumerate(sorted(tick.unique()))}
    kidx = {k: i for i, k in enumerate(keys)}
    ti, di = tick.map(tk).to_numpy(), _date_keys(x).map(kidx).to_numpy()
    ex = x["excess_return"].to_numpy(dtype=float)
    mats = {}
    for lab in (a, b):
        m = (x["pred_dir"] == lab).to_numpy()
        S = np.zeros((len(tk), len(keys))); C = np.zeros_like(S)
        np.add.at(S, (ti[m], di[m]), ex[m]); np.add.at(C, (ti[m], di[m]), 1.0)
        mats[lab] = (S, C)
    def _contrast(wv):                     # wv: date weights (D, B); returns mean diff per draw
        with np.errstate(invalid="ignore", divide="ignore"):
            ma = (mats[a][0] @ wv) / (mats[a][1] @ wv)
            mb = (mats[b][0] @ wv) / (mats[b][1] @ wv)
        diff = ma - mb                      # (T, B), NaN where a ticker lacks a side in that draw
        return np.nanmean(diff, axis=0) if np.isfinite(diff).any() else np.full(diff.shape[1], np.nan)
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        point = _contrast(np.ones((len(keys), 1)))[0]
        if np.isnan(point):
            return {**empty, "n_dates": len(keys)}
        w = rng.multinomial(len(keys), [1 / len(keys)] * len(keys), size=n_boot).T.astype(float)
        draws = _contrast(w)
    both = (mats[a][1].sum(axis=1) > 0) & (mats[b][1].sum(axis=1) > 0)
    lo, hi = _ci(draws)
    return {"pair": f"{a}-{b}", "mean_diff": float(point), "ci_low": lo, "ci_high": hi, "n_tickers": int(both.sum()),
            "n_obs": int(both @ (mats[a][1].sum(axis=1) + mats[b][1].sum(axis=1))), "n_dates": len(keys)}


def _core_ranking(d: pd.DataFrame, n_boot: int, seed: int) -> dict:
    rng = np.random.default_rng(seed)
    p_up = p_up_series(d)
    comp = d["composite_score"].astype(float) if "composite_score" in d.columns else pd.Series(np.nan, index=d.index)
    n_dates = int(_date_keys(d).nunique()) if len(d) else 0
    return {
        "n_rows": len(d), "n_dates": n_dates, "low_dates": n_dates < MIN_DATES_WARN,
        "auc": {"p_up": auc_vs_beat_spy(d, p_up, n_boot, rng), "composite": auc_vs_beat_spy(d, comp, n_boot, rng)},
        "rank_ic": {"p_up": rank_ic(d, p_up, n_boot, rng), "composite": rank_ic(d, comp, n_boot, rng)},
        "label_excess": label_excess(d, n_boot, rng),
        "within_ticker": within_ticker_contrast(d, n_boot, rng),
    }


def ranking_report(df: pd.DataFrame, n_boot: int = N_BOOT, seed: int = 0, by_model: bool = True) -> dict:
    """
    Date-aware ranking metrics for ONE horizon (see the block comment above):
    AUC of p_up / composite vs beating SPY, daily cross-sectional rank IC,
    mean excess by predicted label, the within-ticker UP-vs-FLAT contrast,
    and (by_model=True) the same per model_name. Seeded, so reruns agree.
    """
    d = prepare_rows(df)
    _require_single_horizon(d)
    if d.empty:
        return {"n_rows": 0, "n_dates": 0, "low_dates": True, "auc": {}, "rank_ic": {}, "label_excess": {"labels": {}, "diffs": {}},
                "within_ticker": {}, "by_model": {}}
    out = _core_ranking(d, n_boot, seed)
    out["by_model"] = {}
    if by_model:
        models = d["model_name"].fillna("unknown") if "model_name" in d.columns else pd.Series("unknown", index=d.index)
        for name, g in d.groupby(models):
            out["by_model"][str(name)] = _core_ranking(g, max(200, n_boot // 3), seed)
    return out


# ── FLAT-label diagnostic ─────────────────────────────────────────────────────

_BUCKET_CLASS = {"p_strong_down": "DOWN", "p_moderate_down": "DOWN", "p_flat": "FLAT",
                 "p_moderate_up": "UP", "p_strong_up": "UP"}


def flat_diagnostic(df: pd.DataFrame) -> dict:
    """
    Does APEX's FLAT label agree with APEX's own distribution?

    For FLAT calls: how often is p_flat the largest of the five buckets (strictly / tied), and the
    mean p_flat vs the up and down mass. Then two rules that DERIVE a direction from the
    probabilities -- argmax5 (class of the largest bucket) and mass3 (largest of
    p_down, p_flat, p_up masses) -- scored against actual direction on the same rows (ties
    excluded), next to APEX's own labels. Finally, mean p_flat vs the realised FLAT rate.
    """
    d = prepare_rows(df)
    _require_single_horizon(d)
    out: dict = {"n_rows": 0}
    if d.empty or not set(P_COLUMNS) <= set(d.columns):
        return out
    d = d[d[P_COLUMNS].notna().all(axis=1)]
    p = d[P_COLUMNS].astype(float)
    p = p.div(p.sum(axis=1).where(lambda t: t > 0), axis=0).dropna()
    d = d.loc[p.index]
    out["n_rows"] = len(d)
    if d.empty:
        return out
    mx = p.max(axis=1)
    n_at_max = p.eq(mx, axis=0).sum(axis=1)
    mass = pd.DataFrame({"DOWN": p["p_strong_down"] + p["p_moderate_down"], "FLAT": p["p_flat"],
                         "UP": p["p_moderate_up"] + p["p_strong_up"]})
    mmx = mass.max(axis=1)
    m_at_max = mass.eq(mmx, axis=0).sum(axis=1)

    f = d["pred_dir"] == "FLAT"
    out["flat_calls"] = {
        "n": int(f.sum()),
        "p_flat_strict_max": int((f & (p["p_flat"] == mx) & (n_at_max == 1)).sum()),
        "p_flat_tied_max": int((f & (p["p_flat"] == mx) & (n_at_max > 1)).sum()),
        "p_flat_not_max": int((f & (p["p_flat"] < mx)).sum()),
        "mass3_flat_plurality": int((f & (mass["FLAT"] == mmx) & (m_at_max == 1)).sum()),
        "mean_p_flat": float(p.loc[f, "p_flat"].mean()) if f.any() else None,
        "mean_p_up": float(mass.loc[f, "UP"].mean()) if f.any() else None,
        "mean_p_down": float(mass.loc[f, "DOWN"].mean()) if f.any() else None,
    }
    arg5 = p.idxmax(axis=1).map(_BUCKET_CLASS).where(n_at_max == 1)
    arg3 = mass.idxmax(axis=1).where(m_at_max == 1)
    rules = {}
    for name, derived in (("argmax5", arg5), ("mass3", arg3)):
        ok = derived.notna()
        rules[name] = {
            "n": int(ok.sum()), "n_ambiguous": int((~ok).sum()),
            "agrees_with_label": float((derived[ok] == d.loc[ok, "pred_dir"]).mean()) if ok.any() else None,
            "derived_accuracy": float((derived[ok] == d.loc[ok, "actual_dir"]).mean()) if ok.any() else None,
            "apex_accuracy_same_rows": float((d.loc[ok, "pred_dir"] == d.loc[ok, "actual_dir"]).mean()) if ok.any() else None,
            "derived_mix": {c: float((derived[ok] == c).mean()) for c in DIRECTIONS} if ok.any() else None,
        }
    out["derived_rules"] = rules
    out["calibration"] = {"mean_p_flat_all_rows": float(p["p_flat"].mean()),
                          "actual_flat_rate": float((d["actual_dir"] == "FLAT").mean())}
    return out


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
        "ranking": ranking_report(d),
        "flat_diagnostic": flat_diagnostic(d),
        "effective_sample": {k: v for k, v in eff.items() if k != "kept"},
    }
