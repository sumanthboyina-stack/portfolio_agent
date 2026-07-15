"""
Chat rendering helpers.

Contains:
  - _build_plan()              : checks DB coverage + computes dynamic weights
  - _render_plan_card()        : Streamlit card shown before APEX runs
  - _render_prediction_card()  : full APEX prediction result card
"""

from __future__ import annotations

import html as _html
import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import streamlit as st

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from web.styles import (
    score_bar_html, ticker_label,
    REC_STYLES, SUCCESS, PRIMARY, WARNING, NEUTRAL, NEUTRAL_LIGHT,
)


# ── Pre-flight plan ───────────────────────────────────────────────────────────

def _build_plan(ticker: str) -> dict:
    from portfolio_agent.tools.fundamentals_db import get_stored_fundamentals
    from portfolio_agent.tools.research_db import get_stored_research
    from portfolio_agent.tools.prediction_db import get_latest_prediction

    plan: dict = {"ticker": ticker, "sources": [], "gaps": []}

    fund = get_stored_fundamentals(ticker)
    if fund:
        plan["sources"].append({
            "icon": "✅", "label": "Fundamentals",
            "detail": f"As of {fund.get('as_of_date','?')} · Score {fund.get('fundamental_score','?')}/10",
            "live": False,
        })
    else:
        plan["gaps"].append("fundamentals")
        plan["sources"].append({"icon": "⚡", "label": "Fundamentals",
                                "detail": "Not in DB -- live agent will run", "live": True})

    res = get_stored_research(ticker)
    if res:
        fetched = (res.get("raw_fetched_at") or res.get("as_of_date") or "?")[:10]
        stale_7d = fetched < (date.today() - timedelta(days=7)).isoformat() if fetched != "?" else False
        plan["sources"].append({
            "icon": "⚠️" if stale_7d else "✅",
            "label": "Broker Research",
            "detail": (
                f"Last fetched {fetched} (>7d old -- using cached) · {res.get('consensus','?')}"
                if stale_7d else
                f"Fetched {fetched} · {res.get('consensus','?')}"
            ),
            "live": False,
        })
    else:
        plan["gaps"].append("research")
        plan["sources"].append({"icon": "⚡", "label": "Broker Research",
                                "detail": "Not in DB -- live agent will run", "live": True})

    news_data = []
    try:
        cutoff = (date.today() - timedelta(days=7)).isoformat()
        conn = sqlite3.connect(str(_ROOT / "data" / "portfolio.db"))
        conn.row_factory = sqlite3.Row
        rows_news = conn.execute(
            """SELECT date, headline_1, headline_2, sentiment, sentiment_score, top_themes, trending
               FROM news_daily_update WHERE ticker=? AND date>=? AND row_type='ticker'
               ORDER BY date DESC""",
            [ticker, cutoff],
        ).fetchall()
        news_data = [dict(r) for r in rows_news]
        conn.close()
        cnt = len(news_data)
        if cnt > 0:
            plan["sources"].append({"icon": "✅", "label": "News (7d)",
                                    "detail": f"{cnt} records found", "live": False})
        else:
            plan["gaps"].append("news")
            plan["sources"].append({"icon": "⚡", "label": "News (7d)",
                                    "detail": "No recent records -- live agent will run", "live": True})
    except Exception:
        plan["gaps"].append("news")
        plan["sources"].append({"icon": "⚡", "label": "News (7d)",
                                "detail": "DB unavailable -- live agent will run", "live": True})

    plan["sources"].append({"icon": "📡", "label": "Macro Snapshot",
                            "detail": "VIX · 10Y yield · S&P trend (yfinance)", "live": True})

    prior = get_latest_prediction(ticker)
    if prior:
        plan["prior"] = prior
        plan["sources"].append({
            "icon": "📜", "label": "Prior Prediction",
            "detail": f"{prior.get('recommendation','?')} on {prior.get('created_at','')[:10]} "
                      f"· Confidence {prior.get('confidence','?')}/10",
            "live": False,
        })
    else:
        plan["sources"].append({"icon": "🆕", "label": "Prior Prediction",
                                "detail": "First analysis for this ticker", "live": False})

    try:
        from portfolio_agent.tools.reasoning_tools import get_macro_snapshot
        from portfolio_agent.tools.weight_engine import compute_dynamic_weights
        import json as _json
        macro = _json.loads(get_macro_snapshot())
        weight_data = compute_dynamic_weights(
            ticker=ticker,
            news_data=news_data,
            research_data=res or {},
            macro_snapshot=macro,
            fundamentals_data=fund or {},
        )
        plan["weight_data"] = weight_data
    except Exception:
        plan["weight_data"] = None

    return plan


