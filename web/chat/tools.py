"""
Chat tool definitions + implementations.

Contains:
  - _search_ticker_by_name   : yfinance company-name lookup
  - _CHAT_TOOLS              : tool schema list for LiteLLM
  - _chat_tool_*             : one function per tool
  - _execute_chat_tool()     : dispatcher called by orchestration
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))


# ── Company-name → ticker lookup ──────────────────────────────────────────────

def _search_ticker_by_name(text: str) -> tuple[str, str] | tuple[None, None]:
    """
    Resolve a free-text company name to a ticker via yfinance Search.
    Returns (ticker, display_name) or (None, None) if nothing found.
    Only matches EQUITY quotes on major exchanges.
    """
    try:
        import yfinance as yf
        results = yf.Search(text, max_results=5).quotes
        for r in results:
            if r.get("quoteType") == "EQUITY" and r.get("symbol"):
                return r["symbol"], r.get("longname") or r.get("shortname") or r["symbol"]
    except Exception:
        pass
    return None, None


# ── Tool schema list ──────────────────────────────────────────────────────────

_CHAT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web for current financial news, events, data, or facts. "
                "Use this as a FALLBACK when internal tools (get_market_news, get_ticker_news, "
                "get_macro_snapshot, etc.) return no relevant information, or when the question "
                "requires real-time data that may not be in the local database -- "
                "e.g. breaking news, recent earnings, new IPO filings, policy changes, "
                "live price moves, or any event from the last few days."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query. Be specific -- include company name, ticker, date, or event type.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Number of results to return (default 5, max 10).",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_ticker",
            "description": "Look up the stock ticker symbol for a company by name. Use this when the user mentions a company name and you need to find its ticker before calling other tools.",
            "parameters": {
                "type": "object",
                "properties": {
                    "company_name": {
                        "type": "string",
                        "description": "Company name or search query, e.g. 'Duolingo', 'Palantir Technologies'",
                    }
                },
                "required": ["company_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_ticker_news",
            "description": "Get recent news headlines, sentiment, and themes for a specific stock ticker (7-day lookback from DB + live fallback).",
            "parameters": {
                "type": "object",
                "properties": {"ticker": {"type": "string", "description": "Stock ticker symbol"}},
                "required": ["ticker"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_market_news",
            "description": "Get the latest general financial/market news headlines from Reuters, Yahoo Finance, CNBC, MarketWatch. Use for questions about market trends, economy, macro events, IPOs, M&A, or any broad financial news.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Number of headlines to return (default 20, max 30)",
                        "default": 20,
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_ipo_info",
            "description": "Get information about recent or upcoming IPOs, or company listing details for a given ticker. Use for IPO-related questions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {
                        "type": "string",
                        "description": "Optional ticker of a recently IPO'd company. Leave empty for a general recent IPO list.",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_sector_performance",
            "description": "Get performance data for major market sectors (Technology, Healthcare, Finance, Energy, etc.) and broad indices (S&P 500, Nasdaq, Dow Jones). Use for sector rotation questions or broad market performance.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_fundamentals",
            "description": "Get financial fundamentals for a specific stock: revenue growth, net margin, FCF, debt/equity, fundamental score, key strengths and risks.",
            "parameters": {
                "type": "object",
                "properties": {"ticker": {"type": "string"}},
                "required": ["ticker"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_research",
            "description": "Get broker/analyst research for a specific stock: consensus rating, price targets (mean/median/high/low), recent upgrades and downgrades.",
            "parameters": {
                "type": "object",
                "properties": {"ticker": {"type": "string"}},
                "required": ["ticker"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_macro_snapshot",
            "description": "Get current macro environment: VIX, 10-year treasury yield, S&P 500 1-month trend, yield curve spread, macro regime.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_prediction_history",
            "description": "Get APEX prediction history for a ticker (last 10 predictions with scores and reasoning).",
            "parameters": {
                "type": "object",
                "properties": {"ticker": {"type": "string"}},
                "required": ["ticker"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_prediction_accuracy",
            "description": (
                "Get APEX's validated track record for a specific ticker: how many past "
                "predictions have matured and been scored against actual price moves, the "
                "directional accuracy %, average Brier score, and the most recent scored "
                "outcome -- broken down by horizon (5d/21d/63d/250d). Use this whenever a "
                "user asks whether to trust a recommendation for a ticker, or how accurate "
                "past predictions have been -- it's the 'validations' layer, separate from "
                "the recommendation itself (get_prediction_history)."
            ),
            "parameters": {
                "type": "object",
                "properties": {"ticker": {"type": "string"}},
                "required": ["ticker"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_latest_opportunities",
            "description": (
                "Query the local database for the latest trending/new investment opportunities "
                "discovered by the pipeline. These are tickers analyzed as 'trending_opportunity' "
                "with BUY or STRONG_BUY signals. Use this FIRST when the user asks about: "
                "new opportunities, trending stocks, what stocks look good, latest discoveries, "
                "what should I invest in, top picks, opportunity stocks, trending investments."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "min_confidence": {
                        "type": "integer",
                        "description": "Minimum confidence score (1-10, default 6)",
                        "default": 6,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results to return (default 10)",
                        "default": 10,
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_portfolio_summary",
            "description": (
                "Get current portfolio holdings from the local database: tickers, shares, "
                "current value, cost basis, weight%, and latest APEX recommendation for each. "
                "Use when user asks about their portfolio, what they own, their holdings, "
                "portfolio performance, or portfolio composition."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_predictions_summary",
            "description": (
                "Get the latest APEX predictions for all tickers in the database — both portfolio "
                "holdings and any trending tickers analyzed today. Returns recommendation, "
                "confidence, composite score, and scores breakdown per ticker. "
                "Use when user asks: what are today's predictions, latest signals, "
                "what does APEX say, summary of all recommendations, or any broad question "
                "about the system's current views."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "segment": {
                        "type": "string",
                        "description": "Filter: 'all' (default), 'portfolio', or 'opportunities'",
                        "default": "all",
                    },
                    "recommendation": {
                        "type": "string",
                        "description": "Optional filter: 'BUY', 'SELL', 'HOLD', 'STRONG_BUY', 'STRONG_SELL'",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_undervalued_opportunities",
            "description": (
                "Screen the local database for tickers trading below analyst price targets "
                "with a bullish APEX signal -- i.e. 'undervalued but the outlook is strong.' "
                "Joins each ticker's latest prediction (recommendation, composite score) "
                "against its stored analyst price target to compute upside % to target, "
                "sorted by upside descending. Use this FIRST for questions like "
                "'what's the most opportunistic stock trading below where it should be', "
                "'which stock has the most upside to its price target', or any screen "
                "combining valuation gap with analyst/APEX conviction -- do not run a "
                "single-ticker analysis for this kind of broad screening question."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "min_upside_pct": {
                        "type": "number",
                        "description": "Minimum upside % to analyst target to include (default 5)",
                        "default": 5,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results to return (default 10)",
                        "default": 10,
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_price_history",
            "description": "Get price performance for a specific stock: latest close, period change %, 52-week high/low, average volume.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "period": {
                        "type": "string",
                        "description": "Time period: 1mo, 3mo, 6mo, 1y, 2y",
                        "default": "3mo",
                    },
                },
                "required": ["ticker"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_technical_snapshot",
            "description": (
                "Get the current technical setup for a stock: SMA20/50/200, RSI14, MACD, "
                "Bollinger Bands, ATR14, and price vs SMA50 %. Use for questions about "
                "momentum, trend, overbought/oversold, moving averages, or 'technical setup' "
                "on a specific ticker."
            ),
            "parameters": {
                "type": "object",
                "properties": {"ticker": {"type": "string"}},
                "required": ["ticker"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_portfolio_sector_allocation",
            "description": (
                "Get the current portfolio's exposure broken down by sector -- % of "
                "portfolio value and the tickers in each sector. Use when the user asks "
                "about sector allocation, diversification, or 'what sectors am I in.'"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_portfolio_risk_flags",
            "description": (
                "Get the daily Risk/Technical specialists' flags for portfolio holdings -- "
                "correlation vs the portfolio, sector/issuer concentration %, beta vs SPY, "
                "and bearish-momentum warnings. Pass a ticker to get that position's flags "
                "specifically (active or not); omit it to get every currently-active "
                "('flag=1') warning across the whole portfolio, same as the dashboard's "
                "Attention Queue. Use for questions about concentration risk, correlation "
                "between holdings, or whether a position is flagged."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {
                        "type": "string",
                        "description": "Optional -- restrict to this ticker's flags.",
                    }
                },
            },
        },
    },
]


# ── Tool implementations ───────────────────────────────────────────────────────

def _chat_tool_get_news(ticker: str) -> dict:
    from portfolio_agent.tools.reasoning_tools import _get_recent_news
    news = _get_recent_news(ticker.upper(), days=7)
    if news:
        return {"source": "db", "ticker": ticker.upper(), "news": news}
    try:
        from portfolio_agent.tools.news_sources import get_ticker_news_merged
        arts = json.loads(get_ticker_news_merged(ticker, limit=12, max_age_hours=168))
        return {"source": "live", "ticker": ticker.upper(),
                "articles": arts.get("articles", [])[:8]}
    except Exception:
        return {"ticker": ticker.upper(), "note": "No news data available in the last 7 days."}


def _chat_tool_get_fundamentals(ticker: str) -> dict:
    from portfolio_agent.tools.fundamentals_db import get_stored_fundamentals
    data = get_stored_fundamentals(ticker.upper())
    if data:
        return {"source": "db", "ticker": ticker.upper(), "fundamentals": data}
    return {"ticker": ticker.upper(),
            "note": "No stored fundamentals. Run a full analysis to populate."}


def _chat_tool_get_research(ticker: str) -> dict:
    from portfolio_agent.tools.research_db import get_stored_research
    data = get_stored_research(ticker.upper())
    if data and (data.get("as_of_date") or data.get("raw_fetched_at")):
        return {"source": "db", "ticker": ticker.upper(), "research": data}
    try:
        from portfolio_agent.tools.broker_research import get_broker_research
        d = json.loads(get_broker_research(ticker))
        return {"source": "live", "ticker": ticker.upper(), "research": d}
    except Exception:
        return {"ticker": ticker.upper(), "note": "Research data unavailable."}


def _chat_tool_get_macro() -> dict:
    from portfolio_agent.tools.reasoning_tools import get_macro_snapshot
    try:
        return json.loads(get_macro_snapshot())
    except Exception:
        return {"note": "Macro data temporarily unavailable."}


def _chat_tool_get_predictions(ticker: str) -> dict:
    from portfolio_agent.tools.prediction_db import get_prediction_history
    hist = get_prediction_history(ticker.upper(), limit=10)
    return {"ticker": ticker.upper(), "prediction_history": hist}


def _chat_tool_get_prediction_accuracy(ticker: str) -> dict:
    db_path = _ROOT / "data" / "portfolio.db"
    if not db_path.exists():
        return {"ticker": ticker.upper(), "note": "Database not found."}
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT horizon_days, outcome, outcome_score, brier_score,
                   actual_direction, actual_return, evaluated_at, recommendation
            FROM predictions
            WHERE ticker = ? AND evaluation_status = 'evaluated'
            ORDER BY evaluated_at DESC
        """, [ticker.upper()]).fetchall()
        conn.close()
        rows = [dict(r) for r in rows]
        if not rows:
            return {
                "ticker": ticker.upper(), "evaluated_count": 0,
                "note": (
                    "No matured/scored predictions yet for this ticker -- either it's new "
                    "to the system or its predictions haven't reached their evaluation date."
                ),
            }
        _correct_labels = {"strong_correct", "directionally_correct", "flat_correct"}
        by_horizon: dict[int, dict] = {}
        for r in rows:
            h = r.get("horizon_days")
            b = by_horizon.setdefault(h, {"horizon_days": h, "count": 0, "correct": 0, "briers": []})
            b["count"] += 1
            if r.get("outcome") in _correct_labels:
                b["correct"] += 1
            if r.get("brier_score") is not None:
                b["briers"].append(r["brier_score"])
        horizon_summary = []
        for h, b in sorted(by_horizon.items(), key=lambda kv: (kv[0] is None, kv[0])):
            horizon_summary.append({
                "horizon_days": h,
                "evaluated_count": b["count"],
                "directional_accuracy_pct": round(b["correct"] / b["count"] * 100, 1),
                "avg_brier_score": round(sum(b["briers"]) / len(b["briers"]), 3) if b["briers"] else None,
            })
        latest = rows[0]
        return {
            "ticker": ticker.upper(),
            "evaluated_count": len(rows),
            "overall_directional_accuracy_pct": round(
                sum(1 for r in rows if r.get("outcome") in _correct_labels) / len(rows) * 100, 1
            ),
            "by_horizon": horizon_summary,
            "most_recent_outcome": {
                "horizon_days": latest.get("horizon_days"),
                "recommendation_at_time": latest.get("recommendation"),
                "outcome": latest.get("outcome"),
                "actual_direction": latest.get("actual_direction"),
                "actual_return_pct": round((latest.get("actual_return") or 0) * 100, 2),
                "evaluated_at": (latest.get("evaluated_at") or "")[:10],
            },
        }
    except Exception as exc:
        return {"ticker": ticker.upper(), "note": f"DB query failed: {exc}"}


