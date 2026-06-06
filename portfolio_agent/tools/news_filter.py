"""
Three-stage news pre-filter for daily batch — minimises LLM tokens.

Stage 1  (0 LLM tokens): parallel yfinance + Finnhub fetch for every ticker
Stage 2  (0 LLM tokens): rule-based keyword / volume triage on merged articles
Stage 3  (1 cheap Haiku call): batch materiality scoring of survivors

Yahoo Finance and Finnhub are fetched in parallel; results are merged and
de-duplicated per ticker before triage. Finnhub's summaries give keyword
matching more text surface area, improving triage recall.

Typical flow for 100 tickers on a quiet day:
  Stage 1+2: 100 → 25 "interesting"   (keyword / volume rules)
  Stage 3  :  25 →  8 "material"      (Haiku scores headlines 0-3, keeps >0)
  Full news agent:   8 Sonnet calls    (saves to DB)

Token cost: 1 Haiku call (~2k tokens) vs 100 Sonnet calls (~800k tokens).
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import yfinance as yf

from portfolio_agent.tools.finnhub_news import batch_fetch_finnhub, merge_articles
from portfolio_agent.log import get_logger as _get_logger

_log = _get_logger("news")

# ── Tiered keyword sets ────────────────────────────────────────────────────────
#
# Precision/recall tradeoffs:
#   _HIGH_SIGNAL   : 1 match is sufficient — binary events that are always material
#   _MEDIUM_SIGNAL : 2+ matches required  — directional signals, often sector-wide
#   _LOW_SIGNAL    : 3+ matches required  — vague terms that need corroboration
#
# This eliminates false positives from "results", "outlook", "deal" matching every
# macro piece that mentions a ticker in passing. Stage 3 Haiku still reviews all
# survivors, but now has a much cleaner signal set to judge.

_HIGH_SIGNAL = frozenset({
    # Earnings (direct)
    "earnings", "eps",
    # Results direction
    "beat", "miss",
    # Corporate transactions (binary events)
    "merger", "acquisition", "takeover", "buyout", "spinoff", "ipo",
    # Regulatory / legal (binary outcomes)
    "fda", "approval", "recall", "subpoena",
    "lawsuit", "settlement", "investigation",
    # Executive-level changes
    "ceo", "cfo", "cto", "resign", "fired", "departure",
    # Material workforce events
    "layoffs", "layoff",
    # Explicit company forward statement
    "guidance",
})

_MEDIUM_SIGNAL = frozenset({
    # Financial performance (not as binary as EPS beat/miss)
    "revenue", "quarterly", "profit", "loss", "results",
    # Analyst actions
    "upgrade", "downgrade", "price target", "overweight", "underweight",
    "initiates", "raises", "cuts",
    "buy rating", "sell rating",
    # Regulatory (less immediate than FDA)
    "sec", "doj", "fine", "penalty", "charges", "regulation", "antitrust",
    # Capital structure
    "dividend", "buyback", "stock split", "offering", "dilution",
    # Corporate events (softer)
    "deal", "partnership", "joint venture", "divest",
    # Macro
    "tariff", "sanction", "ban",
    # Personnel / org (below C-suite)
    "appointed", "restructuring", "job cuts", "hiring freeze",
})

_LOW_SIGNAL = frozenset({
    # Vague forward-looking terms — match constantly in financial coverage
    "forecast", "outlook",
})


def _hours_ago(published_at: str) -> float:
    """Return hours since published_at (ISO 8601). Returns inf if unparseable."""
    if not published_at:
        return float("inf")
    try:
        pub = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - pub
        return delta.total_seconds() / 3600
    except Exception:
        return float("inf")


def _fetch_one(ticker: str, max_age_hours: int) -> dict:
    """Fetch recent headlines for a single ticker. Returns {ticker, articles}."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    try:
        news = yf.Ticker(ticker).news or []
    except Exception:
        news = []

    recent = []
    for item in news:
        ts = item.get("providerPublishTime", 0)
        if ts:
            pub = datetime.fromtimestamp(ts, tz=timezone.utc)
            if pub >= cutoff:
                recent.append({
                    "title":        item.get("title", ""),
                    "publisher":    item.get("publisher", ""),
                    "published_at": pub.isoformat(),
                    "_source":      "yahoo",
                })
    return {"ticker": ticker, "articles": recent}


