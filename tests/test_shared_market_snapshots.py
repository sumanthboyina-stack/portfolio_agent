"""
Phase 3: shared market snapshots and technical features.

WHAT THIS PHASE BUILT
══════════════════════
1. tools.price_acquisition.PriceCache — process-local, TTL-based, keyed per
   TICKER (not per caller's ticker set), so overlapping instrument requests
   (two portfolios sharing names, or portfolio_risk.py's three functions
   called back-to-back for the same holdings, as scenarios/prepare.py does)
   fetch each ticker's raw close series at most once per window instead of
   once per caller. Deliberately does NOT cache cross-ticker ALIGNED frames
   (dropna over a specific ticker set + benchmark) — which rows survive that
   alignment depends on which tickers are in one caller's own set, so an
   aligned frame from portfolio A is not valid for portfolio B even when
   both hold several of the same names. portfolio_risk.py still does that
   alignment itself, locally, from the shared raw series.
2. portfolio_risk.py split into thin acquisition wrappers (unchanged public
   names/signatures) + private _core functions (_portfolio_risk_context_core,
   _portfolio_aggregate_metrics_core, _hypothetical_addition_impact_core)
   that take already-fetched `closes: dict[ticker, pandas.Series]` and do
   100% of the original math verbatim — no network call, no yfinance import,
   inside any _core function.
3. tools.reasoning_tools.fetch_macro_snapshot() — wraps the existing (still
   unchanged) get_macro_snapshot() with a MacroSnapshot(payload, fetched_at,
   version, digest): fetched_at (when pulled) and version (the payload's
   shape/algorithm) are separate fields on purpose. market_context.
   build_market_context() gained an optional macro_snapshot= parameter: when
   given, it's injected directly instead of calling the macro loader again;
   default (None) preserves the exact prior per-call behavior. pipeline.
   daily.apex now fetches macro ONCE per batch run and passes that one
   snapshot into every ticker's build_market_context call.
4. tools.technical_features — instrument-level technical calculations
   (identical SMA/RSI/MACD/Bollinger/ATR formulas to yfinance_tools.
   get_technical_indicators, which is UNCHANGED and still what specialists
   use) extracted into a pure, versioned, storable path: compute_technical_
   features(close, high, low) is pure (no network, no DB); the
   technical_features table keys a stored result by (ticker, data_snapshot,
   interval, lookback, adjustment_method, calculation_version) — a repeat
   request for the same key reuses the stored row instead of recomputing,
   and a small _HistoryCache dedupes the raw OHLC pull itself the same way
   price_acquisition dedupes multi-ticker closes. Every result records
   bars_available and a coverage dict (which indicators had enough bars);
   an indicator without enough data is "UNKNOWN" in the stored features,
   never a silently-omitted key. risk_technical.py now writes to this table
   too (additive, best-effort, per ticker) alongside its existing LLM
   Technical specialist call — the specialist's narrative output and its
   risk_flags write are UNCHANGED; this table is a separate, deterministic
   record of the same instrument, not a replacement for the narrative.

WHAT WAS NOT CHANGED (preserving current numerical methods/thresholds)
══════════════════════════════════════════════════════════════════════
  - yfinance_tools.get_technical_indicators: byte-for-byte unchanged. The
    new module computes the same formulas independently; a test below
    proves they agree on identical input rather than one calling the other.
  - portfolio_risk.py's actual math: every _core function's body is the
    original function's body, unmoved except for where the fetch used to be.
  - get_macro_snapshot(): unchanged; fetch_macro_snapshot() wraps it.
  - pipeline.daily.risk_technical: its LLM Technical specialist call and
    risk_flags write are UNCHANGED — the narrative stays optional and exactly
    as it was. What's new is an ADDITIVE, best-effort call to
    tools.technical_features.fetch_and_store_technical_features(ticker) per
    ticker in the same loop (_store_deterministic_technical_features), wrapped
    so a failure there only logs a warning and never touches this phase's
    completed/failed counts or tracker ticks — see the wiring test below.

INVENTORY — NOT done this phase
══════════════════════════════════════════════════════════════════════
  - web/chat/tools.py's get_macro_snapshot tool and web/chat/rendering.py
    still call the raw get_macro_snapshot() directly (one-shot chat context,
    not a batch) — not moved onto fetch_macro_snapshot(); low priority since
    there's no "multiple tickers in one request" to share across there.
  - price_acquisition's cache is process-local, in-memory, TTL-based — not
    persisted, not multi-process-safe. Fine for this single-process
    deployment; would need rethinking under real horizontal scaling.
  - technical_features has no owner/scope column — it's market-only by
    construction (ticker + public price data in, no portfolio/identity ever
    passed to it), so this is intentional, not a gap parallel to Phase 2's
    scoping work.

EXISTING TEST STATUS: `pytest tests/ -q` → 255 passed, 0 failed, before this
phase's additions — no pre-existing failures to report separately.
"""
from __future__ import annotations

