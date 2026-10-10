"""
Step 3 — ETF routing: a fund is forecast WITHOUT company fundamentals / valuation (weight exactly 0, renormalised, no
"No data" cap, no 5/10 placeholder stored), cash / money market / ticker-less funds / unknown-without-price are not
forecast at all, fund tickers never reach EDGAR or 8-K guidance extraction, instrument_type + leverage are stored on
each prediction, and validation can split its numbers by instrument type. The stock path must be unchanged.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import date, datetime, timezone

import pandas as pd
import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.domain import FUND_INSTRUMENT_TYPES
from portfolio_agent.services import instrument_classifier as ic
from portfolio_agent.services.context import system_context
from portfolio_agent.tools import holdings_db as repo
from portfolio_agent.tools import market_context as market_context_mod
from portfolio_agent.tools import prediction_db as pdb
from portfolio_agent.tools import reasoning_tools as rt_mod
from portfolio_agent.tools.apex_dual_run import ModelResult, SinglePrediction
from portfolio_agent.tools.instrument_reference import get_instrument_profiles
from portfolio_agent.tools.market_context import build_market_context
from portfolio_agent.tools.weight_engine import (
    HORIZON_BASE_WEIGHTS, WEIGHT_FLOOR, _normalize, compute_dynamic_weights, compute_dynamic_weights_for_horizon,
    excluded_sources,
)

FUNDS = ("ETF", "ETN", "MUTUAL_FUND", "TARGET_DATE_FUND")
CTX = system_context("test")


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")


def _typed(ticker: str, itype: str, leverage: float = 1.0, source: str = "manual") -> dict:
    """An instrument row with a fixed type (manual, so nothing tries to look it up)."""
    inst = repo.get_or_create_instrument(ticker)
    repo.update_instrument(inst["instrument_id"], instrument_type=itype, leverage_factor=leverage,
                           classification_source=source, classified_at=datetime.now(timezone.utc).isoformat())
    return repo.find_instrument(ticker)


# ── Weight engine ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("itype", FUNDS)
def test_fund_weights_zero_fundamentals_and_valuation_and_sum_to_one_on_every_horizon(itype):
    for h in HORIZON_BASE_WEIGHTS:
        for ctx in ({}, {"news": [{"headline": "Fed rate decision", "date": date.today().isoformat(), "sentiment_score": 0.7}],
                         "research": {"as_of_date": date.today().isoformat()}}):
            w = compute_dynamic_weights_for_horizon("X", h, ctx, {"vix": 31.0}, instrument_type=itype)
            assert w["fundamentals"] == 0.0 and w["valuation"] == 0.0
            assert round(sum(w.values()), 3) == 1.0 and sum(w.values()) == pytest.approx(1.0, abs=1e-9)
            assert all(w[k] > 0 for k in ("research", "macro", "news"))


def test_fund_weights_respect_the_floor_for_the_remaining_sources():
    for h in HORIZON_BASE_WEIGHTS:                      # nothing known: every remaining source sits at its floor-clamped raw
        w = compute_dynamic_weights_for_horizon("X", h, {}, {}, instrument_type="ETF")
        assert all(w[k] >= WEIGHT_FLOOR - 1e-9 for k in ("research", "macro", "news")), (h, w)


def test_floor_never_resurrects_an_excluded_source_and_rounding_remainder_goes_to_a_remaining_one():
    raw = {"fundamentals": 0.0, "valuation": 0.0, "research": 0.333, "macro": 0.333, "news": 0.334}
    w = _normalize(raw, use_floor=True, excluded=("fundamentals", "valuation"))
    assert w["fundamentals"] == 0.0 and w["valuation"] == 0.0 and round(sum(w.values()), 3) == 1.0
    odd = _normalize({"fundamentals": 9.0, "valuation": 9.0, "research": 1.0, "macro": 1.0, "news": 1.0},
                     excluded=("fundamentals", "valuation"))
    assert odd["fundamentals"] == 0.0 and round(sum(odd.values()), 3) == 1.0


def test_stock_path_is_numerically_unchanged_whatever_the_non_fund_type():
    kw = dict(news_data=[], research_data=None, macro_snapshot=None, fundamentals_data=None, horizons=[5, 21, 63, 250])
    base = compute_dynamic_weights("X", **kw)
    for it in ("STOCK", "UNKNOWN", "BOND", "OTHER", "MONEY_MARKET"):
        assert compute_dynamic_weights("X", instrument_type=it, **kw) == base
    assert excluded_sources("STOCK") == () and set(excluded_sources("ETF")) == {"fundamentals", "valuation"}
    assert base["weights"]["fundamentals"] > 0 and base["weights_by_horizon"][63]["fundamentals"] > 0
    assert set(base["data_caps"]) == {"fundamentals", "valuation", "research", "macro", "news"}


def test_fund_result_has_no_data_cap_for_excluded_sources_and_says_why():
    r = compute_dynamic_weights("X", news_data=[], research_data=None, macro_snapshot=None, fundamentals_data=None,
                                horizons=[5, 63], instrument_type="ETF")
    assert set(r["data_caps"]) == {"research", "macro", "news"}                      # no "fundamentals ≤ 5"
    assert r["weights"]["fundamentals"] == 0.0 and r["weights"]["valuation"] == 0.0
    assert "Not applicable to a fund" in r["per_source"]["fundamentals"]["rationale"]
    assert r["per_source"]["valuation"]["delta"].startswith("0%")


# ── Market context ────────────────────────────────────────────────────────────

NEWS = [{"headline": "steady", "sentiment": "NEUTRAL", "sentiment_score": 0.0, "published_at": "2026-09-20", "date": "2026-09-20"}]
RESEARCH = {"price_target_avg": 120.0, "current_price": 100.0, "num_analysts": 10, "as_of_date": "2026-09-15"}
FUND_ROW = {"as_of_date": "2026-08-01", "fundamental_score": 7}


def _loaders(itype=None, leverage=1.0, **over):
    def _boom(name):
        def f(*a, **k):
            raise AssertionError(f"{name} loader must not be called for a fund")
        return f
    base = {"research": lambda t: RESEARCH, "news": lambda t: NEWS, "macro": lambda: {"vix": 20.0},
            "history": lambda t: [], "failure_patterns": lambda: [], "technical": lambda t: None}
    if itype in FUNDS:
        base |= {"fundamentals": _boom("fundamentals"), "valuation": _boom("valuation")}
    else:
        base |= {"fundamentals": lambda t: FUND_ROW, "valuation": lambda t: {"as_of_date": "2026-09-01"}}
    if itype is not None:
        base["instrument"] = lambda t: {"instrument_type": itype, "leverage_factor": leverage, "asset_class": "US_EQUITY"}
    base.update(over)
    return base


@pytest.mark.parametrize("itype", FUNDS)
def test_etf_context_runs_no_fundamentals_or_valuation_call_and_adds_no_placeholder(itype):
    ctx = build_market_context("SPY", [5, 63], loaders=_loaders(itype))          # a loader call would raise AssertionError
    p = ctx.payload
    assert p["fundamentals"] is None and p["valuation"] is None
    assert p["data_gaps"] == []                                                  # absence is routing, not a gap
    assert all(w["fundamentals"] == 0.0 and w["valuation"] == 0.0 for w in p["dynamic_weights"]["weights_by_horizon"].values())
    wi = p["weight_instruction"]
    assert "fundamentals ≤" not in wi and "valuation ≤" not in wi                # no "No data" cap on them
    assert "Do NOT score the FUNDAMENTAL or VALUATION analysts" in wi and "never a placeholder like 5" in wi
    assert p["instrument"]["instrument_type"] == itype
    assert "fund_context" in p and p["fund_context"] is None                      # hook for the later ETF-context task
    assert ctx.instrument_type == itype and ctx.evidence_refs["instrument_type"] == itype


def test_fund_with_missing_research_and_news_reports_only_those_gaps():
    ctx = build_market_context("SPY", [5], loaders=_loaders("ETF", research=lambda t: None, news=lambda t: []))
    assert ctx.payload["data_gaps"] == ["research", "news"]


def test_leveraged_and_inverse_funds_are_flagged_in_the_prompt_and_context():
    for lev, word in ((3.0, "3x leveraged"), (-1.0, "1x inverse"), (-2.0, "2x inverse")):
        ctx = build_market_context("TQQQ", [5], loaders=_loaders("ETF", leverage=lev))
        assert word in ctx.payload["weight_instruction"] and ctx.leverage_factor == lev
    plain = build_market_context("SPY", [5], loaders=_loaders("ETF", leverage=1.0))
    assert "leveraged" not in plain.payload["weight_instruction"] and "inverse" not in plain.payload["weight_instruction"]


def test_stock_context_is_byte_identical_with_or_without_instrument_info():
    a = build_market_context("AAPL", [5, 21], loaders=_loaders(None))
    b = build_market_context("AAPL", [5, 21], loaders=_loaders("STOCK"))
    c = build_market_context("AAPL", [5, 21], loaders=_loaders("UNKNOWN"))
    assert a.payload == b.payload == c.payload and a.digest == b.digest == c.digest
    assert "instrument" not in a.payload and "fund_context" not in a.payload
    assert a.payload["data_gaps"] == [] and "fundamentals" in a.payload["dynamic_weights"]["data_caps"]


def test_default_loader_reads_the_instrument_row_from_the_instruments_table_only():
    _typed("SPY", "ETF")
    assert get_instrument_profiles(["SPY", "NOPE"]) == {"SPY": {"instrument_type": "ETF", "asset_class": "UNKNOWN", "leverage_factor": 1.0}}
    from portfolio_agent.tools.market_context import default_loaders
    assert default_loaders()["instrument"]("SPY")["instrument_type"] == "ETF"


# ── The APEX phase ────────────────────────────────────────────────────────────

def _full_loaders():
    """Stand-in for default_loaders(): fake data, but the instrument lookup is real (reads the test DB)."""
    base = _loaders(None, fundamentals=lambda t: FUND_ROW, valuation=lambda t: {"as_of_date": "2026-09-01"})
    base["instrument"] = lambda t: get_instrument_profiles([t]).get(t.upper())
    # a fund must never reach these two
    real_f, real_v = base["fundamentals"], base["valuation"]
    base["fundamentals"] = lambda t: (_ for _ in ()).throw(AssertionError("fundamentals loader called for a fund")) \
        if (get_instrument_profiles([t]).get(t.upper()) or {}).get("instrument_type") in FUND_INSTRUMENT_TYPES else real_f(t)
    base["valuation"] = lambda t: (_ for _ in ()).throw(AssertionError("valuation loader called for a fund")) \
        if (get_instrument_profiles([t]).get(t.upper()) or {}).get("instrument_type") in FUND_INSTRUMENT_TYPES else real_v(t)
    return base


def _model_result(ticker: str) -> ModelResult:
    """A model that (wrongly, for a fund) scores fundamentals and valuation 5/10 — the placeholder this step removes."""
    data = {"prediction": "BULLISH", "recommendation": "BUY", "confidence": 7, "composite_score": 6.0,
            "fundamental_score": 5, "valuation_score": 5, "research_score": 7, "macro_score": 6, "news_score": 6,
            "weights_used": {"fundamentals": 0.25, "valuation": 0.2, "research": 0.25, "macro": 0.15, "news": 0.15},
            "horizons": [{"horizon_days": 5, "predicted_direction": "UP", "predicted_return_low": 1.0, "predicted_return_high": 3.0,
                          "conviction_score": 6, "reasoning_text": "x"}]}
    return ModelResult(merged=data, individuals=[SinglePrediction("m", "x", "Claude-Sonnet", data, 5, True)],
                       model_sources=["Claude-Sonnet"], used_fallback=False)


def _run_apex(monkeypatch, tickers, *, no_price=()):
    from portfolio_agent.tools import apex_dual_run as adr
    from portfolio_agent.tools import yfinance_tools as yft
    monkeypatch.setattr(market_context_mod, "default_loaders", _full_loaders)
    monkeypatch.setattr(rt_mod, "get_macro_snapshot", lambda: '{"vix": 20.0}')
    monkeypatch.setattr(pdb, "is_trading_day", lambda d: True)
    monkeypatch.setattr(yft, "get_close", lambda t, d: None if t in no_price else 100.0)
    monkeypatch.setattr(yft, "get_trailing_return", lambda t, d: 0.01)
    called: list[str] = []

    async def _fake_run_apex(ticker, prompt):
        called.append(ticker)
        return _model_result(ticker)
    monkeypatch.setattr(adr, "run_apex", _fake_run_apex)
    import portfolio_agent.pipeline.daily.apex as apex_mod
    asyncio.run(apex_mod._run_daily_apex(tickers, scheduled_horizons_override=[5]))
    return called


def _rows() -> dict[str, dict]:
    with sqlite3.connect(str(db_module.DB_PATH)) as c:
        c.row_factory = sqlite3.Row
        return {r["ticker"]: dict(r) for r in c.execute("SELECT * FROM predictions")}


def test_apex_routes_funds_stores_no_placeholder_and_skips_cash_ciT_and_unpriceable(monkeypatch, caplog):
    _typed("AAPL", "STOCK"); _typed("SPY", "ETF"); _typed("TQQQ", "ETF", leverage=3.0); _typed("SQQQ", "ETF", leverage=-3.0)
    _typed("SPAXX", "MONEY_MARKET"); _typed("CASHX", "CASH")
    repo.get_or_create_instrument("CIT:ACME-S-P-500")                    # typed CIT on creation
    caplog.set_level(logging.INFO)
    called = _run_apex(monkeypatch, ["AAPL", "SPY", "TQQQ", "SQQQ", "SPAXX", "CASHX", "CIT:ACME-S-P-500", "ZZZ", "NEWCO"],
                       no_price=("ZZZ",))

    assert sorted(called) == ["AAPL", "NEWCO", "SPY", "SQQQ", "TQQQ"]          # NEWCO: unknown type but priced → stock path
    rows = _rows()
    assert set(rows) == {"AAPL", "SPY", "TQQQ", "SQQQ", "NEWCO"}

    for t in ("SPY", "TQQQ", "SQQQ"):                                         # funds: no 5/10 placeholder is stored
        r = rows[t]
        assert (r["instrument_type"], r["fundamental_score"], r["valuation_score"]) == ("ETF", None, None)
        assert r["research_score"] == 7 and r["macro_score"] == 6 and r["news_score"] == 6
        import json
        w = json.loads(r["weights_used"])                                       # the engine's weights, not the model's echo
        assert w["fundamentals"] == 0.0 and w["valuation"] == 0.0 and sum(w.values()) == pytest.approx(1.0, abs=1e-6)
    assert (rows["TQQQ"]["leverage_factor"], rows["SQQQ"]["leverage_factor"], rows["SPY"]["leverage_factor"]) == (3.0, -3.0, 1.0)
    assert (rows["AAPL"]["instrument_type"], rows["AAPL"]["fundamental_score"], rows["AAPL"]["valuation_score"]) == ("STOCK", 5, 5)
    assert (rows["NEWCO"]["instrument_type"], rows["NEWCO"]["fundamental_score"]) == ("UNKNOWN", 5)   # stock path, unchanged

    log_text = caplog.text
    for t in ("SPAXX", "CASHX", "CIT:ACME-S-P-500"):
        assert f"[skip] {t}" in log_text
    assert "[skip] ZZZ — type unknown and no price available" in log_text and "money market — nothing to forecast" in log_text


def test_pipeline_gives_unheld_watchlist_tickers_an_instrument_row_and_type(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(ic, "fetch_info", lambda t: seen.append(t) or {"quoteType": "ETF", "longName": "Some Index ETF",
                                                                         "category": "Large Blend"})
    called = _run_apex(monkeypatch, ["WATCHETF"])
    assert seen == ["WATCHETF"] and called == ["WATCHETF"]
    inst = repo.find_instrument("WATCHETF")
    assert (inst["instrument_type"], inst["classification_source"]) == ("ETF", "auto")
    r = _rows()["WATCHETF"]
    assert (r["instrument_type"], r["fundamental_score"], r["valuation_score"]) == ("ETF", None, None)


# ── ensure_classified / split_company_filers ──────────────────────────────────

def test_ensure_classified_creates_rows_leaves_manual_alone_and_remembers_failures(monkeypatch):
    _typed("MANUAL1", "STOCK")
    calls: list[str] = []
    table = {"NEWETF": {"quoteType": "ETF", "longName": "X ETF"}, "BROKEN": None}
    monkeypatch.setattr(ic, "fetch_info", lambda t: calls.append(t) or table.get(t))
    types = ic.ensure_classified(CTX, ["NEWETF", "MANUAL1", "BROKEN", "CIT:A-B", ""])
    assert types["NEWETF"] == "ETF" and types["MANUAL1"] == "STOCK" and types["CIT:A-B"] == "CIT"
    assert types["BROKEN"] == "UNKNOWN" and sorted(calls) == ["BROKEN", "NEWETF"]    # a manual one is never looked up
    calls.clear()
    types2 = ic.ensure_classified(CTX, ["NEWETF", "BROKEN"])
    assert calls == [] and types2["NEWETF"] == "ETF"                               # classified: not re-fetched; failure remembered
    assert [e["operation"] for e in repo.get_audit_events()].count("create_instruments") == 1


def test_split_company_filers_separates_funds_cash_and_ciT_from_filers():
    for t, k in (("SPY", "ETF"), ("VFIAX", "MUTUAL_FUND"), ("SPAXX", "MONEY_MARKET"), ("AAPL", "STOCK")):
        _typed(t, k)
    filers, skipped = ic.split_company_filers(CTX, ["AAPL", "SPY", "VFIAX", "SPAXX", "CIT:A-B", "UNSEEN"])
    assert filers == ["AAPL", "UNSEEN"]                                            # unclassified stays on the unchanged path
    assert skipped == {"SPY": "ETF", "VFIAX": "MUTUAL_FUND", "SPAXX": "MONEY_MARKET", "CIT:A-B": "CIT"}


# ── EDGAR / 8-K guidance / valuation phases skip funds cleanly ────────────────

def test_fundamentals_phase_never_reaches_edgar_or_guidance_for_fund_tickers(monkeypatch, tmp_path, caplog):
    from portfolio_agent.pipeline.daily import fundamentals as fmod
    from portfolio_agent.tools import checkpoint, edgar, edgar_check, fundamentals_db, pipeline_skip
    for t, k in (("SPY", "ETF"), ("VFIAX", "MUTUAL_FUND"), ("AAPL", "STOCK")):
        _typed(t, k)
    monkeypatch.setattr(checkpoint, "_CHECKPOINT_PATH", tmp_path / "ckpt.json")
    monkeypatch.setattr(pipeline_skip, "get_skip_tickers", lambda phase: set())
    monkeypatch.setattr(fundamentals_db, "needs_refresh", lambda t, d=None: (True, "new"))
    checked: list[list[str]] = []
    prefetched: list[list[str]] = []
    guidance: list[str] = []
    monkeypatch.setattr(edgar_check, "batch_check_filings", lambda ts: checked.append(list(ts)) or {})
    monkeypatch.setattr(edgar_check, "_load_cik_map", lambda: {})
    monkeypatch.setattr(edgar, "batch_prefetch_fundamentals",
                        lambda ts, cik, max_workers=4: prefetched.append(list(ts)) or {t: {"error": "no data"} for t in ts})
    monkeypatch.setattr(edgar, "get_earnings_guidance", lambda t, cik=None: guidance.append(t) or {})

    async def _no_llm(**kw):
        assert kw["has_edgar"] == []
        return {}, False
    monkeypatch.setattr(fmod, "_run_fundamentals_llm_batches", _no_llm)
    monkeypatch.setattr(fmod, "_persist_fundamentals", lambda **kw: ([], []))
    caplog.set_level(logging.INFO)

    ok = asyncio.run(fmod._run_daily_fundamentals(["AAPL", "SPY", "VFIAX"], always_run={"SPY", "AAPL"}))
    assert ok is True
    assert checked == [["AAPL"]] and prefetched == [["AAPL"]] and guidance == []
    assert "[skip] 2 fund / non-company ticker(s)" in caplog.text and "SPY" in caplog.text
    noise = [ln for ln in caplog.text.splitlines() if "no EDGAR data" in ln]          # the old "(ETFs/foreign)" warning line
    assert not any("SPY" in ln or "VFIAX" in ln for ln in noise)                      # ...never names a fund now


def test_valuation_phase_skips_fund_tickers_before_any_edgar_call(monkeypatch, caplog):
    from portfolio_agent.pipeline.daily import valuation as vmod
    from portfolio_agent.tools import valuation_db
    for t, k in (("SPY", "ETF"), ("AAPL", "STOCK")):
        _typed(t, k)
    with pdb._db():                                                                  # the phase also reads predictions
        pass
    asked: list[str] = []
    monkeypatch.setattr(valuation_db, "needs_refresh", lambda t: asked.append(t) or False)
    caplog.set_level(logging.INFO)
    asyncio.run(vmod._run_daily_valuation(["AAPL", "SPY"]))
    assert asked == ["AAPL"] and "no DCF valuation applies: SPY" in caplog.text


# ── Storage: columns, migration, backfill ─────────────────────────────────────

def test_prediction_columns_migrate_idempotently_and_old_rows_stay_null_until_backfilled():
    with sqlite3.connect(str(db_module.DB_PATH)) as c:
        pdb._create_schema(c)                                                      # pre-Step-3 shape
        assert "instrument_type" not in {r[1] for r in c.execute("PRAGMA table_info(predictions)")}
        c.executemany("INSERT INTO predictions (ticker, created_at, prediction, recommendation) VALUES (?, '2026-09-01', 'x', 'BUY')",
                      [("SPY",), ("AAPL",), ("ZZZ",), ("OLD",)])
    _typed("SPY", "ETF", leverage=1.0); _typed("AAPL", "STOCK"); repo.get_or_create_instrument("ZZZ")     # ZZZ stays UNKNOWN
    with pdb._db(): pass
    with pdb._db(): pass                                                           # idempotent
    with sqlite3.connect(str(db_module.DB_PATH)) as c:
        cols = [r[1] for r in c.execute("PRAGMA table_info(predictions)")]
        assert cols.count("instrument_type") == 1 and cols.count("leverage_factor") == 1
        assert {r[0] for r in c.execute("SELECT instrument_type FROM predictions")} == {None}

    assert pdb.backfill_instrument_types() == 2                                    # SPY + AAPL; UNKNOWN / unseen untouched
    assert pdb.backfill_instrument_types() == 0                                    # idempotent
    with sqlite3.connect(str(db_module.DB_PATH)) as c:
        assert dict(c.execute("SELECT ticker, instrument_type FROM predictions")) == {"SPY": "ETF", "AAPL": "STOCK", "ZZZ": None, "OLD": None}


def test_insert_prediction_stores_type_and_leverage_and_defaults_to_null():
    pdb.insert_prediction(ticker="TQQQ", prediction="BULLISH", recommendation="BUY", horizon_days=5,
                          instrument_type="ETF", leverage_factor=3.0)
    pdb.insert_prediction(ticker="OLD", prediction="BULLISH", recommendation="BUY", horizon_days=5)
    rows = _rows()
    assert (rows["TQQQ"]["instrument_type"], rows["TQQQ"]["leverage_factor"]) == ("ETF", 3.0)
    assert (rows["OLD"]["instrument_type"], rows["OLD"]["leverage_factor"]) == (None, None)


def test_backfill_script_stamps_predictions_after_classifying(monkeypatch, capsys):
    from scripts import backfill_instrument_classification as script
    repo.get_or_create_instrument("SPY")
    with sqlite3.connect(str(db_module.DB_PATH)) as c:
        pdb._create_schema(c); pdb._migrate(c)
        c.execute("INSERT INTO predictions (ticker, created_at, prediction, recommendation) VALUES ('SPY', '2026-09-01', 'x', 'BUY')")
    monkeypatch.setattr(ic, "fetch_info", lambda t: {"quoteType": "ETF", "longName": "SPDR S&P 500 ETF Trust"})
    assert script.main(["--stamp-predictions"]) == 0
    assert "stamped instrument_type on 1 older prediction(s)" in capsys.readouterr().out
    assert _rows()["SPY"]["instrument_type"] == "ETF"


# ── Validation by instrument type ─────────────────────────────────────────────

def _evaluated(ticker, itype, *, pred="UP", actual=0.03, leverage=None, role="champion", horizon=5, as_of="2026-09-01", **extra):
    pdb.insert_prediction(ticker=ticker, prediction="BULLISH", recommendation="BUY", horizon_days=horizon,
                          predicted_direction=pred, model_name="Claude-Sonnet", scope=pdb.SCOPE_SHARED, context_digest="t",
                          prediction_role=role, instrument_type=itype, leverage_factor=leverage, **extra)
    with sqlite3.connect(str(db_module.DB_PATH)) as c:
        c.execute("""UPDATE predictions SET as_of_date=?, evaluation_status='evaluated', evaluated_at=?, actual_return=?,
                     actual_direction=?, outcome='directionally_correct', excess_return=?, brier_score=0.2
                     WHERE id=(SELECT MAX(id) FROM predictions)""",
                  [as_of, as_of, actual, "UP" if actual > 0.01 else "DOWN", actual - 0.01])


def test_validation_engine_breakdown_by_instrument_type(monkeypatch):
    from portfolio_agent.tools import validation_engine as ve
    monkeypatch.setattr(ve, "_today_cst", lambda: date(2026, 9, 10))
    _evaluated("AAPL", "STOCK", pred="UP", actual=0.03)
    _evaluated("MSFT", "STOCK", pred="UP", actual=-0.03)
    _evaluated("SPY", "ETF", pred="UP", actual=0.03)
    _evaluated("TQQQ", "ETF", pred="UP", actual=0.05, leverage=3.0)
    _evaluated("OLD", None, pred="DOWN", actual=-0.04)                               # made before the column
    _evaluated("HID", "ETF", pred="UP", actual=0.03, role="challenger")              # default roles exclude it
    _evaluated("LEG", "ETF", pred="UP", actual=0.03, role="legacy_random")

    by = {r["instrument_type"]: r for r in ve.get_instrument_type_breakdown(lookback_days=30)}
    assert set(by) == {"STOCK", "ETF", ve.UNCLASSIFIED}
    assert (by["STOCK"]["n"], by["STOCK"]["dir_acc"]) == (2, 0.5)
    assert (by["ETF"]["n"], by["ETF"]["dir_acc"], by["ETF"]["n_leveraged"]) == (2, 1.0, 1)
    assert by[ve.UNCLASSIFIED]["n"] == 1 and by["ETF"]["horizon_days"] == 5
    assert by["STOCK"]["mean_excess"] == pytest.approx(-0.01, abs=1e-9)        # (0.03-0.01 and -0.03-0.01) / 2 and by["ETF"]["brier"] == pytest.approx(0.2)
    with_legacy = {r["instrument_type"]: r["n"] for r in
                   ve.get_instrument_type_breakdown(lookback_days=30, roles=pdb.VALIDATION_ROLES_WITH_LEGACY)}
    assert with_legacy["ETF"] == 3 and ve.get_instrument_type_breakdown(lookback_days=30, model_names=["nobody"]) == []


def test_pandas_breakdown_by_instrument_type_uses_the_standard_yardsticks():
    from portfolio_agent.tools.validation_roles import instrument_type_breakdown
    rows = [("AAPL", "STOCK", "UP", 0.03, None), ("MSFT", "STOCK", "UP", -0.03, None), ("SPY", "ETF", "UP", 0.03, 1.0),
            ("TQQQ", "ETF", "DOWN", 0.05, 3.0), ("OLD", None, "UP", 0.02, None)]
    df = pd.DataFrame([{"id": i, "ticker": t, "as_of_date": "2026-09-01", "horizon_days": 5, "instrument_type": it,
                        "predicted_direction": p, "actual_return": a, "leverage_factor": lev}
                       for i, (t, it, p, a, lev) in enumerate(rows)])
    out = {r["instrument_type"]: r for r in instrument_type_breakdown(df)}
    assert set(out) == {"STOCK", "ETF", "UNCLASSIFIED"}
    assert (out["STOCK"]["n"], out["STOCK"]["accuracy"]) == (2, 0.5) and (out["ETF"]["n"], out["ETF"]["accuracy"]) == (2, 0.5)
    assert out["ETF"]["leveraged"] == 1 and out["STOCK"]["leveraged"] == 0
    assert instrument_type_breakdown(pd.DataFrame()) == []


def _qa_page():
    import runpy
    import sys
    import types
    from pathlib import Path
    root = Path.cwd()
    sys.path.insert(0, str(root))
    import web.auth as auth
    import web.styles as styles
    from portfolio_agent.services.context import local_context
    styles.top_nav = lambda *a, **k: None
    auth.require_login = lambda: None
    auth.current_user = lambda: types.SimpleNamespace(is_admin=True)
    auth.current_context = lambda source: local_context(source)
    runpy.run_path(str(root / "web/pages/11_🔬_Validation_QA.py"), run_name="__main__")


def test_validation_qa_page_shows_the_instrument_types_tab(monkeypatch):
    from datetime import timedelta
    from streamlit.testing.v1 import AppTest
    import web.data.validation as wdv
    monkeypatch.setattr(wdv, "_DB", db_module.DB_PATH)
    wdv._price_cache.clear()
    for i in range(6):
        day = (date.today() - timedelta(days=10 + i)).isoformat()
        _evaluated(f"S{i}", "STOCK", as_of=day)
        _evaluated(f"E{i}", "ETF", as_of=day, pred="DOWN")
    at = AppTest.from_function(_qa_page, default_timeout=120)
    at.session_state["valqa_segment"] = "All"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert "Instrument types" in at.tabs[13].label
    tables = [df.value for df in at.dataframe if "Type" in df.value.columns and "Leveraged / inverse" in df.value.columns]
    assert tables and set(tables[0]["Type"]) == {"STOCK", "ETF"}