def _triage_one(ticker: str, articles: list[dict]) -> dict:
    """Apply tiered keyword + publisher-diversity rules to decide if a ticker warrants LLM analysis."""
    count = len(articles)
    if count == 0:
        return {
            "ticker": ticker,
            "article_count": 0,
            "is_interesting": False,
            "reason": "no_news_48h",
            "matched_keywords": {"high": [], "medium": [], "low": []},
            "articles": [],
        }

    high_hits:   list[str] = []
    medium_hits: list[str] = []
    low_hits:    list[str] = []
    for art in articles:
        title_lower = art["title"].lower()
        for kw in _HIGH_SIGNAL:
            if kw in title_lower and kw not in high_hits:
                high_hits.append(kw)
        for kw in _MEDIUM_SIGNAL:
            if kw in title_lower and kw not in medium_hits:
                medium_hits.append(kw)
        for kw in _LOW_SIGNAL:
            if kw in title_lower and kw not in low_hits:
                low_hits.append(kw)

    # Volume spike: 3+ articles from 2+ distinct publishers within 24h.
    # Filters out "one Reuters story republished by 3 outlets" scenarios —
    # a single event looks like volume but all publishers are the same outlet family.
    recent_24h = [a for a in articles if _hours_ago(a.get("published_at", "")) < 24]
    unique_pubs = {
        a.get("publisher") or a.get("source") or ""
        for a in recent_24h
    } - {""}
    is_volume_spike = len(recent_24h) >= 3 and len(unique_pubs) >= 2

    is_interesting = (
        bool(high_hits)            # any HIGH match — always material
        or len(medium_hits) >= 2   # 2+ MEDIUM — converging signals
        or len(low_hits) >= 3      # 3+ LOW — very unlikely, kept for safety
        or is_volume_spike         # multi-source coverage surge
    )

    # Build reason string (top 3 matched keywords, or volume annotation)
    all_matched = high_hits + medium_hits + low_hits
    if all_matched:
        reason = ", ".join(all_matched[:3])
    elif is_volume_spike:
        reason = f"volume_spike:{len(recent_24h)}art/{len(unique_pubs)}pubs"
    else:
        reason = "routine"

    return {
        "ticker": ticker,
        "article_count": count,
        "is_interesting": is_interesting,
        "reason": reason,
        "matched_keywords": {          # carried into filter_log + conservative fallback
            "high":   high_hits[:5],
            "medium": medium_hits[:5],
            "low":    low_hits[:5],
        },
        "articles": articles[:12],
    }


