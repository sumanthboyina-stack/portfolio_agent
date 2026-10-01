"""
Predictions page — pure data-loading functions (DB queries, no Streamlit calls).

Contains:
  - _load_predictions()      : latest prediction per ticker/horizon for a date
  - _load_ticker_history()   : full prediction history for one ticker
  - _load_edge_data()        : validation/accuracy metrics for a horizon+segment
  - _load_system_stats()     : stats for the system strip
  - _latest_as_of_date()     : most recent as_of_date in the DB
  - _fetch_price_chart_data(): OHLCV + SMA/RSI time series for charting
  - _cname()                 : ticker -> company name lookup
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from web.lib import db_conn as _conn


def _load_predictions(
    selected_date: date | None = None,
    horizon_days: int | None = None,
    min_conviction: float = 0,
    owner: str | None = None,
) -> pd.DataFrame:
    """
    Load the latest SHARED prediction per ticker/horizon for the given date (or
    all dates if None) -- scoped to SHARED_SCOPES so a private chat forecast
    (scope='private', written by web/pages/4_🤖_Chat.py for whoever asked) never
    shows up on someone else's screen, and never silently replaces the shared
    call for that ticker/horizon just because it happens to be more recent.

    owner: when given, that owner's OWN private forecasts are also included
    (marked is_own_private=True), deduped separately from the shared rows so
    one never displaces the other -- a viewer can see both "the shared call"
    and "my own chat forecast" for the same ticker/horizon.
    """
    from portfolio_agent.tools.prediction_db import SHARED_SCOPES, scope_clause

    _sc, _sp = scope_clause(SHARED_SCOPES)
    where = _sc
    params = list(_sp)
    if owner:
        where = f"({_sc} OR (scope = 'private' AND owner_scope = ?))"
        params = [*_sp, f"user:{owner}"]

    with _conn() as conn:
        rows = conn.execute(
            f"""SELECT id, ticker, as_of_date, recommendation, prediction,
                      confidence, composite_score, horizon_days, prediction_type,
                      predicted_direction, predicted_return_low, predicted_return_high,
                      conviction_score, fundamental_score, valuation_score, research_score, macro_score,
                      news_score, reasoning, reasoning_text, panel_summary,
                      model_name, model_provider, evaluation_status, outcome,
                      actual_return, actual_direction, pt_current_price, pt_mean,
                      pt_num_analysts, start_price, changed_from_previous,
                      previous_prediction, p_strong_down, p_moderate_down, p_flat,
                      p_moderate_up, p_strong_up, created_at, evaluation_date,
                      risk_segment, system_version, snapshot_fundamentals_score,
                      snapshot_news_score, snapshot_research_score, snapshot_macro_score,
                      snapshot_news_headlines, brier_score,
                      trigger_type, trigger_event_id, scope
               FROM predictions
               WHERE as_of_date IS NOT NULL AND {where}
               ORDER BY as_of_date DESC, created_at DESC""",
            params,
        ).fetchall()

    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        df["is_own_private"] = pd.Series(dtype=bool)
        return df

    if selected_date is not None:
        df = df[df["as_of_date"] == selected_date.isoformat()]

    if horizon_days is not None:
        df = df[df["horizon_days"] == horizon_days]

    if min_conviction > 0:
        df = df[df["conviction_score"].fillna(0) >= min_conviction]

    # Latest per ticker+horizon -- shared and the viewer's own private rows are
    # deduped SEPARATELY (never against each other), so a private forecast can
    # never win the "latest" slot and hide the shared call for everyone else.
    df["is_own_private"] = df["scope"] == "private"
    shared = df[~df["is_own_private"]]
    private = df[df["is_own_private"]]
    parts = [
        part.sort_values("created_at", ascending=False).drop_duplicates(subset=["ticker", "horizon_days"])
        for part in (shared, private) if not part.empty
    ]
    df = pd.concat(parts) if parts else df.iloc[0:0]
    return df.sort_values(["ticker", "horizon_days"]).reset_index(drop=True)


def _personal_overlay(ctx, tickers: list[str]) -> dict[str, dict]:
    """
    Deterministic, per-viewer badges for a set of tickers on the Predictions
    page: held (and at what portfolio weight), on the viewer's own restricted
    list, inside an active blackout window, or gated by the viewer's own
    pre-clearance preference. Reuses the exact same primitives clearance.py's
    PolicyDecision is built from — this never re-derives or second-guesses the
    rule, just surfaces each deterministic fact per row instead of collapsing
    it into one pass/fail memo. The recommendation itself is never touched:
    a BUY stays a BUY even if, say, the sector cap is already full — these are
    read-only context badges, not a rewrite of the call.
    """
    from portfolio_agent.clearance import evaluate_blackout_windows
    from portfolio_agent.services.account_service import list_holdings_for
    from portfolio_agent.tools.blackout_windows_db import list_active_blackout_windows
    from portfolio_agent.tools.restricted_list_db import list_restricted_for
    from portfolio_agent.tools.user_profile_db import get_user_profile_for
    from datetime import datetime, timezone as _timezone

    wanted = {t.upper() for t in tickers}

    held_value_by_ticker: dict[str, float] = {}
    for h in list_holdings_for(ctx):
        t = str(h.get("ticker") or "").upper()
        if t:
            held_value_by_ticker[t] = held_value_by_ticker.get(t, 0.0) + float(h.get("current_value") or 0)
    total_value = sum(held_value_by_ticker.values())

    restricted_by_ticker = {r["ticker"].upper(): r.get("reason") for r in list_restricted_for(ctx)}
    windows = list_active_blackout_windows()
    needs_pre_clearance = bool(get_user_profile_for(ctx).pre_clearance_required)
    now_utc = datetime.now(_timezone.utc)

    overlay: dict[str, dict] = {}
    for t in wanted:
        value = held_value_by_ticker.get(t)
        overlay[t] = {
            "held": t in held_value_by_ticker,
            "weight_pct": (value / total_value * 100) if value and total_value else None,
            "restricted": t in restricted_by_ticker,
            "restricted_reason": restricted_by_ticker.get(t),
            "blackout": evaluate_blackout_windows(windows, t, at_utc=now_utc)["active"],
            "needs_pre_clearance": needs_pre_clearance,
        }
    return overlay


def _load_ticker_history(ticker: str) -> pd.DataFrame:
    with _conn() as conn:
        rows = conn.execute(
            """SELECT id, ticker, as_of_date, recommendation, prediction,
                      confidence, composite_score, horizon_days,
                      predicted_direction, predicted_return_low, predicted_return_high,
                      conviction_score, evaluation_status, outcome, actual_return,
                      start_price, created_at
               FROM predictions
               WHERE ticker = ? AND as_of_date IS NOT NULL
               ORDER BY as_of_date ASC, created_at ASC""",
            (ticker,),
        ).fetchall()
    return pd.DataFrame([dict(r) for r in rows])


def _load_edge_data(horizon_days: int, risk_segment: str | None) -> dict | None:
    """Query metrics_rolling for accuracy at given horizon + segment."""
    with _conn() as conn:
        if risk_segment:
            row = conn.execute(
                """SELECT directional_accuracy, num_predictions
                   FROM metrics_rolling
                   WHERE horizon_days = ? AND segment = ?
                   ORDER BY computed_at DESC LIMIT 1""",
                (horizon_days, risk_segment),
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT directional_accuracy, num_predictions
                   FROM metrics_rolling
                   WHERE horizon_days = ?
                   ORDER BY computed_at DESC LIMIT 1""",
                (horizon_days,),
            ).fetchone()
    return dict(row) if row else None