def _chat_tool_get_price_history(ticker: str, period: str = "3mo") -> dict:
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period=period)
        if hist.empty:
            return {"ticker": ticker.upper(), "note": "No price data available."}
        yr = yf.Ticker(ticker).history(period="1y")
        return {
            "ticker":       ticker.upper(),
            "period":       period,
            "latest_close": round(float(hist["Close"].iloc[-1]), 2),
            "period_start": round(float(hist["Close"].iloc[0]), 2),
            "change_pct":   round((hist["Close"].iloc[-1] / hist["Close"].iloc[0] - 1) * 100, 2),
            "period_high":  round(float(hist["High"].max()), 2),
            "period_low":   round(float(hist["Low"].min()), 2),
            "52w_high":     round(float(yr["High"].max()), 2) if not yr.empty else None,
            "52w_low":      round(float(yr["Low"].min()), 2) if not yr.empty else None,
            "avg_volume":   int(hist["Volume"].mean()),
        }
    except Exception as exc:
        return {"ticker": ticker.upper(), "note": f"Price data unavailable: {exc}"}


def _chat_tool_get_market_news(limit: int = 20) -> dict:
    try:
        from portfolio_agent.tools.news_db import get_todays_market_news
        db_result = json.loads(get_todays_market_news())
        if db_result.get("count", 0) >= 5:
            return {"source": "db", **db_result}
    except Exception:
        pass
    try:
        from portfolio_agent.tools.news_sources import get_market_news
        return {"source": "live", **json.loads(get_market_news(limit=min(limit, 30)))}
    except Exception as exc:
        return {"note": f"Market news temporarily unavailable: {exc}"}


