"""
Position view — the PRIVATE, per-owner companion to a shared market forecast, for a ticker the owner holds.

  forecast   what APEX's SHARED forecasts say for the ticker (label, direction, horizon, age) — read, never generated here
  position   what the owner holds: shares, value, weight, cost basis, unrealized gain, short/long holding period of the
             open lots (ledger-managed accounts only)
  limits     the owner's own concentration limits (position, sector, issuer) against the current figures
  clearance  the owner's own action permission for the ticker, as information
  read       a short, neutral relation between the forecast direction and the limit status, plus a one-line summary

Deterministic: no LLM, no network, no price fetch. The forecast side is market-only; the position side is the owner's own
rows. Nothing here ever flows back into a forecast, a prompt or a shared table, and the text is information, never an
instruction to trade. Cost basis and holding period are FACTS shown beside the forecast; they do not change the read.

Entry points: build_position_view_for_owner(owner, ticker) takes a trusted owner string (the batch phase and the chat
page, whose owner the web layer took from the verified login); build_position_view(ctx, ticker) is the checked version.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

VIEW_VERSION = "position-view/1"
HORIZONS = (5, 21, 63)
LONG_TERM_DAYS = 365

_DIRECTION = {"STRONG_BUY": "positive", "BUY": "positive", "HOLD": "neutral", "SELL": "negative", "STRONG_SELL": "negative"}

# the fixed vocabulary of the "relation" field
REL_NO_FORECAST = "no_forecast"
REL_FIT_UNKNOWN = "fit_unknown"
REL_NEUTRAL = "forecast_neutral"
REL_MIXED = "forecast_mixed"
REL_NEGATIVE_OVER = "forecast_negative_over_limit"
REL_POSITIVE_OVER = "forecast_positive_over_limit"
REL_NEGATIVE_WITHIN = "forecast_negative_within_limits"
REL_POSITIVE_WITHIN = "forecast_positive_within_limits"


def _age_days(created_at: str | None, today: date) -> int | None:
    try:
        created = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    except Exception:
        return None
    return max(0, (today - created.date()).days)


def _forecasts(ticker: str, today: date) -> list[dict]:
    """The latest SHARED, non-challenger forecast per horizon (shared scope passed explicitly: a private or challenger row
    can never be returned)."""
    from portfolio_agent.tools.prediction_db import SHARED_SCOPES, get_latest_prediction
    out = []
    for h in HORIZONS:
        try:
            p = get_latest_prediction(ticker, h, scopes=SHARED_SCOPES)
        except Exception:
            p = None
        if p is None or not p.recommendation:
            continue
        out.append({"horizon_days": h, "label": p.recommendation, "direction": _DIRECTION.get(p.recommendation, "neutral"),
                    "predicted_direction": p.predicted_direction, "conviction_score": p.conviction_score,
                    "as_of": p.created_at, "age_days": _age_days(p.created_at, today)})
    return out


def _forecast_direction(forecasts: list[dict]) -> str:
    if not forecasts:
        return "none"
    directions = {f["direction"] for f in forecasts}
    return directions.pop() if len(directions) == 1 else "mixed"


def _holding_period(portfolio_id: int, ticker: str, today: date) -> str | None:
    """SHORT / LONG / MIXED from the open lots of ledger-managed accounts (LONG = held more than 365 days); None when no
    ledger account holds the ticker (snapshot accounts have no lots)."""
    from portfolio_agent.tools import holdings_db as repo
    from portfolio_agent.tools import ledger_db
    from portfolio_agent.tools.db import db_conn
    periods = set()
    try:
        accounts = [a for a in repo.list_accounts(portfolio_id) if a.get("holdings_source") == "ledger"]
        if not accounts:
            return None
        with db_conn() as c:
            for a in accounts:
                for lot in ledger_db.list_lots(c, a["account_id"], open_only=True):
                    if lot.get("ticker") != ticker:
                        continue
                    opened = date.fromisoformat(str(lot["open_date"])[:10])
                    periods.add("LONG" if (today - opened).days > LONG_TERM_DAYS else "SHORT")
    except Exception:
        return None
    if not periods:
        return None
    return periods.pop() if len(periods) == 1 else "MIXED"


def _position(owner: str, portfolio: dict, ticker: str, exposures, today: date) -> dict | None:
    from portfolio_agent.tools import holdings_db as repo
    rows = [h.to_dict() for h in repo.get_holdings(portfolio_id=portfolio["portfolio_id"]) if h.ticker == ticker]
    if not rows:
        return None
    shares = sum(float(r.get("shares") or 0) for r in rows)
    # current_value is only stored when supplied; otherwise the position is worth shares x last price (as the exposures value it)
    value = sum(float(r["current_value"]) if r.get("current_value") is not None
                else float(r.get("shares") or 0) * float(r.get("current_price") or 0) for r in rows)
    cost = 0.0
    cost_known = True
    for r in rows:
        c = r.get("cost_basis_total")
        if c is None and r.get("avg_cost") is not None and r.get("shares") is not None:
            c = float(r["avg_cost"]) * float(r["shares"])
        if c is None:
            cost_known = False
        else:
            cost += float(c)
    cost_basis = round(cost, 2) if cost_known and cost > 0 else None
    gain_pct = round((value - cost) / cost * 100, 2) if cost_basis and value else None
    return {"shares": round(shares, 6), "current_value": round(value, 2) if value else None,
            "weight_pct": exposures.per_holding_weight_pct.get(ticker), "cost_basis_total": cost_basis,
            "unrealized_gain_pct": gain_pct, "holding_period": _holding_period(portfolio["portfolio_id"], ticker, today)}


def _limits(assessment) -> list[dict]:
    out = []
    for c in assessment.limits:
        d = c.to_dict() if hasattr(c, "to_dict") else dict(c)
        if d.get("name") not in ("position_concentration", "sector_concentration", "issuer_concentration"):
            continue
        breached = d.get("breached")
        status = "over" if breached is True else "within" if breached is False else "unknown"
        out.append({"name": d["name"], "limit_pct": d.get("limit_pct"), "actual_pct": d.get("actual_pct"), "status": status})
    return out


def _fit(limits: list[dict]) -> str:
    if any(l["status"] == "over" for l in limits):
        return "over_limit"
    if not limits or any(l["status"] == "unknown" for l in limits):
        return "unknown"
    return "within_limits"


def _relation(forecast: str, fit: str) -> str:
    if forecast == "none":
        return REL_NO_FORECAST
    if forecast == "mixed":
        return REL_MIXED
    if forecast == "neutral":
        return REL_NEUTRAL
    if fit == "unknown":
        return REL_FIT_UNKNOWN
    if fit == "over_limit":
        return REL_NEGATIVE_OVER if forecast == "negative" else REL_POSITIVE_OVER
    return REL_NEGATIVE_WITHIN if forecast == "negative" else REL_POSITIVE_WITHIN


def _summary(ticker: str, forecasts: list[dict], position: dict, limits: list[dict], clearance: dict | None) -> str:
    parts = []
    if forecasts:
        labels = ", ".join(f"{f['label']} ({f['horizon_days']}d)" for f in forecasts)
        age = min((f["age_days"] for f in forecasts if f["age_days"] is not None), default=None)
        parts.append(f"Shared forecast label: {labels}" + (f", as of {age} day{'s' if age != 1 else ''} ago" if age is not None else "") + ".")
    else:
        parts.append("No shared forecast on file.")
    w = position.get("weight_pct")
    over = [l for l in limits if l["status"] == "over"]
    parts.append(f"Your weight {w:.1f}%" + (f"; over: {', '.join(l['name'].replace('_', ' ') for l in over)}." if over else "; no limit is exceeded."
                                              if limits and all(l["status"] == "within" for l in limits) else "; limits not fully known.")
                 if isinstance(w, (int, float)) else "Your weight is not known.")
    g = position.get("unrealized_gain_pct")
    if g is not None:
        hp = position.get("holding_period")
        parts.append(f"Unrealized {g:+.1f}% on cost" + (f", {hp.lower()}-term lots." if hp and hp != "MIXED" else ", mixed holding periods." if hp == "MIXED" else "."))
    else:
        parts.append("Cost basis is not known.")
    if clearance:
        parts.append(f"Clearance: {clearance['decision']}.")
    return " ".join(parts)


def build_position_view_for_owner(owner: str, ticker: str, *, exposures=None, policy=None, today: date | None = None) -> dict | None:
    """The owner's position view for *ticker*, or None when the owner has no portfolio or does not hold it. Reads only that
    owner's own rows plus shared market forecasts. Pass the owner's own *exposures* and *policy* to reuse them across
    tickers (the batch phase does); never another owner's."""
    from portfolio_agent.clearance import evaluate_clearance
    from portfolio_agent.tools import holdings_db as repo
    from portfolio_agent.tools.portfolio_assessment import build_assessment_for_owner, compute_exposures_for_owner
    from portfolio_agent.tools.portfolio_policy import get_effective_policy_for_owner
    from portfolio_agent.tools.user_profile_db import get_user_profile_by_owner

    ticker = (ticker or "").upper().strip()
    if not owner or not ticker:
        return None
    today = today or date.today()
    portfolio = repo.get_portfolio_by_owner(owner)
    if portfolio is None:
        return None
    policy = policy or get_effective_policy_for_owner(owner)
    exposures = exposures or compute_exposures_for_owner(owner, portfolio, policy=policy)
    position = _position(owner, portfolio, ticker, exposures, today)
    if position is None:
        return None

    assessment = build_assessment_for_owner(owner, ticker, exposures=exposures, policy=policy)
    limits = _limits(assessment)
    forecasts = _forecasts(ticker, today)
    forecast_direction = _forecast_direction(forecasts)
    fit = _fit(limits)
    clearance = None
    try:
        decision = evaluate_clearance(ticker, get_user_profile_by_owner(owner), owner_scope=f"user:{owner}")
        clearance = {"decision": decision.decision, "memo": decision.memo}
    except Exception:
        clearance = None            # information only: a failed evaluation never hides the rest of the view
    return {
        "version": VIEW_VERSION, "ticker": ticker, "as_of": today.isoformat(),
        "forecast": {"direction": forecast_direction, "horizons": forecasts},
        "position": position, "limits": limits, "fit": fit, "clearance": clearance,
        "relation": _relation(forecast_direction, fit),
        "summary": _summary(ticker, forecasts, position, limits, clearance),
    }


def build_position_view(ctx, ticker: str) -> dict | None:
    """Checked entry point: the actor's own view (NotAuthorized for an actor with no portfolio)."""
    from portfolio_agent.services.account_service import resolve_portfolio
    resolve_portfolio(ctx)
    return build_position_view_for_owner(ctx.actor, ticker)
