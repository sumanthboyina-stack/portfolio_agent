"""
Validation engine — 3-layer prediction outcome tracking.

Layer 1: Daily outcome assignment (nightly, no LLM) — evaluate_matured_predictions()
Layer 2: Rolling metrics recomputation (nightly, incremental) — recompute_rolling_metrics()
Layer 3: Weekly/monthly LLM pattern analysis — weekly_pattern_analysis()
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from portfolio_agent.tools.prediction_db import _db
from portfolio_agent.tools.yfinance_tools import get_close
from portfolio_agent.log import get_logger as _get_logger

_log = _get_logger("validation")
_CST = ZoneInfo("America/Chicago")


def _today_cst() -> date:
    """Current date in CST/CDT."""
    return datetime.now(_CST).date()


# ── Return buckets ────────────────────────────────────────────────────────────

# Ordered list of (name, lower_bound_inclusive, upper_bound_exclusive).
# None means unbounded on that side.
RETURN_BUCKETS = [
    ("strong_down",   None,  -0.05),
    ("moderate_down", -0.05, -0.01),
    ("flat",          -0.01,  0.01),
    ("moderate_up",    0.01,  0.05),
    ("strong_up",      0.05,  None),
]
_BUCKET_NAMES = [b[0] for b in RETURN_BUCKETS]
_BUCKET_TO_PROB_KEY = {name: f"p_{name}" for name in _BUCKET_NAMES}

# Single source of truth for the UP/DOWN/FLAT categorical threshold.
# Must match the "flat (-1% to +1%)" band the model is prompted with
# (see prompt_templates.py's distribution instructions) — previously this
# was hardcoded to 0.005 (±0.5%) in six places below, half the width of what
# the model was actually told "flat" means, which made every FLAT call
# nearly impossible to score correct (real 5-day moves rarely land inside
# ±0.5%). Kept as a single constant so it can't drift out of sync again.
FLAT_THRESHOLD = 0.01


def actual_return_bucket(ret: float) -> str:
    """Map an actual return (fraction, e.g. 0.03 = +3%) to its bucket name."""
    if ret < -0.05:
        return "strong_down"
    if ret < -0.01:
        return "moderate_down"
    if ret < 0.01:
        return "flat"
    if ret < 0.05:
        return "moderate_up"
    return "strong_up"


def _extract_p_up(pred: dict) -> Optional[float]:
    """
    Derive P(up) from the stored 5-bucket distribution.
    p_up = (p_moderate_up + p_strong_up) / total_weight.
    Returns None if distribution is absent.
    """
    p_keys = [_BUCKET_TO_PROB_KEY[b] for b in _BUCKET_NAMES]
    raw = [pred.get(k) for k in p_keys]
    if any(v is None for v in raw):
        return None
    total = sum(raw) or 100.0
    return (raw[3] + raw[4]) / total   # moderate_up + strong_up


def compute_distribution_metrics(pred: dict, actual_ret: float) -> tuple[Optional[float], Optional[float]]:
    """
    Compute BINARY Brier score and log-loss from the stored probability distribution.
    Returns (brier_score, log_loss), or (None, None) if distribution is absent.

    p_up = P(moderate_up) + P(strong_up) derived from the 5-bucket distribution.
    actual_up = 1 if actual_ret > +1%, else 0 (flat and down both count as "not up").

    Binary Brier = (p_up − actual_up)²   range [0, 1]  baseline (coin flip) = 0.25
    Binary log-loss = −[a·log(p) + (1−a)·log(1−p)]   baseline = ln(2) ≈ 0.693
    Lower is always better for both.
    """
    p_up = _extract_p_up(pred)
    if p_up is None:
        return None, None

    actual_up = 1.0 if actual_ret > FLAT_THRESHOLD else 0.0

    brier = (p_up - actual_up) ** 2

    p_clip = max(min(p_up, 1 - 1e-6), 1e-6)
    log_loss = -(actual_up * math.log(p_clip) + (1 - actual_up) * math.log(1 - p_clip))

    return brier, log_loss


# ── Outcome scoring ───────────────────────────────────────────────────────────

@dataclass
class Outcome:
    label: str
    score: float


def score_outcome(pred: dict, actual_return: float) -> Outcome:
    actual_dir  = "UP" if actual_return > FLAT_THRESHOLD else "DOWN" if actual_return < -FLAT_THRESHOLD else "FLAT"
    pred_dir    = (pred.get("predicted_direction") or "").upper()
    r_lo = (pred.get("predicted_return_low")  or 0.0) / 100
    r_hi = (pred.get("predicted_return_high") or 0.0) / 100
    in_range    = bool(r_lo != 0 or r_hi != 0) and (r_lo <= actual_return <= r_hi)
    dir_correct = pred_dir == actual_dir

    if dir_correct and in_range:
        return Outcome("strong_correct", 1.0)
    if dir_correct:
        return Outcome("directionally_correct", 0.7)
    if actual_dir == "FLAT" and pred_dir == "FLAT":
        return Outcome("flat_correct", 0.8)
    if not dir_correct and abs(actual_return) > 0.02:
        return Outcome("wrong_significant", 0.0)
    return Outcome("wrong_minor", 0.3)


# ── Layer 1: Daily outcome assignment ─────────────────────────────────────────

def evaluate_matured_predictions(today: Optional[date] = None, force: bool = False) -> dict:
    """
    Layer 1 — For each prediction whose evaluation_date is today or earlier and
    still pending, fetch actual price returns, score the outcome, and persist to DB.
    No LLM involved. Pass force=True to run on non-trading days (e.g. manual runs).
    """
    from portfolio_agent.tools.prediction_db import get_matured_pending_predictions, update_prediction_outcome, is_trading_day
    if today is None:
        today = _today_cst()
    if not force and not is_trading_day(today):
        _log.info(f"  [validation/L1] {today} is not a trading day — skipped (use force=True to override)",
                  event_type="phase_start", date=str(today), skipped=True)
        return {"skipped": True, "reason": "not a trading day"}

    _log.info(f"  [validation/L1] Scanning for matured predictions on {today}…",
              event_type="phase_start", date=str(today))
    pending = get_matured_pending_predictions(today.isoformat())

    if not pending:
        _log.info(f"  [validation/L1] No pending predictions due today — nothing to evaluate",
                  event_type="summary", count=0)
        return {"evaluated": 0, "data_missing": 0, "errors": 0}

    _log.info(f"  [validation/L1] Found {len(pending)} pending prediction(s) to evaluate",
              event_type="summary", count=len(pending))
    evaluated = 0
    data_missing = 0
    errors = 0

    for i, pred in enumerate(pending, 1):
        ticker   = pred.get("ticker", "?")
        h_days   = pred.get("horizon_days", "?")
        pred_dir = pred.get("predicted_direction", "?")
        try:
            start = (pred.get("as_of_date") or pred.get("created_at", "")[:10])
            end   = pred.get("evaluation_date")
            if not start or not end:
                _log.warning(f"  [{i}/{len(pending)}] {ticker} {h_days}d — skipped (missing dates)",
                             event_type="ticker_progress", ticker=ticker, index=i, total=len(pending),
                             horizon_days=h_days, skipped=True)
                continue

            _log.info(f"  [{i}/{len(pending)}] {ticker} {h_days}d  predicted={pred_dir}  "
                      f"window={start}→{end}  fetching prices…",
                      event_type="ticker_progress", ticker=ticker, index=i, total=len(pending),
                      horizon_days=h_days, predicted_direction=pred_dir)

            # Use locked-in start_price if stored at prediction time; fetch as fallback
            start_price = pred.get("start_price") or get_close(ticker, start)
            end_price   = get_close(ticker, end)
            spy_start   = get_close("SPY", start)
            spy_end     = get_close("SPY", end)

            if start_price is None or end_price is None or start_price == 0:
                update_prediction_outcome(
                    pred["id"], 0.0, "UNKNOWN", None, None,
                    "data_missing", 0.0, 0.0, False, "data_missing",
                )
                _log.warning(f"  [{i}/{len(pending)}] {ticker} — DATA MISSING (price unavailable)",
                             event_type="warning", ticker=ticker, index=i, total=len(pending))
                data_missing += 1
                continue

            actual_return    = (end_price / start_price) - 1
            benchmark_return = (spy_end / spy_start - 1) if (spy_start and spy_end and spy_start != 0) else None
            excess_return = (
                (actual_return - benchmark_return) if benchmark_return is not None else None
            )

            outcome    = score_outcome(pred, actual_return)
            actual_dir = "UP" if actual_return > FLAT_THRESHOLD else "DOWN" if actual_return < -FLAT_THRESHOLD else "FLAT"
            r_lo       = (pred.get("predicted_return_low")  or 0.0) / 100
            r_hi       = (pred.get("predicted_return_high") or 0.0) / 100
            in_range   = bool(r_lo != 0 or r_hi != 0) and (r_lo <= actual_return <= r_hi)
            midpoint   = (r_lo + r_hi) / 2 if (r_lo or r_hi) else 0.0
            error_mag  = abs(actual_return - midpoint)

            bucket = actual_return_bucket(actual_return)
            brier, log_loss = compute_distribution_metrics(pred, actual_return)

            update_prediction_outcome(
                pred["id"],
                actual_return,
                actual_dir,
                benchmark_return,
                excess_return,
                outcome.label,
                outcome.score,
                error_mag,
                in_range,
                actual_bucket=bucket,
                brier_score=brier,
                log_loss=log_loss,
            )
            dist_str = f"  brier={brier:.3f}" if brier is not None else ""
            exc_str = f"  excess={excess_return*100:+.2f}%" if excess_return is not None else ""
            _log.info(f"  [{i}/{len(pending)}] {ticker} {h_days}d  actual={actual_return*100:+.2f}%  "
                      f"bucket={bucket}  outcome={outcome.label}{exc_str}{dist_str}",
                      event_type="db_write", ticker=ticker, index=i, total=len(pending),
                      actual_return_pct=round(actual_return * 100, 4),
                      bucket=bucket, outcome=outcome.label)
            evaluated += 1
        except Exception as exc:
            _log.error(f"  [{i}/{len(pending)}] {ticker} — ERROR: {exc}",
                       event_type="error", ticker=ticker, index=i, total=len(pending),
                       error_type=type(exc).__name__, error=str(exc)[:200])
            errors += 1

    _log.info(f"  [validation/L1] Done — {evaluated} evaluated, {data_missing} data_missing, {errors} errors",
              event_type="phase_end", evaluated=evaluated, data_missing=data_missing, errors=errors)

    # ── Backfill followup_5d_return in news_filter_log for dropped tickers ────
    # For tickers we filtered out of the news batch 5–10 calendar days ago,
    # compute their actual return over that window so we can audit the filter.
    # Query: "Of tickers we dropped, which had big moves we missed?"
    #   SELECT ticker, date, followup_5d_return FROM news_filter_log
    #   WHERE final_decision='dropped' AND followup_5d_return > 0.05
    try:
        _fl_recent = (today - timedelta(days=5)).isoformat()   # must be at least 5d old
        _fl_old    = (today - timedelta(days=10)).isoformat()  # cap at 10d (5d return is stale beyond that)
        with _db() as _fl_c:
            _fl_rows = _fl_c.execute(
                """SELECT id, ticker, as_of_date FROM news_filter_log
                   WHERE final_decision = 'dropped'
                     AND followup_5d_return IS NULL
                     AND as_of_date <= ?
                     AND as_of_date >= ?""",
                [_fl_recent, _fl_old],
            ).fetchall()
        _fl_rows = [dict(r) for r in _fl_rows]
        _fl_updated = 0
        for _fl in _fl_rows:
            _start_p = get_close(_fl["ticker"], _fl["as_of_date"])
            _end_p   = get_close(_fl["ticker"], today.isoformat())
            if _start_p and _end_p and _start_p > 0:
                _ret = (_end_p / _start_p) - 1
                with _db() as _fl_c:
                    _fl_c.execute(
                        "UPDATE news_filter_log SET followup_5d_return = ? WHERE id = ?",
                        [_ret, _fl["id"]],
                    )
                    _fl_c.commit()
                _fl_updated += 1
        if _fl_rows:
            _log.info(f"  [validation/L1] filter_log: {_fl_updated}/{len(_fl_rows)} "
                      f"dropped-ticker returns backfilled",
                      event_type="db_write", updated=_fl_updated, total=len(_fl_rows))
    except Exception as _fl_exc:
        _log.warning(f"  [validation/L1] filter_log backfill failed (non-fatal): {_fl_exc}",
                     event_type="warning", error_type=type(_fl_exc).__name__)

    return {"evaluated": evaluated, "data_missing": data_missing, "errors": errors}


# ── Layer 2: Rolling metrics ───────────────────────────────────────────────────

def recompute_rolling_metrics(today: Optional[date] = None) -> dict:
    """
    Layer 2 — Recompute rolling accuracy metrics grouped by (horizon, segment, version).
    Runs after Layer 1 each night.
    """
    from portfolio_agent.tools.prediction_db import upsert_rolling_metric

    if today is None:
        today = _today_cst()
    today_str = today.isoformat()

    _log.info(f"  [validation/L2] Recomputing rolling metrics for {today_str}…",
              event_type="phase_start", date=today_str)
    metrics_written = 0

    for lookback_days in [30, 90, 365]:
        _log.info(f"  [validation/L2] Processing {lookback_days}-day lookback window…",
                  event_type="summary", lookback_days=lookback_days)
        cutoff = (today - timedelta(days=lookback_days)).isoformat()

        with _db() as c:
            rows = c.execute(
                """SELECT horizon_days,
                          COALESCE(system_version, 'v1.0')  AS sys_ver,
                          trigger_type,
                          actual_direction, predicted_direction,
                          in_predicted_range, excess_return, error_magnitude,
                          outcome_score, conviction_score,
                          brier_score, log_loss
                   FROM predictions
                   WHERE evaluation_status = 'evaluated'
                     AND evaluated_at >= ?
                     AND horizon_days IS NOT NULL""",
                [cutoff],
            ).fetchall()

        # Group by (horizon_days, segment, system_version). Every row lands in the
        # blended 'all' bucket, and also in 'opportunity' or 'portfolio' depending
        # on how it was discovered -- trending-opportunity picks are validated
        # separately from portfolio-holding predictions so accuracy on discovery
        # calls doesn't get diluted by (or hide behind) the much larger holdings
        # population. (risk_segment is unused/always NULL -- it never carried a
        # real bucketing signal, so it's dropped here rather than kept as dead
        # weight alongside the new opportunity/portfolio split.)
        from collections import defaultdict
        groups: dict[tuple, list] = defaultdict(list)
        for r in rows:
            d = dict(r)
            opp_seg = "opportunity" if d.get("trigger_type") == "trending_opportunity" else "portfolio"
            groups[(d["horizon_days"], "all", d["sys_ver"])].append(d)
            groups[(d["horizon_days"], opp_seg, d["sys_ver"])].append(d)

        def _dir_acc_for(subset: list) -> Optional[float]:
            if not subset:
                return None
            return sum(
                1 for p in subset
                if (p.get("actual_direction") or "").upper() ==
                   (p.get("predicted_direction") or "").upper()
            ) / len(subset)

        for (h_days, seg, ver), preds in groups.items():
            n = len(preds)

            dir_acc = (
                sum(1 for p in preds
                    if (p.get("actual_direction") or "").upper() ==
                       (p.get("predicted_direction") or "").upper())
                / n if n else None
            )
            in_range_pct = (
                sum(1 for p in preds if p.get("in_predicted_range")) / n if n else None
            )
            exc_vals = [p["excess_return"] for p in preds if p.get("excess_return") is not None]
            mean_exc = (sum(exc_vals) / len(exc_vals)) if exc_vals else None

            err_vals = [p["error_magnitude"] for p in preds if p.get("error_magnitude") is not None]
            mean_err = (sum(err_vals) / len(err_vals)) if err_vals else None

            hi_preds = [p for p in preds if (p.get("conviction_score") or 0) >= 7]
            lo_preds = [p for p in preds if (p.get("conviction_score") or 0) <  7]

            hi_acc = _dir_acc_for(hi_preds)
            lo_acc = _dir_acc_for(lo_preds)

            # Brier score: use stored per-prediction brier (from distribution) when available;
            # fall back to conviction-proxy for old rows that predate the distribution schema.
            dist_brier_vals = [p["brier_score"] for p in preds if p.get("brier_score") is not None]
            if dist_brier_vals:
                brier = sum(dist_brier_vals) / len(dist_brier_vals)
            else:
                # Legacy fallback: mean((conviction/10 − correct)²)
                fallback = []
                for p in preds:
                    conv = p.get("conviction_score")
                    os_  = p.get("outcome_score")
                    if conv is not None and os_ is not None:
                        fallback.append((conv / 10.0 - (1.0 if os_ >= 0.7 else 0.0)) ** 2)
                brier = (sum(fallback) / len(fallback)) if fallback else None

            ll_vals = [p["log_loss"] for p in preds if p.get("log_loss") is not None]
            mean_ll = (sum(ll_vals) / len(ll_vals)) if ll_vals else None

            upsert_rolling_metric({
                "metric_date":            today_str,
                "horizon_days":           h_days,
                "segment":                seg,
                "system_version":         ver,
                "lookback_days":          lookback_days,
                "directional_accuracy":   dir_acc,
                "in_range_pct":           in_range_pct,
                "mean_excess_return":     mean_exc,
                "mean_error_magnitude":   mean_err,
                "high_conviction_accuracy": hi_acc,
                "low_conviction_accuracy":  lo_acc,
                "brier_score":            brier,
                "mean_log_loss":          mean_ll,
                "num_predictions":        n,
            })
            metrics_written += 1

    _log.info(f"  [validation/L2] Done — {metrics_written} metric rows written",
              event_type="phase_end", metrics_written=metrics_written)
    return {"metrics_written": metrics_written}


# ── Layer 3: Weekly LLM pattern analysis ──────────────────────────────────────

def weekly_pattern_analysis() -> dict:
    """
    Layer 3 — One LLM call per week summarising failure patterns in wrong predictions.
    """
    from portfolio_agent.tools.prediction_db import insert_validation_report

    # Skip if already ran this week
    with _db() as c:
        _week_ago = (_today_cst() - timedelta(days=7)).isoformat()
        existing = c.execute(
            """SELECT id FROM validation_reports
               WHERE period = 'weekly' AND report_date >= ?""",
            [_week_ago],
        ).fetchone()
    if existing:
        return {"skipped": True, "reason": "already ran this week"}

    with _db() as c:
        rows = c.execute(
            """SELECT ticker, horizon_days, predicted_direction,
                      predicted_return_low, predicted_return_high,
                      actual_return, outcome, conviction_score,
                      risk_segment, evaluated_at, error_magnitude
               FROM predictions
               WHERE evaluation_status = 'evaluated'
                 AND evaluated_at >= ?
                 AND outcome IN ('wrong_significant', 'wrong_minor')
               ORDER BY ABS(error_magnitude) DESC LIMIT 30""",
            [_week_ago],
        ).fetchall()

    wrong = [dict(r) for r in rows]
    if len(wrong) < 5:
        return {"skipped": True, "reason": f"insufficient data ({len(wrong)} wrong predictions)"}

    prompt = _build_postmortem_prompt(wrong)

    try:
        import litellm
        from portfolio_agent._models import FAILOVER_CHAINS
        chain = FAILOVER_CHAINS.get("flash", [])
        response_text = ""
        for model_id, _, label in chain:
            try:
                resp = litellm.completion(
                    model=model_id,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.2,
                    max_tokens=1024,
                )
                response_text = resp.choices[0].message.content or ""
                break
            except Exception:
                continue

        # Try to parse JSON
        import re
        patterns_dict: dict = {}
        llm_summary = response_text
        m = re.search(r"```json\s*(\{.*?\})\s*```", response_text, re.DOTALL)
        if not m:
            m = re.search(r"(\{.*?\})", response_text, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(1))
                patterns_dict = parsed
                llm_summary   = parsed.get("summary", response_text)
            except Exception:
                pass

        insert_validation_report(
            "weekly",
            patterns_dict,
            llm_summary,
            {"wrong_count": len(wrong)},
        )
        return {"inserted": True, "wrong_analyzed": len(wrong)}

    except Exception as exc:
        return {"error": str(exc)}


def _build_postmortem_prompt(wrong: list[dict]) -> str:
    lines = ["Wrong predictions this week:"]
    for p in wrong[:20]:
        lines.append(
            f"  {p.get('ticker')} {p.get('horizon_days')}d | predicted {p.get('predicted_direction')} "
            f"[{p.get('predicted_return_low','?')}% to {p.get('predicted_return_high','?')}%] | "
            f"actual {(p.get('actual_return') or 0)*100:.1f}% | "
            f"conviction={p.get('conviction_score','?')} | segment={p.get('risk_segment','?')}"
        )
    return (
        "You are a systematic trading analyst reviewing prediction errors.\n\n"
        + "\n".join(lines)
        + "\n\nIdentify 2-4 patterns in these failures. "
        "Return a JSON object with:\n"
        '  "patterns": [list of pattern strings],\n'
        '  "summary": "2-3 sentence overall narrative",\n'
        '  "suggestions": [list of improvement suggestions]\n\n'
        "JSON only, no prose outside the block."
    )


# ── Dashboard read helpers ─────────────────────────────────────────────────────

def get_recent_evaluated_predictions(limit: int = 100, lookback_days: int | None = None) -> list[dict]:
    where = "WHERE evaluation_status IN ('evaluated', 'data_missing')"
    params: list = []
    if lookback_days:
        cutoff = (_today_cst() - timedelta(days=lookback_days)).isoformat()
        where += " AND as_of_date >= ?"
        params.append(cutoff)
    params.append(limit)
    with _db() as c:
        rows = c.execute(
            f"""SELECT ticker, as_of_date, horizon_days, predicted_direction,
                      predicted_return_low, predicted_return_high,
                      actual_return, actual_bucket, outcome, conviction_score, excess_return,
                      risk_segment, system_version, evaluated_at, evaluation_status,
                      in_predicted_range, error_magnitude,
                      p_strong_down, p_moderate_down, p_flat, p_moderate_up, p_strong_up,
                      brier_score, log_loss, model_name,
                      start_price, pt_current_price
               FROM predictions
               {where}
               ORDER BY as_of_date DESC, evaluated_at DESC LIMIT ?""",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def get_accuracy_heatmap_data(lookback_days: int | None = None, model_names: list[str] | None = None) -> list[dict]:
    clauses = ["evaluation_status = 'evaluated'", "horizon_days IS NOT NULL", "actual_direction IS NOT NULL"]
    params: list = []
    if lookback_days:
        clauses.append("as_of_date >= ?")
        params.append((_today_cst() - timedelta(days=lookback_days)).isoformat())
    if model_names:
        clauses.append(f"model_name IN ({','.join('?' * len(model_names))})")
        params.extend(model_names)
    where = "WHERE " + " AND ".join(clauses)
    with _db() as c:
        rows = c.execute(
            f"""SELECT horizon_days,
                      COALESCE(risk_segment, 'unknown') AS segment,
                      COUNT(*) AS n,
                      AVG(CASE WHEN UPPER(actual_direction) = UPPER(predicted_direction) THEN 1.0 ELSE 0.0 END) AS dir_acc
               FROM predictions
               {where}
               GROUP BY horizon_days, segment""",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def get_calibration_data(lookback_days: int | None = None, model_names: list[str] | None = None) -> dict:
    """
    Returns all data needed for the calibration tab:

    'summary'   — overall binary Brier, log-loss, N with baseline comparisons
    'reliability' — reliability diagram: 10 p_up bins × (stated_conf, actual_hit_rate, n)
    'bucket_table' — same bins as a table row per bin
    'conviction'  — legacy conviction-score vs directional accuracy
    'horizon_breakdown' — per-horizon binary Brier / log-loss / N

    Binary metrics use:
      p_up = (p_moderate_up + p_strong_up) / 100   (normalised to [0, 1])
      actual_up = 1 if actual_return > +1%, else 0
      Brier baseline = 0.25 (coin flip), log-loss baseline = ln(2) ≈ 0.693
    """
    clauses = ["evaluation_status = 'evaluated'", "actual_direction IS NOT NULL"]
    params: list = []
    if lookback_days:
        clauses.append("as_of_date >= ?")
        params.append((_today_cst() - timedelta(days=lookback_days)).isoformat())
    if model_names:
        clauses.append(f"model_name IN ({','.join('?' * len(model_names))})")
        params.extend(model_names)
    where = "WHERE " + " AND ".join(clauses)
    with _db() as c:
        rows = c.execute(
            f"""SELECT conviction_score,
                      actual_return,
                      actual_direction,
                      CASE WHEN UPPER(actual_direction) = UPPER(predicted_direction) THEN 1.0 ELSE 0.0 END AS dir_correct,
                      p_strong_down, p_moderate_down, p_flat, p_moderate_up, p_strong_up,
                      horizon_days,
                      COALESCE(risk_segment, 'unknown') AS segment
               FROM predictions
               {where}""",
            params,
        ).fetchall()

    rows = [dict(r) for r in rows]

    p_keys = ["p_strong_down", "p_moderate_down", "p_flat", "p_moderate_up", "p_strong_up"]

    def _p_up_from_row(r) -> Optional[float]:
        raw = [r.get(k) for k in p_keys]
        if any(v is None for v in raw):
            return None
        total = sum(raw) or 100.0
        return (raw[3] + raw[4]) / total

    # Rows that have distribution data
    dist_rows = [(r, _p_up_from_row(r)) for r in rows]
    dist_rows = [(r, p) for r, p in dist_rows if p is not None]

    # ── Summary (overall binary metrics) ───────────────────────────────────────
    summary = {"brier": None, "log_loss": None, "n": len(dist_rows),
               "brier_baseline": 0.25, "log_loss_baseline": math.log(2)}
    if dist_rows:
        brier_vals, ll_vals = [], []
        for r, p_up in dist_rows:
            actual_up = 1.0 if (r.get("actual_return") or 0) > FLAT_THRESHOLD else 0.0
            brier_vals.append((p_up - actual_up) ** 2)
            p_clip = max(min(p_up, 1 - 1e-6), 1e-6)
            ll_vals.append(-(actual_up * math.log(p_clip) + (1 - actual_up) * math.log(1 - p_clip)))
        summary["brier"]    = sum(brier_vals) / len(brier_vals)
        summary["log_loss"] = sum(ll_vals) / len(ll_vals)

    # ── Reliability diagram (10 bins of p_up) ─────────────────────────────────
    bins: list[list] = [[] for _ in range(10)]
    for r, p_up in dist_rows:
        actual_up = 1.0 if (r.get("actual_return") or 0) > FLAT_THRESHOLD else 0.0
        idx = min(int(p_up * 10), 9)
        bins[idx].append((p_up, actual_up))

    reliability = []
    bucket_table = []
    for i, bucket in enumerate(bins):
        lo, hi = i * 10, (i + 1) * 10
        label = f"{lo}–{hi}%"
        center = (i + 0.5) / 10
        if bucket:
            mean_p   = sum(p for p, _ in bucket) / len(bucket)
            mean_hit = sum(a for _, a in bucket) / len(bucket)
            reliability.append({"bin_label": label, "center": center,
                                 "mean_stated": mean_p, "actual_hit_rate": mean_hit,
                                 "n": len(bucket)})
        bucket_table.append({"Stated confidence": label,
                              "# predictions": len(bucket),
                              "# correct": sum(int(a) for _, a in bucket),
                              "Actual hit rate": f"{sum(a for _, a in bucket)/len(bucket)*100:.0f}%" if bucket else "—"})

    # ── Conviction calibration (legacy — always available) ─────────────────────
    conv_buckets = [
        {"label": "0–2",  "min": 0, "max": 2,  "mid": 1},
        {"label": "2–4",  "min": 2, "max": 4,  "mid": 3},
        {"label": "4–6",  "min": 4, "max": 6,  "mid": 5},
        {"label": "6–8",  "min": 6, "max": 8,  "mid": 7},
        {"label": "8–10", "min": 8, "max": 10, "mid": 9},
    ]
    conviction_cal = []
    for b in conv_buckets:
        subset = [r for r in rows if r.get("conviction_score") is not None
                  and b["min"] <= (r["conviction_score"] or 0) < b["max"]]
        if subset:
            acc = sum(r["dir_correct"] for r in subset) / len(subset)
            conviction_cal.append({"label": b["label"], "mid": b["mid"],
                                   "accuracy": acc, "n": len(subset)})

    # ── Per-horizon breakdown ──────────────────────────────────────────────────
    horizon_breakdown = {}
    for h in [5, 21, 63]:
        h_rows = [(r, p) for r, p in dist_rows if r.get("horizon_days") == h]
        if not h_rows:
            continue
        b_vals, l_vals = [], []
        for r, p_up in h_rows:
            actual_up = 1.0 if (r.get("actual_return") or 0) > FLAT_THRESHOLD else 0.0
            b_vals.append((p_up - actual_up) ** 2)
            p_clip = max(min(p_up, 1 - 1e-6), 1e-6)
            l_vals.append(-(actual_up * math.log(p_clip) + (1 - actual_up) * math.log(1 - p_clip)))
        horizon_breakdown[h] = {
            "brier":    sum(b_vals) / len(b_vals),
            "log_loss": sum(l_vals) / len(l_vals),
            "n":        len(h_rows),
        }

    return {
        "summary":           summary,
        "reliability":       reliability,
        "bucket_table":      bucket_table,
        "conviction":        conviction_cal,
        "horizon_breakdown": horizon_breakdown,
    }


def get_drift_data(horizon_days: int = 5, window: int = 30, lookback_days: int = 90, model_names: list[str] | None = None) -> list[dict]:
    cutoff = (_today_cst() - timedelta(days=lookback_days)).isoformat()
    extra = ""
    params: list = [horizon_days, cutoff]
    if model_names:
        extra = f"AND model_name IN ({','.join('?' * len(model_names))})"
        params.extend(model_names)
    with _db() as c:
        rows = c.execute(
            f"""SELECT DATE(evaluated_at) AS eval_date,
                      CASE WHEN UPPER(actual_direction) = UPPER(predicted_direction) THEN 1.0 ELSE 0.0 END AS correct
               FROM predictions
               WHERE evaluation_status = 'evaluated'
                 AND horizon_days = ?
                 AND evaluated_at >= ?
                 {extra}
               ORDER BY eval_date""",
            params,
        ).fetchall()

    if not rows:
        return []

    records = [dict(r) for r in rows]
    dates_seen = sorted({r["eval_date"] for r in records})
    result = []
    for d in dates_seen:
        window_cutoff = (date.fromisoformat(d) - timedelta(days=window)).isoformat()
        subset = [r for r in records if window_cutoff <= r["eval_date"] <= d]
        if subset:
            result.append({
                "date": d,
                "accuracy": sum(r["correct"] for r in subset) / len(subset),
                "n": len(subset),
            })
    return result


def get_volume_by_horizon(lookback_days: int = 90) -> list[dict]:
    cutoff = (_today_cst() - timedelta(days=lookback_days)).isoformat()
    with _db() as c:
        rows = c.execute(
            """SELECT as_of_date AS pred_date, horizon_days, COUNT(*) AS n
               FROM predictions
               WHERE as_of_date >= ? AND horizon_days IS NOT NULL
               GROUP BY as_of_date, horizon_days
               ORDER BY as_of_date""",
            [cutoff],
        ).fetchall()
    return [dict(r) for r in rows]


def get_system_version_comparison(lookback_days: int | None = None, model_names: list[str] | None = None) -> list[dict]:
    clauses = ["evaluation_status = 'evaluated'", "horizon_days IS NOT NULL"]
    params: list = []
    if lookback_days:
        clauses.append("as_of_date >= ?")
        params.append((_today_cst() - timedelta(days=lookback_days)).isoformat())
    if model_names:
        clauses.append(f"model_name IN ({','.join('?' * len(model_names))})")
        params.extend(model_names)
    where = "WHERE " + " AND ".join(clauses)
    with _db() as c:
        rows = c.execute(
            f"""SELECT COALESCE(system_version, 'v1.0') AS sys_ver,
                      horizon_days,
                      COUNT(*) AS n,
                      AVG(CASE WHEN UPPER(actual_direction) = UPPER(predicted_direction) THEN 1.0 ELSE 0.0 END) AS dir_acc,
                      AVG(excess_return) AS mean_excess
               FROM predictions
               {where}
               GROUP BY sys_ver, horizon_days
               ORDER BY sys_ver, horizon_days""",
            params,
        ).fetchall()
    return [dict(r) for r in rows]
