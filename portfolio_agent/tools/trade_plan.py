"""
Trade plan — a deterministic entry / stop / target / time stop for a shared UP forecast.

The LLM never sets these numbers. The prediction supplies the entry (its as-of close) and the horizon; the stop is
an ATR multiple, the target an R-multiple of the stop distance, and the time stop is the horizon in trading days.
All of it is a pure function of (entry, ATR as of the prediction date, horizon, settings), so a plan can be
recomputed, backfilled and tested. `llm_target` (the model's own upper return) is recorded for comparison only.

Pure: no DB, no network. The persistence + ATR lookup live in tools.trade_plan_db; the settings in engine_settings_db.

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

RULES_VERSION = "trade-plan-v1"

SUPPORTED_HORIZONS = (5, 21, 63)

SEED_STOP_ATR_MULT = {5: 1.5, 21: 2.5, 63: 4.0}
SEED_TARGET_R_MULTIPLE = {5: 1.5, 21: 2.0, 63: 2.5}
SEED_MIN_REWARD_RISK = 1.5
SEED_MAX_STOP_PCT = {5: 0.08, 21: 0.15, 63: 0.25}
SEED_MAX_LOSS_PCT = {"conservative": 0.005, "moderate": 0.01, "aggressive": 0.015}


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

    def to_dict(self) -> dict:
        return {"plan_status": "planned", "entry": self.entry, "stop": self.stop, "target": self.target,
                "risk_per_share": self.risk_per_share, "reward_risk": self.reward_risk,
                "time_stop_date": self.time_stop_date, "atr_14": self.atr_14, "horizon_days": self.horizon_days,
                "llm_target": self.llm_target}


@dataclass(frozen=True)
class NoPlan:
    reason: str
    atr_14: float | None = None
    llm_target: float | None = None

    @property
    def message(self) -> str:
        return NO_PLAN_REASONS.get(self.reason, self.reason)

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


def build_plan(entry, atr_14, horizon_days, direction, settings, *, as_of_date=None,
               predicted_return_high=None) -> TradePlan | NoPlan:
    """
    entry = the as-of close; risk/share = stop_atr_mult[h] * atr_14; stop = entry - risk; target = entry + R * risk.
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
