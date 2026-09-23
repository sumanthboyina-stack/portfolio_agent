"""Allocation charts: fixed-order colours, Other folding, sector resolution."""
from __future__ import annotations

from web.components.portfolio_charts import (
    MAX_SERIES, OTHER_COLOR, PALETTE, assign_colors, build_sector_pie, build_value_stack_chart, rank_by_latest_value,
)
from web.data.performance import build_trend_series
from web.data.portfolio import FUND_SECTOR, UNKNOWN_SECTOR, is_fund, sector_breakdown


def _rows(n_tickers: int, dates=("2026-09-01", "2026-09-02")) -> list[dict]:
    rows = []
    for i in range(n_tickers):
        t = f"T{i:02d}"
        for d in dates:
            rows.append({"date": d, "broker": "b", "account_number": "a", "ticker": t, "close_price": 10.0,
                         "shares": float(n_tickers - i), "market_value": 10.0 * (n_tickers - i), "cost_basis": 5.0, "source": "snapshot"})
    return rows


def test_value_stack_has_eight_hues_an_other_bucket_and_totals_on_top():
    series = build_trend_series(_rows(11))
    fig, legend = build_value_stack_chart(series, fund_tickers={"T01"})
    bars = [t for t in fig.data if t.type == "bar"]
    assert len(bars) == MAX_SERIES + 1 and fig.layout.barmode == "stack" and fig.layout.showlegend is False
    assert [b.marker.color for b in bars[:MAX_SERIES]] == PALETTE          # fixed order, largest first
    assert bars[-1].name.startswith("Other (3 positions)") and bars[-1].marker.color == OTHER_COLOR
    assert bars[1].marker.pattern.shape == "/" and bars[1].marker.pattern.bgcolor == PALETTE[1]   # funds hatched on their hue
    assert abs(sum(b.y[0] for b in bars) - series["value"][0]) < 1e-6      # stack totals the recorded value
    totals = [t for t in fig.data if t.type == "scatter"][0]
    assert totals.mode == "text" and totals.text[0] == "$660"              # 11 tickers × … at 10.0
    assert fig.layout.xaxis.type == "category"                             # evenly spaced trading days
    assert [it["color"] for it in legend] == PALETTE + [OTHER_COLOR] and legend[1]["fund"] is True
    # Many bars → no printed totals, and `days` trims to the last N trading days.
    many = build_trend_series(_rows(3, dates=tuple(f"2026-0{m}-{d:02d}" for m in (6, 7) for d in range(1, 29))))
    fig_all, _ = build_value_stack_chart(many)
    fig_5, _ = build_value_stack_chart(many, days=5)
    assert [t for t in fig_all.data if t.type == "scatter"][0].mode == "markers"
    assert len(fig_5.data[0].x) == 5 and len(fig_all.data[0].x) == 56


def test_line_views_keep_the_same_colour_per_ticker_as_the_stack():
    from web.components.portfolio_charts import VIEW_CHANGE, VIEW_GAIN, build_portfolio_trend_chart
    series = build_trend_series(_rows(11))
    fig = build_portfolio_trend_chart(series, {"^GSPC": {"2026-09-01": 0.0, "2026-09-02": 1.0}}, view=VIEW_CHANGE)
    lines = {t.name: t for t in fig.data}
    assert lines["T00"].line.color == PALETTE[0] and lines["T10"].line.color == OTHER_COLOR
    assert lines["T00"].visible == "legendonly" and "S&P 500" in lines
    gain = build_portfolio_trend_chart(series, view=VIEW_GAIN)
    assert len(gain.data) == 1 and gain.layout.yaxis.tickprefix == "$"


def test_rank_and_colour_assignment_never_cycle():
    ranked = rank_by_latest_value({"A": {"d": 1.0}, "B": {"d": 3.0}, "C": {"d": 2.0}})
    assert ranked == ["B", "C", "A"]
    colors = assign_colors([f"X{i}" for i in range(12)])
    assert list(colors.values())[:MAX_SERIES] == PALETTE and set(list(colors.values())[MAX_SERIES:]) == {OTHER_COLOR}


def test_sector_breakdown_resolves_sectors_and_labels_funds():
    holdings = [
        {"ticker": "AAPL", "current_value": 100.0, "sector": "", "description": "APPLE INC"},
        {"ticker": "XOM", "current_value": 50.0, "sector": "Energy", "description": "EXXON"},
        {"ticker": "VOO", "current_value": 30.0, "sector": None, "description": "VANGUARD S&P 500 INDEX ETF"},
        {"ticker": "GLD", "current_value": 20.0, "sector": None, "description": "SPDR GOLD TR GOLD SHS"},
        {"ticker": "ZZZ", "current_value": 10.0, "sector": None, "description": "MYSTERY CO"},
        {"ticker": "NOPX", "current_value": None, "sector": None, "description": "UNPRICED"},
    ]
    b = sector_breakdown(holdings, sectors={"AAPL": "Information Technology"})
    by = {r["sector"]: r for r in b["rows"]}
    assert b["total"] == 210.0 and b["unvalued"] == 1
    assert by["Information Technology"]["tickers"] == ["AAPL"] and by["Energy"]["pct"] == 23.81
    assert by[FUND_SECTOR]["tickers"] == ["GLD", "VOO"] and by[UNKNOWN_SECTOR]["tickers"] == ["ZZZ"]
    assert [r["sector"] for r in b["rows"]][0] == "Information Technology"          # sorted by value
    assert is_fund({"description": "ISHARES CORE S&P 500 ETF"}) and not is_fund({"description": "APPLE INC"})


def test_sector_pie_folds_small_sectors_into_other():
    rows = [{"sector": f"S{i}", "value": float(100 - i), "pct": 0.0, "tickers": [f"T{i}"]} for i in range(10)]
    fig = build_sector_pie({"total": sum(r["value"] for r in rows), "rows": rows, "unvalued": 0})
    pie = fig.data[0]
    assert len(pie.labels) == 8 and pie.labels[-1] == "Other (3 sectors)"
    assert list(pie.marker.colors) == PALETTE[:7] + [OTHER_COLOR] and pie.hole > 0.5
    assert abs(sum(pie.values) - sum(r["value"] for r in rows)) < 1e-9
