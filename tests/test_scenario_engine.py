"""Deterministic scenario engine: accounting identities, rounding order, per-account cash, explanations."""
from __future__ import annotations

from decimal import Decimal as X

import pytest

from portfolio_agent.scenarios.engine import (
    Assumptions, Limits, Position, ScenarioInputs, Trade, evaluate, D,
    STATUS_FEASIBLE, STATUS_INFEASIBLE, STATUS_NOT_EVALUABLE,
)

LIMITS = Limits(max_sector=X("0.25"), max_issuer=X("0.15"), max_per_trade_of_contribution=X("0.4"),
                max_post_trade_position=X("0.15"))


def _inputs(**over) -> ScenarioInputs:
    base = dict(
        scope="all", account_ids=(1, 2), account_labels={1: "Taxable", 2: "IRA"},
        positions=(Position(1, "AAPL", X("10"), "Tech"), Position(1, "XOM", X("100"), "Energy"),
                   Position(2, "VTI", X("50"), "Broad")),
        prices={"AAPL": X("200"), "XOM": X("100"), "VTI": X("250"), "MSFT": X("400.50")},
        price_as_of="2026-09-22",
        starting_cash={1: X("1000"), 2: X("500")},
        external_contribution={1: X("5000"), 2: X("0")},
        limits=LIMITS,
        sector_by_ticker={"AAPL": "Tech", "XOM": "Energy", "VTI": "Broad", "MSFT": "Tech"},
        expected_returns={"AAPL": X("2.0"), "XOM": X("-1.0"), "VTI": X("1.0"), "MSFT": X("3.0")},
        versions={"holdings": "h1", "predictions": "p1", "profile": "u1"},
    )
    base.update(over)
    return ScenarioInputs(**base)


def _c(result, name, account_id=None):
    return [c for c in result.constraints if c.name == name and (account_id is None or c.account_id == account_id)]


def test_accounting_identities_hold_to_the_cent_with_fees_and_both_sides():
    inp = _inputs(assumptions=Assumptions(fee_per_trade=X("1.25")))
    res = evaluate(inp, [
        Trade(1, "MSFT", "BUY", amount=X("1999.99"), source="user"),     # 4 whole shares = 1602.00
        Trade(1, "XOM", "SELL", quantity=X("10"), source="user"),        # +1000.00
        Trade(2, "VTI", "SELL", amount=X("300"), source="user"),         # 1.2 shares = 300.00
    ])
    assert res.status == STATUS_FEASIBLE, [c.to_dict() for c in res.constraints if c.status != "pass"]
    t = res.accounting["total"]
    # starting value = 2000+10000+12500 + 1500 cash = 26000; ending = 26000 + 5000 − 3.75
    assert t["starting_portfolio_value"] == "26000.00" and t["ending_portfolio_value"] == "30996.25"
    assert res.accounting["identity_residual"] == {"cash": "0.00", "portfolio_value": "0.00"}
    a1 = res.accounting["accounts"]["1"]
    assert (a1["purchase_cost"], a1["sale_proceeds"], a1["fees"], a1["ending_cash"]) == ("1602.00", "1000.00", "2.50", "5395.50")
    assert res.accounting["accounts"]["2"]["ending_cash"] == "798.75"
    assert res.after["accounts"]["1"]["positions"]["MSFT"]["shares"] == "4.0000"
    assert res.after["accounts"]["2"]["positions"]["VTI"]["shares"] == "48.8000"


def test_buys_round_down_to_whole_shares_and_zero_share_buys_are_rejected():
    res = evaluate(_inputs(), [Trade(1, "MSFT", "BUY", amount=X("399.99"), source="user")])
    assert res.status == STATUS_INFEASIBLE and res.trades == []
    assert "rounds down to zero shares" in res.rejected[0].reason
    res = evaluate(_inputs(), [Trade(1, "MSFT", "BUY", quantity=X("2.9"), source="user")])
    assert res.trades[0].quantity == X("2") and res.trades[0].gross == X("801.00")


def test_overselling_and_wrong_account_are_infeasible_and_explained():
    res = evaluate(_inputs(), [Trade(1, "XOM", "SELL", quantity=X("101"), source="user"),
                               Trade(1, "VTI", "SELL", quantity=X("1"), source="user"),
                               Trade(9, "AAPL", "BUY", quantity=X("1"), source="user")])
    assert res.status == STATUS_INFEASIBLE
    reasons = [r.reason for r in res.rejected]
    assert any("exceeds the 100.0000 held" in r for r in reasons)
    assert any("exceeds the 0.0000 held" in r for r in reasons)          # VTI is in the IRA, not taxable
    assert any("not in this scenario's scope" in r for r in reasons)


