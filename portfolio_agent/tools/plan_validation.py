"""
Plan validation — did the simulator and the model's conviction earn their keep?

Once a planned forecast matures, plan_outcomes holds what ACTUALLY happened (TARGET / STOP / TIMEOUT and the realized R).
Against that, per horizon:

  a) simulator calibration   reliability table of p_target_first, 3-outcome Brier vs a base-rate Brier
  b) conviction calibration  realized TARGET rate and mean R by raw-conviction band
  c) edge over the simulator mean(hit - p_target_first): does the model pick calls that beat the stock's own odds?
  d) agreement grids         conviction band x sim-probability band, and the 'sim disagrees' cell vs the 'both confident' cell
  e) blended probability     logistic fit  P(TARGET) = sigmoid(a + b*logit(p_sim) + c*conviction), used only if it earns its place

Every interval resamples whole as_of_dates, never rows (same-day calls are correlated), the scheme validation_metrics uses.
The functions are pure (a DataFrame in, plain dicts out); load_plan_rows and the blend_models helpers are the DB parts.

Rows need: prediction_id, as_of_date, horizon_days, conviction, p_target_first, p_stop_first, p_timeout,
p_target_first_zero_drift, plan_outcome, realized_r.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from portfolio_agent.tools.validation_metrics import N_BOOT, _ci, _date_keys

MIN_CELL_N = 10                 # the UI hides anything thinner than this
MIN_DISAGREE_N = 30             # the cap recommendation needs both cells at least this large
CONVICTION_BANDS = [("1-4", 0.0, 5.0), ("5-6", 5.0, 7.0), ("7-8", 7.0, 9.0), ("9-10", 9.0, 10.01)]
P_BANDS = [("<15%", 0.0, 0.15), ("15-25%", 0.15, 0.25), ("25-35%", 0.25, 0.35), ("35%+", 0.35, 1.01)]
_OUTCOMES = ("TARGET", "STOP", "TIMEOUT")
_P_CLIP = 0.01


# ── intervals: resample whole as_of_dates ─────────────────────────────────────

def _ratio_ci(d: pd.DataFrame, num: np.ndarray, den: np.ndarray | None = None, n_boot: int = N_BOOT, seed: int = 0) -> dict:
    """Estimate sum(num)/sum(den) (a plain mean when den is None) with a date-block bootstrap 95% interval."""
    n = len(d)
    if n == 0:
        return {"est": None, "lo": None, "hi": None, "n": 0, "n_dates": 0}
    num = np.asarray(num, dtype=float)
    den = np.ones(n) if den is None else np.asarray(den, dtype=float)
    keys = _date_keys(d).to_numpy()
    uniq, inv = np.unique(keys, return_inverse=True)
    sn, sd = np.bincount(inv, weights=num), np.bincount(inv, weights=den)
    est = float(num.sum() / den.sum()) if den.sum() else None
    lo = hi = None
    if len(uniq) >= 3 and est is not None:
        rng = np.random.default_rng(seed)
        pick = rng.integers(0, len(uniq), size=(n_boot, len(uniq)))
        tot = sd[pick].sum(axis=1)
        draws = np.where(tot > 0, sn[pick].sum(axis=1) / np.where(tot > 0, tot, 1), np.nan)
        lo, hi = _ci(draws)
    return {"est": est, "lo": lo, "hi": hi, "n": n, "n_dates": int(len(uniq))}


def _hit(d: pd.DataFrame) -> np.ndarray:
    return (d["plan_outcome"] == "TARGET").to_numpy(dtype=float)


def _band(value: float, bands) -> str | None:
    for name, lo, hi in bands:
        if lo <= value < hi:
            return name
    return None


# ── a) simulator calibration ──────────────────────────────────────────────────

def reliability_table(d: pd.DataFrame, n_bins: int = 10) -> list[dict]:
    """Fixed-width probability bins: mean predicted P(target first) vs the realized TARGET rate, with n and an interval."""
    out = []
    p = d["p_target_first"].to_numpy(dtype=float)
    idx = np.minimum((p * n_bins).astype(int), n_bins - 1)
    for b in range(n_bins):
        g = d[idx == b]
        if g.empty:
            continue
        r = _ratio_ci(g, _hit(g), seed=b)
        out.append({"bin_lo": b / n_bins, "bin_hi": (b + 1) / n_bins, "n": len(g), "mean_pred": float(g["p_target_first"].mean()),
                    "realized": r["est"], "lo": r["lo"], "hi": r["hi"]})
    return out


def _brier_rows(d: pd.DataFrame, probs: np.ndarray) -> np.ndarray:
    """Per-row 3-outcome Brier: the sum over target / stop / timeout of (p - indicator)^2 (0 best, 2 worst)."""
    obs = np.stack([(d["plan_outcome"] == o).to_numpy(dtype=float) for o in _OUTCOMES], axis=1)
    return ((probs - obs) ** 2).sum(axis=1)


def simulator_brier(d: pd.DataFrame) -> dict:
    """The simulator's 3-outcome Brier vs a base-rate forecast (every row gets the overall target / stop / timeout rates)."""
    if d.empty:
        return {"n": 0, "brier_sim": None, "brier_base": None, "skill": None, "gain_lo": None, "gain_hi": None}
    sim = _brier_rows(d, d[["p_target_first", "p_stop_first", "p_timeout"]].to_numpy(dtype=float))
    base_rates = np.array([(d["plan_outcome"] == o).mean() for o in _OUTCOMES])
    base = _brier_rows(d, np.tile(base_rates, (len(d), 1)))
    gain = _ratio_ci(d, base - sim, seed=11)             # positive = the simulator beats the base rate
    return {"n": len(d), "brier_sim": float(sim.mean()), "brier_base": float(base.mean()),
            "skill": float(1 - sim.mean() / base.mean()) if base.mean() > 0 else None,
            "gain_lo": gain["lo"], "gain_hi": gain["hi"], "base_rates": dict(zip(_OUTCOMES, map(float, base_rates)))}


