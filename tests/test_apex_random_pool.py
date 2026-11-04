"""DeepSeek as an APEX prediction model, and the random pool: each ticker draws ONE model (OpenAI or DeepSeek) per day. The draw is a hash of
(ticker, date): random across tickers, never different for the same ticker and day. No live LLM calls: litellm.acompletion is stubbed."""
from __future__ import annotations

import asyncio
import collections
from datetime import date

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent._models import _is_failover_error
from portfolio_agent.tools import apex_dual_run as adr
from portfolio_agent.tools import engine_settings_db as esdb
from portfolio_agent.tools import prediction_db as pdb

_VALID = '{"recommendation": "BUY", "horizons": [{"horizon_days": 5}]}'
TICKERS = [f"T{i:03d}" for i in range(400)]
DAY = "2026-10-26"


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    adr._OUT_OF_CREDIT.clear()
    yield
    adr._OUT_OF_CREDIT.clear()


def _resp(text):
    class _M:
        content = text
    class _C:
        message = _M()
    class _R:
        choices = [_C()]
    return _R()


def pool_plan(pool=("GPT-5", "DeepSeek-Chat"), fallbacks=("o3",), champion="GPT-5", **kw):
    return adr.build_model_plan(champion=champion, fallbacks=list(fallbacks), selection_mode="random_pool", random_pool=list(pool), **kw)


# ── the model ─────────────────────────────────────────────────────────────────

def test_deepseek_is_a_registered_apex_model_with_its_own_provider():
    assert "DeepSeek-Chat" in adr.known_model_labels()
    assert {label: (mid, prov) for mid, prov, label in adr.APEX_MODELS}["DeepSeek-Chat"] == ("deepseek/deepseek-chat", "deepseek")


def test_deepseek_running_out_of_credit_counts_as_a_failover_error_so_the_next_model_is_tried():
    msg = 'litellm.BadRequestError: DeepseekException - {"error":{"message":"Insufficient Balance","type":"insufficient_balance_error"}}'
    assert _is_failover_error(Exception(msg)) is True
    assert _is_failover_error(Exception("402 Client Error: Payment Required for url: https://api.deepseek.com/chat/completions")) is True


# ── the plan ──────────────────────────────────────────────────────────────────

def test_default_mode_is_still_one_fixed_champion():
    plan = adr.load_model_plan()
    assert plan.selection_mode == "champion" and plan.pool == ()
    assert adr.plan_for_ticker(plan, "AAPL", DAY) is plan                        # nothing to resolve


def test_a_pool_plan_describes_itself_and_keeps_the_pool_in_order():
    plan = pool_plan()
    assert plan.selection_mode == "random_pool" and [m[2] for m in plan.pool] == ["GPT-5", "DeepSeek-Chat"]
    assert "random pool [GPT-5 | DeepSeek-Chat]" in plan.describe()


def test_a_pool_needs_two_usable_models_otherwise_the_champion_plan_is_used():
    assert pool_plan(pool=("GPT-5",)).selection_mode == "champion"
    assert pool_plan(pool=("GPT-5", "Not-A-Model")).selection_mode == "champion"     # the unknown one is dropped, leaving one
    assert pool_plan(pool=("GPT-5", "GPT-5")).selection_mode == "champion"            # duplicates count once
    bad = adr.build_model_plan(selection_mode="nonsense")
    assert bad.selection_mode == "champion"


# ── the draw ──────────────────────────────────────────────────────────────────

def test_the_draw_is_the_same_for_the_same_ticker_and_day_and_spread_across_tickers():
    labels = ["GPT-5", "DeepSeek-Chat"]
    assert all(adr.pool_pick(t, DAY, labels) == adr.pool_pick(t, DAY, labels) for t in TICKERS)
    counts = collections.Counter(adr.pool_pick(t, DAY, labels) for t in TICKERS)
    assert set(counts) == set(labels) and all(150 <= n <= 250 for n in counts.values()), counts      # about half each
    assert adr.pool_pick("aapl", DAY, labels) == adr.pool_pick("AAPL", DAY, labels)                    # case does not matter
    assert len({adr.pool_pick("AAPL", f"2026-10-{d:02d}", labels) for d in range(1, 29)}) == 2         # a ticker's model varies from day to day


