"""
Phase 1: market-only context, writer, and history boundary.

  - build_market_context never touches a profile/holdings/chat state, and
    identical explicit inputs give an identical context regardless of who asks.
  - write_shared_forecast only accepts a market-only MarketContext from an
    allow-listed origin, and refuses text containing a private term.
  - write_private_forecast always lands as SCOPE_PRIVATE, tied to its owner.
  - legacy_unclassified rows are excluded from shared reads and failure-pattern
    mining, and reachable only through the restricted legacy review path.
  - fixed-input weights and guardrails are unchanged from before the refactor.
"""
from __future__ import annotations

from decimal import Decimal as X

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.pipeline.daily.apex import _apply_conviction_guardrails
from portfolio_agent.privacy import PrivateDataLeak, assert_market_only, find_private_terms
from portfolio_agent.tools import prediction_db as pdb
from portfolio_agent.tools.forecast_writer import ForecastProvenance, write_private_forecast, write_shared_forecast
from portfolio_agent.tools.market_context import MarketContext, build_market_context
from portfolio_agent.tools.weight_engine import compute_dynamic_weights, compute_dynamic_weights_for_horizon


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")


NEWS = [{"headline": "Company beats earnings estimates", "sentiment": "POSITIVE", "sentiment_score": 0.6,
        "published_at": "2026-09-20", "date": "2026-09-20"}]
RESEARCH = {"price_target_avg": 120.0, "current_price": 100.0, "num_analysts": 12,
           "as_of_date": "2026-09-15", "consensus": "BUY"}
MACRO = {"vix": 22.5, "regime": "RISK-ON", "sp500_trend": "UP"}
FUND = {"as_of_date": "2026-08-01", "fundamental_score": 7, "filing_type": "10-Q", "filing_date": "2026-08-01"}
VAL = {"as_of_date": "2026-09-01", "intrinsic_value": 110.0}


def _loaders(**over):
    base = {"fundamentals": lambda t: FUND, "research": lambda t: RESEARCH, "news": lambda t: NEWS,
            "valuation": lambda t: VAL, "macro": lambda: MACRO, "history": lambda t: [],
            "failure_patterns": lambda: []}
    base.update(over)
    return base


# ── Fixed-input weights and guardrails are unchanged (golden values captured pre-refactor) ──

def test_fixed_input_weights_match_pre_refactor_golden_values():
    out = compute_dynamic_weights("TEST", NEWS, RESEARCH, MACRO, FUND, horizons=[5, 21, 63], valuation_data=VAL)
    assert out["regime"] == "MIXED"
    assert out["data_caps"] == {"fundamentals": 10, "macro": 10, "news": 10, "research": 10, "valuation": 10}
    assert out["weights"] == {"fundamentals": 0.198, "macro": 0.281, "news": 0.132, "research": 0.207, "valuation": 0.182}
    assert out["weights_by_horizon"][5] == {"fundamentals": 0.029, "macro": 0.154, "news": 0.651, "research": 0.136, "valuation": 0.03}
    assert out["weights_by_horizon"][21] == {"fundamentals": 0.123, "macro": 0.188, "news": 0.406, "research": 0.196, "valuation": 0.087}
    assert out["weights_by_horizon"][63] == {"fundamentals": 0.204, "macro": 0.271, "news": 0.207, "research": 0.159, "valuation": 0.159}
    h21 = compute_dynamic_weights_for_horizon("TEST", 21, {"news": NEWS, "research": RESEARCH, "fundamentals": FUND, "valuation": VAL}, MACRO)
    assert h21 == out["weights_by_horizon"][21]


def test_fixed_input_guardrails_match_pre_refactor_golden_values():
    cases = [("UP", 8.0, False, 0.01), ("UP", 8.0, True, 0.12), ("DOWN", 7.5, True, -0.15),
             ("UP", 4.0, False, None), ("FLAT", 9.0, True, 0.0)]
    assert [_apply_conviction_guardrails(d, c, hn, tr) for d, c, hn, tr in cases] == [
        (5, ["no_news_bullish_capped"]), (8.0, []), (7.5, []), (4.0, []), (9.0, []),
    ]


# ── build_market_context: no profile/holdings dependency, deterministic for fixed inputs ──

def test_market_context_never_touches_profile_or_holdings_and_is_deterministic():
    import inspect
    from portfolio_agent.tools import market_context as mc
    src = inspect.getsource(mc)
    assert "user_profile_db" not in src and "holdings_db" not in src and "get_portfolio_holdings" not in src

    a = build_market_context("AAPL", [5, 21], loaders=_loaders(), private_terms=frozenset())
    b = build_market_context("AAPL", [5, 21], loaders=_loaders(), private_terms=frozenset())
    assert a.digest == b.digest and a.payload == b.payload
    assert a.market_only is True and a.horizons == (5, 21)
    assert a.evidence_refs["horizons"] == [5, 21]
    assert a.evidence_refs["fundamentals_as_of"] == "2026-08-01"


