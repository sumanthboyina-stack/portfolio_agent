"""Owner-scoped data composition for the My Wealth overview."""

from __future__ import annotations

from portfolio_agent.services.account_service import (
    get_cash_balances_for,
    list_accounts_for,
    list_holdings_for,
)
from portfolio_agent.services.context import RequestContext
from portfolio_agent.services.investor_service import list_goals
from web.data.ledger import transactions_view
from web.data.portfolio import masked_account_number


def build_wealth_overview(
    accounts: list[dict],
    holdings: list[dict],
    cash_balances: list[dict],
    goals: list[dict],
    activity: list[dict],
) -> dict:
    """Aggregate tracked investment assets without treating missing data as zero."""
    by_account = {
        account["account_id"]: {
            **account,
            "securities_value": 0.0,
            "valued_positions": 0,
            "position_count": 0,
            "cost_basis": 0.0,
            "costed_positions": 0,
            "unvalued_positions": 0,
            "cash_amount": None,
        }
        for account in accounts
    }

    for holding in holdings:
        account = by_account.get(holding.get("account_id"))
        if account is None:
            continue
        account["position_count"] += 1
        value = holding.get("current_value")
        if value is None:
            account["unvalued_positions"] += 1
        else:
            account["securities_value"] += float(value)
            account["valued_positions"] += 1
        cost = holding.get("cost_basis_total")
        if value is not None and cost is not None:
            account["cost_basis"] += float(cost)
            account["costed_positions"] += 1

    for cash in cash_balances:
        account = by_account.get(cash.get("account_id"))
        if account is not None:
            account["cash_amount"] = cash.get("amount")

    values = list(by_account.values())
    securities_value = sum(account["securities_value"] for account in values)
    cash_on_file = sum(
        float(account["cash_amount"])
        for account in values
        if account["cash_amount"] is not None
    )
    cost_basis = sum(account["cost_basis"] for account in values)
    costed_positions = sum(account["costed_positions"] for account in values)
    valued_positions = sum(account["valued_positions"] for account in values)
    gain = securities_value - cost_basis if costed_positions == valued_positions and valued_positions else None

    return {
        "accounts": values,
        "account_count": len(values),
        "securities_value": securities_value,
        "cash_on_file": cash_on_file,
        "cash_account_count": sum(account["cash_amount"] is not None for account in values),
        "tracked_assets": securities_value + cash_on_file,
        "unrealized_gain": gain,
        "unrealized_gain_pct": gain / cost_basis * 100 if gain is not None and cost_basis else None,
        "position_count": sum(account["position_count"] for account in values),
        "valued_positions": valued_positions,
        "unvalued_positions": sum(account["unvalued_positions"] for account in values),
        "goals": goals,
        "active_goal_count": sum(goal["status"] == "ACTIVE" for goal in goals),
        "activity": activity,
    }


def load_wealth_overview(ctx: RequestContext) -> dict:
    """Load the signed-in user's accounts, positions, goals, and ledger activity."""
    accounts = list_accounts_for(ctx)
    holdings = list_holdings_for(ctx)
    cash_balances = get_cash_balances_for(ctx)
    goals = list_goals(ctx)

    labels = {
        account["account_id"]: (
            account.get("display_name")
            or masked_account_number(account.get("account_number"))
            or f"Account {account['account_id']}"
        )
        for account in accounts
    }
    activity = []
    for account in accounts:
        if account.get("holdings_source") != "ledger":
            continue
        for transaction in transactions_view(ctx, account["account_id"], show_voided=False):
            activity.append({
                **transaction,
                "account_name": labels[account["account_id"]],
            })
    activity.sort(key=lambda row: (row["date"], row["txn_id"]), reverse=True)

    return build_wealth_overview(accounts, holdings, cash_balances, goals, activity[:8])
