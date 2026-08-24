"""
Predictions page — Plotly figure builders and small chart-adjacent helpers.

Contains:
  - _prob_strip_html()  : gradient color strip for the probability distribution
  - _plot_prob_full()   : full probability distribution bar chart
  - _plot_score_radar() : radar chart of the four component scores
  - _plot_price_chart() : price + SMA overlay, volume, and RSI sub-panel
"""

from __future__ import annotations

import sys
from pathlib import Path

import plotly.graph_objects as go

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from web.styles import (
    SUCCESS, WARNING, DANGER, PRIMARY, NEUTRAL, PURPLE,
)
from web.data.predictions import _fetch_price_chart_data


_PROB_BUCKETS = [
    ("p_strong_down",   "#EF4444", "Significant Drop",  "< −5%"),
    ("p_moderate_down", "#F97316", "Mild Decline",       "−5% to −1%"),
    ("p_flat",          "#94A3B8", "Flat / Sideways",    "−1% to +1%"),
    ("p_moderate_up",   "#22C55E", "Moderate Gain",      "+1% to +5%"),
    ("p_strong_up",     "#059669", "Strong Rally",       "> +5%"),
]


def _prob_strip_html(row: dict, height: int = 14) -> str:
    """Render a gradient color strip for the probability distribution.

    Replaces mini bar charts — reads left-to-right as bearish → bullish.
    The dominant bucket gets a small label above it.
    """
    vals = [row.get(key) or 0 for key, *_ in _PROB_BUCKETS]
    total = sum(vals) or 100
    pcts  = [v / total * 100 for v in vals]

    # Dominant bucket
    max_idx = pcts.index(max(pcts))

    # Build strip segments
    segments = "".join(
        f'<div title="{label} ({range_}): {v:.0f}%" '
        f'style="width:{pct:.1f}%;background:{color};height:{height}px"></div>'
        for (_, color, label, range_), pct, v in zip(_PROB_BUCKETS, pcts, vals)
    )

    # Percentage labels below the strip
    label_cells = "".join(
        f'<div style="width:{pct:.1f}%;text-align:center;font-size:0.6rem;'
        f'color:{color};font-weight:{"700" if i == max_idx else "400"}">'
        f'{v:.0f}%</div>'
        for i, ((_, color, *_rest), pct, v) in enumerate(zip(_PROB_BUCKETS, pcts, vals))
    )

    # Bullish / bearish summary chips
    p_up   = vals[3] + vals[4]
    p_down = vals[0] + vals[1]
    p_flat = vals[2]

    return (
        f'<div style="margin:4px 0">'
        f'<div style="display:flex;border-radius:6px;overflow:hidden">{segments}</div>'
        f'<div style="display:flex;margin-top:1px">{label_cells}</div>'
        f'<div style="display:flex;gap:8px;margin-top:4px">'
        f'<span style="font-size:0.65rem;color:#059669">📈 {p_up:.0f}% bullish</span>'
        f'<span style="font-size:0.65rem;color:#94A3B8">➡ {p_flat:.0f}% flat</span>'
        f'<span style="font-size:0.65rem;color:#EF4444">📉 {p_down:.0f}% bearish</span>'
        f'</div>'
        f'</div>'
    )


def _plot_prob_full(row: dict) -> go.Figure:
    """Full probability distribution — horizontal bars with meaningful labels and % annotations."""
    data = [(key, color, label, range_) for key, color, label, range_ in _PROB_BUCKETS]
    display_labels = [f"{label}  {range_}" for _, _, label, range_ in data[::-1]]
    values_r       = [(row.get(key) or 0) for key, *_ in data[::-1]]
    colors_r       = [color for _, color, *_ in data[::-1]]

    fig = go.Figure()
    for y_label, val, color in zip(display_labels, values_r, colors_r):
        fig.add_trace(go.Bar(
            y=[y_label],
            x=[val],
            orientation="h",
            marker_color=color,
            marker_line=dict(color=color, width=0),
            text=[f"<b>{val:.0f}%</b>"],
            textposition="outside",
            textfont=dict(size=12, color="#1E293B"),
            showlegend=False,
            hovertemplate=f"<b>{y_label}</b>: {val:.0f}%<extra></extra>",
        ))

    p_up   = (row.get("p_moderate_up") or 0) + (row.get("p_strong_up") or 0)
    p_down = (row.get("p_strong_down") or 0) + (row.get("p_moderate_down") or 0)

    fig.update_layout(
        height=260,
        margin=dict(l=10, r=65, t=30, b=10),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        xaxis=dict(range=[0, 120], showgrid=False, showticklabels=False, zeroline=False),
        yaxis=dict(showgrid=False, tickfont=dict(size=11, color="#334155")),
        bargap=0.3,
        title=dict(
            text=f"📈 <b>{p_up:.0f}%</b> bullish   ➡ <b>{row.get('p_flat') or 0:.0f}%</b> flat   📉 <b>{p_down:.0f}%</b> bearish",
            font=dict(size=12, color="#475569"),
            x=0, xanchor="left",
        ),
    )
    return fig