# ── b) conviction calibration ─────────────────────────────────────────────────

def conviction_bands(d: pd.DataFrame) -> list[dict]:
    """Realized TARGET rate and mean realized R by raw-conviction band."""
    out = []
    band = d["conviction"].map(lambda v: _band(float(v), CONVICTION_BANDS) if pd.notna(v) else None)
    for name, _, _ in CONVICTION_BANDS:
        g = d[band == name]
        if g.empty:
            out.append({"band": name, "n": 0, "target_rate": None, "lo": None, "hi": None, "mean_r": None})
            continue
        t, r = _ratio_ci(g, _hit(g), seed=21), _ratio_ci(g, g["realized_r"].to_numpy(dtype=float), seed=22)
        out.append({"band": name, "n": len(g), "target_rate": t["est"], "lo": t["lo"], "hi": t["hi"], "mean_r": r["est"]})
    return out


# ── c) edge over the simulator ────────────────────────────────────────────────

def edge_over_simulator(d: pd.DataFrame) -> dict:
    """
    edge = mean(realized TARGET hit - p_target_first) over planned rows, with a date-block 95% interval; also against the zero-drift
    baseline ('the stock just wiggles'). Edge above 0 with the interval above 0: the model's calls beat the stock's own odds.
    Edge near 0: the simulator alone would have done as well.
    """
    if d.empty:
        return {"n": 0, "edge": None, "lo": None, "hi": None, "vs_zero_drift": None, "by_band": []}
    hit = _hit(d)
    e = _ratio_ci(d, hit - d["p_target_first"].to_numpy(dtype=float), seed=31)
    z = _ratio_ci(d, hit - d["p_target_first_zero_drift"].to_numpy(dtype=float), seed=32)
    band = d["conviction"].map(lambda v: _band(float(v), CONVICTION_BANDS) if pd.notna(v) else None)
    by_band = []
    for name, _, _ in CONVICTION_BANDS:
        g = band == name
        if not g.any():
            by_band.append({"band": name, "n": 0, "edge": None, "lo": None, "hi": None})
            continue
        r = _ratio_ci(d[g], hit[g.to_numpy()] - d.loc[g, "p_target_first"].to_numpy(dtype=float), seed=33)
        by_band.append({"band": name, "n": int(g.sum()), "edge": r["est"], "lo": r["lo"], "hi": r["hi"]})
    return {"n": len(d), "n_dates": e["n_dates"], "edge": e["est"], "lo": e["lo"], "hi": e["hi"],
            "mean_sim": float(d["p_target_first"].mean()), "realized": float(hit.mean()),
            "vs_zero_drift": {"edge": z["est"], "lo": z["lo"], "hi": z["hi"]}, "by_band": by_band}


# ── d) agreement grids ────────────────────────────────────────────────────────

