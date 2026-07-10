"""Database viewer — browse pipeline output by date."""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st
from st_aggrid import AgGrid, GridOptionsBuilder, GridUpdateMode, DataReturnMode, JsCode

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from web.styles import (
    inject_global_css, page_header, section_title, top_nav,
    SUCCESS, WARNING, DANGER, PRIMARY, NEUTRAL, REC_STYLES,
)

_DB    = _ROOT / "data" / "portfolio.db"
_TODAY = date.today().isoformat()


def _available_dates() -> list[str]:
    """Return all distinct dates that have data across the main tables, newest first."""
    if not _DB.exists():
        return [_TODAY]
    queries = [
        "SELECT DISTINCT DATE(as_of_date)   FROM fundamentals",
        "SELECT DISTINCT DATE(as_of_date)     FROM news_daily_update",
        "SELECT DISTINCT DATE(as_of_date)     FROM research",
        "SELECT DISTINCT DATE(as_of_date)    FROM predictions",
        "SELECT DISTINCT DATE(as_of_date)    FROM holdings",
        "SELECT DISTINCT DATE(metric_date)   FROM metrics_rolling",
    ]
    dates: set[str] = set()
    try:
        c = sqlite3.connect(str(_DB))
        for q in queries:
            try:
                for row in c.execute(q).fetchall():
                    if row[0]:
                        dates.add(row[0])
            except Exception:
                pass
        c.close()
    except Exception:
        pass
    if not dates:
        return [_TODAY]
    return sorted(dates, reverse=True)

st.set_page_config(
    page_title="Today's Data — Portfolio Intelligence",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)
inject_global_css()
top_nav("database")

if not _DB.exists():
    page_header("Database", icon="📊")
    st.warning("Database not found. Run `python main.py --daily` to initialise it.", icon="⚠️")
    st.stop()

_avail_dates = _available_dates()
_min_date = date.fromisoformat(_avail_dates[-1]) if _avail_dates else date.today()
_max_date = date.today()

# ── Session-state defaults for date range ─────────────────────────────────────
if "db_date_from" not in st.session_state:
    st.session_state.db_date_from = date.today()
if "db_date_to" not in st.session_state:
    st.session_state.db_date_to = date.today()

with st.sidebar:
    st.markdown(
        '<p style="font-size:0.65rem;font-weight:700;text-transform:uppercase;'
        'letter-spacing:0.1em;color:#4B5563;margin:0 0 12px">Database</p>',
        unsafe_allow_html=True,
    )

    # Quick-select preset buttons
    st.markdown(
        '<p style="font-size:0.72rem;font-weight:600;color:#9CA3AF;margin:0 0 6px">'
        'Quick select</p>',
        unsafe_allow_html=True,
    )
    _qc1, _qc2 = st.columns(2)
    with _qc1:
        if st.button("Today",   use_container_width=True, key="qsel_today", type="primary"):
            st.session_state.db_date_from = date.today()
            st.session_state.db_date_to   = date.today()
            st.rerun()
        if st.button("30 days", use_container_width=True, key="qsel_30", type="primary"):
            st.session_state.db_date_from = date.today() - timedelta(days=29)
            st.session_state.db_date_to   = date.today()
            st.rerun()
    with _qc2:
        if st.button("7 days",  use_container_width=True, key="qsel_7", type="primary"):
            st.session_state.db_date_from = date.today() - timedelta(days=6)
            st.session_state.db_date_to   = date.today()
            st.rerun()
        if st.button("All time",use_container_width=True, key="qsel_all", type="primary"):
            st.session_state.db_date_from = _min_date
            st.session_state.db_date_to   = date.today()
            st.rerun()

    st.markdown(
        '<p style="font-size:0.72rem;font-weight:600;color:#9CA3AF;margin:10px 0 4px">'
        'Custom range</p>',
        unsafe_allow_html=True,
    )
    _range_val = st.date_input(
        "date_range",
        value=(st.session_state.db_date_from, st.session_state.db_date_to),
        min_value=_min_date,
        max_value=_max_date,
        label_visibility="collapsed",
        key="db_date_range",
    )
    # date_input returns a tuple when range is complete, single date while selecting
    if isinstance(_range_val, (list, tuple)) and len(_range_val) == 2:
        _DATE_FROM = _range_val[0].isoformat()
        _DATE_TO   = _range_val[1].isoformat()
        st.session_state.db_date_from = _range_val[0]
        st.session_state.db_date_to   = _range_val[1]
    else:
        # Mid-selection — keep previous range
        _DATE_FROM = st.session_state.db_date_from.isoformat()
        _DATE_TO   = st.session_state.db_date_to.isoformat()

    _is_single_day = _DATE_FROM == _DATE_TO
    _is_today_only = _DATE_FROM == _TODAY and _DATE_TO == _TODAY

    st.divider()
    _span_label = "today" if _is_today_only else (
        _DATE_FROM if _is_single_day else f"{_DATE_FROM}  →  {_DATE_TO}"
    )
    st.markdown(
        f'<p style="font-size:0.7rem;color:#6B7280;line-height:1.5">'
        f'{len(_avail_dates)} date{"s" if len(_avail_dates) != 1 else ""} with data<br>'
        f'<span style="color:#9CA3AF">{_span_label}</span></p>',
        unsafe_allow_html=True,
    )



# ── Helpers ───────────────────────────────────────────────────────────────────

def _to_local(utc_str: str) -> str:
    try:
        return datetime.fromisoformat(utc_str).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return (utc_str or "")[:19].replace("T", " ")


from web.lib import db_conn as _conn


def _count_for(table: str, date_col: str, date_from: str, date_to: str,
               extra_where: str = "") -> int:
    """Count rows between date_from and date_to (inclusive) in a table."""
    try:
        where = f"DATE({date_col}) BETWEEN ? AND ?"
        if extra_where:
            where = f"{where} AND {extra_where}"
        with _conn() as c:
            return c.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {where}", [date_from, date_to]
            ).fetchone()[0]
    except Exception:
        return 0


_HIGHER_MODELS    = ('Claude-Sonnet', 'GPT-4o', 'Claude-Sonnet + GPT-4o')
_HIGHER_PROVIDERS = ('anthropic', 'openai')


def _model_tier_sql(tier: str,
                    name_col: str = "model_name",
                    prov_col: str = "model_provider") -> tuple[str, list]:
    """Returns (WHERE fragment, params) for model tier filtering on predictions table."""
    if tier == "Higher reasoning":
        ph = ','.join('?' * len(_HIGHER_MODELS))
        return (f"({name_col} IN ({ph}) OR {prov_col} IN (?,?))",
                list(_HIGHER_MODELS) + list(_HIGHER_PROVIDERS))
    if tier == "Lower reasoning":
        ph = ','.join('?' * len(_HIGHER_MODELS))
        return (f"NOT ({name_col} IN ({ph}) OR {prov_col} IN (?,?))",
                list(_HIGHER_MODELS) + list(_HIGHER_PROVIDERS))
    return "1=1", []


