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
) -> pd.DataFrame:
    """Load latest prediction per ticker/horizon for the given date (or all dates if None)."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT id, ticker, as_of_date, recommendation, prediction,
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
                      trigger_type, trigger_event_id
               FROM predictions
               WHERE as_of_date IS NOT NULL
               ORDER BY as_of_date DESC, created_at DESC""",
        ).fetchall()

    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df

    if selected_date is not None:
        df = df[df["as_of_date"] == selected_date.isoformat()]

    if horizon_days is not None:
        df = df[df["horizon_days"] == horizon_days]

    if min_conviction > 0:
        df = df[df["conviction_score"].fillna(0) >= min_conviction]

    # Latest per ticker+horizon
    df = (
        df.sort_values("created_at", ascending=False)
          .drop_duplicates(subset=["ticker", "horizon_days"])
          .sort_values(["ticker", "horizon_days"])
    )
    return df.reset_index(drop=True)


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