def agreement_grid(d: pd.DataFrame, conv_bands, p_bands) -> dict:
    """conviction band x simulator-probability band -> n, realized TARGET rate, mean realized R."""
    cb = d["conviction"].map(lambda v: _band(float(v), conv_bands) if pd.notna(v) else None)
    pb = d["p_target_first"].map(lambda v: _band(float(v), p_bands))
    cells = []
    for cname, _, _ in conv_bands:
        for pname, _, _ in p_bands:
            g = d[(cb == cname) & (pb == pname)]
            cells.append({"conviction": cname, "sim_p": pname, "n": len(g),
                          "target_rate": float(_hit(g).mean()) if len(g) else None,
                          "mean_r": float(g["realized_r"].mean()) if len(g) else None})
    return {"rows": [b[0] for b in conv_bands], "cols": [b[0] for b in p_bands], "cells": cells}


def two_by_two(d: pd.DataFrame, min_conviction: float, max_p_target: float) -> dict:
    """Confident (conviction >= min) or not x simulator agrees (P(target) >= max) or not."""
    conf = d["conviction"].astype(float) >= float(min_conviction)
    agree = d["p_target_first"].astype(float) >= float(max_p_target)
    cells = {}
    for ck, cm in (("confident", conf), ("not_confident", ~conf)):
        for ak, am in (("sim_agrees", agree), ("sim_disagrees", ~agree)):
            g = d[cm & am]
            cells[f"{ck}|{ak}"] = {"n": len(g), "target_rate": float(_hit(g).mean()) if len(g) else None,
                                  "mean_r": float(g["realized_r"].mean()) if len(g) else None}
    return cells


def disagreement_test(d: pd.DataFrame, min_conviction: float, max_p_target: float) -> dict:
    """
    The 'sim_disagrees' cell (confident, simulator says the target is rare) against the 'both confident' cell: n, realized TARGET
    rates and a two-proportion p-value. significantly_worse needs p < 0.05, the disagree rate below the confident rate and both
    cells at least MIN_DISAGREE_N. (The test treats rows as independent; same-day rows are correlated, so it is on the generous side.)
    """
    from portfolio_agent.tools.validation_engine import _two_proportion_p_value
    conf = d["conviction"].astype(float) >= float(min_conviction)
    agree = d["p_target_first"].astype(float) >= float(max_p_target)
    dis, both = d[conf & ~agree], d[conf & agree]
    xd, xb = int(_hit(dis).sum()), int(_hit(both).sum())
    p = _two_proportion_p_value(xd, len(dis), xb, len(both))
    rd = xd / len(dis) if len(dis) else None
    rb = xb / len(both) if len(both) else None
    worse = bool(p is not None and p < 0.05 and rd is not None and rb is not None and rd < rb
                 and len(dis) >= MIN_DISAGREE_N and len(both) >= MIN_DISAGREE_N)
    return {"n_disagree": len(dis), "rate_disagree": rd, "n_confident": len(both), "rate_confident": rb,
            "p_value": p, "significantly_worse": worse}


# ── e) blended probability ────────────────────────────────────────────────────

def _logit(p) -> np.ndarray:
    p = np.clip(np.asarray(p, dtype=float), _P_CLIP, 1 - _P_CLIP)
    return np.log(p / (1 - p))


def _sigmoid(z) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def fit_logistic(X: np.ndarray, y: np.ndarray, l2: float = 1e-6, iters: int = 30) -> np.ndarray:
    """Logistic regression by Newton / IRLS. X includes its own intercept column; a tiny ridge keeps a separable fit finite."""
    X, y = np.asarray(X, dtype=float), np.asarray(y, dtype=float)
    beta = np.zeros(X.shape[1])
    ridge = np.eye(X.shape[1]) * l2
    for _ in range(iters):
        p = _sigmoid(X @ beta)
        grad = X.T @ (y - p) - ridge @ beta
        hess = (X * (p * (1 - p))[:, None]).T @ X + ridge
        step = np.linalg.solve(hess, grad)
        beta = beta + step
        if np.max(np.abs(step)) < 1e-9:
            break
    return beta


def _design(p_sim, conviction) -> np.ndarray:
    return np.column_stack([np.ones(len(p_sim)), _logit(p_sim), np.asarray(conviction, dtype=float)])


def blend_probability(coef, p_sim, conviction) -> np.ndarray:
    """sigmoid(a + b*logit(p_sim) + c*conviction); p_sim is clipped to [0.01, 0.99] first."""
    a, b, c = coef
    return _sigmoid(a + b * _logit(p_sim) + c * np.asarray(conviction, dtype=float))


