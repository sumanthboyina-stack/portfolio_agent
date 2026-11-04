"""
Trade plan persistence + the data lookups around tools.trade_plan (which is pure).

    create_plan_for_prediction(prediction_id)   one immutable plan per shared_market forecast; a re-run is a no-op
    get_plan(prediction_id) / get_plans(ids)    read back
    backfill_plans(since)                       plans for stored shared forecasts that have none

Look-ahead: the ATR comes from bars dated on or before the forecast's as_of_date, nothing later. A stored technical_features
snapshot is used only if its data_snapshot <= as_of_date; otherwise the OHLC history is fetched and CUT at as_of_date before the
ATR is computed. Table: trade_plans (created by prediction_db._migrate).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Callable

from portfolio_agent.log import get_logger
from portfolio_agent.tools import trade_plan as tp
from portfolio_agent.tools.prediction_db import SCOPE_SHARED, _db

_log = get_logger("trade_plan")

MAX_ATR_STALENESS_DAYS = 7      # the newest bar used must be this close to the forecast date


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _default_fetch_history(ticker: str, as_of: date):
    """OHLC up to and including as_of (yfinance's `end` is exclusive, and the result is cut again by the caller)."""
    import yfinance as yf
    return yf.Ticker(ticker).history(start=(as_of - timedelta(days=150)).isoformat(),
                                     end=(as_of + timedelta(days=1)).isoformat(), interval="1d", auto_adjust=True)


def atr_as_of(ticker: str, as_of_date: str, *, fetch_history: Callable | None = None) -> float | None:
    """ATR(14) from data dated on or before as_of_date; None if there is none recent enough."""
    as_of = date.fromisoformat(str(as_of_date)[:10])
    floor = (as_of - timedelta(days=MAX_ATR_STALENESS_DAYS)).isoformat()
    from portfolio_agent.tools import technical_features as tf
    import json
    try:
        with tf._db() as conn:
            row = conn.execute(
                """SELECT data_snapshot, features FROM technical_features
                   WHERE ticker = ? AND data_snapshot <= ? AND data_snapshot >= ? AND interval = '1d'
                   ORDER BY data_snapshot DESC LIMIT 1""", [ticker.upper(), as_of.isoformat(), floor]).fetchone()
        if row:
            atr = tp._num(json.loads(row["features"]).get("atr_14"))
            if atr:
                return atr
    except Exception as exc:                       # a missing/odd table is "no stored reading", not a failure
        _log.warning(f"  [plan] {ticker} stored ATR lookup failed: {exc}", event_type="warning", ticker=ticker)
    try:
        hist = (fetch_history or _default_fetch_history)(ticker.upper(), as_of)
        if hist is None or len(hist) == 0:
            return None
        hist = hist[[d.date() <= as_of for d in hist.index]]       # the cut that keeps later bars out
        if len(hist) == 0 or hist.index[-1].date() < as_of - timedelta(days=MAX_ATR_STALENESS_DAYS):
            return None
        feats = tf.compute_technical_features(hist["Close"], hist["High"], hist["Low"])["features"]
        return tp._num(feats.get("atr_14"))
    except Exception as exc:
        _log.warning(f"  [plan] {ticker} ATR fetch failed: {exc}", event_type="warning", ticker=ticker)
        return None


def _row_to_plan(row) -> dict | None:
    return dict(row) if row is not None else None


def get_plan(prediction_id: int) -> dict | None:
    with _db() as c:
        return _row_to_plan(c.execute("SELECT * FROM trade_plans WHERE prediction_id = ?", [int(prediction_id)]).fetchone())


def get_plans(prediction_ids) -> dict[int, dict]:
    ids = [int(i) for i in prediction_ids if i is not None]
    if not ids:
        return {}
    with _db() as c:
        rows = c.execute(f"SELECT * FROM trade_plans WHERE prediction_id IN ({','.join('?' * len(ids))})", ids).fetchall()
    return {r["prediction_id"]: dict(r) for r in rows}


def create_plan_for_prediction(prediction_id: int, *, settings: dict | None = None,
                               fetch_history: Callable | None = None) -> dict | None:
    """
    Write the plan for one shared_market forecast and return the stored row. None for a row that is not shared_market (a
    private chat forecast never gets one). A plan that already exists is returned untouched: plans are immutable.
    """
    existing = get_plan(prediction_id)
    if existing:
        return existing
    with _db() as c:
        pred = c.execute("SELECT id, ticker, scope, as_of_date, horizon_days, predicted_direction, predicted_return_high, "
                         "start_price FROM predictions WHERE id = ?", [int(prediction_id)]).fetchone()
    if pred is None or pred["scope"] != SCOPE_SHARED:
        return None
    if settings is None:
        from portfolio_agent.tools.engine_settings_db import get_trade_plan_settings
        settings = get_trade_plan_settings()

    direction, h = pred["predicted_direction"], pred["horizon_days"]
    atr = None
    if str(direction or "").upper() == "UP" and h in tp.SUPPORTED_HORIZONS and tp._num(pred["start_price"]):
        atr = atr_as_of(pred["ticker"], pred["as_of_date"], fetch_history=fetch_history)
    ret_high = tp._num(pred["predicted_return_high"])
    result = tp.build_plan(pred["start_price"], atr, h, direction, settings, as_of_date=pred["as_of_date"],
                           # predictions stores returns in percentage points (3.0 = 3%); build_plan takes a fraction
                           predicted_return_high=None if ret_high is None else ret_high / 100.0)
    if isinstance(result, tp.TradePlan):
        vals = ("planned", None, result.entry, result.stop, result.target, result.risk_per_share, result.reward_risk,
                result.time_stop_date, result.atr_14, result.llm_target)
    else:
        vals = ("no_plan", result.reason, None, None, None, None, None, None, result.atr_14, result.llm_target)
    with _db() as c:
        c.execute("""INSERT OR IGNORE INTO trade_plans
                     (prediction_id, plan_status, no_plan_reason, entry, stop, target, risk_per_share, reward_risk,
                      time_stop_date, atr_14, llm_target, rules_version, created_at)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                  [int(prediction_id), *vals, tp.RULES_VERSION, _now()])
        c.commit()
    return get_plan(prediction_id)


def backfill_plans(since: str, *, settings: dict | None = None, fetch_history: Callable | None = None) -> dict:
    """Plans for every shared_market forecast with as_of_date >= since that has none. Each plan uses only data on or before
    its own as_of_date. Safe to re-run: rows that already have a plan are skipped."""
    date.fromisoformat(since)                          # fail early on a bad date
    with _db() as c:
        ids = [r["id"] for r in c.execute(
            """SELECT p.id FROM predictions p LEFT JOIN trade_plans t ON t.prediction_id = p.id
               WHERE p.scope = ? AND p.as_of_date >= ? AND t.prediction_id IS NULL
               ORDER BY p.as_of_date, p.id""", [SCOPE_SHARED, since]).fetchall()]
    out = {"candidates": len(ids), "planned": 0, "no_plan": 0, "failed": 0}
    for pid in ids:
        try:
            row = create_plan_for_prediction(pid, settings=settings, fetch_history=fetch_history)
            out["planned" if row and row["plan_status"] == "planned" else "no_plan"] += 1
        except Exception as exc:
            out["failed"] += 1
            _log.warning(f"  [plan] backfill of prediction {pid} failed: {exc}", event_type="warning")
    return out