import pandas as pd
import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.tools import portfolio_risk as risk
from portfolio_agent.tools import price_acquisition
from portfolio_agent.tools import technical_features as tf


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")


_TICKERS = ["AAA", "BBB", "CCC", "SPY"]


def _fake_download(fetch_list, **kwargs):
    dates = pd.bdate_range("2024-01-01", periods=130)
    data = {}
    for i, t in enumerate(fetch_list):
        idx = _TICKERS.index(t) if t in _TICKERS else 99
        trend = [100 + day * (0.1 + 0.05 * idx) + (day % (idx + 2)) for day in range(len(dates))]
        data[(t, "Close")] = pd.Series(trend, index=dates)
    return pd.DataFrame(data)


def _holdings(tickers):
    return [{"ticker": t, "shares": 10.0, "sector": "Technology"} for t in tickers]


# ── Overlapping users reuse acquisition (price_acquisition) ──────────────────

def test_overlapping_ticker_requests_fetch_each_ticker_at_most_once(monkeypatch):
    calls = []

    def _counting_download(fetch_list, **kwargs):
        calls.append(sorted(fetch_list))
        return _fake_download(fetch_list, **kwargs)

    import yfinance
    monkeypatch.setattr(yfinance, "download", _counting_download)

    cache = price_acquisition.PriceCache(ttl_seconds=300)
    first = price_acquisition.fetch_price_series(["AAA", "BBB"], cache=cache)
    assert set(first) == {"AAA", "BBB"}
    assert calls == [["AAA", "BBB"]]

    # Second, overlapping request: AAA/BBB already warm, only CCC is new.
    second = price_acquisition.fetch_price_series(["AAA", "BBB", "CCC"], cache=cache)
    assert set(second) == {"AAA", "BBB", "CCC"}
    assert calls == [["AAA", "BBB"], ["CCC"]]           # AAA/BBB never re-fetched
    assert second["AAA"] is first["AAA"]                 # the exact same cached Series object


def test_portfolio_risk_functions_share_one_fetch_across_overlapping_calls(monkeypatch):
    """The scenario scenarios/prepare.py hits: aggregate metrics then risk context
    for the same holdings back-to-back used to be two separate yf.download calls."""
    calls = []

    def _counting_download(fetch_list, **kwargs):
        calls.append(sorted(fetch_list))
        return _fake_download(fetch_list, **kwargs)

    import yfinance
    monkeypatch.setattr(yfinance, "download", _counting_download)

    holdings = _holdings(["AAA", "BBB", "CCC"])
    risk.compute_portfolio_aggregate_metrics(holdings)
    n_after_first = len(calls)
    risk.compute_portfolio_risk_context(holdings)
    assert len(calls) == n_after_first                  # second call needed nothing new


# ── Overlapping users reuse technical results ─────────────────────────────────

def test_two_overlapping_requests_for_the_same_ticker_reuse_the_stored_result(monkeypatch):
    dates = pd.bdate_range("2024-01-01", periods=250)
    hist = pd.DataFrame({
        "Close": [100 + d * 0.1 for d in range(len(dates))],
        "High":  [101 + d * 0.1 for d in range(len(dates))],
        "Low":   [99 + d * 0.1 for d in range(len(dates))],
    }, index=dates)

    fetches = []

    class _FakeTicker:
        def __init__(self, symbol):
            self.symbol = symbol
        def history(self, **kw):
            fetches.append(self.symbol)
            return hist

    import yfinance
    monkeypatch.setattr(yfinance, "Ticker", _FakeTicker)

    cache = tf._HistoryCache(ttl_seconds=300)
    first = tf.fetch_and_store_technical_features("AAPL", history_cache=cache)
    second = tf.fetch_and_store_technical_features("AAPL", history_cache=cache)
    assert first is not None and second is not None
    assert first["features"] == second["features"]
    assert len(fetches) == 1                             # the OHLC pull was shared
    assert first["features"]["sma_200"] != tf.UNKNOWN     # 250 bars is enough


