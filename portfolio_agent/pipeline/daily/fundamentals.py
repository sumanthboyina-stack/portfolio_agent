"""Fundamentals phase — EDGAR check + LLM analysis + DB upsert."""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

from portfolio_agent.log import get_logger as _get_logger
from portfolio_agent.pipeline.failover import (
    _is_failover_error,
    _is_groq_rpm_error,
    _first_groq_idx,
    _groq_should_sleep,
    _GROQ_MAX_SLEEP_SECS,
)
from portfolio_agent.pipeline.json_parsing import _extract_json_array
from portfolio_agent.pipeline.prompt_templates import _BATCH_SIZE, _BATCH_FUND_PROMPT


def _format_data_block(data: dict) -> str:
    """Format one ticker's prefetched bundle as a compact text block for the batch prompt."""
    ticker = data["ticker"]
    lines  = [f"=== {ticker} ==="]
    filing = data.get("latest_10k")
    if filing:
        lines.append(f"Latest 10-K: {filing['date']}")

    inc = data.get("income", {})
    bal = data.get("balance", {})
    cf  = data.get("cashflow", {})

    def _series_rows(series_dict: dict) -> str:
        rows: list[str] = []
        periods: dict[str, dict] = {}
        for key, entries in series_dict.items():
            for entry in (entries or []):
                p = entry.get("period", "?")
                if p not in periods:
                    periods[p] = {}
                periods[p][key] = entry.get("value")
        for period in sorted(periods.keys(), reverse=True)[:4]:
            vals = "  ".join(
                f"{k}={periods[period].get(k, 'n/a')}"
                for k in series_dict.keys()
                if periods[period].get(k) is not None
            )
            rows.append(f"  {period}: {vals}")
        return "\n".join(rows) if rows else "  (no data)"

    lines.append("Income Statement:")
    lines.append(_series_rows(inc))
    lines.append("Balance Sheet:")
    lines.append(_series_rows(bal))
    lines.append("Cash Flow:")
    lines.append(_series_rows(cf))
    return "\n".join(lines)


def _fetch_edgar_data(
    all_tickers: list[str],
    always_run: set[str],
    yesterday_pending: list[str],
    log,
):
    """Fetch EDGAR filings, determine which tickers need refresh, and prefetch financial data.

    Returns (to_refresh, prefetch, has_edgar, skipped_no_edgar, skipped_count, info_map).
    """
    from portfolio_agent.tools.edgar_check import batch_check_filings, _load_cik_map
    from portfolio_agent.tools.fundamentals_db import needs_refresh
    from portfolio_agent.tools.edgar import batch_prefetch_fundamentals, get_fundamentals_bundle

    log.info(
        f"\n  Checking EDGAR for new filings ({len(all_tickers)} tickers, max 4 workers)…",
        event_type="fetch_start",
    )
    filing_info = batch_check_filings(all_tickers)

    to_refresh: list[tuple[str, str, dict]] = []
    seen: set[str] = set()

    for ticker in yesterday_pending:
        if ticker not in seen:
            info = filing_info.get(ticker, {})
            to_refresh.append((ticker, "resume", info))
            seen.add(ticker)

    for ticker in all_tickers:
        if ticker in seen:
            continue
        info         = filing_info.get(ticker)
        latest_date  = info["filing_date"] if info else None
        refresh, reason = needs_refresh(ticker, latest_date)
        if refresh or ticker in always_run:
            to_refresh.append((ticker, reason if refresh else "forced", info or {}))
            seen.add(ticker)

    skipped_count = len(all_tickers) - len(to_refresh) + len(yesterday_pending)
    log.info(
        f"  Fundamentals: {len(to_refresh)} to process  |  {skipped_count} current (skipped)",
        event_type="summary",
    )

    info_map = {t: i for t, _, i in to_refresh}

    tickers_to_fetch = [t for t, _, _ in to_refresh]
    log.info(
        f"\n  Pre-fetching financial data for {len(tickers_to_fetch)} tickers (parallel EDGAR, no LLM)…",
        event_type="fetch_start",
    )
    cik_map  = _load_cik_map()
    prefetch = batch_prefetch_fundamentals(tickers_to_fetch, cik_map, max_workers=4)

    no_edgar  = [t for t in tickers_to_fetch if prefetch.get(t, {}).get("error")]
    has_edgar = [t for t in tickers_to_fetch if not prefetch.get(t, {}).get("error")]
    skipped_no_edgar: list[str] = []
    if no_edgar:
        log.info(
            f"  [skip] {len(no_edgar)} tickers have no EDGAR data "
            f"(ETFs/foreign): {', '.join(no_edgar[:8])}{'…' if len(no_edgar)>8 else ''}",
            event_type="summary",
        )
        skipped_no_edgar = no_edgar

    log.info(
        f"  Pre-fetch done — {len(has_edgar)} with data, {len(no_edgar)} skipped",
        event_type="fetch_start",
    )

    return to_refresh, prefetch, has_edgar, skipped_no_edgar, skipped_count, info_map


