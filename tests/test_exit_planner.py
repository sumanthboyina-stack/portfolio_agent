"""
Exit Planner v2 (trade-plan-v2): the pure rules in tools.trade_plan. Hand-computed, no DB, no network.
DB / schema / settings, simulator, service + privacy and UI tests are added alongside the later commits.
"""
from __future__ import annotations

import sqlite3

import pandas as pd
import pytest

import portfolio_agent.tools.db as db_module
from portfolio_agent.tools import engine_settings_db as esdb
from portfolio_agent.tools import prediction_db as pdb
from portfolio_agent.tools import trade_plan as tp
from portfolio_agent.tools import trade_plan_db as tpdb
from tests.test_trade_plan import _ohlc, _shared, _store_snapshot   # noqa: F401  (shared fixtures/helpers)

AS_OF = "2026-10-26"            # a Monday
S = tp.seed_settings()


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(pdb, "_today_cst", lambda: AS_OF)
    monkeypatch.setattr(pdb, "is_trading_day", lambda d: d.weekday() < 5)
V2 = tp.RULES_VERSION


def plan(entry=100.0, atr=2.0, h=5, direction="UP", settings=S, **kw):
    kw.setdefault("as_of_date", AS_OF)
    return tp.build_plan(entry, atr, h, direction, settings, rules_version=V2, **kw)


def reason(*a, **kw):
    r = plan(*a, **kw)
    assert isinstance(r, tp.NoPlan), r
    return r.reason


def test_versions():
    assert tp.RULES_VERSION == "trade-plan-v2" and tp.RULES_VERSION_V1 == "trade-plan-v1"
    assert tp.build_plan(100.0, 2.0, 5, "UP", S, as_of_date=AS_OF).stop == 97.0      # default stays v1


def test_v1_rejects_v2_only_arguments():
    with pytest.raises(ValueError):
        tp.build_plan(100.0, 2.0, 5, "UP", S, as_of_date=AS_OF, stop_rule="pct", stop_param=0.05)


# ── defaults: v2 == v1 apart from cent rounding ───────────────────────────────

@pytest.mark.parametrize("h, atr", [(5, 2.0), (21, 2.0), (63, 2.0), (5, 1.337), (21, 0.873), (63, 3.1415)])
def test_v2_default_matches_v1_within_a_cent(h, atr):
    a = tp.build_plan(100.0, atr, h, "UP", S, as_of_date=AS_OF, predicted_return_high=0.03)
    b = plan(100.0, atr, h, predicted_return_high=0.03)
    assert isinstance(a, tp.TradePlan) and isinstance(b, tp.TradePlan)
    r = tp.SEED_TARGET_R_MULTIPLE[h]
    assert abs(a.stop - b.stop) <= 0.01 + 1e-9                    # one cent: the stop's floor
    assert abs(a.target - b.target) <= 0.005 + r * 0.01 + 0.005 + 1e-9   # entry half-cent + R x (stop cent) + target half-cent
    assert b.stop <= a.stop + 1e-9                                # the stop only ever rounds AWAY from entry
    assert b.time_stop_date == a.time_stop_date and b.llm_target == a.llm_target
    assert (b.stop_rule, b.target_rule, b.rules_version) == ("atr", "r_multiple", V2)
    assert b.stop_param == tp.SEED_STOP_ATR_MULT[h] and b.target_param == tp.SEED_TARGET_R_MULTIPLE[h]


def test_v2_prices_are_whole_cents_and_risk_is_entry_minus_stop():
    p = plan(123.456, 1.337, 21)
    for v in (p.entry, p.stop, p.target):
        assert round(v, 2) == pytest.approx(v)
    assert p.entry == 123.46 and p.risk_per_share == pytest.approx(p.entry - p.stop)
    assert p.reward_risk == pytest.approx((p.target - p.entry) / p.risk_per_share, abs=1e-4)


def test_stop_rounds_down_target_and_entry_round_half_up():
    p = plan(100.005, 2.0, 5)                       # entry 100.005 -> 100.01 (half up); raw stop 97.005 -> floor 97.00
    assert p.entry == 100.01 and p.stop == 97.00
    assert p.target == 104.53                       # 100.01 + 1.5 * 3.01 = 104.525 -> half up


# ── stop rules × horizons ─────────────────────────────────────────────────────

RANGE = (110.0, 94.37)           # (highest high, lowest low) of the lookback bars


@pytest.mark.parametrize("h", [5, 21, 63])
def test_atr_rule_with_explicit_multiple(h):
    p = plan(100.0, 2.0, h, stop_rule="atr", stop_param=2.0)
    assert (p.stop, p.risk_per_share, p.stop_rule, p.stop_param) == (96.0, 4.0, "atr", 2.0)


@pytest.mark.parametrize("h", [5, 21, 63])
def test_pct_rule(h):
    p = plan(100.0, 2.0, h, stop_rule="pct", stop_param=0.05)
    assert (p.stop, p.risk_per_share) == (95.0, 5.0)
    assert p.reward_risk == pytest.approx(tp.SEED_TARGET_R_MULTIPLE[h])


def test_pct_rule_floors_to_the_cent_away_from_entry():
    p = plan(33.33, 0.5, 5, stop_rule="pct", stop_param=0.07)      # 33.33 * 0.93 = 30.9969 -> 30.99
    assert p.stop == 30.99


@pytest.mark.parametrize("h", [5, 21, 63])
def test_range_low_rule(h):
    p = plan(100.0, 2.0, h, stop_rule="range_low", range_hl=RANGE)
    assert p.stop == 94.37 and p.stop_rule == "range_low" and p.stop_param == tp.SEED_RANGE_LOOKBACK_DAYS[h]


@pytest.mark.parametrize("h", [5, 21, 63])
def test_range_mid_rule_floors(h):
    p = plan(100.0, 2.0, h, stop_rule="range_mid", range_hl=RANGE)  # (110 + 94.37)/2 = 102.185 -> above entry
    assert isinstance(p, tp.NoPlan) and p.reason == "stop_not_below_entry"
    q = plan(105.0, 2.0, h, stop_rule="range_mid", range_hl=RANGE)  # midpoint 102.185 -> floor 102.18
    assert q.stop == 102.18


def test_range_rules_honour_an_explicit_lookback_and_validate_it():
    assert plan(100.0, 2.0, 5, stop_rule="range_low", stop_param=10, range_hl=RANGE).stop_param == 10
    for bad in (1, 61, 7.5, "x"):
        assert reason(100.0, 2.0, 5, stop_rule="range_low", stop_param=bad, range_hl=RANGE) == "invalid_rule_param"
    assert reason(100.0, 2.0, 5, stop_rule="range_low") == "missing_range"
    assert reason(100.0, 2.0, 5, stop_rule="range_low", range_hl=(90.0, 95.0)) == "missing_range"   # high < low