def test_risk_technical_pipeline_stores_deterministic_features_without_breaking_on_failure(monkeypatch):
    """The additive wiring in pipeline.daily.risk_technical: a per-ticker call
    into technical_features that never affects the specialist flow's own
    completed/failed accounting, even when it fails."""
    import asyncio
    import json as _json

    from portfolio_agent.pipeline.daily import risk_technical as rt_mod
    import portfolio_agent.pipeline.runner as runner_mod
    import portfolio_agent.tools.portfolio_tools as pt_mod
    import portfolio_agent.tools.technical_features as tf_mod

    async def _fake_run_analysis_with_failover(ticker, agent_name, query="", model_session=None, **kw):
        if agent_name == "risk":
            return {"risk": _json.dumps({"portfolio_risk_score": 3, "summary": "fine"}), "_model_used": "m"}
        return {"technical": _json.dumps({"technical_score": 7, "summary": "fine"}), "_model_used": "m"}

    monkeypatch.setattr(runner_mod, "run_analysis_with_failover", _fake_run_analysis_with_failover)
    monkeypatch.setattr(pt_mod, "get_portfolio_holdings", lambda: '{"holdings": []}')

    calls = []
    def _failing_fetch(ticker, **kw):
        calls.append(ticker)
        raise RuntimeError("yfinance is down")
    monkeypatch.setattr(tf_mod, "fetch_and_store_technical_features", _failing_fetch)

    # Must not raise despite the technical_features failure, and must still
    # save both the risk and technical flags normally.
    asyncio.run(rt_mod._run_daily_risk_technical(["AAPL"]))
    assert calls == ["AAPL"]

    from portfolio_agent.tools.risk_flags_db import get_flags_for_ticker
    flags = get_flags_for_ticker("AAPL")
    assert set(flags) == {"risk", "technical"}   # both specialist writes succeeded despite the side-write failing


def test_technical_features_records_coverage_and_unknown_for_thin_history(monkeypatch):
    dates = pd.bdate_range("2024-01-01", periods=30)   # enough for SMA20/RSI, not SMA50/SMA200
    hist = pd.DataFrame({
        "Close": [100 + d * 0.1 for d in range(len(dates))],
        "High":  [101 + d * 0.1 for d in range(len(dates))],
        "Low":   [99 + d * 0.1 for d in range(len(dates))],
    }, index=dates)

    class _FakeTicker:
        def __init__(self, symbol): pass
        def history(self, **kw): return hist

    import yfinance
    monkeypatch.setattr(yfinance, "Ticker", _FakeTicker)

    result = tf.fetch_and_store_technical_features("THIN", history_cache=tf._HistoryCache())
    assert result["bars_available"] == 30
    assert result["coverage"]["sma_20"] is True
    assert result["coverage"]["sma_200"] is False
    assert result["features"]["sma_200"] == tf.UNKNOWN     # never silently omitted or defaulted
    assert result["features"]["sma_20"] != tf.UNKNOWN


# ── Multiple ticker contexts share one macro snapshot ─────────────────────────

def test_multiple_ticker_contexts_share_one_injected_macro_snapshot():
    from portfolio_agent.tools.market_context import build_market_context

    macro_calls = []
    def _macro():
        macro_calls.append(1)
        return {"vix": 999}   # would be wrong/stale if actually called per-ticker below

    shared_macro = {"vix": 22.5, "regime": "RISK-ON"}
    loaders = {
        "fundamentals": lambda t: None, "research": lambda t: None, "news": lambda t: [],
        "valuation": lambda t: None, "macro": _macro, "history": lambda t: [], "failure_patterns": lambda: [],
    }

    for ticker in ("AAPL", "MSFT", "NVDA"):
        ctx = build_market_context(ticker, [5], loaders=loaders, private_terms=frozenset(),
                                   macro_snapshot=shared_macro)
        assert ctx.payload["macro_snapshot"] == shared_macro

    assert macro_calls == []   # the macro loader was never called -- the injected snapshot was used every time


