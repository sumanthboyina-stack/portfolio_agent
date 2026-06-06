"""News phase — three-stage triage + batch LLM summarization."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime as _datetime

from portfolio_agent.log import get_logger as _get_logger
from portfolio_agent._models import GEMINI_COUNTER
from portfolio_agent.pipeline.failover import (
    _is_failover_error,
    _is_groq_rpm_error,
    _is_context_too_large,
    _first_groq_idx,
    _groq_should_sleep,
    _GROQ_MAX_SLEEP_SECS,
)
from portfolio_agent.pipeline.json_parsing import _extract_json_array
from portfolio_agent.pipeline.prompt_templates import (
    _BATCH_NEWS_SIZE,
    _BATCH_NEWS_PROMPT,
    _NEWS_PUB_TIER1,
    _NEWS_PUB_TIER2,
)
from portfolio_agent.pipeline.watchlist import update_watchlist


def _rank_articles(articles: list[dict], top_n: int = 5) -> list[dict]:
    """
    Rank articles by signal strength, publisher quality, recency, and uniqueness.
    Deduplicates by Jaccard title overlap > 60%.
    """
    from datetime import timezone as _tz
    from portfolio_agent.tools.news_filter import _HIGH_SIGNAL, _MEDIUM_SIGNAL

    now = _datetime.now(_tz.utc)

    def _score(art: dict) -> float:
        title = art.get("title", "").lower()
        s = 0.0
        s += min(sum(1 for kw in _HIGH_SIGNAL   if kw in title) * 3, 9)
        s += min(sum(1 for kw in _MEDIUM_SIGNAL if kw in title) * 1, 3)
        pub = (art.get("publisher") or art.get("source") or art.get("_source") or "").lower()
        if any(t in pub for t in _NEWS_PUB_TIER1):
            s += 2
        elif any(t in pub for t in _NEWS_PUB_TIER2):
            s += 1
        ts = art.get("published_at") or art.get("published") or ""
        if ts:
            try:
                age_h = (now - _datetime.fromisoformat(
                    ts.replace("Z", "+00:00")
                )).total_seconds() / 3600
                s += 2 if age_h < 6 else (1 if age_h < 24 else 0)
            except Exception:
                pass
        return s

    scored = sorted(articles, key=_score, reverse=True)
    selected_word_sets: list[set] = []
    result: list[dict] = []
    for art in scored:
        words = set((art.get("title") or "").lower().split())
        is_dup = any(
            len(words & prev) / max(len(words | prev), 1) > 0.6
            for prev in selected_word_sets
        )
        if not is_dup:
            selected_word_sets.append(words)
            result.append(art)
        if len(result) >= top_n:
            break
    return result


def _format_news_block(ticker: str, articles: list) -> str:
    """Format one ticker's pre-fetched articles as a compact block for the batch news prompt."""
    lines = [f"=== {ticker} ==="]
    if not articles:
        lines.append("  (no articles found)")
        return "\n".join(lines)
    for i, a in enumerate(_rank_articles(articles, top_n=5), 1):
        ts  = (a.get("published_at") or a.get("published") or "")[:10]
        src = a.get("publisher") or a.get("source") or a.get("_source") or ""
        title   = a.get("title", "").strip()
        summary = (a.get("summary") or "").strip()
        lines.append(f"{i}. [{ts}] {title} ({src})")
        if summary:
            lines.append(f"   {summary[:200]}")
    return "\n".join(lines)