def _load_system_stats(selected_date: date) -> dict:
    """Load stats for the system strip."""
    date_str = selected_date.isoformat()
    with _conn() as conn:
        total_today = conn.execute(
            "SELECT COUNT(DISTINCT ticker) FROM predictions WHERE as_of_date = ?",
            (date_str,),
        ).fetchone()[0]

        latest_row = conn.execute(
            "SELECT created_at, system_version FROM predictions WHERE as_of_date IS NOT NULL ORDER BY created_at DESC LIMIT 1"
        ).fetchone()

        total_tickers = conn.execute(
            "SELECT COUNT(DISTINCT ticker) FROM predictions WHERE as_of_date IS NOT NULL"
        ).fetchone()[0]

        n_matured = conn.execute(
            "SELECT COUNT(*) FROM predictions WHERE evaluation_status = 'evaluated'"
        ).fetchone()[0]

        brier_row = conn.execute(
            "SELECT AVG(brier_score) FROM predictions WHERE horizon_days = 5 AND brier_score IS NOT NULL"
        ).fetchone()

    return {
        "latest_created_at": dict(latest_row)["created_at"] if latest_row else "—",
        "system_version": dict(latest_row)["system_version"] if latest_row else "v1.0",
        "total_today": total_today,
        "total_tickers": total_tickers,
        "n_matured": n_matured,
        "brier_5d": brier_row[0] if brier_row and brier_row[0] else None,
    }


def _latest_as_of_date() -> date:
    """Return the most recent as_of_date in the DB, falling back to today."""
    try:
        with _conn() as conn:
            row = conn.execute(
                "SELECT MAX(as_of_date) FROM predictions WHERE as_of_date IS NOT NULL"
            ).fetchone()
        if row and row[0]:
            return date.fromisoformat(row[0])
    except Exception:
        pass
    return date.today()


# ── Company name lookup ───────────────────────────────────────────────────────