@pytest.mark.parametrize("h", [5, 21, 63])
def test_price_rule(h):
    p = plan(100.0, 2.0, h, stop_rule="price", stop_param=96.557)
    assert p.stop == 96.55 and p.stop_param == 96.557            # floored to the cent


def test_atr_rule_still_needs_atr_but_other_rules_do_not():
    assert reason(100.0, None, 5) == "missing_atr"
    assert isinstance(plan(100.0, None, 5, stop_rule="pct", stop_param=0.05), tp.TradePlan)


def test_bad_rules_and_params():
    assert reason(stop_rule="bogus") == "invalid_rule"
    assert reason(target_rule="bogus") == "invalid_rule"
    for bad in (None, 0, -0.1, 1.0, 1.5):
        assert reason(stop_rule="pct", stop_param=bad) == "invalid_rule_param"
    assert reason(stop_rule="price", stop_param=None) == "invalid_rule_param"
    assert reason(stop_rule="atr", stop_param=-1) == "invalid_rule_param"


# ── check order ───────────────────────────────────────────────────────────────

def test_stop_not_below_entry_and_at_or_below_zero():
    assert reason(stop_rule="price", stop_param=100.0) == "stop_not_below_entry"
    assert reason(stop_rule="price", stop_param=105.0) == "stop_not_below_entry"
    assert reason(100.0, 40.0, 63) == "stop_at_or_below_zero"      # 4.0 * 40 = 160 > entry
    assert reason(stop_rule="price", stop_param=0.004) == "stop_at_or_below_zero"   # floors to $0.00


def test_too_volatile_beats_too_tight_and_below_min_rr():
    # 9% stop on a 5d plan (max 8%) with a target price that also fails the minimum reward:risk
    assert reason(stop_rule="price", stop_param=91.0, target_rule="price", target_param=101.0) == "too_volatile"


def test_too_volatile_reported_before_below_min_rr_for_the_atr_rule():
    assert reason(100.0, 6.0, 5, target_rule="price", target_param=100.5) == "too_volatile"


def test_below_min_rr_and_target_rules():
    assert reason(stop_rule="price", stop_param=95.0, target_rule="price", target_param=105.0) == "below_min_rr"   # R = 1.0 < 1.5
    p = plan(stop_rule="price", stop_param=95.0, target_rule="price", target_param=107.5)
    assert p.target == 107.5 and p.reward_risk == pytest.approx(1.5) and p.target_rule == "price"
    assert reason(target_rule="price", target_param=100.0) == "target_not_above_entry"
    assert reason(target_rule="price", target_param=90.0) == "target_not_above_entry"
    assert reason(target_rule="price", target_param=None) == "invalid_rule_param"


def test_reward_risk_is_recomputed_from_rounded_prices():
    p = plan(100.0, 2.0, 5, stop_rule="price", stop_param=96.667, target_rule="price", target_param=105.555)
    assert (p.stop, p.target) == (96.66, 105.56)
    assert p.reward_risk == pytest.approx((105.56 - 100.0) / (100.0 - 96.66), abs=1e-4)


def test_rounding_alone_never_fails_the_minimum_for_the_r_multiple_rule():
    # R == min reward:risk (5d seed 1.5): cent rounding of the target must not flip the plan to below_min_rr
    for entry in (50.07, 87.31, 213.49, 9.99):
        for atr in (0.41, 1.07, 2.33):
            r = plan(entry, atr, 5)
            assert not (isinstance(r, tp.NoPlan) and r.reason == "below_min_rr"), (entry, atr)


# ── too tight ─────────────────────────────────────────────────────────────────

def test_min_risk_floor_picks_the_binding_floor_and_accepts_a_spread():
    assert tp.min_risk_floor(100.0, 2.0, 5, S) == (pytest.approx(1.0), "atr")       # 0.5 * 2 = 1.0 > 0.05
    assert tp.min_risk_floor(100.0, 0.02, 5, S) == (pytest.approx(0.05), "pct")     # 0.05% of 100 > 0.5 * 0.02
    assert tp.min_risk_floor(100.0, 2.0, 5, S, spread=1.5) == (pytest.approx(1.5), "spread")
    assert tp.min_risk_floor(100.0, None, 5, S) == (pytest.approx(0.05), "pct")


def test_too_tight_fires_on_the_atr_floor_alone():
    r = plan(100.0, 2.0, 5, stop_rule="pct", stop_param=0.005)       # risk $0.50 < 0.5 * 2.0 = $1.00; pct floor is only $0.05
    assert isinstance(r, tp.NoPlan) and r.reason == "too_tight"
    assert "$0.50" in r.message and "half a day's typical move ($1.00)" in r.message and "Widen it" in r.message


def test_too_tight_fires_on_the_pct_floor_alone():
    r = plan(100.0, 0.02, 5, stop_rule="price", stop_param=99.97)    # risk $0.03 < $0.05; ATR floor is only $0.01
    assert r.reason == "too_tight" and "$0.03" in r.message and "0.05%" in r.message and "$100.00" in r.message


def test_too_tight_when_both_floors_are_breached_names_the_larger():
    r = plan(100.0, 2.0, 5, stop_rule="price", stop_param=99.97)     # risk $0.03: below $0.05 and below $1.00
    assert r.reason == "too_tight" and "typical move ($1.00)" in r.message


def test_too_tight_does_not_fire_just_above_the_floors():
    assert isinstance(plan(100.0, 2.0, 5, stop_rule="price", stop_param=99.0), tp.TradePlan)          # risk exactly $1.00 = floor
    assert isinstance(plan(100.0, 0.02, 5, stop_rule="price", stop_param=99.94), tp.TradePlan)        # risk $0.06 > $0.05
    assert reason(100.0, 2.0, 5, stop_rule="price", stop_param=99.01) == "too_tight"                  # risk $0.99 < $1.00


def test_too_tight_applies_to_every_horizon_floor():
    for h, frac in tp.SEED_MIN_STOP_ATR_FRAC.items():
        atr = 2.0
        just_under = 100.0 - (frac * atr - 0.02)
        assert reason(100.0, atr, h, stop_rule="price", stop_param=just_under) == "too_tight"


# ── non-UP forecasts ──────────────────────────────────────────────────────────

def test_down_flat_and_no_direction_have_their_own_reasons():
    assert reason(direction="DOWN") == "down"
    assert reason(direction="FLAT") == "flat"
    assert reason(direction=None) == "no_direction"
    assert reason(direction="sideways") == "no_direction"
    assert plan(direction="FLAT").message == "The forecast is FLAT: no directional trade plan."
    assert "short" in tp.NoPlan("down").message
    assert "not_up" in tp.NO_PLAN_REASONS                           # old v1 rows stay readable


def test_250d_stays_unsupported():
    assert reason(100.0, 2.0, 250) == "unsupported_horizon"
    assert tp.SUPPORTED_HORIZONS == (5, 21, 63)


