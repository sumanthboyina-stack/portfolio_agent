"""
Dynamic weight engine for the APEX reasoning agent.

Replaces static 40/30/20/10 weights with context-aware weights that reflect
the actual information content of each data source at analysis time.

Core insight: weights should represent signal-to-noise ratio, not category
importance. On an earnings release day, yesterday's cached fundamentals
carry almost no signal — the earnings print is everything. On a quiet
Tuesday with no news, fundamentals and research should carry the weight.

Algorithm:
  1. Compute a boost multiplier per source (how much extra signal does it
     carry today above its baseline?)
  2. Compute a penalty per source (how stale / about-to-be-superseded is it?)
  3. raw_weight = base × (1 + boost) × (1 - penalty)
  4. Normalize to sum to 1.0

Output includes: weights dict, regime label, signal rationale per source.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

# ── Base weights (quiet day, all data fresh, no major events) ─────────────────
BASE: dict[str, float] = {
    "fundamentals": 0.40,
    "research":     0.30,
    "macro":        0.20,
    "news":         0.10,
}

# Boost constants — applied as: base × (1 + boost)
_B_NONE     = 0.00   # no incremental signal
_B_LOW      = 0.40   # mild — recent activity but nothing dramatic
_B_MEDIUM   = 1.00   # notable — worth upweighting 2x
_B_HIGH     = 2.50   # major — 3.5x effective weight
_B_DOMINANT = 4.50   # event owns the story — 5.5x effective weight

# Penalty constants — applied as: (1 - penalty)
_P_NONE   = 0.00
_P_LIGHT  = 0.15
_P_MEDIUM = 0.35
_P_HEAVY  = 0.60   # source is stale or about to be superseded

# ── Keyword sets for news event detection ────────────────────────────────────

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


# ── Signal detection functions ────────────────────────────────────────────────

def _news_signal(
    news_data: list[dict],
) -> tuple[float, str, str]:
    """
    Returns (boost, event_type, rationale).

    event_type:
      EARNINGS_RELEASE | EARNINGS_PREVIEW | LEADERSHIP_CHANGE | MA_EVENT |
      REGULATORY | ELEVATED_SENTIMENT | MILD_SENTIMENT | NONE
    """
    if not news_data:
        return _B_NONE, "NONE", "No news data — news receives minimum weight"

    # Only last 2 days qualify as high-impact (today's signal, not week-old context)
    recent_cutoff = (date.today() - timedelta(days=2)).isoformat()
    recent = [n for n in news_data if (n.get("date") or "") >= recent_cutoff]

    # Combine all text from recent items
    all_text = " ".join(
        f"{n.get('headline_1','')} {n.get('headline_2','')} "
        f"{n.get('summary','')} {str(n.get('top_themes',''))}"
        for n in recent
    ).lower()

    # Full 7-day text for gentler signals
    full_text = " ".join(
        f"{n.get('headline_1','')} {n.get('headline_2','')} "
        f"{n.get('summary','')} {str(n.get('top_themes',''))}"
        for n in news_data
    ).lower()

    # Check in priority order (most impactful first)
    if any(kw in all_text for kw in _EARNINGS_BEAT_MISS):
        return _B_DOMINANT, "EARNINGS_RELEASE", (
            "Earnings print detected in last 2 days "
            "(beat/miss keywords) — actual results dominate signal"
        )
    if any(kw in all_text for kw in _EARNINGS_BROAD):
        return _B_HIGH, "EARNINGS_PREVIEW", (
            "Earnings-adjacent news (guidance / preview) detected — elevated news weight"
        )
    if any(kw in all_text for kw in _LEADERSHIP):
        return _B_DOMINANT, "LEADERSHIP_CHANGE", (
            "Executive change detected — leadership news dominates near-term signal; "
            "historical fundamentals become less predictive"
        )
    if any(kw in all_text for kw in _MA):
        return _B_HIGH, "MA_EVENT", (
            "M&A activity detected — deal news dominates valuation signal"
        )
    if any(kw in all_text for kw in _REGULATORY):
        return _B_HIGH, "REGULATORY", (
            "Regulatory event detected (FDA / SEC / legal) — binary risk dominates"
        )
    if any(kw in all_text for kw in _MACRO_EVENT):
        return _B_MEDIUM, "MACRO_NEWS", (
            "Fed / macro event news detected — macro weight also elevated separately"
        )
    if any(kw in full_text for kw in _SECTOR_ROTATION):
        return _B_LOW, "SECTOR_ROTATION", (
            "Sector rotation keywords in 7-day news — macro + news both mildly elevated"
        )

    # Sentiment magnitude fallback across all 7 days
    scores = [
        n["sentiment_score"] for n in news_data
        if n.get("sentiment_score") is not None
    ]
    if scores:
        avg_mag = sum(abs(s) for s in scores) / len(scores)
        if avg_mag > 0.5:
            return _B_MEDIUM, "ELEVATED_SENTIMENT", (
                f"Strong average sentiment magnitude ({avg_mag:.2f}) — "
                "news signal elevated above baseline"
            )
        if avg_mag > 0.2:
            return _B_LOW, "MILD_SENTIMENT", (
                f"Mild average sentiment ({avg_mag:.2f}) — "
                "slight news weight boost"
            )

    return _B_NONE, "NONE", (
        "No material news signal detected — news receives minimum weight; "
        "fundamentals and research carry the analysis"
    )


def _macro_signal(
    macro_snapshot: Optional[dict],
) -> tuple[float, str, str]:
    """Returns (boost, event_type, rationale)."""
    if not macro_snapshot:
        return _B_NONE, "NO_DATA", "Macro snapshot unavailable"

    vix     = macro_snapshot.get("vix")
    sp_1m   = macro_snapshot.get("sp500_1mo_pct")
    regime  = macro_snapshot.get("macro_regime_hint", "NEUTRAL")
    spread  = macro_snapshot.get("yield_curve_spread")

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


def _research_signal(
    research_data: Optional[dict],
) -> tuple[float, float, str]:
    """Returns (boost, penalty, rationale). Boost and penalty are additive adjustments."""
    if not research_data:
        return 0.0, _P_MEDIUM, "No research data in DB — research weight penalised"

    raw_fetched    = research_data.get("raw_fetched_at") or ""
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

    # Count very recent analyst actions
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

    # If we are on/near an earnings event, check freshness of filing
    if news_event_type in ("EARNINGS_RELEASE", "EARNINGS_PREVIEW"):
        # New filing just dropped (last 3 days) → actually very fresh
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

    # Age-based staleness
    anchor = filing or as_of
    if anchor:
        try:
            age = (date.today() - date.fromisoformat(anchor)).days
            if age < 30:
                return _P_NONE, f"Fundamentals are fresh ({age}d old) — full weight applied"
            if age < 90:
                return _P_LIGHT, f"Fundamentals are {age}d old — slight staleness penalty"
            return _P_MEDIUM, (
                f"Fundamentals are {age}d old — notable staleness; "
                "weight modestly reduced"
            )
        except ValueError:
            pass

    return _P_LIGHT, "Fundamentals age indeterminate — light staleness penalty applied"


# ── Public API ────────────────────────────────────────────────────────────────

def compute_dynamic_weights(
    ticker: str,
    news_data: list[dict],
    research_data: Optional[dict],
    macro_snapshot: Optional[dict],
    fundamentals_data: Optional[dict],
) -> dict:
    """
    Compute context-aware weights for the APEX panel synthesis.

    Returns a dict with:
      weights          — {fundamentals, research, macro, news} summing to 1.0
      regime           — human-readable label for this analysis session
      signal_strengths — raw boost/penalty values per source
      rationale        — one-sentence explanation per source weight
    """
    # Step 1: compute signals
    n_boost, news_event, n_rationale    = _news_signal(news_data)
    m_boost, macro_event, m_rationale   = _macro_signal(macro_snapshot)
    r_boost, r_penalty, r_rationale     = _research_signal(research_data)
    f_penalty, f_rationale              = _fundamentals_penalty(fundamentals_data, news_event)

    # Step 2: raw weights
    raw = {
        "fundamentals": BASE["fundamentals"] * (1.0 - f_penalty),
        "research":     BASE["research"]     * (1.0 + r_boost) * (1.0 - r_penalty),
        "macro":        BASE["macro"]        * (1.0 + m_boost),
        "news":         BASE["news"]         * (1.0 + n_boost),
    }

    # Step 3: normalize
    total = sum(raw.values())
    weights = {k: round(v / total, 3) for k, v in raw.items()}

    # Fix floating-point rounding so they sum exactly to 1.000
    diff = round(1.000 - sum(weights.values()), 3)
    weights["fundamentals"] = round(weights["fundamentals"] + diff, 3)

    # Score caps: domains with no data are capped at 5; heavily penalised at 7.
    # Macro is always available (live yfinance) so never capped.
    data_caps = {
        "fundamentals": 5 if not fundamentals_data else (7 if f_penalty >= _P_HEAVY else 10),
        "research":     5 if not research_data else 10,
        "macro":        10,
        "news":         5 if not news_data else 10,
    }

    # Step 4: regime label
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

    # Step 5: human-readable weight summary
    def _pct(v: float) -> str:
        return f"{round(v * 100)}%"

    weight_summary = (
        f"Fundamentals {_pct(weights['fundamentals'])} · "
        f"Research {_pct(weights['research'])} · "
        f"Macro {_pct(weights['macro'])} · "
        f"News {_pct(weights['news'])}"
    )

    # Deviation from base (for display)
    def _delta(key: str) -> str:
        base_pct = round(BASE[key] * 100)
        cur_pct  = round(weights[key] * 100)
        diff_val = cur_pct - base_pct
        if diff_val == 0:
            return f"{cur_pct}% (base)"
        sign = "+" if diff_val > 0 else ""
        return f"{cur_pct}% ({sign}{diff_val}% vs base {base_pct}%)"

    return {
        "weights":   weights,
        "data_caps": data_caps,
        "regime":    regime,
        "weight_summary": weight_summary,
        "per_source": {
            "fundamentals": {"weight": weights["fundamentals"], "delta": _delta("fundamentals"),
                             "rationale": f_rationale},
            "research":     {"weight": weights["research"],     "delta": _delta("research"),
                             "rationale": r_rationale},
            "macro":        {"weight": weights["macro"],        "delta": _delta("macro"),
                             "rationale": m_rationale},
            "news":         {"weight": weights["news"],         "delta": _delta("news"),
                             "rationale": n_rationale},
        },
        "signal_strengths": {
            "news_boost":     n_boost,
            "macro_boost":    m_boost,
            "research_boost": r_boost,
            "fund_penalty":   f_penalty,
            "news_event":     news_event,
            "macro_event":    macro_event,
        },
    }
