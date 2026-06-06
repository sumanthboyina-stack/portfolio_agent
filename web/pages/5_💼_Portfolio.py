"""
Portfolio Manager — import holdings from Fidelity / Vanguard CSV exports,
view current positions, and sync to portfolio.yaml.

Upload flow (ad-hoc, user-initiated):
  1. Click "Upload CSV" on the broker card
  2. Export Holdings CSV from your broker (Fidelity: Positions → Download CSV;
     Vanguard: My Accounts → Holdings → Download)
  3. Drop the file — parser auto-detects broker, imports rows, enriches with
     live prices from yfinance, writes to DB and portfolio.yaml
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path

import streamlit as st

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from web.styles import (
    inject_global_css, page_header, section_title, badge_html, top_nav,
    SUCCESS, WARNING, DANGER, PRIMARY, NEUTRAL,
    SUCCESS_LIGHT, WARNING_LIGHT, PRIMARY_LIGHT,
)

_YAML_PATH = str(_ROOT / "config" / "portfolio.yaml")

st.set_page_config(
    page_title="Portfolio — Portfolio Intelligence",
    page_icon="💼",
    layout="wide",
    initial_sidebar_state="expanded",
)
inject_global_css()
top_nav("portfolio")

with st.sidebar:
    st.markdown('<p style="font-size:0.7rem;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;color:#475569;margin:0 0 10px">Portfolio</p>', unsafe_allow_html=True)

page_header(
    "Portfolio Manager",
    subtitle="Import holdings from Fidelity or Vanguard · Syncs to portfolio.yaml",
    icon="💼",
)

# ── Imports ───────────────────────────────────────────────────────────────────
from portfolio_agent.tools.holdings_db import (
    upsert_holdings, get_holdings, get_holdings_summary,
    delete_broker_holdings, sync_to_yaml,
)
from portfolio_agent.tools.holdings_parser import parse_csv


# ── Helpers ───────────────────────────────────────────────────────────────────

def _enrich_prices(holdings: list[dict]) -> list[dict]:
    """Fetch live prices for holdings that don't already have current_price."""
    tickers = [h["ticker"] for h in holdings if not h.get("current_price")]
    if not tickers:
        return holdings
    try:
        import yfinance as yf
        prices = {}
        for chunk in [tickers[i:i+10] for i in range(0, len(tickers), 10)]:
            data = yf.download(
                " ".join(chunk), period="1d", auto_adjust=True,
                progress=False, group_by="ticker",
            )
            for t in chunk:
                try:
                    if len(chunk) == 1:
                        prices[t] = float(data["Close"].iloc[-1])
                    else:
                        prices[t] = float(data[t]["Close"].iloc[-1])
                except Exception:
                    pass

        for h in holdings:
            if h["ticker"] in prices and not h.get("current_price"):
                price = prices[h["ticker"]]
                h["current_price"] = round(price, 2)
                if h.get("shares"):
                    h["current_value"] = round(price * h["shares"], 2)
    except Exception:
        pass
    return holdings


def _fmt_dollars(v) -> str:
    if v is None:
        return "—"
    try:
        return f"${float(v):,.2f}"
    except Exception:
        return "—"


def _fmt_shares(v) -> str:
    if v is None:
        return "—"
    try:
        f = float(v)
        return f"{f:,.4f}".rstrip("0").rstrip(".")
    except Exception:
        return "—"


def _pnl_html(cost_total, current_val) -> str:
    try:
        gain = current_val - cost_total
        pct  = gain / cost_total * 100
        col  = SUCCESS if gain >= 0 else DANGER
        sign = "+" if gain >= 0 else ""
        return (
            f'<span style="color:{col};font-weight:600">'
            f'{sign}${gain:,.0f} ({sign}{pct:.1f}%)</span>'
        )
    except Exception:
        return "—"


BROKER_META = {
    "fidelity": {
        "label": "Fidelity",
        "icon": "🟢",
        "color": "#22863A",
        "bg": "#F0FDF4",
        "border": "#BBF7D0",
        "instructions": (
            "1. Log into fidelity.com\n"
            "2. Go to **Accounts & Trade → Portfolio**\n"
            "3. Click **Download** → **Download portfolio as spreadsheet (CSV)**\n"
            "4. Upload the downloaded file below"
        ),
    },
    "vanguard": {
        "label": "Vanguard",
        "icon": "🔴",
        "color": "#9B1C1C",
        "bg": "#FFF5F5",
        "border": "#FECACA",
        "instructions": (
            "1. Log into vanguard.com\n"
            "2. Go to **My Accounts → Holdings**\n"
            "3. Click **Download** (spreadsheet icon)\n"
            "4. Upload the downloaded CSV file below"
        ),
    },
}