def batch_fetch_and_triage(
    tickers: list[str],
    max_age_hours: int = 48,   # 48h window: triage + batch LLM use the same articles
    max_workers: int = 12,
) -> str:
    """
    Fetch headlines from Yahoo Finance AND Finnhub in parallel, select per ticker
    (Finnhub primary; Yahoo fallback only when Finnhub returned nothing), then
    apply rule-based keyword/volume triage.

    No LLM calls — pure HTTP + keyword matching.

    Args:
        tickers:       List of ticker symbols.
        max_age_hours: Only consider articles published within this window.
        max_workers:   Thread pool size for Yahoo parallel fetching.

    Returns:
        JSON: {
          total, interesting_count, skipped_count,
          sources: {yahoo: int, finnhub: int, active: int},
          interesting:        [{ticker, article_count, reason, matched_keywords,
                                articles (up to 12)}],
          skipped:            [{ticker, article_count, reason}],
          source_by_ticker:   {ticker: "finnhub" | "yahoo_fallback" | "none"},
          articles_by_ticker: {ticker: [articles (up to 12)]} for ALL tickers —
                              lets callers reuse fetched articles without a second
                              Finnhub round-trip that would burn the rate limit.
        }
    """
    import os as _os
    from concurrent.futures import Future as _Future

    # ── Yahoo Finance fetch (existing) ────────────────────────────────────────
    yahoo_raw: dict[str, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures: dict[_Future, str] = {
            pool.submit(_fetch_one, t, max_age_hours): t for t in tickers
        }
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                result = future.result()
                yahoo_raw[result["ticker"]] = result.get("articles", [])
            except Exception as exc:
                yahoo_raw[ticker] = []

    # ── Finnhub fetch (parallel with Yahoo above, now merge) ─────────────────
    finnhub_raw = batch_fetch_finnhub(tickers, max_age_hours=max_age_hours, max_workers=8)

    # ── Source selection per ticker (Finnhub primary, Yahoo fallback) ────────
    yahoo_total   = sum(len(v) for v in yahoo_raw.values())
    finnhub_total = sum(len(v) for v in finnhub_raw.values())

    merged_by_ticker: dict[str, list[dict]] = {}
    source_by_ticker: dict[str, str]        = {}
    for ticker in tickers:
        ya = yahoo_raw.get(ticker, [])
        fh = finnhub_raw.get(ticker, [])
        merged_by_ticker[ticker] = merge_articles(ya, fh)
        if fh:
            source_by_ticker[ticker] = "finnhub"
        elif ya:
            source_by_ticker[ticker] = "yahoo_fallback"
        else:
            source_by_ticker[ticker] = "none"

    active_total = sum(len(v) for v in merged_by_ticker.values())
    yahoo_fb_count = sum(1 for s in source_by_ticker.values() if s == "yahoo_fallback")

    _log.info(
        f"  [news/sources] Yahoo: {yahoo_total} articles, "
        f"Finnhub: {finnhub_total} articles, "
        f"active: {active_total}"
        + (f"  [{yahoo_fb_count} tickers used Yahoo fallback]" if yahoo_fb_count else ""),
        event_type="fetch_end",
        yahoo=yahoo_total, finnhub=finnhub_total, active=active_total,
        yahoo_fallback_count=yahoo_fb_count,
    )

    # ── Triage on selected articles ───────────────────────────────────────────
    triaged = [
        _triage_one(ticker, merged_by_ticker.get(ticker, []))
        for ticker in tickers
    ]

    interesting = [t for t in triaged if t["is_interesting"]]
    skipped = [
        {"ticker": t["ticker"], "article_count": t["article_count"], "reason": t["reason"]}
        for t in triaged
        if not t["is_interesting"]
    ]

    return json.dumps({
        "total": len(triaged),
        "interesting_count": len(interesting),
        "skipped_count": len(skipped),
        "sources": {
            "yahoo":   yahoo_total,
            "finnhub": finnhub_total,
            "active":  active_total,   # articles actually used for triage
        },
        "interesting": interesting,
        "skipped": skipped,
        # Per-ticker source label — used at save time so source field is meaningful
        "source_by_ticker": source_by_ticker,
        # Full article lists — callers reuse these without a second Finnhub fetch
        "articles_by_ticker": {
            t: merged_by_ticker.get(t, [])[:12] for t in tickers
        },
    })


# ── Stage 3: one Haiku call scores all survivors ───────────────────────────────

_SCORER_PROMPT = """\
You are a financial news analyst. Below are today's headlines grouped by ticker.

For each ticker output exactly ONE JSON object on its own line:
  {"ticker": "XXX", "score": N, "summary": "one sentence"}

score scale:
  0 = noise / generic market commentary / ticker mentioned in passing
  1 = minor (routine analyst note, sector piece with limited specific impact)
  2 = moderate (upgrade/downgrade, small deal, guidance tweak)
  3 = high (earnings, M&A, FDA decision, SEC action, CEO departure, major guidance change)

Rules:
- score > 0 ONLY if this news could materially move THIS specific stock price.
- Generic ETF flows, index rebalancing, or "stocks fell" articles → always score 0.
- If a ticker has no headlines → {"ticker": "XXX", "score": 0, "summary": "no headlines"}

Output ONLY the JSON lines. No intro text, no markdown, no trailing comma.

TICKERS AND HEADLINES:
"""

_CHUNK_SIZE = 35  # max tickers per Haiku call to stay under context limits


def _build_headlines_block(items: list[dict]) -> str:
    lines = []
    for item in items:
        ticker = item["ticker"]
        headlines = [a["title"] for a in item.get("articles", []) if a.get("title")]
        if headlines:
            joined = " | ".join(headlines[:5])
            lines.append(f"{ticker}: {joined}")
        else:
            lines.append(f"{ticker}: (no headlines)")
    return "\n".join(lines)


def _call_scorer(headlines_block: str) -> tuple[list[dict], str]:
    """
    Score headlines using the triage failover chain (free Gemini/Groq first).

    Returns:
        (results, model_label) — parsed score objects and the model label that succeeded.
    """
    import litellm
    from portfolio_agent._models import FAILOVER_CHAINS, _is_failover_error

    # Use only the non-ADK-tool models from the triage chain (direct completion call)
    chain = FAILOVER_CHAINS.get("triage", [])
    messages = [{"role": "user", "content": _SCORER_PROMPT + headlines_block}]

    for model_id, _, label in chain:
        try:
            resp = litellm.completion(
                model=model_id,
                messages=messages,
                temperature=0.0,
                max_tokens=1024,
            )
            raw_text = resp.choices[0].message.content or ""
            results = []
            for line in raw_text.splitlines():
                line = line.strip().rstrip(",")
                if not line or not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                    if "ticker" in obj and "score" in obj:
                        results.append({
                            "ticker": str(obj["ticker"]).upper(),
                            "score": int(obj.get("score", 0)),
                            "summary": str(obj.get("summary", "")),
                        })
                except (json.JSONDecodeError, ValueError):
                    continue
            _log.info(f"  [scorer/{label}] scored {len(results)} tickers",
                      event_type="llm_call_success", model=label, count=len(results))
            return results, label
        except Exception as exc:
            if _is_failover_error(exc):
                _log.warning(f"  [scorer] {label} failed ({type(exc).__name__}: "
                             f"{str(exc)[:80]}), trying next…",
                             event_type="llm_call_error", model=label,
                             error_type=type(exc).__name__, error=str(exc)[:80])
                continue
            raise

    raise RuntimeError("All scorer models exhausted.")


def _conservative_score_fallback(item: dict) -> int:
    """
    Keyword-based scoring when all scorer models are unavailable.

    Only advances tickers with HIGH-signal keywords — these are material by
    definition and don't need LLM judgment to proceed.  Everything else gets
    score=0 (dropped from Stage 4).

    This preserves the filter's cost-control function even during scorer outages.
    The failure mode goes from "Stage 4 processes 40 tickers" (old score=1 default)
    to "Stage 4 processes 8–10 tickers" (only unambiguous events advance).
    """
    _STRONG = {
        "earnings", "merger", "acquisition", "fda", "approval",
        "ceo", "cfo", "cto", "resign", "fired", "departure",
        "lawsuit", "settlement", "guidance", "layoffs", "layoff",
        "beat", "miss", "subpoena", "recall", "ipo",
    }
    # Prefer structured matched_keywords (present when _triage_one ran)
    mk = item.get("matched_keywords", {})
    high_hits = set(mk.get("high", []))
    if high_hits & _STRONG:
        return 2
    # Also check reason string (old-format items or volume_spike entries)
    reason = item.get("reason", "")
    if reason.startswith("volume_spike"):
        return 0   # volume-only, no keyword signal → drop
    matched = {kw.strip().lower() for kw in reason.split(",") if kw.strip()}
    return 2 if matched & _STRONG else 0


def batch_score_materiality(
    interesting: list[dict],
) -> dict[str, dict]:
    """
    Stage 3: send all interesting tickers to Haiku in one call (chunked at 35).

    Args:
        interesting: The `interesting` list from batch_fetch_and_triage output
                     — each item has {ticker, articles, reason, article_count,
                     matched_keywords}.

    Returns:
        Dict mapping ticker → {score: 0-3, summary: str}.
        Missing tickers default to score=0.
    """
    scores: dict[str, dict] = {}
    _scorer_model: str | None = None

    for chunk_start in range(0, len(interesting), _CHUNK_SIZE):
        chunk = interesting[chunk_start: chunk_start + _CHUNK_SIZE]
        block = _build_headlines_block(chunk)
        try:
            chunk_results, model_label = _call_scorer(block)
            if model_label and _scorer_model is None:
                _scorer_model = model_label
        except Exception as exc:
            # Conservative fallback: only HIGH-signal tickers advance.
            # Old behaviour (score=1 for all) was the expensive failure mode —
            # it sent every watchlist ticker to Stage 4 when the scorer was down.
            _log.error(f"  [scorer/exhausted] All models failed: {exc}",
                       event_type="llm_call_error", error_type=type(exc).__name__, error=str(exc)[:200])
            _log.warning(f"  [scorer/fallback] Keyword fallback for {len(chunk)} tickers "
                         f"— only HIGH-signal keywords advance",
                         event_type="llm_fallback", ticker_count=len(chunk))
            chunk_results = []
            for item in chunk:
                fb_score = _conservative_score_fallback(item)
                chunk_results.append({
                    "ticker":  item["ticker"],
                    "score":   fb_score,
                    "summary": "keyword-fallback" if fb_score > 0 else "scorer-unavailable",
                })
        valid_tickers = {item["ticker"].upper() for item in chunk}
        hallucinated = []
        for obj in chunk_results:
            t = obj["ticker"]
            if t not in valid_tickers:
                hallucinated.append(t)
                continue
            scores[t] = {"score": obj["score"], "summary": obj["summary"]}
        if hallucinated:
            _log.warning(
                f"  [scorer/hallucination] LLM returned {len(hallucinated)} ticker(s) "
                f"not in input — discarded: {', '.join(hallucinated)}",
                event_type="warning", extra_tickers=hallucinated,
            )

    if _scorer_model:
        _log.info(f"  [scorer] model used: {_scorer_model}",
                  event_type="summary", model=_scorer_model)

    return scores
