"""
Trade plan — a deterministic entry / stop / target / time stop for a shared UP forecast.

The LLM never sets these numbers. The prediction supplies the entry (its as-of close) and the horizon; the stop is
an ATR multiple, the target an R-multiple of the stop distance, and the time stop is the horizon in trading days.
All of it is a pure function of (entry, ATR as of the prediction date, horizon, settings), so a plan can be
recomputed, backfilled and tested. `llm_target` (the model's own upper return) is recorded for comparison only.

Pure: no DB, no network. The persistence + ATR lookup live in tools.trade_plan_db; the settings in engine_settings_db.

Rules versions
    trade-plan-v1   ATR stop, R-multiple target, 4-decimal prices, UP only. Kept verbatim (_build_plan_v1): stored v1 rows and the
                    v1 tests describe it, and build_plan() still defaults to it for callers that do not ask for v2.
    trade-plan-v2   Exit Planner: stop rules (atr | pct | range_low | range_mid | price), target rules (r_multiple | price), a
                    "too tight" refusal, explicit reasons for DOWN / FLAT, cent rounding (stop floored away from entry, target and
                    entry half-up), and a private protective exit for a DOWN forecast on a held position (build_protective_plan).
                    Shared (stored) plans use v2 with the default rules only; a user-supplied price is owner-scoped, never shared.

Settings shape (engine_settings_db.get_trade_plan_settings):
    plan_stop_atr_mult                {horizon: multiple of ATR14 below entry}
    plan_target_r_multiple            {horizon: reward as a multiple of the risk per share}
    plan_min_reward_risk              no plan if reward:risk is below this
    plan_max_stop_pct                 {horizon: widest allowed stop, fraction of entry}; wider = 'too_volatile'
    plan_max_loss_pct_by_risk_profile {profile: fraction of total portfolio value lost if the stop hits}
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal

RULES_VERSION_V1 = "trade-plan-v1"
RULES_VERSION = "trade-plan-v2"          # what new shared plans are written with

SUPPORTED_HORIZONS = (5, 21, 63)

SEED_STOP_ATR_MULT = {5: 1.5, 21: 2.5, 63: 4.0}
SEED_TARGET_R_MULTIPLE = {5: 1.5, 21: 2.0, 63: 2.5}
SEED_MIN_REWARD_RISK = 1.5
SEED_MAX_STOP_PCT = {5: 0.08, 21: 0.15, 63: 0.25}
SEED_MAX_LOSS_PCT = {"conservative": 0.005, "moderate": 0.01, "aggressive": 0.015}
# v2
SEED_MIN_STOP_PCT = 0.0005                       # spread / tick-noise floor: 0.05% of entry
SEED_MIN_STOP_ATR_FRAC = {5: 0.5, 21: 0.75, 63: 1.0}   # a stop inside this fraction of a day's ATR is ordinary noise
SEED_RANGE_LOOKBACK_DAYS = {5: 5, 21: 10, 63: 20}
SEED_DEFAULT_STOP_RULE = "atr"
SEED_DEFAULT_TARGET_RULE = "r_multiple"
STOP_RULES = ("atr", "pct", "range_low", "range_mid", "price")
TARGET_RULES = ("r_multiple", "price")
RANGE_LOOKBACK_BOUNDS = (2, 60)
_HALF_CENT = Decimal("0.005")
_CENT = Decimal("0.01")


def seed_settings() -> dict:
    return {
        "plan_stop_atr_mult": dict(SEED_STOP_ATR_MULT),
        "plan_target_r_multiple": dict(SEED_TARGET_R_MULTIPLE),
        "plan_min_reward_risk": SEED_MIN_REWARD_RISK,
        "plan_max_stop_pct": dict(SEED_MAX_STOP_PCT),
        "plan_max_loss_pct_by_risk_profile": dict(SEED_MAX_LOSS_PCT),
    }


NO_PLAN_REASONS = {
    "not_up": "The forecast is not UP, so there is no long trade plan.",
    "unsupported_horizon": "Trade plans cover the 5, 21 and 63 day horizons only.",
    "missing_atr": "No volatility (ATR) reading was available as of the forecast date.",
    "missing_price": "No entry price was recorded with the forecast.",
    "too_volatile": "The ATR stop is wider than the maximum allowed for this horizon.",
    "below_min_rr": "Reward:risk is below the minimum.",
    # v2
    "down": "A DOWN forecast has no shared trade plan: this app never plans a short. If you hold the stock, a private protective exit applies.",
    "flat": "The forecast is FLAT: no directional trade plan.",
    "no_direction": "The forecast has no direction, so there is no trade plan.",
    "not_down": "A protective exit plan applies only to a DOWN forecast.",
    "stop_not_below_entry": "The stop is not below the entry price.",
    "stop_at_or_below_zero": "The stop would be at or below zero.",
    "too_tight": "The stop is too close to the entry: ordinary daily noise or the bid/ask spread would trigger it.",
    "target_not_above_entry": "The target is not above the entry price.",
    "missing_range": "No recent daily price range was available as of the forecast date.",
    "invalid_rule": "That stop or target rule is not supported.",
    "invalid_rule_param": "The stop or target value is missing or out of range.",
}


@dataclass(frozen=True)
class TradePlan:
    entry: float
    stop: float
    target: float
    risk_per_share: float
    reward_risk: float
    time_stop_date: str
    atr_14: float
    horizon_days: int
    llm_target: float | None      # entry * (1 + predicted_return_high): shown next to the plan, never drives it
    stop_rule: str = "atr"
    stop_param: float | None = None       # the value actually used: ATR multiple, fraction, lookback days or price
    target_rule: str = "r_multiple"
    target_param: float | None = None
    rules_version: str = RULES_VERSION_V1

    def to_dict(self) -> dict:
        return {"plan_status": "planned", "entry": self.entry, "stop": self.stop, "target": self.target,
                "risk_per_share": self.risk_per_share, "reward_risk": self.reward_risk,
                "time_stop_date": self.time_stop_date, "atr_14": self.atr_14, "horizon_days": self.horizon_days,
                "llm_target": self.llm_target, "stop_rule": self.stop_rule, "stop_param": self.stop_param,
                "target_rule": self.target_rule, "target_param": self.target_param, "rules_version": self.rules_version}


@dataclass(frozen=True)
class ProtectivePlan:
    """A protective exit for a position the owner HOLDS when the forecast is DOWN: where to sell to cap the damage if the call is
    right. Not a short, no target; trim_level is the model's own number, display only. Built privately, never stored as shared."""
    entry: float                  # the forecast's start price (reference)
    stop: float
    trim_level: float | None      # entry * (1 + predicted_return_high) when that is above entry, else None
    risk_per_share: float
    time_stop_date: str
    atr_14: float | None
    horizon_days: int
    stop_rule: str = "atr"
    stop_param: float | None = None
    rules_version: str = RULES_VERSION

    def to_dict(self) -> dict:
        return {"plan_status": "planned", "kind": "protective", "entry": self.entry, "stop": self.stop, "target": None,
                "trim_level": self.trim_level, "risk_per_share": self.risk_per_share, "reward_risk": None,
                "time_stop_date": self.time_stop_date, "atr_14": self.atr_14, "horizon_days": self.horizon_days,
                "stop_rule": self.stop_rule, "stop_param": self.stop_param, "rules_version": self.rules_version}