def _chat_tool_get_ipo_info(ticker: str = "") -> dict:
    try:
        import yfinance as yf
        if ticker:
            t = yf.Ticker(ticker.upper())
            info = t.info or {}
            return {
                "ticker": ticker.upper(),
                "company_name": info.get("longName"),
                "sector": info.get("sector"),
                "industry": info.get("industry"),
                "ipo_date": None,
                "market_cap": info.get("marketCap"),
                "shares_outstanding": info.get("sharesOutstanding"),
                "float_shares": info.get("floatShares"),
                "current_price": info.get("currentPrice") or info.get("regularMarketPrice"),
                "52w_high": info.get("fiftyTwoWeekHigh"),
                "52w_low": info.get("fiftyTwoWeekLow"),
                "exchange": info.get("exchange"),
                "description": (info.get("longBusinessSummary") or "")[:400],
            }
        from portfolio_agent.tools.news_sources import get_market_news
        raw = json.loads(get_market_news(limit=30))
        ipo_articles = [
            a for a in raw.get("articles", [])
            if any(kw in (a.get("title", "") + a.get("summary", "")).lower()
                   for kw in ("ipo", "initial public offering", "goes public", "listing", "debut"))
        ]
        return {"source": "rss", "ipo_news": ipo_articles[:10],
                "note": "Filtered IPO-related headlines from financial RSS feeds."}
    except Exception as exc:
        return {"note": f"IPO data unavailable: {exc}"}


