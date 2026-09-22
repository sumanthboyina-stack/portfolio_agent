"""
Portfolio holdings formatting helpers.

Contains:
  - _fmt_shares  : format a share quantity, trimming trailing zeros
  - _pnl_html    : render a colored P&L span (gain/loss $ and %)
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from web.styles import SUCCESS, DANGER, fmt_money, fmt_pct


def _fmt_shares(v) -> str:
    if v is None:
        return "—"
    try:
        f = float(v)
        return f"{f:,.2f}".rstrip("0").rstrip(".")
    except Exception:
        return "—"


def _pnl_html(cost_total, current_val) -> str:
    try:
        gain = current_val - cost_total
        pct  = gain / cost_total * 100
        col  = SUCCESS if gain >= 0 else DANGER
        return (
            f'<span style="color:{col};font-weight:600">'
            f'{fmt_money(gain, signed=True)} ({fmt_pct(pct, signed=True)})</span>'
        )
    except Exception:
        return "—"