@dataclass(frozen=True)
class NoPlan:
    reason: str
    atr_14: float | None = None
    llm_target: float | None = None
    detail: str | None = None     # a specific, human sentence for this case (e.g. the too_tight dollars); not stored

    @property
    def message(self) -> str:
        return self.detail or NO_PLAN_REASONS.get(self.reason, self.reason)

    def to_dict(self) -> dict:
        return {"plan_status": "no_plan", "no_plan_reason": self.reason, "message": self.message,
                "atr_14": self.atr_14, "llm_target": self.llm_target}


@dataclass(frozen=True)
class Sizing:
    shares: int
    position_value: float
    loss_if_stopped: float
    max_loss_usd: float
    binding_cap: str              # 'loss' | 'position' | 'none' (nothing can be bought)

    def to_dict(self) -> dict:
        return {"shares": self.shares, "position_value": self.position_value, "loss_if_stopped": self.loss_if_stopped,
                "max_loss_usd": self.max_loss_usd, "binding_cap": self.binding_cap}


def _num(v) -> float | None:
    """A finite float, or None for None / NaN / inf / non-numeric."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _time_stop(as_of: date | str, horizon_days: int) -> str:
    from portfolio_agent.tools.prediction_db import trading_day_evaluation_date
    d = as_of if isinstance(as_of, date) else date.fromisoformat(str(as_of)[:10])
    return trading_day_evaluation_date(d, horizon_days)


def _build_plan_v1(entry, atr_14, horizon_days, direction, settings, *, as_of_date=None,
                   predicted_return_high=None) -> TradePlan | NoPlan:
    """
    trade-plan-v1, unchanged. entry = the as-of close; risk/share = stop_atr_mult[h] * atr_14; stop = entry - risk; target = entry + R * risk.
    A NoPlan carries the first reason that applies, checked in this order: not_up, unsupported_horizon, missing_price,
    missing_atr, too_volatile, below_min_rr.
    """
    entry_f, atr = _num(entry), _num(atr_14)
    ret_high = _num(predicted_return_high)
    llm_target = round(entry_f * (1 + ret_high), 4) if (entry_f and entry_f > 0 and ret_high is not None) else None

    if str(direction or "").upper() != "UP":
        return NoPlan("not_up", atr, llm_target)
    try:
        h = int(horizon_days)
    except (TypeError, ValueError):
        return NoPlan("unsupported_horizon", atr, llm_target)
    mult = settings["plan_stop_atr_mult"]
    r_mult = settings["plan_target_r_multiple"]
    if h not in SUPPORTED_HORIZONS or h not in mult or h not in r_mult:
        return NoPlan("unsupported_horizon", atr, llm_target)
    if entry_f is None or entry_f <= 0:
        return NoPlan("missing_price", atr, llm_target)
    if atr is None or atr <= 0:
        return NoPlan("missing_atr", None, llm_target)

    risk = mult[h] * atr
    if risk <= 0 or risk >= entry_f:                      # a stop at or below zero is not a stop
        return NoPlan("too_volatile", atr, llm_target)
    max_stop = settings["plan_max_stop_pct"].get(h)
    if max_stop is not None and risk / entry_f > max_stop:
        return NoPlan("too_volatile", atr, llm_target)

    stop = entry_f - risk
    target = entry_f + r_mult[h] * risk
    rr = (target - entry_f) / (entry_f - stop)
    if rr < settings["plan_min_reward_risk"] - 1e-9:
        return NoPlan("below_min_rr", atr, llm_target)
    if as_of_date is None:
        raise ValueError("build_plan: as_of_date is required to place the time stop")
    return TradePlan(
        entry=round(entry_f, 4), stop=round(stop, 4), target=round(target, 4), risk_per_share=round(risk, 4),
        reward_risk=round(rr, 4), time_stop_date=_time_stop(as_of_date, h), atr_14=round(atr, 4),
        horizon_days=h, llm_target=llm_target,
    )


def min_risk_floor(entry, atr, horizon_days, settings, spread=None) -> tuple[float, str]:
    """
    The smallest stop distance (dollars) that can survive normal noise, and which floor set it ('pct' | 'atr' | 'spread').
    Floors: plan_min_stop_pct of entry (spread / tick noise), plan_min_stop_atr_frac[h] of a day's ATR, and — once the app has
    quotes — the bid/ask `spread` itself (None today). The binding floor is the largest.
    """
    entry_f, atr_f = _num(entry) or 0.0, _num(atr)
    h = int(horizon_days)
    pct = entry_f * float(settings.get("plan_min_stop_pct", SEED_MIN_STOP_PCT))
    frac = settings.get("plan_min_stop_atr_frac", SEED_MIN_STOP_ATR_FRAC).get(h, 0.0)
    atr_floor = float(frac) * atr_f if atr_f else 0.0
    spread_f = _num(spread) or 0.0
    floor, binding = pct, "pct"
    if atr_floor > floor:
        floor, binding = atr_floor, "atr"
    if spread_f > floor:
        floor, binding = spread_f, "spread"
    return floor, binding


def _too_tight_message(risk, entry, floor, binding, h, settings) -> str:
    if binding == "atr":
        frac = float(settings.get("plan_min_stop_atr_frac", SEED_MIN_STOP_ATR_FRAC).get(h, 0.0))
        words = "half a day's" if abs(frac - 0.5) < 1e-9 else "a full day's" if abs(frac - 1.0) < 1e-9 else f"{frac:g} of a day's"
        return f"The stop is ${risk:.2f} under the entry, inside {words} typical move (${floor:.2f}). Widen it."
    if binding == "spread":
        return f"The stop is ${risk:.2f} under the entry, inside the bid/ask spread (${floor:.2f}). Widen it."
    pct = float(settings.get("plan_min_stop_pct", SEED_MIN_STOP_PCT))
    return (f"The stop is ${risk:.2f} under the ${entry:.2f} entry, tighter than the {pct:.2%} minimum (${floor:.2f}) "
            f"needed to clear spread and tick noise. Widen it.")


def _d(x) -> Decimal:
    return Decimal(str(x))


def _fnum(x, positive: bool = True) -> float | None:
    v = _num(x)
    return v if v is not None and (v > 0 or not positive) else None


def _direction_reason(direction) -> str | None:
    """None for UP; else why a shared long plan does not exist."""
    d = str(direction or "").upper()
    return None if d == "UP" else "down" if d == "DOWN" else "flat" if d == "FLAT" else "no_direction"


def _resolve_stop(entry_f, atr, h, settings, stop_rule, stop_param, range_hl, llm_target):
    """
    Shared by the long and the protective builder. Returns (entry, stop, risk, rule, param) as Decimals/str/float, or a NoPlan.
    Checks, in order: rule inputs, stop_not_below_entry, stop_at_or_below_zero, too_volatile, too_tight.
    """
    rule = stop_rule or settings.get("plan_default_stop_rule", SEED_DEFAULT_STOP_RULE)
    if rule not in STOP_RULES:
        return NoPlan("invalid_rule", atr, llm_target)
    raw_entry = _d(entry_f)                  # the stop is rounded away from the TRUE price, not from the rounded entry
    entry = raw_entry.quantize(_CENT, ROUND_HALF_UP)
    param = None
    if rule == "atr":
        if atr is None or atr <= 0:
            return NoPlan("missing_atr", None, llm_target)
        k = _fnum(stop_param) if stop_param is not None else _fnum(settings["plan_stop_atr_mult"].get(h))
        if k is None:
            return NoPlan("invalid_rule_param", atr, llm_target)
        raw, param = raw_entry - _d(k) * _d(atr), k
    elif rule == "pct":
        p = _num(stop_param)
        if p is None or not (0 < p < 1):
            return NoPlan("invalid_rule_param", atr, llm_target)
        raw, param = raw_entry * (1 - _d(p)), p
    elif rule in ("range_low", "range_mid"):
        n = stop_param if stop_param is not None else settings.get("plan_range_lookback_days", SEED_RANGE_LOOKBACK_DAYS).get(h)
        lo_b, hi_b = RANGE_LOOKBACK_BOUNDS
        if _num(n) is None or float(n) != int(n) or not (lo_b <= int(n) <= hi_b):
            return NoPlan("invalid_rule_param", atr, llm_target)
        param = int(n)
        hi, lo = (_fnum(range_hl[0]), _fnum(range_hl[1])) if range_hl else (None, None)
        if hi is None or lo is None or hi < lo:
            return NoPlan("missing_range", atr, llm_target)
        raw = _d(lo) if rule == "range_low" else (_d(hi) + _d(lo)) / 2
    else:                                                        # "price": the user's own stop
        px = _fnum(stop_param)
        if px is None:
            return NoPlan("invalid_rule_param", atr, llm_target)
        raw, param = _d(px), px
    stop = raw.quantize(_CENT, ROUND_FLOOR)                      # always away from entry for a long
    if stop >= entry:
        return NoPlan("stop_not_below_entry", atr, llm_target)
    if stop <= 0:
        return NoPlan("stop_at_or_below_zero", atr, llm_target)
    risk = entry - stop
    max_stop = settings["plan_max_stop_pct"].get(h)
    if max_stop is not None and float(risk / entry) > max_stop:
        return NoPlan("too_volatile", atr, llm_target)
    floor, binding = min_risk_floor(float(entry), atr, h, settings)
    if float(risk) < floor - 1e-9:
        return NoPlan("too_tight", atr, llm_target,
                      detail=_too_tight_message(float(risk), float(entry), floor, binding, h, settings))
    return entry, stop, risk, rule, param


def _prelude(entry, atr_14, horizon_days, settings, predicted_return_high):
    """(entry_f, atr, h, llm_target, NoPlan|None) for the checks every v2 plan shares after direction."""
    entry_f, atr = _num(entry), _num(atr_14)
    ret_high = _num(predicted_return_high)
    llm_target = round(entry_f * (1 + ret_high), 4) if (entry_f and entry_f > 0 and ret_high is not None) else None
    try:
        h = int(horizon_days)
    except (TypeError, ValueError):
        return entry_f, atr, None, llm_target, NoPlan("unsupported_horizon", atr, llm_target)
    if h not in SUPPORTED_HORIZONS or h not in settings["plan_stop_atr_mult"] or h not in settings["plan_target_r_multiple"]:
        return entry_f, atr, h, llm_target, NoPlan("unsupported_horizon", atr, llm_target)
    if entry_f is None or entry_f <= 0:
        return entry_f, atr, h, llm_target, NoPlan("missing_price", atr, llm_target)
    return entry_f, atr, h, llm_target, None


def _build_plan_v2(entry, atr_14, horizon_days, direction, settings, *, as_of_date, predicted_return_high,
                   stop_rule, stop_param, target_rule, target_param, range_hl) -> TradePlan | NoPlan:
    entry_f, atr, h, llm_target, early = _prelude(entry, atr_14, horizon_days, settings, predicted_return_high)
    why = _direction_reason(direction)
    if why:
        return NoPlan(why, atr, llm_target)
    if early:
        return early
    got = _resolve_stop(entry_f, atr, h, settings, stop_rule, stop_param, range_hl, llm_target)
    if isinstance(got, NoPlan):
        return got
    entry_d, stop_d, risk_d, s_rule, s_param = got

    t_rule = target_rule or settings.get("plan_default_target_rule", SEED_DEFAULT_TARGET_RULE)
    if t_rule not in TARGET_RULES:
        return NoPlan("invalid_rule", atr, llm_target)
    if t_rule == "r_multiple":
        r = _fnum(target_param) if target_param is not None else _fnum(settings["plan_target_r_multiple"].get(h))
        if r is None:
            return NoPlan("invalid_rule_param", atr, llm_target)
        target_d, t_param, tol = (entry_d + _d(r) * risk_d).quantize(_CENT, ROUND_HALF_UP), r, _HALF_CENT
    else:
        px = _fnum(target_param)
        if px is None:
            return NoPlan("invalid_rule_param", atr, llm_target)
        target_d, t_param, tol = _d(px).quantize(_CENT, ROUND_HALF_UP), px, Decimal(0)
    if target_d <= entry_d:
        return NoPlan("target_not_above_entry", atr, llm_target)
    rr = (target_d - entry_d) / risk_d                            # recomputed from the rounded prices
    # Rounding the target to a cent can move reward:risk by at most half a cent / risk: never let that alone fail the minimum.
    if float((target_d - entry_d + tol) / risk_d) < settings["plan_min_reward_risk"] - 1e-9:
        return NoPlan("below_min_rr", atr, llm_target)
    if as_of_date is None:
        raise ValueError("build_plan: as_of_date is required to place the time stop")
    return TradePlan(
        entry=float(entry_d), stop=float(stop_d), target=float(target_d), risk_per_share=float(risk_d),
        reward_risk=round(float(rr), 4), time_stop_date=_time_stop(as_of_date, h), atr_14=round(atr, 4) if atr else None,
        horizon_days=h, llm_target=llm_target, stop_rule=s_rule, stop_param=s_param, target_rule=t_rule,
        target_param=t_param, rules_version=RULES_VERSION)


def build_plan(entry, atr_14, horizon_days, direction, settings, *, as_of_date=None, predicted_return_high=None,
               rules_version: str = RULES_VERSION_V1, stop_rule=None, stop_param=None, target_rule=None,
               target_param=None, range_hl=None) -> TradePlan | NoPlan:
    """
    A long plan for an UP forecast. rules_version="trade-plan-v1" (the default, so existing callers and tests are untouched) is the
    original ATR/R-multiple rule with 4-decimal prices; "trade-plan-v2" adds the stop/target rules, the too_tight refusal, explicit
    DOWN/FLAT reasons and cent rounding. range_hl = (highest high, lowest low) of the last N completed daily bars up to the forecast
    date, supplied by the caller (trade_plan_db.recent_range_as_of) for the range rules; this module never fetches anything.

    v2 NoPlan reasons, first that applies: down | flat | no_direction, unsupported_horizon, missing_price, then for the stop rule
    missing_atr | missing_range | invalid_rule | invalid_rule_param, stop_not_below_entry, stop_at_or_below_zero, too_volatile,
    too_tight, then target: invalid_rule_param, target_not_above_entry, below_min_rr.
    """
    if rules_version == RULES_VERSION_V1:
        if any(x is not None for x in (stop_rule, stop_param, target_rule, target_param, range_hl)):
            raise ValueError("build_plan: stop/target rules need rules_version='trade-plan-v2'")
        return _build_plan_v1(entry, atr_14, horizon_days, direction, settings, as_of_date=as_of_date,
                              predicted_return_high=predicted_return_high)
    if rules_version != RULES_VERSION:
        raise ValueError(f"build_plan: unknown rules_version {rules_version!r}")
    return _build_plan_v2(entry, atr_14, horizon_days, direction, settings, as_of_date=as_of_date,
                          predicted_return_high=predicted_return_high, stop_rule=stop_rule, stop_param=stop_param,
                          target_rule=target_rule, target_param=target_param, range_hl=range_hl)


def build_protective_plan(entry, atr_14, horizon_days, direction, settings, *, as_of_date=None,
                          predicted_return_high=None, stop_rule=None, stop_param=None, range_hl=None) -> ProtectivePlan | NoPlan:
    """
    A protective exit for a position the owner holds when the forecast is DOWN — "where I'd sell to cap the damage if the DOWN call
    is right". Never a short, never an order. entry = the forecast's start price. Stop: the same rules and checks as a long's (so
    stop_not_below_entry, too_volatile, too_tight...). No target and no reward:risk. trim_level = entry * (1 + predicted_return_high)
    when that is above entry, else None; display only, the model's number. The caller must already know the owner holds the ticker.
    """
    entry_f, atr, h, llm_target, early = _prelude(entry, atr_14, horizon_days, settings, predicted_return_high)
    if str(direction or "").upper() != "DOWN":
        return NoPlan("not_down", atr, llm_target)
    if early:
        return early
    got = _resolve_stop(entry_f, atr, h, settings, stop_rule, stop_param, range_hl, llm_target)
    if isinstance(got, NoPlan):
        return got
    entry_d, stop_d, risk_d, s_rule, s_param = got
    ret_high = _num(predicted_return_high)
    trim = None
    if ret_high is not None:
        t = (entry_d * (1 + _d(ret_high))).quantize(_CENT, ROUND_HALF_UP)
        trim = float(t) if t > entry_d else None
    if as_of_date is None:
        raise ValueError("build_protective_plan: as_of_date is required to place the time stop")
    return ProtectivePlan(entry=float(entry_d), stop=float(stop_d), trim_level=trim, risk_per_share=float(risk_d),
                          time_stop_date=_time_stop(as_of_date, h), atr_14=round(atr, 4) if atr else None, horizon_days=h,
                          stop_rule=s_rule, stop_param=s_param)


def size_position(plan, portfolio_value, risk_profile, max_position_pct, settings) -> Sizing:
    """
    shares = floor(max_loss_usd / risk_per_share), then capped so the position value stays within max_position_pct of the
    portfolio. max_position_pct is a percent (the mandate's 10.0 means 10%), as everywhere else in the app.
    Anything non-positive or unknown gives zero shares, never a guess.
    """
    pv = _num(portfolio_value)
    risk = _num(getattr(plan, "risk_per_share", None))
    entry = _num(getattr(plan, "entry", None))
    loss_pct = settings["plan_max_loss_pct_by_risk_profile"].get(str(risk_profile or "").lower())
    max_pos = _num(max_position_pct)
    zero = Sizing(0, 0.0, 0.0, 0.0, "none")
    if not pv or pv <= 0 or not risk or risk <= 0 or not entry or entry <= 0 or loss_pct is None:
        return zero
    max_loss_usd = pv * float(loss_pct)
    by_loss = math.floor(max_loss_usd / risk)
    shares, binding = by_loss, "loss"
    if max_pos is not None and max_pos >= 0:
        by_position = math.floor(pv * (max_pos / 100.0) / entry)
        if by_position < by_loss:
            shares, binding = by_position, "position"
    shares = max(0, int(shares))
    if shares == 0:
        return Sizing(0, 0.0, 0.0, round(max_loss_usd, 2), "none" if binding == "loss" else binding)
    return Sizing(shares=shares, position_value=round(shares * entry, 2), loss_if_stopped=round(shares * risk, 2),
                  max_loss_usd=round(max_loss_usd, 2), binding_cap=binding)
