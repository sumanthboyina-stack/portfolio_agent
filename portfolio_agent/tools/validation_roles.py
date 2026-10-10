"""
Validation by prediction_role — which model-selection path produced a row
(champion / fallback / challenger / legacy_random; see prediction_db.ROLE_*).

Pure functions over an evaluated-rows frame (web.data.validation.
_load_evaluated_predictions_df with roles=REVIEW_ROLES): no DB, no network, no
Streamlit. Every number comes from the existing yardsticks —
validation_metrics.compare_predictors and probability_baselines — run one
horizon at a time, so a role's figures are directly comparable to the
Baselines / Calibration tabs.

  role_breakdown          one row per (horizon, role): n, APEX accuracy, best
                          naive baseline, binary / 5-bucket Brier skill
  champion_vs_challenger  per horizon, the PRODUCTION row (champion or fallback)
                          and the CHALLENGER row for the SAME (ticker, as_of_date),
                          scored on exactly those paired rows, plus a head-to-head
                          direction tally
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from portfolio_agent.tools.prediction_db import (
    ROLE_CHALLENGER, ROLE_LABELS, ROLE_LEGACY_RANDOM, REVIEW_ROLES, VALIDATION_ROLES,
)
from portfolio_agent.tools.validation_metrics import compare_predictors, prepare_rows, probability_baselines

_PAIR_KEY = ["ticker", "as_of_date", "horizon_days"]


def _role_series(df: pd.DataFrame) -> pd.Series:
    if "prediction_role" not in df.columns:
        return pd.Series(ROLE_LEGACY_RANDOM, index=df.index)
    return df["prediction_role"].fillna(ROLE_LEGACY_RANDOM)


def _score(sub: pd.DataFrame, closes_by_symbol: Optional[dict]) -> dict:
    """APEX accuracy (all usable rows), best naive baseline, Brier skill — one horizon."""
    comp = compare_predictors(sub, closes_by_symbol)
    prob = probability_baselines(sub)
    apex = comp.get("apex_all_rows") or {}
    best = comp.get("best_baseline") or {}
    return {
        "n": comp["n_total"],
        "accuracy": apex.get("accuracy"),
        "balanced_accuracy": apex.get("balanced_accuracy"),
        "best_baseline": best.get("name"),
        "best_baseline_accuracy": best.get("accuracy"),
        "n_prob": prob["n"],
        "brier_skill_binary": (prob["binary"] or {}).get("brier_skill"),
        "brier_skill_five": (prob["five"] or {}).get("brier_skill"),
        "comparison": comp,
        "probability": prob,
    }


def role_breakdown(df: pd.DataFrame, closes_by_symbol: Optional[dict] = None) -> list[dict]:
    """[{horizon_days, role, label, n, accuracy, best_baseline, ..., brier_skill_*}] in REVIEW_ROLES order."""
    if df is None or df.empty or "horizon_days" not in df.columns:
        return []
    d = df.assign(_role=_role_series(df))
    order = {r: i for i, r in enumerate(REVIEW_ROLES)}
    out: list[dict] = []
    for (h, role), sub in d.dropna(subset=["horizon_days"]).groupby(["horizon_days", "_role"]):
        s = _score(sub.drop(columns="_role").reset_index(drop=True), closes_by_symbol)
        out.append({"horizon_days": int(h), "role": role, "label": ROLE_LABELS.get(role, role),
                    **{k: v for k, v in s.items() if k not in ("comparison", "probability")}})
    out.sort(key=lambda r: (r["horizon_days"], order.get(r["role"], len(order))))
    return out


def instrument_type_breakdown(df: pd.DataFrame, closes_by_symbol: Optional[dict] = None) -> list[dict]:
    """
    One row per (horizon, instrument_type) with the same yardsticks as role_breakdown — n, accuracy, best naive
    baseline, Brier skill — so an ETF's record is read against ITS baselines, not the stock record. `leveraged` counts
    the leveraged / inverse fund rows in the group. A missing type is "UNCLASSIFIED".
    """
    if df is None or df.empty or "horizon_days" not in df.columns:
        return []
    from portfolio_agent.tools.validation_engine import UNCLASSIFIED
    d = df.assign(_itype=(df["instrument_type"] if "instrument_type" in df.columns else pd.Series(None, index=df.index))
                  .fillna(UNCLASSIFIED))
    out: list[dict] = []
    for (h, itype), sub in d.dropna(subset=["horizon_days"]).groupby(["horizon_days", "_itype"]):
        sub = sub.drop(columns="_itype").reset_index(drop=True)
        s = _score(sub, closes_by_symbol)
        lev = sub["leverage_factor"].fillna(1.0).ne(1.0).sum() if "leverage_factor" in sub.columns else 0
        out.append({"horizon_days": int(h), "instrument_type": itype, "leveraged": int(lev),
                    **{k: v for k, v in s.items() if k not in ("comparison", "probability")}})
    out.sort(key=lambda r: (r["horizon_days"], -r["n"]))
    return out


def _latest_per_key(sub: pd.DataFrame) -> pd.DataFrame:
    """One row per (ticker, as_of_date, horizon) — the latest (highest id) — mirroring the
    evaluation-cohort rule recompute_rolling_metrics uses for same-day regenerations."""
    if "id" in sub.columns:
        sub = sub.sort_values("id")
    return sub.drop_duplicates(subset=_PAIR_KEY, keep="last")


def champion_vs_challenger(df: pd.DataFrame, closes_by_symbol: Optional[dict] = None) -> dict[int, dict]:
    """
    {horizon_days: {...}} comparing the production prediction (champion, or the
    fallback that stood in for it) with the challenger on the SAME tickers and
    dates. Only keys present on both sides count. Returns, per horizon:
        n_pairs, n_dates, challenger_models, production_models
        production / challenger   _score() on the paired rows
        head_to_head              {both_right, production_only, challenger_only, both_wrong}
                                  (direction calls, rows where both made a usable call)
    Horizons with no pairs are omitted.
    """
    if df is None or df.empty or not set(_PAIR_KEY) <= set(df.columns):
        return {}
    d = df.assign(_role=_role_series(df)).dropna(subset=["horizon_days"])
    prod = _latest_per_key(d[d["_role"].isin(VALIDATION_ROLES)])
    chall = _latest_per_key(d[d["_role"] == ROLE_CHALLENGER])
    if prod.empty or chall.empty:
        return {}
    keys = prod[_PAIR_KEY].merge(chall[_PAIR_KEY], on=_PAIR_KEY)
    out: dict[int, dict] = {}
    for h, k in keys.groupby("horizon_days"):
        p = prod.merge(k, on=_PAIR_KEY).drop(columns="_role").reset_index(drop=True)
        c = chall.merge(k, on=_PAIR_KEY).drop(columns="_role").reset_index(drop=True)

        pp, cp = prepare_rows(p), prepare_rows(c)
        both = pp[_PAIR_KEY + ["pred_dir", "actual_dir"]].merge(
            cp[_PAIR_KEY + ["pred_dir"]], on=_PAIR_KEY, suffixes=("_p", "_c"))
        p_ok = both["pred_dir_p"] == both["actual_dir"]
        c_ok = both["pred_dir_c"] == both["actual_dir"]

        out[int(h)] = {
            "n_pairs": len(k),
            "n_dates": int(k["as_of_date"].nunique()),
            "production_models": sorted(p["model_name"].dropna().unique().tolist()) if "model_name" in p.columns else [],
            "challenger_models": sorted(c["model_name"].dropna().unique().tolist()) if "model_name" in c.columns else [],
            "production": _score(p, closes_by_symbol),
            "challenger": _score(c, closes_by_symbol),
            "head_to_head": {
                "n": len(both),
                "both_right": int((p_ok & c_ok).sum()),
                "production_only": int((p_ok & ~c_ok).sum()),
                "challenger_only": int((~p_ok & c_ok).sum()),
                "both_wrong": int((~p_ok & ~c_ok).sum()),
            },
        }
    return out