def _pj(v, default=None):
    try:
        return json.loads(v) if v else default
    except Exception:
        return default


def _score_color(score) -> str:
    try:
        s = float(score)
        return "🟢" if s >= 7 else ("🟡" if s >= 4 else "🔴")
    except Exception:
        return "⚪"


def _detail_card(label: str, items: list[tuple[str, str]]) -> None:
    html = (
        f'<div style="background:#F8FAFC;border:1px solid #E2E8F0;border-radius:10px;'
        f'padding:14px 18px;margin-bottom:10px">'
        f'<div style="font-weight:700;color:#0F172A;margin-bottom:8px;font-size:0.9rem">{label}</div>'
    )
    for k, v in items:
        html += (
            f'<div style="display:flex;justify-content:space-between;padding:3px 0;'
            f'border-bottom:1px solid #F1F5F9">'
            f'<span style="color:#64748B;font-size:0.82rem">{k}</span>'
            f'<span style="font-weight:600;font-size:0.82rem;color:#0F172A">{v}</span>'
            f'</div>'
        )
    html += '</div>'
    st.markdown(html, unsafe_allow_html=True)


def _aggrid(df: pd.DataFrame, col_defs: list[dict], height: int = 420,
            key: str = "grid") -> list[dict]:
    gb = GridOptionsBuilder.from_dataframe(df)
    gb.configure_default_column(
        filter=True,
        sortable=True,
        resizable=True,
        floatingFilter=True,
        filterParams={"buttons": ["reset"]},
    )
    gb.configure_selection("single", use_checkbox=False, pre_selected_rows=[])
    gb.configure_grid_options(suppressRowClickSelection=False, rowHeight=32, headerHeight=40)

    for cd in col_defs:
        field = cd.pop("field")
        gb.configure_column(field, **cd)

    go = gb.build()

    resp = AgGrid(
        df,
        gridOptions=go,
        update_mode=GridUpdateMode.SELECTION_CHANGED,
        data_return_mode=DataReturnMode.FILTERED_AND_SORTED,
        fit_columns_on_grid_load=True,
        enable_enterprise_modules=False,
        theme="streamlit",
        height=height,
        key=key,
        allow_unsafe_jscode=True,
    )
    sel = resp.get("selected_rows")
    if sel is None:
        return []
    if isinstance(sel, pd.DataFrame):
        return sel.to_dict("records")
    return list(sel) if sel else []


# ── Counts for selected range ─────────────────────────────────────────────────
cnt_fund = _count_for("fundamentals",      "as_of_date",     _DATE_FROM, _DATE_TO)
cnt_news = _count_for("news_daily_update", "as_of_date",     _DATE_FROM, _DATE_TO, extra_where="row_type = 'ticker'")
cnt_res  = _count_for("research",          "as_of_date", _DATE_FROM, _DATE_TO)
cnt_pred = _count_for("predictions",       "as_of_date",     _DATE_FROM, _DATE_TO)
cnt_hold = _count_for("holdings",          "as_of_date",     _DATE_FROM, _DATE_TO)
cnt_metr = _count_for("metrics_rolling",   "metric_date",    _DATE_FROM, _DATE_TO)

if _is_single_day:
    _date_display = datetime.strptime(_DATE_FROM, "%Y-%m-%d").strftime("%B %d, %Y")
    if _is_today_only:
        _date_display += "  · today"
else:
    _df = datetime.strptime(_DATE_FROM, "%Y-%m-%d").strftime("%b %d")
    _dt = datetime.strptime(_DATE_TO,   "%Y-%m-%d").strftime("%b %d, %Y")
    _date_display = f"{_df} – {_dt}"

page_header(
    "Database",
    subtitle=(
        f"{_date_display}  ·  "
        f"{cnt_fund} fundamentals · {cnt_news} news · "
        f"{cnt_res} research · {cnt_pred} predictions"
    ),
    icon="📊",
)

tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs([
    f"📋 Fundamentals ({cnt_fund})",
    f"📰 News ({cnt_news})",
    f"🔬 Research ({cnt_res})",
    f"🤖 Predictions ({cnt_pred})",
    f"💼 Holdings ({cnt_hold})",
    f"📈 Validation ({cnt_metr})",
    "⚡ Events",
])


# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — FUNDAMENTALS
# ══════════════════════════════════════════════════════════════════════════════
with tab1:
    try:
        with _conn() as c:
            rows = c.execute(
                """SELECT ticker, as_of_date, filing_type, filing_date,
                           revenue_growth_yoy_pct, net_margin, fcf, debt_to_equity,
                           fundamental_score, summary, key_strengths, key_risks,
                           model_name, model_provider
                   FROM fundamentals
                   WHERE DATE(as_of_date) BETWEEN ? AND ?
                   ORDER BY ticker""",
                [_DATE_FROM, _DATE_TO],
            ).fetchall()
    except Exception as exc:
        st.error(f"Query error: {exc}"); rows = []

    raw_df = pd.DataFrame([dict(r) for r in rows]) if rows else pd.DataFrame()

    if raw_df.empty:
        st.info(f"No fundamentals data between {_DATE_FROM} and {_DATE_TO}.", icon="💡")
    else:
        df = raw_df.copy()
        df["revenue_growth_yoy_pct"] = pd.to_numeric(df["revenue_growth_yoy_pct"], errors="coerce").round(1)
        df["net_margin"]             = pd.to_numeric(df["net_margin"],             errors="coerce").round(1)
        df["fcf"]                    = pd.to_numeric(df["fcf"],                    errors="coerce").round(0)
        df["debt_to_equity"]         = pd.to_numeric(df["debt_to_equity"],         errors="coerce").round(2)
        df["fundamental_score"]      = pd.to_numeric(df["fundamental_score"],      errors="coerce").round(1)

        df["top_strength"] = df["key_strengths"].apply(
            lambda v: (_pj(v, []) or [""])[0][:60] if v else ""
        )
        df["top_risk"] = df["key_risks"].apply(
            lambda v: (_pj(v, []) or [""])[0][:60] if v else ""
        )

        grid_df = df[[
            "as_of_date", "ticker", "filing_type", "filing_date",
            "revenue_growth_yoy_pct", "net_margin", "fcf", "debt_to_equity",
            "fundamental_score", "top_strength", "top_risk",
            "model_name", "model_provider",
        ]].rename(columns={
            "ticker":                 "Ticker",
            "as_of_date":             "As Of Date",
            "filing_type":            "Filing",
            "filing_date":            "Filing Date",
            "revenue_growth_yoy_pct": "Rev Gth %",
            "net_margin":             "Net Margin %",
            "fcf":                    "FCF ($)",
            "debt_to_equity":         "D/E",
            "fundamental_score":      "Score",
            "top_strength":           "Top Strength",
            "top_risk":               "Top Risk",
            "model_name":             "Model",
            "model_provider":         "Provider",
        })

        section_title("Grid", badge_text=f"{len(df)} rows", badge_color=PRIMARY)
        st.caption("Click any column header to sort · Use the filter row beneath each header to filter · Click a row to see details below")

        col_defs = [
            {"field": "As Of Date",        "width": 110, "filter": "agDateColumnFilter",   "pinned": "left"},
            {"field": "Ticker",       "width": 90,  "filter": "agTextColumnFilter",   "pinned": "left"},
            {"field": "Filing",       "width": 80,  "filter": "agTextColumnFilter"},
            {"field": "Filing Date",  "width": 110, "filter": "agDateColumnFilter"},
            {"field": "Rev Gth %",    "width": 100, "filter": "agNumberColumnFilter", "valueFormatter": "value != null ? value.toFixed(1) + '%' : '—'"},
            {"field": "Net Margin %", "width": 110, "filter": "agNumberColumnFilter", "valueFormatter": "value != null ? value.toFixed(1) + '%' : '—'"},
            {"field": "FCF ($)",      "width": 110, "filter": "agNumberColumnFilter", "valueFormatter": "value != null ? '$' + (value/1e9).toFixed(2) + 'B' : '—'"},
            {"field": "D/E",          "width": 80,  "filter": "agNumberColumnFilter", "valueFormatter": "value != null ? value.toFixed(2) : '—'"},
            {"field": "Score",        "width": 80,  "filter": "agNumberColumnFilter"},
            {"field": "Top Strength", "width": 220, "filter": "agTextColumnFilter"},
            {"field": "Top Risk",     "width": 220, "filter": "agTextColumnFilter"},
            {"field": "Model",        "width": 160, "filter": "agTextColumnFilter"},
            {"field": "Provider",     "width": 110, "filter": "agTextColumnFilter"},
        ]

        selected = _aggrid(grid_df, col_defs, height=440, key="fund_grid")

        if selected:
            hit = selected[0]
            ticker = hit.get("Ticker", "")
            row_matches = raw_df[raw_df["ticker"] == ticker]
            if not row_matches.empty:
                row = row_matches.iloc[0]
                st.divider()
                section_title(f"Detail — {ticker}", badge_text=row.get('filing_type',''), badge_color=PRIMARY)
                dc1, dc2 = st.columns(2)
                with dc1:
                    _detail_card("Financials", [
                        ("Revenue Growth", f"{row.get('revenue_growth_yoy_pct') or '—'}%"),
                        ("Net Margin",     f"{row.get('net_margin') or '—'}%"),
                        ("FCF",            f"${(row.get('fcf') or 0)/1e9:.2f}B" if row.get('fcf') else "—"),
                        ("Debt/Equity",    f"{row.get('debt_to_equity') or '—'}"),
                        ("Score",          f"{_score_color(row.get('fundamental_score'))} {row.get('fundamental_score') or '—'}/10"),
                        ("As Of Date",          str(row.get('as_of_date') or '—')),
                        ("Filing Date",    str(row.get('filing_date') or '—')),
                    ])
                with dc2:
                    strengths = _pj(row.get("key_strengths"), [])
                    risks     = _pj(row.get("key_risks"), [])
                    if strengths:
                        st.markdown("**✅ Key Strengths**")
                        for s in strengths[:5]: st.markdown(f"- {s}")
                    if risks:
                        st.markdown("**⚠️ Key Risks**")
                        for r in risks[:5]: st.markdown(f"- {r}")
                if row.get("summary"):
                    st.markdown(f"> {row['summary']}")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — NEWS
# ══════════════════════════════════════════════════════════════════════════════
with tab2:
    try:
        with _conn() as c:
            _news_cols = {r[1] for r in c.execute("PRAGMA table_info(news_daily_update)").fetchall()}
            _model_sel = ", model_name, model_provider" if "model_name" in _news_cols else ", NULL as model_name, NULL as model_provider"
            rows = c.execute(
                f"""SELECT as_of_date, ticker, sentiment, sentiment_score,
                           headline_1, headline_2, top_themes, trending,
                           source, impacted_tickers
                           {_model_sel}
                   FROM news_daily_update
                   WHERE row_type = 'ticker' AND DATE(as_of_date) BETWEEN ? AND ?
                   ORDER BY ticker""",
                [_DATE_FROM, _DATE_TO],
            ).fetchall()
    except Exception as exc:
        st.error(f"Query error: {exc}"); rows = []

    raw_df = pd.DataFrame([dict(r) for r in rows]) if rows else pd.DataFrame()

    if raw_df.empty:
        st.info(f"No news records between {_DATE_FROM} and {_DATE_TO}.", icon="📰")
    else:
        df = raw_df.copy()
        df["sentiment_score"] = pd.to_numeric(df["sentiment_score"], errors="coerce").round(3)
        df["themes_str"] = df["top_themes"].apply(
            lambda v: ", ".join(_pj(v, [])[:4]) if v else ""
        )

        grid_df = df[[
            "as_of_date", "ticker", "sentiment", "sentiment_score",
            "headline_1", "headline_2", "themes_str", "trending",
            "source", "impacted_tickers",
            "model_name", "model_provider",
        ]].rename(columns={
            "as_of_date":        "As Of Date",
            "ticker":            "Ticker",
            "sentiment":         "Sentiment",
            "sentiment_score":   "Score",
            "headline_1":        "Headline 1",
            "headline_2":        "Headline 2",
            "themes_str":        "Themes",
            "trending":          "Trending",
            "source":            "Source",
            "impacted_tickers":  "Impacted",
            "model_name":        "Model",
            "model_provider":    "Provider",
        })

        section_title("Grid", badge_text=f"{len(df)} rows", badge_color=PRIMARY)
        st.caption("Inline filter row below each header · Click row for details")

        col_defs = [
            {"field": "As Of Date",      "width": 110, "filter": "agDateColumnFilter",   "pinned": "left"},
            {"field": "Ticker",     "width": 90,  "filter": "agTextColumnFilter",   "pinned": "left"},
            {"field": "Sentiment",  "width": 110, "filter": "agTextColumnFilter"},
            {"field": "Score",      "width": 90,  "filter": "agNumberColumnFilter", "valueFormatter": "value != null ? value.toFixed(3) : '—'"},
            {"field": "Headline 1", "width": 280, "filter": "agTextColumnFilter"},
            {"field": "Headline 2", "width": 280, "filter": "agTextColumnFilter"},
            {"field": "Themes",     "width": 200, "filter": "agTextColumnFilter"},
            {"field": "Trending",   "width": 200, "filter": "agTextColumnFilter"},
            {"field": "Source",     "width": 130, "filter": "agTextColumnFilter"},
            {"field": "Impacted",   "width": 140, "filter": "agTextColumnFilter"},
            {"field": "Model",      "width": 160, "filter": "agTextColumnFilter"},
            {"field": "Provider",   "width": 110, "filter": "agTextColumnFilter"},
        ]

        selected = _aggrid(grid_df, col_defs, height=440, key="news_grid")

        if selected:
            hit = selected[0]
            ticker  = hit.get("Ticker", "")
            dt      = hit.get("As Of Date", "")
            row_matches = raw_df[(raw_df["ticker"] == ticker) & (raw_df["as_of_date"] == dt)]
            if not row_matches.empty:
                row = row_matches.iloc[0]
                st.divider()
                section_title(f"Detail — {ticker}  {dt}", badge_color=PRIMARY)
                sent = (row.get("sentiment") or "").upper()
                sent_col = SUCCESS if sent == "POSITIVE" else (DANGER if sent == "NEGATIVE" else NEUTRAL)
                score = row.get("sentiment_score")
                st.markdown(
                    f'<span style="background:{sent_col};color:white;padding:3px 10px;'
                    f'border-radius:12px;font-size:0.8rem;font-weight:700">{sent}</span>'
                    f'&nbsp; Score: <strong>{f"{score:+.3f}" if score is not None else "—"}</strong>',
                    unsafe_allow_html=True,
                )
                if row.get("headline_1"): st.markdown(f"**►** {row['headline_1']}")
                if row.get("headline_2"): st.markdown(f"**►** {row['headline_2']}")
                if row.get("trending"):   st.info(row["trending"])
                themes = _pj(row.get("top_themes"), [])
                if themes:
                    st.markdown("**Themes:** " + "  ·  ".join(f"`{t}`" for t in themes[:8]))
                meta_parts = []
                if row.get("source"):            meta_parts.append(f"📡 Source: **{row['source']}**")
                if row.get("impacted_tickers"):  meta_parts.append(f"🎯 Impacted: **{row['impacted_tickers']}**")
                if meta_parts:
                    st.markdown("  ·  ".join(meta_parts))
                model = row.get("model_name")
                if model:
                    st.caption(f"🤖 Model: {model}  ·  Provider: {row.get('model_provider','—')}")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — RESEARCH