async def _run_fundamentals_llm_batches(
    has_edgar: list[str],
    prefetch: dict,
    flash_chain: list,
    run_date: str,
    completed: list[str],
    failed: list[str],
    tracker,
    to_refresh: list[tuple[str, str, dict]],
    log,
) -> tuple[dict[str, list[dict]], bool]:
    """Run the batch LLM processing loop with failover, Groq RPM/TPD handling, and checkpointing.

    Returns (batch_analyses_by_ticker, checkpoint_done).
    batch_analyses_by_ticker maps ticker -> list of analysis dicts from the LLM for that batch.
    checkpoint_done is True if we had to checkpoint and should return False to the caller.
    """
    from portfolio_agent.tools.checkpoint import save_checkpoint
    import litellm as _litellm

    def _chunks(lst, n):
        for k in range(0, len(lst), n):
            yield lst[k: k + n]

    model_idx   = 0
    batch_num       = 0
    total_batches   = (len(has_edgar) + _BATCH_SIZE - 1) // _BATCH_SIZE
    _fund_last_exc: BaseException | None     = None
    _fund_groq_rpm_exc: BaseException | None = None

    # Maps each ticker to its analysis dict returned from LLM
    all_batch_results: list[tuple[list[str], list[dict], bool]] = []

    for batch_tickers in _chunks(has_edgar, _BATCH_SIZE):
        batch_num += 1
        log.info(
            f"\n── Batch {batch_num}/{total_batches}: {', '.join(batch_tickers)}",
            event_type="batch_start", batch=batch_num, total_batches=total_batches,
            tickers=list(batch_tickers),
        )

        data_blocks = "\n\n".join(_format_data_block(prefetch[t]) for t in batch_tickers)
        prompt      = _BATCH_FUND_PROMPT.format(n=len(batch_tickers), data_blocks=data_blocks)

        analyses: list[dict] = []
        _batch_hard_failed = False
        for _attempt in range(2):
            if model_idx >= len(flash_chain):
                groq_idx     = _first_groq_idx(flash_chain)
                _fund_retry  = False
                if _attempt == 0 and groq_idx is not None and _fund_groq_rpm_exc is not None:
                    _ok, _sleep = _groq_should_sleep(_fund_groq_rpm_exc)
                    _fund_groq_rpm_exc = None
                    if _ok:
                        log.warning(
                            f"  [groq/rpm] Groq hit rate limit earlier — sleeping "
                            f"{_sleep:.0f}s then retrying from Groq…",
                            event_type="rate_limit", provider="groq", wait_secs=_sleep, reason="rpm",
                        )
                        await asyncio.sleep(_sleep)
                        model_idx   = groq_idx
                        _fund_retry = True
                    else:
                        log.warning(
                            f"  [groq/tpd] Daily token quota exhausted "
                            f"(wait >{_GROQ_MAX_SLEEP_SECS//60}min) — checkpointing",
                            event_type="rate_limit", provider="groq", reason="tpd",
                        )
                if not _fund_retry:
                    remaining = [t for t in has_edgar if t not in completed and t not in failed]
                    save_checkpoint(run_date, "fundamentals", completed, remaining, failed,
                                    "All models exhausted")
                    log.error(
                        f"  [ALL MODELS EXHAUSTED] Saved checkpoint — "
                        f"{len(remaining)} tickers will resume on next run.",
                        event_type="checkpoint_save", remaining=len(remaining),
                        phase_name="fundamentals",
                    )
                    return all_batch_results, True

            while model_idx < len(flash_chain):
                model_id, provider, label = flash_chain[model_idx]
                log.info(
                    f"  ⟳ [fundamentals/batch] trying {label} ({provider})  "
                    f"[{model_idx+1}/{len(flash_chain)}]",
                    event_type="llm_call_start", model=label, provider=provider,
                    model_idx=model_idx, chain_len=len(flash_chain),
                )
                try:
                    resp = await _litellm.acompletion(
                        model=model_id,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.0,
                        max_tokens=3000,
                    )
                    raw_text = resp.choices[0].message.content or ""
                    analyses = _extract_json_array(raw_text)
                    if not analyses:
                        log.warning(
                            f"  [warn] Batch {batch_num}: no JSON array in response, "
                            f"retrying with next model…",
                            event_type="warning",
                        )
                        model_idx += 1
                        continue
                    log.info(
                        f"  [{label}] Batch {batch_num}: {len(analyses)} analyses returned",
                        event_type="llm_call_success", model=label, provider=provider,
                        count=len(analyses),
                    )
                    break
                except Exception as exc:
                    _fund_last_exc = exc
                    if _is_groq_rpm_error(exc):
                        _fund_groq_rpm_exc = exc
                    if _is_failover_error(exc):
                        log.warning(
                            f"  ⚠ {label} failed ({type(exc).__name__}: "
                            f"{str(exc)[:300]}). Falling back…",
                            event_type="llm_call_error", model=label, provider=provider,
                            error_type=type(exc).__name__, error=str(exc)[:200],
                        )
                        model_idx += 1
                        continue
                    log.error(f"  [error] Batch {batch_num} failed: {exc}", event_type="error")
                    failed.extend(t for t in batch_tickers if t not in completed)
                    _batch_hard_failed = True
                    break
            else:
                groq_idx = _first_groq_idx(flash_chain)
                if _attempt == 0 and groq_idx is not None and _fund_groq_rpm_exc is not None:
                    _ok, _sleep = _groq_should_sleep(_fund_groq_rpm_exc)
                    _fund_groq_rpm_exc = None
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
                remaining = [t for t in has_edgar if t not in completed and t not in failed]
                save_checkpoint(run_date, "fundamentals", completed, remaining, failed,
                                "All models exhausted")
                if tracker:
                    tracker.error_phase("fundamentals", len(completed), len(failed),
                                        "all models exhausted")
                log.error(
                    f"  [ALL MODELS EXHAUSTED] Saved checkpoint — "
                    f"{len(remaining)} tickers will resume on next run.",
                    event_type="checkpoint_save", remaining=len(remaining),
                    phase_name="fundamentals",
                )
                return all_batch_results, True
            if tracker:
                tracker.tick("fundamentals", len(completed), len(to_refresh),
                             note=f"batch {batch_num}/{total_batches}")
            break

        # Carry batch results (including hard-failed flag) to the persist phase
        all_batch_results.append((batch_tickers, analyses, _batch_hard_failed))

    return all_batch_results, False