def fit_blend(d: pd.DataFrame, min_n: int, folds: int = 5, seed: int = 0) -> dict:
    """
    Fit one horizon's blend. 'insufficient_data' below min_n matured rows. Otherwise returns the coefficients (fitted on every row) and
    two Brier scores for the TARGET outcome: the blend's, measured OUT OF SAMPLE by date-blocked cross-validation (so three free
    parameters cannot flatter themselves), and the simulator alone. `valid` = the blend beats the simulator alone.
    """
    d = d.dropna(subset=["p_target_first", "conviction"])
    if len(d) < min_n:
        return {"status": "insufficient_data", "n": len(d), "min_n": min_n}
    y = _hit(d)
    X = _design(d["p_target_first"].to_numpy(), d["conviction"].to_numpy())
    coef = fit_logistic(X, y)
    dates = _date_keys(d).to_numpy()
    uniq = np.unique(dates)
    rng = np.random.default_rng(seed)
    fold_of = dict(zip(rng.permutation(uniq), np.arange(len(uniq)) % max(2, min(folds, len(uniq)))))
    fold = np.array([fold_of[x] for x in dates])
    oos = np.full(len(d), np.nan)
    for k in np.unique(fold):
        tr, te = fold != k, fold == k
        if tr.sum() < 10 or y[tr].min() == y[tr].max():
            continue
        oos[te] = _sigmoid(X[te] @ fit_logistic(X[tr], y[tr]))
    ok = ~np.isnan(oos)
    p_sim = np.clip(d["p_target_first"].to_numpy(dtype=float), _P_CLIP, 1 - _P_CLIP)
    if not ok.any():
        return {"status": "insufficient_data", "n": len(d), "min_n": min_n}
    brier_blend = float(np.mean((oos[ok] - y[ok]) ** 2))
    brier_sim = float(np.mean((p_sim[ok] - y[ok]) ** 2))
    p_hat = _sigmoid(X @ coef)
    slope = float(np.mean(p_hat * (1 - p_hat)))
    return {"status": "ok", "n": len(d), "a": float(coef[0]), "b": float(coef[1]), "c": float(coef[2]),
            "brier_blend": brier_blend, "brier_sim_only": brier_sim, "valid": brier_blend < brier_sim,
            # plain-words effect sizes at the typical row: percentage points of P(target) per +1 conviction / per +10 pts of sim p
            "pts_per_conviction": float(coef[2] * slope * 100),
            "pts_per_10pts_sim": float(coef[1] * slope * 100 * 0.10 / max(float(np.mean(p_sim) * (1 - np.mean(p_sim))), 1e-6))}


# ── data ──────────────────────────────────────────────────────────────────────

def load_plan_rows(roles=None, since: str | None = None) -> pd.DataFrame:
    """Matured planned shared forecasts with an 'ok' simulation and a recorded outcome, one row per prediction."""
    from portfolio_agent.tools.prediction_db import SHARED_SCOPES, VALIDATION_ROLES, _db, role_clause, scope_clause
    sc, sp = scope_clause(SHARED_SCOPES)
    rc, rp = role_clause(tuple(roles or VALIDATION_ROLES))
    sql = f"""SELECT p.id AS prediction_id, p.ticker, p.as_of_date, p.horizon_days,
                     COALESCE(p.raw_conviction_score, p.conviction_score) AS conviction, p.guardrail_flags,
                     s.p_target_first, s.p_stop_first, s.p_timeout, s.p_target_first_zero_drift,
                     o.plan_outcome, o.realized_r
              FROM predictions p
              JOIN trade_plans t ON t.prediction_id = p.id AND t.plan_status = 'planned'
              JOIN plan_simulations s ON s.prediction_id = p.id AND s.sim_status = 'ok'
              JOIN plan_outcomes o ON o.prediction_id = p.id
              WHERE p.{sc} AND p.{rc}""" + (" AND p.as_of_date >= ?" if since else "")
    with _db() as c:
        rows = c.execute(sql, [*sp, *rp, *([since] if since else [])]).fetchall()
    return pd.DataFrame([dict(r) for r in rows], columns=[
        "prediction_id", "ticker", "as_of_date", "horizon_days", "conviction", "guardrail_flags", "p_target_first", "p_stop_first",
        "p_timeout", "p_target_first_zero_drift", "plan_outcome", "realized_r"])


def horizon_report(d: pd.DataFrame, min_conviction: float = 7.0, max_p_target: float = 0.35, blend_min_n: int = 150) -> dict:
    """Everything above for one horizon's rows."""
    return {"n": len(d), "n_dates": int(_date_keys(d).nunique()) if len(d) else 0,
            "reliability": reliability_table(d), "brier": simulator_brier(d), "conviction": conviction_bands(d),
            "edge": edge_over_simulator(d), "grid4": agreement_grid(d, CONVICTION_BANDS, P_BANDS),
            "grid2": two_by_two(d, min_conviction, max_p_target),
            "disagreement": disagreement_test(d, min_conviction, max_p_target),
            "blend": fit_blend(d, blend_min_n)}