# ── Summary stats ─────────────────────────────────────────────────────────────

summary = get_holdings_summary()
all_holdings = get_holdings()
total_val = summary.get("total_value", 0)
holding_count = summary.get("holding_count", 0)
brokers_connected = summary.get("brokers", [])

col1, col2, col3, col4 = st.columns(4)
col1.metric("Total Portfolio Value", _fmt_dollars(total_val) if total_val else "—")
col2.metric("Positions", holding_count)
col3.metric("Brokers Connected", len(brokers_connected))
col4.metric(
    "Last Synced",
    (summary.get("last_synced_at") or "Never")[:16].replace("T", " "),
)

st.divider()


# ══════════════════════════════════════════════════════════════════════════════
# BROKER CONNECTION CARDS
# ══════════════════════════════════════════════════════════════════════════════

section_title("Broker Connections", badge_text="CSV upload · ad-hoc")

broker_cols = st.columns(2)

for col, broker_key in zip(broker_cols, ["fidelity", "vanguard"]):
    meta = BROKER_META[broker_key]
    connected = broker_key in brokers_connected
    broker_holdings = [h for h in all_holdings if h.get("broker") == broker_key]
    broker_val = sum(h.get("current_value") or 0 for h in broker_holdings)

    status_color = SUCCESS if connected else "#94A3B8"
    status_label = f"✅ Connected · {len(broker_holdings)} positions" if connected else "⬜ Not connected"

    with col:
        st.markdown(
            f'<div style="background:{meta["bg"]};border:1px solid {meta["border"]};'
            f'border-radius:14px;padding:18px 20px;min-height:200px">'
            f'<div style="display:flex;justify-content:space-between;align-items:flex-start">'
            f'<div>'
            f'<span style="font-size:1.4rem">{meta["icon"]}</span>'
            f'<span style="font-size:1.1rem;font-weight:800;color:{meta["color"]};margin-left:8px">'
            f'{meta["label"]}</span>'
            f'</div>'
            f'<span style="font-size:0.78rem;color:{status_color};font-weight:600">{status_label}</span>'
            f'</div>'
            + (
                f'<div style="margin-top:8px;font-size:1.2rem;font-weight:800;color:#0F172A">'
                f'{_fmt_dollars(broker_val)}</div>'
                f'<div style="font-size:0.75rem;color:#64748B">portfolio value</div>'
                if connected else
                f'<div style="margin-top:12px;font-size:0.8rem;color:#64748B;line-height:1.5">'
                f'Upload your {meta["label"]} holdings CSV to connect.</div>'
            )
            + f'</div>',
            unsafe_allow_html=True,
        )

        uploaded = st.file_uploader(
            f"{'🔄 Re-upload' if connected else '📤 Upload'} {meta['label']} CSV",
            type=["csv"],
            key=f"upload_{broker_key}",
        )

        with st.expander("How to export from " + meta["label"]):
            st.markdown(
                f'<div style="font-size:0.82rem;color:#374151;white-space:pre-line">'
                f'{meta["instructions"]}</div>',
                unsafe_allow_html=True,
            )

        if uploaded:
            with st.spinner(f"Parsing {meta['label']} CSV…"):
                try:
                    content = uploaded.read()
                    detected_broker, parsed = parse_csv(content)

                    if not parsed:
                        st.error("No valid holdings found in this file. Check the CSV format.")
                    else:
                        with st.spinner(f"Fetching live prices for {len(parsed)} positions…"):
                            parsed = _enrich_prices(parsed)

                        today = date.today().isoformat()
                        result = upsert_holdings(parsed, detected_broker, today)
                        sync_to_yaml(_YAML_PATH)

                        st.success(
                            f"✅ Imported **{result['saved']} positions** from {meta['label']}. "
                            f"portfolio.yaml updated."
                        )
                        st.rerun()

                except ValueError as e:
                    st.error(str(e))
                except Exception as e:
                    st.error(f"Import failed: {e}")

        if connected:
            if st.button(f"🗑️ Disconnect {meta['label']}", key=f"del_{broker_key}",
                         help="Remove all holdings imported from this broker"):
                n = delete_broker_holdings(broker_key)
                sync_to_yaml(_YAML_PATH)
                st.success(f"Removed {n} {meta['label']} positions.")
                st.rerun()