# ══════════════════════════════════════════════════════════════════════════════
with tab3:
    try:
        with _conn() as c:
            rows = c.execute(
                """SELECT ticker, as_of_date, consensus, rec_trend, consensus_mean,
                           num_analysts, price_target_avg, price_target_median,
                           price_target_high, price_target_low,
                           current_price, upside_to_mean_pct,
                           latest_upgrade_date, research_score,
                           highlights, summary, last_llm_run_date,
                           model_name, model_provider
                   FROM research
                   WHERE DATE(as_of_date) BETWEEN ? AND ?
                   ORDER BY research_score DESC NULLS LAST, ticker""",
                [_DATE_FROM, _DATE_TO],
            ).fetchall()
    except Exception as exc:
        st.error(f"Query error: {exc}"); rows = []

    raw_df = pd.DataFrame([dict(r) for r in rows]) if rows else pd.DataFrame()

    if raw_df.empty:
        st.info(f"No research data between {_DATE_FROM} and {_DATE_TO}.", icon="🔬")
    else:
        df = raw_df.copy()
        for col in ["consensus_mean","num_analysts","price_target_avg","price_target_high",
                    "price_target_low","current_price","upside_to_mean_pct","research_score"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["num_analysts"]   = df["num_analysts"].round(0)
        df["research_score"] = df["research_score"].round(1)
        df["upside_to_mean_pct"] = df["upside_to_mean_pct"].round(1)
        for c in ["price_target_avg","price_target_high","price_target_low","current_price"]:
            df[c] = df[c].round(2)

        for col in ["rec_trend", "price_target_median"]:
            if col not in df.columns:
                df[col] = None
        df["price_target_median"] = pd.to_numeric(df.get("price_target_median"), errors="coerce").round(2)

        grid_df = df[[
            "as_of_date", "ticker", "consensus", "rec_trend", "consensus_mean",
            "num_analysts", "price_target_avg", "price_target_median",
            "price_target_high", "price_target_low",
            "current_price", "upside_to_mean_pct", "latest_upgrade_date",
            "research_score", "model_name", "model_provider",
        ]].rename(columns={
            "ticker":               "Ticker",
            "as_of_date":           "As Of Date",
            "consensus":            "Consensus",
            "rec_trend":            "Rec Trend",
            "consensus_mean":       "Mean Rtg",
            "num_analysts":         "Analysts",
            "price_target_avg":     "Target Avg",
            "price_target_median":  "Target Med",
            "price_target_high":    "Target High",
            "price_target_low":     "Target Low",
            "current_price":        "Price",
            "upside_to_mean_pct":   "Upside %",
            "latest_upgrade_date":  "Last Upgrade",
            "research_score":       "Score",
            "model_name":           "Model",
            "model_provider":       "Provider",
        })

        section_title("Grid", badge_text=f"{len(df)} rows", badge_color=PRIMARY)
        st.caption("Inline filter row below each header · Click row for details")

        col_defs = [
            {"field": "As Of Date",    "width": 110, "filter": "agDateColumnFilter",   "pinned": "left"},
            {"field": "Ticker",        "width": 90,  "filter": "agTextColumnFilter",   "pinned": "left"},
            {"field": "Consensus",     "width": 110, "filter": "agTextColumnFilter"},
            {"field": "Rec Trend",     "width": 105, "filter": "agTextColumnFilter"},
            {"field": "Mean Rtg",      "width": 90,  "filter": "agNumberColumnFilter", "valueFormatter": "value != null ? value.toFixed(1) : '—'"},
            {"field": "Analysts",      "width": 85,  "filter": "agNumberColumnFilter", "valueFormatter": "value != null ? Math.round(value) : '—'"},
            {"field": "Target Avg",    "width": 100, "filter": "agNumberColumnFilter", "valueFormatter": "value != null ? '$' + value.toFixed(2) : '—'"},
            {"field": "Target Med",    "width": 100, "filter": "agNumberColumnFilter", "valueFormatter": "value != null ? '$' + value.toFixed(2) : '—'"},
            {"field": "Target High",   "width": 105, "filter": "agNumberColumnFilter", "valueFormatter": "value != null ? '$' + value.toFixed(2) : '—'"},
            {"field": "Target Low",    "width": 100, "filter": "agNumberColumnFilter", "valueFormatter": "value != null ? '$' + value.toFixed(2) : '—'"},
            {"field": "Price",         "width": 90,  "filter": "agNumberColumnFilter", "valueFormatter": "value != null ? '$' + value.toFixed(2) : '—'"},
            {"field": "Upside %",      "width": 90,  "filter": "agNumberColumnFilter", "valueFormatter": "value != null ? value.toFixed(1) + '%' : '—'"},
            {"field": "Last Upgrade",  "width": 110, "filter": "agDateColumnFilter"},
            {"field": "Score",         "width": 80,  "filter": "agNumberColumnFilter"},
            {"field": "Model",         "width": 160, "filter": "agTextColumnFilter"},
            {"field": "Provider",      "width": 110, "filter": "agTextColumnFilter"},
        ]

        selected = _aggrid(grid_df, col_defs, height=440, key="res_grid")

        if selected:
            hit = selected[0]
            ticker = hit.get("Ticker", "")
            row_matches = raw_df[raw_df["ticker"] == ticker]
            if not row_matches.empty:
                row = row_matches.iloc[0]
                st.divider()
                section_title(f"Detail — {ticker}", badge_color=PRIMARY)
                dc1, dc2 = st.columns(2)
                with dc1:
                    _detail_card("Broker Data", [
                        ("Consensus",    (row.get("consensus") or "—").upper().replace("_"," ")),
                        ("Rec Trend",    str(row.get("rec_trend") or "—")),
                        ("Mean Rating",  f"{row.get('consensus_mean') or '—'}"),
                        ("# Analysts",   str(int(row.get("num_analysts") or 0) or "—")),
                        ("Target Avg",   f"${row.get('price_target_avg') or '—'}"),
                        ("Target Median",f"${row.get('price_target_median') or '—'}"),
                        ("Target High",  f"${row.get('price_target_high') or '—'}"),
                        ("Target Low",   f"${row.get('price_target_low') or '—'}"),
                        ("Current",      f"${row.get('current_price') or '—'}"),
                        ("Upside",       f"{row.get('upside_to_mean_pct') or '—'}%"),
                        ("Last Upgrade", str(row.get('latest_upgrade_date') or '—')),
                        ("Score",        f"{_score_color(row.get('research_score'))} {row.get('research_score') or '—'}/10"),
                    ])
                with dc2:
                    highlights = _pj(row.get("highlights"), [])
                    if highlights:
                        st.markdown("**Highlights**")
                        for h in highlights[:6]: st.markdown(f"- {h}")
                if row.get("summary"):
                    st.markdown(f"> {row['summary']}")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 4 — PREDICTIONS
# ══════════════════════════════════════════════════════════════════════════════
with tab4:
    try:
        with _conn() as c:
            rows = c.execute(
                """SELECT as_of_date, ticker, created_at, prediction, recommendation,
                           confidence, composite_score,
                           fundamental_score, research_score, macro_score, news_score,
                           horizon_days, trigger_type, trigger_event_id,
                           reasoning, panel_summary,
                           changed_from_previous, previous_prediction, model_name
                   FROM predictions
                   WHERE DATE(as_of_date) BETWEEN ? AND ?
                   ORDER BY composite_score DESC NULLS LAST, ticker""",
                [_DATE_FROM, _DATE_TO],
            ).fetchall()
    except Exception as exc:
        st.error(f"Query error: {exc}"); rows = []

    raw_df = pd.DataFrame([dict(r) for r in rows]) if rows else pd.DataFrame()

    if raw_df.empty:
        st.info(f"No predictions between {_DATE_FROM} and {_DATE_TO}. Open **APEX Chat** to generate your first recommendation.", icon="🤖")
        if st.button("🤖 Go to APEX Chat", type="primary"):
            st.switch_page("pages/4_🤖_Chat.py")
    else:
        df = raw_df.copy()
        df["created_at"] = pd.to_datetime(df["created_at"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M")
        for col in ["confidence","composite_score","fundamental_score",
                    "research_score","macro_score","news_score"]:
            df[col] = pd.to_numeric(df[col], errors="coerce").round(1)
        df["changed_from_previous"] = df["changed_from_previous"].apply(
            lambda v: "Yes" if v else "No"
        )

        for col in ["horizon_days", "trigger_type", "trigger_event_id"]:
            if col not in df.columns:
                df[col] = None

        grid_df = df[[
            "as_of_date", "ticker", "created_at", "recommendation", "prediction",
            "horizon_days", "confidence", "composite_score",
            "fundamental_score", "research_score", "macro_score", "news_score",
            "trigger_type", "changed_from_previous", "previous_prediction", "model_name",
        ]].rename(columns={
            "as_of_date":            "As Of Date",
            "ticker":                "Ticker",
            "created_at":            "Created",
            "recommendation":        "Rec",
            "prediction":            "Direction",
            "horizon_days":          "Horizon",
            "confidence":            "Conf",
            "composite_score":       "Composite",
            "fundamental_score":     "Fund",
            "research_score":        "Research",
            "macro_score":           "Macro",
            "news_score":            "News",
            "trigger_type":          "Trigger",
            "changed_from_previous": "Changed",
            "previous_prediction":   "Prev Rec",
            "model_name":            "Model",
        })

        section_title("Grid", badge_text=f"{len(df)} rows", badge_color="#7C3AED")
        st.caption("Inline filter row below each header · Click row for details")

        col_defs = [
            {"field": "As Of Date",  "width": 105, "filter": "agDateColumnFilter",   "pinned": "left"},
            {"field": "Ticker",      "width": 85,  "filter": "agTextColumnFilter",   "pinned": "left"},
            {"field": "Created",     "width": 140, "filter": "agTextColumnFilter"},
            {"field": "Rec",         "width": 120, "filter": "agTextColumnFilter"},
            {"field": "Direction",   "width": 100, "filter": "agTextColumnFilter"},
            {"field": "Horizon",     "width": 75,  "filter": "agNumberColumnFilter", "valueFormatter": "value != null ? value + 'd' : '—'"},
            {"field": "Conf",        "width": 65,  "filter": "agNumberColumnFilter"},
            {"field": "Composite",   "width": 90,  "filter": "agNumberColumnFilter"},
            {"field": "Fund",        "width": 65,  "filter": "agNumberColumnFilter"},
            {"field": "Research",    "width": 80,  "filter": "agNumberColumnFilter"},
            {"field": "Macro",       "width": 70,  "filter": "agNumberColumnFilter"},
            {"field": "News",        "width": 65,  "filter": "agNumberColumnFilter"},
            {"field": "Trigger",     "width": 170, "filter": "agTextColumnFilter"},
            {"field": "Changed",     "width": 85,  "filter": "agTextColumnFilter"},
            {"field": "Prev Rec",    "width": 110, "filter": "agTextColumnFilter"},
            {"field": "Model",       "width": 160, "filter": "agTextColumnFilter"},
        ]

        selected = _aggrid(grid_df, col_defs, height=440, key="pred_grid")

        if selected:
            hit = selected[0]
            ticker = hit.get("Ticker", "")
            created = hit.get("Created", "")
            row_matches = raw_df[raw_df["ticker"] == ticker]
            if not row_matches.empty:
                row = row_matches.iloc[0]
                for _, candidate in row_matches.iterrows():
                    if str(candidate.get("created_at", "")).startswith(created[:16]):
                        row = candidate
                        break

                st.divider()
                rec = row.get("recommendation", "HOLD")
                col_hex, _, label = REC_STYLES.get(rec, (NEUTRAL, "#F1F5F9", rec))
                section_title(f"Detail — {ticker}", badge_text=label, badge_color=col_hex)

                xc1, xc2, xc3 = st.columns(3)
                with xc1:
                    _detail_card("Scores", [
                        ("Composite",    f"{row.get('composite_score') or '—'}/10"),
                        ("Confidence",   f"{row.get('confidence') or '—'}/10"),
                        ("Fundamentals", f"{_score_color(row.get('fundamental_score'))} {row.get('fundamental_score') or '—'}/10"),
                        ("Research",     f"{_score_color(row.get('research_score'))} {row.get('research_score') or '—'}/10"),
                        ("Macro",        f"{_score_color(row.get('macro_score'))} {row.get('macro_score') or '—'}/10"),
                        ("News",         f"{_score_color(row.get('news_score'))} {row.get('news_score') or '—'}/10"),
                    ])
                with xc2:
                    trigger_raw = row.get("trigger_type") or "—"
                    trigger_fmt = trigger_raw.replace("_", " ").title() if trigger_raw != "—" else "—"
                    _detail_card("Meta", [
                        ("Direction",  row.get("prediction", "—")),
                        ("Horizon",    f"{row.get('horizon_days') or '—'}d"),
                        ("Trigger",    trigger_fmt),
                        ("Created",    (str(row.get("created_at") or ""))[:16]),
                        ("Model",      row.get("model_name", "—") or "—"),
                        ("Changed?",   "🔄 Yes" if row.get("changed_from_previous") else "No"),
                        ("Previous",   row.get("previous_prediction", "—") or "—"),
                    ])
                with xc3:
                    try:
                        panel = _pj(row.get("panel_summary"), {})
                        if panel:
                            st.markdown("**Panel Verdicts**")
                            for k, name in [
                                ("chen_verdict",  "🧮 Fundamental Analyst"),
                                ("webb_verdict",  "📊 Research Analyst"),
                                ("varga_verdict", "🌐 Macro Analyst"),
                                ("park_verdict",  "📰 News Analyst"),
                            ]:
                                if panel.get(k):
                                    st.markdown(f"**{name}:** {panel[k]}")
                            if panel.get("key_debate"):
                                st.info(panel["key_debate"], icon="💬")
                    except Exception:
                        pass

                if row.get("reasoning"):
                    st.markdown(f"> {row['reasoning']}")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 5 — HOLDINGS
# ══════════════════════════════════════════════════════════════════════════════
with tab5:
    try:
        with _conn() as c:
            rows = c.execute(
                """SELECT ticker, description, shares, avg_cost, cost_basis_total,
                          current_price, current_value, account_name, account_type,
                          broker, sector, as_of_date, synced_at
                   FROM holdings
                   WHERE DATE(as_of_date) BETWEEN ? AND ?
                   ORDER BY current_value DESC NULLS LAST""",
                [_DATE_FROM, _DATE_TO],
            ).fetchall()
    except Exception as exc:
        st.error(f"Query error: {exc}"); rows = []

    raw_df = pd.DataFrame([dict(r) for r in rows]) if rows else pd.DataFrame()

    if raw_df.empty:
        st.info(f"No holdings data between {_DATE_FROM} and {_DATE_TO}.", icon="💼")
    else:
        df = raw_df.copy()
        for col in ["shares", "avg_cost", "cost_basis_total", "current_price", "current_value"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["gain_loss"]     = df["current_value"] - df["cost_basis_total"]
        df["gain_loss_pct"] = ((df["current_value"] - df["cost_basis_total"]) / df["cost_basis_total"] * 100).round(1)
        total_value     = df["current_value"].sum()
        total_cost      = df["cost_basis_total"].sum()
        total_gain_loss = total_value - total_cost

        mc1, mc2, mc3, mc4 = st.columns(4)
        mc1.metric("Portfolio Value",  f"${total_value:,.0f}")
        mc2.metric("Total Cost Basis", f"${total_cost:,.0f}")
        mc3.metric("Total Gain/Loss",  f"${total_gain_loss:+,.0f}",
                   delta=f"{total_gain_loss / total_cost * 100:+.1f}%" if total_cost else None)
        mc4.metric("Positions", str(len(df)))
        st.divider()

        grid_df = df[[
            "as_of_date", "ticker", "description", "shares", "avg_cost", "cost_basis_total",
            "current_price", "current_value", "gain_loss", "gain_loss_pct",
            "account_name", "account_type", "broker", "sector",
        ]].rename(columns={
            "as_of_date":       "As Of Date",
            "ticker":           "Ticker",
            "description":      "Name",
            "shares":           "Shares",
            "avg_cost":         "Avg Cost",
            "cost_basis_total": "Cost Basis",
            "current_price":    "Price",
            "current_value":    "Value",
            "gain_loss":        "Gain/Loss",
            "gain_loss_pct":    "G/L %",
            "account_name":     "Account",
            "account_type":     "Type",
            "broker":           "Broker",
            "sector":           "Sector",
            "as_of_date":       "As Of Date",
        })

        section_title("Holdings", badge_text=f"{len(df)} positions", badge_color=PRIMARY)
        st.caption("Sorted by current value · Click row for detail")

        col_defs = [
            {"field": "As Of Date",      "width": 105, "filter": "agDateColumnFilter",   "pinned": "left"},
            {"field": "Ticker",     "width": 85,  "filter": "agTextColumnFilter",   "pinned": "left"},
            {"field": "Name",       "width": 200, "filter": "agTextColumnFilter"},
            {"field": "Shares",     "width": 85,  "filter": "agNumberColumnFilter", "valueFormatter": "value != null ? value.toFixed(3) : '—'"},
            {"field": "Avg Cost",   "width": 95,  "filter": "agNumberColumnFilter", "valueFormatter": "value != null ? '$' + value.toFixed(2) : '—'"},
            {"field": "Cost Basis", "width": 105, "filter": "agNumberColumnFilter", "valueFormatter": "value != null ? '$' + value.toLocaleString('en-US', {maximumFractionDigits:0}) : '—'"},
            {"field": "Price",      "width": 90,  "filter": "agNumberColumnFilter", "valueFormatter": "value != null ? '$' + value.toFixed(2) : '—'"},
            {"field": "Value",      "width": 105, "filter": "agNumberColumnFilter", "valueFormatter": "value != null ? '$' + value.toLocaleString('en-US', {maximumFractionDigits:0}) : '—'"},
            {"field": "Gain/Loss",  "width": 105, "filter": "agNumberColumnFilter", "valueFormatter": "value != null ? (value >= 0 ? '+$' : '-$') + Math.abs(value).toLocaleString('en-US', {maximumFractionDigits:0}) : '—'"},
            {"field": "G/L %",     "width": 85,  "filter": "agNumberColumnFilter", "valueFormatter": "value != null ? (value >= 0 ? '+' : '') + value.toFixed(1) + '%' : '—'"},
            {"field": "Account",    "width": 120, "filter": "agTextColumnFilter"},
            {"field": "Type",       "width": 100, "filter": "agTextColumnFilter"},
            {"field": "Broker",     "width": 100, "filter": "agTextColumnFilter"},
            {"field": "Sector",     "width": 130, "filter": "agTextColumnFilter"},
        ]

        selected = _aggrid(grid_df, col_defs, height=460, key="hold_grid")

        if selected:
            hit    = selected[0]
            ticker = hit.get("Ticker", "")
            row_m  = df[df["ticker"] == ticker]
            if not row_m.empty:
                row = row_m.iloc[0]
                st.divider()
                gl_icon = "🟢" if (row.get("gain_loss") or 0) >= 0 else "🔴"
                section_title(f"Detail — {ticker}", badge_text=str(row.get("description",""))[:40], badge_color=PRIMARY)
                dc1, dc2 = st.columns(2)
                with dc1:
                    _detail_card("Position", [
                        ("Shares",      f"{row.get('shares') or '—':.3f}" if row.get('shares') else "—"),
                        ("Avg Cost",    f"${row.get('avg_cost') or '—':.2f}" if row.get('avg_cost') else "—"),
                        ("Cost Basis",  f"${(row.get('cost_basis_total') or 0):,.2f}"),
                        ("Price",       f"${row.get('current_price') or '—':.2f}" if row.get('current_price') else "—"),
                        ("Value",       f"${(row.get('current_value') or 0):,.2f}"),
                        ("Gain/Loss",   f"{gl_icon} ${(row.get('gain_loss') or 0):+,.2f}"),
                        ("G/L %",       f"{(row.get('gain_loss_pct') or 0):+.1f}%"),
                    ])
                with dc2:
                    _detail_card("Account", [
                        ("Account",    str(row.get('account_name') or '—')),
                        ("Type",       str(row.get('account_type') or '—')),
                        ("Broker",     str(row.get('broker') or '—')),
                        ("Sector",     str(row.get('sector') or '—')),
                        ("As Of Date",      str(row.get('as_of_date') or '—')),
                        ("Synced At",  str(row.get('synced_at') or '—')[:16]),
                    ])


# ══════════════════════════════════════════════════════════════════════════════
# TAB 6 — VALIDATION
# ══════════════════════════════════════════════════════════════════════════════
with tab6:
    # ── Controls ──────────────────────────────────────────────────────────────
    _vc1, _vc2 = st.columns([3, 2])
    with _vc1:
        _MODEL_TIER = st.selectbox(
            "Model filter",
            ["All models", "Higher reasoning", "Lower reasoning"],
            key="val_model_tier_sel",
            label_visibility="visible",
        )
    with _vc2:
        _VAL_SCOPE = st.selectbox(
            "Date scope",
            ["All time", "Use sidebar range"],
            key="val_date_scope_sel",
            label_visibility="visible",
        )

    _VAL_FROM = "2000-01-01" if _VAL_SCOPE == "All time" else _DATE_FROM
    _VAL_TO   = _TODAY       if _VAL_SCOPE == "All time" else _DATE_TO
    _TIER_SQL, _TIER_PARAMS = _model_tier_sql(_MODEL_TIER)

    # ── Query: individual evaluated predictions ───────────────────────────────
    try:
        with _conn() as c:
            val_rows = c.execute(
                f"""SELECT DATE(evaluated_at) as validated_at,
                           ticker, DATE(as_of_date) as pred_date, horizon_days,
                           model_name, model_provider,
                           predicted_direction, actual_direction, outcome,
                           ROUND(brier_score, 4)   as brier_score,
                           ROUND(log_loss, 4)       as log_loss,
                           ROUND(excess_return * 100, 2) as excess_return_pct,
                           conviction_score,
                           CASE WHEN in_predicted_range = 1 THEN 'Yes' ELSE 'No' END as in_range,
                           ROUND(predicted_return_low, 2)  as pred_low,
                           ROUND(predicted_return_high, 2) as pred_high,
                           ROUND(actual_return * 100, 2)   as actual_return_pct,
                           evaluated_at
                   FROM predictions
                   WHERE evaluation_status = 'evaluated'
                     AND DATE(as_of_date) BETWEEN ? AND ?
                     AND {_TIER_SQL}
                   ORDER BY as_of_date DESC, ticker""",
                [_VAL_FROM, _VAL_TO] + _TIER_PARAMS,
            ).fetchall()
    except Exception as exc:
        st.error(f"Query error: {exc}")
        val_rows = []

    raw_val = pd.DataFrame([dict(r) for r in val_rows]) if val_rows else pd.DataFrame()

    if raw_val.empty:
        st.info(
            f"No evaluated predictions between {_VAL_FROM} and {_VAL_TO}. "
            "Predictions are scored automatically once their horizon matures.",
            icon="📈",
        )
    else:
        grid_df = raw_val[[
            "validated_at", "pred_date", "ticker", "horizon_days", "model_name",
            "predicted_direction", "actual_direction", "outcome",
            "brier_score", "log_loss", "excess_return_pct",
            "conviction_score", "in_range",
            "pred_low", "pred_high", "actual_return_pct",
        ]].rename(columns={
            "validated_at":       "As Of Date",
            "ticker":             "Ticker",
            "pred_date":          "Pred Date",
            "horizon_days":       "Horizon",
            "model_name":         "Pred Model",
            "predicted_direction":"Pred Dir",
            "actual_direction":   "Actual Dir",
            "outcome":            "Outcome",
            "brier_score":        "Brier",
            "log_loss":           "Log-Loss",
            "excess_return_pct":  "Excess Rtn %",
            "conviction_score":   "Conviction",
            "in_range":           "In Range",
            "pred_low":           "Pred Low %",
            "pred_high":          "Pred High %",
            "actual_return_pct":  "Actual Rtn %",
        })

        section_title("Grid", badge_text=f"{len(grid_df)} evaluated predictions", badge_color="#10B981")
        st.caption("Click any column header to sort · Use the filter row beneath each header to filter · Click a row to see details below")

        col_defs = [
            {"field": "As Of Date",   "width": 110, "filter": "agDateColumnFilter",   "pinned": "left"},
            {"field": "Pred Date",    "width": 105, "filter": "agDateColumnFilter",   "pinned": "left"},
            {"field": "Ticker",       "width": 85,  "filter": "agTextColumnFilter",   "pinned": "left"},
            {"field": "Horizon",      "width": 80,  "filter": "agNumberColumnFilter"},
            {"field": "Pred Model",   "width": 160, "filter": "agTextColumnFilter"},
            {"field": "Pred Dir",     "width": 90,  "filter": "agTextColumnFilter"},
            {"field": "Actual Dir",   "width": 90,  "filter": "agTextColumnFilter"},
            {"field": "Outcome",      "width": 170, "filter": "agTextColumnFilter"},
            {"field": "Brier",        "width": 80,  "filter": "agNumberColumnFilter"},
            {"field": "Log-Loss",     "width": 85,  "filter": "agNumberColumnFilter"},
            {"field": "Excess Rtn %", "width": 105, "filter": "agNumberColumnFilter",
             "valueFormatter": "value != null ? value.toFixed(2)+'%' : '—'"},
            {"field": "Conviction",   "width": 95,  "filter": "agNumberColumnFilter"},
            {"field": "In Range",     "width": 85,  "filter": "agTextColumnFilter"},
            {"field": "Pred Low %",   "width": 95,  "filter": "agNumberColumnFilter"},
            {"field": "Pred High %",  "width": 95,  "filter": "agNumberColumnFilter"},
            {"field": "Actual Rtn %", "width": 100, "filter": "agNumberColumnFilter",
             "valueFormatter": "value != null ? value.toFixed(2)+'%' : '—'"},
        ]

        selected = _aggrid(grid_df, col_defs, height=440, key="val_grid")

        if selected:
            hit = selected[0]
            ticker = hit.get("Ticker", "")
            pred_date = hit.get("Pred Date", "")
            row_matches = raw_val[
                (raw_val["ticker"] == ticker) &
                (raw_val["pred_date"].astype(str).str.startswith(str(pred_date)[:10]))
            ]
            if not row_matches.empty:
                row = row_matches.iloc[0]
                st.divider()
                outcome = row.get("outcome", "—")
                outcome_color = {
                    "strong_correct": SUCCESS, "directionally_correct": SUCCESS,
                    "flat_correct": PRIMARY, "wrong_minor": WARNING,
                    "wrong_significant": DANGER,
                }.get(str(outcome), NEUTRAL)
                section_title(
                    f"Detail — {ticker}  ·  {pred_date}  ·  {row.get('horizon_days')}d",
                    badge_text=str(outcome).replace("_", " ").title(),
                    badge_color=outcome_color,
                )
                dc1, dc2 = st.columns(2)
                with dc1:
                    _detail_card("Prediction", [
                        ("Pred Model",    row.get("model_name") or "—"),
                        ("Predicted Dir", row.get("predicted_direction") or "—"),
                        ("Actual Dir",    row.get("actual_direction") or "—"),
                        ("Pred Range",    f"{row.get('pred_low') or '—'}% – {row.get('pred_high') or '—'}%"),
                        ("Actual Return", f"{row.get('actual_return_pct') or '—'}%"),
                        ("In Range",      row.get("in_range") or "—"),
                        ("Conviction",    f"{row.get('conviction_score') or '—'}/10"),
                    ])
                with dc2:
                    _detail_card("Scores", [
                        ("Brier Score",   f"{row.get('brier_score') or '—'}"),
                        ("Log-Loss",      f"{row.get('log_loss') or '—'}"),
                        ("Excess Return", f"{row.get('excess_return_pct') or '—'}%"),
                        ("Outcome",       str(outcome).replace("_", " ").title()),
                        ("Evaluated At",  str(row.get("evaluated_at") or "—")[:16]),
                    ])


# ══════════════════════════════════════════════════════════════════════════════
# TAB 7 — TRIGGER EVENTS
# ══════════════════════════════════════════════════════════════════════════════
with tab7:
    try:
        with _conn() as c:
            _ev_cols = {r[1] for r in c.execute("PRAGMA table_info(trigger_events)").fetchall()}
            if "id" not in _ev_cols:
                st.info("No trigger_events table yet — run `--batch morning` to populate.", icon="⚡")
            else:
                _ev_rows = c.execute(
                    """SELECT id, detected_at, ticker, event_type, severity, source,
                              summary, processed, processed_at, prediction_id
                       FROM trigger_events
                       ORDER BY detected_at DESC
                       LIMIT 500"""
                ).fetchall()
                ev_df = pd.DataFrame([dict(r) for r in _ev_rows]) if _ev_rows else pd.DataFrame()

                if ev_df.empty:
                    st.info("No events detected yet. Events are populated by the morning batch.", icon="⚡")
                else:
                    sev_counts = ev_df.groupby("severity").size().to_dict()
                    proc_pct = (ev_df["processed"] > 0).mean() * 100
                    m1, m2, m3, m4 = st.columns(4)
                    m1.metric("Total Events", len(ev_df))
                    m2.metric("Severity 3", sev_counts.get(3, 0))
                    m3.metric("Processed", f"{proc_pct:.0f}%")
                    m4.metric("Unique Tickers", ev_df["ticker"].nunique())

                    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
                    section_title("Events", badge_text=f"{len(ev_df)} rows", badge_color=PRIMARY)
                    st.caption("Showing latest 500 events · Click row for details")

                    ev_df["processed"] = ev_df["processed"].apply(lambda v: "Yes" if v else "No")
                    ev_df["detected_at"] = ev_df["detected_at"].apply(
                        lambda v: str(v)[:19].replace("T", " ") if isinstance(v, str) else ""
                    )
                    ev_df["processed_at"] = ev_df["processed_at"].apply(
                        lambda v: str(v)[:19].replace("T", " ") if isinstance(v, str) else ""
                    )

                    grid_ev = ev_df.rename(columns={
                        "id":            "ID",
                        "detected_at":   "Detected At",
                        "ticker":        "Ticker",
                        "event_type":    "Event Type",
                        "severity":      "Severity",
                        "source":        "Source",
                        "summary":       "Summary",
                        "processed":     "Processed",
                        "processed_at":  "Processed At",
                        "prediction_id": "Pred ID",
                    })
                    ev_col_defs = [
                        {"field": "Detected At",  "width": 155, "filter": "agTextColumnFilter",   "pinned": "left"},
                        {"field": "Ticker",        "width": 90,  "filter": "agTextColumnFilter",   "pinned": "left"},
                        {"field": "Severity",      "width": 85,  "filter": "agNumberColumnFilter"},
                        {"field": "Event Type",    "width": 160, "filter": "agTextColumnFilter"},
                        {"field": "Source",        "width": 120, "filter": "agTextColumnFilter"},
                        {"field": "Summary",       "width": 320, "filter": "agTextColumnFilter"},
                        {"field": "Processed",     "width": 95,  "filter": "agTextColumnFilter"},
                        {"field": "Processed At",  "width": 155, "filter": "agTextColumnFilter"},
                        {"field": "Pred ID",       "width": 80,  "filter": "agNumberColumnFilter"},
                        {"field": "ID",            "width": 65,  "filter": "agNumberColumnFilter"},
                    ]
                    _aggrid(grid_ev, ev_col_defs, height=440, key="events_grid")

                    st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)
                    section_title("Trigger Breakdown (last 7 days)", badge_color=PRIMARY)
                    try:
                        breakdown = c.execute(
                            """SELECT COALESCE(trigger_type, 'none') as trigger_type, COUNT(*) AS cnt
                               FROM predictions
                               WHERE as_of_date >= date('now', '-7 days')
                               GROUP BY trigger_type
                               ORDER BY cnt DESC"""
                        ).fetchall()
                        if breakdown:
                            br_df = pd.DataFrame([dict(r) for r in breakdown])
                            br_df.columns = ["Trigger Type", "Count"]
                            st.dataframe(br_df, use_container_width=True, hide_index=True)
                        else:
                            st.caption("No predictions in the last 7 days.")
                    except Exception:
                        pass
    except Exception as exc:
        st.error(f"Query error: {exc}")