def test_a_ticker_gets_a_champion_plan_with_the_other_pool_member_first_then_the_configured_fallbacks():
    plan = pool_plan(fallbacks=("o3", "GPT-4o"))
    seen = set()
    for t in TICKERS[:60]:
        p = adr.plan_for_ticker(plan, t, DAY)
        assert p.selection_mode == "champion" and p.pool == () and p.picked_from == "GPT-5 | DeepSeek-Chat"
        champ = p.champion[2]
        seen.add(champ)
        other = "DeepSeek-Chat" if champ == "GPT-5" else "GPT-5"
        assert [m[2] for m in p.fallbacks] == [other, "o3", "GPT-4o"]
        assert adr.plan_for_ticker(p, t, DAY) is p                                # resolving a resolved plan changes nothing
    assert seen == {"GPT-5", "DeepSeek-Chat"}


def test_the_configured_champion_is_ignored_under_a_pool_and_a_fallback_equal_to_the_pick_is_not_repeated():
    plan = pool_plan(champion="o3", fallbacks=("GPT-5", "o3"))
    for t in TICKERS[:40]:
        p = adr.plan_for_ticker(plan, t, DAY)
        labels = [p.champion[2]] + [m[2] for m in p.fallbacks]
        assert p.champion[2] in ("GPT-5", "DeepSeek-Chat") and len(labels) == len(set(labels))


# ── running it ────────────────────────────────────────────────────────────────

def test_each_ticker_is_predicted_by_exactly_one_pool_model_and_both_models_get_used(monkeypatch):
    calls = []

    async def _fake(model, **kw):
        calls.append(model)
        return _resp(_VALID)

    monkeypatch.setattr(adr.litellm, "acompletion", _fake)
    plan = pool_plan()
    used = {}
    for t in TICKERS[:40]:
        before = len(calls)
        r = asyncio.run(adr.run_apex(t, "prompt", plan=plan, as_of_date=DAY))
        assert len(calls) - before == 1                                            # one call: no second opinion
        assert r.role == pdb.ROLE_CHAMPION and r.used_fallback is False and r.model_sources[0] in ("GPT-5", "DeepSeek-Chat")
        used[t] = r.model_sources[0]
    assert set(used.values()) == {"GPT-5", "DeepSeek-Chat"}
    assert set(calls) == {"openai/gpt-5", "deepseek/deepseek-chat"}
    again = asyncio.run(adr.run_apex(TICKERS[0], "prompt", plan=plan, as_of_date=DAY))
    assert again.model_sources[0] == used[TICKERS[0]]                              # same ticker, same day: same model


def test_when_deepseek_is_out_of_credit_the_ticker_falls_back_to_openai_and_deepseek_is_not_drawn_again(monkeypatch):
    calls = []

    async def _fake(model, **kw):
        calls.append(model)
        if "deepseek" in model:
            raise Exception('litellm.BadRequestError: DeepseekException - {"error":{"message":"Insufficient Balance"}}')
        return _resp(_VALID)

    monkeypatch.setattr(adr.litellm, "acompletion", _fake)
    plan = pool_plan()
    ds_ticker = next(t for t in TICKERS if adr.pool_pick(t, DAY, ["GPT-5", "DeepSeek-Chat"]) == "DeepSeek-Chat")
    r = asyncio.run(adr.run_apex(ds_ticker, "prompt", plan=plan, as_of_date=DAY))
    assert r.model_sources == ["GPT-5"] and r.role == pdb.ROLE_FALLBACK and r.used_fallback is True and "failover_error" in r.fallback_reason
    assert calls == ["deepseek/deepseek-chat", "openai/gpt-5"] and "DeepSeek-Chat" in adr._OUT_OF_CREDIT
    calls.clear()
    for t in TICKERS[:60]:                                                          # now nobody draws DeepSeek, so nobody pays for a failed call
        r = asyncio.run(adr.run_apex(t, "prompt", plan=plan, as_of_date=DAY))
        assert r.model_sources == ["GPT-5"] and r.role == pdb.ROLE_CHAMPION
    assert "deepseek/deepseek-chat" not in calls


def test_if_every_pool_model_is_out_of_credit_the_pool_is_still_tried_rather_than_giving_up_silently():
    adr._OUT_OF_CREDIT.update({"GPT-5", "DeepSeek-Chat"})
    p = adr.plan_for_ticker(pool_plan(), "AAPL", DAY)
    assert p.champion[2] in ("GPT-5", "DeepSeek-Chat")


