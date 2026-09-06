from __future__ import annotations

import pandas as pd
import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.tools.portfolio_risk import (
    compute_hypothetical_addition_impact,
    compute_portfolio_fit_score,
)

_TICKERS = ["AAA", "BBB", "CCC", "DDD", "SPY"]


def _fake_download(fetch_list, **kwargs):
    """Synthetic 6mo daily closes with distinct (non-degenerate) trends per
    ticker, shaped like yfinance's group_by='ticker' MultiIndex output."""
    dates = pd.bdate_range("2024-01-01", periods=130)
    data = {}
    for i, t in enumerate(_TICKERS):
        # Different trend + a little ticker-specific noise so correlations
        # aren't all exactly 1.0 or 0.0 by construction.
        trend = [100 + day * (0.1 + 0.05 * i) + (day % (i + 2)) for day in range(len(dates))]
        data[(t, "Close")] = pd.Series(trend, index=dates)
        data[(t, "Volume")] = pd.Series([1_000_000] * len(dates), index=dates)
    return pd.DataFrame(data)


@pytest.fixture(autouse=True)
def _patch_network(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    import yfinance
    monkeypatch.setattr(yfinance, "download", _fake_download)


def _holdings(tickers):
    return [{"ticker": t, "shares": 10.0, "sector": "Technology"} for t in tickers]


def test_compute_hypothetical_addition_impact_excludes_ticker_from_own_holdings():
    # AAA is already "held" — passing it in holdings must not degenerate its
    # own correlation-with-portfolio to 1.0 by comparing it against itself.
    holdings = _holdings(["AAA", "BBB", "CCC"])
    impact = compute_hypothetical_addition_impact("AAA", holdings)
    assert impact is not None
    assert impact["correlation_with_portfolio"] != 1.0


def test_compute_hypothetical_addition_impact_same_result_whether_or_not_ticker_pre_included():
    # Scoring AAA should give the same answer whether or not the caller's
    # holdings list happens to already include AAA — the function must
    # normalize this itself, not rely on callers remembering to filter.
    without_self = compute_hypothetical_addition_impact("AAA", _holdings(["BBB", "CCC"]))
    with_self = compute_hypothetical_addition_impact("AAA", _holdings(["AAA", "BBB", "CCC"]))
    assert without_self is not None and with_self is not None
    assert without_self["correlation_with_portfolio"] == with_self["correlation_with_portfolio"]


def test_compute_portfolio_fit_score_in_range_for_held_and_unheld_tickers():
    holdings = _holdings(["AAA", "BBB", "CCC"])
    for ticker in ("AAA", "DDD"):  # AAA is held, DDD is not
        score = compute_portfolio_fit_score(ticker, holdings)
        assert score is not None
        assert 0.0 <= score <= 100.0


def test_compute_portfolio_fit_score_none_with_insufficient_holdings():
    assert compute_portfolio_fit_score("AAA", _holdings(["AAA"])) is None
