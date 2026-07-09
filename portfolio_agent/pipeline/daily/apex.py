"""APEX phase — per-ticker dual-run predictions."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed
from datetime import date

from portfolio_agent.log import get_logger as _get_logger
from portfolio_agent.pipeline.prompt_templates import _APEX_PROMPT_TEMPLATE, _build_macro_section


def _build_horizons_instruction(horizons: list[int]) -> str:
    """Build the per-horizon guidance block to inject into APEX prompts."""
    guidance = {
        5:  "5d  (~1 week)  — focus on news flow, momentum, short-term catalysts",
        21: "21d (~1 month) — focus on earnings drift, monthly themes, near catalysts",
        63: "63d (~1 quarter) — focus on fundamentals, valuation, full earnings cycle",
    }
    lines = [f"  • {guidance.get(h, f'{h}d horizon')}" for h in sorted(horizons)]
    return "\n".join(lines) if lines else "  • 5d (~1 week) — news flow, momentum"


async def _run_daily_apex(
    portfolio_tickers: list[str],
    tracker=None,
    trigger_map: dict[str, tuple[str, int | None]] | None = None,
    scheduled_horizons_override: list[int] | None = None,
) -> None:
    """
    Phase 4 — per-ticker APEX dual-run predictions.

    trigger_map: {ticker → (trigger_type, trigger_event_id)} for event-driven runs.
    scheduled_horizons_override: pass explicit horizons (event-driven cadence path);
        when None, falls back to get_scheduled_horizons() (legacy / --force-all path).
    """
    log = _get_logger("apex")
    from portfolio_agent.tools.reasoning_tools import get_full_analysis_context
    from portfolio_agent.tools.prediction_db import (
        insert_prediction, get_latest_prediction,
        is_trading_day, get_scheduled_horizons, get_today_horizons,
    )
    from portfolio_agent.tools.yfinance_tools import get_close as _get_close
    from portfolio_agent.tools.apex_dual_run import run_apex_dual as _run_apex_dual

    if trigger_map is None:
        trigger_map = {}

    if not portfolio_tickers:
        log.info("  No portfolio tickers configured — skipping APEX phase.", event_type="info")
        if tracker:
            tracker.start_phase("apex", total=0)
            tracker.finish_phase("apex", 0, 0, note="no portfolio tickers")
        return

    today_date = date.today()
    today      = today_date.isoformat()

    if not is_trading_day(today_date):
        log.info(f"  [{today}] Not a trading day — skipping APEX phase.", event_type="info")
        if tracker:
            tracker.start_phase("apex", total=0)
            tracker.finish_phase("apex", 0, 0, note="not a trading day")
        return

    if scheduled_horizons_override is not None:
        scheduled_horizons = scheduled_horizons_override
    else:
        scheduled_horizons = get_scheduled_horizons(today_date)
    horizons_instr = _build_horizons_instruction(scheduled_horizons)
    log.info(f"  Scheduled horizons today: {scheduled_horizons}", event_type="info")

    seen_pt: set[str] = set()
    to_predict = []
    for ticker in portfolio_tickers:
        if ticker in seen_pt:
            continue
        seen_pt.add(ticker)
        done_horizons = get_today_horizons(ticker, today)
        missing       = [h for h in scheduled_horizons if h not in done_horizons]
        if not missing:
            latest = get_latest_prediction(ticker)
            log.info(
                f"  [skip] {ticker} — all horizons {scheduled_horizons} already saved today "
                f"({latest['recommendation'] if latest else '?'})",
                event_type="info",
            )
        else:
            if done_horizons:
                log.info(
                    f"  {ticker} — adding horizons {missing} (already have {list(done_horizons)})",
                    event_type="info",
                )
            to_predict.append(ticker)

    if not to_predict:
        log.info(
            f"  All {len(portfolio_tickers)} portfolio tickers already predicted today.",
            event_type="info",
        )
        if tracker:
            tracker.start_phase("apex", total=0)
            tracker.finish_phase(
                "apex", 0, 0,
                note=f"all {len(portfolio_tickers)} tickers already predicted today",
            )
        return

    log.info(
        f"  {len(to_predict)} / {len(portfolio_tickers)} portfolio tickers → APEX reasoning",
        event_type="summary",
    )

    log.info(
        f"  Pre-fetching APEX contexts for {len(to_predict)} tickers (parallel)…",
        event_type="fetch_start",
    )
    ctx_map:    dict[str, str]   = {}
    price_map:  dict[str, float] = {}
    ctx_failed: list[str]        = []

    def _fetch_ctx(t: str) -> tuple[str, str | None, float | None]:
        ctx   = None
        price = None
        try:
            ctx = get_full_analysis_context(t, horizons=scheduled_horizons)
        except Exception:
            pass
        try:
            price = _get_close(t, today)
        except Exception:
            pass
        return t, ctx, price

    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = {pool.submit(_fetch_ctx, t): t for t in to_predict}
        for f in _as_completed(futs):
            t, ctx, price = f.result()
            if ctx:
                ctx_map[t] = ctx
                if price is not None:
                    price_map[t] = price
            else:
                log.warning(
                    f"  [warn] Context pre-fetch failed for {t} — skipping",
                    event_type="warning",
                )
                ctx_failed.append(t)

    has_ctx = [t for t in to_predict if t in ctx_map]
    log.info(
        f"  Pre-fetch done — {len(has_ctx)} ready, {len(ctx_failed)} failed",
        event_type="fetch_start",
    )

    completed: list[str] = []
    failed:    list[str] = list(ctx_failed)

    if tracker:
        tracker.start_phase("apex", total=len(has_ctx))

    for ticker_num, ticker in enumerate(has_ctx, 1):
        log.info(
            f"\n── APEX [{ticker_num}/{len(has_ctx)}]: {ticker}",
            event_type="batch_start", ticker=ticker,
            batch=ticker_num, total_batches=len(has_ctx),
        )

        # Extract macro snapshot from pre-fetched context for the formatted section
        _ctx_parsed: dict = {}
        try:
            _ctx_parsed = json.loads(ctx_map[ticker])
        except Exception:
            pass
        _macro_snap = _ctx_parsed.get("macro_snapshot") or {}

        prompt = _APEX_PROMPT_TEMPLATE.format(
            ticker=ticker,
            ctx_json=ctx_map[ticker],
            macro_section=_build_macro_section(_macro_snap),
            horizons_instruction=horizons_instr,
        )

        result = await _run_apex_dual(ticker, prompt)

        if result is None:
            log.error(
                f"  [ALL APEX MODELS EXHAUSTED] {ticker} — skipping",
                event_type="checkpoint_save", ticker=ticker, phase_name="apex",
            )
            failed.append(ticker)
            if tracker:
                tracker.tick("apex", len(completed), len(has_ctx), ticker=ticker)
            continue

        data             = result.merged
        model_label_used = "+".join(result.model_sources)
        model_prov_used  = (
            "ensemble" if not result.is_single_model
            else result.model_sources[0].split("-")[0].lower()
        )

        try:
            _ctx_res = {}
            try:
                _ctx_res = json.loads(ctx_map.get(ticker, "{}")).get("research") or {}
            except Exception:
                pass

            _common = dict(
                ticker=ticker,
                prediction=data.get("prediction", "NEUTRAL"),
                recommendation=data.get("recommendation", "HOLD"),
                confidence=data.get("confidence"),
                target_price=data.get("target_price"),
                fundamental_score=data.get("fundamental_score"),
                research_score=data.get("research_score"),
                macro_score=data.get("macro_score"),
                news_score=data.get("news_score"),
                composite_score=data.get("composite_score"),
                reasoning=data.get("reasoning", ""),
                panel_summary=data.get("panel_summary", {}),
                model_name=model_label_used,
                model_provider=model_prov_used,
                pt_mean=_ctx_res.get("price_target_avg"),
                pt_median=_ctx_res.get("price_target_median"),
                pt_high=_ctx_res.get("price_target_high"),
                pt_low=_ctx_res.get("price_target_low"),
                pt_num_analysts=_ctx_res.get("num_analysts"),
                pt_current_price=_ctx_res.get("current_price"),
                is_ensemble=not result.is_single_model,
                agreement_score=result.agreement_score,
                is_single_model=result.is_single_model,
                used_fallback=result.used_fallback,
            )

            done_today       = get_today_horizons(ticker, today)
            horizons_data    = data.get("horizons") or []
            horizons_by_days = {
                h.get("horizon_days"): h for h in horizons_data
                if isinstance(h, dict) and h.get("horizon_days")
            }
            saved_count   = 0
            save_errors: list[str] = []
            horizons_needed = [h for h in scheduled_horizons if h not in done_today]

            _trig_type, _trig_ev_id = trigger_map.get(ticker, (None, None))

            for h_days in scheduled_horizons:
                if h_days in done_today:
                    continue
                h_data = horizons_by_days.get(h_days, {})
                dist   = h_data.get("distribution") or {}
                save   = insert_prediction(
                    **_common,
                    horizon_days=h_days,
                    predicted_direction=h_data.get("predicted_direction"),
                    predicted_return_low=h_data.get("predicted_return_low"),
                    predicted_return_high=h_data.get("predicted_return_high"),
                    conviction_score=h_data.get("conviction_score"),
                    reasoning_text=h_data.get("reasoning_text") or data.get("reasoning", ""),
                    start_price=price_map.get(ticker),
                    p_strong_down=dist.get("strong_down"),
                    p_moderate_down=dist.get("moderate_down"),
                    p_flat=dist.get("flat"),
                    p_moderate_up=dist.get("moderate_up"),
                    p_strong_up=dist.get("strong_up"),
                    trigger_type=_trig_type,
                    trigger_event_id=_trig_ev_id,
                )
                if save.get("saved"):
                    saved_count += 1
                else:
                    save_errors.append(f"{h_days}d: {save.get('error', 'unknown error')}")

            rec  = data.get("recommendation", "?")
            conf = data.get("confidence", "?")
            agr  = (
                f"agr={result.agreement_score:.2f}"
                if not result.is_single_model else "single-model"
            )
            log.info(
                f"  [{model_label_used}] APEX: {ticker} → {rec} (conf={conf}, {agr}) "
                f"· {saved_count}/{len(horizons_needed)} horizon row(s) saved",
                event_type="db_write", ticker=ticker, action=rec, model=model_label_used,
            )

            for _err in save_errors:
                log.error(
                    f"  [DB ERROR] APEX {ticker} save failed — {_err}",
                    event_type="error", ticker=ticker,
                )

            if horizons_needed and saved_count == 0:
                failed.append(ticker)
                log.error(
                    f"  [DB ERROR] APEX {ticker}: 0/{len(horizons_needed)} horizons saved "
                    f"— marking as failed",
                    event_type="error", ticker=ticker,
                )
            else:
                completed.append(ticker)

        except Exception as exc:
            log.warning(
                f"  [warn] Could not save APEX prediction for {ticker}: {exc}",
                event_type="warning",
            )
            failed.append(ticker)

        if tracker:
            tracker.tick("apex", len(completed), len(has_ctx), ticker=ticker,
                         note=f"{ticker_num}/{len(has_ctx)}")

    if tracker:
        tracker.finish_phase("apex", len(completed), len(failed))
    log.info(
        f"\n  APEX phase done — {len(completed)} tickers · {len(failed)} failed"
        f"  ({len(has_ctx)} per-ticker dual-run calls, horizons {scheduled_horizons})",
        event_type="phase_end", saved=len(completed), failed=len(failed),
    )
