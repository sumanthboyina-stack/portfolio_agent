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