def test_protective_plan_has_a_stop_and_a_trim_level_but_no_target():
    p = tp.build_protective_plan(100.0, 2.0, 5, "DOWN", S, as_of_date=AS_OF, predicted_return_high=0.02)
    assert isinstance(p, tp.ProtectivePlan)
    assert (p.stop, p.risk_per_share, p.trim_level, p.stop_rule) == (97.0, 3.0, 102.0, "atr")
    d = p.to_dict()
    assert d["target"] is None and d["reward_risk"] is None and d["kind"] == "protective"
    assert p.time_stop_date == tp.build_plan(100.0, 2.0, 5, "UP", S, as_of_date=AS_OF).time_stop_date


def test_protective_trim_level_only_when_above_entry():
    for high in (-0.01, 0.0, None):
        p = tp.build_protective_plan(100.0, 2.0, 5, "DOWN", S, as_of_date=AS_OF, predicted_return_high=high)
        assert p.trim_level is None


def test_protective_plan_uses_the_same_stop_rules_and_checks():
    p = tp.build_protective_plan(100.0, 2.0, 21, "DOWN", S, as_of_date=AS_OF, stop_rule="range_low", range_hl=RANGE)
    assert p.stop == 94.37
    for kw, why in [(dict(stop_rule="pct", stop_param=0.005), "too_tight"), (dict(stop_rule="price", stop_param=105.0), "stop_not_below_entry"),
                    (dict(stop_rule="pct", stop_param=0.2), "too_volatile")]:
        r = tp.build_protective_plan(100.0, 2.0, 5, "DOWN", S, as_of_date=AS_OF, **kw)
        assert isinstance(r, tp.NoPlan) and r.reason == why


def test_protective_plan_only_for_down_and_supported_horizons():
    for d in ("UP", "FLAT", None):
        assert tp.build_protective_plan(100.0, 2.0, 5, d, S, as_of_date=AS_OF).reason == "not_down"
    assert tp.build_protective_plan(100.0, 2.0, 250, "DOWN", S, as_of_date=AS_OF).reason == "unsupported_horizon"
    assert tp.build_protective_plan(None, 2.0, 5, "DOWN", S, as_of_date=AS_OF).reason == "missing_price"


def test_llm_target_never_moves_a_v2_plan():
    a = plan(predicted_return_high=0.01)
    b = plan(predicted_return_high=0.50)
    assert (a.stop, a.target, a.reward_risk) == (b.stop, b.target, b.reward_risk)


# ══ DB layer, schema and settings ═════════════════════════════════════════════

def _rows():
    with sqlite3.connect(str(db_module.DB_PATH)) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute("SELECT * FROM trade_plans ORDER BY prediction_id")]


def test_shared_plan_is_stored_as_v2_with_its_rules():
    _store_snapshot("AAPL", AS_OF, 2.0)
    row = tpdb.create_plan_for_prediction(_shared())
    assert row["rules_version"] == "trade-plan-v2"
    assert (row["stop_rule"], row["stop_param"], row["target_rule"], row["target_param"]) == ("atr", 1.5, "r_multiple", 1.5)
    assert (row["stop"], row["target"]) == (97.0, 104.5)


def test_stored_non_up_reasons():
    _store_snapshot("AAPL", AS_OF, 2.0)
    assert tpdb.create_plan_for_prediction(_shared("MSFT", direction="DOWN"))["no_plan_reason"] == "not_up"   # unchanged for DOWN
    flat = tpdb.create_plan_for_prediction(_shared("NVDA", direction="FLAT"))
    assert (flat["plan_status"], flat["no_plan_reason"], flat["stop"]) == ("no_plan", "flat", None)
    assert tpdb.create_plan_for_prediction(_shared("TSLA", horizon=21, direction="UP"))["no_plan_reason"] == "missing_atr"


def test_a_row_written_before_v2_reads_as_atr_and_r_multiple():
    pid = _shared("AAPL")
    with sqlite3.connect(str(db_module.DB_PATH)) as c:        # exactly the v1 column set: no stop_rule/target_rule given
        c.execute("""INSERT INTO trade_plans (prediction_id, plan_status, entry, stop, target, risk_per_share, reward_risk,
                     time_stop_date, atr_14, rules_version, created_at)
                     VALUES (?, 'planned', 100, 97, 104.5, 3, 1.5, '2026-11-02', 2.0, 'trade-plan-v1', 'x')""", [pid])
    row = tpdb.get_plan(pid)
    assert (row["stop_rule"], row["target_rule"], row["stop_param"], row["rules_version"]) == ("atr", "r_multiple", None, "trade-plan-v1")


def test_backfill_never_rewrites_a_v1_row_and_create_is_idempotent():
    pid = _shared("AAPL")
    with sqlite3.connect(str(db_module.DB_PATH)) as c:
        c.execute("""INSERT INTO trade_plans (prediction_id, plan_status, entry, stop, target, risk_per_share, reward_risk,
                     time_stop_date, atr_14, rules_version, created_at)
                     VALUES (?, 'planned', 100, 97, 104.5, 3, 1.5, '2026-11-02', 2.0, 'trade-plan-v1', 'x')""", [pid])
    before = _rows()
    assert tpdb.backfill_plans("2026-01-01", fetch_history=lambda t, d: _ohlc(False))["candidates"] == 0
    assert tpdb.create_plan_for_prediction(pid, fetch_history=lambda t, d: _ohlc(False)) == tpdb.get_plan(pid)
    assert _rows() == before and before[0]["rules_version"] == "trade-plan-v1"
    other = _shared("MSFT")
    first = tpdb.create_plan_for_prediction(other, fetch_history=lambda t, d: _ohlc(False))
    assert tpdb.create_plan_for_prediction(other, fetch_history=lambda t, d: _ohlc(True)) == first


def test_migration_is_idempotent():
    for _ in range(2):
        with pdb._db() as c:
            cols = {r[1] for r in c.execute("PRAGMA table_info(trade_plans)").fetchall()}
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"stop_rule", "stop_param", "target_rule", "target_param"} <= cols and "custom_trade_plans" in tables


def test_custom_trade_plans_is_append_only_except_superseded_by():
    pid = _shared("AAPL")
    ins = """INSERT INTO custom_trade_plans (owner, prediction_id, kind, stop_rule, plan_status, rules_version, created_at, created_by)
             VALUES ('alice', ?, 'custom_long', 'pct', 'planned', 'trade-plan-v2', 'x', 'alice')"""
    with pdb._db() as c:
        c.execute(ins, [pid]); c.execute(ins, [pid]); c.commit()
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            c.execute("UPDATE custom_trade_plans SET stop = 1 WHERE id = 1")
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            c.execute("DELETE FROM custom_trade_plans WHERE id = 1")
        c.execute("UPDATE custom_trade_plans SET superseded_by = 2 WHERE id = 1")          # the one allowed change
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            c.execute("UPDATE custom_trade_plans SET superseded_by = 3 WHERE id = 1")      # ...and only once
        with pytest.raises(sqlite3.IntegrityError):                                         # kind is constrained
            c.execute("INSERT INTO custom_trade_plans (owner, prediction_id, kind, stop_rule, plan_status, rules_version, created_at, created_by) "
                      "VALUES ('a', ?, 'bogus', 'pct', 'planned', 'v', 'x', 'a')", [pid])


