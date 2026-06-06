"""Database viewer — today's pipeline output only."""

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

st.set_page_config(
    page_title="Today's Data — Portfolio Intelligence",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)
inject_global_css()
top_nav("database")

with st.sidebar:
    st.markdown('<p style="font-size:0.7rem;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;color:#475569;margin:0 0 10px">Database</p>', unsafe_allow_html=True)

if not _DB.exists():
    page_header("Today's Data", icon="📊")
    st.warning("Database not found. Run `python main.py --daily` to initialise it.", icon="⚠️")
    st.stop()


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


def _count_today(table: str, date_col: str, extra_where: str = "") -> int:
    """Count rows for today in a table. extra_where is ANDed in if provided."""
    try:
        where = f"DATE({date_col}) = ?"
        if extra_where:
            where = f"{where} AND {extra_where}"
        with _conn() as c:
            return c.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", [_TODAY]).fetchone()[0]
    except Exception:
        return 0


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


# ── Today's counts ────────────────────────────────────────────────────────────
cnt_fund = _count_today("fundamentals",      "as_of_date")
cnt_news = _count_today("news_daily_update", "date", extra_where="row_type = 'ticker'")
cnt_res  = _count_today("research",          "raw_fetched_at")
cnt_pred = _count_today("predictions",       "created_at")
cnt_hold = _count_today("holdings",          "as_of_date")
cnt_metr = _count_today("metrics_rolling",   "metric_date")

