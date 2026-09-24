"""Session-wide test fixtures."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_shared_price_cache():
    """
    tools.price_acquisition keeps a process-local cache so overlapping
    instrument requests within a real run share one fetch. That's exactly
    the behavior under test in some files and exactly what must NOT leak
    between tests in others (a ticker cached warm by one test's mocked
    yfinance data must never answer a different test's assertions) — reset
    it before every test.
    """
    from portfolio_agent.tools import portfolio_assessment, price_acquisition, technical_features
    price_acquisition._default_cache.invalidate_all()
    technical_features._default_history_cache.invalidate_all()
    portfolio_assessment._get_default_cache().invalidate_all()
    yield
    price_acquisition._default_cache.invalidate_all()
    technical_features._default_history_cache.invalidate_all()
    portfolio_assessment._get_default_cache().invalidate_all()
