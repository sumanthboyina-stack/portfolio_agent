"""
Canonical market-only context for APEX forecasts.

build_market_context(ticker, horizons) gathers everything a forecast prompt
may see — stored fundamentals, research, news, valuation, a live macro
snapshot, the ticker's SHARED forecast history, system-wide failure patterns
mined from shared history, and deterministic per-horizon weights — for an
explicit set of supported horizons. It never reads a user profile, holdings,
accounts or chat state, and it refuses to return content that contains a
known private term (see portfolio_agent.privacy).

The returned MarketContext is the proof a writer needs to store a forecast as
shared: it carries the evidence references, the history lineage and a digest
of the exact payload the model saw.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping

from portfolio_agent.privacy import assert_market_only
from portfolio_agent.tools.weight_engine import SUPPORTED_HORIZONS, compute_dynamic_weights

MARKET_CONTEXT_VERSION = "market-context/1.0"
DEFAULT_CONTEXT_HORIZONS: tuple[int, ...] = (5, 21, 63)   # interactive / unscheduled callers


@dataclass(frozen=True)
class MarketContext:
    ticker: str
    horizons: tuple[int, ...]
    payload: dict                       # exactly what the prompt receives
    evidence_refs: dict                 # what the payload was built from
    history_lineage: tuple[dict, ...]   # shared forecast rows consulted
    digest: str                         # sha256 of the payload
    built_at: str                       # UTC ISO
    version: str = MARKET_CONTEXT_VERSION
    market_only: bool = True

    def to_json(self) -> str:
        return json.dumps(self.payload, default=str)


def _to_dict(obj):
    if obj is None or isinstance(obj, dict):
        return obj
    return obj.to_dict() if hasattr(obj, "to_dict") else dict(obj)


def _get(d, *keys):
    d = _to_dict(d) or {}
    for k in keys:
        if d.get(k) not in (None, ""):
            return d.get(k)
    return None


def default_loaders() -> dict[str, Callable]:
    from portfolio_agent.tools.fundamentals_db import get_stored_fundamentals
    from portfolio_agent.tools.prediction_db import SHARED_SCOPES, get_prediction_history
    from portfolio_agent.tools.reasoning_tools import _get_recent_news, get_macro_snapshot
    from portfolio_agent.tools.research_db import get_stored_research
    from portfolio_agent.tools.validation_engine import get_active_failure_patterns
    from portfolio_agent.tools.valuation_db import get_stored_valuation

    def _macro() -> dict:
        try:
            return json.loads(get_macro_snapshot())
        except Exception:
            return {}

    return {
        "fundamentals": get_stored_fundamentals,
        "research": get_stored_research,
        "news": lambda t: _get_recent_news(t, days=7),
        "valuation": get_stored_valuation,
        "macro": _macro,
        "history": lambda t: get_prediction_history(t, limit=5, scopes=SHARED_SCOPES),
        "failure_patterns": get_active_failure_patterns,
    }


def build_market_context(ticker: str, horizons: list[int] | tuple[int, ...], *,
                         loaders: Mapping[str, Callable] | None = None,
                         private_terms: frozenset[str] | None = None,
                         macro_snapshot: dict | None = None) -> MarketContext:
    """
    *horizons* must be explicit and supported (see weight_engine.SUPPORTED_HORIZONS);
    nothing here consults a user's preferred horizons. *loaders* overrides the
    data sources (tests); *private_terms* overrides the private-term set.

    *macro_snapshot*: inject an already-fetched macro payload (e.g. from
    tools.reasoning_tools.fetch_macro_snapshot().payload) instead of calling
    the macro loader again. Macro data is ticker-independent, so a caller
    building contexts for many tickers in one run (see pipeline.daily.apex)
    fetches it ONCE and passes the same dict into every call here — this
    parameter is what makes that sharing possible without changing anything
    about what a single, standalone build_market_context call does by
    default (macro_snapshot=None still calls the loader, exactly as before).
    """
    ticker = ticker.upper().strip()
    hz = tuple(sorted({int(h) for h in horizons}))
    if not hz:
        raise ValueError("build_market_context: at least one horizon is required")
    bad = [h for h in hz if h not in SUPPORTED_HORIZONS]
    if bad:
        raise ValueError(f"build_market_context: unsupported horizon(s) {bad}; supported: {list(SUPPORTED_HORIZONS)}")
    L = dict(default_loaders()) if loaders is None else dict(loaders)

    fundamentals = L["fundamentals"](ticker)
    research = L["research"](ticker)
    news = L["news"](ticker) or []
    valuation = L["valuation"](ticker)
    macro_snapshot = (L["macro"]() or {}) if macro_snapshot is None else macro_snapshot
    history = list(L["history"](ticker) or [])
    try:
        failure_patterns = list(L["failure_patterns"]() or [])
    except Exception:
        failure_patterns = []

    data_gaps = [name for name, present in (("fundamentals", fundamentals), ("research", research),
                                             ("news", news), ("valuation", valuation)) if not present]

    weight_data = compute_dynamic_weights(
        ticker=ticker, news_data=news, research_data=_to_dict(research), macro_snapshot=macro_snapshot,
        fundamentals_data=_to_dict(fundamentals), horizons=list(hz), valuation_data=_to_dict(valuation),
    )
    caps = weight_data.get("data_caps", {})
    cap_lines = [f"{domain} ≤ {cap} (no data in DB — analyst must state 'No {domain} data')"
                 for domain, cap in caps.items() if cap < 10]
    cap_instruction = ("SCORE CAPS (mandatory — scores must not exceed these values): " + "; ".join(cap_lines)
                       if cap_lines else "All data sources populated — no score caps.")

    def _pct(v: float) -> str:
        return f"{round(v * 100)}%"

    wbh = weight_data.get("weights_by_horizon") or {}
    horizon_weight_lines = [
        f"  {h}d: News {_pct(w['news'])} · Research {_pct(w['research'])} · Macro {_pct(w['macro'])} · "
        f"Fundamentals {_pct(w['fundamentals'])} · Valuation {_pct(w['valuation'])}"
        for h, w in sorted(wbh.items())
    ]
    horizon_weight_str = "\n".join(horizon_weight_lines) if horizon_weight_lines else "(none)"

    if failure_patterns:
        failure_pattern_instruction = (
            "MANDATORY — KNOWN RECURRING FAILURE PATTERNS (statistically confirmed "
            "across multiple weekly validation reports): check whether this ticker's "
            "current weight_regime, horizon, or conviction band matches any pattern below. "
            "If it matches, say so explicitly and do not exceed conviction 5 for that "
            "horizon unless you can articulate a concrete, ticker-specific reason this "
            "case differs from the historical pattern.\n"
            + "\n".join(
                f"  - {p['description']} (seen in {p['weeks_seen']} weekly reports; "
                f"accuracy {p.get('latest_accuracy', '?')} vs baseline {p.get('latest_baseline_accuracy', '?')})"
                for p in failure_patterns
            )
        )
    else:
        failure_pattern_instruction = "No recurring system-wide failure patterns currently flagged."

    payload = {
        "ticker": ticker,
        "fundamentals": _to_dict(fundamentals),
        "research": _to_dict(research),
        "news": news,
        "valuation": valuation,
        "macro_snapshot": macro_snapshot,
        "prediction_history": [_to_dict(p) for p in history],
        "data_gaps": data_gaps,
        "dynamic_weights": weight_data,
        "known_failure_patterns": failure_patterns,
        "failure_pattern_instruction": failure_pattern_instruction,
        "weight_instruction": (
            f"CRITICAL: Use the exact per-horizon weights from dynamic_weights.weights_by_horizon "
            f"for each horizon's composite score. Regime: {weight_data['regime']}.\n"
            f"Per-horizon weights:\n{horizon_weight_str}\n{cap_instruction}"
        ),
    }
    text = json.dumps(payload, default=str)
    assert_market_only(text, private_terms, what=f"market context for {ticker}")

    built_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lineage = tuple(
        {"id": _get(p, "id"), "as_of_date": _get(p, "prediction_date", "as_of_date"),
         "horizon_days": _get(p, "horizon_days"), "scope": _get(p, "scope")}
        for p in history
    )
    news_dates = sorted(str(_get(n, "published_at", "date", "published") or "") for n in news if isinstance(n, (dict,)) or hasattr(n, "get"))
    evidence_refs = {
        "context_version": MARKET_CONTEXT_VERSION,
        "horizons": list(hz),
        "fundamentals_as_of": _get(fundamentals, "as_of_date", "filing_date"),
        "research_as_of": _get(research, "as_of_date", "fetched_at", "updated_at"),
        "valuation_as_of": _get(valuation, "as_of_date", "computed_at"),
        "news_count": len(news),
        "news_latest": (news_dates[-1] or None) if news_dates else None,
        "macro_snapshot_at": _get(macro_snapshot, "as_of", "timestamp") or built_at,
        "prediction_history_ids": [x["id"] for x in lineage if x.get("id") is not None],
        "failure_pattern_count": len(failure_patterns),
        "weight_regime": weight_data.get("regime"),
    }
    return MarketContext(
        ticker=ticker, horizons=hz, payload=payload, evidence_refs=evidence_refs, history_lineage=lineage,
        digest=hashlib.sha256(text.encode()).hexdigest()[:16], built_at=built_at,
    )