def _persist_fundamentals(
    all_batch_results: list[tuple[list[str], list[dict], bool]],
    flash_chain: list,
    model_idx: int,
    info_map: dict,
    log,
) -> tuple[list[str], list[str]]:
    """Parse LLM analyses and upsert each ticker's fundamentals into the DB.

    Returns (completed, failed).
    """
    from portfolio_agent.tools.fundamentals_db import upsert_fundamentals

    completed: list[str] = []
    failed:    list[str] = []

    # Recover label/provider for DB writes — use last known model index
    label    = flash_chain[model_idx][2] if model_idx < len(flash_chain) else ""
    provider = flash_chain[model_idx][1] if model_idx < len(flash_chain) else ""

    for batch_tickers, analyses, _batch_hard_failed in all_batch_results:
        if _batch_hard_failed:
            continue

        analysis_by_ticker = {
            a.get("ticker", "").upper(): a
            for a in analyses
            if isinstance(a, dict) and a.get("ticker")
        }
        model_name, model_prov, _ = (
            flash_chain[model_idx] if model_idx < len(flash_chain)
            else (label, provider, label)
        )

        for ticker in batch_tickers:
            data = analysis_by_ticker.get(ticker)
            info = info_map.get(ticker) or {}
            if not data:
                log.warning(
                    f"  [warn] No analysis returned for {ticker} in batch",
                    event_type="warning",
                )
                failed.append(ticker)
                continue
            try:
                result = upsert_fundamentals(
                    ticker=ticker,
                    as_of_date=info.get("period_of_report") or date.today().isoformat(),
                    filing_type=info.get("filing_type", ""),
                    filing_date=info.get("filing_date", ""),
                    revenue_growth_yoy_pct=data.get("revenue_growth_yoy_pct"),
                    net_margin=data.get("net_margin"),
                    fcf=data.get("fcf"),
                    debt_to_equity=data.get("debt_to_equity"),
                    fundamental_score=data.get("fundamental_score"),
                    key_strengths=data.get("key_strengths"),
                    key_risks=data.get("key_risks"),
                    summary=data.get("summary", ""),
                    raw_filing_ref=info.get("raw_filing_ref", ""),
                    model_name=label,
                    model_provider=provider,
                )
                log.info(
                    f"  DB: fundamentals {result.get('action','?')} for {ticker} [{label}]",
                    event_type="db_write", ticker=ticker, action=result.get("action", "?"),
                    model=label,
                )
                completed.append(ticker)
            except Exception as exc:
                log.warning(
                    f"  [warn] Could not save fundamentals for {ticker}: {exc}",
                    event_type="warning",
                )
                failed.append(ticker)

    return completed, failed


