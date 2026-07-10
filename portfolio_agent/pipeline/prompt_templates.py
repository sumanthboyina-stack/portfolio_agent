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
  fundamental_score       — 1–10 integer (10 = strongest; weight growth, margins, leverage, CF quality, AND guidance)
  key_strengths           — list of exactly 2 specific strengths observed in the numbers
  key_risks               — list of exactly 2 specific risks observed in the numbers
  summary                 — 2 sentence narrative
  guidance_direction      — "raised" | "maintained" | "lowered" | "withdrawn" | "none"

Guidance scoring rule (applies when "Latest 8-K Guidance/Outlook" is present):
  Read the guidance text and determine whether management raised, maintained, or lowered forward
  guidance relative to prior expectations implied by the text (e.g. "above consensus", "in-line",
  "below prior range"). Set guidance_direction accordingly. Then factor this into fundamental_score:
  - raised + cited demand/pricing strength → add 1–2 points above what trailing numbers alone suggest
  - maintained with conservative tone        → neutral effect on score
  - lowered or withdrawn                     → subtract 1–2 points
  A strong guidance raise from a company with mediocre TTM numbers still warrants a higher
  fundamental_score than the trailing data alone would justify. When no guidance text is provided,
  set guidance_direction to "none" and score purely on the financial data.

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
  highlights      — list of exactly 3 specific, data-driven observations (e.g. consensus trend, PT gap vs current price, notable upgrades/downgrades, Finnhub trend direction, short interest context if meaningful)
  research_score  — 1–10 integer (10 = strongest buy conviction; weight consensus mean, upside to target, recent upgrade bias, monthly trend direction, and short interest signal)
  summary         — 2 sentence narrative covering overall analyst stance, key near-term catalyst, and short interest context when significant

Short interest scoring guidance (apply when "Short interest" line is present):
  - Rising short interest + high days-to-cover (≥ 5): bearish signal; lower research_score by 1 point unless strong upgrade momentum offsets it
  - Falling short interest: mild bullish confirmation; can support a 1-point boost when consensus is already positive
  - High squeeze pressure (days-to-cover ≥ 10): noteworthy in highlights — elevated short covering risk is a catalyst
  - Stable / low: neutral; do not adjust score

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

