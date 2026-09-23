"""Test seeding through the account service (the only writer of accounts and positions)."""
from __future__ import annotations

from portfolio_agent.services.account_service import add_position, create_account, find_account
from portfolio_agent.services.context import local_context

CTX = local_context("test")


def seed_holding(holding: dict, broker: str, as_of_date: str) -> dict:
    """Mirror of the old add_holding(): ensure the account exists, add the position, return it with an ``id``."""
    number = holding.get("account_number") or holding.get("account_name") or ""
    acct = find_account(CTX, broker, number)
    if acct is None:
        acct = create_account(CTX, broker=broker, account_number=number, display_name=holding.get("account_name"),
                              account_type=holding.get("account_type"))
    pos = add_position(CTX, acct["account_id"], holding, as_of_date=as_of_date)
    return {"id": pos["position_id"], **pos}


def account_id_of(broker: str, account_number: str) -> int:
    return find_account(CTX, broker, account_number)["account_id"]