# ── recent_range_as_of ────────────────────────────────────────────────────────

def _bars_with_range(n=30):
    idx = pd.bdate_range(end=AS_OF, periods=n)
    close = pd.Series(100.0, index=idx)
    high, low = close + 1.0, close - 1.0
    high.iloc[-3], low.iloc[-4] = 108.0, 93.5          # inside the last 5 bars
    high.iloc[-12], low.iloc[-12] = 120.0, 80.0         # only inside a long window
    return pd.DataFrame({"Close": close, "High": high, "Low": low})


def test_recent_range_picks_the_last_n_completed_bars():
    f = lambda t, d: _bars_with_range()
    assert tpdb.recent_range_as_of("AAPL", AS_OF, 5, fetch_history=f) == (108.0, 93.5)
    assert tpdb.recent_range_as_of("AAPL", AS_OF, 10, fetch_history=f) == (108.0, 93.5)
    assert tpdb.recent_range_as_of("AAPL", AS_OF, 20, fetch_history=f) == (120.0, 80.0)
    assert tpdb.recent_range_as_of("AAPL", AS_OF, 1, fetch_history=f) == (101.0, 99.0)   # the as_of bar itself is included


def test_recent_range_ignores_bars_after_the_forecast_date():
    quiet = tpdb.recent_range_as_of("AAPL", AS_OF, 5, fetch_history=lambda t, d: _ohlc(False))
    spiked = tpdb.recent_range_as_of("AAPL", AS_OF, 5, fetch_history=lambda t, d: _ohlc(True))   # next 3 days: high 200, low 90
    assert quiet == (101.0, 99.0) and spiked == quiet


def test_recent_range_needs_enough_fresh_bars():
    assert tpdb.recent_range_as_of("AAPL", AS_OF, 30, fetch_history=lambda t, d: _bars_with_range(10)) is None
    assert tpdb.recent_range_as_of("AAPL", AS_OF, 5, fetch_history=lambda t, d: None) is None
    assert tpdb.recent_range_as_of("AAPL", AS_OF, 5, fetch_history=lambda t, d: 1 / 0) is None   # a fetch failure is "no range"
    stale = _bars_with_range().iloc[:-10]
    assert tpdb.recent_range_as_of("AAPL", AS_OF, 5, fetch_history=lambda t, d: stale) is None


def test_shared_plan_can_default_to_a_range_stop():
    s = {**tp.seed_settings(), "plan_default_stop_rule": "range_low"}
    row = tpdb.create_plan_for_prediction(_shared("AAPL"), settings=s, fetch_history=lambda t, d: _ohlc(False))
    assert (row["stop_rule"], row["stop_param"]) == ("range_low", 5.0)
    # the last 5 bars' lowest low is 99 -> risk $1.00, exactly the 5d floor (0.5 x ATR 2.0): allowed at the boundary
    assert (row["plan_status"], row["stop"], row["risk_per_share"], row["target"]) == ("planned", 99.0, 1.0, 101.5)


# ── settings ──────────────────────────────────────────────────────────────────

def test_seeds_include_the_v2_keys():
    seed = tp.seed_settings()
    assert seed["plan_min_stop_pct"] == 0.0005
    assert seed["plan_min_stop_atr_frac"] == {5: 0.5, 21: 0.75, 63: 1.0}
    assert seed["plan_range_lookback_days"] == {5: 5, 21: 10, 63: 20}
    assert (seed["plan_default_stop_rule"], seed["plan_default_target_rule"]) == ("atr", "r_multiple")
    assert esdb.get_trade_plan_settings() == seed                       # a fresh DB reads exactly the seeds


@pytest.mark.parametrize("patch", [
    {"plan_min_stop_pct": 0}, {"plan_min_stop_pct": 0.011}, {"plan_min_stop_pct": -0.1},
    {"plan_min_stop_atr_frac": {5: -0.1}}, {"plan_min_stop_atr_frac": {5: 3.1}},
    {"plan_range_lookback_days": {5: 1}}, {"plan_range_lookback_days": {21: 61}}, {"plan_range_lookback_days": {21: 7.5}},
    {"plan_default_stop_rule": "price"}, {"plan_default_stop_rule": "pct"}, {"plan_default_stop_rule": "bogus"},
    {"plan_default_target_rule": "price"}, {"plan_min_stop_atr_frac": {250: 1.0}}, {"plan_nonsense": 1},
])
def test_settings_validation_rejects_out_of_range(patch):
    with pytest.raises(ValueError):
        esdb.update_engine_settings(updated_by="t", trade_plan=patch)
    assert esdb.get_trade_plan_settings() == tp.seed_settings()          # a rejected patch changes nothing


def test_settings_accept_boundaries_and_persist_with_int_lookbacks():
    esdb.update_engine_settings(updated_by="t", trade_plan={"plan_min_stop_pct": 0.01, "plan_min_stop_atr_frac": {5: 0, 21: 3},
                                                             "plan_range_lookback_days": {5: 2, 63: 60}, "plan_default_stop_rule": "range_mid"})
    got = esdb.get_trade_plan_settings()
    assert got["plan_min_stop_pct"] == 0.01 and got["plan_min_stop_atr_frac"][5] == 0.0 and got["plan_min_stop_atr_frac"][21] == 3.0
    assert got["plan_range_lookback_days"] == {5: 2, 21: 10, 63: 60} and all(isinstance(v, int) for v in got["plan_range_lookback_days"].values())
    assert got["plan_default_stop_rule"] == "range_mid"


def test_reset_restores_seeds_and_updates_are_versioned_and_audited(monkeypatch):
    from portfolio_agent.services import investor_service as svc
    from portfolio_agent.services.context import local_context
    from portfolio_agent.tools import holdings_db as repo
    ctx = local_context("test")
    with pytest.raises(svc.NotAdmin):
        svc.update_trade_plan_settings(ctx, {"plan_min_stop_pct": 0.002})
    monkeypatch.setattr(svc, "require_admin", lambda c: None)
    v0 = esdb.get_engine_settings()["version"]
    after = svc.update_trade_plan_settings(ctx, {"plan_min_stop_pct": 0.002, "plan_range_lookback_days": {5: 7}}, expected_version=v0)
    assert after["plan_min_stop_pct"] == 0.002 and after["plan_range_lookback_days"][5] == 7
    assert esdb.get_engine_settings()["version"] == v0 + 1
    with pytest.raises(Exception):                                       # a stale expected_version is refused
        svc.update_trade_plan_settings(ctx, {"plan_min_stop_pct": 0.003}, expected_version=v0)
    reset = svc.reset_trade_plan_settings(ctx)
    assert reset == tp.seed_settings() and esdb.get_trade_plan_settings() == tp.seed_settings()
    ops = [e["operation"] for e in repo.get_audit_events()]
    assert ops.count("update_trade_plan_settings") == 2


