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
        "SELECT DISTINCT DATE(date)          FROM news_daily_update",
        "SELECT DISTINCT DATE(raw_fetched_at) FROM research",
        "SELECT DISTINCT DATE(created_at)    FROM predictions",
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


def _conn():
    c = sqlite3.connect(str(_DB))
    c.row_factory = sqlite3.Row
    return c


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
cnt_news = _count_for("news_daily_update", "date",           _DATE_FROM, _DATE_TO, extra_where="row_type = 'ticker'")
cnt_res  = _count_for("research",          "raw_fetched_at", _DATE_FROM, _DATE_TO)
cnt_pred = _count_for("predictions",       "created_at",     _DATE_FROM, _DATE_TO)
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

tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
    f"📋 Fundamentals ({cnt_fund})",
    f"📰 News ({cnt_news})",
    f"🔬 Research ({cnt_res})",
    f"🤖 Predictions ({cnt_pred})",
    f"💼 Holdings ({cnt_hold})",
    f"📈 Validation ({cnt_metr})",
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
            "ticker", "as_of_date", "filing_type", "filing_date",
            "revenue_growth_yoy_pct", "net_margin", "fcf", "debt_to_equity",
            "fundamental_score", "top_strength", "top_risk",
            "model_name", "model_provider",
        ]].rename(columns={
            "ticker":                 "Ticker",
            "as_of_date":             "As Of",
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
            {"field": "Ticker",       "width": 90,  "filter": "agTextColumnFilter",   "pinned": "left"},
            {"field": "As Of",        "width": 110, "filter": "agDateColumnFilter"},
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
                        ("As Of",          str(row.get('as_of_date') or '—')),
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
                f"""SELECT date, ticker, sentiment, sentiment_score,
                           headline_1, headline_2, top_themes, trending,
                           source, impacted_tickers
                           {_model_sel}
                   FROM news_daily_update
                   WHERE row_type = 'ticker' AND DATE(date) BETWEEN ? AND ?
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
            "date", "ticker", "sentiment", "sentiment_score",
            "headline_1", "headline_2", "themes_str", "trending",
            "source", "impacted_tickers",
            "model_name", "model_provider",
        ]].rename(columns={
            "date":              "Date",
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
            {"field": "Date",       "width": 110, "filter": "agDateColumnFilter",   "pinned": "left"},
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
            dt      = hit.get("Date", "")
            row_matches = raw_df[(raw_df["ticker"] == ticker) & (raw_df["date"] == dt)]
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
                """SELECT ticker, raw_fetched_at, consensus, consensus_mean,
                           num_analysts, price_target_avg, price_target_high,
                           price_target_low, current_price, upside_to_mean_pct,
                           latest_upgrade_date, research_score,
                           highlights, summary, last_llm_run_date,
                           model_name, model_provider
                   FROM research
                   WHERE DATE(raw_fetched_at) BETWEEN ? AND ?
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

        grid_df = df[[
            "ticker", "raw_fetched_at", "consensus", "consensus_mean",
            "num_analysts", "price_target_avg", "price_target_high", "price_target_low",
            "current_price", "upside_to_mean_pct", "latest_upgrade_date",
            "research_score", "last_llm_run_date", "model_name", "model_provider",
        ]].rename(columns={
            "ticker":               "Ticker",
            "raw_fetched_at":       "Fetched",
            "consensus":            "Consensus",
            "consensus_mean":       "Mean Rtg",
            "num_analysts":         "Analysts",
            "price_target_avg":     "Target Avg",
            "price_target_high":    "Target High",
            "price_target_low":     "Target Low",
            "current_price":        "Price",
            "upside_to_mean_pct":   "Upside %",
            "latest_upgrade_date":  "Last Upgrade",
            "research_score":       "Score",
            "last_llm_run_date":    "LLM Run",
            "model_name":           "Model",
            "model_provider":       "Provider",
        })

        section_title("Grid", badge_text=f"{len(df)} rows", badge_color=PRIMARY)
        st.caption("Inline filter row below each header · Click row for details")

        col_defs = [
            {"field": "Ticker",      "width": 90,  "filter": "agTextColumnFilter",   "pinned": "left"},
            {"field": "Fetched",     "width": 110, "filter": "agDateColumnFilter"},
            {"field": "Consensus",   "width": 110, "filter": "agTextColumnFilter"},
            {"field": "Mean Rtg",    "width": 90,  "filter": "agNumberColumnFilter", "valueFormatter": "value != null ? value.toFixed(1) : '—'"},
            {"field": "Analysts",    "width": 85,  "filter": "agNumberColumnFilter", "valueFormatter": "value != null ? Math.round(value) : '—'"},
            {"field": "Target Avg",  "width": 100, "filter": "agNumberColumnFilter", "valueFormatter": "value != null ? '$' + value.toFixed(2) : '—'"},
            {"field": "Target High", "width": 105, "filter": "agNumberColumnFilter", "valueFormatter": "value != null ? '$' + value.toFixed(2) : '—'"},
            {"field": "Target Low",  "width": 100, "filter": "agNumberColumnFilter", "valueFormatter": "value != null ? '$' + value.toFixed(2) : '—'"},
            {"field": "Price",       "width": 90,  "filter": "agNumberColumnFilter", "valueFormatter": "value != null ? '$' + value.toFixed(2) : '—'"},
            {"field": "Upside %",    "width": 90,  "filter": "agNumberColumnFilter", "valueFormatter": "value != null ? value.toFixed(1) + '%' : '—'"},
            {"field": "Last Upgrade","width": 110, "filter": "agDateColumnFilter"},
            {"field": "Score",       "width": 80,  "filter": "agNumberColumnFilter"},
            {"field": "LLM Run",     "width": 110, "filter": "agDateColumnFilter"},
            {"field": "Model",       "width": 160, "filter": "agTextColumnFilter"},
            {"field": "Provider",    "width": 110, "filter": "agTextColumnFilter"},
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
                        ("Mean Rating",  f"{row.get('consensus_mean') or '—'}"),
                        ("# Analysts",   str(int(row.get("num_analysts") or 0) or "—")),
                        ("Target Avg",   f"${row.get('price_target_avg') or '—'}"),
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
            existing_cols = {r[1] for r in c.execute("PRAGMA table_info(predictions)").fetchall()}
            regime_col = ", weight_regime" if "weight_regime" in existing_cols else ", NULL as weight_regime"
            rows = c.execute(
                f"""SELECT ticker, created_at, prediction, recommendation,
                           confidence, composite_score,
                           fundamental_score, research_score, macro_score, news_score
                           {regime_col}, reasoning, panel_summary,
                           changed_from_previous, previous_prediction, model_name
                   FROM predictions
                   WHERE DATE(created_at) BETWEEN ? AND ?
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

        grid_df = df[[
            "ticker", "created_at", "recommendation", "prediction",
            "confidence", "composite_score",
            "fundamental_score", "research_score", "macro_score", "news_score",
            "weight_regime", "changed_from_previous", "previous_prediction", "model_name",
        ]].rename(columns={
            "ticker":                "Ticker",
            "created_at":            "Created",
            "recommendation":        "Rec",
            "prediction":            "Direction",
            "confidence":            "Conf",
            "composite_score":       "Composite",
            "fundamental_score":     "Fund",
            "research_score":        "Research",
            "macro_score":           "Macro",
            "news_score":            "News",
            "weight_regime":         "Regime",
            "changed_from_previous": "Changed",
            "previous_prediction":   "Prev Rec",
            "model_name":            "Model",
        })

        section_title("Grid", badge_text=f"{len(df)} rows", badge_color="#7C3AED")
        st.caption("Inline filter row below each header · Click row for details")

        col_defs = [
            {"field": "Ticker",    "width": 85,  "filter": "agTextColumnFilter",   "pinned": "left"},
            {"field": "Created",   "width": 140, "filter": "agTextColumnFilter",   "pinned": "left"},
            {"field": "Rec",       "width": 120, "filter": "agTextColumnFilter"},
            {"field": "Direction", "width": 100, "filter": "agTextColumnFilter"},
            {"field": "Conf",      "width": 75,  "filter": "agNumberColumnFilter"},
            {"field": "Composite", "width": 90,  "filter": "agNumberColumnFilter"},
            {"field": "Fund",      "width": 70,  "filter": "agNumberColumnFilter"},
            {"field": "Research",  "width": 85,  "filter": "agNumberColumnFilter"},
            {"field": "Macro",     "width": 75,  "filter": "agNumberColumnFilter"},
            {"field": "News",      "width": 70,  "filter": "agNumberColumnFilter"},
            {"field": "Regime",    "width": 140, "filter": "agTextColumnFilter"},
            {"field": "Changed",   "width": 85,  "filter": "agTextColumnFilter"},
            {"field": "Prev Rec",  "width": 110, "filter": "agTextColumnFilter"},
            {"field": "Model",     "width": 160, "filter": "agTextColumnFilter"},
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
                    _detail_card("Meta", [
                        ("Direction",  row.get("prediction", "—")),
                        ("Regime",     row.get("weight_regime", "—") or "—"),
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
                                ("chen_verdict",  "🧮 Dr. Chen"),
                                ("webb_verdict",  "📊 Marcus"),
                                ("varga_verdict", "🌐 Elena"),
                                ("park_verdict",  "📰 James"),
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
            "ticker", "description", "shares", "avg_cost", "cost_basis_total",
            "current_price", "current_value", "gain_loss", "gain_loss_pct",
            "account_name", "account_type", "broker", "sector", "as_of_date",
        ]].rename(columns={
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
            "as_of_date":       "As Of",
        })

        section_title("Holdings", badge_text=f"{len(df)} positions", badge_color=PRIMARY)
        st.caption("Sorted by current value · Click row for detail")

        col_defs = [
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
            {"field": "As Of",      "width": 100, "filter": "agDateColumnFilter"},
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
                        ("As Of",      str(row.get('as_of_date') or '—')),
                        ("Synced At",  str(row.get('synced_at') or '—')[:16]),
                    ])


# ══════════════════════════════════════════════════════════════════════════════
# TAB 6 — VALIDATION METRICS
# ══════════════════════════════════════════════════════════════════════════════
with tab6:
    import plotly.graph_objects as _go

    # ── Controls row: model tier filter + date scope ──────────────────────────
    _vc1, _vc2 = st.columns([3, 2])
    with _vc1:
        st.markdown(
            '<p style="font-size:0.75rem;font-weight:700;color:#374151;margin:0 0 6px">'
            'Model tier</p>',
            unsafe_allow_html=True,
        )
        _MODEL_TIER = st.radio(
            "val_model_tier",
            ["All models", "Higher reasoning", "Lower reasoning"],
            horizontal=True,
            label_visibility="collapsed",
            key="val_model_tier_radio",
        )
        _tier_hint = {
            "Higher reasoning": "Claude-Sonnet · GPT-4o · GPT-4",
            "Lower reasoning":  "Groq · Cerebras · OpenRouter models",
        }.get(_MODEL_TIER, "All predictions — no model filter applied")
        st.caption(_tier_hint)

    with _vc2:
        st.markdown(
            '<p style="font-size:0.75rem;font-weight:700;color:#374151;margin:0 0 6px">'
            'Date scope</p>',
            unsafe_allow_html=True,
        )
        _VAL_SCOPE = st.radio(
            "val_date_scope",
            ["All time", "Use sidebar range"],
            horizontal=True,
            label_visibility="collapsed",
            key="val_date_scope_radio",
        )

    # Validation uses all-time by default (predictions mature over weeks)
    if _VAL_SCOPE == "Use sidebar range":
        _VAL_FROM, _VAL_TO = _VAL_FROM, _VAL_TO
    else:
        _VAL_FROM = "2000-01-01"
        _VAL_TO   = _TODAY

    st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)
    _TIER_SQL, _TIER_PARAMS = _model_tier_sql(_MODEL_TIER)

    # ── Per-model metrics from predictions (respects date + tier filter) ──────
    try:
        with _conn() as c:
            # All evaluated predictions in date range, filtered by model tier
            model_rows = c.execute(
                f"""SELECT model_name, model_provider,
                          COUNT(*) as total,
                          SUM(CASE WHEN actual_direction = predicted_direction THEN 1 ELSE 0 END) as correct,
                          CAST(SUM(CASE WHEN actual_direction = predicted_direction THEN 1 ELSE 0 END) AS FLOAT)
                              / NULLIF(COUNT(*), 0) as dir_acc,
                          AVG(excess_return) as avg_excess,
                          AVG(brier_score) as avg_brier,
                          AVG(CASE WHEN in_predicted_range = 1 THEN 1.0 ELSE 0.0 END) as in_range_pct,
                          AVG(conviction_score) as avg_conviction,
                          SUM(CASE WHEN conviction_score >= 7 AND actual_direction = predicted_direction THEN 1 ELSE 0 END) as hi_conv_correct,
                          SUM(CASE WHEN conviction_score >= 7 THEN 1 ELSE 0 END) as hi_conv_total
                   FROM predictions
                   WHERE evaluation_status = 'evaluated'
                   AND DATE(created_at) BETWEEN ? AND ?
                   AND {_TIER_SQL}
                   GROUP BY model_name, model_provider
                   ORDER BY dir_acc DESC NULLS LAST""",
                [_VAL_FROM, _VAL_TO] + _TIER_PARAMS,
            ).fetchall()

            # Rolling metrics from metrics_rolling (date-filtered only, no model column)
            rolling_rows = c.execute(
                """SELECT metric_date, horizon_days, lookback_days,
                          directional_accuracy, in_range_pct, mean_excess_return,
                          mean_error_magnitude, high_conviction_accuracy,
                          low_conviction_accuracy, brier_score, mean_log_loss,
                          num_predictions, computed_at, system_version, segment
                   FROM metrics_rolling
                   WHERE metric_date BETWEEN ? AND ?
                   ORDER BY lookback_days""",
                [_VAL_FROM, _VAL_TO],
            ).fetchall()

            # Trend: dir accuracy per date (from predictions, tier-filtered)
            trend_rows = c.execute(
                f"""SELECT DATE(created_at) as pred_date,
                          CAST(SUM(CASE WHEN actual_direction = predicted_direction THEN 1 ELSE 0 END) AS FLOAT)
                              / NULLIF(COUNT(*), 0) as dir_acc,
                          AVG(excess_return) as avg_excess,
                          COUNT(*) as n
                   FROM predictions
                   WHERE evaluation_status = 'evaluated'
                   AND DATE(created_at) BETWEEN ? AND ?
                   AND {_TIER_SQL}
                   GROUP BY DATE(created_at)
                   ORDER BY pred_date""",
                [_VAL_FROM, _VAL_TO] + _TIER_PARAMS,
            ).fetchall()

    except Exception as exc:
        st.error(f"Query error: {exc}")
        model_rows = []
        rolling_rows = []
        trend_rows = []

    model_df   = pd.DataFrame([dict(r) for r in model_rows])   if model_rows   else pd.DataFrame()
    rolling_df = pd.DataFrame([dict(r) for r in rolling_rows]) if rolling_rows else pd.DataFrame()
    trend_df   = pd.DataFrame([dict(r) for r in trend_rows])   if trend_rows   else pd.DataFrame()

    # ── helpers ───────────────────────────────────────────────────────────────
    def _pct(v):
        return f"{v*100:.1f}%" if v is not None and not (isinstance(v, float) and pd.isna(v)) else "—"
    def _flt(v, d=4):
        return f"{v:.{d}f}" if v is not None and not (isinstance(v, float) and pd.isna(v)) else "—"
    def _acc_color(v):
        if v is None or (isinstance(v, float) and pd.isna(v)): return NEUTRAL
        return SUCCESS if v >= 0.55 else (WARNING if v >= 0.45 else DANGER)
    def _rtn_color(v):
        if v is None or (isinstance(v, float) and pd.isna(v)): return NEUTRAL
        return SUCCESS if v > 0 else DANGER
    def _brier_color(v):
        if v is None or (isinstance(v, float) and pd.isna(v)): return NEUTRAL
        return SUCCESS if v < 0.2 else (WARNING if v < 0.3 else DANGER)

    def _is_higher(model_name, provider):
        return (model_name in _HIGHER_MODELS or provider in _HIGHER_PROVIDERS)

    if model_df.empty and rolling_df.empty:
        st.info(f"No validation metrics between {_VAL_FROM} and {_VAL_TO}. "
                f"Metrics are computed after predictions mature.", icon="📈")
    else:
        tier_badge = {"Higher reasoning": "🧠 Higher reasoning models",
                      "Lower reasoning": "⚡ Lower reasoning models"}.get(_MODEL_TIER, "📊 All models")

        # ── Overall KPI cards (from model_df aggregate, tier-filtered) ────────
        if not model_df.empty:
            for col in ["dir_acc","avg_excess","avg_brier","in_range_pct"]:
                model_df[col] = pd.to_numeric(model_df[col], errors="coerce")

            total_preds = int(model_df["total"].sum())
            total_correct = int(model_df["correct"].sum())
            overall_da  = total_correct / total_preds if total_preds else None
            overall_er  = model_df["avg_excess"].mean() if not model_df["avg_excess"].isna().all() else None
            overall_ir  = model_df["in_range_pct"].mean() if not model_df["in_range_pct"].isna().all() else None
            overall_bs  = model_df["avg_brier"].mean() if not model_df["avg_brier"].isna().all() else None

            st.markdown(
                f'<div style="background:#F9FAFB;border:1px solid #F3F4F6;border-radius:12px;'
                f'padding:14px 20px;margin-bottom:18px;display:flex;align-items:center;gap:14px">'
                f'<div style="font-size:1.4rem">📊</div>'
                f'<div>'
                f'<div style="font-size:0.88rem;font-weight:700;color:#111827">'
                f'{tier_badge}  ·  {total_preds} evaluated predictions</div>'
                f'<div style="font-size:0.76rem;color:#6B7280;margin-top:3px">'
                f'{_VAL_FROM}  →  {_VAL_TO}</div>'
                f'</div></div>',
                unsafe_allow_html=True,
            )

            k1, k2, k3, k4 = st.columns(4)
            def _kpi(col, label, value_str, sub, color, note):
                col.markdown(
                    f'<div style="background:#FFFFFF;border:1px solid #F3F4F6;border-radius:12px;'
                    f'padding:16px 18px;border-top:3px solid {color}">'
                    f'<div style="font-size:0.68rem;font-weight:700;text-transform:uppercase;'
                    f'letter-spacing:0.08em;color:#9CA3AF">{label}</div>'
                    f'<div style="font-size:1.6rem;font-weight:800;color:#111827;'
                    f'letter-spacing:-0.03em;margin:6px 0 2px">{value_str}</div>'
                    f'<div style="font-size:0.76rem;color:{color};font-weight:600">{sub}</div>'
                    f'<div style="font-size:0.7rem;color:#9CA3AF;margin-top:4px">{note}</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
            _kpi(k1, "Directional Accuracy", _pct(overall_da),
                 "Good ≥55%  ·  Random=50%", _acc_color(overall_da),
                 "Price moved in predicted direction")
            _kpi(k2, "Mean Excess Return", _pct(overall_er),
                 "Positive = outperforms benchmark", _rtn_color(overall_er),
                 "Avg return above market over window")
            _kpi(k3, "In-Range %", _pct(overall_ir),
                 "Higher = better calibration", SUCCESS if overall_ir and overall_ir >= 0.5 else DANGER,
                 "Actual move inside predicted range")
            _kpi(k4, "Brier Score", _flt(overall_bs, 4),
                 "Lower=better  ·  Random=0.25", _brier_color(overall_bs),
                 "Probability calibration (0=perfect)")

            st.markdown("<div style='height:22px'></div>", unsafe_allow_html=True)

        # ── Model Performance Comparison ──────────────────────────────────────
        if not model_df.empty:
            section_title("Model Performance", badge_text=tier_badge)

            # Classify each row as higher or lower
            model_df["tier"] = model_df.apply(
                lambda r: "Higher" if _is_higher(r["model_name"], r["model_provider"]) else "Lower",
                axis=1,
            )

            for _, mrow in model_df.iterrows():
                mn        = mrow.get("model_name") or "Unknown"
                mp        = mrow.get("model_provider") or ""
                tier_tag  = mrow["tier"]
                da        = mrow.get("dir_acc")
                er        = mrow.get("avg_excess")
                bs        = mrow.get("avg_brier")
                ir        = mrow.get("in_range_pct")
                total     = int(mrow.get("total", 0))
                correct   = int(mrow.get("correct", 0))

                tier_color = PRIMARY if tier_tag == "Higher" else "#6B7280"
                tier_label = "🧠 Higher" if tier_tag == "Higher" else "⚡ Lower"

                st.markdown(
                    f'<div style="background:#FFFFFF;border:1px solid #F3F4F6;border-radius:12px;'
                    f'padding:14px 18px;margin-bottom:10px;'
                    f'border-left:4px solid {tier_color}">'
                    # header row
                    f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">'
                    f'<div>'
                    f'<span style="font-size:0.95rem;font-weight:700;color:#111827">{mn}</span>'
                    f'<span style="font-size:0.72rem;color:#9CA3AF;margin-left:8px">{mp}</span>'
                    f'</div>'
                    f'<span style="background:{"#EFF6FF" if tier_tag=="Higher" else "#F9FAFB"};'
                    f'color:{tier_color};font-size:0.72rem;font-weight:600;'
                    f'padding:3px 10px;border-radius:999px;border:1px solid {"#BFDBFE" if tier_tag=="Higher" else "#E5E7EB"}">'
                    f'{tier_label}</span>'
                    f'</div>'
                    # metrics row
                    f'<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px">'
                    f'<div style="text-align:center">'
                    f'<div style="font-size:1.2rem;font-weight:800;color:{_acc_color(da)}">{_pct(da)}</div>'
                    f'<div style="font-size:0.68rem;color:#9CA3AF;font-weight:600;text-transform:uppercase;letter-spacing:0.06em">Dir Acc</div>'
                    f'<div style="font-size:0.7rem;color:#6B7280">{correct}/{total} correct</div>'
                    f'</div>'
                    f'<div style="text-align:center">'
                    f'<div style="font-size:1.2rem;font-weight:800;color:{_rtn_color(er)}">{_pct(er)}</div>'
                    f'<div style="font-size:0.68rem;color:#9CA3AF;font-weight:600;text-transform:uppercase;letter-spacing:0.06em">Excess Rtn</div>'
                    f'</div>'
                    f'<div style="text-align:center">'
                    f'<div style="font-size:1.2rem;font-weight:800;color:{_brier_color(bs)}">{_flt(bs, 3)}</div>'
                    f'<div style="font-size:0.68rem;color:#9CA3AF;font-weight:600;text-transform:uppercase;letter-spacing:0.06em">Brier Score</div>'
                    f'<div style="font-size:0.7rem;color:#6B7280">↓ better</div>'
                    f'</div>'
                    f'<div style="text-align:center">'
                    f'<div style="font-size:1.2rem;font-weight:800;color:#374151">{_pct(ir)}</div>'
                    f'<div style="font-size:0.68rem;color:#9CA3AF;font-weight:600;text-transform:uppercase;letter-spacing:0.06em">In Range</div>'
                    f'</div>'
                    f'</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

            st.markdown("<div style='height:18px'></div>", unsafe_allow_html=True)

        # ── Tier summary (only for "All models" view) ─────────────────────────
        if _MODEL_TIER == "All models" and not model_df.empty:
            higher_rows = model_df[model_df["tier"] == "Higher"]
            lower_rows  = model_df[model_df["tier"] == "Lower"]

            if not higher_rows.empty and not lower_rows.empty:
                section_title("Higher vs Lower Reasoning")
                tc1, tc2 = st.columns(2)
                for tcol, trows, tlabel, tcolor in [
                    (tc1, higher_rows, "🧠 Higher Reasoning", PRIMARY),
                    (tc2, lower_rows,  "⚡ Lower Reasoning", "#6B7280"),
                ]:
                    ht = int(trows["total"].sum())
                    hc = int(trows["correct"].sum())
                    hda = hc / ht if ht else None
                    her = trows["avg_excess"].mean() if not trows["avg_excess"].isna().all() else None
                    hbs = trows["avg_brier"].mean()  if not trows["avg_brier"].isna().all()  else None
                    models_list = ", ".join(str(n) for n in trows["model_name"].dropna().tolist())
                    tcol.markdown(
                        f'<div style="background:#FFFFFF;border:1px solid #F3F4F6;border-radius:12px;'
                        f'padding:16px 18px;border-top:3px solid {tcolor}">'
                        f'<div style="font-size:0.85rem;font-weight:700;color:{tcolor};margin-bottom:10px">{tlabel}</div>'
                        f'<div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #F9FAFB">'
                        f'<span style="font-size:0.8rem;color:#6B7280">Dir Accuracy</span>'
                        f'<span style="font-size:0.8rem;font-weight:700;color:{_acc_color(hda)}">{_pct(hda)}</span></div>'
                        f'<div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #F9FAFB">'
                        f'<span style="font-size:0.8rem;color:#6B7280">Mean Excess Return</span>'
                        f'<span style="font-size:0.8rem;font-weight:700;color:{_rtn_color(her)}">{_pct(her)}</span></div>'
                        f'<div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #F9FAFB">'
                        f'<span style="font-size:0.8rem;color:#6B7280">Brier Score</span>'
                        f'<span style="font-size:0.8rem;font-weight:700;color:{_brier_color(hbs)}">{_flt(hbs,3)}</span></div>'
                        f'<div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #F9FAFB">'
                        f'<span style="font-size:0.8rem;color:#6B7280">Predictions</span>'
                        f'<span style="font-size:0.8rem;font-weight:700;color:#374151">{ht}</span></div>'
                        f'<div style="margin-top:10px;font-size:0.7rem;color:#9CA3AF;line-height:1.6">'
                        f'Models: {models_list}</div>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )
                st.markdown("<div style='height:18px'></div>", unsafe_allow_html=True)

        # ── Directional accuracy trend (tier-filtered) ────────────────────────
        if not trend_df.empty and len(trend_df) >= 2:
            trend_df["dir_acc_pct"] = (pd.to_numeric(trend_df["dir_acc"], errors="coerce") * 100).round(1)
            trend_df = trend_df.dropna(subset=["dir_acc_pct"])
            if len(trend_df) >= 2:
                section_title("Directional Accuracy Trend", badge_text=f"Last {len(trend_df)} dates · {tier_badge}")
                fig = _go.Figure()
                fig.add_hline(y=50, line_dash="dot", line_color="#E5E7EB",
                              annotation_text="50% (random)", annotation_position="right")
                fig.add_hline(y=55, line_dash="dash", line_color="#D1FAE5",
                              annotation_text="55% (target)", annotation_position="right")
                fig.add_trace(_go.Scatter(
                    x=trend_df["pred_date"],
                    y=trend_df["dir_acc_pct"],
                    mode="lines+markers",
                    name="Dir. Accuracy",
                    line=dict(color=PRIMARY if _MODEL_TIER == "Higher reasoning" else
                              ("#6B7280" if _MODEL_TIER == "Lower reasoning" else "#2563EB"), width=2.5),
                    marker=dict(size=6),
                    hovertemplate="%{x}<br>Dir Acc: %{y:.1f}%<br>n=%{customdata}<extra></extra>",
                    customdata=trend_df["n"],
                ))
                fig.update_layout(
                    height=260, margin=dict(l=0, r=40, t=10, b=0),
                    paper_bgcolor="white", plot_bgcolor="white",
                    yaxis=dict(title="Directional Accuracy %", ticksuffix="%",
                               gridcolor="#F9FAFB", range=[0, 100],
                               title_font=dict(size=11), tickfont=dict(size=10)),
                    xaxis=dict(gridcolor="#F9FAFB", tickfont=dict(size=10)),
                    legend=dict(font=dict(size=10)),
                    font=dict(family="Inter, sans-serif"),
                )
                st.plotly_chart(fig, use_container_width=True)

        # ── Lookback window breakdown (from metrics_rolling, date only) ───────
        if not rolling_df.empty:
            rolling_df_num = rolling_df.copy()
            for col in ["directional_accuracy","mean_excess_return","num_predictions","lookback_days","brier_score"]:
                rolling_df_num[col] = pd.to_numeric(rolling_df_num[col], errors="coerce")

            st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)
            note = " (not model-filtered — metrics_rolling has no model column)" if _MODEL_TIER != "All models" else ""
            section_title("Rolling Window Breakdown", badge_text=f"metrics_rolling{note}")

            for lb in sorted(rolling_df_num["lookback_days"].dropna().unique()):
                lb_rows = rolling_df_num[rolling_df_num["lookback_days"] == lb]
                if lb_rows.empty: continue
                lb_row = lb_rows.iloc[0]
                lb_da = lb_row.get("directional_accuracy")
                lb_er = lb_row.get("mean_excess_return")
                lb_n  = int(lb_row.get("num_predictions", 0))
                st.markdown(
                    f'<div style="background:#FFFFFF;border:1px solid #F3F4F6;border-radius:10px;'
                    f'padding:12px 16px;margin-bottom:8px;display:flex;'
                    f'justify-content:space-between;align-items:center">'
                    f'<div>'
                    f'<div style="font-size:0.85rem;font-weight:700;color:#111827">'
                    f'{int(lb)}-day lookback window</div>'
                    f'<div style="font-size:0.72rem;color:#9CA3AF;margin-top:2px">'
                    f'{lb_n} predictions evaluated</div>'
                    f'</div>'
                    f'<div style="text-align:right">'
                    f'<div style="font-size:0.9rem;font-weight:800;color:{_acc_color(lb_da)}">'
                    f'{_pct(lb_da)}</div>'
                    f'<div style="font-size:0.72rem;color:{_rtn_color(lb_er)};font-weight:600">'
                    f'excess rtn {_pct(lb_er)}</div>'
                    f'</div></div>',
                    unsafe_allow_html=True,
                )

        # ── Raw data expander ──────────────────────────────────────────────────
        with st.expander("Raw model data", expanded=False):
            if not model_df.empty:
                disp = model_df[["model_name","model_provider","tier","total","correct",
                                  "dir_acc","avg_excess","avg_brier","in_range_pct"]].copy()
                for c in ["dir_acc","avg_excess","avg_brier","in_range_pct"]:
                    disp[c] = disp[c].round(4)
                disp = disp.rename(columns={
                    "model_name":"Model","model_provider":"Provider","tier":"Tier",
                    "total":"Total","correct":"Correct",
                    "dir_acc":"Dir Acc","avg_excess":"Excess Rtn",
                    "avg_brier":"Brier","in_range_pct":"In Range",
                })
                col_defs2 = [
                    {"field":"Model",      "width":180, "filter":"agTextColumnFilter",   "pinned":"left"},
                    {"field":"Provider",   "width":110, "filter":"agTextColumnFilter"},
                    {"field":"Tier",       "width":90,  "filter":"agTextColumnFilter"},
                    {"field":"Total",      "width":80,  "filter":"agNumberColumnFilter"},
                    {"field":"Correct",    "width":80,  "filter":"agNumberColumnFilter"},
                    {"field":"Dir Acc",    "width":90,  "filter":"agNumberColumnFilter",
                     "valueFormatter":"value != null ? (value*100).toFixed(1)+'%' : '—'"},
                    {"field":"Excess Rtn", "width":100, "filter":"agNumberColumnFilter",
                     "valueFormatter":"value != null ? (value*100).toFixed(2)+'%' : '—'"},
                    {"field":"Brier",      "width":80,  "filter":"agNumberColumnFilter",
                     "valueFormatter":"value != null ? value.toFixed(4) : '—'"},
                    {"field":"In Range",   "width":90,  "filter":"agNumberColumnFilter",
                     "valueFormatter":"value != null ? (value*100).toFixed(1)+'%' : '—'"},
                ]
                _aggrid(disp, col_defs2, height=220, key="model_grid")
