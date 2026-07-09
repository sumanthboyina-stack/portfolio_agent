"""FRED series metadata registry."""
from __future__ import annotations

FRED_SERIES: dict[str, dict] = {
    # ── Inflation ─────────────────────────────────────────────────────────────
    "CPI": {
        "id": "CPIAUCSL", "name": "Consumer Price Index",
        "frequency": "monthly", "tier": 1, "category": "inflation",
        "typical_monthly_sigma": 0.15,   # σ of m/m change in index points
    },
    "CORE_CPI": {
        "id": "CPILFESL", "name": "Core CPI (ex Food & Energy)",
        "frequency": "monthly", "tier": 1, "category": "inflation",
        "typical_monthly_sigma": 0.12,
    },
    "PCE": {
        "id": "PCEPI", "name": "PCE Price Index",
        "frequency": "monthly", "tier": 1, "category": "inflation",
        "typical_monthly_sigma": 0.10,
    },
    "PPI": {
        "id": "PPIACO", "name": "Producer Price Index",
        "frequency": "monthly", "tier": 2, "category": "inflation",
        "typical_monthly_sigma": 0.40,
    },

    # ── Labor ─────────────────────────────────────────────────────────────────
    "NFP": {
        "id": "PAYEMS", "name": "Nonfarm Payrolls",
        "frequency": "monthly", "tier": 1, "category": "labor",
        "typical_monthly_sigma": 75_000,  # σ of m/m change in jobs
    },
    "UNEMPLOYMENT": {
        "id": "UNRATE", "name": "Unemployment Rate",
        "frequency": "monthly", "tier": 1, "category": "labor",
        "typical_monthly_sigma": 0.10,
    },
    "JOBLESS_CLAIMS": {
        "id": "ICSA", "name": "Initial Jobless Claims",
        "frequency": "weekly", "tier": 1, "category": "labor",
        "typical_monthly_sigma": 15_000,
    },

    # ── Growth ────────────────────────────────────────────────────────────────
    "GDP": {
        "id": "GDPC1", "name": "Real GDP",
        "frequency": "quarterly", "tier": 1, "category": "growth",
        "typical_monthly_sigma": 0.5,  # σ of q/q annualized % change
    },
    "RETAIL_SALES": {
        "id": "RSAFS", "name": "Retail Sales",
        "frequency": "monthly", "tier": 2, "category": "growth",
        "typical_monthly_sigma": 0.5,
    },

    # ── Rates / Fed ───────────────────────────────────────────────────────────
    "FED_FUNDS": {
        "id": "FEDFUNDS", "name": "Effective Fed Funds Rate",
        "frequency": "monthly", "tier": 1, "category": "rates",
        "typical_monthly_sigma": 0.25,
    },
    "10Y": {
        "id": "DGS10", "name": "10-Year Treasury Yield",
        "frequency": "daily", "tier": 1, "category": "rates",
        "typical_monthly_sigma": 0.20,
    },
    "2Y": {
        "id": "DGS2", "name": "2-Year Treasury Yield",
        "frequency": "daily", "tier": 1, "category": "rates",
        "typical_monthly_sigma": 0.25,
    },
    "3M": {
        "id": "DGS3MO", "name": "3-Month Treasury Yield",
        "frequency": "daily", "tier": 2, "category": "rates",
        "typical_monthly_sigma": 0.20,
    },

    # ── Markets / Risk ────────────────────────────────────────────────────────
    "VIX": {
        "id": "VIXCLS", "name": "VIX",
        "frequency": "daily", "tier": 1, "category": "risk",
        "typical_monthly_sigma": 3.0,
    },
    "HY_SPREAD": {
        "id": "BAMLH0A0HYM2", "name": "High Yield OAS",
        "frequency": "daily", "tier": 2, "category": "credit",
        "typical_monthly_sigma": 30,  # basis points
    },
    "DXY": {
        "id": "DTWEXBGS", "name": "USD Broad Dollar Index",
        "frequency": "daily", "tier": 2, "category": "currency",
        "typical_monthly_sigma": 1.5,
    },

    # ── Sentiment ────────────────────────────────────────────────────────────
    "CONSUMER_SENTIMENT": {
        "id": "UMCSENT", "name": "U. Michigan Consumer Sentiment",
        "frequency": "monthly", "tier": 2, "category": "sentiment",
        "typical_monthly_sigma": 3.0,
    },
}

# Tier-1 series fetched every run for the enriched snapshot
TIER1_KEYS = [k for k, v in FRED_SERIES.items() if v["tier"] == 1]