def _chat_tool_get_sector_performance() -> dict:
    try:
        import yfinance as yf
        symbols = {
            "S&P 500": "^GSPC", "Nasdaq": "^IXIC", "Dow Jones": "^DJI",
            "Russell 2000": "^RUT", "Technology (XLK)": "XLK", "Healthcare (XLV)": "XLV",
            "Financials (XLF)": "XLF", "Energy (XLE)": "XLE", "Consumer Disc (XLY)": "XLY",
            "Consumer Staples (XLP)": "XLP", "Industrials (XLI)": "XLI",
            "Materials (XLB)": "XLB", "Real Estate (XLRE)": "XLRE",
            "Utilities (XLU)": "XLU", "Communication (XLC)": "XLC",
        }
        results = {}
        tickers_str = " ".join(symbols.values())
        data = yf.download(tickers_str, period="1mo", auto_adjust=True, progress=False)
        closes = data["Close"] if "Close" in data.columns else data
        for name, sym in symbols.items():
            try:
                col = closes[sym] if sym in closes.columns else None
                if col is not None and len(col.dropna()) >= 2:
                    start = float(col.dropna().iloc[0])
                    end   = float(col.dropna().iloc[-1])
                    results[name] = {"symbol": sym, "latest": round(end, 2),
                                     "1mo_change_pct": round((end / start - 1) * 100, 2)}
            except Exception:
                continue
        return {"period": "1 month", "sectors": results}
    except Exception as exc:
        return {"note": f"Sector data unavailable: {exc}"}


