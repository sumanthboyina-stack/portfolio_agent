"""
Context loader and live-fallback helpers for the APEX reasoning agent.

load_ticker_context(ticker)        — reads all DBs; reports data_gaps
fill_data_gaps(ticker, ctx)        — runs live specialist agents for any missing data
get_macro_snapshot()               — lightweight yfinance pull of key macro indicators (no LLM)
get_full_analysis_context(ticker)  — combined single-call: all DB data + macro + dynamic weights
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Optional


# ── macro snapshot (no LLM, pure yfinance) ────────────────────────────────────

def get_macro_snapshot() -> str:
    """
    Fetch key macro indicators from yfinance and return as JSON string.
    No LLM involved — used by the reasoning agent as lightweight macro context.

    Returns JSON with: vix, treasury_10y, treasury_2y, sp500_1mo_pct,
    yield_curve_spread, macro_regime_hint.
    """
    import yfinance as yf

    def _pct(ticker_sym: str, period: str = "1mo") -> Optional[float]:
        try:
            hist = yf.Ticker(ticker_sym).history(period=period)
            if hist.empty:
                return None
            start, end = hist["Close"].iloc[0], hist["Close"].iloc[-1]
            return round((end - start) / start * 100, 2)
        except Exception:
            return None

    def _last(ticker_sym: str) -> Optional[float]:
        try:
            info = yf.Ticker(ticker_sym).info
            return info.get("regularMarketPrice") or info.get("currentPrice")
        except Exception:
            return None

    vix      = _last("^VIX")
    sp500_1m = _pct("^GSPC", "1mo")
    t10y     = _last("^TNX")   # 10Y yield (%)
    t2y      = _last("^IRX")   # 13-week proxy; real 2Y is ^TWOYEAR but less liquid

    spread = None
    if t10y is not None and t2y is not None:
        spread = round(t10y - t2y, 3)

    # Simple regime heuristic
    if vix and vix > 25:
        regime_hint = "HIGH_VOLATILITY"
    elif sp500_1m and sp500_1m > 3:
        regime_hint = "RISK_ON"
    elif sp500_1m and sp500_1m < -3:
        regime_hint = "RISK_OFF"
    else:
        regime_hint = "NEUTRAL"

    return json.dumps({
        "as_of_date":          date.today().isoformat(),
        "vix":                 vix,
        "treasury_10y_yield":  t10y,
        "treasury_2y_proxy":   t2y,
        "yield_curve_spread":  spread,
        "sp500_1mo_pct":       sp500_1m,
        "macro_regime_hint":   regime_hint,
        "note": (
            "VIX>25=high vol | spread<0=inverted curve | "
            "sp500_1mo_pct>3=risk-on, <-3=risk-off"
        ),
    })


# ── DB readers ────────────────────────────────────────────────────────────────

def _get_recent_news(ticker: str, days: int = 7) -> list[dict]:
    from portfolio_agent.tools.db import db_conn
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    try:
        with db_conn() as conn:
            rows = conn.execute(
                """SELECT date, headline_1, headline_2, sentiment, sentiment_score,
                          top_themes,
                          trending   AS summary,
                          source,
                          model_name
                   FROM news_daily_update
                   WHERE ticker = ? AND date >= ? AND row_type = 'ticker'
                   ORDER BY date DESC""",
                [ticker.upper(), cutoff],
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


# ── public API ────────────────────────────────────────────────────────────────

def load_ticker_context(ticker: str, news_days: int = 7) -> str:
    """
    Load all stored data for *ticker* from the database.

    Returns JSON string with keys:
        ticker, fundamentals, research, news (list, last N days),
        prediction_history (list, last 5), data_gaps (list)
    """
    from portfolio_agent.tools.fundamentals_db import get_stored_fundamentals
    from portfolio_agent.tools.research_db import get_stored_research
    from portfolio_agent.tools.prediction_db import get_prediction_history

    ticker = ticker.upper()
    data_gaps: list[str] = []

    fundamentals = get_stored_fundamentals(ticker)
    if not fundamentals:
        data_gaps.append("fundamentals")

    research = get_stored_research(ticker)
    if not research:
        data_gaps.append("research")

    news = _get_recent_news(ticker, days=news_days)
    if not news:
        data_gaps.append("news")

    prediction_history = get_prediction_history(ticker, limit=5)

    return json.dumps({
        "ticker":             ticker,
        "fundamentals":       fundamentals.to_dict() if fundamentals else None,
        "research":           research.to_dict() if research else None,
        "news":               news,
        "prediction_history": [p.to_dict() for p in prediction_history],
        "data_gaps":          data_gaps,
        "data_gap_note":      (
            "The reasoning agent will attempt to fill gaps by running live specialist "
            "agents before producing its recommendation." if data_gaps else "All data sources available."
        ),
    }, default=str)


def fill_data_gaps(ticker: str, context: dict) -> tuple[dict, list[str]]:
    """
    For each data gap, fetch data directly (no ADK) and store in DB.

    Track A (no LLM): EDGAR / yfinance / Finnhub / news raw fetch.
    Track B (litellm): direct flash-chain call for LLM summarisation.

    Returns (updated_context, list_of_domains_filled).
    """
    import re as _re
    import litellm
    from portfolio_agent._models import FAILOVER_CHAINS
    from portfolio_agent.tools.fundamentals_db import (
        upsert_fundamentals, get_stored_fundamentals,
    )
    from portfolio_agent.tools.research_db import (
        upsert_raw_research, upsert_llm_summary, get_stored_research,
    )
    from portfolio_agent.tools.broker_research import get_broker_research
    from portfolio_agent.tools.news_db import save_ticker_news

    ticker = ticker.upper()
    gaps   = list(context.get("data_gaps", []))
    agents_run: list[str] = []
    flash_chain = FAILOVER_CHAINS.get("flash", [])

    def _llm(prompt: str, max_tokens: int = 1500) -> str:
        for model_id, _prov, _lbl in flash_chain:
            try:
                resp = litellm.completion(
                    model=model_id,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.0,
                    max_tokens=max_tokens,
                )
                return resp.choices[0].message.content or ""
            except Exception:
                continue
        return ""

    def _obj(text: str) -> dict:
        for pat in [r'```json\s*(\{.*?\})\s*```', r'```\s*(\{.*?\})\s*```', r'(\{[\s\S]*\})']:
            m = _re.search(pat, text, _re.DOTALL)
            if m:
                try:
                    return json.loads(m.group(1))
                except Exception:
                    continue
        return {}

    # ── Fundamentals ──────────────────────────────────────────────────────────
    if "fundamentals" in gaps:
        try:
            from portfolio_agent.tools.edgar import get_fundamentals_bundle
            from portfolio_agent.tools.edgar_check import _load_cik_map
            cik_map = _load_cik_map()
            bundle  = get_fundamentals_bundle(ticker, cik_map)
            edgar_ok = not bundle.get("error")
            inc = bundle.get("income", {}) if edgar_ok else {}
            bal = bundle.get("balance", {}) if edgar_ok else {}
            cf  = bundle.get("cashflow", {}) if edgar_ok else {}
            filing = (bundle.get("latest_10k") or {}) if edgar_ok else {}

            if not edgar_ok:
                # yfinance fallback — covers ETFs, foreign listings, tickers not in EDGAR
                try:
                    import yfinance as yf
                    info = yf.Ticker(ticker).info or {}
                    if info.get("regularMarketPrice") or info.get("currentPrice"):
                        inc = {
                            "revenueGrowth":  info.get("revenueGrowth"),
                            "profitMargins":  info.get("profitMargins"),
                            "grossMargins":   info.get("grossMargins"),
                            "operatingMargins": info.get("operatingMargins"),
                        }
                        bal = {"debtToEquity": info.get("debtToEquity")}
                        cf  = {
                            "freeCashflow":    info.get("freeCashflow"),
                            "operatingCashflow": info.get("operatingCashflow"),
                        }
                        edgar_ok = True   # re-use the LLM path below
                except Exception:
                    pass

            if edgar_ok:
                prompt = (
                    f"Fundamental analyst for {ticker}. Compute from the data below:\n"
                    f"  revenue_growth_yoy_pct (float), net_margin (decimal),\n"
                    f"  fcf (operating_cf minus capex, USD int), debt_to_equity (float),\n"
                    f"  fundamental_score (1-10 int), key_strengths (list of 2 strings),\n"
                    f"  key_risks (list of 2 strings), summary (2 sentences).\n"
                    f"Use null for anything uncomputable. Output ONLY a JSON object.\n\n"
                    f"Income: {json.dumps(inc)}\nBalance: {json.dumps(bal)}\nCashFlow: {json.dumps(cf)}\n"
                )
                data = _obj(_llm(prompt))
                if data:
                    upsert_fundamentals(
                        ticker=ticker,
                        as_of_date=date.today().isoformat(),
                        filing_type=filing.get("type", "10-K" if not filing else ""),
                        filing_date=filing.get("date", ""),
                        revenue_growth_yoy_pct=data.get("revenue_growth_yoy_pct"),
                        net_margin=data.get("net_margin"),
                        fcf=data.get("fcf"),
                        debt_to_equity=data.get("debt_to_equity"),
                        fundamental_score=data.get("fundamental_score"),
                        key_strengths=data.get("key_strengths"),
                        key_risks=data.get("key_risks"),
                        summary=data.get("summary", ""),
                        raw_filing_ref="",
                        model_name="flash_chain",
                        model_provider="auto",
                    )
                    context["fundamentals"] = get_stored_fundamentals(ticker)
                    agents_run.append("fundamentals")
        except Exception:
            pass

    # ── Research ──────────────────────────────────────────────────────────────
    if "research" in gaps:
        try:
            d = json.loads(get_broker_research(ticker))
            upsert_raw_research(
                ticker=ticker,
                consensus=d.get("consensus_key", ""),
                consensus_mean=d.get("consensus_mean"),
                num_analysts=d.get("num_analysts"),
                price_target_avg=d.get("target_mean"),
                price_target_median=d.get("target_median"),
                price_target_high=d.get("target_high"),
                price_target_low=d.get("target_low"),
                current_price=d.get("current_price"),
                upside_to_mean_pct=d.get("upside_to_mean_pct"),
                latest_upgrade_date=d.get("latest_upgrade_date") or "",
                recent_upgrades=d.get("recent_upgrades", []),
                quarterly_ratings=d.get("quarterly_ratings", []),
                rec_trend=d.get("rec_trend", []),
                finnhub_pt=d.get("finnhub_pt"),
            )
            rt_lines = "".join(
                f"  {m.get('period','?')}: {m.get('pct_bullish','?')}% bullish "
                f"({m.get('total',0)} analysts)\n"
                for m in (d.get("rec_trend") or [])[:4]
            )
            ru_lines = "".join(
                f"  {u.get('date','?')} {u.get('firm','?')}: "
                f"{u.get('from_grade','?')} → {u.get('to_grade','?')}\n"
                for u in (d.get("recent_upgrades") or [])[:5]
            )
            prompt = (
                f"Sell-side research summary for {ticker}.\n"
                f"Consensus: {d.get('consensus_key','?')} (mean {d.get('consensus_mean','?')}, "
                f"{d.get('num_analysts','?')} analysts)\n"
                f"Price ${d.get('current_price','?')} → Target avg ${d.get('target_mean','?')} "
                f"({d.get('upside_to_mean_pct','?')}% upside)\n"
                f"Monthly trend:\n{rt_lines}"
                f"Recent actions:\n{ru_lines}"
                f"\nReturn ONLY a JSON object: "
                f"highlights (list of 3 strings), research_score (1-10 int), summary (2 sentences)."
            )
            rdata = _obj(_llm(prompt))
            if rdata:
                upsert_llm_summary(
                    ticker=ticker,
                    highlights=rdata.get("highlights", []),
                    research_score=rdata.get("research_score"),
                    summary=rdata.get("summary", ""),
                    model_name="flash_chain",
                    model_provider="auto",
                )
            context["research"] = get_stored_research(ticker)
            agents_run.append("research")
        except Exception:
            pass

    # ── News ──────────────────────────────────────────────────────────────────
    if "news" in gaps:
        try:
            from portfolio_agent.tools.news_sources import get_ticker_news_merged
            articles = json.loads(get_ticker_news_merged(ticker, limit=15, max_age_hours=168))
            if articles:
                headlines = "\n".join(
                    f"- {a.get('title','')} ({a.get('source','')})"
                    for a in articles[:10]
                )
                prompt = (
                    f"Summarize today's news for {ticker}. "
                    f"Return ONLY a JSON object with: "
                    f"headline_1 (str), headline_2 (str), "
                    f"sentiment (POSITIVE|NEUTRAL|NEGATIVE), sentiment_score (float -1 to 1), "
                    f"top_themes (list of up to 3 strings), trending (2-3 sentence narrative).\n\n"
                    f"Articles:\n{headlines}"
                )
                ndata = _obj(_llm(prompt))
                if ndata:
                    save_ticker_news(
                        ticker=ticker,
                        headline_1=ndata.get("headline_1", ""),
                        headline_2=ndata.get("headline_2", ""),
                        sentiment=ndata.get("sentiment", "NEUTRAL"),
                        sentiment_score=float(ndata.get("sentiment_score", 0.0)),
                        top_themes=ndata.get("top_themes", []),
                        trending=ndata.get("trending", ""),
                        source="merged",
                    )
            context["news"] = _get_recent_news(ticker, days=7)
            agents_run.append("news")
        except Exception:
            pass

    context["data_gaps"] = []
    return context, agents_run


# ── Combined single-call context for the reasoning agent ─────────────────────

def get_full_analysis_context(ticker: str) -> str:
    """
    Load ALL data needed by the APEX reasoning agent in a single call:
      1. Stored fundamentals, research, news (7d), prediction history
      2. Live macro snapshot (yfinance, no LLM)
      3. Pre-computed dynamic weights via the weight engine

    The weights are deterministically computed by Python — the LLM must use
    the returned weights verbatim in the synthesis step.

    Returns a single JSON string.
    """
    from portfolio_agent.tools.fundamentals_db import get_stored_fundamentals
    from portfolio_agent.tools.research_db import get_stored_research
    from portfolio_agent.tools.prediction_db import get_prediction_history
    from portfolio_agent.tools.weight_engine import compute_dynamic_weights

    ticker = ticker.upper()

    # ── 1. Load DB data ───────────────────────────────────────────────────────
    fundamentals        = get_stored_fundamentals(ticker)
    research            = get_stored_research(ticker)
    news                = _get_recent_news(ticker, days=7)
    prediction_history  = get_prediction_history(ticker, limit=5)

    data_gaps = []
    if not fundamentals:
        data_gaps.append("fundamentals")
    if not research:
        data_gaps.append("research")
    if not news:
        data_gaps.append("news")

    # ── 2. Macro snapshot (live, no LLM) ─────────────────────────────────────
    macro_snapshot: dict = {}
    try:
        macro_snapshot = json.loads(get_macro_snapshot())
    except Exception:
        pass

    # ── 3. Dynamic weights ────────────────────────────────────────────────────
    weight_data = compute_dynamic_weights(
        ticker=ticker,
        news_data=news,
        research_data=research,
        macro_snapshot=macro_snapshot,
        fundamentals_data=fundamentals,
    )

    # Build score-cap instruction for the LLM
    caps = weight_data.get("data_caps", {})
    cap_lines = [
        f"{domain} ≤ {cap} (no data in DB — analyst must state 'No {domain} data')"
        for domain, cap in caps.items()
        if cap < 10
    ]
    cap_instruction = (
        "SCORE CAPS (mandatory — scores must not exceed these values): "
        + "; ".join(cap_lines)
        if cap_lines else "All data sources populated — no score caps."
    )

    return json.dumps({
        "ticker":             ticker,
        "fundamentals":       fundamentals.to_dict() if fundamentals else None,
        "research":           research.to_dict() if research else None,
        "news":               news,
        "macro_snapshot":     macro_snapshot,
        "prediction_history": [p.to_dict() for p in prediction_history],
        "data_gaps":          data_gaps,
        "dynamic_weights":    weight_data,
        "weight_instruction": (
            f"CRITICAL: Use the exact weights in dynamic_weights.weights. "
            f"Regime: {weight_data['regime']}. {weight_data['weight_summary']}. "
            f"{cap_instruction}"
        ),
    }, default=str)