def test_market_context_rejects_unsupported_horizons():
    with pytest.raises(ValueError, match="unsupported horizon"):
        build_market_context("AAPL", [7], loaders=_loaders())
    with pytest.raises(ValueError, match="at least one horizon"):
        build_market_context("AAPL", [], loaders=_loaders())


def test_market_context_refuses_to_build_over_private_text():
    """A leak from a data source (e.g. a research note that echoed an account nickname)
    is caught at context-build time, not just at the writer."""
    leaky_research = dict(RESEARCH, consensus="Sold from the Fidelity-TaxableBrokerage account last week")
    with pytest.raises(PrivateDataLeak):
        build_market_context("AAPL", [5], loaders=_loaders(research=lambda t: leaky_research),
                             private_terms=frozenset({"Fidelity-TaxableBrokerage"}))


def test_different_profiles_produce_identical_shared_market_context():
    """The canonical builder is profile/holdings-independent: two people with different
    names, employers, preferences and holdings get byte-identical shared inputs (payload,
    evidence_refs and digest) for the same ticker and explicit horizons. private_terms is
    left unset so this also exercises the REAL collect_private_terms() read of the (now
    different) profile/accounts each time -- proving that read affects only the leak
    check, never the payload itself."""
    from portfolio_agent.services import account_service
    from portfolio_agent.services.context import local_context
    from portfolio_agent.tools.user_profile_db import update_user_profile

    ctx_local = local_context("test")
    update_user_profile(display_name="Alice Anderson", employer="Northwind Bank", preferred_horizons=["5d", "21d"])
    acct_a = account_service.create_account(ctx_local, broker="manual", display_name="Alice-401k")
    account_service.add_position(ctx_local, acct_a["account_id"], {"ticker": "TSLA", "shares": 5, "current_price": 200.0})
    a = build_market_context("AAPL", [5, 21], loaders=_loaders())

    update_user_profile(display_name="Bob Brown", employer="Southgate Capital", preferred_horizons=["5d"])
    acct_b = account_service.create_account(ctx_local, broker="fidelity", display_name="Bob-Roth")
    account_service.add_position(ctx_local, acct_b["account_id"], {"ticker": "NVDA", "shares": 3, "current_price": 400.0})
    b = build_market_context("AAPL", [5, 21], loaders=_loaders())

    assert a.digest == b.digest
    assert a.payload == b.payload
    assert a.evidence_refs == b.evidence_refs


def test_private_text_refusal_never_logs_the_private_text():
    """A refused shared write logs that it refused something, never what it refused."""
    import json as _json
    import logging

    _seed_real_private_account("Fidelity-TaxableBrokerage")
    ctx = build_market_context("AAPL", [5], loaders=_loaders(), private_terms=frozenset())
    prov = ForecastProvenance(origin="pipeline.apex", model_used="anthropic:claude")

    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append
    logger = logging.getLogger("portfolio_agent.forecast_writer")
    logger.addHandler(handler)
    try:
        res = write_shared_forecast(ctx, prov, **_row(reasoning="Sold from the Fidelity-TaxableBrokerage account"))
    finally:
        logger.removeHandler(handler)

    assert res["saved"] is False
    logged_text = " ".join(r.getMessage() for r in records)
    logged_text += " " + " ".join(_json.dumps(getattr(r, "extra_fields", {}), default=str) for r in records)
    assert records, "expected the refusal to be logged at all"
    assert "Fidelity-TaxableBrokerage" not in logged_text


# ── Private-term detection ────────────────────────────────────────────────────

def test_private_terms_detected_case_insensitively_and_error_never_echoes_them():
    terms = frozenset({"Fidelity-TaxableBrokerage", "Jane Smith"})
    assert find_private_terms("holds a position in FIDELITY-taxablebrokerage", terms) == ["Fidelity-TaxableBrokerage"]
    try:
        assert_market_only("Jane Smith's account", terms, what="test")
        assert False, "expected PrivateDataLeak"
    except PrivateDataLeak as exc:
        assert "Jane Smith" not in str(exc) and "test" in str(exc)


# ── write_shared_forecast: only a market-only context, from an allow-listed origin ──

def _row(ticker="AAPL", horizon_days=5, **over):
    row = dict(ticker=ticker, prediction="BULLISH", recommendation="BUY", horizon_days=horizon_days,
              reasoning="strong fundamentals and momentum")
    row.update(over)
    return row


