"""Session-wide test fixtures."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

# SAFETY NET: the default database for the WHOLE test session is a throwaway file, set before any test runs. Individual tests
# still point DB_PATH at their own tmp file; but if one ever undoes that (monkeypatch.undo(), a fixture mistake), it falls back
# to THIS file — never to the real data/portfolio.db. (This guard exists because exactly that once wrote test rows into it.)
import portfolio_agent.tools.db as _apex_db_module

_apex_db_module.DB_PATH = Path(tempfile.mkdtemp(prefix="apex-tests-")) / "session.db"


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
    from portfolio_agent.tools import portfolio_health
    portfolio_health.clear_health_cache()
    yield
    price_acquisition._default_cache.invalidate_all()
    technical_features._default_history_cache.invalidate_all()
    portfolio_assessment._get_default_cache().invalidate_all()


@pytest.fixture(autouse=True)
def _no_live_instrument_lookups(monkeypatch):
    """
    Adding a position or committing an import classifies new instruments through yfinance. No test may reach
    the network: every lookup comes back empty (a failed lookup — classification is skipped, nothing raises).
    Tests of the classifier itself monkeypatch fetch_info with their own canned data.
    """
    from portfolio_agent.services import instrument_classifier
    monkeypatch.setattr(instrument_classifier, "fetch_info", lambda ticker: None)
    instrument_classifier._reset_failed_lookups()       # the failed-lookup memo is per process: don't leak between tests
    yield
    instrument_classifier._reset_failed_lookups()


@pytest.fixture(autouse=True)
def _no_live_fund_lookups(monkeypatch):
    """
    Fund data (holdings, sector mix, expense ratio) is fetched through yfinance by the default provider. No test may
    reach the network: unless a test passes its own provider, every fetch comes back empty with a reason code
    (a failed lookup — nothing raises, nothing is stored).
    """
    from portfolio_agent.tools import fund_data_providers as fdp
    monkeypatch.setattr(fdp.YFinanceFundProvider, "fetch",
                        lambda self, ticker: fdp.FundData(ticker=ticker.upper(), source="yfinance",
                                                          reason_codes=[fdp.RC_FUND_FETCH_FAILED]))


@pytest.fixture(autouse=True)
def _no_live_identifier_or_sec_calls(monkeypatch):
    """
    CUSIP/ISIN mapping (OpenFIGI) and SEC N-PORT fetches are network calls. No test may reach either: a client built without an injected
    session fails like a transient outage (nothing is cached, nothing raises). Tests of those modules pass a fake session.
    """
    from portfolio_agent.tools import identifier_mapping as idm
    from portfolio_agent.tools import sec_nport
    real_map, real_get = idm.OpenFigiClient.map, sec_nport.SecClient.get

    def _map(self, jobs):
        if self._session is None:
            raise idm.MappingError("live OpenFIGI calls are blocked in tests")
        return real_map(self, jobs)

    def _get(self, url, params=None):
        if self._session is None:
            raise sec_nport.SecError("live SEC calls are blocked in tests")
        return real_get(self, url, params)
    from portfolio_agent.tools import issuer_holdings
    real_dl = issuer_holdings.IssuerCSVProvider._download

    def _dl(self, url):
        if self._session is None:
            raise issuer_holdings.IssuerFileError("live issuer downloads are blocked in tests")
        return real_dl(self, url)
    monkeypatch.setattr(issuer_holdings.IssuerCSVProvider, "_download", _dl)
    monkeypatch.setattr(idm.OpenFigiClient, "map", _map)
    monkeypatch.setattr(sec_nport.SecClient, "get", _get)