def test_a_pool_member_that_returns_invalid_json_falls_through_to_the_other_one(monkeypatch):
    async def _fake(model, **kw):
        return _resp("not json" if "deepseek" in model else _VALID)

    monkeypatch.setattr(adr.litellm, "acompletion", _fake)
    ds_ticker = next(t for t in TICKERS if adr.pool_pick(t, DAY, ["GPT-5", "DeepSeek-Chat"]) == "DeepSeek-Chat")
    r = asyncio.run(adr.run_apex(ds_ticker, "prompt", plan=pool_plan(), as_of_date=DAY))
    assert r.model_sources == ["GPT-5"] and r.fallback_reason == "invalid_json"
    assert "DeepSeek-Chat" not in adr._OUT_OF_CREDIT                                # bad JSON is not an out-of-credit signal


# ── settings ──────────────────────────────────────────────────────────────────

def test_engine_settings_store_the_mode_and_pool_and_the_plan_loads_them():
    s = esdb.get_engine_settings()
    assert s["apex_selection_mode"] == "champion" and s["apex_random_pool"] == ["GPT-5", "DeepSeek-Chat"]
    out = esdb.update_engine_settings(updated_by="test", apex_selection_mode="random_pool", apex_random_pool=["GPT-5", "DeepSeek-Chat", "o3"])
    assert out["apex_selection_mode"] == "random_pool" and out["apex_random_pool"] == ["GPT-5", "DeepSeek-Chat", "o3"]
    plan = adr.load_model_plan()
    assert plan.selection_mode == "random_pool" and [m[2] for m in plan.pool] == ["GPT-5", "DeepSeek-Chat", "o3"]


def test_engine_settings_refuse_a_bad_mode_a_short_pool_and_an_unknown_model():
    with pytest.raises(ValueError, match="apex_selection_mode"):
        esdb.update_engine_settings(updated_by="test", apex_selection_mode="roulette")
    with pytest.raises(ValueError, match="at least 2"):
        esdb.update_engine_settings(updated_by="test", apex_random_pool=["GPT-5"])
    with pytest.raises(ValueError, match="unknown APEX model"):
        esdb.update_engine_settings(updated_by="test", apex_random_pool=["GPT-5", "Nope"])
    assert esdb.get_engine_settings()["apex_selection_mode"] == "champion"              # nothing was changed


def test_the_pipeline_draws_per_ticker_and_passes_the_ticker_plan_to_the_run(monkeypatch):
    from tests import test_apex_champion as tc
    esdb.update_engine_settings(updated_by="test", apex_selection_mode="random_pool", apex_random_pool=["GPT-5", "DeepSeek-Chat"])
    seen = {}
    base = tc._result("BUY", "GPT-5", pdb.ROLE_CHAMPION)
    tc._patch_pipeline(monkeypatch, champion=base)

    async def _capture(ticker, prompt, **kw):
        seen[ticker] = kw["plan"]
        return base

    monkeypatch.setattr(adr, "run_apex", _capture)
    tc._run(["AAPL", "MSFT", "NVDA", "AMZN", "GOOG", "META", "TSLA", "KO"], [5])
    assert set(seen) == {"AAPL", "MSFT", "NVDA", "AMZN", "GOOG", "META", "TSLA", "KO"}
    today = date.today().isoformat()
    for t, p in seen.items():
        assert p.selection_mode == "champion" and p.champion[2] == adr.pool_pick(t, today, ["GPT-5", "DeepSeek-Chat"])


def test_the_models_script_shows_and_changes_the_plan(capsys):
    from scripts import apex_models
    assert apex_models.main([]) == 0
    assert "Mode:       champion" in capsys.readouterr().out
    assert apex_models.main(["--pool", "GPT-5", "DeepSeek-Chat", "--fallbacks", "o3"]) == 0
    out = capsys.readouterr().out
    assert "Updated." in out and "random pool [GPT-5 | DeepSeek-Chat]" in out and "Mode:       random_pool" in out
    assert apex_models.main(["--pool", "GPT-5"]) == 2                                    # one model is not a pool
    assert "at least 2" in capsys.readouterr().err
    assert apex_models.main(["--champion", "GPT-5"]) == 0 and "Mode:       champion" in capsys.readouterr().out
    assert apex_models.main(["--challenger", "DeepSeek-Chat", "--challenger-pct", "20"]) == 0
    assert "Challenger: DeepSeek-Chat on 20% of tickers" in capsys.readouterr().out
    assert apex_models.main(["--challenger", "none"]) == 0 and "Challenger: off" in capsys.readouterr().out


# ── Mistral ───────────────────────────────────────────────────────────────────

