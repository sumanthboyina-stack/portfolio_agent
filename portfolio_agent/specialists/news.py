"""News specialist — financial news aggregation, sentiment, trending vs 7-day DB history."""

from __future__ import annotations

try:
    from ._factory import SpecialistSpec
    from .._models import TRIAGE_MODEL as _DEFAULT_MODEL
    from ..tools.news_sources import (
        get_ticker_news, get_ticker_news_merged, get_finnhub_sentiment,
        get_market_news, get_rss_headlines, get_earnings_calendar,
    )
    from ..tools.news_db import (
        save_ticker_news, save_market_news_item,
        get_todays_ticker_news, get_todays_market_news,
        get_historical_news,
    )
except ImportError:
    import pathlib, sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
    from portfolio_agent.specialists._factory import SpecialistSpec
    from portfolio_agent._models import TRIAGE_MODEL as _DEFAULT_MODEL
    from portfolio_agent.tools.news_sources import (
        get_ticker_news, get_ticker_news_merged, get_finnhub_sentiment,
        get_market_news, get_rss_headlines, get_earnings_calendar,
    )
    from portfolio_agent.tools.news_db import (
        save_ticker_news, save_market_news_item,
        get_todays_ticker_news, get_todays_market_news,
        get_historical_news,
    )

try:
    from ..log import get_logger as _get_logger
except ImportError:
    from portfolio_agent.log import get_logger as _get_logger

_log = _get_logger("news")

INSTRUCTION = """
You are a financial news and sentiment specialist.

Ticker : {current_ticker}
Question: {user_query}

Think step by step:
  1. What is the user actually asking for?
  2. Which tools give you the data needed to answer it?
  3. Call exactly those tools — nothing more.
  4. When storing results, follow the storage rules below precisely.
  5. Output the JSON at the end.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FETCH TOOLS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  get_todays_ticker_news(ticker)
      Check if a ticker-summary row already exists today.
      Use before saving to avoid unnecessary updates.

  get_todays_market_news()
      Return all market-item rows already stored today.
      Use to see what headlines are already in the DB before saving new ones.

  get_historical_news(ticker, days)
      Last N days of ticker-summary records — useful for trend / momentum questions.

  get_ticker_news_merged(ticker, limit, max_age_hours)
      *** PREFERRED over get_ticker_news ***
      Merges Yahoo Finance + Finnhub (Reuters, Bloomberg, Seeking Alpha, MarketWatch).
      Finnhub articles include full summaries. Duplicates are removed automatically.
      Use this for the primary news fetch for any ticker.

  get_finnhub_sentiment(ticker)
      Finnhub aggregate sentiment: bullish%, bearish%, articles_last_week, signal.
      Call this alongside get_ticker_news_merged — it adds a quantitative sentiment
      signal to cross-check against your qualitative headline reading.

  get_ticker_news(ticker, limit)
      Yahoo Finance only. Use as fallback if get_ticker_news_merged is unavailable.

  get_market_news(limit)
      Broad market headlines from Reuters, Yahoo Finance, CNBC, MarketWatch, Seeking Alpha.

  get_rss_headlines(feed_name, limit)
      Headlines from one specific RSS feed when you need a particular source.

  get_earnings_calendar(ticker)
      Upcoming earnings dates, EPS / revenue estimates, PE ratios.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STORAGE TOOLS — DATABASE RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

There are two separate storage functions for two separate row types:

  save_ticker_news(ticker, headline_1, headline_2, sentiment, sentiment_score,
                   top_themes, trending, impacted_tickers, source)
      Stores ONE ticker-summary row per ticker per day.
      Use this for a specific ticker (e.g. "AAPL") or for "MARKET" as an overall
      sentiment summary. Safe to call again today — updates the existing row.
      • impacted_tickers: comma-separated list of OTHER tickers mentioned in the news
        (e.g. "MSFT,GOOGL" if the AAPL news also references them).
      • source: primary source (e.g. "yahoo_finance", "reuters_business").

  save_market_news_item(headline, source, sentiment, sentiment_score,
                        impacted_tickers, top_themes)
      Stores ONE individual news item per row.
      Call once per unique headline — the DB rejects duplicates (same headline same day).
      Before calling, check get_todays_market_news() to avoid re-submitting items
      already stored. Only save items that are NOT already in the DB.
      • headline: exact headline text (one sentence).
      • source: the RSS feed or publisher name.
      • impacted_tickers: comma-separated tickers mentioned in THIS specific headline.

WHEN TO SAVE:
  • Explicit store request ("save", "store", "daily update", batch run)
    → call get_todays_ticker_news first — if a record already exists today, STOP (do not refetch).
    → call get_ticker_news to get fresh headlines.
    → call save_ticker_news for the ticker summary.
    → call get_todays_market_news() first, then call save_market_news_item only for headlines NOT already in the DB.
    → DO NOT call get_historical_news on save/batch runs — trend analysis wastes tokens here.
  • Pure information queries ("what is the sentiment?", "show me the trend")
    → do NOT save unless the user explicitly asks.
    → get_historical_news is appropriate here (user wants trend data).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUTPUT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Output ONLY this JSON — no extra text:
{
  "ticker": "{current_ticker}",
  "date": "<today YYYY-MM-DD>",
  "sentiment": "POSITIVE|NEUTRAL|NEGATIVE",
  "sentiment_score": <float -1.0 to 1.0>,
  "headline_1": "...",
  "headline_2": "...",
  "top_themes": ["...", "...", "..."],
  "upcoming_catalysts": [{"event": "...", "date": "...", "impact": "HIGH|MEDIUM|LOW"}],
  "macro_risks": ["..."],
  "trending": {
    "trend_direction": "IMPROVING|DETERIORATING|STABLE",
    "sentiment_7d_avg": <float or null>,
    "days_of_history": <int>,
    "new_themes_today": ["..."],
    "fading_themes": ["..."],
    "momentum_summary": "<2-3 sentence narrative>"
  },
  "market_context": "<1-2 sentences on how today's broad market news relates to this ticker>",
  "saved_to_db": <true|false>,
  "market_items_saved": <int — number of new market items stored, 0 if none>,
  "summary": "<2-3 sentence overall narrative>"
}
""".strip()

