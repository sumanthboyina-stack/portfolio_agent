"""
Market-data provider adapter (yfinance).

The only module in the holdings stack that talks to the price provider. It
returns plain Python values and never touches the database, so services can
call it before opening a transaction and tests can stub `yfinance.download`.

    fetch_last_closes(tickers)               -> ({ticker: close}, as_of_date)
    fetch_daily_closes(tickers, start, end)  -> {ticker: {date: close}}
"""

from __future__ import annotations

_CHUNK = 20   # tickers per yfinance.download() call


def _chunks(tickers: list[str]) -> list[list[str]]:
    return [tickers[i:i + _CHUNK] for i in range(0, len(tickers), _CHUNK)]


def _close_series(data, chunk: list[str], ticker: str):
    return (data["Close"] if len(chunk) == 1 else data[ticker]["Close"]).dropna()


def _date_str(idx) -> str:
    return idx.date().isoformat() if hasattr(idx, "date") else str(idx)[:10]


def fetch_last_closes(tickers: list[str]) -> tuple[dict[str, float], str | None]:
    """
    Last close per ticker, plus the most recent trading date those closes are
    for. Tickers the provider cannot price are simply absent from the result;
    a failed download for one chunk never fails the others.
    """
    import yfinance as yf
    prices: dict[str, float] = {}
    as_of: str | None = None
    for chunk in _chunks(tickers):
        try:
            data = yf.download(" ".join(chunk), period="5d", interval="1d",
                               auto_adjust=True, progress=False, group_by="ticker")
        except Exception:
            continue
        for t in chunk:
            try:
                col = _close_series(data, chunk, t)
                prices[t] = float(col.iloc[-1])
                d = _date_str(col.index[-1])
                as_of = max(as_of, d) if as_of else d
            except Exception:
                pass
    return prices, as_of


def fetch_daily_closes(tickers: list[str], start: str, end: str) -> dict[str, dict[str, float]]:
    """
    Daily closes for *tickers* from *start* (inclusive) to *end* (exclusive),
    ISO dates. Days the market was shut are absent, never zero. Raises if the
    provider download itself fails so the caller can report the error; a
    ticker that cannot be parsed just comes back empty.
    """
    import yfinance as yf
    out: dict[str, dict[str, float]] = {t: {} for t in tickers}
    for chunk in _chunks(tickers):
        data = yf.download(" ".join(chunk), start=start, end=end, interval="1d",
                           auto_adjust=True, progress=False, group_by="ticker")
        for t in chunk:
            try:
                for idx, val in _close_series(data, chunk, t).items():
                    out[t][_date_str(idx)] = float(val)
            except Exception:
                pass
    return out