_COMPANY_NAMES: dict[str, str] = {
    # Portfolio
    "AAPL":  "Apple",
    "AJG":   "Arthur J. Gallagher",
    "AMZN":  "Amazon",
    "COST":  "Costco",
    "GOOG":  "Alphabet (C)",
    "GOOGL": "Alphabet (A)",
    "IVV":   "iShares S&P 500 ETF",
    "KO":    "Coca-Cola",
    "LLY":   "Eli Lilly",
    "LULU":  "Lululemon",
    "META":  "Meta Platforms",
    "MSFT":  "Microsoft",
    "NVDA":  "NVIDIA",
    "TSLA":  "Tesla",
    "VOO":   "Vanguard S&P 500 ETF",
    "WMT":   "Walmart",
    "XOM":   "ExxonMobil",
    # Watchlist — semis & hardware
    "AMD":   "AMD",
    "INTC":  "Intel",
    "ASML":  "ASML Holding",
    "MU":    "Micron Technology",
    "AMAT":  "Applied Materials",
    "KLAC":  "KLA Corporation",
    "LRCX":  "Lam Research",
    "MRVL":  "Marvell Technology",
    "MCHP":  "Microchip Technology",
    "NXPI":  "NXP Semiconductors",
    "ON":    "ON Semiconductor",
    "MPWR":  "Monolithic Power",
    "ARM":   "Arm Holdings",
    "SMCI":  "Super Micro Computer",
    "QCOM":  "Qualcomm",
    # Watchlist — software & cloud
    "PANW":  "Palo Alto Networks",
    "CRWD":  "CrowdStrike",
    "FTNT":  "Fortinet",
    "ZS":    "Zscaler",
    "NET":   "Cloudflare",
    "SNPS":  "Synopsys",
    "CDNS":  "Cadence Design",
    "WDAY":  "Workday",
    "ADBE":  "Adobe",
    "CRM":   "Salesforce",
    "ORCL":  "Oracle",
    "NOW":   "ServiceNow",
    "INTU":  "Intuit",
    "SNOW":  "Snowflake",
    "DDOG":  "Datadog",
    "HUBS":  "HubSpot",
    # Watchlist — fintech & payments
    "SHOP":  "Shopify",
    "SQ":    "Block",
    "PYPL":  "PayPal",
    "COIN":  "Coinbase",
    "HOOD":  "Robinhood",
    "SOFI":  "SoFi Technologies",
    "AFRM":  "Affirm",
    "UPST":  "Upstart",
    # Watchlist — consumer & travel
    "ABNB":  "Airbnb",
    "UBER":  "Uber",
    "DASH":  "DoorDash",
    "RIVN":  "Rivian",
    "NIO":   "NIO",
    # Watchlist — media & social
    "PLTR":  "Palantir",
    "RBLX":  "Roblox",
    "APP":   "Applovin",
    "DUOL":  "Duolingo",
    "ROKU":  "Roku",
    "PINS":  "Pinterest",
    "SNAP":  "Snap",
    "SPOT":  "Spotify",
    "TTD":   "The Trade Desk",
    "SE":    "Sea Limited",
    # Watchlist — other
    "IONQ":  "IonQ",
    "AGNC":  "AGNC Investment",
    "AXT":   "AXT Inc.",
    "AAR":   "AAR Corp",
    "BP":    "BP",
    "RXO":   "RXO Inc.",
    "HP":    "Helmerich & Payne",
    "BMW":   "BMW AG",
    "SPCX":  "SPCX",
}


def _cname(ticker: str) -> str:
    """Return company name for display, or empty string if unknown.

    Prefers the curated short names above (nicer for display, disambiguates
    share classes like GOOG/GOOGL); falls back to the SEC-registry lookup so
    tickers outside the fixed watchlist (e.g. trending/discovered candidates)
    still get a name.
    """
    curated = _COMPANY_NAMES.get(ticker.upper())
    if curated:
        return curated
    from portfolio_agent.tools.company_names import get_company_name
    return get_company_name(ticker) or ""


def _fetch_price_chart_data(ticker: str, period: str = "6mo") -> pd.DataFrame | None:
    """
    OHLCV + SMA20/50/200 + RSI14 as a full time series for charting (not the
    single latest-value snapshot get_technical_indicators returns — same
    formulas, computed across the whole window instead of just .iloc[-1]).
    """
    import yfinance as yf
    try:
        hist = yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=True)
    except Exception:
        return None
    if hist is None or hist.empty:
        return None

    close = hist["Close"]
    hist = hist.copy()
    hist["sma20"] = close.rolling(20).mean()
    hist["sma50"] = close.rolling(50).mean()
    hist["sma200"] = close.rolling(200).mean()
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss
    hist["rsi14"] = 100 - 100 / (1 + rs)
    return hist
