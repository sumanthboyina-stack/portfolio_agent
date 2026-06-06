"""Research phase — daily raw broker fetch (Track A) + weekly LLM summary (Track B)."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed
from datetime import date

from portfolio_agent.log import get_logger as _get_logger
from portfolio_agent._models import GEMINI_COUNTER
from portfolio_agent.pipeline.failover import (
    _is_failover_error,
    _is_groq_rpm_error,
    _first_groq_idx,
    _groq_should_sleep,
    _GROQ_MAX_SLEEP_SECS,
)
from portfolio_agent.pipeline.json_parsing import _extract_json_array
from portfolio_agent.pipeline.prompt_templates import _BATCH_SIZE, _BATCH_RESEARCH_PROMPT


def _format_research_block(data: dict) -> str:
    """Format one ticker's raw broker data as a compact text block for the batch research prompt."""
    ticker = data.get("ticker", "?")
    lines  = [f"=== {ticker} ==="]

    cons   = data.get("consensus_key", "n/a")
    cmean  = data.get("consensus_mean")
    nana   = data.get("num_analysts")
    cp     = data.get("current_price")
    tavg   = data.get("target_mean")
    thi    = data.get("target_high")
    tlo    = data.get("target_low")
    upside = data.get("upside_to_mean_pct")
    lines.append(
        f"Consensus: {cons}"
        + (f" (mean {cmean}, {nana} analysts)" if cmean else "")
    )
    if cp and tavg:
        lines.append(
            f"Price: ${cp}  |  Target avg ${tavg}"
            + (f" ({upside:+.1f}%)" if upside is not None else "")
            + (f"  high ${thi} / low ${tlo}" if thi and tlo else "")
        )
    lud = data.get("latest_upgrade_date")
    if lud:
        lines.append(f"Latest firm action: {lud}")
    qr = data.get("quarterly_ratings", [])
    if qr:
        lines.append("Quarterly ratings:")
        for row in qr[-4:]:
            lines.append(
                f"  {row.get('period','?')}: "
                f"SB={row.get('strongBuy',0)} B={row.get('buy',0)} "
                f"H={row.get('hold',0)} S={row.get('sell',0)} SS={row.get('strongSell',0)}"
            )
    ru = data.get("recent_upgrades", [])
    if ru:
        lines.append("Recent upgrades/downgrades (newest first):")
        for u in ru[:6]:
            tgt_str = f" → ${u['new_target']}" if u.get("new_target") else ""
            lines.append(
                f"  {u.get('date','?')} {u.get('firm','?')}: "
                f"{u.get('action','?')} {u.get('from_grade','?')} → {u.get('to_grade','?')}"
                + tgt_str
            )
    rt = data.get("rec_trend", [])
    if rt:
        lines.append("Monthly analyst trend (Finnhub, newest first):")
        for m in rt[:6]:
            pct = f" ({m['pct_bullish']}% bullish)" if m.get("pct_bullish") is not None else ""
            lines.append(
                f"  {m.get('period','?')}: "
                f"SB={m.get('strongBuy',0)} B={m.get('buy',0)} "
                f"H={m.get('hold',0)} S={m.get('sell',0)} total={m.get('total',0)}"
                + pct
            )
    fpt = data.get("finnhub_pt")
    if fpt and fpt.get("target_mean"):
        lines.append(
            f"Finnhub PT: mean ${fpt['target_mean']} / "
            f"high ${fpt.get('target_high','n/a')} / low ${fpt.get('target_low','n/a')}"
            + (f"  (as of {fpt['last_updated']})" if fpt.get("last_updated") else "")
        )
    return "\n".join(lines)


