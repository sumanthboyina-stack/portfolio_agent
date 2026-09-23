"""
Portfolio chart renderers.

Render the series prepared by web.data.performance.build_trend_series — this
module decides colours, axes and labels, never financial methodology.

  build_value_stack_chart     Value ($): one stacked bar per trading day, one
                              segment per position (the 8 largest by latest
                              value get a hue, the rest fold into "Other"),
                              funds hatched, the day's total on top. Trading
                              days are evenly spaced (no weekend holes) and the
                              legend lives in a collapsed expander beside it.
  build_portfolio_trend_chart Line views: unrealized gain/loss ($), recorded
                              value change (%) per ticker and vs indices, and
                              the flow-adjusted investment return (%).
  build_sector_pie            Donut of value by sector with a table beside it.
"""

from __future__ import annotations

import sys
from datetime import date as _date
from pathlib import Path

import plotly.graph_objects as go

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from web.styles import ticker_label

# Validated categorical palette (light surface): 8 fixed slots, assigned by
# entity in fixed order and never cycled — a 9th entity folds into "Other".
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
OTHER_COLOR = "#9CA3AF"
MAX_SERIES = len(PALETTE)
_SURFACE = "white"
_FONT = dict(family="Inter, -apple-system, sans-serif", size=12)
_HOVER = dict(bgcolor="white", bordercolor="#E5E7EB", font=dict(size=12, color="#111827", family="Inter, sans-serif"),
              namelength=-1)

INDEX_META = {
    "^GSPC": ("S&P 500", "#111827"),
    "^IXIC": ("Nasdaq",  "#0891B2"),
    "^DJI":  ("Dow Jones", "#D97706"),
}
VIEW_GAIN, VIEW_CHANGE, VIEW_RETURN = "gain", "change", "return"
_LINE = dict(shape="spline", smoothing=0.4)


def _signed(vals, fmt="+.2f"):
    return [f"{v:{fmt}}" if v is not None else "" for v in vals]


def assign_colors(ranked: list[str]) -> dict[str, str]:
    """Fixed-order hue per entity for the first MAX_SERIES; everything after is 'Other' gray."""
    return {t: (PALETTE[i] if i < MAX_SERIES else OTHER_COLOR) for i, t in enumerate(ranked)}


def rank_by_latest_value(ticker_value: dict[str, dict[str, float]]) -> list[str]:
    """Tickers ordered by their most recent recorded value, largest first (ties by name)."""
    def latest(t):
        days = ticker_value[t]
        return days[max(days)] if days else 0.0
    return sorted(ticker_value, key=lambda t: (-latest(t), t))


def _short_date(d: str) -> str:
    try:
        return _date.fromisoformat(d).strftime("%b %d")
    except ValueError:
        return d


def _compact_money(v: float) -> str:
    if abs(v) >= 1_000_000:
        return f"${v / 1_000_000:.2f}M"
    if abs(v) >= 1_000:
        return f"${v / 1_000:.1f}k"
    return f"${v:,.0f}"


# ── Value ($): stacked bars ───────────────────────────────────────────────────