# ══ Simulator ═════════════════════════════════════════════════════════════════

import numpy as np                                                   # noqa: E402
from types import SimpleNamespace                                     # noqa: E402

from portfolio_agent.tools import plan_simulator as ps               # noqa: E402
from tests.test_plan_simulator import _bars                          # noqa: E402

LOOSE = {"max_stop_pct": 0.5, "min_reward_risk": 1.5, "min_stop_pct": 0.0005, "min_stop_atr_frac": 0.0}


def _rule(stop_rule, stop_param=None, target_rule="r_multiple", target_param=None, **kw):
    return {"stop_atr_mult": 1.0, "target_r_multiple": 2.0, **LOOSE, "stop_rule": stop_rule, "stop_param": stop_param,
            "target_rule": target_rule, "target_param": target_param, "range_lookback": 3, **kw}


def test_rule_from_settings_stays_v1_without_rule_arguments_and_adds_v2_keys_with_them():
    legacy = ps.rule_from_settings(S, 5)
    assert set(legacy) == {"stop_atr_mult", "target_r_multiple", "max_stop_pct", "min_reward_risk"}
    v2 = ps.rule_from_settings(S, 21, stop_rule="range_low", target_rule="price", target_param=110.0)
    assert (v2["stop_rule"], v2["target_rule"], v2["target_param"], v2["range_lookback"]) == ("range_low", "price", 110.0, 10)
    assert v2["min_stop_pct"] == 0.0005 and v2["min_stop_atr_frac"] == 0.75


def test_rule_for_plan_uses_the_stored_rule_and_v1_rows_stay_on_the_v1_path():
    v1 = ps.rule_for_plan({"rules_version": "trade-plan-v1", "stop_rule": "atr"}, S, 5)
    assert "stop_rule" not in v1
    v2 = ps.rule_for_plan({"rules_version": "trade-plan-v2", "stop_rule": "range_low", "stop_param": 7.0,
                           "target_rule": "r_multiple", "target_param": 2.0}, S, 5)
    assert (v2["stop_rule"], v2["stop_param"], v2["target_param"]) == ("range_low", 7.0, 2.0)


def test_v2_default_replay_matches_v1_replay_on_a_random_walk():
    rng = np.random.default_rng(3)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.012, 400)))
    bars = _bars(close, up=0.006, down=0.006)
    for h in (5, 21):
        a = ps.historical_replay(bars, ps.rule_from_settings(S, h), h)
        b = ps.historical_replay(bars, ps.rule_from_settings(S, h, stop_rule="atr", target_rule="r_multiple"), h)
        assert a.n > 100 and abs(a.n - b.n) <= max(2, 0.03 * a.n)
        assert abs(a.hits_target - b.hits_target) <= 0.05 * a.n and abs(a.avg_r - b.avg_r) < 0.1


def test_replay_range_low_hand_checked():
    # lows rise 0.5/day from 90, so the N-bar low sits below every later low and the stop is never touched; highs never reach 2R
    idx = np.arange(20)
    close = np.full(20, 100.0)
    bars = _bars(close).assign(High=100.5, Low=90.0 + 0.5 * idx)
    r = ps.historical_replay(bars, _rule("range_low", 3), 2)
    valid_days = range(2, 18)                                    # a 3-bar window needs bars d-2..d; two bars must follow
    assert (r.n, r.hits_stop, r.hits_target, r.timeouts) == (len(valid_days), 0, 0, len(valid_days))
    assert r.avg_r == pytest.approx(0.0)
    # now a spike to 130 on bar 12: any day whose 2-bar horizon contains it hits the 2R target (risk 10 - 0.5*(d-2))
    spiked = bars.copy()
    spiked.iloc[12, spiked.columns.get_loc("High")] = 130.0
    r2 = ps.historical_replay(spiked, _rule("range_low", 3), 2)
    assert r2.n == r.n and r2.hits_target == 2 and r2.hits_stop == 0     # days 10 and 11 see bar 12


def test_replay_range_mid_uses_the_midpoint_and_skips_days_where_it_is_not_below_entry():
    idx = np.arange(20)
    bars = _bars(np.full(20, 100.0)).assign(High=100.5, Low=90.0 + 0.5 * idx)
    r = ps.historical_replay(bars, _rule("range_mid", 3), 2)          # mid = (100.5 + low)/2 = 95.25..99: below entry 100 -> valid
    # ...and above the next day's rising low every time (unlike range_low, whose stop sits at the low itself): all 16 days stop out
    assert (r.n, r.hits_stop, r.hits_target) == (16, 16, 0) and r.avg_r == pytest.approx(-1.0)
    high_bars = bars.assign(High=130.0)                                # midpoint (130 + low)/2 > 100 -> stop not below entry
    assert ps.historical_replay(high_bars, _rule("range_mid", 3), 2).n == 0


def test_replay_pct_and_price_rules_are_fixed_levels():
    flat = _bars(np.full(20, 100.0), up=0.01, down=0.01)              # 99..101 every day
    flat.iloc[10, flat.columns.get_loc("High")] = 110.0
    pct = ps.historical_replay(flat, _rule("pct", 0.05, target_rule="r_multiple", target_param=2.0), 2)   # stop 95, target 110
    assert pct.n == 18 and pct.hits_target == 2 and pct.hits_stop == 0
    price = ps.historical_replay(flat, _rule("price", 95.0, target_rule="price", target_param=109.0), 2)  # fixed $95 / $109
    assert price.n == 18 and price.hits_target == 2 and price.hits_stop == 0
    low_target = ps.historical_replay(flat, _rule("price", 95.0, target_rule="price", target_param=99.0), 2)
    assert low_target.n == 0                                           # a target at/below entry is never a valid day
    too_tight = ps.historical_replay(flat, _rule("pct", 0.00001), 2)
    assert too_tight.n == 0                                            # inside the 0.05% floor


