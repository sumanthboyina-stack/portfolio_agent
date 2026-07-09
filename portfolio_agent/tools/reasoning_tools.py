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


def _safe_to_dict(obj, label: str = "object") -> Optional[dict]:
    """Call .to_dict() defensively — if obj is already a plain dict, return it as-is."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj
    return obj.to_dict()


# ── macro snapshot (no LLM) ───────────────────────────────────────────────────

def _interpret_rates(fed_funds: Optional[float], prev_fed_funds: Optional[float],
                     implied: str) -> str:
    if fed_funds is None:
        return "Fed policy data unavailable"
    if prev_fed_funds and fed_funds < prev_fed_funds:
        if implied == "cut":
            return "Fed in easing cycle; market pricing continued cuts"
        return "Fed recently cut; market expects pause near-term"
    if prev_fed_funds and fed_funds > prev_fed_funds:
        return "Fed recently hiked; restrictive policy still in effect"
    return "Fed on hold; watching data before next move"


def _interpret_inflation(cpi_yoy: Optional[float], core_yoy: Optional[float],
                         cpi_trend: str) -> str:
    if cpi_yoy is None:
        return "CPI data unavailable"
    if cpi_yoy > 4:
        return f"CPI running hot at {cpi_yoy:.1f}% YoY — well above Fed 2% target"
    if cpi_yoy > 2.5:
        return f"Inflation {cpi_yoy:.1f}% YoY, {cpi_trend} toward target — Fed cautious"
    return f"Inflation {cpi_yoy:.1f}% YoY near target — disinflationary trend intact"


def _interpret_labor(nfp: Optional[float], unemp: Optional[float]) -> str:
    if nfp is None and unemp is None:
        return "Labor data unavailable"
    parts = []
    if nfp is not None:
        if nfp > 200_000:
            parts.append(f"payrolls strong (+{nfp:,.0f})")
        elif nfp > 100_000:
            parts.append(f"payrolls solid (+{nfp:,.0f})")
        else:
            parts.append(f"payrolls softening (+{nfp:,.0f})")
    if unemp is not None:
        if unemp < 4.0:
            parts.append(f"unemployment tight at {unemp:.1f}%")
        elif unemp < 5.0:
            parts.append(f"unemployment moderate at {unemp:.1f}%")
        else:
            parts.append(f"unemployment elevated at {unemp:.1f}%")
    return "; ".join(parts).capitalize() if parts else "Labor market data partial"


def _interpret_risk(vix: Optional[float], hy_spread: Optional[float],
                    sp500_1m: Optional[float]) -> str:
    if vix and vix > 30:
        return f"Risk-off — VIX at {vix:.0f}, elevated fear; credit spreads likely widening"
    if vix and vix > 22:
        return f"Caution — VIX elevated at {vix:.0f}; uncertainty above baseline"
    if hy_spread and hy_spread > 500:
        return f"Credit stress — HY spread at {hy_spread:.0f}bps; risk-off signal"
    if sp500_1m and sp500_1m > 4:
        return f"Risk-on — equities up {sp500_1m:.1f}% past month; sentiment constructive"
    return "Risk environment contained — vol and spreads within normal range"


def get_macro_snapshot() -> str:
    """
    Enriched macro snapshot for the APEX panel.

    Combines yfinance (VIX, S&P) with FRED data (rates, inflation, labor, credit).
    Existing fields are preserved unchanged so the weight engine continues to work.
    New structured blocks are added for the Macro Analyst's reasoning.

    Degrades gracefully — if FRED is unavailable, existing yfinance fields
    remain and the new blocks are omitted or marked as unavailable.
    """
    import yfinance as yf
    from portfolio_agent.macro.fred_client import (
        get_latest_value, get_historical_series, get_yoy_change, get_recent_change,
    )
    from portfolio_agent.macro.calendar import get_upcoming_events, _get_fomc_calendar
    from portfolio_agent.macro.releases import get_recent_surprises, fetch_recent_releases

    # ── yfinance: existing signals ────────────────────────────────────────────
    def _yf_pct(sym: str, period: str = "1mo") -> Optional[float]:
        try:
            hist = yf.Ticker(sym).history(period=period)
            if hist.empty:
                return None
            s, e = hist["Close"].iloc[0], hist["Close"].iloc[-1]
            return round((e - s) / s * 100, 2)
        except Exception:
            return None

    def _yf_last(sym: str) -> Optional[float]:
        try:
            info = yf.Ticker(sym).info
            return info.get("regularMarketPrice") or info.get("currentPrice")
        except Exception:
            return None

    vix      = _yf_last("^VIX")
    sp500_1m = _yf_pct("^GSPC", "1mo")
    sp500_cur = _yf_last("^GSPC")

    # ── FRED: rates ───────────────────────────────────────────────────────────
    t10y_data = get_latest_value("DGS10")
    t2y_data  = get_latest_value("DGS2")
    t3m_data  = get_latest_value("DGS3MO")
    ff_data   = get_latest_value("FEDFUNDS")

    t10y   = t10y_data.get("value") if "error" not in t10y_data else _yf_last("^TNX")
    t2y    = t2y_data.get("value")  if "error" not in t2y_data  else None
    t3m    = t3m_data.get("value")  if "error" not in t3m_data  else None
    ff_cur = ff_data.get("value")   if "error" not in ff_data   else None

    # Prior fed funds (3 months ago) for direction
    ff_hist = get_historical_series("FEDFUNDS", observations=5)
    ff_prev = ff_hist[-3]["value"] if len(ff_hist) >= 3 and "error" not in ff_hist[-1] else None

    spread_10_2  = round(t10y - t2y, 3)  if (t10y and t2y)  else None
    spread_10_3m = round(t10y - t3m, 3)  if (t10y and t3m)  else None

    # ── FRED: inflation ───────────────────────────────────────────────────────
    cpi_yoy      = get_yoy_change("CPIAUCSL")
    core_yoy     = get_yoy_change("CPILFESL")
    pce_yoy      = get_yoy_change("PCEPI")
    cpi_trend    = get_recent_change("CPIAUCSL", periods=3)

    cpi_yoy_val  = cpi_yoy.get("yoy_pct")  if "error" not in cpi_yoy  else None
    core_yoy_val = core_yoy.get("yoy_pct") if "error" not in core_yoy else None
    pce_yoy_val  = pce_yoy.get("yoy_pct")  if "error" not in pce_yoy  else None
    cpi_trend_dir = cpi_trend.get("trend", "unknown") if "error" not in cpi_trend else "unknown"

    cpi_date   = cpi_yoy.get("date")  if "error" not in cpi_yoy  else None
    core_date  = core_yoy.get("date") if "error" not in core_yoy else None

    # ── FRED: labor ───────────────────────────────────────────────────────────
    nfp_latest   = get_latest_value("PAYEMS")
    nfp_prev     = get_historical_series("PAYEMS", observations=3)
    unemp_latest = get_latest_value("UNRATE")
    claims_data  = get_historical_series("ICSA", observations=5)

    nfp_cur      = nfp_latest.get("value")   if "error" not in nfp_latest  else None
    nfp_prior    = nfp_prev[-2]["value"]      if len(nfp_prev) >= 2 and "error" not in nfp_prev[-1] else None
    # PAYEMS is in thousands of persons; delta × 1000 = actual job change
    nfp_mm       = round((nfp_cur - nfp_prior) * 1000) if (nfp_cur is not None and nfp_prior is not None) else None
    unemp        = unemp_latest.get("value")  if "error" not in unemp_latest else None
    nfp_date     = nfp_latest.get("date")     if "error" not in nfp_latest  else None

    claims_4wk: Optional[float] = None
    if len(claims_data) >= 4 and "error" not in claims_data[-1]:
        claims_4wk = round(sum(o["value"] for o in claims_data[-4:]) / 4)

    labor_trend = get_recent_change("PAYEMS", periods=3)
    labor_trend_dir = labor_trend.get("trend", "unknown") if "error" not in labor_trend else "unknown"

    # ── FRED: credit & currency ───────────────────────────────────────────────
    hy_data  = get_latest_value("BAMLH0A0HYM2")
    dxy_data = get_latest_value("DTWEXBGS")
    hy_chg   = get_recent_change("BAMLH0A0HYM2", periods=5)
    dxy_chg  = get_recent_change("DTWEXBGS", periods=5)

    # BAMLH0A0HYM2 is in percent (e.g. 2.63) — convert to basis points (×100)
    _hy_pct   = hy_data.get("value")  if "error" not in hy_data  else None
    hy_spread = round(_hy_pct * 100, 1) if _hy_pct is not None else None
    hy_30d_chg    = None
    if "error" not in hy_chg and hy_spread and hy_chg.get("prior_avg"):
        prior_bps  = round(hy_chg["prior_avg"] * 100, 1)
        hy_30d_chg = round(hy_spread - prior_bps, 1)

    dxy           = dxy_data.get("value") if "error" not in dxy_data else None
    dxy_trend     = dxy_chg.get("trend",  "unknown") if "error" not in dxy_chg else "unknown"
    dxy_30d_pct   = dxy_chg.get("change_pct") if "error" not in dxy_chg else None

    hy_regime = "unknown"
    if hy_spread is not None:
        hy_regime = "tight" if hy_spread < 300 else ("normal" if hy_spread < 500 else "wide")

    # ── Regime logic (enriched) ───────────────────────────────────────────────
    if vix and vix > 25:
        regime_hint = "HIGH_VOLATILITY"
    elif (sp500_1m and sp500_1m < -3) or (hy_spread and hy_spread > 500):
        regime_hint = "RISK_OFF"
    elif sp500_1m and sp500_1m > 3:
        regime_hint = "RISK_ON"
    else:
        regime_hint = "NEUTRAL"

    # ── Market-implied Fed move (simple heuristic) ────────────────────────────
    implied_move = "hold"
    if ff_cur and t3m:
        if t3m < ff_cur - 0.25:
            implied_move = "cut"
        elif t3m > ff_cur + 0.25:
            implied_move = "hike"

    # ── Upcoming events ───────────────────────────────────────────────────────
    upcoming: list[dict] = []
    try:
        upcoming = get_upcoming_events(days_ahead=14)
    except Exception:
        pass

    # ── Recent surprises (from DB; fetch new ones first) ─────────────────────
    surprises: list[dict] = []
    try:
        fetch_recent_releases(lookback_days=30)
        surprises = get_recent_surprises(lookback_days=30)
    except Exception:
        pass

    # ── Next FOMC (look up to 90 days ahead — not limited by the 14-day events window) ──
    _fomc_90d = _get_fomc_calendar(days_ahead=90)
    next_fomc = _fomc_90d[0] if _fomc_90d else None

    # ── Deterministic interpretations ─────────────────────────────────────────
    interpretation = {
        "rates_trajectory": _interpret_rates(ff_cur, ff_prev, implied_move),
        "inflation_state":  _interpret_inflation(cpi_yoy_val, core_yoy_val, cpi_trend_dir),
        "growth_state":     _interpret_labor(nfp_mm, unemp),
        "risk_environment": _interpret_risk(vix, hy_spread, sp500_1m),
    }

    # ── Final snapshot — existing fields preserved, new blocks added ──────────
    snapshot: dict = {
        # ── EXISTING FIELDS (weight engine reads these) ───────────────────────
        "as_of_date":          date.today().isoformat(),
        "vix":                 vix,
        "treasury_10y_yield":  t10y,
        "treasury_2y_proxy":   t2y,           # now FRED DGS2 (was ^IRX)
        "yield_curve_spread":  spread_10_2,
        "sp500_1mo_pct":       sp500_1m,
        "macro_regime_hint":   regime_hint,

        # ── NEW: Fed / rates ──────────────────────────────────────────────────
        "fed": {
            "current_fed_funds":     ff_cur,
            "last_change_direction": (
                "cut" if (ff_prev and ff_cur and ff_cur < ff_prev) else
                "hike" if (ff_prev and ff_cur and ff_cur > ff_prev) else "hold"
            ),
            "next_fomc_date":        next_fomc["date"] if next_fomc else None,
            "days_until_fomc":       next_fomc["days_away"] if next_fomc else None,
            "fomc_detail":           next_fomc.get("detail") if next_fomc else None,
            "market_implied_move":   implied_move,
            "yield_10y":             t10y,
            "yield_2y":              t2y,
            "yield_3m":              t3m,
            "spread_10y_2y":         spread_10_2,
            "spread_10y_3m":         spread_10_3m,
            "curve_inverted":        (spread_10_2 < 0) if spread_10_2 is not None else None,
        },

        # ── NEW: Inflation ────────────────────────────────────────────────────
        "inflation": {
            "cpi_yoy":        cpi_yoy_val,
            "cpi_date":       cpi_date,
            "core_cpi_yoy":   core_yoy_val,
            "pce_yoy":        pce_yoy_val,
            "trend":          cpi_trend_dir,
        },

        # ── NEW: Labor ────────────────────────────────────────────────────────
        "labor": {
            "nfp_latest_mm":       nfp_mm,
            "nfp_date":            nfp_date,
            "unemployment_rate":   unemp,
            "jobless_claims_4wk":  claims_4wk,
            "trend":               labor_trend_dir,
        },

        # ── NEW: Credit ───────────────────────────────────────────────────────
        "credit": {
            "hy_spread_bps":       hy_spread,
            "hy_30d_change_bps":   hy_30d_chg,
            "regime":              hy_regime,
        },

        # ── NEW: Currency ─────────────────────────────────────────────────────
        "currency": {
            "dxy":              dxy,
            "dxy_30d_chg_pct":  dxy_30d_pct,
            "trend":            dxy_trend,
        },

        # ── NEW: Recent macro surprises ───────────────────────────────────────
        "recent_surprises": [
            {
                "event":        s["event_name"],
                "date":         s["released_at"],
                "period":       s.get("period_label"),
                "actual":       s["actual_value"],
                "prior":        s["prior_value"],
                "yoy_change":   s.get("yoy_change"),
                "severity":     s["surprise_severity"],
            }
            for s in surprises
        ],

        # ── NEW: Upcoming events ──────────────────────────────────────────────
        "upcoming_events": [
            {
                "event":      e["event"],
                "date":       e["date"],
                "days_away":  e["days_away"],
                "importance": e["importance"],
                "detail":     e.get("detail", ""),
            }
            for e in upcoming[:5]
        ],

        # ── NEW: Plain-English interpretations (deterministic, no LLM) ───────
        "interpretation": interpretation,
    }

    return json.dumps(snapshot, default=str)


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
        "fundamentals":       _safe_to_dict(fundamentals),
        "research":           _safe_to_dict(research),
        "news":               news,
        "prediction_history": [_safe_to_dict(p) for p in prediction_history],
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

def get_full_analysis_context(
    ticker: str,
    horizons: list[int] | None = None,
) -> str:
    """
    Load ALL data needed by the APEX reasoning agent in a single call:
      1. Stored fundamentals, research, news (7d), prediction history
      2. Live macro snapshot (yfinance, no LLM)
      3. Pre-computed dynamic weights — one set per scheduled horizon

    horizons: the horizon_days list for today's run (e.g. [5], [21], [5, 21, 63]).
    Defaults to [5, 21, 63] when called from the interactive chat path.

    The weights are deterministically computed by Python — the LLM must use
    the returned weights verbatim in the synthesis step.

    Returns a single JSON string.
    """
    from portfolio_agent.tools.fundamentals_db import get_stored_fundamentals
    from portfolio_agent.tools.research_db import get_stored_research
    from portfolio_agent.tools.prediction_db import get_prediction_history
    from portfolio_agent.tools.weight_engine import compute_dynamic_weights

    if horizons is None:
        horizons = [5, 21, 63]

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

    # ── 3. Dynamic weights — per horizon + shared signal context ─────────────
    weight_data = compute_dynamic_weights(
        ticker=ticker,
        news_data=news,
        research_data=research,
        macro_snapshot=macro_snapshot,
        fundamentals_data=fundamentals,
        horizons=horizons,
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

    # Per-horizon weight summaries for the weight instruction
    wbh = weight_data.get("weights_by_horizon") or {}

    def _pct(v: float) -> str:
        return f"{round(v * 100)}%"

    horizon_weight_lines = [
        f"  {h}d: News {_pct(w['news'])} · Research {_pct(w['research'])} · "
        f"Macro {_pct(w['macro'])} · Fundamentals {_pct(w['fundamentals'])}"
        for h, w in sorted(wbh.items())
    ]
    horizon_weight_str = "\n".join(horizon_weight_lines) if horizon_weight_lines else "(none)"

    return json.dumps({
        "ticker":             ticker,
        "fundamentals":       _safe_to_dict(fundamentals),
        "research":           _safe_to_dict(research),
        "news":               news,
        "macro_snapshot":     macro_snapshot,
        "prediction_history": [_safe_to_dict(p) for p in prediction_history],
        "data_gaps":          data_gaps,
        "dynamic_weights":    weight_data,
        "weight_instruction": (
            f"CRITICAL: Use the exact per-horizon weights from "
            f"dynamic_weights.weights_by_horizon for each horizon's composite score. "
            f"Regime: {weight_data['regime']}.\n"
            f"Per-horizon weights:\n{horizon_weight_str}\n"
            f"{cap_instruction}"
        ),
    }, default=str)