def test_write_shared_forecast_requires_a_market_only_context_from_an_allowed_origin():
    ctx = build_market_context("AAPL", [5], loaders=_loaders(), private_terms=frozenset())
    prov = ForecastProvenance(origin="pipeline.apex", model_used="anthropic:claude")
    res = write_shared_forecast(ctx, prov, **_row())
    assert res["saved"] is True
    row = pdb.get_latest_prediction("AAPL", scopes=pdb.SHARED_SCOPES)
    assert row.scope == pdb.SCOPE_SHARED and row.origin == "pipeline.apex" and row.context_digest == ctx.digest

    with pytest.raises(Exception):
        write_shared_forecast(ctx, ForecastProvenance(origin="chat.ticker_prediction", model_used="x"), **_row())
    with pytest.raises(Exception):
        write_shared_forecast("not-a-context", prov, **_row())
    with pytest.raises(Exception):                          # horizon outside the context's own horizons
        write_shared_forecast(ctx, prov, **_row(horizon_days=63))
    with pytest.raises(Exception):                          # ticker mismatch
        write_shared_forecast(ctx, prov, **_row(ticker="MSFT"))


def test_write_shared_forecast_round_trips_full_provenance_onto_the_readable_prediction():
    """Everything the module docstrings promise to 'record' is actually readable back
    through the domain object, not just written blind into columns nothing parses."""
    ctx = build_market_context("AAPL", [5], loaders=_loaders(), private_terms=frozenset())
    prov = ForecastProvenance(origin="pipeline.apex", model_used="anthropic:claude-x",
                              guardrail_version="conviction-guardrails/1.0", origin_run_id="run-42")
    res = write_shared_forecast(ctx, prov, **_row())
    assert res["saved"] is True
    row = pdb.get_latest_prediction("AAPL", scopes=pdb.SHARED_SCOPES)
    assert row.origin == "pipeline.apex" and row.origin_run_id == "run-42"
    assert row.origin_time_utc  # recorded, ISO-ish non-empty
    assert row.horizon_days == 5
    assert row.evidence_refs == ctx.evidence_refs
    assert row.model_used == "anthropic:claude-x"
    assert row.policy_version == prov.policy_version
    assert row.guardrail_version == "conviction-guardrails/1.0"
    assert row.history_lineage == list(ctx.history_lineage)
    assert row.context_digest == ctx.digest


def _seed_real_private_account(nickname: str) -> None:
    """write_shared_forecast checks against collect_private_terms(), which reads the
    REAL profile/accounts tables — so exercising the refusal means seeding one for real."""
    from portfolio_agent.services.account_service import create_account
    from portfolio_agent.services.context import local_context
    create_account(local_context("test"), broker="manual", display_name=nickname)


def test_write_shared_forecast_refuses_text_containing_a_private_term():
    _seed_real_private_account("Fidelity-TaxableBrokerage")
    ctx = build_market_context("AAPL", [5], loaders=_loaders(), private_terms=frozenset())
    prov = ForecastProvenance(origin="pipeline.apex", model_used="anthropic:claude")
    res = write_shared_forecast(ctx, prov, **_row(reasoning="Sold from the Fidelity-TaxableBrokerage account"))
    assert res["saved"] is False and "private_text_rejected" in res["error"]
    assert pdb.get_latest_prediction("AAPL", scopes=pdb.SHARED_SCOPES) is None    # nothing was written


def test_private_sentinel_text_cannot_enter_the_shared_writer_even_via_panel_summary():
    from portfolio_agent.tools.user_profile_db import update_user_profile
    update_user_profile(display_name="Jane Smith")
    ctx = build_market_context("AAPL", [5], loaders=_loaders(), private_terms=frozenset())
    prov = ForecastProvenance(origin="pipeline.apex", model_used="anthropic:claude")
    res = write_shared_forecast(ctx, prov, **_row(panel_summary={"note": "held in Jane Smith's Roth IRA"}))
    assert res["saved"] is False
    assert pdb.get_prediction_history("AAPL", scopes=pdb.SHARED_SCOPES) == []


# ── write_private_forecast: always private, tied to its owner, never shared ──

def test_write_private_forecast_is_always_private_and_owned():
    prov = ForecastProvenance(origin="chat.ticker_prediction", model_used="anthropic:claude")
    res = write_private_forecast("alice", prov, **_row(reasoning="Jane Smith asked about this in chat"))
    assert res["saved"] is True
    assert pdb.get_latest_prediction("AAPL", scopes=pdb.SHARED_SCOPES) is None          # never shared
    row = pdb.get_latest_prediction("AAPL", scopes=(pdb.SCOPE_PRIVATE,))
    assert row.scope == pdb.SCOPE_PRIVATE and row.owner_scope == "user:alice" and row.origin == "chat.ticker_prediction"