st.divider()


# ── Manual add ────────────────────────────────────────────────────────────────

with st.expander("➕ Add holding manually"):
    mc1, mc2, mc3, mc4, mc5 = st.columns([2, 2, 2, 2, 2])
    with mc1:
        m_ticker = st.text_input("Ticker", placeholder="AAPL", key="m_ticker").upper().strip()
    with mc2:
        m_shares = st.number_input("Shares", min_value=0.0, step=0.001, format="%.4f", key="m_shares")
    with mc3:
        m_cost   = st.number_input("Avg cost / share ($)", min_value=0.0, step=0.01, key="m_cost")
    with mc4:
        m_acct   = st.text_input("Account name", placeholder="Brokerage", key="m_acct")
    with mc5:
        m_atype  = st.selectbox("Account type", ["TAXABLE","IRA","ROTH_IRA","401K","HSA"],
                                key="m_atype")

    if st.button("Add", type="primary", key="m_add"):
        if not m_ticker:
            st.warning("Enter a ticker symbol.")
        else:
            holding = {
                "ticker": m_ticker,
                "description": "",
                "shares": float(m_shares) if m_shares else None,
                "avg_cost": float(m_cost) if m_cost else None,
                "cost_basis_total": (m_shares * m_cost) if m_shares and m_cost else None,
                "current_price": None,
                "current_value": None,
                "account_name": m_acct or "Manual",
                "account_number": "",
                "account_type": m_atype,
                "sector": "",
            }
            # Enrich with live price
            enriched = _enrich_prices([holding])
            upsert_holdings(enriched, "manual", date.today().isoformat())
            sync_to_yaml(_YAML_PATH)
            st.success(f"Added {m_ticker} to portfolio.")
            st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# HOLDINGS TABLE
# ══════════════════════════════════════════════════════════════════════════════

