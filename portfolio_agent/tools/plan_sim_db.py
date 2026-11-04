"""
Plan simulation persistence + the data lookups around tools.plan_simulator (which is pure).

    create_simulation_for_prediction(prediction_id)   one simulation per PLANNED shared forecast; a re-run is a no-op
    get_sim(id) / get_sims(ids) / get_flagged_ids(ids)
    backfill_sims(since)                              simulations for stored plans that have none

Look-ahead: the pool is cut at the forecast's as_of_date (plan_simulator.prepare_bars) before anything is computed.
The 'sim_disagrees' flag is appended to the prediction's guardrail_flags; in 'cap' mode only, conviction_score and composite_score are
also capped to 5 (the way the existing guardrails cap them). Table: plan_simulations (created by prediction_db._migrate).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Callable

from portfolio_agent.log import get_logger
from portfolio_agent.tools import plan_simulator as ps
from portfolio_agent.tools.prediction_db import SCOPE_SHARED, _db

_log = get_logger("plan_sim")

FLAG = "sim_disagrees"
CAP_VALUE = 5.0
_BARS_TTL_SECONDS = 3600
_bars_cache: dict[str, tuple[float, object]] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _default_fetch_bars(ticker: str):
    """Daily OHLC for the ticker (about five years), cached per process: the horizons of one ticker, and a backfill across many
    days, share one download. Callers slice it at their own as_of_date, so a cached future bar can never reach a past forecast."""
    hit = _bars_cache.get(ticker)
    if hit and time.monotonic() - hit[0] < _BARS_TTL_SECONDS:
        return hit[1]
    import yfinance as yf
    hist = yf.Ticker(ticker).history(period="5y", interval="1d", auto_adjust=True)
    _bars_cache[ticker] = (time.monotonic(), hist)
    return hist


def get_sim(prediction_id: int) -> dict | None:
    with _db() as c:
        row = c.execute("SELECT * FROM plan_simulations WHERE prediction_id = ?", [int(prediction_id)]).fetchone()
    return dict(row) if row else None


def get_sims(prediction_ids) -> dict[int, dict]:
    ids = [int(i) for i in prediction_ids if i is not None]
    if not ids:
        return {}
    with _db() as c:
        rows = c.execute(f"SELECT * FROM plan_simulations WHERE prediction_id IN ({','.join('?' * len(ids))})", ids).fetchall()
    return {r["prediction_id"]: dict(r) for r in rows}


def get_flagged_ids(prediction_ids) -> set[int]:
    """Which of these predictions carry the sim_disagrees flag."""
    ids = [int(i) for i in prediction_ids if i is not None]
    if not ids:
        return set()
    with _db() as c:
        rows = c.execute(f"SELECT id, guardrail_flags FROM predictions WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
    return {r["id"] for r in rows if FLAG in _flags_list(r["guardrail_flags"])}


def _flags_list(raw) -> list:
    try:
        v = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    return v if isinstance(v, list) else []


def _apply_flag(prediction_id: int, p_target_first: float, sim_settings: dict, *, apply_cap: bool) -> dict:
    """Append 'sim_disagrees' when the rule fires; in cap mode (and only when apply_cap) cap conviction and composite to 5.
    Returns {flagged, capped}. The model's own conviction is preserved: raw_conviction_score keeps its value, and where it was
    empty (no earlier guardrail fired) it is stamped with the pre-cap conviction, never with a different number."""
    with _db() as c:
        row = c.execute("SELECT conviction_score, raw_conviction_score, composite_score, guardrail_flags FROM predictions WHERE id = ?",
                        [int(prediction_id)]).fetchone()
        if row is None:
            return {"flagged": False, "capped": False}
        raw = row["raw_conviction_score"] if row["raw_conviction_score"] is not None else row["conviction_score"]
        if not ps.sim_disagrees(raw, p_target_first, sim_settings["sim_flag_min_conviction"], sim_settings["sim_flag_max_p_target"]):
            return {"flagged": False, "capped": False}
        flags = _flags_list(row["guardrail_flags"])
        if FLAG not in flags:
            flags.append(FLAG)
        capped = False
        if apply_cap and sim_settings["sim_guardrail_mode"] == "cap":
            conv, comp = row["conviction_score"], row["composite_score"]
            new_conv = min(float(conv), CAP_VALUE) if conv is not None else None
            new_comp = min(float(comp), CAP_VALUE) if comp is not None else None
            capped = (new_conv, new_comp) != (conv, comp)
            c.execute("UPDATE predictions SET conviction_score = ?, composite_score = ?, "
                      "raw_conviction_score = COALESCE(raw_conviction_score, ?), guardrail_flags = ? WHERE id = ?",
                      [new_conv, new_comp, conv, json.dumps(flags), int(prediction_id)])
        else:
            c.execute("UPDATE predictions SET guardrail_flags = ? WHERE id = ?", [json.dumps(flags), int(prediction_id)])
        c.commit()
    return {"flagged": True, "capped": capped}


def create_simulation_for_prediction(prediction_id: int, *, settings: dict | None = None, plan_settings: dict | None = None,
                                     fetch_bars: Callable | None = None, apply_cap: bool = True) -> dict | None:
    """
    Simulate one planned shared forecast and return the stored row. None if the prediction has no PLANNED trade plan (nothing to
    simulate). An existing simulation is returned untouched. apply_cap=False (the backfill) flags but never caps: old rows are
    not rewritten.
    """
    existing = get_sim(prediction_id)
    if existing:
        return existing
    with _db() as c:
        row = c.execute(
            """SELECT p.ticker, p.as_of_date, p.horizon_days, p.scope, t.plan_status, t.entry, t.stop, t.target, t.risk_per_share,
                      t.reward_risk, t.rules_version
               FROM predictions p JOIN trade_plans t ON t.prediction_id = p.id WHERE p.id = ?""", [int(prediction_id)]).fetchone()
    if row is None or row["scope"] != SCOPE_SHARED or row["plan_status"] != "planned":
        return None
    if settings is None or plan_settings is None:
        from portfolio_agent.tools import engine_settings_db as esdb
        es = esdb.get_engine_settings()
        settings, plan_settings = settings or esdb.get_sim_settings(es), plan_settings or esdb.get_trade_plan_settings(es)

    h, as_of = int(row["horizon_days"]), row["as_of_date"]
    plan = type("P", (), dict(entry=row["entry"], stop=row["stop"], target=row["target"], risk_per_share=row["risk_per_share"],
                              reward_risk=row["reward_risk"], horizon_days=h))()
    seed = ps.sim_seed(row["ticker"], as_of, h, row["rules_version"])
    base = dict(paths=settings["sim_paths"], block_len=settings["sim_block_len"], lookback_days=settings["sim_lookback_days"], seed=seed)
    vals: dict
    try:
        bars = (fetch_bars or _default_fetch_bars)(row["ticker"])
        pool = ps.prepare_bars(bars, as_of, settings["sim_lookback_days"])
        if pool is None:
            vals = {**base, "sim_status": "insufficient_history"}
        else:
            r = ps.simulate(pool, plan, settings["sim_paths"], settings["sim_block_len"], seed)
            rep = ps.historical_replay(pool, ps.rule_from_settings(plan_settings, h), h)
            vals = {**base, "sim_status": "ok", "p_target_first": r.p_target_first, "p_stop_first": r.p_stop_first,
                    "p_timeout": r.p_timeout, "p_target_first_zero_drift": r.p_target_first_zero_drift,
                    "expected_r": r.expected_r, "p_close_above_entry": r.p_close_above_entry, "end_ret_p05": r.end_return_p05,
                    "end_ret_p50": r.end_return_p50, "end_ret_p95": r.end_return_p95, "hist_n": rep.n,
                    "hist_target": rep.hits_target, "hist_stop": rep.hits_stop, "hist_timeout": rep.timeouts,
                    "hist_avg_r": rep.avg_r}
    except Exception as exc:                      # stored as 'error' so it can be retried; never raised into the caller
        _log.warning(f"  [sim] {row['ticker']} {h}d simulation failed: {exc}", event_type="warning", ticker=row["ticker"])
        vals = {**base, "sim_status": "error"}
    cols = ["prediction_id", *vals, "sim_version", "created_at"]
    with _db() as c:
        cur = c.execute(f"INSERT OR IGNORE INTO plan_simulations ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                        [int(prediction_id), *vals.values(), ps.SIM_VERSION, _now()])
        c.commit()
        inserted = cur.rowcount == 1
    if inserted and vals["sim_status"] == "ok":
        _apply_flag(prediction_id, vals["p_target_first"], settings, apply_cap=apply_cap)
    return get_sim(prediction_id)


def backfill_sims(since: str, *, settings: dict | None = None, plan_settings: dict | None = None,
                  fetch_bars: Callable | None = None) -> dict:
    """Simulations for every planned shared forecast with as_of_date >= since that has none (rows that previously ended in 'error'
    are retried). Flags are written, conviction is never capped. Each uses only bars on or before its own as_of_date."""
    from datetime import date
    date.fromisoformat(since)
    with _db() as c:
        ids = [r["id"] for r in c.execute(
            """SELECT p.id FROM predictions p JOIN trade_plans t ON t.prediction_id = p.id
               LEFT JOIN plan_simulations s ON s.prediction_id = p.id
               WHERE p.scope = ? AND p.as_of_date >= ? AND t.plan_status = 'planned'
                     AND (s.prediction_id IS NULL OR s.sim_status = 'error')
               ORDER BY p.ticker, p.as_of_date, p.id""", [SCOPE_SHARED, since]).fetchall()]
        c.execute("DELETE FROM plan_simulations WHERE sim_status = 'error' AND prediction_id IN (SELECT id FROM predictions WHERE as_of_date >= ?)",
                  [since])
        c.commit()
    out = {"candidates": len(ids), "ok": 0, "insufficient_history": 0, "error": 0, "flagged": 0}
    t0 = time.perf_counter()
    for pid in ids:
        row = create_simulation_for_prediction(pid, settings=settings, plan_settings=plan_settings, fetch_bars=fetch_bars, apply_cap=False)
        status = row["sim_status"] if row else "error"
        out[status] += 1
    out["flagged"] = len(get_flagged_ids(ids))
    out["seconds"] = round(time.perf_counter() - t0, 1)
    _log.info(f"  [sim] backfill: {out}", event_type="summary")
    return out


def get_raw_convictions(prediction_ids) -> dict[int, float]:
    """The model's own conviction per prediction: raw_conviction_score where a guardrail recorded one, else conviction_score."""
    ids = [int(i) for i in prediction_ids if i is not None]
    if not ids:
        return {}
    with _db() as c:
        rows = c.execute(f"SELECT id, COALESCE(raw_conviction_score, conviction_score) AS conv FROM predictions "
                         f"WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
    return {r["id"]: r["conv"] for r in rows if r["conv"] is not None}


def evaluate_matured_plans(today: str, *, fetch_bars: Callable | None = None) -> dict:
    """
    Record what actually happened to every planned shared forecast whose time stop is on or before `today` and has no outcome yet:
    the real bars after as_of_date, up to the time stop, walked by plan_simulator.walk_plan_outcome. A parallel scorecard: the
    direction / bucket scoring is not touched. Rows whose bars have not caught up are left for the next run.
    """
    with _db() as c:
        rows = [dict(r) for r in c.execute(
            """SELECT p.id, p.ticker, p.as_of_date, t.entry, t.stop, t.target, t.risk_per_share, t.reward_risk, t.time_stop_date
               FROM predictions p JOIN trade_plans t ON t.prediction_id = p.id
               LEFT JOIN plan_outcomes o ON o.prediction_id = p.id
               WHERE p.scope = ? AND t.plan_status = 'planned' AND t.time_stop_date <= ? AND o.prediction_id IS NULL
               ORDER BY p.ticker, p.as_of_date""", [SCOPE_SHARED, today]).fetchall()]
    out = {"candidates": len(rows), "evaluated": 0, "pending": 0, "errors": 0}
    bars_by_ticker: dict[str, object] = {}
    for r in rows:
        try:
            if r["ticker"] not in bars_by_ticker:
                bars_by_ticker[r["ticker"]] = (fetch_bars or _default_fetch_bars)(r["ticker"])
            res = ps.walk_plan_outcome(bars_by_ticker[r["ticker"]], type("P", (), r)(), r["as_of_date"], r["time_stop_date"])
            if res is None:
                out["pending"] += 1
                continue
            with _db() as c:
                c.execute("INSERT OR IGNORE INTO plan_outcomes (prediction_id, plan_outcome, realized_r, exit_date, evaluated_at) "
                          "VALUES (?, ?, ?, ?, ?)", [r["id"], res.plan_outcome, res.realized_r, res.exit_date, _now()])
                c.commit()
            out["evaluated"] += 1
        except Exception as exc:
            out["errors"] += 1
            _log.warning(f"  [plan-outcome] {r['ticker']} (prediction {r['id']}) not evaluated: {exc}", event_type="warning",
                         ticker=r["ticker"])
    _log.info(f"  [plan-outcome] {out}", event_type="summary")
    return out
