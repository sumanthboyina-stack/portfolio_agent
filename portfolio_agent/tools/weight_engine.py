"""
Dynamic weight engine — horizon-aware, context-driven source weighting.

Each horizon has its own base weights reflecting which signal dominates that
time window.  The same event-driven boosts/penalties are then applied on top:
earnings prints, VIX spikes, analyst staleness, leadership changes, etc.

Horizon intuition:
  5d  — news + short-term momentum dominate; fundamentals nearly irrelevant
  21d — balanced but research (analyst consensus) gains parity with news
  63d — fundamentals reclaim the majority; news fades to noise
  250d — fundamentals + macro dominate; news is minimal

A WEIGHT_FLOOR (5%) prevents any source from being fully zeroed out.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

# ── Legacy single-horizon base (kept for backward compat / chat path) ──────────
BASE: dict[str, float] = {
    "fundamentals": 0.28,
    "valuation":    0.22,
    "research":     0.25,
    "macro":        0.17,
    "news":         0.08,
}

# ── Horizon-specific base weights ─────────────────────────────────────────────
# valuation's share scales up with horizon — a DCF says little about a 5-day
# move and a lot about a 250-day one — taken mostly from fundamentals's share,
# with a small trim from macro/research/news at each horizon. All four rows
# still sum to 1.0 (see tests/test_weight_engine.py).
HORIZON_BASE_WEIGHTS: dict[int, dict[str, float]] = {
    5: {
        "news":         0.55,
        "research":     0.23,
        "macro":        0.13,
        "fundamentals": 0.05,
        "valuation":    0.04,
    },
    21: {
        "news":         0.28,
        "research":     0.27,
        "macro":        0.13,
        "fundamentals": 0.20,
        "valuation":    0.12,
    },
    63: {
        "news":         0.13,
        "research":     0.20,
        "macro":        0.17,
        "fundamentals": 0.30,
        "valuation":    0.20,
    },
    250: {
        "news":         0.05,
        "research":     0.12,
        "macro":        0.18,
        "fundamentals": 0.35,
        "valuation":    0.30,
    },
}

DEFAULT_BASE_WEIGHTS: dict[str, float] = {
    "news":         0.15,
    "research":     0.22,
    "macro":        0.18,
    "fundamentals": 0.25,
    "valuation":    0.20,
}

WEIGHT_FLOOR = 0.05

# ── Boost / penalty constants ─────────────────────────────────────────────────
_B_NONE     = 0.00
_B_LOW      = 0.40
_B_MEDIUM   = 1.00
_B_HIGH     = 2.50
_B_DOMINANT = 4.50

_P_NONE   = 0.00
_P_LIGHT  = 0.15
_P_MEDIUM = 0.35
_P_HEAVY  = 0.60

# ── Keyword sets for news event detection ─────────────────────────────────────

_EARNINGS_BEAT_MISS = {
    "beat", "miss", "exceeded", "fell short", "topped estimates",
    "missed estimates", "reported earnings", "q1 results", "q2 results",
    "q3 results", "q4 results", "full year results", "fiscal year",
}
_EARNINGS_BROAD = {
    "earnings", "eps", "quarterly", "revenue beat", "revenue miss",
    "guidance", "raised guidance", "lowered guidance", "outlook",
    "profit", "net income", "operating income",
}
_LEADERSHIP = {
    "ceo", "cfo", "coo", "cto", "chief executive", "chief financial",
    "resigns", "resign", "resigned", "steps down", "stepping down",
    "appointed", "replacement", "departure", "fired", "ousted",
}
_MA = {
    "acquires", "acquisition", "merger", "takeover", "buyout",
    "acquired by", "deal valued", "bid for", "offer for",
}
_REGULATORY = {
    "fda", "sec investigation", "doj", "antitrust", "lawsuit",
    "settlement", "class action", "recall", "banned", "approval",
    "approved by", "rejected by",
}
_MACRO_EVENT = {
    "federal reserve", "fed rate", "fomc", "rate decision",
    "rate hike", "rate cut", "quantitative", "balance sheet",
    "treasury yield", "yield curve", "inverted", "cpi report",
    "inflation data", "pce data",
}
_SECTOR_ROTATION = {
    "sector rotation", "tech selloff", "tech rally", "semis selling",
    "risk-off", "de-risking", "sector outflow", "rotation from",
}


# ── Signal detection (horizon-independent) ────────────────────────────────────

def _news_signal(news_data: list[dict]) -> tuple[float, str, float, str]:
    """
    Returns (boost, event_type, penalty, rationale).

    Unlike research/fundamentals, this used to only ever apply a *boost* (0 to
    4.5x) on top of the base news weight, with no penalty path — so a ticker
    with zero news still kept its full base news weight (55% for the 5-day
    horizon) despite the rationale text claiming "news receives minimum
    weight." That mismatch let an empty, unpenalized news slot quietly anchor
    the composite score toward whatever neutral-ish news_score the LLM had to
    invent for missing data, while the model's own reasoning text correctly
    talked around the gap (citing research/fundamentals instead) — a real
    disconnect between the stated explanation and the actual weighting.
    penalty now actually shrinks the news weight when data is absent or inert,
    matching how _fundamentals_penalty / research's r_penalty already work.
    """
    if not news_data:
        return _B_NONE, "NONE", _P_HEAVY, "No news data — news weight reduced, redistributed to other analysts"

    recent_cutoff = (date.today() - timedelta(days=2)).isoformat()
    recent = [n for n in news_data if (n.get("date") or "") >= recent_cutoff]

    all_text = " ".join(
        f"{n.get('headline_1','')} {n.get('headline_2','')} "
        f"{n.get('summary','')} {str(n.get('top_themes',''))}"
        for n in recent
    ).lower()

    full_text = " ".join(
        f"{n.get('headline_1','')} {n.get('headline_2','')} "
        f"{n.get('summary','')} {str(n.get('top_themes',''))}"
        for n in news_data
    ).lower()

    if any(kw in all_text for kw in _EARNINGS_BEAT_MISS):
        return _B_DOMINANT, "EARNINGS_RELEASE", _P_NONE, (
            "Earnings print detected in last 2 days "
            "(beat/miss keywords) — actual results dominate signal"
        )
    if any(kw in all_text for kw in _EARNINGS_BROAD):
        return _B_HIGH, "EARNINGS_PREVIEW", _P_NONE, (
            "Earnings-adjacent news (guidance / preview) detected — elevated news weight"
        )
    if any(kw in all_text for kw in _LEADERSHIP):
        return _B_DOMINANT, "LEADERSHIP_CHANGE", _P_NONE, (
            "Executive change detected — leadership news dominates near-term signal; "
            "historical fundamentals become less predictive"
        )
    if any(kw in all_text for kw in _MA):
        return _B_HIGH, "MA_EVENT", _P_NONE, (
            "M&A activity detected — deal news dominates valuation signal"
        )
    if any(kw in all_text for kw in _REGULATORY):
        return _B_HIGH, "REGULATORY", _P_NONE, (
            "Regulatory event detected (FDA / SEC / legal) — binary risk dominates"
        )
    if any(kw in all_text for kw in _MACRO_EVENT):
        return _B_MEDIUM, "MACRO_NEWS", _P_NONE, (
            "Fed / macro event news detected — macro weight also elevated separately"
        )
    if any(kw in full_text for kw in _SECTOR_ROTATION):
        return _B_LOW, "SECTOR_ROTATION", _P_NONE, (
            "Sector rotation keywords in 7-day news — macro + news both mildly elevated"
        )

    scores = [
        n["sentiment_score"] for n in news_data
        if n.get("sentiment_score") is not None
    ]
    if scores:
        avg_mag = sum(abs(s) for s in scores) / len(scores)
        if avg_mag > 0.5:
            return _B_MEDIUM, "ELEVATED_SENTIMENT", _P_NONE, (
                f"Strong average sentiment magnitude ({avg_mag:.2f}) — "
                "news signal elevated above baseline"
            )
        if avg_mag > 0.2:
            return _B_LOW, "MILD_SENTIMENT", _P_NONE, (
                f"Mild average sentiment ({avg_mag:.2f}) — "
                "slight news weight boost"
            )

    return _B_NONE, "NONE", _P_MEDIUM, (
        "No material news signal detected — news weight reduced; "
        "fundamentals and research carry more of the analysis"
    )


def _macro_signal(macro_snapshot: Optional[dict]) -> tuple[float, str, str]:
    """Returns (boost, event_type, rationale)."""
    if not macro_snapshot:
        return _B_NONE, "NO_DATA", "Macro snapshot unavailable"

    vix    = macro_snapshot.get("vix")
    sp_1m  = macro_snapshot.get("sp500_1mo_pct")
    regime = macro_snapshot.get("macro_regime_hint", "NEUTRAL")
    spread = macro_snapshot.get("yield_curve_spread")

    if vix and vix > 30:
        return _B_HIGH, "EXTREME_VOLATILITY", (
            f"VIX at {vix:.1f} (fear extreme) — macro conditions dominate; "
            "individual fundamentals matter less when the whole market is repricing"
        )
    if vix and vix > 22:
        return _B_MEDIUM, "ELEVATED_VOLATILITY", (
            f"VIX at {vix:.1f} (elevated) — macro uncertainty above baseline; "
            "macro weight boosted"
        )
    if sp_1m is not None and sp_1m < -5:
        return _B_MEDIUM, "MARKET_SELLOFF", (
            f"S&P 500 down {sp_1m:.1f}% this month — broad market weakness, "
            "macro/sector context elevated"
        )
    if sp_1m is not None and sp_1m > 6:
        return _B_LOW, "MARKET_RALLY", (
            f"S&P 500 up {sp_1m:.1f}% this month — strong risk-on, "
            "slight macro tailwind boost"
        )
    if spread is not None and spread < -0.5:
        return _B_MEDIUM, "INVERTED_CURVE", (
            f"Yield curve inverted ({spread:+.2f}%) — recession signal active; "
            "macro conditions elevated"
        )
    if regime == "RISK_OFF":
        return _B_MEDIUM, "RISK_OFF", (
            "Market in risk-off regime — macro headwinds elevated; "
            "sector rotation likely"
        )
    if regime == "RISK_ON":
        return _B_NONE, "RISK_ON", (
            "Risk-on environment, low volatility — macro is a tailwind but not dominant"
        )

    return _B_NONE, "NEUTRAL", (
        "Macro environment is neutral — standard weight; "
        "fundamentals and research should carry the analysis"
    )


def _research_signal(research_data: Optional[dict]) -> tuple[float, float, str]:
    """Returns (boost, penalty, rationale)."""
    if not research_data:
        return 0.0, _P_MEDIUM, "No research data in DB — research weight penalised"

    raw_fetched    = research_data.get("as_of_date") or research_data.get("raw_fetched_at") or ""
    latest_upgrade = research_data.get("latest_upgrade_date") or ""
    recent_upgs    = research_data.get("recent_upgrades") or []
    if isinstance(recent_upgs, str):
        import json
        try:
            recent_upgs = json.loads(recent_upgs)
        except Exception:
            recent_upgs = []

    week_ago  = (date.today() - timedelta(days=7)).isoformat()
    month_ago = (date.today() - timedelta(days=30)).isoformat()

    recent_count = sum(
        1 for u in recent_upgs
        if isinstance(u, dict) and (u.get("date") or "") >= week_ago
    )

    if latest_upgrade and latest_upgrade >= week_ago:
        if recent_count >= 3:
            return _B_MEDIUM, 0.0, (
                f"{recent_count} analyst actions in the last 7 days — "
                "research is highly fresh and very relevant"
            )
        return _B_LOW, 0.0, (
            f"Recent analyst action on {latest_upgrade} — "
            "research is fresh and modestly elevated"
        )

    if not raw_fetched or raw_fetched < month_ago:
        return 0.0, _P_LIGHT, (
            "Research data is over 30 days old — slight staleness penalty applied"
        )

    return 0.0, 0.0, "Research data is current with no special signal — standard weight"


def _fundamentals_penalty(
    fundamentals_data: Optional[dict],
    news_event_type: str,
) -> tuple[float, str]:
    """Returns (penalty 0.0–0.7, rationale). Higher penalty = less weight."""
    if not fundamentals_data:
        return _P_MEDIUM, "No fundamentals data in DB — weight penalised"

    as_of  = fundamentals_data.get("as_of_date") or ""
    filing = fundamentals_data.get("filing_date") or ""

    if news_event_type in ("EARNINGS_RELEASE", "EARNINGS_PREVIEW"):
        three_days_ago = (date.today() - timedelta(days=3)).isoformat()
        if filing and filing >= three_days_ago:
            return _P_NONE, (
                "New filing just landed — fundamentals data is current and directly relevant"
            )
        return _P_HEAVY, (
            "Earnings event in progress — stored fundamentals are about to be superseded "
            "by the new filing; news signal carries the near-term view"
        )

    if news_event_type == "LEADERSHIP_CHANGE":
        return _P_MEDIUM, (
            "Leadership change detected — historical fundamentals are less predictive "
            "when the management team that generated them is no longer in place"
        )

    anchor = filing or as_of
    if anchor:
        try:
            age = (date.today() - date.fromisoformat(anchor)).days
            if age < 30:
                return _P_NONE, f"Fundamentals are fresh ({age}d old) — full weight applied"
            if age < 90:
                return _P_LIGHT, f"Fundamentals are {age}d old — slight staleness penalty"
            return _P_MEDIUM, (
                f"Fundamentals are {age}d old — notable staleness; weight modestly reduced"
            )
        except ValueError:
            pass

    return _P_LIGHT, "Fundamentals age indeterminate — light staleness penalty applied"


def _valuation_penalty(
    valuation_data: Optional[dict],
    news_event_type: str,
) -> tuple[float, str]:
    """
    Returns (penalty 0.0-0.7, rationale). Structurally identical to
    _fundamentals_penalty — a DCF ages the same way a filing does, and an
    earnings release predates the DCF's own revenue/margin assumptions even
    more directly than it predates the Fundamentals score.
    """
    if not valuation_data:
        return _P_MEDIUM, "No valuation data in DB — weight penalised"

    as_of = valuation_data.get("as_of_date") or ""

    if news_event_type in ("EARNINGS_RELEASE", "EARNINGS_PREVIEW"):
        three_days_ago = (date.today() - timedelta(days=3)).isoformat()
        if as_of and as_of >= three_days_ago:
            return _P_NONE, "DCF was refreshed alongside the new filing — current and directly relevant"
        return _P_HEAVY, (
            "Earnings event in progress — the DCF's revenue/margin assumptions predate the "
            "new filing; news signal carries the near-term view"
        )

    if as_of:
        try:
            age = (date.today() - date.fromisoformat(as_of)).days
            if age < 30:
                return _P_NONE, f"Valuation is fresh ({age}d old) — full weight applied"
            if age < 90:
                return _P_LIGHT, f"Valuation is {age}d old — slight staleness penalty"
            return _P_MEDIUM, f"Valuation is {age}d old — notable staleness; weight modestly reduced"
        except ValueError:
            pass

    return _P_LIGHT, "Valuation age indeterminate — light staleness penalty applied"


# ── Core signal application ────────────────────────────────────────────────────

def _apply_signals(
    base: dict[str, float],
    news_data: list[dict],
    research_data: Optional[dict],
    macro_snapshot: Optional[dict],
    fundamentals_data: Optional[dict],
    valuation_data: Optional[dict] = None,
) -> tuple[dict, str, dict, dict, dict]:
    """
    Apply event-driven boosts/penalties to a base weight dict.

    Returns (raw_weights, regime, rationale, data_caps, signal_strengths).
    raw_weights are pre-floor, pre-normalization.
    """
    n_boost, news_event, n_penalty, n_rat = _news_signal(news_data)
    m_boost, macro_event, m_rat  = _macro_signal(macro_snapshot)
    r_boost, r_penalty, r_rat    = _research_signal(research_data)
    f_penalty, f_rat             = _fundamentals_penalty(fundamentals_data, news_event)
    v_penalty, v_rat             = _valuation_penalty(valuation_data, news_event)

    raw = {
        "fundamentals": base["fundamentals"] * (1.0 - f_penalty),
        "valuation":    base.get("valuation", 0.0) * (1.0 - v_penalty),
        "research":     base["research"]     * (1.0 + r_boost) * (1.0 - r_penalty),
        "macro":        base["macro"]        * (1.0 + m_boost),
        "news":         base["news"]         * (1.0 + n_boost) * (1.0 - n_penalty),
    }

    if news_event in ("EARNINGS_RELEASE", "LEADERSHIP_CHANGE"):
        regime = news_event
    elif news_event == "MA_EVENT":
        regime = "MA_EVENT"
    elif macro_event in ("EXTREME_VOLATILITY", "RISK_OFF", "INVERTED_CURVE"):
        regime = macro_event
    elif macro_event == "MARKET_SELLOFF":
        regime = "MARKET_SELLOFF"
    elif n_boost >= _B_HIGH or m_boost >= _B_HIGH:
        regime = "HIGH_SIGNAL"
    elif n_boost == _B_NONE and m_boost == _B_NONE and r_boost == _B_NONE:
        regime = "QUIET_DAY"
    else:
        regime = "MIXED"

    data_caps = {
        "fundamentals": 5 if not fundamentals_data else (7 if f_penalty >= _P_HEAVY else 10),
        "valuation":    5 if not valuation_data else (7 if v_penalty >= _P_HEAVY else 10),
        "research":     5 if not research_data else 10,
        "macro":        10,
        "news":         5 if not news_data else 10,
    }

    rationale = {
        "fundamentals": f_rat,
        "valuation":    v_rat,
        "research":     r_rat,
        "macro":        m_rat,
        "news":         n_rat,
    }
    signal_strengths = {
        "news_boost":      n_boost,
        "macro_boost":     m_boost,
        "research_boost":  r_boost,
        "fund_penalty":    f_penalty,
        "valuation_penalty": v_penalty,
        "news_penalty":    n_penalty,
        "news_event":      news_event,
        "macro_event":     macro_event,
    }

    return raw, regime, rationale, data_caps, signal_strengths


def _normalize(raw: dict[str, float], use_floor: bool = False) -> dict[str, float]:
    """Normalize weights to 1.0, with optional WEIGHT_FLOOR clamping."""
    if use_floor:
        raw = {k: max(v, WEIGHT_FLOOR) for k, v in raw.items()}
    total = sum(raw.values())
    weights = {k: round(v / total, 3) for k, v in raw.items()}
    diff = round(1.000 - sum(weights.values()), 3)
    weights["fundamentals"] = round(weights["fundamentals"] + diff, 3)
    return weights


# ── Public API: per-horizon ────────────────────────────────────────────────────

def compute_dynamic_weights_for_horizon(
    ticker: str,
    horizon_days: int,
    db_context: dict,
    macro_snapshot: dict,
) -> dict[str, float]:
    """
    Compute dynamic weights for a specific horizon.

    Picks the horizon's base weights, then applies the same dynamic
    adjustments (event triggers, staleness penalties) as before.
    db_context expects keys: 'news' (list), 'research' (dict),
    'fundamentals' (dict), 'valuation' (dict, optional).
    """
    base = HORIZON_BASE_WEIGHTS.get(horizon_days, DEFAULT_BASE_WEIGHTS).copy()
    raw, _, _, _, _ = _apply_signals(
        base,
        db_context.get("news") or [],
        db_context.get("research"),
        macro_snapshot,
        db_context.get("fundamentals"),
        db_context.get("valuation"),
    )
    return _normalize(raw, use_floor=True)


def _preferred_horizon_days() -> set[int] | None:
    """
    Horizon days the user wants generated, from user_profile.preferred_horizons
    (labels like "5d"). Returns None for "no filtering" — when the profile is
    unavailable, or when the stored list is empty/unparseable (an empty
    preference must never silently produce zero horizons).
    """
    try:
        from portfolio_agent.tools.user_profile_db import get_user_profile
        labels = get_user_profile().preferred_horizons
    except Exception:
        return None
    days: set[int] = set()
    for label in labels or []:
        try:
            days.add(int(str(label).strip().lower().rstrip("d")))
        except ValueError:
            continue
    return days or None


def filter_preferred_horizons(horizons: list[int]) -> list[int]:
    """Keep only the horizons in the user's preferred_horizons (order preserved).
    A pure output filter — it never changes how any remaining horizon is weighted."""
    preferred = _preferred_horizon_days()
    if preferred is None:
        return list(horizons)
    return [h for h in horizons if int(h) in preferred]


def compute_dynamic_weights_all_horizons(
    ticker: str,
    horizons: list[int],
    db_context: dict,
    macro_snapshot: dict,
) -> dict[int, dict[str, float]]:
    """Compute weights for all requested horizons in a single call, limited to
    the user's preferred_horizons (see filter_preferred_horizons)."""
    return {
        h: compute_dynamic_weights_for_horizon(ticker, h, db_context, macro_snapshot)
        for h in filter_preferred_horizons(horizons)
    }