# ── Plan card ─────────────────────────────────────────────────────────────────

def _render_plan_card(plan: dict) -> None:
    ticker = plan["ticker"]
    gaps   = plan.get("gaps", [])
    wd     = plan.get("weight_data")

    rows = ""
    for s in plan["sources"]:
        dot_color = "#DC2626" if s["live"] else "#059669"
        rows += (
            f'<div style="display:flex;gap:10px;padding:8px 0;border-bottom:1px solid #F1F5F9">'
            f'<span style="font-size:1rem;width:20px;text-align:center">{s["icon"]}</span>'
            f'<div style="flex:1">'
            f'<span style="font-weight:600;font-size:0.875rem;color:#0F172A">{s["label"]}</span>'
            f'<br><span style="font-size:0.8rem;color:#64748B">{s["detail"]}</span></div>'
            f'<span style="width:8px;height:8px;border-radius:50%;background:{dot_color};'
            f'margin-top:6px;flex-shrink:0;display:inline-block"></span>'
            f'</div>'
        )

    live_banner = ""
    if gaps:
        live_banner = (
            f'<div style="background:#EFF6FF;border:1px solid #BFDBFE;border-radius:8px;'
            f'padding:8px 12px;margin-bottom:12px;font-size:0.83rem;color:#1E40AF">'
            f'⚡ <strong>{len(gaps)} live agent(s)</strong> will run to fill data gaps: '
            f'{", ".join(gaps)}. Results stored for future use.</div>'
        )

    BASE = {"fundamentals": 0.40, "research": 0.30, "macro": 0.20, "news": 0.10}
    if wd and wd.get("weights"):
        w = wd["weights"]
        regime = wd.get("regime", "QUIET_DAY")
        regime_colors = {
            "EARNINGS_RELEASE": ("#DC2626", "#FEF2F2"),
            "LEADERSHIP_CHANGE": ("#DC2626", "#FEF2F2"),
            "MA_EVENT": ("#D97706", "#FFFBEB"),
            "EXTREME_VOLATILITY": ("#DC2626", "#FEF2F2"),
            "HIGH_VOLATILITY": ("#D97706", "#FFFBEB"),
            "RISK_OFF": ("#D97706", "#FFFBEB"),
            "RISK_ON": ("#059669", "#F0FDF4"),
            "SECTOR_ROTATION": ("#7C3AED", "#F5F3FF"),
            "RESEARCH_BOOST": ("#2563EB", "#EFF6FF"),
            "QUIET_DAY": ("#64748B", "#F8FAFC"),
        }
        rc, rb = regime_colors.get(regime, ("#64748B", "#F8FAFC"))

        def _delta(key: str) -> str:
            diff = round((w.get(key, 0) - BASE.get(key, 0)) * 100)
            if diff > 0:
                return f'<span style="color:#059669;font-size:0.7rem"> +{diff}%</span>'
            elif diff < 0:
                return f'<span style="color:#DC2626;font-size:0.7rem"> {diff}%</span>'
            return ""

        weight_badge = (
            f'<div style="background:{rb};border:1px solid {rc}44;border-radius:8px;'
            f'padding:8px 12px;margin-bottom:12px">'
            f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">'
            f'<span style="background:{rc};color:white;padding:2px 8px;border-radius:4px;'
            f'font-size:0.72rem;font-weight:700;letter-spacing:0.04em">{regime.replace("_"," ")}</span>'
            f'<span style="font-size:0.78rem;color:{rc};font-weight:600">Dynamic weight regime</span>'
            f'</div>'
            f'<div style="display:flex;gap:16px;flex-wrap:wrap">'
            + "".join(
                f'<span style="font-size:0.8rem;color:#374151">'
                f'<strong style="color:#0F172A">{lbl}</strong> '
                f'{round(w.get(key,0)*100)}%{_delta(key)}</span>'
                for lbl, key in [
                    ("📋 Fundamentals", "fundamentals"), ("🔬 Research", "research"),
                    ("🌐 Macro", "macro"), ("📰 News", "news"),
                ]
            )
            + f'</div>'
            + (
                f'<div style="margin-top:4px;font-size:0.75rem;color:#64748B">'
                f'{wd.get("weight_summary","")}</div>'
                if wd.get("weight_summary") else ""
            )
            + f'</div>'
        )
    else:
        weight_badge = (
            f'<div style="background:#F8FAFC;border:1px solid #E2E8F0;border-radius:8px;'
            f'padding:6px 12px;margin-bottom:12px;font-size:0.78rem;color:#64748B">'
            f'📋 Fundamentals 40% · 🔬 Research 30% · 🌐 Macro 20% · 📰 News 10% '
            f'<em>(default weights -- macro fetch pending)</em></div>'
        )

    steps_html = "".join(
        f'<div style="display:flex;gap:8px;padding:5px 0;font-size:0.83rem;color:#475569">'
        f'<span style="font-weight:700;color:#2563EB;min-width:20px">{i}.</span>'
        f'<span>{s}</span></div>'
        for i, s in enumerate([
            "Load all stored context + live macro snapshot",
            "Panel opening: each analyst states score & verdict",
            "Cross-examination: analysts challenge divergent views",
            "History check: compare with prior recommendations",
            "Weighted synthesis using the dynamic weights above → final recommendation",
        ], 1)
    )

    st.markdown(
        f'<div style="background:white;border:1px solid #E2E8F0;border-radius:12px;'
        f'padding:16px 20px;margin-bottom:12px;box-shadow:0 1px 3px rgba(0,0,0,0.05)">'
        f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">'
        f'<h4 style="margin:0;color:#0F172A;font-size:1rem">🗺️ Execution Plan -- <code>{ticker_label(ticker)}</code></h4>'
        f'</div>'
        f'{weight_badge}'
        f'{live_banner}{rows}'
        f'<details style="margin-top:10px"><summary style="cursor:pointer;color:#64748B;'
        f'font-size:0.8rem;font-weight:600">Deliberation steps</summary>'
        f'<div style="margin-top:8px">{steps_html}</div></details>'
        f'</div>',
        unsafe_allow_html=True,
    )