def _build_macro_section(macro: dict) -> str:
    """
    Render the enriched macro snapshot as a formatted, readable block for the Macro Analyst.
    Falls back gracefully if any sub-block is missing.
    """
    if not macro:
        return "(macro data unavailable)"

    lines: list[str] = []

    # ── Rates & Fed policy ────────────────────────────────────────────────────
    fed = macro.get("fed") or {}
    ff  = fed.get("current_fed_funds")
    t10 = fed.get("yield_10y")
    t2  = fed.get("yield_2y")
    t3m = fed.get("yield_3m")
    sp_10_2  = fed.get("spread_10y_2y")
    inverted = fed.get("curve_inverted")
    fomc_dt  = fed.get("next_fomc_date")
    fomc_da  = fed.get("days_until_fomc")
    fomc_det = fed.get("fomc_detail", "")
    imp_move = fed.get("market_implied_move", "")
    last_chg = fed.get("last_change_direction", "")

    ff_str    = f"{ff:.2f}%" if ff else "n/a"
    t10_str   = f"{t10:.2f}%" if t10 else "n/a"
    t2_str    = f"{t2:.2f}%" if t2  else "n/a"
    t3m_str   = f"{t3m:.2f}%" if t3m else "n/a"
    sp_str    = (
        f"{sp_10_2:+.2f}% {'(INVERTED ⚠)' if inverted else '(positive)'}"
        if sp_10_2 is not None else "n/a"
    )
    fomc_str  = (
        f"{fomc_dt} (in {fomc_da}d){' — ' + fomc_det if fomc_det else ''}"
        if fomc_dt else "n/a"
    )

    lines.append("RATES & FED POLICY")
    lines.append(
        f"  Fed Funds: {ff_str}  |  Last move: {last_chg or 'hold'}  |  "
        f"Next FOMC: {fomc_str}"
    )
    lines.append(f"  Yields: 10Y {t10_str} | 2Y {t2_str} | 3M {t3m_str}")
    lines.append(f"  10Y-2Y Spread: {sp_str}  |  Market-implied next move: {imp_move or 'hold'}")
    lines.append("")

    # ── Inflation ─────────────────────────────────────────────────────────────
    inf = macro.get("inflation") or {}
    cpi  = inf.get("cpi_yoy")
    core = inf.get("core_cpi_yoy")
    pce  = inf.get("pce_yoy")
    cpi_dt = inf.get("cpi_date", "")
    trend  = inf.get("trend", "")

    lines.append("INFLATION")
    lines.append(
        f"  CPI YoY: {f'{cpi:.1f}%' if cpi else 'n/a'} ({cpi_dt})  |  "
        f"Core CPI: {f'{core:.1f}%' if core else 'n/a'}  |  "
        f"PCE: {f'{pce:.1f}%' if pce else 'n/a'}  |  Trend: {trend or 'unknown'}"
    )
    lines.append("")

    # ── Labor ─────────────────────────────────────────────────────────────────
    lab = macro.get("labor") or {}
    nfp    = lab.get("nfp_latest_mm")
    nfp_dt = lab.get("nfp_date", "")
    unemp  = lab.get("unemployment_rate")
    claims = lab.get("jobless_claims_4wk")
    l_trend = lab.get("trend", "")

    lines.append("LABOR MARKET")
    lines.append(
        f"  NFP: {f'+{nfp:,.0f}' if nfp and nfp >= 0 else (f'{nfp:,.0f}' if nfp else 'n/a')} ({nfp_dt})  |  "
        f"Unemployment: {f'{unemp:.1f}%' if unemp else 'n/a'}  |  "
        f"Jobless claims (4-wk avg): {f'{claims:,.0f}' if claims else 'n/a'}  |  "
        f"Trend: {l_trend or 'unknown'}"
    )
    lines.append("")

    # ── Credit & risk ─────────────────────────────────────────────────────────
    cred = macro.get("credit") or {}
    hy      = cred.get("hy_spread_bps")
    hy_chg  = cred.get("hy_30d_change_bps")
    hy_reg  = cred.get("regime", "")
    vix     = macro.get("vix")

    lines.append("CREDIT & RISK")
    lines.append(
        f"  HY spread: {f'{hy:.0f}bps ({hy_reg})' if hy else 'n/a'}  |  "
        f"30d change: {f'{hy_chg:+.0f}bps' if hy_chg is not None else 'n/a'}  |  "
        f"VIX: {f'{vix:.1f}' if vix else 'n/a'}"
    )
    lines.append("")

    # ── Currency ──────────────────────────────────────────────────────────────
    cur = macro.get("currency") or {}
    dxy      = cur.get("dxy")
    dxy_pct  = cur.get("dxy_30d_chg_pct")
    dxy_tr   = cur.get("trend", "")

    lines.append("CURRENCY (USD)")
    lines.append(
        f"  DXY: {f'{dxy:.1f}' if dxy else 'n/a'}  |  "
        f"30d change: {f'{dxy_pct:+.1f}%' if dxy_pct is not None else 'n/a'}  |  "
        f"Trend: {dxy_tr or 'unknown'}"
    )
    lines.append("")

    # ── Recent macro surprises ────────────────────────────────────────────────
    surprises = macro.get("recent_surprises") or []
    if surprises:
        lines.append("RECENT MACRO SURPRISES (last 30 days)")
        sev_label = {0: "no surprise", 1: "minor", 2: "moderate", 3: "major surprise ⚠"}
        for s in surprises[:5]:
            ev    = s.get("event", "?")
            dt    = s.get("date", "")
            per   = s.get("period") or ""
            act   = s.get("actual")
            prior = s.get("prior")
            sev   = s.get("severity", 0)
            yoy   = s.get("yoy_change")
            label = sev_label.get(sev, str(sev))

            act_str   = f"{act:,.2f}" if act is not None else "n/a"
            prior_str = f"{prior:,.2f}" if prior is not None else "n/a"
            yoy_str   = f"  YoY: {yoy:+.1f}%" if yoy is not None else ""
            lines.append(
                f"  • {ev} ({per or dt}): actual {act_str} vs prior {prior_str}{yoy_str}"
                f"  [{label}]"
            )
        lines.append("")

    # ── Upcoming events ───────────────────────────────────────────────────────
    upcoming = macro.get("upcoming_events") or []
    if upcoming:
        lines.append("UPCOMING EVENTS (next 14 days)")
        for e in upcoming[:5]:
            ev  = e.get("event", "?")
            dt  = e.get("date", "")
            da  = e.get("days_away")
            det = e.get("detail", "")
            da_str = f"in {da}d" if da is not None else ""
            lines.append(
                f"  • {ev} — {dt} ({da_str}){('  [' + det + ']') if det else ''}"
            )
        lines.append("")

    # ── Interpretation ────────────────────────────────────────────────────────
    interp = macro.get("interpretation") or {}
    if interp:
        lines.append("MACRO INTERPRETATION (deterministic — no LLM)")
        for key, text in interp.items():
            label = key.replace("_", " ").title()
            lines.append(f"  → {label}: {text}")

    return "\n".join(lines)


_APEX_PROMPT_TEMPLATE = """\
You are APEX (Adaptive Portfolio EXpert), a multi-perspective investment reasoning system.

Ticker  : {ticker}
Question: Daily portfolio review — provide investment recommendation.

=== MACRO ENVIRONMENT (Macro Analyst's domain) ===
{macro_section}
=== END MACRO ===

=== FULL ANALYSIS CONTEXT (pre-loaded) ===
{ctx_json}
=== END CONTEXT ===

Using the context above (fundamentals, research, news, macro, dynamic_weights, prediction_history):

1. State the weight regime and signal summary (from dynamic_weights.regime and dynamic_weights.signal_strengths).
2. Have each analyst score their domain 1-10: FUNDAMENTAL ANALYST, RESEARCH ANALYST,
   MACRO ANALYST, NEWS ANALYST.
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
    "chen_verdict": "BULLISH (7/10) — Fundamental Analyst one sentence",
    "webb_verdict": "BULLISH (7/10) — Research Analyst one sentence",
    "varga_verdict": "NEUTRAL (6/10) — Macro Analyst one sentence",
    "park_verdict":  "NEUTRAL (6/10) — News Analyst one sentence",
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