# ── Public API: legacy + extended ─────────────────────────────────────────────

def compute_dynamic_weights(
    ticker: str,
    news_data: list[dict],
    research_data: Optional[dict],
    macro_snapshot: Optional[dict],
    fundamentals_data: Optional[dict],
    horizons: list[int] | None = None,
    valuation_data: Optional[dict] = None,
) -> dict:
    """
    Compute context-aware weights for the APEX panel synthesis.

    When horizons is provided, also computes per-horizon weights under the
    'weights_by_horizon' key — for the subset of those horizons that appear in
    the user's preferred_horizons (default: all). The weighting math for each
    remaining horizon is unchanged; the preference only filters the output.

    Returns a dict with:
      weights              — single-horizon weights (legacy BASE)
      weights_by_horizon   — {horizon_days: {source: weight}} if horizons given
      data_caps, regime, weight_summary, per_source, signal_strengths
    """
    raw, regime, rationale, data_caps, signal_strengths = _apply_signals(
        BASE, news_data, research_data, macro_snapshot, fundamentals_data, valuation_data
    )
    weights = _normalize(raw, use_floor=False)

    # Per-horizon weights
    weights_by_horizon: dict[int, dict[str, float]] | None = None
    if horizons:
        db_ctx = {
            "news":         news_data,
            "research":     research_data,
            "fundamentals": fundamentals_data,
            "valuation":    valuation_data,
        }
        weights_by_horizon = compute_dynamic_weights_all_horizons(
            ticker, horizons, db_ctx, macro_snapshot or {}
        )

    def _pct(v: float) -> str:
        return f"{round(v * 100)}%"

    weight_summary = (
        f"Fundamentals {_pct(weights['fundamentals'])} · "
        f"Valuation {_pct(weights['valuation'])} · "
        f"Research {_pct(weights['research'])} · "
        f"Macro {_pct(weights['macro'])} · "
        f"News {_pct(weights['news'])}"
    )

    def _delta(key: str) -> str:
        base_pct = round(BASE[key] * 100)
        cur_pct  = round(weights[key] * 100)
        diff_val = cur_pct - base_pct
        if diff_val == 0:
            return f"{cur_pct}% (base)"
        sign = "+" if diff_val > 0 else ""
        return f"{cur_pct}% ({sign}{diff_val}% vs base {base_pct}%)"

    result = {
        "weights":   weights,
        "data_caps": data_caps,
        "regime":    regime,
        "weight_summary": weight_summary,
        "per_source": {
            src: {
                "weight":    weights[src],
                "delta":     _delta(src),
                "rationale": rationale[src],
            }
            for src in ("fundamentals", "valuation", "research", "macro", "news")
        },
        "signal_strengths": signal_strengths,
    }
    if weights_by_horizon is not None:
        result["weights_by_horizon"] = weights_by_horizon
    return result
