"""
Step 1 — champion model selection (no random choice), ordered fallback,
optional challenger, prediction_role storage/migration and the role-filtered
validation views. No live LLM calls: litellm.acompletion / run_apex /
run_challenger are stubbed throughout.
"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import date

import pandas as pd
import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.tools import apex_dual_run as adr
from portfolio_agent.tools import engine_settings_db as esdb
from portfolio_agent.tools import market_context as market_context_mod
from portfolio_agent.tools import prediction_db as pdb
from portfolio_agent.tools import reasoning_tools as rt_mod
from portfolio_agent.tools.apex_dual_run import ModelResult, SinglePrediction


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")


_MODEL_ID = {label: mid for mid, _, label in adr.APEX_MODELS}
_VALID_JSON = '{"recommendation": "BUY", "horizons": [{"horizon_days": 5}]}'


def _fake_resp(text: str):
    class _Msg:
        content = text
    class _Choice:
        message = _Msg()
    class _Resp:
        choices = [_Choice()]
    return _Resp()


def _roles_in_db() -> list[tuple[str, int, str]]:
    with sqlite3.connect(str(db_module.DB_PATH)) as c:
        return c.execute("SELECT prediction_role, horizon_days, recommendation FROM predictions ORDER BY id").fetchall()


# ── Deterministic selection ───────────────────────────────────────────────────

def test_default_plan_is_champion_then_ordered_fallbacks_with_no_randomness():
    plan = adr.load_model_plan()
    assert plan.champion[2] == "Claude-Sonnet"
    assert [m[2] for m in plan.fallbacks] == ["GPT-5", "GPT-4o", "o3"]
    assert plan.challenger is None and plan.challenger_pct == 0
    assert not hasattr(adr, "random")


def test_same_settings_always_pick_the_same_model(monkeypatch):
    calls = []

    async def _fake(model, **kw):
        calls.append(model)
        return _fake_resp(_VALID_JSON)

    monkeypatch.setattr(adr.litellm, "acompletion", _fake)
    for _ in range(20):
        r = asyncio.run(adr.run_apex("AAPL", "prompt"))
        assert r.role == pdb.ROLE_CHAMPION and r.model_sources == ["Claude-Sonnet"] and r.used_fallback is False
    assert set(calls) == {_MODEL_ID["Claude-Sonnet"]}


def test_engine_settings_set_champion_and_fallback_order(monkeypatch):
    esdb.update_engine_settings(updated_by="test", apex_champion_model="GPT-4o",
                                apex_fallback_models=["o3", "Claude-Sonnet"])
    calls = []

    async def _fake(model, **kw):
        calls.append(model)
        raise RuntimeError("429 rate limit")             # failover-class

    monkeypatch.setattr(adr.litellm, "acompletion", _fake)
    assert asyncio.run(adr.run_apex("AAPL", "prompt")) is None
    assert calls == [_MODEL_ID["GPT-4o"], _MODEL_ID["o3"], _MODEL_ID["Claude-Sonnet"]]


def test_fallback_triggers_on_timeout_and_is_labelled(monkeypatch):
    monkeypatch.setattr(adr, "APEX_TIMEOUT", 0.05)
    calls = []

    async def _fake(model, **kw):
        calls.append(model)
        if model == _MODEL_ID["Claude-Sonnet"]:
            await asyncio.sleep(1)                       # champion hangs past the timeout
        return _fake_resp(_VALID_JSON)

    monkeypatch.setattr(adr.litellm, "acompletion", _fake)
    r = asyncio.run(adr.run_apex("AAPL", "prompt"))
    assert calls == [_MODEL_ID["Claude-Sonnet"], _MODEL_ID["GPT-5"]]
    assert r.role == pdb.ROLE_FALLBACK and r.used_fallback is True
    assert r.fallback_reason == "timeout" and r.model_sources == ["GPT-5"] and r.fallback_model == "GPT-5"


def test_invalid_json_moves_to_next_model(monkeypatch):
    async def _fake(model, **kw):
        return _fake_resp("not json" if model == _MODEL_ID["Claude-Sonnet"] else _VALID_JSON)

    monkeypatch.setattr(adr.litellm, "acompletion", _fake)
    r = asyncio.run(adr.run_apex("AAPL", "prompt"))
    assert r.role == pdb.ROLE_FALLBACK and r.fallback_reason == "invalid_json"


def test_non_failover_error_still_propagates(monkeypatch):
    calls = []

    async def _fake(model, **kw):
        calls.append(model)
        raise ValueError("a bug, not a provider outage")

    monkeypatch.setattr(adr.litellm, "acompletion", _fake)
    with pytest.raises(ValueError):
        asyncio.run(adr.run_apex("AAPL", "prompt"))
    assert len(calls) == 1


def test_bad_labels_degrade_to_defaults_instead_of_failing():
    plan = adr.build_model_plan(champion="Nope", fallbacks=["GPT-4o", "Bogus", "GPT-4o", "Claude-Sonnet"],
                                challenger="Claude-Sonnet", challenger_pct=50)
    assert plan.champion[2] == "Claude-Sonnet"
    assert [m[2] for m in plan.fallbacks] == ["GPT-4o"]       # unknown dropped, dupes and champion removed
    assert plan.challenger is None and plan.challenger_pct == 0   # challenger == champion → off


def test_engine_settings_updates_persist():
    before = esdb.get_engine_settings()["version"]
    esdb.update_engine_settings(updated_by="test", apex_champion_model="o3")
    after = esdb.get_engine_settings()
    assert after["apex_champion_model"] == "o3" and after["version"] == before + 1 and after["updated_by"] == "test"


def test_engine_settings_validate_model_labels_and_pct():
    with pytest.raises(ValueError):
        esdb.update_engine_settings(updated_by="test", apex_champion_model="Nope")
    with pytest.raises(ValueError):
        esdb.update_engine_settings(updated_by="test", apex_fallback_models=["GPT-4o", "Nope"])
    with pytest.raises(ValueError):
        esdb.update_engine_settings(updated_by="test", apex_challenger_sample_pct=150)
    s = esdb.update_engine_settings(updated_by="test", apex_challenger_model="GPT-5", apex_challenger_sample_pct=10)
    assert s["apex_challenger_model"] == "GPT-5" and s["apex_challenger_sample_pct"] == 10
    s = esdb.update_engine_settings(updated_by="test", apex_challenger_model=None)
    assert s["apex_challenger_model"] is None


# ── Challenger ────────────────────────────────────────────────────────────────

def test_challenger_is_off_by_default():
    s = esdb.get_engine_settings()
    assert s["apex_challenger_model"] is None and s["apex_challenger_sample_pct"] == 0
    assert adr.load_model_plan().challenger is None
    assert adr.challenger_selected("AAPL", "2026-10-09", 0) is False


def test_challenger_sampling_respects_pct_and_is_deterministic():
    tickers = [f"T{i}" for i in range(4000)]
    day = "2026-10-09"
    picked = {}
    for pct in (10, 30, 75):
        sel = [t for t in tickers if adr.challenger_selected(t, day, pct)]
        assert abs(len(sel) / len(tickers) - pct / 100) < 0.03
        assert sel == [t for t in tickers if adr.challenger_selected(t, day, pct)]   # same answer every time
        picked[pct] = set(sel)
    assert picked[10] <= picked[30] <= picked[75]           # raising the % only adds tickers
    assert not any(adr.challenger_selected(t, day, 0) for t in tickers)
    assert all(adr.challenger_selected(t, day, 100) for t in tickers)


# ── Pipeline: roles on stored rows ────────────────────────────────────────────

NEWS = [{"headline": "steady", "sentiment": "NEUTRAL", "sentiment_score": 0.0,
        "published_at": "2026-09-20", "date": "2026-09-20"}]
RESEARCH = {"price_target_avg": 120.0, "current_price": 100.0, "num_analysts": 10, "as_of_date": "2026-09-15"}


def _fake_loaders():
    return {"fundamentals": lambda t: {"as_of_date": "2026-08-01"}, "research": lambda t: RESEARCH,
            "news": lambda t: NEWS, "valuation": lambda t: {"as_of_date": "2026-09-01"},
            "macro": lambda: {"vix": 20.0}, "history": lambda t: [], "failure_patterns": lambda: []}


def _result(rec: str, label: str, role: str, horizons=(5, 21)) -> ModelResult:
    data = {"prediction": "BULLISH", "recommendation": rec, "confidence": 7, "composite_score": 6.5,
            "horizons": [{"horizon_days": h, "predicted_direction": "UP", "predicted_return_low": 1.0,
                          "predicted_return_high": 3.0, "conviction_score": 6, "reasoning_text": "x"}
                         for h in horizons]}
    return ModelResult(merged=data, individuals=[SinglePrediction("m", "x", label, data, 10, True)],
                       model_sources=[label], used_fallback=role == pdb.ROLE_FALLBACK, role=role)


def _patch_pipeline(monkeypatch, *, champion: ModelResult, challenger=None):
    # The pipeline builds contexts in parallel threads and each calls effective_settings(), whose connection setup
    # (create/migrate/seed) races on a brand-new test DB — and a failed setup leaves a connection holding the write
    # lock. Production's table already exists; here, resolve the settings once, serially, and serve them from memory.
    from portfolio_agent.tools import engine_settings_db as _esdb, weight_engine as _we
    _settings = _esdb.get_engine_settings()
    pdb.get_today_horizons("WARMUP", "2000-01-01")       # prediction_db's create/migrate setup runs on EVERY connection (an UPDATE
    monkeypatch.setattr(pdb, "_migrate", lambda conn: None)   # takes the write lock): run it once, serially, then not in the threads
    monkeypatch.setattr(pdb, "_create_schema", lambda conn: None)
    monkeypatch.setattr(_we, "effective_settings", lambda: _settings)
    monkeypatch.setattr(market_context_mod, "default_loaders", _fake_loaders)
    monkeypatch.setattr(rt_mod, "get_macro_snapshot", lambda: '{"vix": 20.0}')
    monkeypatch.setattr(pdb, "is_trading_day", lambda d: True)
    from portfolio_agent.tools import yfinance_tools as yft
    monkeypatch.setattr(yft, "get_close", lambda t, d: 100.0)
    monkeypatch.setattr(yft, "get_trailing_return", lambda t, d: 0.01)

    async def _fake_run_apex(ticker, prompt):
        return champion

    challenger_calls: list[str] = []

    async def _fake_run_challenger(ticker, prompt, plan):
        challenger_calls.append(ticker)
        if isinstance(challenger, Exception):
            raise challenger
        return challenger

    monkeypatch.setattr(adr, "run_apex", _fake_run_apex)
    monkeypatch.setattr(adr, "run_challenger", _fake_run_challenger)
    return challenger_calls


def _run(tickers, horizons):
    import portfolio_agent.pipeline.daily.apex as apex_mod
    asyncio.run(apex_mod._run_daily_apex(tickers, scheduled_horizons_override=horizons))


def test_pipeline_writes_only_champion_rows_when_challenger_is_off(monkeypatch):
    calls = _patch_pipeline(monkeypatch, champion=_result("BUY", "Claude-Sonnet", pdb.ROLE_CHAMPION),
                            challenger=_result("SELL", "GPT-5", pdb.ROLE_CHALLENGER))
    _run(["AAPL"], [5, 21])
    assert [r[0] for r in _roles_in_db()] == [pdb.ROLE_CHAMPION, pdb.ROLE_CHAMPION]
    assert calls == []


def test_fallback_rows_are_stored_as_fallback(monkeypatch):
    _patch_pipeline(monkeypatch, champion=_result("BUY", "GPT-5", pdb.ROLE_FALLBACK))
    _run(["AAPL"], [5])
    with sqlite3.connect(str(db_module.DB_PATH)) as c:
        role, used_fb, model = c.execute("SELECT prediction_role, used_fallback, model_name FROM predictions").fetchone()
    assert (role, used_fb, model) == (pdb.ROLE_FALLBACK, 1, "GPT-5")


def test_challenger_rows_are_stored_but_never_surface_as_the_prediction(monkeypatch):
    esdb.update_engine_settings(updated_by="test", apex_challenger_model="GPT-5", apex_challenger_sample_pct=100)
    calls = _patch_pipeline(monkeypatch, champion=_result("BUY", "Claude-Sonnet", pdb.ROLE_CHAMPION),
                            challenger=_result("SELL", "GPT-5", pdb.ROLE_CHALLENGER))
    _run(["AAPL"], [5, 21])
    assert calls == ["AAPL"]
    assert sorted(_roles_in_db()) == sorted([
        (pdb.ROLE_CHAMPION, 5, "BUY"), (pdb.ROLE_CHAMPION, 21, "BUY"),
        (pdb.ROLE_CHALLENGER, 5, "SELL"), (pdb.ROLE_CHALLENGER, 21, "SELL"),
    ])
    # Written later, yet never the "latest" anything a user or prompt sees.
    assert pdb.get_latest_prediction("AAPL").recommendation == "BUY"
    assert pdb.get_latest_prediction("AAPL", horizon_days=5).recommendation == "BUY"
    assert pdb.get_all_latest_predictions()["AAPL"].recommendation == "BUY"
    assert {p.recommendation for p in pdb.get_prediction_history("AAPL")} == {"BUY"}
    assert pdb.get_today_horizons("AAPL", date.today().isoformat(), scopes=pdb.SHARED_SCOPES) == {5, 21}

    # A same-day re-run adds nothing: horizons are done, so no champion and no challenger.
    _run(["AAPL"], [5, 21])
    assert len(_roles_in_db()) == 4 and calls == ["AAPL"]


def test_challenger_failure_never_fails_the_ticker(monkeypatch):
    esdb.update_engine_settings(updated_by="test", apex_challenger_model="GPT-5", apex_challenger_sample_pct=100)
    _patch_pipeline(monkeypatch, champion=_result("BUY", "Claude-Sonnet", pdb.ROLE_CHAMPION),
                    challenger=RuntimeError("challenger blew up"))
    _run(["AAPL"], [5])
    assert [r[0] for r in _roles_in_db()] == [pdb.ROLE_CHAMPION]


# ── Storage: migration, backfill, validation of the role ──────────────────────

def test_role_column_migration_backfills_old_rows_as_legacy_random_once():
    with sqlite3.connect(str(db_module.DB_PATH)) as c:     # a predictions table from before prediction_role
        pdb._create_schema(c)                                # base CREATE only — no migrations yet
        assert "prediction_role" not in {r[1] for r in c.execute("PRAGMA table_info(predictions)")}
        c.executemany("INSERT INTO predictions (ticker, created_at, prediction, recommendation, model_name) "
                      "VALUES (?, '2026-09-01T10:00:00', 'BULLISH', 'BUY', ?)", [("AAPL", "GPT-4o"), ("MSFT", "o3")])

    with pdb._db():
        pass
    with sqlite3.connect(str(db_module.DB_PATH)) as c:
        assert [r[0] for r in c.execute("SELECT prediction_role FROM predictions")] == [pdb.ROLE_LEGACY_RANDOM] * 2

    pdb.insert_prediction(ticker="NVDA", prediction="BULLISH", recommendation="BUY", horizon_days=5)
    with pdb._db():                                          # migration runs again: idempotent
        pass
    with sqlite3.connect(str(db_module.DB_PATH)) as c:
        roles = dict(c.execute("SELECT ticker, prediction_role FROM predictions").fetchall())
        n_cols = sum(1 for r in c.execute("PRAGMA table_info(predictions)") if r[1] == "prediction_role")
    assert roles == {"AAPL": pdb.ROLE_LEGACY_RANDOM, "MSFT": pdb.ROLE_LEGACY_RANDOM, "NVDA": pdb.ROLE_CHAMPION}
    assert n_cols == 1


def test_insert_rejects_an_unknown_role():
    with pytest.raises(ValueError):
        pdb.insert_prediction(ticker="AAPL", prediction="BULLISH", recommendation="BUY", horizon_days=5,
                              prediction_role="random")


def test_private_forecasts_are_interactive_and_shared_ones_cannot_claim_it():
    from portfolio_agent.tools.forecast_writer import ForecastProvenance, ForecastScopeError, write_private_forecast, \
        write_shared_forecast
    from portfolio_agent.tools.market_context import build_market_context

    write_private_forecast("someone", ForecastProvenance(origin="chat.ticker_prediction", model_used="x"),
                           ticker="AAPL", prediction="BULLISH", recommendation="BUY", horizon_days=5)
    assert _roles_in_db()[0][0] == pdb.ROLE_INTERACTIVE

    ctx = build_market_context("AAPL", [5], loaders=_fake_loaders())
    with pytest.raises(ForecastScopeError):
        write_shared_forecast(ctx, ForecastProvenance(origin="pipeline.apex", model_used="x"), ticker="AAPL",
                              prediction="BULLISH", recommendation="BUY", horizon_days=5,
                              prediction_role=pdb.ROLE_INTERACTIVE)


# ── Validation: default role filter, legacy on request, champion vs challenger ─

def _evaluated(ticker: str, role: str, *, as_of: str = "2026-09-01", horizon: int = 5,
               pred_dir: str = "UP", actual_return: float = 0.03, model: str = "Claude-Sonnet") -> None:
    pdb.insert_prediction(ticker=ticker, prediction="BULLISH", recommendation="BUY", horizon_days=horizon,
                          predicted_direction=pred_dir, model_name=model, scope=pdb.SCOPE_SHARED,
                          context_digest="test", prediction_role=role)
    with sqlite3.connect(str(db_module.DB_PATH)) as c:
        c.execute("""UPDATE predictions SET as_of_date = ?, evaluation_status = 'evaluated', evaluated_at = ?,
                     actual_return = ?, actual_direction = ?, outcome = 'directionally_correct'
                     WHERE id = (SELECT MAX(id) FROM predictions)""",
                  [as_of, as_of, actual_return, "UP" if actual_return > 0.01 else "DOWN"])


def test_validation_views_default_to_champion_and_fallback(monkeypatch):
    from portfolio_agent.tools import validation_engine as ve
    import web.data.validation as wdv
    monkeypatch.setattr(wdv, "_DB", db_module.DB_PATH)
    monkeypatch.setattr(ve, "_today_cst", lambda: date(2026, 9, 10))

    for t, role in [("AAPL", pdb.ROLE_CHAMPION), ("MSFT", pdb.ROLE_FALLBACK),
                    ("NVDA", pdb.ROLE_LEGACY_RANDOM), ("AMZN", pdb.ROLE_CHALLENGER)]:
        _evaluated(t, role)

    default = {p["ticker"] for p in ve.get_recent_evaluated_predictions()}
    assert default == {"AAPL", "MSFT"}
    with_legacy = {p["ticker"] for p in ve.get_recent_evaluated_predictions(roles=pdb.VALIDATION_ROLES_WITH_LEGACY)}
    assert with_legacy == {"AAPL", "MSFT", "NVDA"}
    assert sum(r["n"] for r in ve.get_volume_by_horizon(lookback_days=30)) == 2
    assert ve.get_calibration_data(lookback_days=30)["summary"].get("n", 0) in (0, 2)   # no distribution → 0, never 4

    df = wdv._load_evaluated_predictions_df(None, None, "All")
    assert set(df["ticker"]) == {"AAPL", "MSFT"} and set(df["prediction_role"]) == set(pdb.VALIDATION_ROLES)
    df_all = wdv._load_evaluated_predictions_df(None, None, "All", roles=pdb.REVIEW_ROLES)
    assert set(df_all["ticker"]) == {"AAPL", "MSFT", "NVDA", "AMZN"}


def test_role_breakdown_and_champion_vs_challenger_use_the_same_tickers_and_dates():
    from portfolio_agent.tools.validation_roles import champion_vs_challenger, role_breakdown

    rows = [
        # (ticker, as_of, role, model, pred, actual)
        ("AAPL", "2026-09-01", "champion",      "Claude-Sonnet", "UP",   0.04),
        ("AAPL", "2026-09-01", "challenger",    "GPT-5",         "DOWN", 0.04),
        ("MSFT", "2026-09-01", "fallback",      "GPT-5",         "UP",  -0.03),
        ("MSFT", "2026-09-01", "challenger",    "GPT-5",         "DOWN", -0.03),
        ("NVDA", "2026-09-01", "champion",      "Claude-Sonnet", "UP",   0.05),   # no challenger → not paired
        ("TSLA", "2026-09-02", "challenger",    "GPT-5",         "UP",   0.02),   # no production row → not paired
        ("AAPL", "2026-08-01", "legacy_random", "GPT-4o",        "UP",   0.02),
    ]
    df = pd.DataFrame([{"id": i, "ticker": t, "as_of_date": d, "horizon_days": 5, "prediction_role": r,
                        "model_name": m, "predicted_direction": p, "actual_return": a}
                       for i, (t, d, r, m, p, a) in enumerate(rows)])

    out = champion_vs_challenger(df)
    assert list(out) == [5]
    c = out[5]
    assert c["n_pairs"] == 2 and c["n_dates"] == 1
    assert c["production_models"] == ["Claude-Sonnet", "GPT-5"] and c["challenger_models"] == ["GPT-5"]
    assert c["production"]["n"] == 2 and c["challenger"]["n"] == 2
    assert c["production"]["accuracy"] == pytest.approx(0.5)     # AAPL right, MSFT wrong
    assert c["challenger"]["accuracy"] == pytest.approx(0.5)     # AAPL wrong, MSFT right
    assert c["head_to_head"] == {"n": 2, "both_right": 0, "production_only": 1, "challenger_only": 1, "both_wrong": 0}

    by_role = {r["role"]: r for r in role_breakdown(df)}
    assert {k: v["n"] for k, v in by_role.items()} == {"champion": 2, "fallback": 1, "challenger": 3, "legacy_random": 1}
    assert by_role["legacy_random"]["label"] == "Legacy (random model)"


# ── Pages: user Validation hides legacy by default; QA Roles tab pairs champion vs challenger ──

def _user_page():
    import runpy
    import sys
    from pathlib import Path
    root = Path.cwd()
    sys.path.insert(0, str(root))
    import web.auth as auth
    import web.styles as styles
    from portfolio_agent.services.context import local_context
    styles.top_nav = lambda *a, **k: None
    auth.require_login = lambda: None
    auth.current_context = lambda source: local_context(source)
    runpy.run_path(str(root / "web/pages/6_🎯_Validation.py"), run_name="__main__")


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


def _seed_page_rows(role: str, model: str, *, flip: bool = False, n_dates: int = 3, per_date: int = 12) -> None:
    from datetime import timedelta
    with pdb._db() as conn:
        for d in range(n_dates):
            day = (date.today() - timedelta(days=10 + d)).isoformat()
            for i in range(per_date):
                up = (i >= per_date // 2) != flip
                conn.execute(
                    """INSERT INTO predictions (ticker, created_at, prediction, recommendation, horizon_days, as_of_date,
                           evaluation_date, evaluation_status, predicted_direction, actual_return, benchmark_return,
                           excess_return, composite_score, in_predicted_range, outcome, scope, model_name,
                           brier_score, log_loss, p_strong_down, p_moderate_down, p_flat, p_moderate_up, p_strong_up,
                           prediction_role)
                       VALUES (?, datetime('now'), 'x', ?, 5, ?, ?, 'evaluated', ?, ?, 0.0, ?, ?, ?, 'strong_correct',
                           'shared_market', ?, 0.2, 0.6, 5, 10, 25, 40, 20, ?)""",
                    [f"T{d}_{i}", "BUY" if up else "HOLD", day, day, "UP" if up else "FLAT",
                     0.002 * i, 0.002 * i, float(i), i % 2, model, role])
        conn.commit()


@pytest.fixture
def _page_db(monkeypatch):
    import web.data.validation as wdv
    monkeypatch.setattr(wdv, "_DB", db_module.DB_PATH)
    wdv._price_cache.clear()
    with pdb._db():
        pass


def test_user_validation_page_hides_legacy_rows_by_default_and_says_so(_page_db):
    from streamlit.testing.v1 import AppTest
    _seed_page_rows(pdb.ROLE_LEGACY_RANDOM, "GPT-4o")

    at = AppTest.from_function(_user_page, default_timeout=120)
    at.session_state["val_segment"] = "All"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("No champion-model predictions have been scored yet" in i.value for i in at.info)
    assert any("36 legacy predictions" in c.value and "hidden" in c.value for c in at.caption)

    at.session_state["val_include_legacy"] = True
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert "Does the rating pick winners?" in " ".join(m.value for m in at.markdown)


def test_validation_qa_roles_tab_compares_champion_and_challenger(_page_db):
    from streamlit.testing.v1 import AppTest
    _seed_page_rows(pdb.ROLE_CHAMPION, "Claude-Sonnet")
    _seed_page_rows(pdb.ROLE_CHALLENGER, "GPT-5", flip=True)

    at = AppTest.from_function(_qa_page, default_timeout=120)
    at.session_state["valqa_segment"] = "All"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert "Roles" in at.tabs[12].label
    assert "Champion vs challenger — same tickers, same dates" in " ".join(m.value for m in at.markdown)
    assert not any("No challenger rows have matured yet" in i.value for i in at.info)