def test_cash_is_not_pooled_across_accounts():
    # IRA has 500 cash and no contribution; buying 3 MSFT there overdraws it even though Taxable has plenty.
    res = evaluate(_inputs(), [Trade(2, "MSFT", "BUY", quantity=X("3"), source="user")])
    assert res.status == STATUS_INFEASIBLE
    c = _c(res, "cash_non_negative", 2)[0]
    assert c.status == "fail" and "IRA: ending cash would be -701.50" in c.detail
    assert _c(res, "cash_non_negative", 1)[0].status == "pass"


def test_unknown_starting_cash_is_not_silently_ignored():
    loose = Limits(X("0.25"), X("0.15"), X("1"), X("0.15"))      # per-trade cap out of the way for this test
    inp = _inputs(starting_cash={1: None, 2: None}, external_contribution={1: X("1000"), 2: X("0")}, limits=loose)
    # Covered by the contribution → pass under a stated assumption.
    res = evaluate(inp, [Trade(1, "MSFT", "BUY", quantity=X("2"), source="user")])          # 801.00 ≤ 1000
    c = _c(res, "cash_non_negative", 1)[0]
    assert c.status == "pass" and "starting cash unknown" in c.detail and res.status == STATUS_FEASIBLE
    # Not covered (no new money at all, starting cash unknown) → not evaluable, never a silent pass.
    inp = _inputs(starting_cash={1: None, 2: None}, external_contribution={1: X("0"), 2: X("0")}, limits=loose)
    res = evaluate(inp, [Trade(1, "MSFT", "BUY", quantity=X("3"), source="user")])          # needs 1201.50 of unknown cash
    assert res.status == STATUS_NOT_EVALUABLE
    assert _c(res, "cash_non_negative", 1)[0].status == "not_evaluable"
    assert res.accounting["total"]["starting_cash_known_for_all_accounts"] is False


def test_restricted_and_excluded_sector_buys_are_rejected_with_reasons():
    inp = _inputs(restricted=frozenset({"MSFT"}), excluded_sectors=frozenset({"Energy"}),
                  prices={**_inputs().prices, "CVX": X("150")}, sector_by_ticker={**_inputs().sector_by_ticker, "CVX": "Energy"})
    res = evaluate(inp, [Trade(1, "MSFT", "BUY", quantity=X("1"), source="user"),
                         Trade(1, "CVX", "BUY", quantity=X("1"), source="user"),
                         Trade(1, "XOM", "SELL", quantity=X("1"), source="user")])   # selling an excluded sector is fine
    assert [r.reason for r in res.rejected] == ["MSFT is on the restricted list", "CVX is in an excluded sector (Energy)"]
    assert len(res.trades) == 1 and res.status == STATUS_INFEASIBLE


def test_limits_are_checked_after_rounding_and_only_blame_groups_that_received_buys():
    # XOM is already 43% of the book — untouched, that is a warning, not a failure.
    res = evaluate(_inputs(), [Trade(1, "MSFT", "BUY", quantity=X("2"), source="user")])
    assert res.status == STATUS_FEASIBLE
    assert any("position XOM" in w and "untouched" in w for w in res.warnings)
    # Buying enough MSFT to exceed 15% of the post-trade book fails position_limit and (Tech) sector stays < 25%.
    res = evaluate(_inputs(external_contribution={1: X("20000"), 2: X("0")}),
                   [Trade(1, "MSFT", "BUY", amount=X("7000"), source="user")])       # 17 sh = 6808.50 of 46000 = 14.8%
    assert _c(res, "position_limit")[0].status == "pass"                             # rounding down kept it under
    res = evaluate(_inputs(external_contribution={1: X("20000"), 2: X("0")}),
                   [Trade(1, "MSFT", "BUY", amount=X("7300"), source="user")])       # 18 sh = 7209.00 = 15.7%
    pl = _c(res, "position_limit")[0]
    assert pl.status == "fail" and pl.ticker == "MSFT" and res.status == STATUS_INFEASIBLE
    assert _c(res, "per_trade_cap", 1)[0].status == "pass"


def test_per_trade_cap_is_relative_to_the_accounts_contribution():
    res = evaluate(_inputs(), [Trade(1, "MSFT", "BUY", amount=X("2500"), source="user")])   # 6 sh = 2403 > 40% of 5000
    c = _c(res, "per_trade_cap", 1)[0]
    assert c.status == "fail" and "exceeds 40.00% of the 5000.00 contribution" in c.detail


