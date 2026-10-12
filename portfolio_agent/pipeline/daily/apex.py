"""APEX phase — per-ticker predictions from the champion model plan (see tools.apex_dual_run)."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed
from datetime import date

from portfolio_agent.log import get_logger as _get_logger
from portfolio_agent.pipeline.prompt_templates import _APEX_PROMPT_TEMPLATE, _build_macro_section


def _build_horizons_instruction(horizons: list[int], edges: dict | None = None) -> str:
    """
    Build the per-horizon guidance block to inject into APEX prompts. Each horizon states ITS OWN return-bucket ranges, rendered
    by tools.bucket_edges.describe() from the very edges the scorer uses (a 250-day "flat" is not a 5-day "flat"). `edges` is
    {horizon: Edges} for the version in force; None reads the current ones from the settings.
    """
    from portfolio_agent.tools import bucket_edges as be
    if edges is None:
        from portfolio_agent.tools.engine_settings_db import current_bucket_edges
        edges = current_bucket_edges()[1]
    guidance = {
        5:  "5d  (~1 week)  — focus on news flow, momentum, short-term catalysts",
        21: "21d (~1 month) — focus on earnings drift, monthly themes, near catalysts",
        63: "63d (~1 quarter) — focus on fundamentals, valuation, full earnings cycle",
        250: "250d (~1 year) — focus on long-run fundamentals, valuation and structural trends, not short-term news",
    }
    lines = []
    for h in sorted(horizons):
        lines.append(f"  • {guidance.get(h, f'{h}d horizon')}")
        lines.append(f"      return buckets for the {h}d distribution: {be.describe(be.edges_for(h, be.SEED_VERSION, table=edges))}")
    return "\n".join(lines) if lines else "  • 5d (~1 week) — news flow, momentum"


def _apply_conviction_guardrails(
    predicted_direction: str | None,
    conviction_score: float | None,
    has_news: bool,
    trailing_10d_return: float | None,
) -> tuple[float | None, list[str]]:
    """
    Deterministic post-hoc caps on conviction, applied after the LLM responds
    and before the prediction is stored. Enforced in code rather than prompt
    instructions because these are two data-confirmed money-losing patterns:
      - "no news, bullish, high conviction" runs of positive research sentiment
        filling a news gap (37.9% UP accuracy in the worst-miss sample)
      - "buy the dip" bounce calls on tickers already declining sharply
        (trailing 10d return -2.34% for wrong UP calls vs -0.32% for correct)
    Mirrors the data_caps pattern in weight_engine.py (capping fundamentals/
    research scores to 5 when data is missing), extended to conviction.
    Thresholds/caps are administrator-configurable (Settings → Engine Weights,
    portfolio_agent.tools.engine_settings_db); these numbers are the seed defaults.
    """
    from portfolio_agent.tools.weight_engine import effective_settings

    flags: list[str] = []
    if conviction_score is None or predicted_direction != "UP":
        return conviction_score, flags

    s = effective_settings()
    capped = conviction_score
    if not has_news and capped >= s["no_news_conviction_threshold"]:
        capped = min(capped, s["no_news_cap"])
        flags.append("no_news_bullish_capped")

    if trailing_10d_return is not None and trailing_10d_return < s["bounce_return_threshold"]:
        capped = min(capped, s["bounce_cap"])
        flags.append("bounce_thesis_capped")

    return capped, flags


async def _run_daily_apex(
    portfolio_tickers: list[str],
    tracker=None,
    trigger_map: dict[str, tuple[str, int | None]] | None = None,
    scheduled_horizons_override: list[int] | None = None,
) -> None:
    """
    Phase 4 — per-ticker APEX predictions: the champion model (fallbacks on
    failure), plus an optional challenger shadow run on a sampled subset.

    trigger_map: {ticker → (trigger_type, trigger_event_id)} for event-driven runs.
    scheduled_horizons_override: pass explicit horizons (event-driven cadence path);
        when None, falls back to get_scheduled_horizons() (legacy / --force-all path).
    """
    log = _get_logger("apex")
    from portfolio_agent.tools.market_context import build_market_context
    from portfolio_agent.tools.forecast_writer import (
        GUARDRAIL_VERSION, ForecastProvenance, prompt_policy_version, write_shared_forecast,
    )
    from portfolio_agent.tools.prediction_db import (
        SHARED_SCOPES, get_latest_prediction,
        is_trading_day, get_scheduled_horizons, get_today_horizons, event_already_processed,
    )
    from portfolio_agent.tools.yfinance_tools import (
        get_close as _get_close,
        get_trailing_return as _get_trailing_return,
    )
    from portfolio_agent.tools.apex_dual_run import run_apex as _run_apex
    from portfolio_agent.tools.apex_dual_run import challenger_selected, load_model_plan, run_challenger
    from portfolio_agent.domain import FUND_INSTRUMENT_TYPES, NO_PREDICTION_TYPES, InstrumentType
    from portfolio_agent.services.context import system_context
    from portfolio_agent.services.instrument_classifier import ensure_classified

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
    # The return-bucket edges in force NOW (version + per-horizon table): they are in the prompt, so they are what every row
    # saved by this run is stored under — and scored with, whatever the edges are changed to later. Read once, serially.
    from portfolio_agent.tools.engine_settings_db import current_bucket_edges
    edges_version, edges_table = current_bucket_edges()
    horizons_instr = _build_horizons_instruction(scheduled_horizons, edges_table)
    log.info(f"  Scheduled horizons today: {scheduled_horizons}", event_type="info")

    # One plan per phase: decides challenger sampling below. run_apex resolves the
    # same engine_settings itself, so champion/fallback order is identical.
    model_plan = load_model_plan()
    log.info(f"  APEX model plan: {model_plan.describe()}", event_type="apex_model_plan",
             champion=model_plan.champion[2], fallbacks=[m[2] for m in model_plan.fallbacks],
             challenger=model_plan.challenger[2] if model_plan.challenger else None,
             challenger_pct=model_plan.challenger_pct)

    # What kind of security is each ticker? Watchlist / trending tickers nobody holds get an instrument row and a type
    # here too (lookups run before any transaction). The type decides routing: cash and ticker-less funds are never
    # predicted, funds are predicted without company fundamentals / valuation, everything else is the stock path.
    itypes = ensure_classified(system_context("apex"), list(portfolio_tickers))
    skipped_by_type: list[str] = []

    # Shared fund data (holdings / sector mix / expense ratio) is kept fresh here, TTL-guarded and outside any
    # transaction; a failure is a reason code on the fund, never a reason to stop the phase.
    _fund_tickers = [t for t in portfolio_tickers if itypes.get(t) in {x.value for x in FUND_INSTRUMENT_TYPES}]
    if _fund_tickers:
        try:
            from portfolio_agent.services.fund_data_service import refresh_fund_data
            refresh_fund_data(system_context("apex"), _fund_tickers)
        except Exception as _exc:
            log.warning(f"  [warn] fund data refresh skipped ({type(_exc).__name__})", event_type="warning")

    # Which horizons each kind of security gets is a global setting (engine_settings.instrument_type_horizons).
    from portfolio_agent.tools.engine_settings_db import get_engine_settings, horizons_for_instrument_type
    _eng_settings = get_engine_settings()
    ticker_horizons: dict[str, list[int]] = {}

    def _skip(t: str, reason: str) -> None:
        skipped_by_type.append(t)
        log.info(f"  [skip] {t} — {reason}", event_type="apex_skip", ticker=t, reason=reason,
                 instrument_type=itypes.get(t))

    seen_pt: set[str] = set()
    to_predict = []
    for ticker in portfolio_tickers:
        if ticker in seen_pt:
            continue
        seen_pt.add(ticker)
        if itypes.get(ticker) in NO_PREDICTION_TYPES:
            _skip(ticker, f"{str(itypes[ticker]).replace('_', ' ').lower()} — nothing to forecast")
            continue
        _allowed = horizons_for_instrument_type(itypes.get(ticker), _eng_settings)
        _th = [h for h in scheduled_horizons if h in _allowed]
        if not _th:
            _skip(ticker, f"no scheduled horizon is enabled for {str(itypes.get(ticker) or 'UNKNOWN').replace('_', ' ').lower()}"
                          f" (scheduled {scheduled_horizons}, enabled {sorted(_allowed)})")
            continue
        ticker_horizons[ticker] = _th
        _trig_type, _trig_event_id = trigger_map.get(ticker, (None, None))

        if _trig_event_id is not None:
            # Event-driven: a specific, identified event must never be
            # suppressed just because SOME horizon already got a row today
            # for an unrelated reason (routine morning cadence, an earlier
            # event) — that was the "date-only suppression" bug. Only
            # skip if THIS exact event has already produced a row.
            if event_already_processed(ticker, _trig_event_id, today, scopes=SHARED_SCOPES):
                log.info(
                    f"  [skip] {ticker} — event {_trig_event_id} already processed today",
                    event_type="info",
                )
            else:
                log.info(
                    f"  {ticker} — new event {_trig_event_id} ({_trig_type}), generating "
                    f"regardless of any earlier same-day generation",
                    event_type="info",
                )
                to_predict.append(ticker)
            continue

        done_horizons = get_today_horizons(ticker, today, scopes=SHARED_SCOPES)   # private rows never suppress shared generation
        missing       = [h for h in ticker_horizons[ticker] if h not in done_horizons]
        if not missing:
            latest = get_latest_prediction(ticker, scopes=SHARED_SCOPES)
            log.info(
                f"  [skip] {ticker} — all horizons {ticker_horizons[ticker]} already saved today "
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
        _why = (f"all {len(portfolio_tickers)} tickers already predicted today"
                + (f" ({len(skipped_by_type)} skipped: cash / no-ticker funds)" if skipped_by_type else ""))
        log.info(f"  {_why[0].upper()}{_why[1:]}.", event_type="info")
        if tracker:
            tracker.start_phase("apex", total=0)
            tracker.finish_phase("apex", 0, 0, note=_why)
        return

    log.info(
        f"  {len(to_predict)} / {len(portfolio_tickers)} portfolio tickers → APEX reasoning",
        event_type="summary",
    )

    log.info(
        f"  Pre-fetching APEX contexts for {len(to_predict)} tickers (parallel)…",
        event_type="fetch_start",
    )
    ctx_map:    dict[str, str]   = {}      # prompt JSON per ticker
    mctx_map:   dict[str, object] = {}     # the MarketContext (provenance) per ticker
    price_map:  dict[str, float] = {}
    ctx_failed: list[str]        = []
    policy_version = prompt_policy_version()

    # One macro pull for the whole batch — macro data is ticker-independent, so
    # every ticker's context below shares this exact snapshot instead of each
    # of the (up to dozens of) parallel _fetch_ctx calls re-fetching it.
    from portfolio_agent.tools.reasoning_tools import fetch_macro_snapshot
    try:
        shared_macro = fetch_macro_snapshot().payload
    except Exception:
        shared_macro = {}   # market_context falls back to its own per-call loader below

    def _fetch_ctx(t: str) -> tuple[str, object | None, float | None, str | None]:
        ctx   = None
        price = None
        err   = None
        try:
            ctx = build_market_context(t, horizons=ticker_horizons.get(t, scheduled_horizons),
                                       macro_snapshot=shared_macro or None)
        except Exception as exc:              # includes PrivateDataLeak: the ticker is skipped, never degraded to shared
            err = type(exc).__name__
        try:
            price = _get_close(t, today)
        except Exception:
            pass
        return t, ctx, price, err

    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = {pool.submit(_fetch_ctx, t): t for t in to_predict}
        for f in _as_completed(futs):
            t, ctx, price, err = f.result()
            if ctx is not None:
                mctx_map[t] = ctx
                ctx_map[t] = ctx.to_json()
                if price is not None:
                    price_map[t] = price
            else:
                log.warning(
                    f"  [warn] Context pre-fetch failed for {t} ({err or 'unknown'}) — skipping",
                    event_type="warning",
                )
                ctx_failed.append(t)

    # A ticker whose type is still unknown AND that has no price cannot be forecast or later scored: skip it.
    for _t in [t for t in to_predict if t in ctx_map and t not in price_map
               and itypes.get(t) in (None, InstrumentType.UNKNOWN.value)]:
        _skip(_t, "type unknown and no price available")
        ctx_map.pop(_t, None)
        mctx_map.pop(_t, None)
    has_ctx = [t for t in to_predict if t in ctx_map]
    log.info(
        f"  Pre-fetch done — {len(has_ctx)} ready, {len(ctx_failed)} failed",
        event_type="fetch_start",
    )

    def _save_horizon_rows(
        ticker: str, result, horizons: list[int], *,
        has_news: bool, trailing_10d: float | None,
        trig_type: str | None, trig_ev_id: int | None,
    ) -> tuple[list[int], list[str]]:
        """Write one shared row per horizon in *horizons* from *result* (champion,
        fallback or challenger — result.role becomes the row's prediction_role),
        applying the same conviction guardrails to each. Returns
        (horizons saved, error strings)."""
        data             = result.merged
        model_label_used = "+".join(result.model_sources)
        model_prov_used  = result.model_sources[0].split("-")[0].lower()
        _mctx            = mctx_map[ticker]
        _itype           = getattr(_mctx, "instrument_type", None)
        _is_fund         = _itype in FUND_INSTRUMENT_TYPES

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
            # A fund has no company fundamentals or DCF: whatever the model wrote, no placeholder score is stored.
            fundamental_score=None if _is_fund else data.get("fundamental_score"),
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
            used_fallback=result.used_fallback,
            prediction_role=result.role,
            weight_regime=data.get("weight_regime"),
            # For a fund the engine's own weights (fundamentals / valuation exactly 0) are the record, not the model's echo.
            weights_used=(_mctx.payload.get("dynamic_weights", {}).get("weights") if _is_fund else data.get("weights_used")),
            valuation_score=None if _is_fund else data.get("valuation_score"),
            instrument_type=_itype,
            leverage_factor=getattr(_mctx, "leverage_factor", None),
        )

        horizons_data    = data.get("horizons") or []
        horizons_by_days = {
            h.get("horizon_days"): h for h in horizons_data
            if isinstance(h, dict) and h.get("horizon_days")
        }
        saved:  list[int] = []
        errors: list[str] = []

        for h_days in horizons:
            h_data = horizons_by_days.get(h_days, {})
            dist   = h_data.get("distribution") or {}

            _pred_dir = h_data.get("predicted_direction")
            _raw_conviction = h_data.get("conviction_score")
            _conviction, _flags = _apply_conviction_guardrails(
                _pred_dir, _raw_conviction, has_news, trailing_10d,
            )

            # A row's own dict, not the shared _common (which is reused across
            # every horizon of this ticker) -- composite_score is capped ONLY
            # for the horizon(s) the guardrail actually fired on.
            _row = dict(_common)
            if _flags:
                _orig_composite = _row.get("composite_score")
                if _orig_composite is not None:
                    _row["composite_score"] = min(float(_orig_composite), _conviction)
                log.warning(
                    f"  [guardrail] {ticker} {h_days}d — conviction capped "
                    f"({_raw_conviction} → {_conviction}), composite_score capped "
                    f"({_orig_composite} → {_row.get('composite_score')}): {', '.join(_flags)}",
                    event_type="warning", ticker=ticker,
                )

            _prov = ForecastProvenance(
                origin="pipeline.apex", model_used=f"{model_prov_used}:{model_label_used}",
                policy_version=policy_version, guardrail_version=GUARDRAIL_VERSION,
            )
            save   = write_shared_forecast(
                mctx_map[ticker], _prov,
                **_row,
                horizon_days=h_days,
                predicted_direction=_pred_dir,
                predicted_return_low=h_data.get("predicted_return_low"),
                predicted_return_high=h_data.get("predicted_return_high"),
                conviction_score=_conviction,
                # Preserved even though the DISPLAYED/stored conviction_score
                # above is capped -- so failure-pattern mining can still find
                # this call in the high-conviction bucket it actually belongs
                # in (see validation_engine._split_by_conviction_for_mining).
                # None (not just equal to conviction_score) when no cap fired,
                # so a reader can tell "never capped" apart from "capped to
                # exactly its own raw value" by coincidence.
                raw_conviction_score=_raw_conviction if _flags else None,
                reasoning_text=h_data.get("reasoning_text") or data.get("reasoning", ""),
                start_price=price_map.get(ticker),
                p_strong_down=dist.get("strong_down"),
                p_moderate_down=dist.get("moderate_down"),
                p_flat=dist.get("flat"),
                p_moderate_up=dist.get("moderate_up"),
                p_strong_up=dist.get("strong_up"),
                bucket_edges_version=edges_version,
                trigger_type=trig_type,
                trigger_event_id=trig_ev_id,
                guardrail_flags=_flags or None,
            )
            if save.get("saved"):
                saved.append(h_days)
            else:
                errors.append(f"{h_days}d: {save.get('error', 'unknown error')}")
        return saved, errors

    completed: list[str] = []
    failed:    list[str] = list(ctx_failed)
    challenger_rows = 0

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
            horizons_instruction=_build_horizons_instruction(ticker_horizons.get(ticker, scheduled_horizons), edges_table),
        )

        result = await _run_apex(ticker, prompt)

        if result is None:
            log.error(
                f"  [ALL APEX MODELS EXHAUSTED] {ticker} — skipping",
                event_type="checkpoint_save", ticker=ticker, phase_name="apex",
            )
            failed.append(ticker)
            if tracker:
                tracker.tick("apex", len(completed), len(has_ctx), ticker=ticker)
            continue

        model_label_used = "+".join(result.model_sources)
        _trig_type, _trig_ev_id = trigger_map.get(ticker, (None, None))
        _has_news = bool(_ctx_parsed.get("news"))
        saved_horizons: list[int] = []

        try:
            done_today = get_today_horizons(ticker, today, scopes=SHARED_SCOPES)

            # This ticker reached generation for an event-driven reason (see
            # the pre-fetch skip logic above, which already confirmed THIS
            # event hasn't produced a row yet) -- the generic "done today"
            # check must not suppress it again here just because an earlier,
            # unrelated row exists for the same horizon. Non-event-driven
            # tickers keep the original per-horizon dedup.
            done_today_for_save = set() if _trig_ev_id is not None else done_today
            horizons_needed = [h for h in ticker_horizons.get(ticker, scheduled_horizons) if h not in done_today_for_save]

            try:
                _trailing_10d = _get_trailing_return(ticker, today)
            except Exception:
                _trailing_10d = None

            saved_horizons, save_errors = _save_horizon_rows(
                ticker, result, horizons_needed,
                has_news=_has_news, trailing_10d=_trailing_10d,
                trig_type=_trig_type, trig_ev_id=_trig_ev_id,
            )
            saved_count = len(saved_horizons)

            rec  = result.merged.get("recommendation", "?")
            conf = result.merged.get("confidence", "?")
            run_tag = result.role if not result.fallback_reason else f"{result.role}: {result.fallback_reason}"
            log.info(
                f"  [{model_label_used}] APEX: {ticker} → {rec} (conf={conf}, {run_tag}) "
                f"· {saved_count}/{len(horizons_needed)} horizon row(s) saved",
                event_type="db_write", ticker=ticker, action=rec, model=model_label_used,
                role=result.role,
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

        # ── Optional challenger: same prompt, same horizons the production row(s)
        # just covered. Kept outside the try above so a challenger problem never
        # changes the ticker's own completed/failed status.
        if saved_horizons and model_plan.challenger and challenger_selected(ticker, today, model_plan.challenger_pct):
            try:
                ch = await run_challenger(ticker, prompt, model_plan)
                if ch is not None:
                    ch_saved, ch_errors = _save_horizon_rows(
                        ticker, ch, saved_horizons,
                        has_news=_has_news, trailing_10d=_trailing_10d,
                        trig_type=_trig_type, trig_ev_id=_trig_ev_id,
                    )
                    challenger_rows += len(ch_saved)
                    log.info(
                        f"  [{ch.model_sources[0]}] APEX challenger: {ticker} → "
                        f"{ch.merged.get('recommendation', '?')} · {len(ch_saved)}/{len(saved_horizons)} row(s) saved",
                        event_type="apex_challenger", ticker=ticker, model=ch.model_sources[0],
                        role=ch.role, saved=len(ch_saved),
                    )
                    for _err in ch_errors:
                        log.warning(f"  [challenger] {ticker} save failed — {_err}",
                                    event_type="warning", ticker=ticker)
            except Exception as exc:
                log.warning(
                    f"  [challenger] {ticker} — {type(exc).__name__}: {exc} — no challenger row",
                    event_type="warning", ticker=ticker,
                )

        if tracker:
            tracker.tick("apex", len(completed), len(has_ctx), ticker=ticker,
                         note=f"{ticker_num}/{len(has_ctx)}")

    if tracker:
        tracker.finish_phase("apex", len(completed), len(failed))
    log.info(
        f"\n  APEX phase done — {len(completed)} tickers · {len(failed)} failed"
        f"{f' · {len(skipped_by_type)} skipped by type' if skipped_by_type else ''}"
        f"  ({len(has_ctx)} per-ticker model-plan calls, horizons {scheduled_horizons}"
        f"{f', {challenger_rows} challenger row(s)' if challenger_rows else ''})",
        event_type="phase_end", saved=len(completed), failed=len(failed),
    )