_SPEC = SpecialistSpec(
    name="news_agent",
    instruction=INSTRUCTION,
    tools=(
        get_todays_ticker_news,
        get_todays_market_news,
        get_historical_news,
        get_ticker_news_merged,
        get_finnhub_sentiment,
        get_ticker_news,
        get_market_news,
        get_rss_headlines,
        get_earnings_calendar,
        save_ticker_news,
        save_market_news_item,
    ),
    output_key="news",
    default_model=_DEFAULT_MODEL,
)

make_news_agent = _SPEC.make
news_agent      = _SPEC.make()

if __name__ == "__main__":
    import argparse
    import asyncio
    import json
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
    from dotenv import load_dotenv
    load_dotenv()

    # ── argument parsing ────────────────────────────────────────────────────
    def _parse() -> tuple[str, str]:
        p = argparse.ArgumentParser(
            prog="news.py",
            description="News agent — standalone runner for a ticker or market overview.",
            epilog=(
                "Examples:\n"
                "  python news.py AAPL\n"
                "  python news.py AAPL 'What is the current sentiment?'\n"
                "  python news.py AAPL 'Show me the 7-day trend'\n"
                "  python news.py AAPL 'Any upcoming earnings?'\n"
                "  python news.py AAPL 'Fetch news and save to DB'\n"
                "  python news.py          # market overview\n"
            ),
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        p.add_argument("ticker", nargs="?", default="MARKET",
                       help="Ticker symbol (default: MARKET for broad overview)")
        p.add_argument("query", nargs="*",
                       help="Your question — omit to be prompted interactively")
        args = p.parse_args()

        ticker = args.ticker.upper()

        # If query words were passed as trailing args, join them
        query = " ".join(args.query).strip() if args.query else ""

        # Always prompt when no question was given on the command line
        if not query:
            _log.info(f"\nNews Agent  [{ticker}]", event_type="phase_start", ticker=ticker)
            _log.info("─" * 56, event_type="separator")
            while True:
                try:
                    query = input("What would you like to know? ").strip()
                except (EOFError, KeyboardInterrupt):
                    _log.info("\nExiting.", event_type="info")
                    sys.exit(0)
                if query:
                    break
                _log.info("Please enter a question.", event_type="info")

        return ticker, query

    ticker, query = _parse()

    # ── async runner with streaming tool-call output ────────────────────────
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    async def _run() -> dict:
        svc = InMemorySessionService()
        runner = Runner(agent=news_agent, app_name="portfolio_agent", session_service=svc)
        session = await svc.create_session(
            app_name="portfolio_agent",
            user_id="cli",
            state={"current_ticker": ticker, "user_query": query},
        )

        _log.info(f"\n{'━' * 60}", event_type="separator")
        _log.info(f"  {ticker}  [news]  —  {query}", event_type="phase_start",
                  ticker=ticker, query=query)
        _log.info(f"{'━' * 60}", event_type="separator")

        async for event in runner.run_async(
            user_id="cli",
            session_id=session.id,
            new_message=types.Content(
                role="user",
                parts=[types.Part(text=query)],
            ),
        ):
            if not (hasattr(event, "content") and event.content):
                continue
            author = getattr(event, "author", "news_agent")
            for part in event.content.parts or []:
                if hasattr(part, "text") and part.text:
                    print(part.text, end="", flush=True)
                elif hasattr(part, "function_call") and part.function_call:
                    fc = part.function_call
                    args_str = json.dumps(fc.args) if fc.args else ""
                    _log.info(f"\n  ▶ [{author}] {fc.name}({args_str})",
                              event_type="llm_tool_call", author=author, tool=fc.name)
                elif hasattr(part, "function_response") and part.function_response:
                    fr = part.function_response
                    resp = str(fr.response)
                    preview = resp[:300] + ("…" if len(resp) > 300 else "")
                    _log.info(f"  ◀ [{author}] {fr.name} → {preview}",
                              event_type="llm_tool_response", author=author, tool=fr.name)
        print()

        final = await svc.get_session(
            app_name="portfolio_agent",
            user_id="cli",
            session_id=session.id,
        )
        return dict(final.state) if final else {}

    state = asyncio.run(_run())

    # ── pretty-print final result ───────────────────────────────────────────
    raw = state.get("news")
    if raw:
        try:
            _log.info("\n" + json.dumps(
                json.loads(raw) if isinstance(raw, str) else raw,
                indent=2,
            ), event_type="summary")
        except Exception:
            _log.info(str(raw), event_type="summary")