async def _run_daily_research(
    all_tickers: list[str],
    always_run: set[str],
    tracker=None,
) -> bool:
    """Two-track research phase: raw broker fetch (Track A) + LLM summary (Track B)."""
    log = _get_logger("research")
    from portfolio_agent.tools.broker_research import get_broker_research
    from portfolio_agent.tools.research_db import (
        upsert_raw_research, upsert_llm_summary, needs_llm_summary,
    )
    from portfolio_agent.tools.checkpoint import (
        pending_from_yesterday, save_checkpoint, clear_phase,
    )
    from portfolio_agent._models import FAILOVER_CHAINS
    import litellm as _litellm

    run_date = date.today().isoformat()

    from portfolio_agent.tools.pipeline_skip import get_skip_tickers as _skip_tickers, log_skip as _log_skip
    _rs = _skip_tickers("research")
    _skipped_restricted = [t for t in all_tickers if t in _rs]
    if _skipped_restricted:
        _log_skip("research", _skipped_restricted)
        all_tickers = [t for t in all_tickers if t not in _rs]

    log.info(
        f"  Track A: fetching raw broker data for {len(all_tickers)} tickers (parallel, no LLM)…",
        event_type="fetch_start",
    )

    def _fetch_and_store(ticker: str) -> tuple[str, bool, str, dict]:
        try:
            d = json.loads(get_broker_research(ticker))
            result = upsert_raw_research(
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
            return ticker, True, result.get("action", "?"), d
        except Exception as exc:
            return ticker, False, str(exc), {}

    raw_data_map: dict[str, dict] = {}
    track_a_failed: list[str] = []

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_fetch_and_store, t): t for t in all_tickers}
        for f in _as_completed(futures):
            ticker, ok, msg, d = f.result()
            if ok:
                raw_data_map[ticker] = d
            else:
                log.warning(
                    f"  [warn] Track A failed for {ticker}: {msg}",
                    event_type="warning",
                )
                track_a_failed.append(ticker)

    log.info(
        f"  Track A done — {len(raw_data_map)} fetched, {len(track_a_failed)} failed",
        event_type="fetch_start",
    )

    yesterday_pending = pending_from_yesterday("research")
    if yesterday_pending:
        log.info(
            f"  [resume] {len(yesterday_pending)} research tickers from previous run: "
            f"{', '.join(yesterday_pending[:10])}{'…' if len(yesterday_pending) > 10 else ''}",
            event_type="resume",
        )

    seen: set[str] = set()
    llm_queue: list[tuple[str, str]] = []

    for ticker in yesterday_pending:
        if ticker not in seen:
            llm_queue.append((ticker, "resume"))
            seen.add(ticker)

    for ticker in all_tickers:
        if ticker in seen:
            continue
        needs, reason = needs_llm_summary(ticker)
        if needs:
            llm_queue.append((ticker, reason))
            seen.add(ticker)

    track_b_skipped = len(all_tickers) - len(llm_queue) + len(yesterday_pending)
    log.info(
        f"  Track B: {len(llm_queue)} need LLM summary  |  {track_b_skipped} current (skipped)",
        event_type="summary",
    )

    if not llm_queue:
        clear_phase("research")
        if tracker:
            tracker.start_phase("research", total=0)
            tracker.finish_phase(
                "research", 0, 0,
                note=f"Track A: {len(raw_data_map)} fetched · all summaries current",
            )
        log.info(
            f"\n  Research phase done — Track A: {len(raw_data_map)} raw fetches, "
            f"Track B: 0 LLM summaries needed",
            event_type="phase_end", saved=0, failed=0,
        )
        return True

    completed: list[str] = []
    failed:    list[str] = []

    llm_with_data    = [(t, r) for t, r in llm_queue if t in raw_data_map]
    if tracker:
        tracker.start_phase("research", total=len(llm_queue))
    llm_without_data = [(t, r) for t, r in llm_queue if t not in raw_data_map]
    if llm_without_data:
        log.warning(
            f"  [warn] {len(llm_without_data)} tickers skipped (no Track A data): "
            f"{', '.join(t for t, _ in llm_without_data[:8])}"
            f"{'…' if len(llm_without_data) > 8 else ''}",
            event_type="warning",
        )
        failed.extend(t for t, _ in llm_without_data)

    flash_chain = FAILOVER_CHAINS.get("flash", [])
    model_idx   = 0

    def _rchunks(lst, n):
        for k in range(0, len(lst), n):
            yield lst[k: k + n]

    batch_num        = 0
    total_batches    = (len(llm_with_data) + _BATCH_SIZE - 1) // _BATCH_SIZE
    _res_last_exc: BaseException | None     = None
    _res_groq_rpm_exc: BaseException | None = None

    for chunk in _rchunks([t for t, _ in llm_with_data], _BATCH_SIZE):
        batch_num += 1
        log.info(
            f"\n── Research Batch {batch_num}/{total_batches}: {', '.join(chunk)}",
            event_type="batch_start", batch=batch_num, total_batches=total_batches,
            tickers=list(chunk),
        )

        data_blocks = "\n\n".join(_format_research_block(raw_data_map[t]) for t in chunk)
        prompt      = _BATCH_RESEARCH_PROMPT.format(n=len(chunk), data_blocks=data_blocks)

        analyses: list[dict] = []
        _res_batch_hard_failed = False
        for _attempt in range(2):
            if model_idx >= len(flash_chain):
                groq_idx   = _first_groq_idx(flash_chain)
                _res_retry = False
                if _attempt == 0 and groq_idx is not None and _res_groq_rpm_exc is not None:
                    _ok, _sleep = _groq_should_sleep(_res_groq_rpm_exc)
                    _res_groq_rpm_exc = None
                    if _ok:
                        log.warning(
                            f"  [groq/rpm] Groq hit rate limit earlier — sleeping "
                            f"{_sleep:.0f}s then retrying from Groq…",
                            event_type="rate_limit", provider="groq", wait_secs=_sleep, reason="rpm",
                        )
                        await asyncio.sleep(_sleep)
                        model_idx  = groq_idx
                        _res_retry = True
                    else:
                        log.warning(
                            f"  [groq/tpd] Daily token quota exhausted "
                            f"(wait >{_GROQ_MAX_SLEEP_SECS//60}min) — checkpointing",
                            event_type="rate_limit", provider="groq", reason="tpd",
                        )
                if not _res_retry:
                    remaining = [t for t, _ in llm_with_data
                                 if t not in completed and t not in failed]
                    save_checkpoint(run_date, "research", completed, remaining, failed,
                                    "All models exhausted")
                    if tracker:
                        tracker.error_phase("research", len(completed), len(failed),
                                            "all models exhausted")
                    log.error(
                        f"  [ALL MODELS EXHAUSTED] Saved checkpoint — "
                        f"{len(remaining)} research tickers will resume on next run.",
                        event_type="checkpoint_save", remaining=len(remaining), phase_name="research",
                    )
                    return False

            while model_idx < len(flash_chain):
                model_id, provider, label = flash_chain[model_idx]
                log.info(
                    f"  ⟳ [research/batch] trying {label} ({provider})  "
                    f"[{model_idx+1}/{len(flash_chain)}]",
                    event_type="llm_call_start", model=label, provider=provider,
                    model_idx=model_idx, chain_len=len(flash_chain),
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
                            f"  [warn] Research batch {batch_num}: no JSON array, "
                            f"trying next model…",
                            event_type="warning",
                        )
                        model_idx += 1
                        continue
                    log.info(
                        f"  [{label}] Research batch {batch_num}: "
                        f"{len(analyses)} summaries returned",
                        event_type="llm_call_success", model=label, provider=provider,
                        count=len(analyses),
                    )
                    if provider == "google":
                        GEMINI_COUNTER.record(model_id)
                    break
                except Exception as exc:
                    _res_last_exc = exc
                    if _is_groq_rpm_error(exc):
                        _res_groq_rpm_exc = exc
                    if _is_failover_error(exc):
                        log.warning(
                            f"  ⚠ {label} failed ({type(exc).__name__}: "
                            f"{str(exc)[:300]}). Falling back…",
                            event_type="llm_call_error", model=label, provider=provider,
                            error_type=type(exc).__name__, error=str(exc)[:200],
                        )
                        model_idx += 1
                        continue
                    log.error(
                        f"  [error] Research batch {batch_num} failed: {exc}",
                        event_type="error",
                    )
                    failed.extend(t for t in chunk if t not in completed)
                    _res_batch_hard_failed = True
                    break
            else:
                groq_idx = _first_groq_idx(flash_chain)
                if _attempt == 0 and groq_idx is not None and _res_groq_rpm_exc is not None:
                    _ok, _sleep = _groq_should_sleep(_res_groq_rpm_exc)
                    _res_groq_rpm_exc = None
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
                remaining = [t for t, _ in llm_with_data
                             if t not in completed and t not in failed]
                save_checkpoint(run_date, "research", completed, remaining, failed,
                                "All models exhausted")
                if tracker:
                    tracker.error_phase("research", len(completed), len(failed),
                                        "all models exhausted")
                log.error(
                    f"  [ALL MODELS EXHAUSTED] Saved checkpoint — "
                    f"{len(remaining)} research tickers will resume on next run.",
                    event_type="checkpoint_save", remaining=len(remaining), phase_name="research",
                )
                return False
            if tracker:
                tracker.tick("research", len(completed), len(llm_with_data),
                             note=f"batch {batch_num}/{total_batches}")
            break

        if _res_batch_hard_failed:
            continue

        analysis_by_ticker = {
            a.get("ticker", "").upper(): a
            for a in analyses
            if isinstance(a, dict) and a.get("ticker")
        }
        model_name_used, model_prov_used, label_used = (
            flash_chain[model_idx] if model_idx < len(flash_chain)
            else (label, provider, label)
        )
        for ticker in chunk:
            data = analysis_by_ticker.get(ticker)
            if not data:
                log.warning(
                    f"  [warn] No summary returned for {ticker} in research batch",
                    event_type="warning",
                )
                failed.append(ticker)
                continue
            try:
                result = upsert_llm_summary(
                    ticker=ticker,
                    highlights=data.get("highlights", []),
                    research_score=data.get("research_score"),
                    summary=data.get("summary", ""),
                    model_name=model_name_used,
                    model_provider=model_prov_used,
                )
                log.info(
                    f"  DB: research {result.get('action','?')} for {ticker} [{label_used}]",
                    event_type="db_write", ticker=ticker, action=result.get("action", "?"),
                    model=label_used,
                )
                completed.append(ticker)
            except Exception as exc:
                log.warning(
                    f"  [warn] Could not save research summary for {ticker}: {exc}",
                    event_type="warning",
                )
                failed.append(ticker)

    clear_phase("research")
    if tracker:
        tracker.finish_phase("research", len(completed), len(failed))
    log.info(
        f"\n  Research phase done — "
        f"Track A: {len(raw_data_map)} raw fetches  |  "
        f"Track B: {len(completed)} LLM summaries, {len(failed)} failed"
        f"  ({total_batches} LLM batch call{'s' if total_batches != 1 else ''} "
        f"vs {len(llm_with_data)} in single-ticker mode)",
        event_type="phase_end", saved=len(completed), failed=len(failed),
    )
    return True
