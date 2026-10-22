"""
Wealth overview — tracked brokerage assets, goals and recent account activity.
The "Overview" section of the Portfolio page (it used to be the My Wealth page).
"""

from __future__ import annotations

import html
from datetime import date

import streamlit as st

from web.components.portfolio_sections import go_to_section_button
from web.data.portfolio import masked_account_number
from web.data.ledger import activity_overview
from web.data.wealth import load_wealth_overview
from web.styles import DANGER, PRIMARY, SUCCESS, fmt_money, section_title


def _render_year_activity(ctx) -> None:
    """This calendar year's money in and out across every account, from saved history files plus the ledger. Shown only when there is any."""
    year = date.today().year
    try:
        act = activity_overview(ctx, year)
    except Exception:                       # a summary aid: never take the Overview down
        return
    events = act["from_history"] + act["from_ledger"]
    if not events:
        return
    t = act["totals"]
    st.divider()
    section_title(f"{year} activity", badge_text=f"{events} events · {act['first_date']} → {act['last_date']}")
    cols = st.columns(4)
    cols[0].metric("Dividends & interest", fmt_money(t["dividends"] + t["interest"]),
                   help=f"Dividends {fmt_money(t['dividends'])} + interest {fmt_money(t['interest'])}.")
    cols[1].metric("Deposits", fmt_money(t["deposits"]), help="Money you put in (transfers and payroll-funded purchases), net of nothing.")
    cols[2].metric("Sale proceeds", fmt_money(t["sale_proceeds"]), help=f"{t['sale_count']} sale(s). Gains need the cost of the shares sold and are not shown.")
    cols[3].metric("Purchases", fmt_money(t["purchases"]), help=f"{t['purchase_count']} buy(s), dividend reinvestments included.")
    st.caption("From the history files you saved and the ledger. Open Accounts & Imports → Transactions & ledger → Activity for the detail by account.")


def render_wealth_overview(ctx) -> None:
    overview = load_wealth_overview(ctx)
    st.caption(
        "Tracked brokerage assets only: imported securities plus cash reported by your accounts. "
        "This is not a complete net-worth statement; debts, property, and accounts not added here are excluded."
    )

    metric_cols = st.columns(4)
    metric_cols[0].metric("Tracked assets", fmt_money(overview["tracked_assets"]))
    metric_cols[1].metric("Investments", fmt_money(overview["securities_value"]))
    metric_cols[2].metric(
        "Cash on file",
        fmt_money(overview["cash_on_file"]) if overview["cash_account_count"] else "—",
        help="Cash is shown only when an account statement includes a cash balance.",
    )
    gain = overview["unrealized_gain"]
    metric_cols[3].metric(
        "Unrealized gain/loss",
        fmt_money(gain, signed=True) if gain is not None else "—",
        delta=(
            f'{overview["unrealized_gain_pct"]:+.2f}%'
            if overview["unrealized_gain_pct"] is not None
            else None
        ),
        help="Shown only when cost basis is recorded for every valued position.",
    )

    if overview["unvalued_positions"]:
        verb = "has" if overview["unvalued_positions"] == 1 else "have"
        st.warning(
            f'{overview["unvalued_positions"]} position'
            f'{"s" if overview["unvalued_positions"] != 1 else ""} '
            f"{verb} no current value "
            "and are excluded from tracked assets. Review prices in the Holdings section."
        )

    _render_year_activity(ctx)

    st.divider()
    account_col, goal_col = st.columns([1.15, 0.85], gap="large")

    with account_col:
        section_title("Accounts", badge_text=f'{overview["account_count"]} tracked')
        if not overview["accounts"]:
            st.info("Add an investment account to see your portfolio here.")
            go_to_section_button("Set up accounts", "accounts", icon=":material/account_balance:", key="wealth_setup_accounts")
        else:
            for account in overview["accounts"]:
                title = html.escape(
                    account.get("display_name")
                    or masked_account_number(account.get("account_number"))
                    or f'Account {account["account_id"]}'
                )
                broker = html.escape((account.get("broker") or "Brokerage").replace("_", " ").title())
                cash = (
                    fmt_money(account["cash_amount"])
                    if account["cash_amount"] is not None
                    else "Not reported"
                )
                securities = (
                    fmt_money(account["securities_value"])
                    if account["valued_positions"]
                    else "—"
                )
                positions = (
                    f'{account["valued_positions"]} of {account["position_count"]} positions valued'
                )
                with st.container(border=True):
                    st.markdown(f"**{title}** · {broker}")
                    left, right = st.columns(2)
                    left.metric("Investments", securities)
                    right.metric("Cash", cash)
                    st.caption(positions)
            go_to_section_button("View holdings and allocation", "holdings", icon=":material/work:", key="wealth_view_holdings")

    with goal_col:
        section_title("Financial goals", badge_text=f'{overview["active_goal_count"]} active')
        st.caption(
            "Targets are personal planning notes. They are not linked to account balances or presented as funded progress."
        )
        active_goals = [goal for goal in overview["goals"] if goal["status"] == "ACTIVE"]
        if not active_goals:
            st.info("No active goals yet. Add a target to keep your plans visible.")
        for goal in active_goals:
            with st.container(border=True):
                st.markdown(f'**{html.escape(goal["name"])}**')
                st.caption(goal["goal_type"].replace("_", " ").title())
                meta = []
                if goal["target_amount"] is not None:
                    meta.append(f'Target · {fmt_money(goal["target_amount"])}')
                if goal["target_date"]:
                    meta.append(f'By · {goal["target_date"]}')
                st.caption(" · ".join(meta) if meta else "No target amount or date set")
        st.page_link("pages/12_👤_Profile.py", label="Manage goals and profile", icon=":material/person:")

    st.divider()
    section_title("Recent account activity", badge_text=f'{len(overview["activity"])} transactions')
    if not overview["activity"]:
        st.info("No transaction-ledger activity to show. Add transactions from Accounts & Imports.")
        go_to_section_button("Open accounts and transactions", "accounts", icon=":material/account_balance:", key="wealth_open_accounts")
    else:
        for item in overview["activity"]:
            label = item["type_label"]
            if item["ticker"]:
                label += f' · {item["ticker"]}'
            amount = fmt_money(item["amount"]) if item["amount"] is not None else "—"
            amount_color = (
                SUCCESS if item["type"] in {"DEPOSIT", "DIVIDEND", "INTEREST", "EMPLOYER_MATCH"}
                else DANGER if item["type"] in {"WITHDRAWAL", "FEE"}
                else PRIMARY
            )
            st.markdown(
                f'<div style="display:flex;justify-content:space-between;gap:1rem;'
                f'padding:0.72rem 0;border-bottom:1px solid #F1F5F9">'
                f'<div><div style="font-weight:600;color:#111827">{html.escape(label)}</div>'
                f'<div style="font-size:0.8rem;color:#64748B">'
                f'{html.escape(item["date"])} · {html.escape(item["account_name"])}</div></div>'
                f'<div style="font-weight:600;color:{amount_color};white-space:nowrap">{amount}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