@pytest.mark.parametrize("rule", [_rule("range_low", 4), _rule("range_mid", 4), _rule("atr", 1.5), _rule("pct", 0.04)])
def test_v2_levels_at_each_bar_depend_only_on_bars_up_to_it(rule):
    rng = np.random.default_rng(11)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 80)))
    bars = _bars(close, up=0.008, down=0.008)
    hi, lo, cl = (np.asarray(bars[c], dtype=float) for c in ("High", "Low", "Close"))
    full = ps._levels_v2(hi, lo, cl, ps._atr_series(hi, lo, cl), rule)
    for i in (20, 40, 79):
        part = ps._levels_v2(hi[: i + 1], lo[: i + 1], cl[: i + 1], ps._atr_series(hi[: i + 1], lo[: i + 1], cl[: i + 1]), rule)
        for a, b in zip(full, part):
            assert np.array_equal(a[i : i + 1], b[i : i + 1], equal_nan=True) if a.dtype != bool else a[i] == b[i]


def test_simulate_accepts_price_plans_as_fixed_prices_and_refuses_protective_ones():
    rng = np.random.default_rng(5)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 400)))
    bars = _bars(close, up=0.006, down=0.006)
    p = plan(close[-1].round(2), 2.0, 5, stop_rule="price", stop_param=float(close[-1] * 0.96), target_rule="price",
             target_param=float(close[-1] * 1.07))
    sim = SimpleNamespace(**{**p.to_dict(), "horizon_days": 5})
    r = ps.simulate(bars, sim, 2_000, 5, 1)
    assert 0 <= r.p_target_first <= 1 and r.p_target_first + r.p_stop_first + r.p_timeout == pytest.approx(1.0)
    prot = SimpleNamespace(**{**tp.build_protective_plan(100.0, 2.0, 5, "DOWN", S, as_of_date=AS_OF).to_dict(), "horizon_days": 5})
    with pytest.raises(ps.NotSimulated):
        ps.simulate(bars, prot, 100, 5, 1)


# ══ Owner-scoped plans: service, audit, privacy ═══════════════════════════════

import inspect                                                        # noqa: E402

from portfolio_agent.services import investor_service as svc          # noqa: E402
from portfolio_agent.services.account_service import add_position, create_account   # noqa: E402
from portfolio_agent.services.context import RequestContext, _mint_identity_for_tests, local_context   # noqa: E402
from portfolio_agent.tools import custom_plan_db as cpdb              # noqa: E402
from portfolio_agent.tools import holdings_db as repo                 # noqa: E402

ALICE = local_context("test")
BOB = RequestContext(identity=_mint_identity_for_tests("bob"), source="test")
FLAT_OHLC = lambda t, d: _ohlc(False)                                 # noqa: E731  ATR 2.0, last close 100


def _hold(ctx, ticker, shares=10):
    acct = create_account(ctx, broker="fidelity", account_number=f"{ctx.actor}-1", display_name="Main", account_type="TAXABLE")
    add_position(ctx, acct["account_id"], {"ticker": ticker, "shares": shares, "avg_cost": 90.0, "cost_basis_total": 90.0 * shares,
                                           "account_name": "Main", "account_type": "TAXABLE"}, as_of_date="2026-09-01")


def _ops():
    return [e["operation"] for e in repo.get_audit_events()]


def test_custom_long_plan_uses_the_users_own_prices_and_is_audited_and_superseded():
    pid = _shared("AAPL")
    row = svc.create_custom_plan(ALICE, pid, "price", 96.5, "price", 108.0, fetch_history=FLAT_OHLC)
    assert (row["kind"], row["owner"], row["plan_status"], row["rules_version"]) == ("custom_long", ALICE.actor, "planned", "trade-plan-v2")
    assert (row["entry"], row["stop"], row["target"], row["risk_per_share"]) == (100.0, 96.5, 108.0, 3.5)
    assert row["reward_risk"] == pytest.approx(8.0 / 3.5, abs=1e-4) and row["created_by"] == ALICE.actor
    assert (row["stop_rule"], row["stop_param"], row["target_rule"], row["target_param"]) == ("price", 96.5, "price", 108.0)
    second = svc.create_custom_plan(ALICE, pid, "pct", 0.05, fetch_history=FLAT_OHLC, reason="widen")
    assert second["id"] != row["id"] and second["stop"] == 95.0 and second["target_rule"] == "r_multiple"
    hist = cpdb.get_history(ALICE.actor, pid)
    assert [h["id"] for h in hist] == [row["id"], second["id"]]
    assert hist[0]["superseded_by"] == second["id"] and hist[1]["superseded_by"] is None
    assert hist[0]["stop"] == 96.5                                    # history is never rewritten
    events = [e for e in repo.get_audit_events() if e["operation"] == "create_custom_plan"]
    assert len(events) == 2 and events[0]["reason"] == "widen"
    assert events[0]["before"]["stop"] == 96.5 and events[0]["after"]["stop"] == 95.0     # newest first: before = the plan it replaced
    assert events[1]["before"] is None and events[1]["after"]["stop"] == 96.5


def test_a_refused_custom_plan_is_recorded_with_its_reason_and_message():
    pid = _shared("AAPL")
    row = svc.create_custom_plan(ALICE, pid, "price", 99.99, fetch_history=FLAT_OHLC)
    assert (row["plan_status"], row["no_plan_reason"], row["stop"]) == ("no_plan", "too_tight", None)
    assert "Widen it" in row["message"] and "$0.01" in row["message"]
    ok = svc.create_custom_plan(ALICE, pid, "price", 96.0, fetch_history=FLAT_OHLC)
    assert ok["plan_status"] == "planned" and cpdb.get_history(ALICE.actor, pid)[0]["superseded_by"] == ok["id"]


def test_custom_plans_validate_their_inputs():
    pid = _shared("AAPL")
    with pytest.raises(ValueError):
        svc.create_custom_plan(ALICE, pid, "bogus", 1.0, fetch_history=FLAT_OHLC)
    with pytest.raises(ValueError):
        svc.create_custom_plan(ALICE, pid, "pct", 0.05, "bogus", fetch_history=FLAT_OHLC)
    with pytest.raises(ValueError):
        svc.create_custom_plan(ALICE, 99999, "pct", 0.05, fetch_history=FLAT_OHLC)           # no such forecast
    with pytest.raises(ValueError):
        svc.create_custom_plan(ALICE, _shared("MSFT", direction="FLAT"), "pct", 0.05, fetch_history=FLAT_OHLC)
    with pytest.raises(Exception):
        svc.create_custom_plan(None, pid, "pct", 0.05, fetch_history=FLAT_OHLC)             # needs a verified context


def test_a_private_forecast_cannot_carry_a_custom_plan():
    from portfolio_agent.tools.forecast_writer import ForecastProvenance, write_private_forecast
    res = write_private_forecast(ALICE.actor, ForecastProvenance(origin="chat.ticker_prediction", model_used="x:y"), ticker="AAPL",
                                 prediction="BULLISH", recommendation="BUY", horizon_days=5, predicted_direction="UP",
                                 predicted_return_high=3.0, start_price=100.0)
    with pytest.raises(ValueError):
        svc.create_custom_plan(ALICE, res["id"], "pct", 0.05, fetch_history=FLAT_OHLC)


