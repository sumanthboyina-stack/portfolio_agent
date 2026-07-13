"""
Validation dashboard Streamlit card/tile renderers.

Contains:
  - _render_scorecard_tiles : horizontal metric tiles with CSS hover tooltips, per horizon
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from web.components.validation_charts import _METRIC_CFG, _H_COLORS, _TILE_CSS, _get_tier


def _render_scorecard_tiles(by_horizon: dict, all_horizons: list, horizon_labels: dict) -> None:
    """Render horizontal metric tiles — one row of cards per horizon, CSS hover tooltips."""
    metric_keys = list(_METRIC_CFG.keys())
    html = _TILE_CSS

    for h_days in all_horizons:
        row    = by_horizon[h_days]
        n      = row.get("num_predictions") or 0
        hlbl   = horizon_labels.get(h_days, f"{h_days}d")
        hcolor = _H_COLORS.get(h_days, "#64748B")
        if n == 0:
            n_label = "no evaluated predictions yet"
            n_warn  = ""
        elif n < 30:
            n_label = f"{n} evaluated predictions"
            n_warn  = "  ⚠️ fewer than 30 — metrics unreliable"
        else:
            n_label = f"{n} evaluated predictions"
            n_warn  = ""

        html += (
            f'<div class="sc-wrap">'
            f'<div class="sc-horizon-hdr" style="color:{hcolor};background:{hcolor}18;border-left:3px solid {hcolor}">'
            f'{hlbl} Horizon &nbsp;·&nbsp; {n_label}{n_warn}</div>'
            f'<div class="sc-row">'
        )

        for key in metric_keys:
            cfg  = _METRIC_CFG[key]
            val  = row.get(key)
            short_label = cfg["label"].split("  ↓")[0].split("  (")[0]

            if val is None:
                color, val_text, tier_lbl = "#475569", "—", "No data"
            else:
                tier_lbl, color = _get_tier(key, val)
                val_text = cfg["fmt"](val)

            # Tier rows inside tooltip — bold + white for the active one
            range_rows = "".join(
                f'<div class="tip-tier{" cur" if lbl == tier_lbl else ""}">'
                f'<span style="color:{tc}">●</span> {lbl}</div>'
                for _, tc, lbl in cfg["tiers"]
            )
            lower_note = " &nbsp;↓ lower is better" if not cfg["higher"] else ""
            ref_html   = (
                f'<div class="tip-ref">Reference: {cfg["ref_label"]}</div>'
                if cfg.get("ref_label") else ""
            )
            tip = (
                f'<div class="sc-tip">'
                f'<div class="tip-title">{cfg["label"]}</div>'
                f'<div class="tip-val" style="color:{color}">{val_text}{lower_note}</div>'
                f'<div class="tip-defn">{cfg["defn"]}</div>'
                f'<div class="tip-ranges-hdr">Performance tiers:</div>'
                f'{range_rows}{ref_html}</div>'
            )

            tier_short = tier_lbl.split("(")[0].strip()
            html += (
                f'<div class="sc-tile">'
                f'<div class="sc-tile-bar" style="background:{color}"></div>'
                f'<div class="sc-tile-label">{short_label}</div>'
                f'<div class="sc-tile-value" style="color:{color}">{val_text}</div>'
                f'<div class="sc-tile-tier">{tier_short}</div>'
                f'{tip}</div>'
            )

        html += '</div></div>'

    st.markdown(html, unsafe_allow_html=True)