page_header(
    "Today's Data",
    subtitle=(
        f"{datetime.now().strftime('%B %d, %Y')}  ·  "
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
                   WHERE DATE(as_of_date) = ?
                   ORDER BY ticker""",
                [_TODAY],
            ).fetchall()
    except Exception as exc:
        st.error(f"Query error: {exc}"); rows = []

    raw_df = pd.DataFrame([dict(r) for r in rows]) if rows else pd.DataFrame()

    if raw_df.empty:
        st.info(f"No fundamentals data for {_TODAY} yet.", icon="💡")
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
                   WHERE row_type = 'ticker' AND date = ?
                   ORDER BY ticker""",
                [_TODAY],
            ).fetchall()
    except Exception as exc:
        st.error(f"Query error: {exc}"); rows = []

    raw_df = pd.DataFrame([dict(r) for r in rows]) if rows else pd.DataFrame()

    if raw_df.empty:
        st.info(f"No news records for {_TODAY} yet.", icon="📰")
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
                   WHERE DATE(raw_fetched_at) = ?
                   ORDER BY research_score DESC NULLS LAST, ticker""",
                [_TODAY],
            ).fetchall()
    except Exception as exc:
        st.error(f"Query error: {exc}"); rows = []

    raw_df = pd.DataFrame([dict(r) for r in rows]) if rows else pd.DataFrame()

    if raw_df.empty:
        st.info(f"No research data for {_TODAY} yet.", icon="🔬")
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
                   WHERE DATE(created_at) = ?
                   ORDER BY composite_score DESC NULLS LAST, ticker""",
                [_TODAY],
            ).fetchall()
    except Exception as exc:
        st.error(f"Query error: {exc}"); rows = []

    raw_df = pd.DataFrame([dict(r) for r in rows]) if rows else pd.DataFrame()

    if raw_df.empty:
        st.info(f"No predictions for {_TODAY} yet. Open **APEX Chat** to generate your first recommendation.", icon="🤖")
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
                   WHERE DATE(as_of_date) = ?
                   ORDER BY current_value DESC NULLS LAST""",
                [_TODAY],
            ).fetchall()
    except Exception as exc:
        st.error(f"Query error: {exc}"); rows = []

    raw_df = pd.DataFrame([dict(r) for r in rows]) if rows else pd.DataFrame()

    if raw_df.empty:
        st.info(f"No holdings data for {_TODAY} yet.", icon="💼")
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
    try:
        with _conn() as c:
            rows = c.execute(
                """SELECT metric_date, horizon_days, segment, lookback_days,
                          directional_accuracy, in_range_pct, mean_excess_return,
                          mean_error_magnitude, high_conviction_accuracy,
                          low_conviction_accuracy, brier_score, mean_log_loss,
                          num_predictions, computed_at, system_version
                   FROM metrics_rolling
                   WHERE metric_date = ?
                   ORDER BY horizon_days""",
                [_TODAY],
            ).fetchall()
    except Exception as exc:
        st.error(f"Query error: {exc}"); rows = []

    raw_df = pd.DataFrame([dict(r) for r in rows]) if rows else pd.DataFrame()

    if raw_df.empty:
        st.info(f"No validation metrics for {_TODAY} yet. Metrics are computed after predictions mature.", icon="📈")
    else:
        df = raw_df.copy()
        pct_cols = ["directional_accuracy", "in_range_pct", "high_conviction_accuracy",
                    "low_conviction_accuracy"]
        for col in pct_cols + ["mean_excess_return", "mean_error_magnitude",
                               "brier_score", "mean_log_loss"]:
            df[col] = pd.to_numeric(df[col], errors="coerce").round(4)

        sm_cols = st.columns(max(len(df), 1))
        for i, (_, mrow) in enumerate(df.iterrows()):
            h  = mrow.get("horizon_days", "?")
            da = mrow.get("directional_accuracy")
            with sm_cols[i]:
                st.metric(
                    f"{h}d horizon",
                    f"{da*100:.0f}% dir. acc" if da is not None else "—",
                    delta=f"{mrow.get('mean_excess_return',0)*100:+.1f}% excess rtn" if mrow.get("mean_excess_return") is not None else None,
                )
        st.divider()

        grid_df = df[[
            "metric_date", "horizon_days", "lookback_days", "num_predictions",
            "directional_accuracy", "in_range_pct", "mean_excess_return",
            "mean_error_magnitude", "high_conviction_accuracy",
            "low_conviction_accuracy", "brier_score", "mean_log_loss",
            "segment", "system_version", "computed_at",
        ]].rename(columns={
            "metric_date":              "Date",
            "horizon_days":             "Horizon",
            "lookback_days":            "Lookback",
            "num_predictions":          "# Preds",
            "directional_accuracy":     "Dir Acc",
            "in_range_pct":             "In Range",
            "mean_excess_return":       "Excess Rtn",
            "mean_error_magnitude":     "Err Mag",
            "high_conviction_accuracy": "Hi Conv Acc",
            "low_conviction_accuracy":  "Lo Conv Acc",
            "brier_score":              "Brier",
            "mean_log_loss":            "Log Loss",
            "segment":                  "Segment",
            "system_version":           "Version",
            "computed_at":              "Computed",
        })

        section_title("Rolling Metrics", badge_text=f"{len(df)} rows", badge_color=PRIMARY)
        st.caption("Directional accuracy and return metrics from matured predictions")

        pct_fmt = "value != null ? (value * 100).toFixed(1) + '%' : '—'"
        col_defs = [
            {"field": "Date",        "width": 110, "filter": "agDateColumnFilter",   "pinned": "left"},
            {"field": "Horizon",     "width": 85,  "filter": "agNumberColumnFilter", "valueFormatter": "value + 'd'"},
            {"field": "Lookback",    "width": 85,  "filter": "agNumberColumnFilter", "valueFormatter": "value + 'd'"},
            {"field": "# Preds",     "width": 80,  "filter": "agNumberColumnFilter"},
            {"field": "Dir Acc",     "width": 90,  "filter": "agNumberColumnFilter", "valueFormatter": pct_fmt},
            {"field": "In Range",    "width": 85,  "filter": "agNumberColumnFilter", "valueFormatter": pct_fmt},
            {"field": "Excess Rtn",  "width": 95,  "filter": "agNumberColumnFilter", "valueFormatter": "value != null ? (value * 100).toFixed(2) + '%' : '—'"},
            {"field": "Err Mag",     "width": 85,  "filter": "agNumberColumnFilter", "valueFormatter": "value != null ? value.toFixed(4) : '—'"},
            {"field": "Hi Conv Acc", "width": 100, "filter": "agNumberColumnFilter", "valueFormatter": pct_fmt},
            {"field": "Lo Conv Acc", "width": 100, "filter": "agNumberColumnFilter", "valueFormatter": pct_fmt},
            {"field": "Brier",       "width": 80,  "filter": "agNumberColumnFilter", "valueFormatter": "value != null ? value.toFixed(4) : '—'"},
            {"field": "Log Loss",    "width": 85,  "filter": "agNumberColumnFilter", "valueFormatter": "value != null ? value.toFixed(4) : '—'"},
            {"field": "Segment",     "width": 100, "filter": "agTextColumnFilter"},
            {"field": "Version",     "width": 90,  "filter": "agTextColumnFilter"},
            {"field": "Computed",    "width": 155, "filter": "agTextColumnFilter"},
        ]
        _aggrid(grid_df, col_defs, height=420, key="metr_grid")