def test_custom_plans_are_invisible_to_other_owners():
    pid = _shared("AAPL")
    svc.create_custom_plan(ALICE, pid, "price", 96.5, "price", 108.0, fetch_history=FLAT_OHLC)
    assert svc.get_custom_plans(BOB, [pid]) == {} and cpdb.get_active("bob", [pid]) == {} and cpdb.get_history("bob", pid) == []
    assert svc.get_custom_plans(ALICE, [pid])[pid]["custom_long"]["stop"] == 96.5
    with pytest.raises(ValueError):
        cpdb.get_active("", [pid])                                    # there is no un-owned read
    # bob writing his own plan neither shows alice's nor supersedes it
    svc.create_custom_plan(BOB, pid, "pct", 0.04, fetch_history=FLAT_OHLC)
    assert svc.get_custom_plans(ALICE, [pid])[pid]["custom_long"]["stop"] == 96.5
    assert svc.get_custom_plans(BOB, [pid])[pid]["custom_long"]["stop"] == 96.0


def test_the_shared_plan_never_changes_and_never_carries_owner_data():
    _store_snapshot("AAPL", AS_OF, 2.0)
    pid = _shared("AAPL")
    shared = tpdb.create_plan_for_prediction(pid)
    svc.create_custom_plan(ALICE, pid, "price", 96.5, "price", 108.0, fetch_history=FLAT_OHLC)
    assert tpdb.get_plan(pid) == shared and shared["stop"] == 97.0
    assert not {"owner", "created_by", "kind", "trim_level"} & set(shared)


def test_composer_uses_the_owners_custom_plan_for_sizing_and_only_for_them(monkeypatch):
    from portfolio_agent.tools import decision_composer as dc
    from portfolio_agent.tools import trade_plan_db  # noqa: F401
    _store_snapshot("AAPL", AS_OF, 2.0)
    pid = _shared("AAPL")
    tpdb.create_plan_for_prediction(pid)
    svc.create_custom_plan(ALICE, pid, "price", 92.0, "price", 130.0, fetch_history=FLAT_OHLC)          # risk $8 (the 5d max), not the shared $3
    monkeypatch.setattr(dc, "plan_sizing", lambda ctx, plan, **kw: {"risk_per_share": (plan or {}).get("risk_per_share")})
    monkeypatch.setattr("portfolio_agent.tools.portfolio_assessment.build_portfolio_assessment",
                        lambda ctx, t: SimpleNamespace(exposures={"total_value": 100000.0}, fit={"status": "OK"}, limits=[], sizing={}, built_at="x"))
    mine = dc._portfolio_fit(ALICE, "AAPL", tpdb.get_plan(pid))
    theirs = dc._portfolio_fit(BOB, "AAPL", tpdb.get_plan(pid))
    assert mine.custom_plan["stop"] == 92.0 and mine.plan_sizing == {"risk_per_share": 8.0}
    assert theirs.custom_plan is None and theirs.plan_sizing == {"risk_per_share": 3.0}


# ── protective exits ──────────────────────────────────────────────────────────

def test_protective_plan_only_for_a_holder_and_never_for_a_non_holder():
    pid = _shared("MSFT", direction="DOWN", ret_high=2.0)
    assert svc.get_protective_plan(ALICE, pid, fetch_history=FLAT_OHLC) is None          # no holding, no plan
    with pytest.raises(svc.NotHeld):
        svc.create_custom_plan(ALICE, pid, "pct", 0.05, fetch_history=FLAT_OHLC)
    _hold(ALICE, "MSFT")
    plan_ = svc.get_protective_plan(ALICE, pid, fetch_history=FLAT_OHLC)
    assert plan_["source"] == "default" and plan_["plan_status"] == "planned" and plan_["kind"] == "protective"
    assert (plan_["stop"], plan_["entry"], plan_["target"], plan_["reward_risk"]) == (97.0, 100.0, None, None)
    assert plan_["trim_level"] == 102.0                                                   # 100 * (1 + 2%) is above entry
    assert svc.get_protective_plan(BOB, pid, fetch_history=FLAT_OHLC) is None             # alice holds it; bob does not
    assert cpdb.get_active(ALICE.actor, [pid]) == {}                                      # the default plan is read-time only, not stored
    assert _ops().count("create_custom_plan") == 0


def test_protective_trim_level_is_absent_when_not_above_entry():
    pid = _shared("MSFT", direction="DOWN", ret_high=-1.0)
    _hold(ALICE, "MSFT")
    assert svc.get_protective_plan(ALICE, pid, fetch_history=FLAT_OHLC)["trim_level"] is None


def test_a_users_own_protective_stop_is_stored_per_owner_and_has_no_target():
    pid = _shared("MSFT", direction="DOWN", ret_high=2.0)
    _hold(ALICE, "MSFT")
    with pytest.raises(ValueError):
        svc.create_custom_plan(ALICE, pid, "pct", 0.05, "price", 90.0, fetch_history=FLAT_OHLC)   # a protective exit has no target
    row = svc.create_custom_plan(ALICE, pid, "pct", 0.05, fetch_history=FLAT_OHLC)
    assert (row["kind"], row["stop"], row["target"], row["target_rule"], row["plan_status"]) == ("protective", 95.0, None, None, "planned")
    got = svc.get_protective_plan(ALICE, pid, fetch_history=FLAT_OHLC)
    assert got["source"] == "custom" and got["stop"] == 95.0
    assert svc.get_protective_plan(BOB, pid, fetch_history=FLAT_OHLC) is None and svc.get_custom_plans(BOB, [pid]) == {}
    assert "create_custom_plan" in _ops()


def test_a_down_forecast_never_yields_a_short_plan():
    pid = _shared("MSFT", direction="DOWN")
    _hold(ALICE, "MSFT")
    got = svc.get_protective_plan(ALICE, pid, fetch_history=FLAT_OHLC)
    assert got["target"] is None and got["reward_risk"] is None
    assert got["stop"] < got["entry"]                                                     # a stop BELOW the position: an exit, never a short entry
    assert "never plans a short" in tp.NoPlan("down").message


# ── boundary: the shared path never touches the private table ─────────────────

@pytest.mark.parametrize("modname", [
    "portfolio_agent.tools.trade_plan", "portfolio_agent.tools.trade_plan_db", "portfolio_agent.tools.plan_simulator",
    "portfolio_agent.tools.plan_sim_db", "portfolio_agent.tools.forecast_writer", "portfolio_agent.tools.market_context",
    "portfolio_agent.tools.instrument_reference", "portfolio_agent.pipeline.daily.apex",
])
def test_shared_forecast_path_never_imports_or_reads_custom_plans(modname):
    import importlib
    src = inspect.getsource(importlib.import_module(modname))
    assert "custom_plan_db" not in src and "custom_trade_plans" not in src