def test_mistral_higher_models_are_apex_models_and_lower_ones_are_pipeline_only():
    from portfolio_agent._models import FAILOVER_CHAINS
    apex = {label: (mid, prov) for mid, prov, label in adr.APEX_MODELS}
    assert apex["Mistral-Large"] == ("mistral/mistral-large-latest", "mistral")
    assert apex["Magistral-Medium"] == ("mistral/magistral-medium-latest", "mistral")
    assert "Mistral-Small" not in apex and "Ministral-8B" not in apex            # lower tier is never an APEX prediction model
    labels = lambda chain: {l for _, _, l in FAILOVER_CHAINS[chain]}             # noqa: E731
    for low in ("triage", "flash", "fast"):
        assert {"Mistral-Small", "Ministral-8B"} <= labels(low) and not {"Mistral-Large", "Magistral-Medium"} & labels(low)
    for high in ("analyst", "reasoning"):
        assert {"Mistral-Large", "Magistral-Medium"} <= labels(high) and not {"Mistral-Small", "Ministral-8B"} & labels(high)
        assert [l for _, _, l in FAILOVER_CHAINS[high]][-2:] == ["Mistral-Large", "Magistral-Medium"]   # last: the key's tier may not include them


def test_mistral_tier_and_rate_limit_errors_fail_over_and_mark_the_model_unusable():
    from portfolio_agent._models import _is_failover_error
    tier = 'MistralException - {"object":"error","message":"This model is not available in your subscription tier","type":"tier_not_allowed"}'
    assert _is_failover_error(Exception(tier)) is True
    assert _is_failover_error(Exception('MistralException - {"message":"Rate limit exceeded","type":"rate_limited"}')) is True
    assert any(m in tier.lower() for m in adr._CREDIT_MARKERS)


# ── Multi-provider pool, model name / id storage, Cloudflare ──────────────────

def test_openrouter_high_reasoning_models_are_registered_with_their_real_provider():
    apex = {label: (mid, prov) for mid, prov, label in adr.APEX_MODELS}
    assert apex["OR-Grok-4.3"][1] == "openrouter" and apex["OR-Qwen3.7-Max"][1] == "openrouter"
    esdb.update_engine_settings(updated_by="t", apex_selection_mode="random_pool",
                                apex_random_pool=["Claude-Sonnet", "GPT-5", "DeepSeek-Chat", "Mistral-Large", "OR-Grok-4.3", "OR-Qwen3.7-Max"])
    plan = adr.load_model_plan()
    assert plan.selection_mode == "random_pool" and len(plan.pool) == 6
    assert len({prov for _, prov, _ in plan.pool}) == 5                      # anthropic, openai, deepseek, mistral, openrouter


def test_the_winning_model_name_id_provider_and_selection_are_stored(monkeypatch):
    import sqlite3
    from tests.test_apex_champion import _patch_pipeline, _result, _run
    from portfolio_agent.tools import prediction_db as pdb
    res = _result("BUY", "OR-Grok-4.3", pdb.ROLE_CHAMPION, horizons=(5,))
    res.individuals[0] = adr.SinglePrediction("openrouter/x-ai/grok-4.3", "openrouter", "OR-Grok-4.3", res.merged, 10, True)
    res.selection = "random pick from Claude-Sonnet | OR-Grok-4.3"
    _patch_pipeline(monkeypatch, champion=res)
    _run(["AAPL"], [5])
    with sqlite3.connect(str(db_module.DB_PATH)) as c:
        name, prov, mid, used, ev = c.execute("SELECT model_name, model_provider, model_id, model_used, evidence_refs FROM predictions").fetchone()
    assert (name, prov, mid, used) == ("OR-Grok-4.3", "openrouter", "openrouter/x-ai/grok-4.3", "openrouter:OR-Grok-4.3")
    assert "random pick from Claude-Sonnet | OR-Grok-4.3" in ev


def test_cloudflare_models_join_only_the_low_tiers_and_only_when_fully_configured(monkeypatch):
    import importlib
    import portfolio_agent._models as m
    try:
        monkeypatch.setenv("CLOUDFLARE_API_KEY", "k")
        monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
        importlib.reload(m)
        assert not [l for ch in m.FAILOVER_CHAINS.values() for _, _, l in ch if l.startswith("CF-")]
        monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
        importlib.reload(m)
        for low in ("triage", "flash", "fast"):
            assert {"CF-Llama-3.3-70B", "CF-Mistral-Small-3.1"} <= {l for _, _, l in m.FAILOVER_CHAINS[low]}
        for high in ("analyst", "reasoning"):
            assert not [l for _, _, l in m.FAILOVER_CHAINS[high] if l.startswith("CF-")]
    finally:
        monkeypatch.undo()
        importlib.reload(m)