# ── Prediction result card ────────────────────────────────────────────────────

def _render_prediction_card(data: dict, elapsed: float = 0.0,
                             show_history: bool = True) -> None:
    rec   = data.get("recommendation", "HOLD")
    pred  = data.get("prediction", "NEUTRAL")
    col, bg, label = REC_STYLES.get(rec, (NEUTRAL, NEUTRAL_LIGHT, rec))
    conf  = data.get("confidence")
    comp  = data.get("composite_score")

    wu = data.get("weights_used") or {}
    def _wpct(key: str, fallback: float) -> str:
        v = wu.get(key)
        return f"{round(v*100)}%" if v is not None else f"{round(fallback*100)}%"

    scores_html = "".join(
        f'<div class="pred-score-item">'
        f'<label>{lbl} ({_wpct(wkey, fb)})</label>'
        f'{score_bar_html(data.get(key), color=bar_col)}'
        f'</div>'
        for lbl, key, wkey, fb, bar_col in [
            ("Fundamentals", "fundamental_score", "fundamentals", 0.40, SUCCESS),
            ("Research",     "research_score",    "research",     0.30, PRIMARY),
            ("Macro",        "macro_score",        "macro",        0.20, WARNING),
            ("News",         "news_score",         "news",         0.10, "#7C3AED"),
        ]
    )

    panel = data.get("panel_summary") or {}
    panel_html = ""
    if panel:
        names = [
            ("Fundamentals Analyst", "chen_verdict",  "Fundamentals"),
            ("Research Analyst",     "webb_verdict",  "Research"),
            ("Macro Analyst",        "varga_verdict", "Macro"),
            ("News Analyst",         "park_verdict",  "News"),
        ]
        panel_html = '<div style="margin-top:14px;border-top:1px solid #F1F5F9;padding-top:12px">'
        panel_html += '<div style="margin:0 0 8px;font-size:0.8rem;font-weight:700;color:#64748B;text-transform:uppercase;letter-spacing:0.05em">Panel Verdicts</div>'
        for name, key, role in names:
            verdict = _html.escape(str(panel.get(key, "--")))
            panel_html += (
                f'<div style="display:flex;gap:8px;margin-bottom:6px;font-size:0.83rem">'
                f'<span style="font-weight:600;color:#475569;min-width:130px">{name}</span>'
                f'<span style="color:#64748B">{role} ·</span>'
                f'<span style="color:#0F172A">{verdict}</span></div>'
            )
        if panel.get("key_debate"):
            panel_html += (
                f'<div style="background:#FEF9C3;border:1px solid #FDE68A;border-radius:6px;'
                f'padding:6px 10px;margin-top:8px;font-size:0.8rem;color:#92400E">'
                f'💬 <strong>Key debate:</strong> {_html.escape(str(panel["key_debate"]))}</div>'
            )
        panel_html += "</div>"

    pt_html = ""
    _pt_mean   = data.get("pt_mean")
    _pt_median = data.get("pt_median")
    _pt_high   = data.get("pt_high")
    _pt_low    = data.get("pt_low")
    _pt_n      = data.get("pt_num_analysts")
    _pt_cur    = data.get("pt_current_price")
    if any(v is not None for v in [_pt_mean, _pt_median, _pt_high, _pt_low]):
        def _pt_fmt(v, cur=None):
            if v is None:
                return '<span style="color:#94A3B8">--</span>'
            s = f"${v:,.2f}"
            if cur:
                pct = (v - cur) / cur * 100
                sign = "+" if pct >= 0 else ""
                color = "#059669" if pct >= 0 else "#DC2626"
                s += f' <span style="color:{color};font-size:0.75rem">({sign}{pct:.1f}%)</span>'
            return s
        _n_str = f"{_pt_n} analyst{'s' if _pt_n and _pt_n != 1 else ''}" if _pt_n else ""
        _n_chip = (
            f' &nbsp;&middot;&nbsp; <span style="font-weight:400">{_n_str}</span>'
            if _n_str else ""
        )
        _cur_chip = (
            f' &nbsp;&middot;&nbsp; <span style="color:#64748B;font-weight:400">'
            f'Current ${_pt_cur:,.2f}</span>'
            if _pt_cur else ""
        )
        pt_html = (
            f'<div style="margin-top:14px;border-top:1px solid #F1F5F9;padding-top:12px">'
            f'<div style="font-size:0.8rem;font-weight:700;color:#64748B;text-transform:uppercase;'
            f'letter-spacing:0.05em;margin-bottom:8px">Analyst Price Targets'
            f'{_n_chip}{_cur_chip}</div>'
            f'<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px">'
        )
        for lbl, val, show_pct in [
            ("Mean", _pt_mean, True), ("Median", _pt_median, True),
            ("High", _pt_high, True), ("Low",    _pt_low,    True),
        ]:
            pt_html += (
                f'<div style="background:#F8FAFC;border:1px solid #E2E8F0;border-radius:8px;'
                f'padding:8px 10px;text-align:center">'
                f'<div style="font-size:0.72rem;font-weight:600;color:#64748B;'
                f'text-transform:uppercase;margin-bottom:4px">{lbl}</div>'
                f'<div style="font-size:0.9rem;font-weight:700;color:#0F172A">'
                f'{_pt_fmt(val, _pt_cur if show_pct else None)}</div>'
                f'</div>'
            )
        pt_html += '</div></div>'

    horizons_html = ""
    _horizons = data.get("horizons") or []
    if _horizons and isinstance(_horizons, list):
        _dir_color = {"UP": "#059669", "DOWN": "#DC2626", "FLAT": "#64748B"}
        _dir_icon  = {"UP": "↑", "DOWN": "↓", "FLAT": "→"}
        _hz_label  = {5: "5d · Week", 21: "21d · Month", 63: "63d · Quarter"}
        rows_html = ""
        for _hrow in sorted(_horizons, key=lambda x: x.get("horizon_days", 99)):
            _hd   = _hrow.get("horizon_days", "?")
            _dir  = (_hrow.get("predicted_direction") or "").upper()
            _rlo  = _hrow.get("predicted_return_low")
            _rhi  = _hrow.get("predicted_return_high")
            _conv = _hrow.get("conviction_score")
            _rtxt = _hrow.get("reasoning_text", "")
            _dcol = _dir_color.get(_dir, "#64748B")
            _dico = _dir_icon.get(_dir, "?")
            _range_str = (
                f'<span style="color:{_dcol};font-weight:700">'
                f'{_dico} {_rlo:+.1f}% to {_rhi:+.1f}%</span>'
                if _rlo is not None and _rhi is not None
                else f'<span style="color:{_dcol};font-weight:700">{_dico} {_dir}</span>'
                if _dir else '<span style="color:#94A3B8">--</span>'
            )
            _conv_str = (
                f'<span style="font-size:0.8rem;color:#475569">{_conv}/10</span>'
                if _conv is not None else '<span style="color:#94A3B8">--</span>'
            )
            _lbl = _hz_label.get(_hd, f"{_hd}d")
            rows_html += (
                f'<tr>'
                f'<td style="padding:6px 10px;font-weight:600;color:#0F172A;white-space:nowrap">{_lbl}</td>'
                f'<td style="padding:6px 10px">{_range_str}</td>'
                f'<td style="padding:6px 10px;text-align:center">{_conv_str}</td>'
                f'<td style="padding:6px 10px;font-size:0.78rem;color:#475569">'
                f'{_html.escape(_rtxt[:120])}</td>'
                f'</tr>'
            )
        if rows_html:
            horizons_html = (
                f'<div style="margin-top:14px;border-top:1px solid #F1F5F9;padding-top:12px">'
                f'<div style="font-size:0.8rem;font-weight:700;color:#64748B;'
                f'text-transform:uppercase;letter-spacing:0.05em;margin-bottom:8px">'
                f'Horizon Forecasts</div>'
                f'<table style="width:100%;border-collapse:collapse;font-size:0.83rem">'
                f'<thead><tr style="background:#F8FAFC">'
                f'<th style="padding:5px 10px;text-align:left;color:#64748B;font-weight:600;font-size:0.75rem">Horizon</th>'
                f'<th style="padding:5px 10px;text-align:left;color:#64748B;font-weight:600;font-size:0.75rem">Return Range</th>'
                f'<th style="padding:5px 10px;text-align:center;color:#64748B;font-weight:600;font-size:0.75rem">Conviction</th>'
                f'<th style="padding:5px 10px;text-align:left;color:#64748B;font-weight:600;font-size:0.75rem">Rationale</th>'
                f'</tr></thead><tbody>{rows_html}</tbody></table></div>'
            )

    changed_html = ""
    if data.get("changed_from_previous"):
        changed_html = (
            f'<div style="background:#FEF3C7;border:1px solid #FDE68A;border-radius:6px;'
            f'padding:6px 10px;margin-top:8px;font-size:0.8rem;color:#92400E">'
            f'🔄 Recommendation changed from '
            f'<strong>{data.get("previous_recommendation","?")}</strong> → '
            f'<strong>{rec.replace("_"," ")}</strong></div>'
        )

    target_html = ""
    if data.get("target_price"):
        target_html = (
            f'<span style="background:#F0FDF4;color:#166534;border:1px solid #BBF7D0;'
            f'border-radius:6px;padding:3px 10px;font-size:0.8rem;font-weight:600;margin-right:6px">'
            f'🎯 Target ${data["target_price"]:.2f}</span>'
        )
    horizon_html = (
        f'<span style="background:#F1F5F9;color:#475569;border-radius:6px;'
        f'padding:3px 10px;font-size:0.8rem;font-weight:600">⏱ {data.get("horizon","1m")}</span>'
    )

    data_sources_html = (
        "&nbsp;· " + " · ".join(data.get("data_sources", []))
        if data.get("data_sources") else ""
    )
    reasoning_html = (
        f'<div style="margin:12px 0 0;font-size:0.875rem;color:#0F172A;line-height:1.6;'
        f'border-top:1px solid #F1F5F9;padding-top:12px">'
        f'{_html.escape(data.get("reasoning", ""))}</div>'
        if data.get("reasoning") else ""
    )

    card_html = (
        f'<div class="pred-card">'
        f'<div class="pred-header" style="background:{bg}18;border-bottom:2px solid {col}22">'
        f'<div style="display:flex;justify-content:space-between;align-items:flex-start">'
        f'<div>'
        f'<span style="font-size:1.5rem;font-weight:800;color:{col}">{label}</span>'
        f'<span style="font-size:0.85rem;color:{NEUTRAL};margin-left:10px">'
        f'{pred} · {_html.escape(ticker_label(str(data.get("ticker",""))))}</span>'
        f'<div style="margin-top:6px">{target_html}{horizon_html}</div>'
        f'</div>'
        f'<div style="text-align:right">'
        f'<div style="font-size:0.78rem;color:#64748B;font-weight:600;text-transform:uppercase;letter-spacing:0.04em">Composite</div>'
        f'<div style="font-size:1.8rem;font-weight:800;color:#0F172A;line-height:1.1">{comp or "--"}'
        f'<span style="font-size:1rem;color:#94A3B8">/10</span></div>'
        f'<div style="font-size:0.78rem;color:#64748B">Confidence: {conf or "--"}/10</div>'
        f'</div></div></div>'
        f'<div class="pred-body">'
        f'<div class="pred-scores">{scores_html}</div>'
        f'{pt_html}'
        f'{horizons_html}'
        f'{panel_html}'
        f'{changed_html}'
        f'{reasoning_html}'
        f'<div style="margin:10px 0 0;font-size:0.75rem;color:#94A3B8">'
        f'Generated in {elapsed:.1f}s{data_sources_html}</div>'
        f'</div></div>'
    )
    st.markdown(card_html, unsafe_allow_html=True)

    # Chart only for a freshly-generated card (show_history=True), not on replay of
    # past messages -- otherwise every historical prediction in the session would
    # re-fetch live price data on each page load.
    if show_history and data.get("ticker"):
        try:
            from web.components.predictions_charts import _plot_price_chart
            fig_price = _plot_price_chart(data["ticker"], period="6mo")
        except Exception:
            fig_price = None
        if fig_price is not None:
            with st.expander("📉 Price Chart", expanded=True):
                st.plotly_chart(fig_price, use_container_width=True,
                                 config={"displayModeBar": False})

    if show_history:
        from portfolio_agent.tools.prediction_db import get_prediction_history
        hist = get_prediction_history(data.get("ticker",""), limit=6)
        if len(hist) > 1:
            with st.expander("📜 Prediction history for this ticker"):
                rows = [
                    {"Date": h.get("created_at","")[:10],
                     "Horizon": f"{h['horizon_days']}d" if h.get("horizon_days") else h.get("horizon","--"),
                     "Recommendation": h.get("recommendation",""),
                     "Direction": h.get("predicted_direction","--"),
                     "Return Range": (
                         f"{h['predicted_return_low']:+.1f}% to {h['predicted_return_high']:+.1f}%"
                         if h.get("predicted_return_low") is not None and h.get("predicted_return_high") is not None
                         else "--"
                     ),
                     "Conviction": h.get("conviction_score","--"),
                     "Confidence": h.get("confidence",""),
                     "Composite": h.get("composite_score",""),
                     "Changed": "↑↓" if h.get("changed_from_previous") else "--"}
                    for h in hist
                ]
                st.dataframe(rows, hide_index=True, use_container_width=True)