def _chat_tool_web_search(query: str, max_results: int = 5) -> dict:
    try:
        from ddgs import DDGS
        results = DDGS().text(query, max_results=min(max_results, 10))
        if not results:
            return {"query": query, "results": [], "note": "No results found."}
        return {
            "query": query,
            "results": [
                {"title": r.get("title", ""), "url": r.get("href", ""),
                 "snippet": r.get("body", "")[:400]}
                for r in results
            ],
        }
    except Exception as exc:
        return {"query": query, "error": str(exc), "note": "Web search unavailable."}


def _chat_tool_search_ticker(company_name: str) -> dict:
    ticker, display = _search_ticker_by_name(company_name)
    if ticker:
        return {"company": company_name, "ticker": ticker, "name": display,
                "note": f"Use '{ticker}' as the ticker symbol for subsequent tool calls."}
    return {"company": company_name, "ticker": None,
            "note": "Could not find a ticker for this company. Ask the user to provide the ticker symbol."}


def _chat_tool_get_latest_opportunities(min_confidence: int = 6, limit: int = 10) -> dict:
    db_path = _ROOT / "data" / "portfolio.db"
    if not db_path.exists():
        return {"note": "Database not found. Run the pipeline first to populate data."}
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT p.ticker, p.recommendation, p.confidence, p.composite_score,
                   p.news_score, p.research_score, p.fundamental_score,
                   p.reasoning, p.created_at
            FROM predictions p
            JOIN (
                SELECT ticker, MAX(created_at) dt
                FROM predictions WHERE trigger_type='trending_opportunity'
                GROUP BY ticker
            ) x ON p.ticker=x.ticker AND p.created_at=x.dt
            WHERE p.trigger_type='trending_opportunity'
              AND p.recommendation IN ('BUY','STRONG_BUY')
              AND COALESCE(p.confidence, 0) >= ?
            ORDER BY p.composite_score DESC NULLS LAST
            LIMIT ?
        """, [min_confidence, limit]).fetchall()
        conn.close()
        opportunities = [dict(r) for r in rows]
        if not opportunities:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT p.ticker, p.recommendation, p.confidence, p.composite_score,
                       p.news_score, p.research_score, p.fundamental_score,
                       p.reasoning, p.created_at
                FROM predictions p
                JOIN (
                    SELECT ticker, MAX(created_at) dt
                    FROM predictions WHERE trigger_type='trending_opportunity'
                    GROUP BY ticker
                ) x ON p.ticker=x.ticker AND p.created_at=x.dt
                WHERE p.trigger_type='trending_opportunity'
                ORDER BY p.created_at DESC, p.composite_score DESC NULLS LAST
                LIMIT ?
            """, [limit]).fetchall()
            conn.close()
            opportunities = [dict(r) for r in rows]
            if not opportunities:
                return {
                    "source": "db", "count": 0,
                    "note": (
                        "No trending opportunity predictions found yet. "
                        "The pipeline discovers trending tickers during intraday and morning "
                        "batch runs. Check back after the next scheduled run."
                    ),
                }
        return {
            "source": "db", "count": len(opportunities),
            "as_of": opportunities[0]["created_at"][:10] if opportunities else None,
            "opportunities": opportunities,
        }
    except Exception as exc:
        return {"note": f"DB query failed: {exc}"}