def build_value_stack_chart(series: dict, fund_tickers: set[str] | frozenset[str] = frozenset(),
                            days: int | None = None) -> tuple[go.Figure, list[dict]]:
    """
    Returns (figure, legend_items). legend_items = [{label, color, fund}] in
    stack order, for the caller to show in a collapsed legend. *days* keeps the
    last N trading days (None = all). Totals are printed above bars only while
    they cannot collide (≤ 31 bars).
    """
    ticker_value = series.get("ticker_value", {})
    all_dates = series["dates"]
    dates = all_dates[-days:] if days else all_dates
    totals = dict(zip(all_dates, series["value"]))
    ranked = rank_by_latest_value(ticker_value)
    color_of = assign_colors(ranked)
    head, rest = ranked[:MAX_SERIES], ranked[MAX_SERIES:]

    fig = go.Figure()
    legend: list[dict] = []

    def _pct(vals):
        return [f"{v / totals[d] * 100:.1f}" if totals.get(d) else "" for d, v in zip(dates, vals)]

    for t in head:
        vals = [ticker_value[t].get(d, 0.0) for d in dates]
        is_fund = t in fund_tickers
        fig.add_trace(go.Bar(
            x=dates, y=vals, name=ticker_label(t, max_len=22),
            marker=dict(color=color_of[t], line=dict(color=_SURFACE, width=0.5),
                        pattern=dict(shape="/" if is_fund else "", bgcolor=color_of[t], fgcolor=_SURFACE,
                                     fgopacity=0.55, size=7, solidity=0.3)),
            customdata=_pct(vals),
            hovertemplate="%{fullData.name}: <b>$%{y:,.0f}</b> (%{customdata}%)<extra></extra>",
        ))
        legend.append({"label": ticker_label(t, max_len=40), "color": color_of[t], "fund": is_fund})
    if rest:
        vals = [sum(ticker_value[t].get(d, 0.0) for t in rest) for d in dates]
        fig.add_trace(go.Bar(
            x=dates, y=vals, name=f"Other ({len(rest)} positions)",
            marker=dict(color=OTHER_COLOR, line=dict(color=_SURFACE, width=0.5)), customdata=_pct(vals),
            hovertemplate="%{fullData.name}: <b>$%{y:,.0f}</b> (%{customdata}%)<extra></extra>",
        ))
        legend.append({"label": f"Other — {', '.join(rest)}", "color": OTHER_COLOR, "fund": False})

    day_totals = [totals.get(d, 0.0) for d in dates]
    show_totals = len(dates) <= 31
    fig.add_trace(go.Scatter(     # total on top of each bar (and the headline line of the unified tooltip)
        x=dates, y=day_totals, mode="text" if show_totals else "markers", name="Total",
        text=[_compact_money(v) for v in day_totals] if show_totals else None, textposition="top center",
        textfont=dict(size=11, color="#111827", family="Inter, sans-serif"),
        marker=dict(size=0.1, color="rgba(0,0,0,0)"), cliponaxis=False, showlegend=False,
        hovertemplate="<b>Total $%{y:,.0f}</b><extra></extra>",
    ))

    step = max(1, len(dates) // 12)
    ymax = max(day_totals or [0.0])
    fig.update_layout(
        barmode="stack", bargap=0.18, showlegend=False, height=420,
        margin=dict(l=56, r=8, t=28, b=0), paper_bgcolor=_SURFACE, plot_bgcolor=_SURFACE, font=_FONT,
        hovermode="x unified", hoverlabel=_HOVER,
        xaxis=dict(type="category", tickvals=dates[::step], ticktext=[_short_date(d) for d in dates[::step]],
                   showgrid=False, tickfont=dict(size=11, color="#6B7280"), fixedrange=True),
        yaxis=dict(showgrid=True, gridcolor="#EEF2F7", zeroline=False, tickprefix="$", tickformat=".3~s",
                   tickfont=dict(size=11, color="#6B7280"), range=[0, ymax * 1.12 if ymax else 1], fixedrange=True),
    )
    return fig, legend


# ── Line views ────────────────────────────────────────────────────────────────

def build_portfolio_trend_chart(series: dict, index_pct: dict[str, dict[str, float]] | None = None,
                                view: str = VIEW_GAIN) -> go.Figure:
    """
    One line view of the prepared series:
      gain    unrealized gain/loss ($)
      change  % change of recorded value since the first date, per ticker (legend-only until clicked)
              and vs indices — moves when you buy or sell, so it is labelled as such, not a return
      return  flow-adjusted (time-weighted) investment return (%) vs indices
    A ticker keeps the same colour here as in the value stack.
    """
    dates = series["dates"]
    ticker_value = series.get("ticker_value", {})
    color_of = assign_colors(rank_by_latest_value(ticker_value))
    fig = go.Figure()
    pct_axis = view in (VIEW_CHANGE, VIEW_RETURN)

    if view == VIEW_GAIN:
        fig.add_trace(go.Scatter(
            x=dates, y=series["gain_abs"], name="Unrealized gain/loss", mode="lines",
            line=dict(color="#059669", width=2.5, **_LINE), fill="tozeroy", fillcolor="rgba(5,150,105,0.07)",
            customdata=_signed(series["gain_pct"]),
            hovertemplate="<b>%{x}</b><br>Unrealized: <b>$%{y:+,.2f}</b>  (%{customdata}% of cost)<extra></extra>",
        ))
    elif view == VIEW_CHANGE:
        fig.add_trace(go.Scatter(
            x=dates, y=series["recorded_change_pct"], name="Recorded value change (incl. buys/sells)",
            mode="lines", line=dict(color="#2563EB", width=3, **_LINE), customdata=_signed(series["recorded_change_pct"]),
            hovertemplate="<b>Recorded value</b>  %{x}<br>%{customdata}%<extra></extra>",
        ))
        for ticker in rank_by_latest_value(ticker_value):
            day_map = ticker_value[ticker]
            tds = sorted(day_map)
            vals = [day_map[d] for d in tds]
            base = vals[0] if vals and vals[0] else None
            pct = [((v - base) / base * 100) if base else 0.0 for v in vals]
            fig.add_trace(go.Scatter(
                x=tds, y=pct, name=ticker_label(ticker, max_len=24), mode="lines",
                line=dict(color=color_of[ticker], width=1.5, **_LINE), customdata=list(zip(_signed(pct), vals)),
                hovertemplate=(f"<b>{ticker_label(ticker, max_len=30)}</b>  %{{x}}<br>"
                               "%{customdata[0]}%  ·  $%{customdata[1]:,.2f}<extra></extra>"),
                visible="legendonly",
            ))
    elif view == VIEW_RETURN:
        fig.add_trace(go.Scatter(
            x=dates, y=series["investment_return_pct"], name="Investment return (flow-adjusted)",
            mode="lines", line=dict(color="#7C3AED", width=3, **_LINE), connectgaps=False,
            customdata=_signed(series["investment_return_pct"]),
            hovertemplate="<b>Investment return</b>  %{x}<br>%{customdata}%<extra></extra>",
        ))
    else:
        raise ValueError(f"unknown view {view!r}")

    if pct_axis:
        for symbol, pcts in (index_pct or {}).items():
            label, color = INDEX_META.get(symbol, (symbol, "#6B7280"))
            ds = sorted(pcts)
            fig.add_trace(go.Scatter(
                x=ds, y=[pcts[d] for d in ds], name=label, mode="lines",
                line=dict(color=color, width=2, dash="dash"), customdata=_signed([pcts[d] for d in ds]),
                hovertemplate=f"<b>{label}</b>  %{{x}}<br>%{{customdata}}%<extra></extra>",
            ))

    fig.update_layout(
        height=420, margin=dict(l=0, r=0, t=36, b=0), paper_bgcolor=_SURFACE, plot_bgcolor=_SURFACE, font=_FONT,
        hovermode="x unified", hoverlabel=_HOVER,
        legend=dict(orientation="h", yanchor="top", y=-0.08, xanchor="left", x=0, traceorder="normal",
                    font=dict(size=11, color="#374151"), bgcolor="rgba(0,0,0,0)",
                    itemclick="toggle", itemdoubleclick="toggleothers"),
        xaxis=dict(
            showgrid=False, zeroline=False, tickfont=dict(size=11, color="#9CA3AF"), tickformat="%b %d, %Y",
            rangeselector=dict(
                buttons=[dict(count=7, label="1W", step="day", stepmode="backward"),
                         dict(count=1, label="1M", step="month", stepmode="backward"),
                         dict(count=3, label="3M", step="month", stepmode="backward"),
                         dict(count=6, label="6M", step="month", stepmode="backward"),
                         dict(step="all", label="All")],
                x=1, xanchor="right", y=1.0, yanchor="bottom", bgcolor="#F9FAFB", bordercolor="#E5E7EB",
                borderwidth=1, activecolor="#2563EB", font=dict(size=11, color="#374151"),
            ),
        ),
        yaxis=dict(showgrid=True, gridcolor="#EEF2F7", gridwidth=1, zeroline=pct_axis, zerolinecolor="#E5E7EB",
                   zerolinewidth=1, tickprefix="" if pct_axis else "$", ticksuffix="%" if pct_axis else "",
                   tickformat="+.2f" if pct_axis else ",.0f", tickfont=dict(size=11, color="#9CA3AF"), side="left"),
    )
    return fig


# ── Sector donut ──────────────────────────────────────────────────────────────

def build_sector_pie(breakdown: dict, max_slices: int = 7) -> go.Figure:
    """
    Donut of value by sector from web.data.portfolio.sector_breakdown(). Sectors
    beyond *max_slices* fold into a neutral "Other" so no more than eight hues
    carry meaning; slices of 3%+ are direct-labelled with their share, and the
    caller shows the same numbers as a table beside it.
    """
    rows = breakdown["rows"]
    head, tail = rows[:max_slices], rows[max_slices:]
    labels = [r["sector"] for r in head]
    values = [r["value"] for r in head]
    tickers = [", ".join(r["tickers"]) for r in head]
    colors = [PALETTE[i] for i in range(len(head))]
    if tail:
        labels.append(f"Other ({len(tail)} sectors)")
        values.append(round(sum(r["value"] for r in tail), 2))
        tickers.append(", ".join(t for r in tail for t in r["tickers"]))
        colors.append(OTHER_COLOR)
    total_v = sum(values) or 1.0
    positions = ["outside" if v / total_v >= 0.03 else "none" for v in values]
    fig = go.Figure(go.Pie(
        labels=labels, values=values, hole=0.58, sort=False, direction="clockwise", rotation=0,
        marker=dict(colors=colors, line=dict(color=_SURFACE, width=2)),
        texttemplate="%{label}<br>%{percent:.1%}", textposition=positions, textfont=dict(size=11, color="#374151"),
        customdata=tickers,
        hovertemplate="<b>%{label}</b><br>$%{value:,.2f}  ·  %{percent:.1%}<br><span style='color:#6B7280'>%{customdata}</span><extra></extra>",
    ))
    total = breakdown["total"]
    fig.add_annotation(text=f"<b>${total:,.0f}</b><br><span style='font-size:11px;color:#6B7280'>securities</span>",
                       x=0.5, y=0.5, showarrow=False, font=dict(size=15, color="#111827"))
    fig.update_layout(
        height=380, margin=dict(l=8, r=8, t=28, b=28), paper_bgcolor=_SURFACE, plot_bgcolor=_SURFACE,
        font=_FONT, showlegend=False, hoverlabel=_HOVER, uniformtext=dict(minsize=10, mode="hide"),
    )
    return fig
