"""
APEX out-of-credit memory (tools.apex_dual_run): a model that refused for lack of credit is not CALLED again for a while, in any
selection mode and whether it is the pick or a fallback, comes back after the TTL or on a success, and when every model in a chain is
marked only one is probed per ticker. Mirrors the 2026-11-16 intraday log, where a dead pool made every ticker walk the whole chain.
No live LLM calls: litellm.acompletion is stubbed.
"""
from __future__ import annotations

import asyncio

import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.tools import apex_dual_run as adr
from portfolio_agent.tools import prediction_db as pdb

_VALID = '{"recommendation": "BUY", "horizons": [{"horizon_days": 5}]}'
DAY = "2026-10-26"
CREDIT_ERR = 'litellm.BadRequestError: AnthropicException - {"type":"error","error":{"message":"Your credit balance is too low to access the API."}}'
ID2LABEL = {mid: label for mid, _prov, label in adr.APEX_MODELS}


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    clock = Clock()
    monkeypatch.setattr(adr, "_now", clock)
    adr._OUT_OF_CREDIT.clear(); adr._DEAD_UNTIL.clear()
    yield clock
    adr._OUT_OF_CREDIT.clear(); adr._DEAD_UNTIL.clear()


def _resp(text):
    class _M:
        content = text
    class _C:
        message = _M()
    class _R:
        choices = [_C()]
    return _R()


class Provider:
    """behaviour per label: 'ok' | 'credit' | 'badjson'; records every call."""
    def __init__(self, monkeypatch, behaviour):
        self.behaviour, self.calls = dict(behaviour), []
        monkeypatch.setattr(adr.litellm, "acompletion", self._call)

    async def _call(self, model, **kw):
        label = ID2LABEL[model]
        self.calls.append(label)
        b = self.behaviour[label]
        if b == "credit":
            raise Exception(CREDIT_ERR)
        if b == "ratelimit":
            raise RuntimeError("429 rate limit")
        return _resp("not json" if b == "badjson" else _VALID)

    def take(self):
        got, self.calls = self.calls, []
        return got


def run(t="AAPL", plan=None):
    return asyncio.run(adr.run_apex(t, "prompt", plan=plan, as_of_date=DAY))


def champ_plan(champion="Claude-Sonnet", fallbacks=("DeepSeek-Chat", "Mistral-Large", "o3"), **kw):
    return adr.build_model_plan(champion=champion, fallbacks=list(fallbacks), **kw)


DEAD = {"Claude-Sonnet": "credit", "DeepSeek-Chat": "credit", "Mistral-Large": "credit", "o3": "ok"}


def test_a_dead_champion_and_dead_fallbacks_are_not_called_again_after_the_first_ticker(monkeypatch):
    prov = Provider(monkeypatch, DEAD)
    r = run("T1", champ_plan())
    assert prov.take() == ["Claude-Sonnet", "DeepSeek-Chat", "Mistral-Large", "o3"]        # the first ticker discovers all three
    assert r.model_sources == ["o3"] and adr._OUT_OF_CREDIT == {"Claude-Sonnet", "DeepSeek-Chat", "Mistral-Large"}
    for t in ("T2", "T3", "T4"):
        r = run(t, champ_plan())
        assert prov.take() == ["o3"]                                                        # straight to the model that works
        assert r.model_sources == ["o3"]


def test_a_skipped_champion_still_records_the_row_as_a_fallback_with_a_reason(monkeypatch):
    prov = Provider(monkeypatch, DEAD)
    run("T1", champ_plan()); prov.take()
    r = run("T2", champ_plan())
    assert r.role == pdb.ROLE_FALLBACK and r.used_fallback is True and r.fallback_model == "o3"
    assert r.fallback_reason == "skipped_out_of_credit"                                    # not mistaken for a champion row


def test_the_pool_mode_log_scenario_every_pool_model_dead_but_one_fallback(monkeypatch):
    pool = ["Claude-Sonnet", "GPT-5", "DeepSeek-Chat", "Mistral-Large"]
    plan = adr.build_model_plan(champion="GPT-5", fallbacks=["o3"], selection_mode="random_pool", random_pool=pool)
    prov = Provider(monkeypatch, {**DEAD, "GPT-5": "credit"})
    for t in [f"T{i}" for i in range(12)]:                                                  # drawing different pool members per ticker
        run(t, plan)
    prov.take()
    assert set(adr._OUT_OF_CREDIT) == set(pool)                                             # the whole pool is marked, as in the log
    for t in [f"U{i}" for i in range(30)]:
        r = run(t, plan)
        assert prov.take() == ["o3"] and r.model_sources == ["o3"]                          # one call each, not seven


def test_a_mark_expires_so_a_topped_up_account_is_retried_and_cleared_on_success(monkeypatch, _env):
    prov = Provider(monkeypatch, DEAD)
    run("T1", champ_plan()); prov.take()
    prov.behaviour["Claude-Sonnet"] = "ok"                                                  # the account is topped up
    run("T2", champ_plan())
    assert prov.take() == ["o3"]                                                            # still inside the TTL: not retried yet
    _env.t += adr.DEAD_MODEL_TTL_SECONDS + 1
    r = run("T3", champ_plan())
    assert prov.take() == ["Claude-Sonnet"] and r.role == pdb.ROLE_CHAMPION                # retried, works, champion again
    assert "Claude-Sonnet" not in adr._OUT_OF_CREDIT and "Claude-Sonnet" not in adr._DEAD_UNTIL