if all_holdings:
    section_title("Current Holdings", badge_text=f"{holding_count} positions")

    # Broker filter
    filter_broker = st.selectbox(
        "Filter by broker", ["All"] + sorted(brokers_connected),
        key="hld_broker_filter",
        label_visibility="collapsed",
    )
    display_holdings = (
        [h for h in all_holdings if h.get("broker") == filter_broker]
        if filter_broker != "All" else all_holdings
    )

    # Table header
    st.markdown(
        '<div style="display:grid;grid-template-columns:100px 220px 90px 100px 100px 110px 130px 90px;'
        'gap:8px;padding:6px 12px;background:#F1F5F9;border-radius:8px;'
        'font-size:0.72rem;font-weight:700;text-transform:uppercase;letter-spacing:0.05em;'
        'color:#475569;margin-bottom:4px">'
        '<span>Ticker</span><span>Description</span><span>Shares</span>'
        '<span>Avg Cost</span><span>Price</span><span>Value</span>'
        '<span>P&amp;L</span><span>Broker</span>'
        '</div>',
        unsafe_allow_html=True,
    )

    for h in display_holdings:
        pnl = _pnl_html(h.get("cost_basis_total"), h.get("current_value"))
        broker_label = (h.get("broker") or "manual").title()
        acct_type = h.get("account_type") or ""

        st.markdown(
            f'<div style="display:grid;grid-template-columns:100px 220px 90px 100px 100px 110px 130px 90px;'
            f'gap:8px;padding:10px 12px;background:white;border:1px solid #F1F5F9;'
            f'border-radius:8px;margin-bottom:3px;font-size:0.85rem;align-items:center">'
            f'<span style="font-weight:800;color:#0F172A">{h["ticker"]}</span>'
            f'<span style="color:#64748B;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">'
            f'{(h.get("description") or "")[:28]}</span>'
            f'<span>{_fmt_shares(h.get("shares"))}</span>'
            f'<span>{_fmt_dollars(h.get("avg_cost"))}</span>'
            f'<span>{_fmt_dollars(h.get("current_price"))}</span>'
            f'<span style="font-weight:600">{_fmt_dollars(h.get("current_value"))}</span>'
            f'<span>{pnl}</span>'
            f'<span style="font-size:0.75rem;color:#64748B">{broker_label}<br>'
            f'<span style="color:#94A3B8">{acct_type}</span></span>'
            f'</div>',
            unsafe_allow_html=True,
        )

    # Totals row
    total_cost = sum(h.get("cost_basis_total") or 0 for h in display_holdings)
    total_mkt  = sum(h.get("current_value") or 0 for h in display_holdings)
    total_pnl  = _pnl_html(total_cost, total_mkt) if total_cost and total_mkt else "—"
    st.markdown(
        f'<div style="display:grid;grid-template-columns:100px 220px 90px 100px 100px 110px 130px 90px;'
        f'gap:8px;padding:10px 12px;background:#F8FAFC;border:1px solid #E2E8F0;'
        f'border-radius:8px;margin-top:4px;font-size:0.85rem;font-weight:700;align-items:center">'
        f'<span style="color:#0F172A">Total</span><span></span><span></span>'
        f'<span>{_fmt_dollars(total_cost) if total_cost else "—"}</span>'
        f'<span></span>'
        f'<span style="color:#0F172A">{_fmt_dollars(total_mkt) if total_mkt else "—"}</span>'
        f'<span>{total_pnl}</span><span></span>'
        f'</div>',
        unsafe_allow_html=True,
    )

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Sector breakdown ──────────────────────────────────────────────────────
    by_sector = summary.get("by_sector", {})
    if by_sector and total_val > 0:
        section_title("Sector Allocation")
        sector_cols = st.columns(min(len(by_sector), 4))
        sorted_sectors = sorted(by_sector.items(), key=lambda x: x[1], reverse=True)
        for i, (sector, val) in enumerate(sorted_sectors):
            pct = val / total_val * 100 if total_val else 0
            with sector_cols[i % len(sector_cols)]:
                st.markdown(
                    f'<div style="background:white;border:1px solid #E2E8F0;border-radius:10px;'
                    f'padding:12px 14px;margin-bottom:8px">'
                    f'<div style="font-size:0.78rem;font-weight:600;color:#64748B">{sector or "Unknown"}</div>'
                    f'<div style="font-size:1rem;font-weight:800;color:#0F172A;margin-top:2px">'
                    f'{_fmt_dollars(val)}</div>'
                    f'<div style="background:#E2E8F0;border-radius:4px;height:4px;margin-top:6px">'
                    f'<div style="background:{PRIMARY};border-radius:4px;height:4px;'
                    f'width:{min(pct, 100):.0f}%"></div></div>'
                    f'<div style="font-size:0.72rem;color:#94A3B8;margin-top:3px">'
                    f'{pct:.1f}% of portfolio</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

    # ── Sync button ───────────────────────────────────────────────────────────
    st.divider()
    c1, c2 = st.columns([2, 6])
    with c1:
        if st.button("💾 Sync to portfolio.yaml", type="primary", use_container_width=True):
            sync_to_yaml(_YAML_PATH)
            st.success("✅ portfolio.yaml updated with current holdings.")
    with c2:
        st.caption(
            "portfolio.yaml is used by the daily pipeline to prioritise your holdings for analysis. "
            "Sync after any import or manual change."
        )

else:
    st.info(
        "No holdings imported yet. Upload a Fidelity or Vanguard CSV above, "
        "or add positions manually.",
        icon="💼",
    )
    st.markdown(
        '<div style="background:#F8FAFC;border:1px solid #E2E8F0;border-radius:10px;'
        'padding:16px 20px;margin-top:12px">'
        '<h4 style="margin:0 0 8px;color:#0F172A;font-size:0.95rem">How it works</h4>'
        '<ol style="margin:0;color:#475569;font-size:0.85rem;line-height:2">'
        '<li>Export your holdings CSV from Fidelity or Vanguard (instructions in the cards above)</li>'
        '<li>Upload the file — broker is auto-detected, positions are parsed and prices fetched live</li>'
        '<li>Holdings are saved to the portfolio database and <code>config/portfolio.yaml</code></li>'
        '<li>The daily pipeline prioritises your portfolio tickers for analysis</li>'
        '</ol></div>',
        unsafe_allow_html=True,
    )