async def _run_daily_fundamentals(
    all_tickers: list[str],
    always_run: set[str],
    tracker=None,
) -> bool:
    """Phase 1 of the daily job — EDGAR check + fundamentals agent + DB upsert."""
    log = _get_logger("fundamentals")
    from portfolio_agent.tools.edgar_check import batch_check_filings
    from portfolio_agent.tools.fundamentals_db import needs_refresh, upsert_fundamentals
    from portfolio_agent.tools.checkpoint import (
        pending_from_yesterday, save_checkpoint, clear_phase,
    )
    from portfolio_agent._models import FAILOVER_CHAINS
    import litellm as _litellm

    run_date = date.today().isoformat()

    from portfolio_agent.tools.pipeline_skip import get_skip_tickers as _skip_tickers, log_skip as _log_skip
    _fs = _skip_tickers("fundamentals")
    _skipped_restricted = [t for t in all_tickers if t in _fs]
    if _skipped_restricted:
        _log_skip("fundamentals", _skipped_restricted)
        all_tickers = [t for t in all_tickers if t not in _fs]

    yesterday_pending = pending_from_yesterday("fundamentals")
    if yesterday_pending:
        log.info(
            f"  [resume] {len(yesterday_pending)} tickers carried over from previous run: "
            f"{', '.join(yesterday_pending[:10])}{'…' if len(yesterday_pending) > 10 else ''}",
            event_type="resume",
        )

    # --- Phase 1: EDGAR fetch + needs-refresh filtering + prefetch ---
    to_refresh, prefetch, has_edgar, skipped_no_edgar, skipped_count, info_map = _fetch_edgar_data(
        all_tickers, always_run, yesterday_pending, log,
    )

    if not to_refresh:
        clear_phase("fundamentals")
        return True

    completed: list[str] = []
    failed:    list[str] = []
    if tracker:
        tracker.start_phase("fundamentals", total=len(to_refresh))

    tickers_to_fetch = [t for t, _, _ in to_refresh]
    flash_chain = FAILOVER_CHAINS.get("flash", [])

    # --- Phase 2: Batch LLM processing with failover + checkpoint ---
    all_batch_results, checkpoint_done = await _run_fundamentals_llm_batches(
        has_edgar=has_edgar,
        prefetch=prefetch,
        flash_chain=flash_chain,
        run_date=run_date,
        completed=completed,
        failed=failed,
        tracker=tracker,
        to_refresh=to_refresh,
        log=log,
    )

    if checkpoint_done:
        return False

    # --- Phase 3: Parse analyses + DB upsert ---
    model_idx = 0  # persist uses the same model_idx bookkeeping; keep at 0 for label resolution
    completed, failed = _persist_fundamentals(
        all_batch_results=all_batch_results,
        flash_chain=flash_chain,
        model_idx=model_idx,
        info_map=info_map,
        log=log,
    )

    total_batches = (len(has_edgar) + _BATCH_SIZE - 1) // _BATCH_SIZE
    clear_phase("fundamentals")
    if tracker:
        tracker.finish_phase("fundamentals", len(completed), len(failed))
    _skip_str = f", {len(skipped_no_edgar)} skipped (no EDGAR)" if skipped_no_edgar else ""
    log.info(
        f"\n  Fundamentals phase done — {len(completed)} saved, {len(failed)} failed"
        f"{_skip_str}"
        f"  ({total_batches} LLM batch call{'s' if total_batches != 1 else ''} "
        f"vs {len(tickers_to_fetch)} in single-ticker mode)",
        event_type="phase_end", saved=len(completed), failed=len(failed),
    )
    return True