def test_a_mark_that_is_still_dead_after_the_ttl_is_re_marked(monkeypatch, _env):
    prov = Provider(monkeypatch, DEAD)
    run("T1", champ_plan()); prov.take()
    _env.t += adr.DEAD_MODEL_TTL_SECONDS + 1
    run("T2", champ_plan())
    assert prov.take() == ["Claude-Sonnet", "DeepSeek-Chat", "Mistral-Large", "o3"]        # all expired: one more discovery walk
    run("T3", champ_plan())
    assert prov.take() == ["o3"]


def test_when_every_model_is_marked_only_one_is_probed_per_ticker_and_the_probe_rotates(monkeypatch, _env):
    allcredit = {k: "credit" for k in DEAD}
    prov = Provider(monkeypatch, allcredit)
    assert run("T1", champ_plan()) is None
    assert prov.take() == ["Claude-Sonnet", "DeepSeek-Chat", "Mistral-Large", "o3"]
    seen = []
    for t in ("T2", "T3", "T4", "T5"):
        _env.t += 1
        assert run(t, champ_plan()) is None
        got = prov.take()
        assert len(got) == 1                                                                # one probe, never the full walk
        seen.append(got[0])
    assert seen[:4] == ["Claude-Sonnet", "DeepSeek-Chat", "Mistral-Large", "o3"]            # earliest-expiring mark first, round robin


def test_a_probe_that_succeeds_brings_that_model_back(monkeypatch, _env):
    prov = Provider(monkeypatch, {k: "credit" for k in DEAD})
    run("T1", champ_plan()); prov.take()
    prov.behaviour["o3"] = "ok"
    got = []
    for t in ("T2", "T3", "T4", "T5"):
        _env.t += 1
        r = run(t, champ_plan())
        got.append((tuple(prov.take()), r.model_sources if r else None))
    assert got[3] == (("o3",), ["o3"]) and "o3" not in adr._OUT_OF_CREDIT                  # the 4th probe reached o3
    r = run("T6", champ_plan())
    assert prov.take() == ["o3"] and r.model_sources == ["o3"]                              # and the dead three stay skipped


def test_bad_json_and_timeouts_are_never_marked_dead(monkeypatch):
    prov = Provider(monkeypatch, {**DEAD, "Claude-Sonnet": "badjson"})
    r = run("T1", champ_plan(fallbacks=("o3",)))
    assert prov.take() == ["Claude-Sonnet", "o3"] and r.fallback_reason == "invalid_json" and not adr._OUT_OF_CREDIT
    run("T2", champ_plan(fallbacks=("o3",)))
    assert prov.take() == ["Claude-Sonnet", "o3"]                                           # still tried: bad JSON is not a credit signal


def test_openai_no_credits_remaining_is_recognised_without_relying_on_a_url_in_the_message():
    res = adr.SinglePrediction("m", "openai", "GPT-5", {}, 1, False, "RateLimitError: You have no credits remaining. Add credits.")
    adr._mark_if_out_of_credit(res)
    assert "GPT-5" in adr._OUT_OF_CREDIT


def test_hand_set_marks_never_expire_and_a_cleared_set_forgets_its_expiries(_env):
    adr._OUT_OF_CREDIT.add("GPT-5")
    _env.t += 10 * adr.DEAD_MODEL_TTL_SECONDS
    assert adr._dead_labels() == {"GPT-5"}
    adr._OUT_OF_CREDIT.clear()
    adr._DEAD_UNTIL["o3"] = 1.0
    assert adr._dead_labels() == set() and "o3" not in adr._DEAD_UNTIL


def test_the_challenger_is_not_called_while_it_is_marked_dead(monkeypatch):
    prov = Provider(monkeypatch, {"Claude-Sonnet": "credit", "o3": "ok", "DeepSeek-Chat": "ok", "Mistral-Large": "ok"})
    plan = champ_plan(champion="o3", fallbacks=("DeepSeek-Chat",), challenger="Claude-Sonnet", challenger_pct=100)
    assert asyncio.run(adr.run_challenger("T1", "prompt", plan)) is None
    assert prov.take() == ["Claude-Sonnet"] and "Claude-Sonnet" in adr._OUT_OF_CREDIT
    assert asyncio.run(adr.run_challenger("T2", "prompt", plan)) is None
    assert prov.take() == []                                                                # no second paid failure


def test_a_rate_limited_model_is_skipped_for_a_short_cooldown_then_retried(monkeypatch, _env):
    prov = Provider(monkeypatch, {**DEAD, "Claude-Sonnet": "ratelimit", "DeepSeek-Chat": "ok"})
    run("T1", champ_plan()); prov.take()
    assert "Claude-Sonnet" in adr._OUT_OF_CREDIT and adr._DEAD_UNTIL["Claude-Sonnet"] == _env.t + adr.RATE_LIMIT_COOLDOWN_SECONDS
    run("T2", champ_plan())
    assert "Claude-Sonnet" not in prov.take()                                               # not paid for again inside the cool-down
    prov.behaviour["Claude-Sonnet"] = "ok"
    _env.t += adr.RATE_LIMIT_COOLDOWN_SECONDS + 1                                           # far shorter than the credit mark
    assert adr.RATE_LIMIT_COOLDOWN_SECONDS < adr.DEAD_MODEL_TTL_SECONDS
    r = run("T3", champ_plan())
    assert prov.take()[0] == "Claude-Sonnet" and r.role == pdb.ROLE_CHAMPION


def test_a_bad_json_answer_is_not_a_rate_limit(monkeypatch):
    prov = Provider(monkeypatch, {**DEAD, "Claude-Sonnet": "badjson"})
    run("T1", champ_plan()); prov.take()
    assert "Claude-Sonnet" not in adr._OUT_OF_CREDIT
