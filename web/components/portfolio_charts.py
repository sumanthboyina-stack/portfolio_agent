"""
Portfolio trend chart builder.

Contains:
  - build_portfolio_trend_chart : builds the portfolio value / cost-basis /
                                   per-ticker % change trend figure
"""

from __future__ import annotations

import sys
from pathlib import Path

import plotly.graph_objects as go

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from web.styles import ticker_label

PALETTE = [
    "#2563EB", "#059669", "#D97706", "#7C3AED",
    "#0891B2", "#DB2777", "#DC2626", "#65A30D", "#EA580C", "#6366F1",
]


def build_portfolio_trend_chart(
    total_dates, total_mv, total_cb, gain_abs, gain_pct_s,
    ticker_list, ticker_day,
) -> go.Figure:
    fig = go.Figure()

    # Trace 0 — portfolio market value area
    has_cb = any(v > 0 for v in total_cb)
    fig.add_trace(go.Scatter(
        x=total_dates, y=total_mv,
        name="Portfolio Value",
        mode="lines",
        line=dict(color="#2563EB", width=2.5, shape="spline", smoothing=0.4),
        fill="tozeroy",
        fillcolor="rgba(37,99,235,0.07)",
        customdata=list(zip(total_cb, gain_abs, gain_pct_s)),
        hovertemplate=(
            "<b>%{x}</b><br>"
            "Value: <b>$%{y:,.0f}</b><br>"
            "Cost:  $%{customdata[0]:,.0f}<br>"
            "P&L:   <b>$%{customdata[1]:+,.0f}  (%{customdata[2]:+.1f}%)</b>"
            "<extra></extra>"
        ),
        visible=True,
    ))

    # Trace 1 — cost basis reference (dotted)
    if has_cb:
        fig.add_trace(go.Scatter(
            x=total_dates, y=total_cb,
            name="Cost Basis",
            mode="lines",
            line=dict(color="#D1D5DB", width=1.5, dash="dot"),
            hoverinfo="skip",
            visible=True,
        ))
    n_total = 2 if has_cb else 1

    # Traces 2+ — per-ticker normalized % change
    for i, ticker in enumerate(ticker_list):
        day_map = ticker_day[ticker]
        dates = sorted(day_map)
        abs_vals = [day_map[d] for d in dates]
        base = abs_vals[0] if abs_vals else 1
        pct_vals = [(v - base) / base * 100 if base else 0 for v in abs_vals]
        fig.add_trace(go.Scatter(
            x=dates, y=pct_vals,
            name=ticker_label(ticker, max_len=24),
            mode="lines",
            line=dict(color=PALETTE[i % len(PALETTE)], width=2, shape="spline", smoothing=0.4),
            customdata=abs_vals,
            hovertemplate=(
                f"<b>{ticker_label(ticker, max_len=30)}</b>  %{{x}}<br>"
                "%{y:+.1f}%  ·  <b>$%{customdata:,.0f}</b>"
                "<extra></extra>"
            ),
            visible=False,
        ))

    n_ticker = len(ticker_list)
    vis_total  = [True]  * n_total + [False] * n_ticker
    vis_ticker = [False] * n_total + [True]  * n_ticker

    # ── Layout with in-chart controls ─────────────────────────────────────────
    fig.update_layout(
        height=440,
        margin=dict(l=0, r=0, t=52, b=0),
        paper_bgcolor="white",
        plot_bgcolor="white",
        font=dict(family="Inter, -apple-system, sans-serif", size=12),
        hovermode="x unified",
        hoverlabel=dict(
            bgcolor="white",
            bordercolor="#E5E7EB",
            font=dict(size=12, color="#111827", family="Inter, sans-serif"),
            namelength=-1,
        ),
        legend=dict(
            orientation="h",
            yanchor="top", y=-0.08,
            xanchor="left", x=0,
            font=dict(size=11, color="#374151"),
            bgcolor="rgba(0,0,0,0)",
            itemclick="toggle",
            itemdoubleclick="toggleothers",
        ),
        # In-chart toggle: Total Portfolio / Per Ticker
        updatemenus=[dict(
            type="buttons",
            direction="right",
            x=0, y=1.0,
            xanchor="left", yanchor="bottom",
            pad=dict(r=0, t=0, b=10),
            buttons=[
                dict(
                    label="Total Portfolio",
                    method="update",
                    args=[
                        {"visible": vis_total},
                        {
                            "yaxis.tickprefix": "$",
                            "yaxis.ticksuffix": "",
                            "yaxis.tickformat": ",.0f",
                            "yaxis.zeroline": False,
                            "hovermode": "x unified",
                        },
                    ],
                ),
                dict(
                    label="Per Ticker  (%)",
                    method="update",
                    args=[
                        {"visible": vis_ticker},
                        {
                            "yaxis.tickprefix": "",
                            "yaxis.ticksuffix": "%",
                            "yaxis.tickformat": "+.0f",
                            "yaxis.zeroline": True,
                            "hovermode": "closest",
                        },
                    ],
                ),
            ],
            bgcolor="#F9FAFB",
            bordercolor="#E5E7EB",
            borderwidth=1,
            font=dict(size=12, color="#374151"),
            showactive=True,
            active=0,
        )],
        xaxis=dict(
            showgrid=False,
            zeroline=False,
            tickfont=dict(size=11, color="#9CA3AF"),
            tickformat="%b %d, %Y",
            # Range selector top-right
            rangeselector=dict(
                buttons=[
                    dict(count=7,  label="1W", step="day",   stepmode="backward"),
                    dict(count=1,  label="1M", step="month", stepmode="backward"),
                    dict(count=3,  label="3M", step="month", stepmode="backward"),
                    dict(count=6,  label="6M", step="month", stepmode="backward"),
                    dict(step="all", label="All"),
                ],
                x=1, xanchor="right",
                y=1.0, yanchor="bottom",
                bgcolor="#F9FAFB",
                bordercolor="#E5E7EB",
                borderwidth=1,
                activecolor="#2563EB",
                font=dict(size=11, color="#374151"),
            ),
        ),
        yaxis=dict(
            showgrid=True,
            gridcolor="#F3F4F6",
            gridwidth=1,
            zeroline=False,
            zerolinecolor="#E5E7EB",
            zerolinewidth=1,
            tickprefix="$",
            tickfont=dict(size=11, color="#9CA3AF"),
            tickformat=",.0f",
            side="right",
        ),
    )

    return fig
