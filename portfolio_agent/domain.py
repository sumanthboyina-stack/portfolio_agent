"""
Typed domain models for the portfolio intelligence system.

Five core entities — all stdlib dataclasses (no extra dependencies):
  Holding              — one brokerage position
  FundamentalsSnapshot — EDGAR-derived financial metrics for one ticker
  ResearchSnapshot     — sell-side broker consensus + LLM summary
  NewsSummary          — daily news headline pair + sentiment for one ticker
  Prediction           — one APEX horizon prediction row
  UserProfile          — the single user_profile row (mandate limits + preferences)
  UserNotificationPrefs — the single user_notifications row (channel, thresholds, quiet hours)

Design:
  - DB read functions return typed models; write functions keep their existing
    flat-arg signatures (no breakage).
  - Dict-compat API (__getitem__, get, items) lets existing call-sites that use
    h["field"] or h.get("field") continue to work without changes.
  - to_dict() provides a clean dict for JSON serialisation.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any


# ── Shared parse helpers ───────────────────────────────────────────────────────

def _parse_json_list(raw: Any) -> list:
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    try:
        result = json.loads(raw)
        return result if isinstance(result, list) else [result]
    except (json.JSONDecodeError, TypeError):
        return []


def _parse_json_dict(raw: Any) -> dict:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        result = json.loads(raw)
        return result if isinstance(result, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (ValueError, TypeError):
        return None


def _safe_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


# ── Dict-compat mixin ─────────────────────────────────────────────────────────
# Lets existing call-sites use h["field"] and h.get("field", default) unchanged.
# Attribute access (h.ticker) is preferred for new code.

class _DictCompat:
    def __getitem__(self, key: str):
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key) from None

    def get(self, key: str, default=None):
        return getattr(self, key, default)

    def items(self):
        return self.to_dict().items()

    def to_dict(self) -> dict:
        raise NotImplementedError


# ── Holding ────────────────────────────────────────────────────────────────────

@dataclass
class Holding(_DictCompat):
    """One brokerage position row from the holdings table."""
    ticker: str
    id: int | None = None
    description: str | None = None
    shares: float | None = None
    avg_cost: float | None = None
    cost_basis_total: float | None = None
    current_price: float | None = None
    current_value: float | None = None
    account_name: str | None = None
    account_number: str | None = None
    account_type: str | None = None
    broker: str | None = None
    sector: str | None = None
    as_of_date: str | None = None
    synced_at: str | None = None

    def __post_init__(self) -> None:
        self.ticker = self.ticker.upper()

    @classmethod
    def from_db_row(cls, row: dict) -> "Holding":
        return cls(
            ticker=row["ticker"],
            id=row.get("id"),
            description=row.get("description"),
            shares=_safe_float(row.get("shares")),
            avg_cost=_safe_float(row.get("avg_cost")),
            cost_basis_total=_safe_float(row.get("cost_basis_total")),
            current_price=_safe_float(row.get("current_price")),
            current_value=_safe_float(row.get("current_value")),
            account_name=row.get("account_name"),
            account_number=row.get("account_number"),
            account_type=row.get("account_type"),
            broker=row.get("broker"),
            sector=row.get("sector"),
            as_of_date=row.get("as_of_date"),
            synced_at=row.get("synced_at"),
        )

    @property
    def unrealized_gain_loss(self) -> float | None:
        if self.current_value is not None and self.cost_basis_total is not None:
            return round(self.current_value - self.cost_basis_total, 2)
        return None

    @property
    def unrealized_gain_loss_pct(self) -> float | None:
        if self.cost_basis_total and self.cost_basis_total > 0 and self.current_value is not None:
            return round(
                (self.current_value - self.cost_basis_total) / self.cost_basis_total * 100, 2
            )
        return None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ticker": self.ticker,
            "description": self.description,
            "shares": self.shares,
            "avg_cost": self.avg_cost,
            "cost_basis_total": self.cost_basis_total,
            "current_price": self.current_price,
            "current_value": self.current_value,
            "account_name": self.account_name,
            "account_number": self.account_number,
            "account_type": self.account_type,
            "broker": self.broker,
            "sector": self.sector,
            "as_of_date": self.as_of_date,
            "synced_at": self.synced_at,
            "unrealized_gain_loss": self.unrealized_gain_loss,
            "unrealized_gain_loss_pct": self.unrealized_gain_loss_pct,
        }


# ── UserProfile ────────────────────────────────────────────────────────────────

# Horizon labels as stored in user_profile.preferred_horizons; days = int(label[:-1]).
ALL_HORIZON_LABELS = ("5d", "21d", "63d", "250d")


@dataclass
class UserProfile(_DictCompat):
    """The single user_profile row: identity, mandate limits, preferences."""
    id: int = 1
    display_name: str | None = None
    base_currency: str = "USD"
    timezone: str = "America/Chicago"
    max_sector_pct: float = 25.0
    max_issuer_pct: float = 15.0
    max_per_candidate_pct_of_cash: float = 0.4
    max_post_trade_position_pct: float = 0.15
    sector_exclusions: list = field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None
    # compliance
    employer: str | None = None
    pre_clearance_required: bool = True
    blackout_start: str | None = None   # ISO date, inclusive
    blackout_end: str | None = None     # ISO date, inclusive
    disclaimer_accepted_at: str | None = None
    # horizons the user wants generated/shown; default = all four (no filtering)
    preferred_horizons: list = field(default_factory=lambda: list(ALL_HORIZON_LABELS))

    @classmethod
    def from_db_row(cls, row: dict) -> "UserProfile":
        defaults = cls()

        def _num(key: str, default: float) -> float:
            # NULL falls back to the default; an explicit 0.0 is kept as-is.
            v = _safe_float(row.get(key))
            return default if v is None else v

        return cls(
            id=row.get("id") or 1,
            display_name=row.get("display_name"),
            base_currency=row.get("base_currency") or defaults.base_currency,
            timezone=row.get("timezone") or defaults.timezone,
            max_sector_pct=_num("max_sector_pct", defaults.max_sector_pct),
            max_issuer_pct=_num("max_issuer_pct", defaults.max_issuer_pct),
            max_per_candidate_pct_of_cash=_num(
                "max_per_candidate_pct_of_cash", defaults.max_per_candidate_pct_of_cash
            ),
            max_post_trade_position_pct=_num(
                "max_post_trade_position_pct", defaults.max_post_trade_position_pct
            ),
            sector_exclusions=_parse_json_list(row.get("sector_exclusions")),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
            employer=row.get("employer"),
            pre_clearance_required=(
                defaults.pre_clearance_required
                if row.get("pre_clearance_required") is None
                else bool(row.get("pre_clearance_required"))
            ),
            blackout_start=row.get("blackout_start") or None,
            blackout_end=row.get("blackout_end") or None,
            disclaimer_accepted_at=row.get("disclaimer_accepted_at") or None,
            # NULL (pre-migration row) → all horizons; an explicit list is kept as-is
            preferred_horizons=(
                list(ALL_HORIZON_LABELS)
                if row.get("preferred_horizons") is None
                else _parse_json_list(row.get("preferred_horizons"))
            ),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "base_currency": self.base_currency,
            "timezone": self.timezone,
            "max_sector_pct": self.max_sector_pct,
            "max_issuer_pct": self.max_issuer_pct,
            "max_per_candidate_pct_of_cash": self.max_per_candidate_pct_of_cash,
            "max_post_trade_position_pct": self.max_post_trade_position_pct,
            "sector_exclusions": self.sector_exclusions,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "employer": self.employer,
            "pre_clearance_required": self.pre_clearance_required,
            "blackout_start": self.blackout_start,
            "blackout_end": self.blackout_end,
            "disclaimer_accepted_at": self.disclaimer_accepted_at,
            "preferred_horizons": self.preferred_horizons,
        }


# ── UserNotificationPrefs ──────────────────────────────────────────────────────

NOTIFICATION_CHANNELS = ("none", "email", "slack")


@dataclass
class UserNotificationPrefs(_DictCompat):
    """The single user_notifications row: personal filter on event notifications."""
    id: int = 1
    channel: str = "none"
    min_severity_threshold: int = 3
    min_conviction_threshold: int | None = None
    quiet_hours_start: str | None = None   # "HH:MM" in the profile's timezone
    quiet_hours_end: str | None = None     # "HH:MM"; start > end spans midnight
    created_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_db_row(cls, row: dict) -> "UserNotificationPrefs":
        defaults = cls()
        sev = _safe_int(row.get("min_severity_threshold"))
        return cls(
            id=row.get("id") or 1,
            channel=row.get("channel") or defaults.channel,
            min_severity_threshold=defaults.min_severity_threshold if sev is None else sev,
            min_conviction_threshold=_safe_int(row.get("min_conviction_threshold")),
            quiet_hours_start=row.get("quiet_hours_start") or None,
            quiet_hours_end=row.get("quiet_hours_end") or None,
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )

    @property
    def enabled(self) -> bool:
        return self.channel != "none"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "channel": self.channel,
            "min_severity_threshold": self.min_severity_threshold,
            "min_conviction_threshold": self.min_conviction_threshold,
            "quiet_hours_start": self.quiet_hours_start,
            "quiet_hours_end": self.quiet_hours_end,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


# ── FundamentalsSnapshot ───────────────────────────────────────────────────────

@dataclass
class FundamentalsSnapshot(_DictCompat):
    """EDGAR-derived financial metrics for one ticker (one row per filing date)."""
    ticker: str
    as_of_date: str
    id: int | None = None
    filing_type: str = ""
    filing_date: str = ""
    revenue_growth_yoy_pct: float | None = None
    net_margin: float | None = None
    fcf: float | None = None
    debt_to_equity: float | None = None
    fundamental_score: int | None = None
    key_strengths: list[str] = field(default_factory=list)
    key_risks: list[str] = field(default_factory=list)
    summary: str = ""
    raw_filing_ref: str = ""
    model_name: str | None = None
    model_provider: str | None = None
    updated_at: str | None = None
    guidance_text: str | None = None
    guidance_date: str | None = None
    guidance_direction: str | None = None  # raised | maintained | lowered | withdrawn | none

    def __post_init__(self) -> None:
        self.ticker = self.ticker.upper()

    @classmethod
    def from_db_row(cls, row: dict) -> "FundamentalsSnapshot":
        return cls(
            ticker=row["ticker"],
            as_of_date=row.get("as_of_date") or "",
            id=row.get("id"),
            filing_type=row.get("filing_type") or "",
            filing_date=row.get("filing_date") or "",
            revenue_growth_yoy_pct=_safe_float(row.get("revenue_growth_yoy_pct")),
            net_margin=_safe_float(row.get("net_margin")),
            fcf=_safe_float(row.get("fcf")),
            debt_to_equity=_safe_float(row.get("debt_to_equity")),
            fundamental_score=_safe_int(row.get("fundamental_score")),
            key_strengths=_parse_json_list(row.get("key_strengths")),
            key_risks=_parse_json_list(row.get("key_risks")),
            summary=row.get("summary") or "",
            raw_filing_ref=row.get("raw_filing_ref") or "",
            model_name=row.get("model_name"),
            model_provider=row.get("model_provider"),
            updated_at=row.get("updated_at"),
            guidance_text=row.get("guidance_text"),
            guidance_date=row.get("guidance_date"),
            guidance_direction=row.get("guidance_direction"),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ticker": self.ticker,
            "as_of_date": self.as_of_date,
            "filing_type": self.filing_type,
            "filing_date": self.filing_date,
            "revenue_growth_yoy_pct": self.revenue_growth_yoy_pct,
            "net_margin": self.net_margin,
            "fcf": self.fcf,
            "debt_to_equity": self.debt_to_equity,
            "fundamental_score": self.fundamental_score,
            "key_strengths": self.key_strengths,
            "key_risks": self.key_risks,
            "summary": self.summary,
            "raw_filing_ref": self.raw_filing_ref,
            "model_name": self.model_name,
            "model_provider": self.model_provider,
            "updated_at": self.updated_at,
            "guidance_text": self.guidance_text,
            "guidance_date": self.guidance_date,
            "guidance_direction": self.guidance_direction,
        }


# ── ResearchSnapshot ───────────────────────────────────────────────────────────

@dataclass
class ResearchSnapshot(_DictCompat):
    """Sell-side broker consensus + LLM summary (one row per ticker)."""
    ticker: str
    id: int | None = None
    raw_fetched_at: str | None = None
    consensus: str = ""
    consensus_mean: float | None = None
    num_analysts: int | None = None
    price_target_avg: float | None = None
    price_target_median: float | None = None
    price_target_high: float | None = None
    price_target_low: float | None = None
    current_price: float | None = None
    upside_to_mean_pct: float | None = None
    latest_upgrade_date: str = ""
    recent_upgrades: list[dict] = field(default_factory=list)
    quarterly_ratings: list[dict] = field(default_factory=list)
    rec_trend: list[dict] = field(default_factory=list)
    finnhub_pt: dict | None = None
    last_llm_run_date: str | None = None
    highlights: list[str] = field(default_factory=list)
    research_score: int | None = None
    summary: str = ""
    model_name: str | None = None
    model_provider: str | None = None
    updated_at: str | None = None

    def __post_init__(self) -> None:
        self.ticker = self.ticker.upper()

    @classmethod
    def from_db_row(cls, row: dict) -> "ResearchSnapshot":
        return cls(
            ticker=row["ticker"],
            id=row.get("id"),
            raw_fetched_at=row.get("as_of_date") or row.get("raw_fetched_at"),
            consensus=row.get("consensus") or "",
            consensus_mean=_safe_float(row.get("consensus_mean")),
            num_analysts=_safe_int(row.get("num_analysts")),
            price_target_avg=_safe_float(row.get("price_target_avg")),
            price_target_median=_safe_float(row.get("price_target_median")),
            price_target_high=_safe_float(row.get("price_target_high")),
            price_target_low=_safe_float(row.get("price_target_low")),
            current_price=_safe_float(row.get("current_price")),
            upside_to_mean_pct=_safe_float(row.get("upside_to_mean_pct")),
            latest_upgrade_date=row.get("latest_upgrade_date") or "",
            recent_upgrades=_parse_json_list(row.get("recent_upgrades")),
            quarterly_ratings=_parse_json_list(row.get("quarterly_ratings")),
            rec_trend=_parse_json_list(row.get("rec_trend")),
            finnhub_pt=_parse_json_dict(row.get("finnhub_pt")) or None,
            last_llm_run_date=row.get("last_llm_run_date"),
            highlights=_parse_json_list(row.get("highlights")),
            research_score=_safe_int(row.get("research_score")),
            summary=row.get("summary") or "",
            model_name=row.get("model_name"),
            model_provider=row.get("model_provider"),
            updated_at=row.get("updated_at"),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ticker": self.ticker,
            "raw_fetched_at": self.raw_fetched_at,
            "consensus": self.consensus,
            "consensus_mean": self.consensus_mean,
            "num_analysts": self.num_analysts,
            "price_target_avg": self.price_target_avg,
            "price_target_median": self.price_target_median,
            "price_target_high": self.price_target_high,
            "price_target_low": self.price_target_low,
            "current_price": self.current_price,
            "upside_to_mean_pct": self.upside_to_mean_pct,
            "latest_upgrade_date": self.latest_upgrade_date,
            "recent_upgrades": self.recent_upgrades,
            "quarterly_ratings": self.quarterly_ratings,
            "rec_trend": self.rec_trend,
            "finnhub_pt": self.finnhub_pt,
            "last_llm_run_date": self.last_llm_run_date,
            "highlights": self.highlights,
            "research_score": self.research_score,
            "summary": self.summary,
            "model_name": self.model_name,
            "model_provider": self.model_provider,
            "updated_at": self.updated_at,
        }


# ── NewsSummary ────────────────────────────────────────────────────────────────

@dataclass
class NewsSummary(_DictCompat):
    """Daily news headline pair + sentiment for one ticker (one row per ticker per day)."""
    ticker: str
    date: str
    id: int | None = None
    headline_1: str = ""
    headline_2: str = ""
    sentiment: str = "NEUTRAL"    # POSITIVE | NEUTRAL | NEGATIVE
    sentiment_score: float = 0.0  # -1.0 to 1.0
    top_themes: list[str] = field(default_factory=list)
    trending: str = ""
    source: str = ""
    model_name: str | None = None
    model_provider: str | None = None
    updated_at: str | None = None

    _VALID_SENTIMENTS = frozenset({"POSITIVE", "NEUTRAL", "NEGATIVE"})

    def __post_init__(self) -> None:
        self.ticker = self.ticker.upper()
        if self.sentiment not in self._VALID_SENTIMENTS:
            self.sentiment = "NEUTRAL"
        self.sentiment_score = max(-1.0, min(1.0, float(self.sentiment_score)))

    @classmethod
    def from_db_row(cls, row: dict) -> "NewsSummary":
        raw = row.get("top_themes")
        if isinstance(raw, list):
            themes = raw
        elif isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                themes = parsed if isinstance(parsed, list) else [str(parsed)]
            except (json.JSONDecodeError, ValueError):
                themes = [t.strip() for t in raw.split(",") if t.strip()]
        else:
            themes = []

        return cls(
            ticker=row["ticker"],
            date=row.get("date") or "",
            id=row.get("id"),
            headline_1=row.get("headline_1") or "",
            headline_2=row.get("headline_2") or "",
            sentiment=row.get("sentiment") or "NEUTRAL",
            sentiment_score=float(row.get("sentiment_score") or 0.0),
            top_themes=themes,
            trending=row.get("trending") or "",
            source=row.get("source") or "",
            model_name=row.get("model_name"),
            model_provider=row.get("model_provider"),
            updated_at=row.get("updated_at"),
        )

    @property
    def is_positive(self) -> bool:
        return self.sentiment == "POSITIVE"

    @property
    def is_negative(self) -> bool:
        return self.sentiment == "NEGATIVE"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ticker": self.ticker,
            "date": self.date,
            "headline_1": self.headline_1,
            "headline_2": self.headline_2,
            "sentiment": self.sentiment,
            "sentiment_score": self.sentiment_score,
            "top_themes": self.top_themes,
            "trending": self.trending,
            "source": self.source,
            "model_name": self.model_name,
            "model_provider": self.model_provider,
            "updated_at": self.updated_at,
        }


# ── HorizonDistribution ────────────────────────────────────────────────────────

@dataclass
class HorizonDistribution:
    """Probability distribution (integers summing to 100) across 5 return buckets."""
    strong_down: float | None = None    # < -5%
    moderate_down: float | None = None  # -5% to -1%
    flat: float | None = None           # -1% to +1%
    moderate_up: float | None = None    # +1% to +5%
    strong_up: float | None = None      # > +5%

    @classmethod
    def from_row(cls, row: dict) -> "HorizonDistribution":
        return cls(
            strong_down=_safe_float(row.get("p_strong_down")),
            moderate_down=_safe_float(row.get("p_moderate_down")),
            flat=_safe_float(row.get("p_flat")),
            moderate_up=_safe_float(row.get("p_moderate_up")),
            strong_up=_safe_float(row.get("p_strong_up")),
        )

    def to_dict(self) -> dict:
        return {
            "strong_down": self.strong_down,
            "moderate_down": self.moderate_down,
            "flat": self.flat,
            "moderate_up": self.moderate_up,
            "strong_up": self.strong_up,
        }


# ── Prediction ─────────────────────────────────────────────────────────────────

_VALID_PREDICTIONS     = frozenset({"BULLISH", "BEARISH", "NEUTRAL"})
_VALID_RECOMMENDATIONS = frozenset({"STRONG_BUY", "BUY", "HOLD", "SELL", "STRONG_SELL"})
_INT_SCORE_FIELDS      = ("confidence", "fundamental_score", "research_score",
                          "macro_score", "news_score")


@dataclass
class Prediction(_DictCompat):
    """One APEX horizon prediction row (one per ticker per horizon per day)."""
    ticker: str
    prediction: str          # BULLISH | BEARISH | NEUTRAL
    recommendation: str      # STRONG_BUY | BUY | HOLD | SELL | STRONG_SELL
    id: int | None = None
    created_at: str = ""
    prediction_date: str = ""
    horizon_days: int | None = None
    prediction_type: str | None = None
    evaluation_date: str | None = None
    predicted_direction: str | None = None
    predicted_return_low: float | None = None
    predicted_return_high: float | None = None
    conviction_score: float | None = None
    confidence: int | None = None
    target_price: float | None = None
    fundamental_score: int | None = None
    research_score: int | None = None
    macro_score: int | None = None
    news_score: int | None = None
    composite_score: float | None = None
    reasoning: str = ""
    reasoning_text: str | None = None
    panel_summary: dict = field(default_factory=dict)
    data_sources: list = field(default_factory=list)
    model_name: str | None = None
    model_provider: str | None = None
    start_price: float | None = None
    pt_mean: float | None = None
    pt_median: float | None = None
    pt_high: float | None = None
    pt_low: float | None = None
    pt_num_analysts: int | None = None
    pt_current_price: float | None = None
    distribution: HorizonDistribution = field(default_factory=HorizonDistribution)
    used_fallback: bool = False
    actual_return: float | None = None
    actual_direction: str | None = None
    outcome: str | None = None
    error_magnitude: float | None = None
    evaluation_status: str = "pending"
    evaluated_at: str | None = None
    actual_bucket: str | None = None
    brier_score: float | None = None
    log_loss: float | None = None

    def __post_init__(self) -> None:
        self.ticker = self.ticker.upper().strip()
        # Normalize enums — clamp to a known-good default rather than raising,
        # so DB rows with legacy values still deserialize cleanly.
        if self.prediction not in _VALID_PREDICTIONS:
            self.prediction = "NEUTRAL"
        if self.recommendation not in _VALID_RECOMMENDATIONS:
            self.recommendation = "HOLD"
        # Clamp integer scores to [1, 10].
        for _f in _INT_SCORE_FIELDS:
            _v = getattr(self, _f)
            if _v is not None:
                setattr(self, _f, max(1, min(10, int(round(_v)))))
        # Clamp composite_score to [1.0, 10.0].
        if self.composite_score is not None:
            self.composite_score = max(1.0, min(10.0, round(float(self.composite_score), 4)))
        # Ensure distribution is always a HorizonDistribution instance, never a plain dict.
        # Python dataclasses don't enforce types at construction time, so guard here.
        if isinstance(self.distribution, dict):
            d = self.distribution
            self.distribution = HorizonDistribution(
                strong_down=d.get("strong_down"),
                moderate_down=d.get("moderate_down"),
                flat=d.get("flat"),
                moderate_up=d.get("moderate_up"),
                strong_up=d.get("strong_up"),
            )

    @classmethod
    def from_db_row(cls, row: dict) -> "Prediction":
        return cls(
            ticker=row["ticker"],
            prediction=row.get("prediction") or "NEUTRAL",
            recommendation=row.get("recommendation") or "HOLD",
            id=row.get("id"),
            created_at=row.get("created_at") or "",
            prediction_date=row.get("as_of_date") or row.get("prediction_date") or "",
            horizon_days=_safe_int(row.get("horizon_days")),
            prediction_type=row.get("prediction_type"),
            evaluation_date=row.get("evaluation_date"),
            predicted_direction=row.get("predicted_direction"),
            predicted_return_low=_safe_float(row.get("predicted_return_low")),
            predicted_return_high=_safe_float(row.get("predicted_return_high")),
            conviction_score=_safe_float(row.get("conviction_score")),
            confidence=_safe_int(row.get("confidence")),
            target_price=_safe_float(row.get("target_price")),
            fundamental_score=_safe_int(row.get("fundamental_score")),
            research_score=_safe_int(row.get("research_score")),
            macro_score=_safe_int(row.get("macro_score")),
            news_score=_safe_int(row.get("news_score")),
            composite_score=_safe_float(row.get("composite_score")),
            reasoning=row.get("reasoning") or "",
            reasoning_text=row.get("reasoning_text"),
            panel_summary=_parse_json_dict(row.get("panel_summary")),
            data_sources=_parse_json_list(row.get("data_sources")),
            model_name=row.get("model_name"),
            model_provider=row.get("model_provider"),
            start_price=_safe_float(row.get("start_price")),
            pt_mean=_safe_float(row.get("pt_mean")),
            pt_median=_safe_float(row.get("pt_median")),
            pt_high=_safe_float(row.get("pt_high")),
            pt_low=_safe_float(row.get("pt_low")),
            pt_num_analysts=_safe_int(row.get("pt_num_analysts")),
            pt_current_price=_safe_float(row.get("pt_current_price")),
            distribution=HorizonDistribution.from_row(row),
            used_fallback=bool(row.get("used_fallback")),
            actual_return=_safe_float(row.get("actual_return")),
            actual_direction=row.get("actual_direction"),
            outcome=row.get("outcome"),
            error_magnitude=_safe_float(row.get("error_magnitude")),
            evaluation_status=row.get("evaluation_status") or "pending",
            evaluated_at=row.get("evaluated_at"),
            actual_bucket=row.get("actual_bucket"),
            brier_score=_safe_float(row.get("brier_score")),
            log_loss=_safe_float(row.get("log_loss")),
        )

    @property
    def is_bullish(self) -> bool:
        return self.recommendation in ("BUY", "STRONG_BUY")

    @property
    def is_bearish(self) -> bool:
        return self.recommendation in ("SELL", "STRONG_SELL")

    @property
    def horizon_label(self) -> str:
        if self.horizon_days is not None:
            return {5: "5d", 21: "21d", 63: "63d"}.get(self.horizon_days, f"{self.horizon_days}d")
        return self.prediction_type or "?"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ticker": self.ticker,
            "created_at": self.created_at,
            "prediction_date": self.prediction_date,
            "horizon_days": self.horizon_days,
            "horizon_label": self.horizon_label,
            "prediction_type": self.prediction_type,
            "evaluation_date": self.evaluation_date,
            "predicted_direction": self.predicted_direction,
            "predicted_return_low": self.predicted_return_low,
            "predicted_return_high": self.predicted_return_high,
            "conviction_score": self.conviction_score,
            "prediction": self.prediction,
            "recommendation": self.recommendation,
            "confidence": self.confidence,
            "target_price": self.target_price,
            "fundamental_score": self.fundamental_score,
            "research_score": self.research_score,
            "macro_score": self.macro_score,
            "news_score": self.news_score,
            "composite_score": self.composite_score,
            "reasoning": self.reasoning,
            "reasoning_text": self.reasoning_text,
            "panel_summary": self.panel_summary,
            "data_sources": self.data_sources,
            "model_name": self.model_name,
            "model_provider": self.model_provider,
            "start_price": self.start_price,
            "pt_mean": self.pt_mean,
            "pt_median": self.pt_median,
            "pt_high": self.pt_high,
            "pt_low": self.pt_low,
            "pt_num_analysts": self.pt_num_analysts,
            "pt_current_price": self.pt_current_price,
            "distribution": self.distribution.to_dict(),
            "used_fallback": self.used_fallback,
            "actual_return": self.actual_return,
            "actual_direction": self.actual_direction,
            "outcome": self.outcome,
            "error_magnitude": self.error_magnitude,
            "evaluation_status": self.evaluation_status,
            "evaluated_at": self.evaluated_at,
            "actual_bucket": self.actual_bucket,
            "brier_score": self.brier_score,
            "log_loss": self.log_loss,
        }
