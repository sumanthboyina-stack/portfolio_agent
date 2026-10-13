from web.data.wealth import build_wealth_overview


def test_overview_aggregates_tracked_assets_by_account_without_guessing_unknown_values():
    result = build_wealth_overview(
        accounts=[
            {"account_id": 1, "display_name": "Brokerage"},
            {"account_id": 2, "display_name": "IRA"},
        ],
        holdings=[
            {"account_id": 1, "current_value": 1200.0, "cost_basis_total": 1000.0},
            {"account_id": 1, "current_value": None, "cost_basis_total": 900.0},
            {"account_id": 2, "current_value": 800.0, "cost_basis_total": 800.0},
        ],
        cash_balances=[{"account_id": 1, "amount": 50.0}],
        goals=[
            {"status": "ACTIVE", "target_amount": 5000.0},
            {"status": "DONE", "target_amount": None},
        ],
        activity=[{"txn_id": 1}],
    )

    assert result["tracked_assets"] == 2050.0
    assert result["securities_value"] == 2000.0
    assert result["cash_on_file"] == 50.0
    assert result["cash_account_count"] == 1
    assert result["unrealized_gain"] == 200.0
    assert result["position_count"] == 3
    assert result["valued_positions"] == 2
    assert result["unvalued_positions"] == 1
    assert result["active_goal_count"] == 1
    assert result["accounts"][0]["securities_value"] == 1200.0
    assert result["accounts"][1]["cash_amount"] is None


def test_overview_shows_gain_only_when_all_valued_positions_have_cost_basis():
    result = build_wealth_overview(
        accounts=[{"account_id": 1}],
        holdings=[
            {"account_id": 1, "current_value": 1200.0, "cost_basis_total": 1000.0},
            {"account_id": 1, "current_value": 800.0, "cost_basis_total": 700.0},
        ],
        cash_balances=[],
        goals=[],
        activity=[],
    )

    assert result["unrealized_gain"] == 300.0
    assert result["unrealized_gain_pct"] == 300.0 / 1700.0 * 100


def test_unvalued_positions_do_not_change_gain_or_cost_basis_totals():
    result = build_wealth_overview(
        accounts=[{"account_id": 1}],
        holdings=[
            {"account_id": 1, "current_value": 1200.0, "cost_basis_total": 1000.0},
            {"account_id": 1, "current_value": None, "cost_basis_total": 900.0},
        ],
        cash_balances=[],
        goals=[],
        activity=[],
    )

    assert result["unrealized_gain"] == 200.0
    assert result["unrealized_gain_pct"] == 20.0
