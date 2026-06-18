"""Batch-prompt templates and LLM constants shared across daily pipeline phases."""

from __future__ import annotations

# ── Batch sizes ───────────────────────────────────────────────────────────────
_BATCH_SIZE      = 10  # tickers per LLM call (fundamentals + research)
_BATCH_NEWS_SIZE = 5   # tickers per news LLM call (articles are verbose)

# ── Prompt templates ──────────────────────────────────────────────────────────
_BATCH_FUND_PROMPT = """\
You are a fundamental analysis specialist. Analyze the {n} companies below using ONLY the provided financial data.

For each company compute ALL of these fields:
  revenue_growth_yoy_pct  — % change from prior year revenue to most recent year (float, e.g. 4.2)
  net_margin              — net_income / revenue, most recent year (decimal, e.g. 0.241)
  fcf                     — operating_cf minus capex, most recent year (raw number in USD, e.g. 108000000000)
  debt_to_equity          — total_liabilities / stockholders_equity, most recent year (float)
  fundamental_score       — 1–10 integer (10 = strongest; weight growth, margins, leverage, CF quality)
  key_strengths           — list of exactly 2 specific strengths observed in the numbers
  key_risks               — list of exactly 2 specific risks observed in the numbers
  summary                 — 2 sentence narrative

Rules:
  - Use null for any metric you cannot compute from the data below
  - Base everything on the numbers shown — no external knowledge
  - Preserve ticker symbols exactly as shown
  - Output ONLY a valid JSON array with {n} objects, in the same order as the input
  - Start with [ and end with ] — no other text

{data_blocks}
"""

_BATCH_RESEARCH_PROMPT = """\
You are a sell-side research analyst. Summarize broker/analyst sentiment for the {n} companies below using ONLY the provided data.

For each company return ALL of these fields:
  ticker          — exact ticker symbol as given
  highlights      — list of exactly 3 specific, data-driven observations (e.g. consensus trend, PT gap vs current price, notable upgrades/downgrades, Finnhub trend direction)
  research_score  — 1–10 integer (10 = strongest buy conviction; weight consensus mean, upside to target, recent upgrade bias, monthly trend direction)
  summary         — 2 sentence narrative covering overall analyst stance and key near-term catalyst

Rules:
  - Use null for research_score if data is insufficient
  - highlights must be a list of plain strings (not nested objects)
  - Preserve ticker symbols exactly as shown
  - Output ONLY a valid JSON array with {n} objects, in the same order as the input
  - Start with [ and end with ] — no other text

{data_blocks}
"""

_BATCH_NEWS_PROMPT = """\
You are a financial news analyst. Analyze the news articles for each company below and produce a structured summary.

Market context (applies to all tickers):
{market_context}

For EACH ticker, return a JSON array entry with exactly these fields:
  ticker          — exact symbol as shown
  headline_1      — most important single-sentence headline (or "No news today" if none)
  headline_2      — second most important headline (or "" if only one article)
  sentiment       — POSITIVE | NEUTRAL | NEGATIVE
  sentiment_score — float from -1.0 (very negative) to 1.0 (very positive)
  top_themes      — list of up to 3 theme strings (e.g. "earnings beat", "product launch")
  trending        — 2-3 sentence narrative: what is driving this ticker in the news today

Rules:
  - Base everything ONLY on the articles provided — no external knowledge
  - If a ticker has very few or minor articles, use NEUTRAL / 0.0
  - Preserve ticker symbols exactly
  - Output ONLY a valid JSON array with {n} objects in the same order as the input
  - Start with [ and end with ] — no other text

{ticker_blocks}
"""

_APEX_PROMPT_TEMPLATE = """\
You are APEX (Adaptive Portfolio EXpert), a multi-perspective investment reasoning system.

Ticker  : {ticker}
Question: Daily portfolio review — provide investment recommendation.

=== FULL ANALYSIS CONTEXT (pre-loaded) ===
{ctx_json}
=== END CONTEXT ===

Using the context above (fundamentals, research, news, macro, dynamic_weights, prediction_history):

1. State the weight regime and signal summary (from dynamic_weights.regime and dynamic_weights.signal_strengths).
2. Have each analyst score their domain 1-10: DR. CHEN (fundamentals), MARCUS WEBB (research),
   ELENA VARGA (macro), JAMES PARK (news).
3. Cross-examine if scores diverge > 3 pts.
4. Compare to prior prediction if one exists.
5. For EACH scheduled horizon, look up its weights from dynamic_weights.weights_by_horizon[horizon_days]
   and compute: horizon_composite = fund_w×fund_score + res_w×res_score + mac_w×mac_score + news_w×news_score.
   For the top-level composite_score use the weights from the longest scheduled horizon (most balanced view).
6. Map the overall composite to STRONG_BUY/BUY/HOLD/SELL/STRONG_SELL.
7. For EACH scheduled horizon below, provide direction, return range, conviction, the horizon-specific
   weights used, AND a probability distribution across 5 return buckets (integers summing to 100):
   strong_down (<-5%), moderate_down (-5% to -1%), flat (-1% to +1%), moderate_up (+1% to +5%), strong_up (>+5%).
   Be honest: if you're uncertain, spread probability across buckets. Don't collapse everything into one.
{horizons_instruction}

End your response with EXACTLY this JSON block (no text after):

```json
{{
  "ticker": "{ticker}",
  "prediction": "BULLISH|BEARISH|NEUTRAL",
  "recommendation": "STRONG_BUY|BUY|HOLD|SELL|STRONG_SELL",
  "confidence": 7,
  "target_price": null,
  "fundamental_score": 7,
  "research_score": 7,
  "macro_score": 6,
  "news_score": 6,
  "composite_score": 6.5,
  "weight_regime": "QUIET_DAY",
  "weights_used": {{"fundamentals": 0.35, "research": 0.30, "macro": 0.20, "news": 0.15}},
  "panel_summary": {{
    "chen_verdict": "BULLISH (7/10) — one sentence",
    "webb_verdict": "BULLISH (7/10) — one sentence",
    "varga_verdict": "NEUTRAL (6/10) — one sentence",
    "park_verdict":  "NEUTRAL (6/10) — one sentence",
    "key_debate": "Panel consensus or main disagreement"
  }},
  "reasoning": "3-5 sentence narrative",
  "changed_from_previous": false,
  "previous_recommendation": null,
  "horizons": [
    {{
      "horizon_days": 5,
      "predicted_direction": "UP|DOWN|FLAT",
      "predicted_return_low": 1.5,
      "predicted_return_high": 4.0,
      "conviction_score": 7,
      "horizon_composite": 6.8,
      "weights_used": {{"news": 0.60, "research": 0.22, "macro": 0.12, "fundamentals": 0.06}},
      "reasoning_text": "News/momentum catalyst driving 5-day outlook",
      "distribution": {{
        "strong_down": 5,
        "moderate_down": 10,
        "flat": 15,
        "moderate_up": 45,
        "strong_up": 25
      }}
    }}
  ]
}}
```"""

# ── Publisher quality tiers used by _rank_articles ────────────────────────────
_NEWS_PUB_TIER1 = frozenset({
    "reuters", "bloomberg", "wall street journal", "wsj",
    "financial times", "cnbc", "marketwatch", "barron",
})
_NEWS_PUB_TIER2 = frozenset({
    "seeking alpha", "benzinga", "motley fool", "yahoo finance",
    "business insider", "thestreet", "investopedia",
})