def test_missing_price_or_sector_makes_the_result_not_evaluable():
    res = evaluate(_inputs(), [Trade(1, "NVDA", "BUY", quantity=X("1"), source="user")])
    assert res.status == STATUS_NOT_EVALUABLE and res.rejected[0].kind == "not_evaluable"
    inp = _inputs(sector_by_ticker={"AAPL": "Tech", "XOM": "Energy", "VTI": "Broad"})   # MSFT sector unknown
    res = evaluate(inp, [Trade(1, "MSFT", "BUY", quantity=X("1"), source="user")])
    assert res.status == STATUS_NOT_EVALUABLE
    assert _c(res, "sector_limit")[0].status == "not_evaluable" and "MSFT" in _c(res, "sector_limit")[0].detail
    inp = _inputs(prices={"AAPL": X("200"), "XOM": X("100"), "MSFT": X("400.50")})       # held VTI unpriced
    res = evaluate(inp, [])
    assert _c(res, "prices_available")[0].status == "not_evaluable" and res.status == STATUS_NOT_EVALUABLE


def test_proposed_rejections_do_not_flip_status_but_user_ones_do():
    trades = [Trade(1, "XOM", "SELL", quantity=X("500"), source="proposed"), Trade(1, "MSFT", "BUY", quantity=X("1"), source="proposed")]
    res = evaluate(_inputs(), trades)
    assert res.status == STATUS_FEASIBLE and len(res.rejected) == 1 and len(res.trades) == 1
    res = evaluate(_inputs(), [Trade(1, "XOM", "SELL", quantity=X("500"), source="user")])
    assert res.status == STATUS_INFEASIBLE


def test_risk_estimates_are_labelled_and_move_with_the_book():
    res = evaluate(_inputs(), [Trade(1, "MSFT", "BUY", quantity=X("5"), source="user")])
    assert res.risk["labels"]["expected_return_pct"] == {"type": "estimate", "horizon_days": 21,
                                                         "model": "apex-latest-prediction", "prediction_version": "p1"}
    assert res.risk["before"]["top_position"] == "VTI" and res.risk["after"]["positions"] == 4
    assert X(res.risk["after"]["expected_return_pct"]) > X(res.risk["before"]["expected_return_pct"])
    assert res.risk["before"]["expected_return_coverage_pct"] == "100.00"


def test_same_inputs_give_the_same_result_and_digest():
    a, b = _inputs(), _inputs()
    assert a.digest() == b.digest()
    trades = [Trade(1, "MSFT", "BUY", amount=X("1000"), source="user")]
    assert evaluate(a, trades).to_dict() == evaluate(b, trades).to_dict()
    assert _inputs(prices={**a.prices, "MSFT": X("401")}).digest() != a.digest()


def test_limits_from_profile_normalise_mixed_units():
    class P:  # user_profile stores percentages for the first two and fractions for the other two
        max_sector_pct = 25.0; max_issuer_pct = 15.0; max_per_candidate_pct_of_cash = 0.4; max_post_trade_position_pct = 0.15
    assert Limits.from_profile(P()) == LIMITS
    assert D(0.1) == X("0.1") and D("0.1") == X("0.1")


def test_propose_then_evaluate_without_io():
    """The proposer works from the prepared context alone and its output survives the engine's rounding."""
    from portfolio_agent.scenarios.propose import propose_trades
    inp = _inputs(external_contribution={1: X("10000"), 2: X("0")}, starting_cash={1: X("0"), 2: X("0")})
    preds = {"AAPL": {"recommendation": "STRONG_BUY", "composite_score": 9.0, "reasoning_text": "great"},
             "XOM": {"recommendation": "SELL", "composite_score": 3.0, "reasoning_text": "weak"}}
    ctx = {"consolidated": [{"ticker": "AAPL", "shares": 10.0, "sector": "Tech"}, {"ticker": "XOM", "shares": 100.0, "sector": "Energy"},
                            {"ticker": "VTI", "shares": 50.0, "sector": "Broad"}],
           "baseline": {"total_value": 24500.0}, "risk_ctx": {}, "latest_preds": preds,
           "opportunities": [{"ticker": "MSFT", "call": "BUY", "why": "momentum", "composite_score": 8.0,
                              "recommendation": "BUY", "portfolio_impact": {"candidate_price": 400.5}}],
           "funding_account_id": 1}
    trades = propose_trades(inp, ctx, top_n=5)
    kinds = {(t.side, t.ticker) for t in trades}
    assert ("SELL", "XOM") in kinds and ("BUY", "AAPL") in kinds and ("BUY", "MSFT") in kinds
    assert all(t.source == "proposed" for t in trades) and all(t.account_id == 1 for t in trades)
    res = evaluate(inp, trades)
    assert res.status in (STATUS_FEASIBLE, STATUS_INFEASIBLE)   # never silently "not evaluable"
    for et in res.trades:
        if et.trade.side == "BUY":
            assert et.quantity == et.quantity.to_integral_value() and et.gross <= et.trade.amount
    assert res.accounting["identity_residual"] == {"cash": "0.00", "portfolio_value": "0.00"}