# ── Legacy rows: unclassified, never promoted, excluded from shared reads ────

def _seed_legacy_row(ticker="AAPL", horizon_days=5, **over):
    """Simulate a pre-scope row the old insert_prediction() would have written."""
    row = dict(ticker=ticker, prediction="BULLISH", recommendation="BUY", horizon_days=horizon_days,
              evaluation_status="evaluated", actual_direction="UP", predicted_direction="UP")
    row.update(over)
    with pdb._db() as c:
        cols = list(row)
        c.execute(f"INSERT INTO predictions ({', '.join(cols)}, created_at, as_of_date) VALUES "
                  f"({', '.join('?' for _ in cols)}, datetime('now'), date('now'))", list(row.values()))
        c.commit()


def test_legacy_rows_are_classified_unclassified_never_promoted_to_shared():
    _seed_legacy_row("AAPL")
    row = pdb.get_latest_prediction("AAPL", scopes=pdb.DISPLAY_SCOPES)
    assert row.scope == pdb.SCOPE_LEGACY
    assert pdb.get_latest_prediction("AAPL", scopes=pdb.SHARED_SCOPES) is None
    assert pdb.get_prediction_history("AAPL", scopes=pdb.SHARED_SCOPES) == []           # prompts never see it


def test_unclassified_history_is_excluded_from_shared_but_kept_for_legacy_review():
    _seed_legacy_row("AAPL", horizon_days=5)
    ctx = build_market_context("AAPL", [21], loaders=_loaders(), private_terms=frozenset())
    write_shared_forecast(ctx, ForecastProvenance(origin="pipeline.apex", model_used="x"), **_row(horizon_days=21))
    assert len(pdb.get_prediction_history("AAPL", limit=10, scopes=pdb.SHARED_SCOPES)) == 1     # legacy excluded
    assert len(pdb.get_prediction_history("AAPL", limit=10, scopes=pdb.LEGACY_REVIEW_SCOPES)) == 2  # both, for historical eval
    assert len(pdb.get_prediction_history("AAPL", limit=10, scopes=pdb.DISPLAY_SCOPES)) == 2


def _mark_evaluated(ticker: str, **fields) -> None:
    fields = {"evaluation_status": "evaluated", "actual_direction": "UP", "predicted_direction": "UP",
             "evaluated_at": "2026-09-20T00:00:00", "conviction_score": 8.0, "weight_regime": "MIXED", **fields}
    with pdb._db() as c:
        c.execute(f"UPDATE predictions SET {', '.join(f'{k} = ?' for k in fields)} WHERE ticker = ?",
                  [*fields.values(), ticker])
        c.commit()


def test_failure_pattern_mining_reads_only_shared_rows(monkeypatch):
    from portfolio_agent.tools import validation_engine as ve
    _seed_legacy_row("LEGACY1")
    _mark_evaluated("LEGACY1")
    ctx = build_market_context("SHARED1", [5], loaders=_loaders(), private_terms=frozenset())
    write_shared_forecast(ctx, ForecastProvenance(origin="pipeline.apex", model_used="x"), **_row("SHARED1"))
    _mark_evaluated("SHARED1")
    write_private_forecast("alice", ForecastProvenance(origin="chat.ticker_prediction", model_used="x"), **_row("PRIVATE1"))
    _mark_evaluated("PRIVATE1")
    with monkeypatch.context() as m:
        m.setattr(ve, "_mine_segment_patterns", lambda rows: [])
        result = ve.weekly_pattern_analysis()
    # Only SHARED1 counted — LEGACY1 (unclassified) and PRIVATE1 were excluded from the scan.
    assert result.get("skipped") is True and "1 evaluated predictions" in result["reason"]


# ── Reasoning-tools wrapper delegates to the canonical builder ───────────────

def test_reasoning_tools_wrapper_delegates_to_canonical_builder(monkeypatch):
    import json
    from portfolio_agent.tools import reasoning_tools as rt
    calls = []

    def _fake_build(ticker, horizons):
        calls.append((ticker, tuple(horizons)))
        return MarketContext(ticker=ticker, horizons=tuple(horizons), payload={"ticker": ticker},
                             evidence_refs={}, history_lineage=(), digest="x", built_at="t")

    monkeypatch.setattr("portfolio_agent.tools.market_context.build_market_context", _fake_build)
    out = json.loads(rt.get_full_analysis_context("aapl", horizons=[21]))
    assert calls == [("aapl", (21,))] and out["ticker"] == "aapl"
    rt.get_full_analysis_context("aapl")
    assert calls[-1] == ("aapl", (5, 21, 63))     # default horizons when none given