def plan_report(df: pd.DataFrame | None = None, settings: dict | None = None) -> dict[int, dict]:
    """{horizon_days: horizon_report}, never mixing horizons."""
    if settings is None:
        from portfolio_agent.tools.engine_settings_db import get_sim_settings
        settings = get_sim_settings()
    df = load_plan_rows() if df is None else df
    return {int(h): horizon_report(g, settings["sim_flag_min_conviction"], settings["sim_flag_max_p_target"], settings["blend_min_n"])
            for h, g in df.groupby("horizon_days")} if not df.empty else {}


# ── stored blend models ───────────────────────────────────────────────────────

def fit_and_store_blends(df: pd.DataFrame | None = None, settings: dict | None = None) -> dict[int, dict]:
    """Refit each horizon with at least blend_min_n matured rows and store it in blend_models. Called weekly."""
    from portfolio_agent.tools.prediction_db import _db
    if settings is None:
        from portfolio_agent.tools.engine_settings_db import get_sim_settings
        settings = get_sim_settings()
    df = load_plan_rows() if df is None else df
    out: dict[int, dict] = {}
    for h, g in (df.groupby("horizon_days") if not df.empty else []):
        res = fit_blend(g, settings["blend_min_n"])
        out[int(h)] = res
        if res["status"] == "ok":
            with _db() as c:
                c.execute("INSERT OR REPLACE INTO blend_models (horizon, a, b, c, n, brier_blend, brier_sim_only, fitted_at) "
                          "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                          [int(h), res["a"], res["b"], res["c"], res["n"], res["brier_blend"], res["brier_sim_only"],
                           datetime.now(timezone.utc).isoformat(timespec="seconds")])
                c.commit()
    return out


def get_valid_blend(horizon_days: int) -> dict | None:
    """The stored blend for a horizon, only if it beat the simulator alone (brier_blend < brier_sim_only)."""
    from portfolio_agent.tools.prediction_db import _db
    with _db() as c:
        row = c.execute("SELECT * FROM blend_models WHERE horizon = ?", [int(horizon_days)]).fetchone()
    if row is None or row["brier_blend"] is None or row["brier_sim_only"] is None or not row["brier_blend"] < row["brier_sim_only"]:
        return None
    return dict(row)


def p_blend(horizon_days: int, p_sim, conviction) -> float | None:
    """The blended P(target first), or None when there is no valid blend for this horizon or an input is unknown."""
    if p_sim is None or conviction is None:
        return None
    m = get_valid_blend(horizon_days)
    if m is None:
        return None
    return float(blend_probability((m["a"], m["b"], m["c"]), np.array([p_sim]), np.array([conviction]))[0])


# ── cap recommendation ────────────────────────────────────────────────────────

def sim_check_snapshot(df: pd.DataFrame | None = None, settings: dict | None = None) -> dict:
    """What the weekly report records for the cap recommendation: the disagree-vs-confident test per horizon."""
    if settings is None:
        from portfolio_agent.tools.engine_settings_db import get_sim_settings
        settings = get_sim_settings()
    df = load_plan_rows() if df is None else df
    per_h = {str(int(h)): disagreement_test(g, settings["sim_flag_min_conviction"], settings["sim_flag_max_p_target"])
             for h, g in df.groupby("horizon_days")} if not df.empty else {}
    return {"per_horizon": per_h, "significantly_worse": any(v["significantly_worse"] for v in per_h.values())}


def sim_cap_recommendation(lookback: int = 4, need: int = 2) -> dict:
    """Recommend sim_guardrail_mode = 'cap' when the weekly check found 'sim_disagrees' significantly worse than 'both confident'
    in `need` of the last `lookback` weekly reports. A recommendation only: a person flips the switch."""
    import json
    from portfolio_agent.tools.prediction_db import _db
    with _db() as c:
        rows = c.execute("SELECT report_date, patterns_identified FROM validation_reports WHERE period = 'weekly' "
                         "ORDER BY report_date DESC, id DESC LIMIT ?", [lookback]).fetchall()
    flags, latest = [], None
    for r in rows:
        try:
            chk = (json.loads(r["patterns_identified"] or "{}")).get("sim_check")
        except (TypeError, ValueError):
            chk = None
        if chk is None:
            continue
        flags.append(bool(chk.get("significantly_worse")))
        latest = latest or chk
    return {"recommend": sum(flags) >= need, "weeks_significant": sum(flags), "weeks_checked": len(flags), "latest": latest}