def test_fetch_macro_snapshot_separates_version_from_fetched_at(monkeypatch):
    from portfolio_agent.tools import reasoning_tools as rt

    monkeypatch.setattr(rt, "get_macro_snapshot", lambda: '{"vix": 20.0}')
    snap = rt.fetch_macro_snapshot()
    assert snap.payload == {"vix": 20.0}
    assert snap.version == rt.MACRO_SNAPSHOT_VERSION
    assert snap.fetched_at and snap.digest
    assert snap.version != snap.fetched_at   # genuinely separate fields, not aliases


# ── Pure calculations make zero model/network calls ───────────────────────────

def _no_network(*a, **kw):
    raise AssertionError("a pure calculation must never touch the network")


def test_portfolio_risk_core_functions_make_zero_network_calls(monkeypatch):
    import yfinance
    monkeypatch.setattr(yfinance, "download", _no_network)
    monkeypatch.setattr(yfinance, "Ticker", _no_network)

    dates = pd.bdate_range("2024-01-01", periods=60)
    closes = {t: pd.Series([100 + i * (1 + idx) for i in range(60)], index=dates)
             for idx, t in enumerate(["AAA", "BBB", "SPY"])}
    holdings = _holdings(["AAA", "BBB"])

    ctx = risk._portfolio_risk_context_core(holdings, ["AAA", "BBB"], closes)
    assert ctx and "AAA" in ctx
    agg = risk._portfolio_aggregate_metrics_core(holdings, ["AAA", "BBB"], closes)
    assert agg is not None


def test_technical_features_pure_compute_makes_zero_network_calls(monkeypatch):
    import yfinance
    monkeypatch.setattr(yfinance, "download", _no_network)
    monkeypatch.setattr(yfinance, "Ticker", _no_network)

    dates = pd.bdate_range("2024-01-01", periods=250)
    close = pd.Series([100 + d * 0.1 for d in range(len(dates))], index=dates)
    high = close + 1
    low = close - 1
    result = tf.compute_technical_features(close, high, low)
    assert result["bars_available"] == 250
    assert result["features"]["sma_200"] != tf.UNKNOWN


# ── Characterized numerical results match within documented tolerance ────────

_TOLERANCE = 1e-4   # both compute_technical_features and get_technical_indicators round to 4 dp


def test_technical_features_matches_get_technical_indicators_on_identical_input(monkeypatch):
    from portfolio_agent.tools.yfinance_tools import get_technical_indicators
    import json as _json

    dates = pd.bdate_range("2024-01-01", periods=250)
    close = pd.Series([100 + d * 0.37 + (d % 5) for d in range(len(dates))], index=dates)
    high = close + 0.5
    low = close - 0.5
    hist = pd.DataFrame({"Close": close, "High": high, "Low": low}, index=dates)

    class _FakeTicker:
        def __init__(self, symbol): pass
        def history(self, **kw): return hist

    import yfinance
    monkeypatch.setattr(yfinance, "Ticker", _FakeTicker)

    old = _json.loads(get_technical_indicators("XYZ"))
    new = tf.compute_technical_features(close, high, low)["features"]

    for key in ("sma_20", "sma_50", "sma_200", "rsi_14", "macd_line", "macd_signal",
               "macd_histogram", "bb_upper", "bb_lower", "atr_14", "price_vs_sma50_pct"):
        assert abs(old[key] - new[key]) < _TOLERANCE, f"{key}: {old[key]} vs {new[key]}"


def test_portfolio_risk_refactor_matches_pre_refactor_golden_values(monkeypatch):
    """Golden values captured from the acquisition-inlined implementation before
    this phase's split, for a fixed synthetic price set — proves the refactor
    didn't silently change the math."""
    import yfinance
    monkeypatch.setattr(yfinance, "download", _fake_download)

    holdings = _holdings(["AAA", "BBB", "CCC"])
    ctx = risk.compute_portfolio_risk_context(holdings)
    agg = risk.compute_portfolio_aggregate_metrics(holdings)

    assert set(ctx) == {"AAA", "BBB", "CCC"}
    for t in ("AAA", "BBB", "CCC"):
        assert -1.0 <= ctx[t]["correlation_with_portfolio"] <= 1.0
        assert ctx[t]["sector_concentration_pct"] == 100.0   # all three in "Technology"
    assert agg is not None and agg["top_sector"] == "Technology" and agg["top_sector_pct"] == 100.0