def _chat_tool_get_undervalued_opportunities(min_upside_pct: float = 5, limit: int = 10) -> dict:
    db_path = _ROOT / "data" / "portfolio.db"
    if not db_path.exists():
        return {"note": "Database not found. Run the pipeline first to populate data."}
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT p.ticker, p.recommendation, p.confidence, p.composite_score,
                   p.pt_mean, p.pt_current_price, p.pt_num_analysts, p.created_at
            FROM predictions p
            JOIN (SELECT ticker, MAX(created_at) dt FROM predictions GROUP BY ticker) x
              ON p.ticker = x.ticker AND p.created_at = x.dt
            WHERE p.recommendation IN ('BUY', 'STRONG_BUY')
              AND p.pt_mean IS NOT NULL AND p.pt_current_price > 0
        """).fetchall()
        conn.close()
        candidates = []
        for r in rows:
            d = dict(r)
            upside = (d["pt_mean"] / d["pt_current_price"] - 1) * 100
            if upside >= min_upside_pct:
                d["upside_to_target_pct"] = round(upside, 1)
                candidates.append(d)
        candidates.sort(key=lambda d: d["upside_to_target_pct"], reverse=True)
        candidates = candidates[:limit]
        if not candidates:
            return {
                "source": "db", "count": 0,
                "note": (
                    "No tickers currently meet the undervalued+bullish screen "
                    f"(BUY/STRONG_BUY with >={min_upside_pct}% upside to analyst target). "
                    "Try a lower min_upside_pct, or note that most tickers may lack a "
                    "stored analyst price target."
                ),
            }
        return {"source": "db", "count": len(candidates), "opportunities": candidates}
    except Exception as exc:
        return {"note": f"DB query failed: {exc}"}


def _chat_tool_get_portfolio_summary() -> dict:
    db_path = _ROOT / "data" / "portfolio.db"
    if not db_path.exists():
        return {"note": "Database not found. Run the pipeline first."}
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        holdings = [dict(r) for r in conn.execute("""
            SELECT ticker,
                   SUM(shares) shares,
                   SUM(COALESCE(current_value,0)) current_value,
                   SUM(COALESCE(cost_basis_total, shares*avg_cost, 0)) cost_basis
            FROM holdings GROUP BY ticker ORDER BY current_value DESC
        """).fetchall()]
        tv = sum(h["current_value"] for h in holdings)
        tc = sum(h["cost_basis"] for h in holdings)
        for h in holdings:
            h["weight_pct"] = round(h["current_value"] / tv * 100, 1) if tv else 0
            h["unrealized_pct"] = (
                round((h["current_value"] - h["cost_basis"]) / h["cost_basis"] * 100, 1)
                if h["cost_basis"] else None
            )
        ht = [h["ticker"] for h in holdings]
        if ht:
            ph = ",".join("?" * len(ht))
            preds = {r[0]: {"recommendation": r[1], "confidence": r[2], "composite_score": r[3]}
                     for r in conn.execute(f"""
                SELECT p.ticker, p.recommendation, p.confidence, p.composite_score
                FROM predictions p
                JOIN (SELECT ticker, MAX(created_at) dt FROM predictions
                      WHERE ticker IN ({ph}) GROUP BY ticker) x
                  ON p.ticker=x.ticker AND p.created_at=x.dt
            """, ht).fetchall()}
            for h in holdings:
                h["apex"] = preds.get(h["ticker"])
        conn.close()
        return {
            "source": "db",
            "total_value": round(tv, 2),
            "total_cost": round(tc, 2),
            "unrealized_pct": round((tv - tc) / tc * 100, 1) if tc else None,
            "holdings": holdings,
        }
    except Exception as exc:
        return {"note": f"DB query failed: {exc}"}


def _chat_tool_get_predictions_summary(segment: str = "all",
                                        recommendation: str | None = None) -> dict:
    db_path = _ROOT / "data" / "portfolio.db"
    if not db_path.exists():
        return {"note": "Database not found."}
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        seg_clause = ""
        seg_params: list = []
        if segment == "portfolio":
            try:
                import yaml as _yaml
                pdata = _yaml.safe_load((_ROOT / "config" / "portfolio.yaml").read_text()) or {}
                ptickers = [h["ticker"].upper() for h in pdata.get("holdings", []) if "ticker" in h]
                if ptickers:
                    ph = ",".join("?" * len(ptickers))
                    seg_clause = f"AND p.ticker IN ({ph})"
                    seg_params = ptickers
            except Exception:
                pass
        elif segment == "opportunities":
            seg_clause = "AND p.trigger_type='trending_opportunity'"

        rec_clause = ""
        rec_params: list = []
        if recommendation:
            rec_clause = "AND p.recommendation=?"
            rec_params = [recommendation.upper()]

        rows = conn.execute(f"""
            SELECT p.ticker, p.recommendation, p.confidence, p.composite_score,
                   p.fundamental_score, p.research_score, p.news_score,
                   p.trigger_type, p.created_at
            FROM predictions p
            JOIN (SELECT ticker, MAX(created_at) dt FROM predictions GROUP BY ticker) x
              ON p.ticker=x.ticker AND p.created_at=x.dt
            WHERE 1=1 {seg_clause} {rec_clause}
            ORDER BY p.composite_score DESC NULLS LAST
            LIMIT 30
        """, seg_params + rec_params).fetchall()
        conn.close()
        preds = [dict(r) for r in rows]
        return {"source": "db", "segment": segment, "count": len(preds), "predictions": preds}
    except Exception as exc:
        return {"note": f"DB query failed: {exc}"}


def _chat_tool_get_technical_snapshot(ticker: str) -> dict:
    try:
        from portfolio_agent.tools.yfinance_tools import get_technical_indicators
        return json.loads(get_technical_indicators(ticker.upper()))
    except Exception as exc:
        return {"ticker": ticker.upper(), "note": f"Technical data unavailable: {exc}"}


def _chat_tool_get_portfolio_sector_allocation() -> dict:
    db_path = _ROOT / "data" / "portfolio.db"
    if not db_path.exists():
        return {"note": "Database not found. Run the pipeline first."}
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        holdings = [dict(r) for r in conn.execute("""
            SELECT ticker, SUM(COALESCE(current_value,0)) current_value
            FROM holdings GROUP BY ticker
        """).fetchall()]
        conn.close()
        if not holdings:
            return {"note": "No holdings found."}
        tv = sum(h["current_value"] for h in holdings) or 1.0

        from portfolio_agent.tools.universe_db import get_sectors
        sectors = get_sectors([h["ticker"] for h in holdings])

        sector_totals: dict[str, float] = {}
        sector_tickers: dict[str, list[str]] = {}
        for h in holdings:
            s = sectors.get(h["ticker"], "Unknown")
            sector_totals[s] = sector_totals.get(s, 0.0) + h["current_value"]
            sector_tickers.setdefault(s, []).append(h["ticker"])

        allocation = sorted(
            (
                {
                    "sector": s,
                    "weight_pct": round(v / tv * 100, 1),
                    "value": round(v, 2),
                    "tickers": sector_tickers[s],
                }
                for s, v in sector_totals.items()
            ),
            key=lambda x: x["weight_pct"], reverse=True,
        )
        return {"source": "db", "total_value": round(tv, 2), "allocation": allocation}
    except Exception as exc:
        return {"note": f"DB query failed: {exc}"}


def _chat_tool_get_portfolio_risk_flags(ticker: str = "") -> dict:
    try:
        from portfolio_agent.tools.risk_flags_db import get_active_flags, get_flags_for_ticker

        if ticker:
            flags = get_flags_for_ticker(ticker.upper())
            if not flags:
                return {"ticker": ticker.upper(),
                        "note": "No risk/technical flag data for this ticker yet."}
            out = {}
            for source, row in flags.items():
                row = dict(row)
                try:
                    row["detail"] = json.loads(row.pop("raw_json", None) or "{}")
                except Exception:
                    row["detail"] = {}
                out[source] = row
            return {"ticker": ticker.upper(), "flags": out}

        db_path = _ROOT / "data" / "portfolio.db"
        holding_tickers: set = set()
        if db_path.exists():
            conn = sqlite3.connect(str(db_path))
            holding_tickers = {r[0] for r in conn.execute("SELECT DISTINCT ticker FROM holdings").fetchall()}
            conn.close()

        active = [f for f in get_active_flags(None)
                  if not holding_tickers or f["ticker"] in holding_tickers]
        for f in active:
            try:
                f["detail"] = json.loads(f.pop("raw_json", None) or "{}")
            except Exception:
                f["detail"] = {}
        return {
            "as_of_date": active[0]["as_of_date"] if active else None,
            "count": len(active),
            "flags": active,
            "note": (
                "flag=1 rows only -- these are the active concentration/correlation/"
                "beta/momentum warnings the dashboard's Attention Queue surfaces."
                if active else
                "No active risk/technical flags for current holdings."
            ),
        }
    except Exception as exc:
        return {"ticker": ticker.upper() if ticker else None, "note": f"DB query failed: {exc}"}


# ── Dispatcher ────────────────────────────────────────────────────────────────

def _execute_chat_tool(name: str, args: dict) -> str:
    try:
        if name == "web_search":
            result = _chat_tool_web_search(args.get("query", ""), args.get("max_results", 5))
        elif name == "search_ticker":
            result = _chat_tool_search_ticker(args.get("company_name", ""))
        elif name == "get_ticker_news":
            result = _chat_tool_get_news(args.get("ticker", ""))
        elif name == "get_market_news":
            result = _chat_tool_get_market_news(args.get("limit", 20))
        elif name == "get_ipo_info":
            result = _chat_tool_get_ipo_info(args.get("ticker", ""))
        elif name == "get_sector_performance":
            result = _chat_tool_get_sector_performance()
        elif name == "get_fundamentals":
            result = _chat_tool_get_fundamentals(args.get("ticker", ""))
        elif name == "get_research":
            result = _chat_tool_get_research(args.get("ticker", ""))
        elif name == "get_macro_snapshot":
            result = _chat_tool_get_macro()
        elif name == "get_prediction_history":
            result = _chat_tool_get_predictions(args.get("ticker", ""))
        elif name == "get_prediction_accuracy":
            result = _chat_tool_get_prediction_accuracy(args.get("ticker", ""))
        elif name == "get_price_history":
            result = _chat_tool_get_price_history(
                args.get("ticker", ""), args.get("period", "3mo")
            )
        elif name == "get_latest_opportunities":
            result = _chat_tool_get_latest_opportunities(
                min_confidence=args.get("min_confidence", 6),
                limit=args.get("limit", 10),
            )
        elif name == "get_undervalued_opportunities":
            result = _chat_tool_get_undervalued_opportunities(
                min_upside_pct=args.get("min_upside_pct", 5),
                limit=args.get("limit", 10),
            )
        elif name == "get_portfolio_summary":
            result = _chat_tool_get_portfolio_summary()
        elif name == "get_predictions_summary":
            result = _chat_tool_get_predictions_summary(
                segment=args.get("segment", "all"),
                recommendation=args.get("recommendation"),
            )
        elif name == "get_technical_snapshot":
            result = _chat_tool_get_technical_snapshot(args.get("ticker", ""))
        elif name == "get_portfolio_sector_allocation":
            result = _chat_tool_get_portfolio_sector_allocation()
        elif name == "get_portfolio_risk_flags":
            result = _chat_tool_get_portfolio_risk_flags(args.get("ticker", ""))
        else:
            result = {"error": f"Unknown tool: {name}"}
        return json.dumps(result, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})