def _plot_score_radar(row: dict) -> go.Figure:
    categories = ["Fundamentals", "Valuation", "Research", "Macro", "News Sentiment"]
    values = [
        row.get("fundamental_score") or 0,
        row.get("valuation_score") or 0,
        row.get("research_score") or 0,
        row.get("macro_score") or 0,
        row.get("news_score") or 0,
    ]
    cats_closed = categories + [categories[0]]
    vals_closed = values + [values[0]]

    fig = go.Figure()
    fig.add_trace(go.Scatterpolar(
        r=vals_closed,
        theta=cats_closed,
        fill="toself",
        fillcolor="rgba(37, 99, 235, 0.12)",
        line=dict(color="#2563EB", width=2),
        marker=dict(size=5, color="#2563EB"),
        hovertemplate="<b>%{theta}</b>: %{r:.1f}/10<extra></extra>",
    ))
    fig.update_layout(
        height=220,
        margin=dict(l=20, r=20, t=20, b=20),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        polar=dict(
            bgcolor="rgba(0,0,0,0)",
            radialaxis=dict(
                visible=True, range=[0, 10],
                tickfont=dict(size=8, color="#94A3B8"),
                gridcolor="#E2E8F0", linecolor="#E2E8F0",
            ),
            angularaxis=dict(
                tickfont=dict(size=10, color="#475569"),
                linecolor="#E2E8F0", gridcolor="#E2E8F0",
            ),
        ),
        showlegend=False,
    )
    return fig


def _plot_price_chart(ticker: str, period: str = "6mo") -> go.Figure | None:
    """Price + SMA overlay, volume, and RSI sub-panel — the pre-trade sanity check."""
    from plotly.subplots import make_subplots

    hist = _fetch_price_chart_data(ticker, period)
    if hist is None:
        return None

    fig = make_subplots(
        rows=3, cols=1, shared_xaxes=True,
        row_heights=[0.55, 0.15, 0.3], vertical_spacing=0.05,
        subplot_titles=("", "Volume", "RSI (14)"),
    )

    fig.add_trace(go.Scatter(x=hist.index, y=hist["Close"], name="Close",
                              line=dict(color=PRIMARY, width=2)), row=1, col=1)
    fig.add_trace(go.Scatter(x=hist.index, y=hist["sma20"], name="SMA20",
                              line=dict(color=WARNING, width=1, dash="dot")), row=1, col=1)
    fig.add_trace(go.Scatter(x=hist.index, y=hist["sma50"], name="SMA50",
                              line=dict(color=PURPLE, width=1, dash="dot")), row=1, col=1)
    fig.add_trace(go.Scatter(x=hist.index, y=hist["sma200"], name="SMA200",
                              line=dict(color=NEUTRAL, width=1, dash="dot")), row=1, col=1)

    vol_colors = [SUCCESS if c >= o else DANGER
                  for o, c in zip(hist["Open"], hist["Close"])]
    fig.add_trace(go.Bar(x=hist.index, y=hist["Volume"], name="Volume",
                          marker_color=vol_colors, showlegend=False), row=2, col=1)

    fig.add_trace(go.Scatter(x=hist.index, y=hist["rsi14"], name="RSI14",
                              line=dict(color=PRIMARY, width=1.5), showlegend=False), row=3, col=1)
    fig.add_hline(y=70, line_dash="dash", line_color=DANGER, line_width=1, row=3, col=1)
    fig.add_hline(y=30, line_dash="dash", line_color=SUCCESS, line_width=1, row=3, col=1)

    fig.update_layout(
        height=480,
        margin=dict(l=10, r=10, t=24, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        plot_bgcolor="white", paper_bgcolor="white",
        hovermode="x unified",
    )
    fig.update_yaxes(range=[0, 100], row=3, col=1)
    fig.update_xaxes(rangeslider_visible=False)
    return fig