async def _run_news_phase(
    all_tickers: list[str],
    watchlist: list[str],
    portfolio_tickers: list[str],
    trending_from_news: list[str],
    extra_tickers: list[str] | None,
    watchlist_path,
    tracker=None,
) -> bool:
    """
    Three-stage news pipeline — shared by --daily-news and --daily.

    Stage 1+2 (no LLM): parallel yfinance fetch + keyword/volume triage
    Stage 3   (1 Haiku): batch materiality scoring of survivors
    Full agent (Sonnet): only tickers with materiality score > 0
    """
    log = _get_logger("news")
    from portfolio_agent.tools.news_filter import batch_fetch_and_triage, batch_score_materiality
    from portfolio_agent.tools.pipeline_skip import get_skip_tickers as _skip_tickers, log_skip as _log_skip
    import litellm as _litellm

    _ns = _skip_tickers("news")
    _skipped_restricted = [t for t in all_tickers if t in _ns]
    if _skipped_restricted:
        _log_skip("news", _skipped_restricted)
        all_tickers = [t for t in all_tickers if t not in _ns]

    always_run = set((extra_tickers or []) + portfolio_tickers)

    new_for_watchlist = [t for t in trending_from_news if t not in set(watchlist)]
    if new_for_watchlist:
        update_watchlist(watchlist_path, new_for_watchlist)

    log.info(
        f"\n  Stage 1+2: fetching + triaging {len(all_tickers)} tickers (no LLM)…",
        event_type="fetch_start",
    )
    triage = json.loads(batch_fetch_and_triage(all_tickers))

    triage_interesting   = triage["interesting"]
    interesting_tickers  = {r["ticker"] for r in triage_interesting}
    skipped_counts: dict[str, int] = {
        s["ticker"]: s["article_count"] for s in triage["skipped"]
    }

    always_run_with_news = [
        {"ticker": t, "articles": [], "reason": "always_run",
         "article_count": skipped_counts.get(t, 0)}
        for t in always_run
        if t not in interesting_tickers and skipped_counts.get(t, 0) > 0
    ]
    always_run_to_agent: set[str] = (
        interesting_tickers & always_run
        | {e["ticker"] for e in always_run_with_news}
    )
    scorer_input = [item for item in triage_interesting if item["ticker"] not in always_run]

    wl_count  = len(watchlist)
    port_new  = len([t for t in portfolio_tickers if t not in set(watchlist)])
    trend_new = len(new_for_watchlist)
    s2_skipped = triage["skipped_count"]
    s2_no_news = sum(
        1 for t in always_run
        if skipped_counts.get(t, 0) == 0 and t not in interesting_tickers
    )

    log.info(
        f"  Stage 1+2: {triage['interesting_count']} survived triage, "
        f"{s2_skipped} skipped (no news / routine)"
        + (f"  [{s2_no_news} portfolio tickers skipped — zero articles]" if s2_no_news else ""),
        event_type="summary",
    )

    scores: dict[str, dict] = {}
    if scorer_input:
        log.info(
            f"  Stage 3: scoring {len(scorer_input)} non-portfolio tickers in one Haiku call…",
            event_type="fetch_start",
        )
        scores   = batch_score_materiality(scorer_input)
        _s3_pass = sum(1 for v in scores.values() if v.get("score", 0) > 0)
        log.info(
            f"  Stage 3 done: {_s3_pass} pass → Stage 4, "
            f"{len(scorer_input) - _s3_pass} filtered out",
            event_type="summary",
        )

    from portfolio_agent.tools.checkpoint import (
        pending_from_yesterday as _news_pending,
        save_checkpoint as _save_cp,
        clear_phase as _clear_news_cp,
    )
    from portfolio_agent.tools.news_db import (
        save_ticker_news, update_ticker_news_model,
        load_seen_hashes as _load_hashes,
        save_article_hashes as _save_art_hashes,
        log_filter_decisions as _log_fd,
    )
    from portfolio_agent.tools.news_sources import get_market_news
    from portfolio_agent._models import FAILOVER_CHAINS
    import hashlib as _hashlib

    run_date = date.today().isoformat()

    _articles_by_ticker: dict[str, list] = triage.get("articles_by_ticker", {})
    _source_by_ticker:   dict[str, str]  = triage.get("source_by_ticker", {})
    articles_map: dict[str, list] = {t: _articles_by_ticker.get(t, []) for t in all_tickers}

    def _compute_hashes(arts: list[dict]) -> set[str]:
        return {
            _hashlib.md5((a.get("title") or "").strip().lower().encode()).hexdigest()
            for a in arts if (a.get("title") or "").strip()
        }

    _candidates_with_arts = [t for t in all_tickers if articles_map.get(t)]
    _seen_hashes          = _load_hashes(_candidates_with_arts)

    _resume_set: set[str] = set()
    news_resume = _news_pending("news")
    if news_resume:
        _resume_set = set(news_resume)
        log.info(
            f"  [resume] {len(news_resume)} news tickers carried over from previous run.",
            event_type="resume",
        )

    tickers_with_news: list[str] = []
    _skipped: dict[str, str] = {}
    s3_skipped = 0

    for _t in list(news_resume or []) + all_tickers:
        if _t in set(tickers_with_news) | set(_skipped):
            continue
        _arts = articles_map.get(_t, [])
        if not _arts:
            _skipped[_t] = "no_articles"
            continue
        if _t not in always_run_to_agent and scores.get(_t, {}).get("score", 0) == 0:
            _skipped[_t] = "low_materiality"
            s3_skipped  += 1
            continue
        if _t not in _resume_set:
            _current = _compute_hashes(_arts)
            if _current and not (_current - _seen_hashes.get(_t.upper(), set())):
                _skipped[_t] = "no_new_articles"
                continue
        tickers_with_news.append(_t)

    try:
        _triage_by_ticker  = {item["ticker"]: item for item in triage_interesting}
        _skipped_by_ticker = {s["ticker"]: s for s in triage["skipped"]}
        _run_set           = set(tickers_with_news)
        _filter_decisions  = []
        for _t in all_tickers:
            _is_int    = _t in interesting_tickers
            _item      = _triage_by_ticker.get(_t) or _skipped_by_ticker.get(_t, {})
            _s2_dec    = "interesting" if _is_int else "routine"
            _s2_reason = _item.get("reason", "")
            _s2_mk     = _item.get("matched_keywords") if _is_int else None
            if _t in always_run_to_agent:
                _s3_dec, _s3_score, _s3_sum = "bypassed_portfolio", None, None
            elif _is_int:
                _sd       = scores.get(_t, {})
                _s3_score = _sd.get("score")
                _s3_sum   = _sd.get("summary")
                _s3_dec   = "pass" if (_s3_score or 0) > 0 else "fail"
            else:
                _s3_dec, _s3_score, _s3_sum = "skipped_stage2", None, None
            _filter_decisions.append({
                "ticker":                   _t,
                "stage_2_decision":         _s2_dec,
                "stage_2_reason":           _s2_reason,
                "stage_2_matched_keywords": _s2_mk,
                "stage_3_decision":         _s3_dec,
                "stage_3_score":            _s3_score,
                "stage_3_summary":          _s3_sum,
                "final_decision":           "analyzed" if _t in _run_set else "dropped",
            })
        _log_fd(_filter_decisions)
        log.info(f"  [filter_log] {len(_filter_decisions)} decisions logged", event_type="info")
    except Exception as _fl_exc:
        log.warning(f"  [warn] filter_log failed (non-fatal): {_fl_exc}", event_type="warning")

    log.info(f"\n{'━' * 64}", event_type="separator")
    log.info(
        f"  News Batch — {len(tickers_with_news)} / {len(all_tickers)} tickers → full agent",
        event_type="phase_start",
    )
    log.info(
        f"  {wl_count} watchlist  |  {port_new} portfolio-only  |  {trend_new} market-trending",
        event_type="summary",
    )
    _skip_counts = {}
    for _r in _skipped.values():
        _skip_counts[_r] = _skip_counts.get(_r, 0) + 1
    log.info(
        f"  Stage 2 skipped: {triage['skipped_count']}  |  "
        f"Stage 3 filtered: {s3_skipped}  |  "
        f"No new articles: {_skip_counts.get('no_new_articles', 0)}",
        event_type="summary",
    )
    log.info(
        f"  [news/filter] total={len(all_tickers)} "
        f"analyzed={len(tickers_with_news)} "
        f"no_articles={_skip_counts.get('no_articles', 0)} "
        f"low_materiality={_skip_counts.get('low_materiality', 0)} "
        f"no_new_articles={_skip_counts.get('no_new_articles', 0)}",
        event_type="filter_summary",
        total=len(all_tickers), analyzed=len(tickers_with_news),
        no_articles=_skip_counts.get('no_articles', 0),
        low_materiality=_skip_counts.get('low_materiality', 0),
        no_new_articles=_skip_counts.get('no_new_articles', 0),
    )

    material = sorted(
        [(t, scores[t]) for t in tickers_with_news if t in scores and scores[t]["score"] > 0],
        key=lambda x: -x[1]["score"],
    )
    if material:
        top = ", ".join(
            f"{t}[{d['score']}] {d['summary'][:40]}…" if len(d['summary']) > 40
            else f"{t}[{d['score']}] {d['summary']}"
            for t, d in material[:6]
        )
        log.info(f"  Top     : {top}", event_type="summary")
    log.info(f"{'━' * 64}\n", event_type="separator")

    try:
        mkt = json.loads(get_market_news(limit=5))
        _raw_headlines = "\n".join(
            f"- {a['title'][:120]}"
            for a in mkt.get("articles", [])[:5]
            if a.get("title")
        )
        _sum_model = FAILOVER_CHAINS["triage"][0][0]
        try:
            _sum_resp = await _litellm.acompletion(
                model=_sum_model,
                messages=[{"role": "user", "content": (
                    "Condense these market headlines into 3-5 bullet points, "
                    "max 10 words each. Output ONLY the bullets:\n" + _raw_headlines
                )}],
                temperature=0.0,
                max_tokens=120,
            )
            market_context = (_sum_resp.choices[0].message.content or "").strip()
            if "gemini" in _sum_model:
                GEMINI_COUNTER.record(_sum_model)
        except Exception:
            market_context = "\n".join(
                f"• {a['title'][:60]}"
                for a in mkt.get("articles", [])[:3]
                if a.get("title")
            )
    except Exception:
        market_context = "(not available)"

    triage_chain  = FAILOVER_CHAINS.get("triage", [])
    model_idx     = 0
    completed_news: list[str] = []
    failed_news:    list[str] = []
    if tracker:
        tracker.start_phase("news", total=len(tickers_with_news))

    def _nchunks(lst, n):
        for k in range(0, len(lst), n):
            yield lst[k: k + n]

    batch_num       = 0
    total_batches   = (len(tickers_with_news) + _BATCH_NEWS_SIZE - 1) // _BATCH_NEWS_SIZE
    _news_last_exc: BaseException | None     = None
    _news_groq_rpm_exc: BaseException | None = None

    for chunk in _nchunks(tickers_with_news, _BATCH_NEWS_SIZE):
        batch_num += 1
        log.info(
            f"\n── News Batch {batch_num}/{total_batches}: {', '.join(chunk)}",
            event_type="batch_start", batch=batch_num, total_batches=total_batches,
            tickers=list(chunk),
        )

        ticker_blocks = "\n\n".join(_format_news_block(t, articles_map[t]) for t in chunk)
        prompt = _BATCH_NEWS_PROMPT.format(
            n=len(chunk),
            market_context=market_context,
            ticker_blocks=ticker_blocks,
        )

        analyses: list[dict] = []
        for _attempt in range(2):
            if model_idx >= len(triage_chain):
                groq_idx    = _first_groq_idx(triage_chain)
                _news_retry = False
                if _attempt == 0 and groq_idx is not None and _news_groq_rpm_exc is not None:
                    _ok, _sleep = _groq_should_sleep(_news_groq_rpm_exc)
                    _news_groq_rpm_exc = None
                    if _ok:
                        log.warning(
                            f"  [groq/rpm] Groq hit rate limit earlier — sleeping "
                            f"{_sleep:.0f}s then retrying from Groq…",
                            event_type="rate_limit", provider="groq", wait_secs=_sleep, reason="rpm",
                        )
                        await asyncio.sleep(_sleep)
                        model_idx   = groq_idx
                        _news_retry = True
                    else:
                        log.warning(
                            f"  [groq/tpd] Daily token quota exhausted "
                            f"(wait >{_GROQ_MAX_SLEEP_SECS//60}min) — checkpointing",
                            event_type="rate_limit", provider="groq", reason="tpd",
                        )
                if not _news_retry:
                    remaining = [t for t in tickers_with_news
                                 if t not in completed_news and t not in failed_news]
                    _save_cp(run_date, "news", completed_news, remaining, failed_news,
                             "All models exhausted")
                    if tracker:
                        tracker.error_phase("news", len(completed_news), len(failed_news),
                                            "all models exhausted")
                    log.error(
                        f"  [ALL MODELS EXHAUSTED] Saved checkpoint — "
                        f"{len(remaining)} news tickers will resume on next run.",
                        event_type="checkpoint_save", remaining=len(remaining), phase_name="news",
                    )
                    return False

            while model_idx < len(triage_chain):
                model_id, provider, label = triage_chain[model_idx]
                log.info(
                    f"  ⟳ [news/batch] trying {label} ({provider})  "
                    f"[{model_idx+1}/{len(triage_chain)}]",
                    event_type="llm_call_start", model=label, provider=provider,
                    model_idx=model_idx, chain_len=len(triage_chain),
                )
                try:
                    resp = await _litellm.acompletion(
                        model=model_id,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.0,
                        max_tokens=2000,
                    )
                    raw_text = resp.choices[0].message.content or ""
                    analyses = _extract_json_array(raw_text)
                    if not analyses:
                        log.warning(
                            f"  [warn] News batch {batch_num}: no JSON array, trying next model…",
                            event_type="warning",
                        )
                        model_idx += 1
                        continue
                    log.info(
                        f"  [{label}] News batch {batch_num}: {len(analyses)} analyses returned",
                        event_type="llm_call_success", model=label, provider=provider,
                        count=len(analyses),
                    )
                    if provider == "google":
                        GEMINI_COUNTER.record(model_id)
                    break
                except Exception as exc:
                    _news_last_exc = exc
                    if _is_groq_rpm_error(exc):
                        _news_groq_rpm_exc = exc
                    if _is_context_too_large(exc):
                        log.warning(
                            f"  ⚠ {label}: prompt too large — retrying {len(chunk)} tickers "
                            f"individually…",
                            event_type="llm_call_error", model=label, provider=provider,
                            error_type=type(exc).__name__, error=str(exc)[:200],
                        )
                        for st in chunk:
                            sp = _BATCH_NEWS_PROMPT.format(
                                n=1, market_context=market_context,
                                ticker_blocks=_format_news_block(st, articles_map[st]),
                            )
                            try:
                                sr = await _litellm.acompletion(
                                    model=model_id,
                                    messages=[{"role": "user", "content": sp}],
                                    temperature=0.0, max_tokens=600,
                                )
                                sd = _extract_json_array(sr.choices[0].message.content or "")
                                if sd:
                                    analyses.extend(sd)
                                else:
                                    failed_news.append(st)
                            except Exception:
                                failed_news.append(st)
                        break
                    if _is_failover_error(exc):
                        log.warning(
                            f"  ⚠ {label} failed ({type(exc).__name__}: "
                            f"{str(exc)[:300]}). Falling back…",
                            event_type="llm_call_error", model=label, provider=provider,
                            error_type=type(exc).__name__, error=str(exc)[:200],
                        )
                        model_idx += 1
                        continue
                    log.error(f"  [error] News batch {batch_num} failed: {exc}", event_type="error")
                    failed_news.extend(t for t in chunk if t not in completed_news)
                    break
            else:
                groq_idx = _first_groq_idx(triage_chain)
                if _attempt == 0 and groq_idx is not None and _news_groq_rpm_exc is not None:
                    _ok, _sleep = _groq_should_sleep(_news_groq_rpm_exc)
                    _news_groq_rpm_exc = None
                    if _ok:
                        log.warning(
                            f"  [groq/rpm] Groq hit rate limit earlier — sleeping "
                            f"{_sleep:.0f}s then retrying from Groq…",
                            event_type="rate_limit", provider="groq", wait_secs=_sleep, reason="rpm",
                        )
                        await asyncio.sleep(_sleep)
                        model_idx = groq_idx
                        continue
                    else:
                        log.warning(
                            f"  [groq/tpd] Daily token quota exhausted "
                            f"(wait >{_GROQ_MAX_SLEEP_SECS//60}min) — checkpointing",
                            event_type="rate_limit", provider="groq", reason="tpd",
                        )
                remaining = [t for t in tickers_with_news
                             if t not in completed_news and t not in failed_news]
                _save_cp(run_date, "news", completed_news, remaining, failed_news,
                         "All models exhausted")
                if tracker:
                    tracker.error_phase("news", len(completed_news), len(failed_news),
                                        "all models exhausted")
                log.error(
                    f"  [ALL MODELS EXHAUSTED] Saved checkpoint — "
                    f"{len(remaining)} news tickers will resume on next run.",
                    event_type="checkpoint_save", remaining=len(remaining), phase_name="news",
                )
                return False
            if tracker:
                tracker.tick("news", len(completed_news), len(tickers_with_news),
                             ticker=chunk[-1] if chunk else "",
                             note=f"batch {batch_num}/{total_batches}")
            break

        analysis_by_ticker = {
            a.get("ticker", "").upper(): a
            for a in analyses
            if isinstance(a, dict) and a.get("ticker")
        }
        model_name_used, model_prov_used, label_used = (
            triage_chain[model_idx] if model_idx < len(triage_chain) else triage_chain[-1]
        )
        for ticker in chunk:
            if ticker in failed_news:
                continue
            data = analysis_by_ticker.get(ticker)
            if not data:
                log.warning(f"  [warn] No analysis returned for {ticker}", event_type="warning")
                failed_news.append(ticker)
                continue
            try:
                save_ticker_news(
                    ticker=ticker,
                    headline_1=data.get("headline_1", ""),
                    headline_2=data.get("headline_2", ""),
                    sentiment=data.get("sentiment", "NEUTRAL"),
                    sentiment_score=float(data.get("sentiment_score") or 0.0),
                    top_themes=data.get("top_themes", []),
                    trending=data.get("trending", ""),
                    source=_source_by_ticker.get(ticker, "finnhub"),
                )
                update_ticker_news_model(ticker, model_name_used, model_prov_used)
                _save_art_hashes(ticker, articles_map.get(ticker, []))
                log.info(
                    f"  [{label_used}] news saved for {ticker}",
                    event_type="db_write", ticker=ticker, action="saved", model=label_used,
                )
                completed_news.append(ticker)
            except Exception as exc:
                log.warning(
                    f"  [warn] Could not save news for {ticker}: {exc}",
                    event_type="warning",
                )
                failed_news.append(ticker)

    _clear_news_cp("news")
    if tracker:
        tracker.finish_phase("news", len(completed_news), len(failed_news))
    log.info(
        f"\n  News phase done — {len(completed_news)} processed, {len(failed_news)} failed"
        f"  ({total_batches} LLM batch call{'s' if total_batches != 1 else ''} "
        f"vs {len(tickers_with_news)} in single-ticker mode)",
        event_type="phase_end", saved=len(completed_news), failed=len(failed_news),
    )
    return True