def test_prediction_db_only_creates_the_private_table_and_never_reads_it():
    src = inspect.getsource(pdb)
    assert "custom_plan_db" not in src and "FROM custom_trade_plans" not in src and "INTO custom_trade_plans" not in src


def test_only_the_service_imports_the_private_store():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / "portfolio_agent"
    users = sorted(str(p.relative_to(root)) for p in root.rglob("*.py")
                   if "custom_plan_db" in p.read_text() and p.name != "custom_plan_db.py")
    assert users == ["services/investor_service.py"], users


# ══ UI helpers ════════════════════════════════════════════════════════════════

def test_rule_words_and_the_rule_line():
    from web.components.trade_plan_view import plan_block_html, stop_rule_words, target_rule_words
    assert stop_rule_words({"stop_rule": "range_low", "stop_param": 10}) == "10-day range low"
    assert stop_rule_words({"stop_rule": "range_mid", "stop_param": 5.0}) == "5-day range midpoint"
    assert stop_rule_words({"stop_rule": "pct", "stop_param": 0.05}) == "5% below entry"
    assert stop_rule_words({"stop_rule": "atr", "stop_param": 1.5}) == "1.5× ATR14 below entry"
    assert stop_rule_words({"stop_rule": "price", "stop_param": 96.5}) == "your price"
    assert stop_rule_words({"stop_rule": "atr", "stop_param": None}) == "ATR stop"          # a row from before v2
    assert target_rule_words({"target_rule": "r_multiple", "target_param": 2.0}) == "2× the risk above entry"
    assert target_rule_words({"target_rule": "price", "target_param": 110.0}) == "your price"
    row = {"plan_status": "planned", "entry": 190.0, "stop": 182.4, "target": 205.2, "reward_risk": 2.0, "time_stop_date": "2026-11-24",
           "stop_rule": "range_low", "stop_param": 10, "target_rule": "r_multiple", "target_param": 2.0, "rules_version": "trade-plan-v2"}
    h = plan_block_html(row, horizon_days=21)
    assert "Stop: 10-day range low, $182.40, rounded down" in h and "Target: 2× the risk above entry, $205.20" in h and "TRADE PLAN" in h
    old = plan_block_html({**row, "rules_version": "trade-plan-v1", "stop_rule": "atr", "stop_param": None})
    assert "rounded down" not in old and "ATR stop" in old
    assert "YOUR PLAN" in plan_block_html(row, title="YOUR PLAN")


def test_no_plan_messages_in_the_block():
    from web.components.trade_plan_view import plan_block_html
    assert "inside half a day" in plan_block_html({"plan_status": "no_plan", "no_plan_reason": "too_tight",
                                                   "message": "The stop is $0.42 under the entry, inside half a day's typical move ($0.61). Widen it."})
    assert "too close to the entry" in plan_block_html({"plan_status": "no_plan", "no_plan_reason": "too_tight"})
    assert "FLAT: no directional trade plan" in plan_block_html({"plan_status": "no_plan", "no_plan_reason": "flat"})
    assert "never plans a short" in plan_block_html({"plan_status": "no_plan", "no_plan_reason": "down"})
    assert "not UP" in plan_block_html({"plan_status": "no_plan", "no_plan_reason": "not_up"})            # old v1 rows still read
    assert "Your plan was not created: " in plan_block_html({"plan_status": "no_plan", "no_plan_reason": "too_tight"}, title="YOUR PLAN")


def test_protective_block_wording():
    from web.components.trade_plan_view import protective_block_html
    p = tp.build_protective_plan(100.0, 2.0, 5, "DOWN", S, as_of_date=AS_OF, predicted_return_high=0.02).to_dict()
    h = protective_block_html({**p, "source": "default"})
    for needle in ("PROTECTIVE EXIT", "a forecast of DOWN, not a short", "$97.00", "$102.00", "model's number, display only", "Not simulated",
                   "nothing here is a short sale", "default rule"):
        assert needle in h
    assert "Target" not in h and "TRADE PLAN" not in h
    assert "your own stop" in protective_block_html({**p, "source": "custom"})
    assert "No trim" not in h and "Model's trim level" not in protective_block_html(
        {**tp.build_protective_plan(100.0, 2.0, 5, "DOWN", S, as_of_date=AS_OF, predicted_return_high=-0.01).to_dict(), "source": "default"})
    refused = protective_block_html({"plan_status": "no_plan", "no_plan_reason": "too_tight", "message": "The stop is $0.10 under. Widen it."})
    assert "No protective exit" in refused and "Widen it" in refused
    assert protective_block_html(None) == ""


def test_form_values_become_service_arguments():
    from web.components.trade_plan_view import to_service_args
    assert to_service_args("pct", 5.0, "r_multiple", 2.0) == {"stop_rule": "pct", "stop_param": 0.05, "target_rule": "r_multiple", "target_param": 2.0}
    assert to_service_args("range_low", 10.0, None, None)["stop_param"] == 10
    assert to_service_args("price", 96.5, "price", 108.0) == {"stop_rule": "price", "stop_param": 96.5, "target_rule": "price", "target_param": 108.0}


def test_owner_plan_html_shows_only_the_viewers_own_plans():
    from web.components.trade_plan_view import owner_plan_html
    up = _shared("AAPL")
    svc.create_custom_plan(ALICE, up, "price", 96.5, "price", 108.0, fetch_history=FLAT_OHLC)
    assert "YOUR PLAN" in owner_plan_html(ALICE, up, "UP", None) and "$96.50" in owner_plan_html(ALICE, up, "UP", None)
    assert owner_plan_html(BOB, up, "UP", None) == ""
    down = _shared("MSFT", direction="DOWN", ret_high=2.0)
    assert owner_plan_html(ALICE, down, "DOWN", None, fetch_history=FLAT_OHLC) == ""          # not held -> nothing, not even a refusal
    _hold(ALICE, "MSFT")
    assert "PROTECTIVE EXIT" in owner_plan_html(ALICE, down, "DOWN", None, fetch_history=FLAT_OHLC)
    assert owner_plan_html(BOB, down, "DOWN", None, fetch_history=FLAT_OHLC) == ""


def test_the_chat_and_admin_modules_import_and_the_admin_form_round_trips_the_new_settings():
    import web.chat.rendering  # noqa: F401
    import web.components.investor_profile  # noqa: F401
    patch = {"plan_min_stop_atr_frac": {5: 0.6, 21: 0.8, 63: 1.1}, "plan_range_lookback_days": {5: 6, 21: 11, 63: 21},
             "plan_min_stop_pct": 0.0007, "plan_default_stop_rule": "range_low"}
    esdb.update_engine_settings(updated_by="t", trade_plan=patch)
    got = esdb.get_trade_plan_settings()
    assert all(got[k] == v for k, v in patch.items())
