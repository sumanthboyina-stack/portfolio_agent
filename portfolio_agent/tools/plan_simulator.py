"""
Plan simulator — replay a stock's OWN past daily bars to ask how a trade plan (tools.trade_plan) would have fared.

This is NOT a forecast. It resamples what the stock actually did before the forecast date and reports how often a plan with this
stop and target would have hit the target first, the stop first, or neither by the time stop. Two views:

    simulate(...)           10,000 bootstrapped futures built from blocks of the stock's real days
    historical_replay(...)  the same rule applied on every past day it would have been valid, walked through the real bars

Pure: numpy / pandas in, plain dataclasses out. No DB, no network, no clock. The caller passes only bars dated on or before the
forecast date (prepare_bars cuts them), so nothing after the forecast can leak in.

Conventions (also shown in the UI):
  * Each past day becomes three ratios against the PREVIOUS close (high/prev, low/prev, close/prev). A sampled day keeps its own
    three together, so an intraday touch of the stop or target is as realistic as the history allows.
  * Blocks of `block_len` consecutive days are drawn at random (moving-block bootstrap) and joined to cover the horizon; that keeps
    volatility clustering that one-day draws would lose.
  * A day touches the stop if its low <= stop and the target if its high >= target. If BOTH happen on the same day it counts as the
    STOP: the order inside a day is unknown, and assuming the bad one is the conservative choice.
  * Exits fill exactly at the stop / target (no gap slippage). A path that touches neither is marked to its horizon close.
  * R is the risk per share: target = +reward_risk, stop = -1, timeout = (close - entry) / risk.
  * The zero-drift run subtracts the pool's mean daily log return before resampling: "the stock just wiggles, it has no edge".
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import numpy as np

SIM_VERSION = "plan-sim-v1"          # the simulation method itself is unchanged by Exit Planner v2 (only the plans it is given)
MIN_HISTORY_BARS = 250          # fewer bars than this and no simulation is run
ATR_WINDOW = 14


class InsufficientHistory(ValueError):
    pass


class NotSimulated(ValueError):
    """A plan this module deliberately does not simulate (a protective exit has no target to hit first)."""


@dataclass(frozen=True)
class SimResult:
    p_target_first: float
    p_stop_first: float
    p_timeout: float
    p_target_first_zero_drift: float
    expected_r: float
    p_close_above_entry: float
    end_return_p05: float          # horizon-close return vs entry across all paths, ignoring any exit
    end_return_p50: float
    end_return_p95: float
    paths: int
    block_len: int
    horizon_days: int
    pool_days: int
    seed: int


@dataclass(frozen=True)
class ReplayResult:
    n: int
    hits_target: int
    hits_stop: int
    timeouts: int
    avg_r: float | None

    def to_dict(self) -> dict:
        return {"n": self.n, "hits_target": self.hits_target, "hits_stop": self.hits_stop, "timeouts": self.timeouts,
                "avg_r": self.avg_r}


def sim_seed(ticker: str, as_of_date, horizon_days: int, rules_version: str) -> int:
    """A stable 32-bit seed: the same (ticker, date, horizon, rules) always give the same numbers, on any machine."""
    key = f"{str(ticker).upper()}|{str(as_of_date)[:10]}|{int(horizon_days)}|{rules_version}"
    return int.from_bytes(hashlib.sha256(key.encode()).digest()[:4], "big")


def prepare_bars(bars, as_of_date, lookback_days: int, min_bars: int = MIN_HISTORY_BARS):
    """
    The pool: bars dated on or before as_of_date, the most recent `lookback_days` of them, rows with a usable high/low/close only.
    Returns a DataFrame (High, Low, Close) or None if fewer than `min_bars` remain. This is the look-ahead cut.
    """
    import pandas as pd
    df = _normalized(bars)
    if df is None:
        return None
    df = df[df.index <= pd.Timestamp(str(as_of_date)[:10])].iloc[-int(lookback_days):]
    return df if len(df) >= min_bars else None


def _normalized(bars):
    """High / Low / Close as floats on a tz-naive date index, unusable rows dropped; None if there is nothing."""
    if bars is None or len(bars) == 0:
        return None
    idx = bars.index
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    df = bars[["High", "Low", "Close"]].copy()
    df.index = idx.normalize()
    df = df.astype(float).replace([np.inf, -np.inf], np.nan).dropna()
    return df[(df["Close"] > 0) & (df["High"] > 0) & (df["Low"] > 0)]


@dataclass(frozen=True)
class PlanOutcome:
    plan_outcome: str            # 'TARGET' | 'STOP' | 'TIMEOUT'
    realized_r: float
    exit_date: str


def walk_plan_outcome(bars, plan, as_of_date, time_stop_date, max_missing_days: int = 4) -> PlanOutcome | None:
    """
    What actually happened to a plan: walk the REAL daily bars from the day after as_of_date through time_stop_date (never past it)
    with the simulator's rules: low <= stop is a STOP, high >= target a TARGET, both on one day is a STOP, a path that touches
    neither TIMEOUTs at the time-stop close. R: target = the plan's reward:risk, stop = -1, timeout = (close - entry) / risk.
    None when it cannot be told yet: no bars in the window, or (for a plan that touched neither level) the last bar is more than
    `max_missing_days` before the time stop, i.e. the data has not caught up. A stop or target already touched is final even if later bars are missing.
    """
    import pandas as pd
    df = _normalized(bars)
    if df is None:
        return None
    lo_d, hi_d = pd.Timestamp(str(as_of_date)[:10]), pd.Timestamp(str(time_stop_date)[:10])
    w = df[(df.index > lo_d) & (df.index <= hi_d)]
    if len(w) == 0:
        return None
    stop, target, entry, risk = float(plan.stop), float(plan.target), float(plan.entry), float(plan.risk_per_share)
    hit_stop, hit_target = (w["Low"].values <= stop), (w["High"].values >= target)
    i_stop = int(hit_stop.argmax()) if hit_stop.any() else None
    i_target = int(hit_target.argmax()) if hit_target.any() else None
    if i_stop is not None and (i_target is None or i_stop <= i_target):       # same-day touch of both: the stop
        return PlanOutcome("STOP", -1.0, w.index[i_stop].date().isoformat())
    if i_target is not None:
        return PlanOutcome("TARGET", float(plan.reward_risk), w.index[i_target].date().isoformat())
    if w.index[-1] < hi_d - pd.Timedelta(days=max_missing_days):
        return None
    return PlanOutcome("TIMEOUT", (float(w["Close"].iloc[-1]) - entry) / risk, w.index[-1].date().isoformat())


def _ratios(bars) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    close = np.asarray(bars["Close"], dtype=float)
    prev = close[:-1]
    return (np.asarray(bars["High"], dtype=float)[1:] / prev,
            np.asarray(bars["Low"], dtype=float)[1:] / prev,
            close[1:] / prev)


def _walk(hr, lr, cr, entry, stop, target):
    """hr/lr/cr: (paths, horizon) ratios vs the previous close. Returns (outcome, end_ratio): outcome 1 target, -1 stop, 0 neither."""
    horizon = cr.shape[1]
    cum = np.cumprod(cr, axis=1)
    prev = np.concatenate([np.ones((cr.shape[0], 1)), cum[:, :-1]], axis=1)
    high = entry * prev * hr
    low = entry * prev * lr
    hit_stop, hit_target = low <= stop, high >= target
    first_stop = np.where(hit_stop.any(axis=1), hit_stop.argmax(axis=1), horizon)
    first_target = np.where(hit_target.any(axis=1), hit_target.argmax(axis=1), horizon)
    outcome = np.zeros(cr.shape[0], dtype=np.int8)
    outcome[(first_stop < horizon) & (first_stop <= first_target)] = -1       # same-day touch of both: the stop
    outcome[(first_target < horizon) & (first_target < first_stop)] = 1
    return outcome, cum[:, -1]


def simulate(bars, plan, paths: int, block_len: int, seed: int) -> SimResult:
    """
    bars: High / Low / Close dated on or before the forecast (see prepare_bars). plan: anything with entry, stop, target,
    risk_per_share, reward_risk and horizon_days (a TradePlan, or a stored plan row with its horizon).
    """
    if getattr(plan, "kind", None) == "protective" or getattr(plan, "target", None) is None:
        raise NotSimulated("protective exit plans are not simulated")
    horizon = int(plan.horizon_days)
    entry, stop, target = float(plan.entry), float(plan.stop), float(plan.target)
    risk, rr = float(plan.risk_per_share), float(plan.reward_risk)
    block_len, paths = int(block_len), int(paths)
    if len(bars) < 3 or block_len < 1 or paths < 1 or horizon < 1 or risk <= 0:
        raise InsufficientHistory("not enough bars, or an unusable plan, to simulate")
    hr, lr, cr = _ratios(bars)
    n = len(cr)
    block_len = min(block_len, n)
    rng = np.random.default_rng(int(seed))
    n_blocks = -(-horizon // block_len)
    starts = rng.integers(0, n - block_len + 1, size=(paths, n_blocks))
    idx = (starts[:, :, None] + np.arange(block_len)).reshape(paths, -1)[:, :horizon]

    outcome, end_ratio = _walk(hr[idx], lr[idx], cr[idx], entry, stop, target)

    drift = float(np.mean(np.log(cr)))
    shift = np.exp(-drift)                                    # remove the pool's average daily drift from every sampled day
    zero_out, _ = _walk(hr[idx] * shift, lr[idx] * shift, cr[idx] * shift, entry, stop, target)

    timeout = outcome == 0
    r_timeout = (entry * end_ratio - entry) / risk
    r = np.where(outcome == 1, rr, np.where(outcome == -1, -1.0, r_timeout))
    end_ret = end_ratio - 1.0
    p05, p50, p95 = np.percentile(end_ret, [5, 50, 95])
    return SimResult(
        p_target_first=float((outcome == 1).mean()), p_stop_first=float((outcome == -1).mean()),
        p_timeout=float(timeout.mean()), p_target_first_zero_drift=float((zero_out == 1).mean()),
        expected_r=float(r.mean()), p_close_above_entry=float((end_ratio > 1.0).mean()),
        end_return_p05=float(p05), end_return_p50=float(p50), end_return_p95=float(p95),
        paths=paths, block_len=block_len, horizon_days=horizon, pool_days=n, seed=int(seed),
    )


def sim_disagrees(raw_conviction, p_target_first, min_conviction: float, max_p_target: float) -> bool:
    """The red flag: the model is confident (raw conviction at or above the threshold) while the stock's own history says the target
    is rarely reached first (below the threshold). Unknown on either side is never a flag."""
    try:
        conv, p = float(raw_conviction), float(p_target_first)
    except (TypeError, ValueError):
        return False
    if conv != conv or p != p:
        return False
    return conv >= float(min_conviction) and p < float(max_p_target)


def rule_from_settings(settings: dict, horizon_days: int, *, stop_rule=None, stop_param=None, target_rule=None,
                       target_param=None) -> dict:
    """
    The plan rule for one horizon, in the shape historical_replay reads (same numbers tools.trade_plan.build_plan uses).
    With no rule arguments it is the v1 rule (ATR stop, R-multiple target) and replays on the original path. Pass any of
    stop_rule / stop_param / target_rule / target_param (a stored plan's own, see rule_for_plan) to replay a v2 rule: stop_rule in
    atr | pct | range_low | range_mid | price, target_rule in r_multiple | price. A `price` rule replays as FIXED prices.
    """
    h = int(horizon_days)
    rule = {"stop_atr_mult": settings["plan_stop_atr_mult"][h], "target_r_multiple": settings["plan_target_r_multiple"][h],
            "max_stop_pct": settings["plan_max_stop_pct"][h], "min_reward_risk": settings["plan_min_reward_risk"]}
    if stop_rule is None and target_rule is None and stop_param is None and target_param is None:
        return rule
    rule.update({
        "stop_rule": stop_rule or settings.get("plan_default_stop_rule", "atr"), "stop_param": stop_param,
        "target_rule": target_rule or settings.get("plan_default_target_rule", "r_multiple"), "target_param": target_param,
        "range_lookback": int((settings.get("plan_range_lookback_days") or {}).get(h, 5)),
        "min_stop_pct": settings.get("plan_min_stop_pct", 0.0005),
        "min_stop_atr_frac": (settings.get("plan_min_stop_atr_frac") or {}).get(h, 0.0),
    })
    return rule


def rule_for_plan(plan, settings: dict, horizon_days: int) -> dict:
    """The replay rule matching a STORED plan row: its own stop/target rule and parameter (a v1 row, or one with no rule recorded,
    replays on the v1 path)."""
    get = plan.get if isinstance(plan, dict) else (lambda k, d=None: getattr(plan, k, d))
    if get("rules_version") in (None, "trade-plan-v1"):
        return rule_from_settings(settings, horizon_days)
    return rule_from_settings(settings, horizon_days, stop_rule=get("stop_rule") or "atr", stop_param=get("stop_param"),
                              target_rule=get("target_rule") or "r_multiple", target_param=get("target_param"))


def _atr_series(high, low, close, window: int = ATR_WINDOW) -> np.ndarray:
    """ATR(window) at every bar: the simple mean of the last `window` true ranges, exactly as technical_features computes it
    (bar 0's true range is its own high - low). NaN until `window` bars exist."""
    prev = np.concatenate([[np.nan], close[:-1]])
    tr = np.fmax(high - low, np.fmax(np.abs(high - prev), np.abs(low - prev)))     # fmax skips the NaN at bar 0
    out = np.full(len(close), np.nan)
    if len(close) >= window:
        c = np.concatenate([[0.0], np.cumsum(tr)])
        out[window - 1:] = (c[window:] - c[:-window]) / window
    return out


def historical_replay(bars, rule: dict, horizon_days: int) -> ReplayResult:
    """
    The real track record of this plan rule on this stock: on every past day in the pool where the rule would have produced a valid
    plan (ATR known, stop inside the widest allowed, reward:risk at least the minimum), enter at that close, place the stop and
    target by the same ATR and R multiples, and walk the REAL next `horizon_days` bars. Days whose horizon runs past the end of the
    pool are skipped (their outcome is not known yet). Same conventions as simulate (same-day touch = stop). Windows overlap, so
    the n days are not independent trials.
    """
    h = int(horizon_days)
    high = np.asarray(bars["High"], dtype=float)
    low = np.asarray(bars["Low"], dtype=float)
    close = np.asarray(bars["Close"], dtype=float)
    n_bars = len(close)
    atr = _atr_series(high, low, close)
    if "stop_rule" in rule:
        return _historical_replay_v2(bars, high, low, close, atr, rule, h)
    risk = rule["stop_atr_mult"] * atr
    entry = close
    rr = rule["target_r_multiple"]
    with np.errstate(invalid="ignore", divide="ignore"):
        valid = (np.isfinite(atr) & (atr > 0) & (risk < entry) & (risk / entry <= rule["max_stop_pct"])
                 & (rr >= rule["min_reward_risk"] - 1e-9))
    days = np.flatnonzero(valid[: max(n_bars - h, 0)])        # needs h bars after it
    if len(days) == 0:
        return ReplayResult(0, 0, 0, 0, None)
    win = np.arange(1, h + 1)[None, :] + days[:, None]
    stop = (entry - risk)[days][:, None]
    target = (entry + rr * risk)[days][:, None]
    hit_stop, hit_target = low[win] <= stop, high[win] >= target
    first_stop = np.where(hit_stop.any(axis=1), hit_stop.argmax(axis=1), h)
    first_target = np.where(hit_target.any(axis=1), hit_target.argmax(axis=1), h)
    is_stop = (first_stop < h) & (first_stop <= first_target)
    is_target = (first_target < h) & (first_target < first_stop)
    is_timeout = ~(is_stop | is_target)
    r_timeout = (close[days + h] - entry[days]) / risk[days]
    r = np.where(is_target, rr, np.where(is_stop, -1.0, r_timeout))
    return ReplayResult(n=int(len(days)), hits_target=int(is_target.sum()), hits_stop=int(is_stop.sum()),
                        timeouts=int(is_timeout.sum()), avg_r=float(r.mean()))



def _half_up_cents(x):
    return np.floor(np.asarray(x, dtype=float) * 100.0 + 0.5) / 100.0


def _floor_cents(x):
    return np.floor(np.asarray(x, dtype=float) * 100.0 + 1e-9) / 100.0


def _levels_v2(high, low, close, atr, rule: dict):
    """Per-bar entry / stop / target / risk / valid for the v2 rule. Every value at bar i is a function of bars 0..i only (rolling
    windows end at i), which is what keeps the replay free of look-ahead (tested by recomputing on prefixes)."""
    import pandas as pd
    n_bars = len(close)
    entry = _half_up_cents(close)
    srule, trule = rule["stop_rule"], rule["target_rule"]
    nan = np.full(n_bars, np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        if srule == "atr":
            k = rule["stop_param"] if rule.get("stop_param") is not None else rule["stop_atr_mult"]
            raw_stop = close - k * atr
        elif srule == "pct":
            raw_stop = close * (1.0 - float(rule["stop_param"])) if rule.get("stop_param") is not None else nan
        elif srule in ("range_low", "range_mid"):
            n = int(rule["stop_param"]) if rule.get("stop_param") is not None else int(rule["range_lookback"])
            rh = pd.Series(high).rolling(n).max().to_numpy()
            rl = pd.Series(low).rolling(n).min().to_numpy()
            raw_stop = rl if srule == "range_low" else (rh + rl) / 2.0
        else:                                                        # "price": a fixed stop
            raw_stop = np.full(n_bars, float(rule["stop_param"])) if rule.get("stop_param") is not None else nan
        stop = _floor_cents(raw_stop)
        risk = entry - stop
        if trule == "price":
            target = _half_up_cents(np.full(n_bars, float(rule["target_param"]))) if rule.get("target_param") is not None else nan
            tol = 0.0
        else:
            r = rule["target_param"] if rule.get("target_param") is not None else rule["target_r_multiple"]
            target, tol = _half_up_cents(entry + r * risk), 0.005
        floor = np.maximum(entry * float(rule["min_stop_pct"]), float(rule["min_stop_atr_frac"]) * np.nan_to_num(atr))
        valid = (np.isfinite(stop) & np.isfinite(target) & (stop < entry) & (stop > 0) & (risk / entry <= rule["max_stop_pct"])
                 & (risk >= floor - 1e-9) & (target > entry)
                 & ((target - entry + tol) / risk >= rule["min_reward_risk"] - 1e-9))
        if srule == "atr":
            valid &= np.isfinite(atr) & (atr > 0)
    return entry, stop, target, risk, valid


def _historical_replay_v2(bars, high, low, close, atr, rule: dict, h: int) -> ReplayResult:
    """
    The v2 rule on every past day: entry = that day's close (cents, half up); stop by stop_rule, floored to the cent; target by
    target_rule (cents, half up); a day counts only if build_plan would have produced a plan (stop below entry and above zero, not
    wider than max_stop_pct, not inside the too-tight floors, target above entry, reward:risk at least the minimum). The range rules
    use the N bars ENDING at the entry day (bars <= that day): no later bar can reach them. `price` rules are fixed dollars.
    Same walk and same-day-touch = stop convention as the v1 replay.
    """
    n_bars = len(close)
    entry, stop, target, risk, valid = _levels_v2(high, low, close, atr, rule)
    days = np.flatnonzero(valid[: max(n_bars - h, 0)])
    if len(days) == 0:
        return ReplayResult(0, 0, 0, 0, None)
    win = np.arange(1, h + 1)[None, :] + days[:, None]
    st, tg, en, rk = stop[days][:, None], target[days][:, None], entry[days], risk[days]
    hit_stop, hit_target = low[win] <= st, high[win] >= tg
    first_stop = np.where(hit_stop.any(axis=1), hit_stop.argmax(axis=1), h)
    first_target = np.where(hit_target.any(axis=1), hit_target.argmax(axis=1), h)
    is_stop = (first_stop < h) & (first_stop <= first_target)
    is_target = (first_target < h) & (first_target < first_stop)
    is_timeout = ~(is_stop | is_target)
    rr = (target[days] - en) / rk
    r_timeout = (close[days + h] - en) / rk
    r = np.where(is_target, rr, np.where(is_stop, -1.0, r_timeout))
    return ReplayResult(n=int(len(days)), hits_target=int(is_target.sum()), hits_stop=int(is_stop.sum()),
                        timeouts=int(is_timeout.sum()), avg_r=float(r.mean()))
