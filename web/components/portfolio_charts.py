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


INDEX_META = {
    "^GSPC": ("S&P 500", "#111827"),
    "^IXIC": ("Nasdaq",  "#0891B2"),
    "^DJI":  ("Dow Jones", "#D97706"),
}


def build_portfolio_trend_chart(
    total_dates, total_mv, total_cb, gain_abs, gain_pct_s,
    ticker_list, ticker_day,
    index_series: dict[str, dict[str, float]] | None = None,
) -> go.Figure:
    """
    index_series: {index_symbol: {date_iso: close}} for the same date range as
    total_dates, e.g. {"^GSPC": {...}, "^IXIC": {...}, "^DJI": {...}}. Each is
    normalized to % change from its own first value in range and added as a
    hidden trace shown only under the "vs Indices" toggle.
    """
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
        # Plotly's hovertemplate silently drops the whole format spec (falls
        # back to the raw float) whenever it includes a "+" sign flag on a
        # customdata field — so the sign-bearing P&L values are pre-formatted
        # in Python and passed as plain strings, referenced with no format spec.
        customdata=list(zip(
            total_cb,
            [f"{g:+,.2f}" for g in gain_abs],
            [f"{p:+.2f}" for p in gain_pct_s],
        )),
        hovertemplate=(
            "<b>%{x}</b><br>"
            "Value: <b>$%{y:,.2f}</b><br>"
            "Cost:  $%{customdata[0]:,.2f}<br>"
            "P&L:   <b>$%{customdata[1]}  (%{customdata[2]}%)</b>"
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
            # Sign-bearing % pre-formatted in Python (see note on trace 0) —
            # Plotly's hovertemplate drops a "+"-flagged format spec entirely.
            customdata=list(zip([f"{p:+.2f}" for p in pct_vals], abs_vals)),
            hovertemplate=(
                f"<b>{ticker_label(ticker, max_len=30)}</b>  %{{x}}<br>"
                "%{customdata[0]}%  ·  <b>$%{customdata[1]:,.2f}</b>"
                "<extra></extra>"
            ),
            visible=False,
        ))

    n_ticker = len(ticker_list)

    # Traces (n_total + n_ticker)+ — portfolio vs. index returns, all normalized
    # to % change from the first date in range so they're directly comparable.
    n_index = 0
    if index_series and total_mv:
        base_mv = total_mv[0] or 1
        port_pct = [(v - base_mv) / base_mv * 100 for v in total_mv]
        fig.add_trace(go.Scatter(
            x=total_dates, y=port_pct,
            name="Your Portfolio",
            mode="lines",
            line=dict(color="#2563EB", width=3, shape="spline", smoothing=0.4),
            # Pre-formatted in Python — see note on trace 0 about Plotly
            # dropping "+"-flagged format specs.
            customdata=[f"{p:+.2f}" for p in port_pct],
            hovertemplate="<b>Your Portfolio</b>  %{x}<br>%{customdata}%<extra></extra>",
            visible=False,
        ))
        n_index += 1
        for symbol, closes in index_series.items():
            if not closes:
                continue
            dates = sorted(closes)
            base_idx = closes[dates[0]] or 1
            pct_vals = [(closes[d] - base_idx) / base_idx * 100 for d in dates]
            label, color = INDEX_META.get(symbol, (symbol, "#6B7280"))
            fig.add_trace(go.Scatter(
                x=dates, y=pct_vals,
                name=label,
                mode="lines",
                line=dict(color=color, width=2, dash="dash"),
                customdata=[f"{p:+.2f}" for p in pct_vals],
                hovertemplate=f"<b>{label}</b>  %{{x}}<br>%{{customdata}}%<extra></extra>",
                visible=False,
            ))
            n_index += 1

    vis_total  = [True]  * n_total + [False] * n_ticker + [False] * n_index
    vis_ticker = [False] * n_total + [True]  * n_ticker + [False] * n_index
    vis_index  = [False] * n_total + [False] * n_ticker + [True]  * n_index

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
                            "yaxis.tickformat": ",.2f",
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
                            "yaxis.tickformat": "+.2f",
                            "yaxis.zeroline": True,
                            "hovermode": "closest",
                        },
                    ],
                ),
            ] + ([
                dict(
                    label="vs Indices  (%)",
                    method="update",
                    args=[
                        {"visible": vis_index},
                        {
                            "yaxis.tickprefix": "",
                            "yaxis.ticksuffix": "%",
                            "yaxis.tickformat": "+.2f",
                            "yaxis.zeroline": True,
                            "hovermode": "x unified",
                        },
                    ],
                ),
            ] if n_index else []),
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
            tickformat=",.2f",
            side="right",
        ),
    )

    return fig
