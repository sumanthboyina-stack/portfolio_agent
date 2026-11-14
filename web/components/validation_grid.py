"""Shared "checks x horizons" grid for the Validation page (Predictions and Plans & Simulation use the same look and the same rules)."""
from __future__ import annotations

import html

from portfolio_agent.tools import validation_scorecard as sc

VERDICT_COLORS = {
    sc.WORKING: "#059669",
    sc.UNPROVEN: "#D97706",
    sc.NOT_WORKING: "#DC2626",
    sc.TOO_EARLY: "#64748B",
    sc.INFO_ONLY: "#2563EB",
}
_BORDER = "border-top:1px solid rgba(128,128,128,.2)"


def _esc(text: str) -> str:
    return html.escape(text or "", quote=True)


def cell_html(item: sc.ScoreItem) -> str:
    color = VERDICT_COLORS.get(item.verdict, "#64748B")
    tip = sc.plain_tooltip(item) + "\n" + sc.verdict_tooltip(item)
    return (
        f'<td style="padding:10px 12px;{_BORDER};cursor:help" title="{_esc(tip)}">'
        f'<div style="font-size:1.35rem;font-weight:800;line-height:1.2">{_esc(item.display)}</div>'
        f'<span style="background:{color}22;color:{color};border:1px solid {color}66;border-radius:20px;'
        f'padding:1px 9px;font-size:.72rem;font-weight:700">{_esc(item.verdict)}</span></td>'
    )


def grid_html(columns: list[tuple[str, str]], rows: list[tuple[str, str, list[sc.ScoreItem]]]) -> str:
    """columns: (label, tooltip) per horizon; rows: (row title, row tooltip, one ScoreItem per column)."""
    head = "".join(
        f'<th style="text-align:left;padding:6px 12px;font-size:.85rem;cursor:help" title="{_esc(tip)}">{_esc(label)}</th>'
        for label, tip in columns
    )
    body = ""
    for title, tip, items in rows:
        cells = "".join(cell_html(it) for it in items)
        body += (f'<tr><td style="padding:10px 12px;{_BORDER};font-weight:600;cursor:help" title="{_esc(tip)}">'
                 f'{_esc(title)} <span style="opacity:.5;font-weight:400">ⓘ</span></td>{cells}</tr>')
    return f'<table style="width:100%;border-collapse:collapse"><tr><th></th>{head}</tr>{body}</table>'
