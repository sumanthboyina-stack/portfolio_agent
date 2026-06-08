"""Portfolio Intelligence — Home Dashboard."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import streamlit as st

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from web.styles import (
    inject_global_css, top_nav, page_header, section_title, card,
    stat_card_html, rec_badge_html, badge_html,
    REC_STYLES, SUCCESS, WARNING, DANGER, PRIMARY, NEUTRAL,
)

_DB   = _ROOT / "data" / "portfolio.db"
_LOGS = _ROOT / "logs"

st.set_page_config(
    page_title="Portfolio Intelligence",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)
inject_global_css()
top_nav("dashboard")


# ── DB helpers ────────────────────────────────────────────────────────────────

def _db_stats() -> dict:
    if not _DB.exists():
        return {}

    from portfolio_agent.tools.db import db_conn
    from portfolio_agent.tools.prediction_db import get_all_latest_predictions

    stats: dict = {}
    schema_errors: list[str] = []
    today = datetime.now().strftime("%Y-%m-%d")

    try:
        with db_conn(_DB) as c:
            for tbl, key in [
                ("fundamentals",      "fundamentals_rows"),
                ("news_daily_update", "news_rows"),
                ("research",          "research_rows"),
                ("predictions",       "prediction_rows"),
                ("chat_sessions",     "chat_sessions"),
                ("holdings",          "holdings_rows"),
            ]:
                try:
                    stats[key] = c.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                except Exception:
                    stats[key] = None
                    schema_errors.append(tbl)

            for tbl, key in [
                ("fundamentals",  "tickers_fundamentals"),
                ("research",      "tickers_research"),
                ("predictions",   "tickers_predictions"),
            ]:
                try:
                    stats[key] = c.execute(
                        f"SELECT COUNT(DISTINCT ticker) FROM {tbl}"
                    ).fetchone()[0]
                except Exception:
                    stats[key] = None
                    schema_errors.append(tbl)

            try:
                stats["news_today"] = c.execute(
                    "SELECT COUNT(*) FROM news_daily_update WHERE date=?", [today]
                ).fetchone()[0]
            except Exception:
                stats["news_today"] = None
                schema_errors.append("news_daily_update")

    except Exception as exc:
        stats["error"] = str(exc)
        return stats

    if schema_errors:
        stats["schema_errors"] = list(dict.fromkeys(schema_errors))

    try:
        preds = get_all_latest_predictions()
        top = sorted(
            preds.values(),
            key=lambda p: (
                p.prediction_date or "",
                p.created_at or "",
            ),
            reverse=True,
        )[:8]
        stats["top_predictions"] = [p.to_dict() for p in top]
    except Exception:
        stats["top_predictions"] = []

    return stats


def _latest_run() -> dict | None:
    if not _LOGS.exists():
        return None
    logs = sorted(_LOGS.glob("*_daily.log"), reverse=True)
    if not logs:
        return None
    latest = logs[0]
    stem = latest.stem
    date_part = stem.split("_")[0]
    try:
        run_date = datetime.strptime(date_part, "%Y%m%d")
    except ValueError:
        return None
    text     = latest.read_text(errors="replace")
    err_file = _LOGS / f"{date_part}_daily-error.log"
    err_text = err_file.read_text(errors="replace") if err_file.exists() else ""
    return {
        "date":       run_date,
        "has_finish": "Finished" in text or "phase done" in text.lower(),
        "has_error":  bool(err_text.strip()),
        "lines":      len(text.splitlines()),
        "date_str":   run_date.strftime("%b %d, %Y"),
    }


# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown(
        '<p style="font-size:0.65rem;font-weight:700;text-transform:uppercase;'
        'letter-spacing:0.1em;color:#4B5563;margin:0 0 12px">Pipeline Status</p>',
        unsafe_allow_html=True,
    )
    run = _latest_run()
    if run:
        status_color = "#10B981" if run["has_finish"] and not run["has_error"] \
            else ("#EF4444" if run["has_error"] else "#3B82F6")
        status_label = "Completed" if run["has_finish"] and not run["has_error"] \
            else ("Errored" if run["has_error"] else "Running")
        status_dot   = "●"
        st.markdown(
            f'<div style="background:#1F2937;border-radius:10px;padding:14px 16px;'
            f'border:1px solid #374151">'
            f'<div style="font-size:0.7rem;color:#6B7280;text-transform:uppercase;'
            f'letter-spacing:0.05em;font-weight:600">Last run</div>'
            f'<div style="font-size:0.95rem;font-weight:700;color:#F9FAFB;margin:4px 0 2px;'
            f'letter-spacing:-0.02em">{run["date_str"]}</div>'
            f'<div style="display:flex;align-items:center;gap:6px;margin-top:4px">'
            f'<span style="color:{status_color};font-size:0.7rem">{status_dot}</span>'
            f'<span style="font-size:0.78rem;color:{status_color};font-weight:600">'
            f'{status_label}</span></div>'
            f'<div style="font-size:0.68rem;color:#6B7280;margin-top:6px;'
            f'padding-top:6px;border-top:1px solid #374151">'
            f'{run["lines"]:,} log lines</div>'
            f'</div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<p style="font-size:0.82rem;color:#6B7280">No runs logged yet.</p>',
            unsafe_allow_html=True,
        )


# ── Main content ──────────────────────────────────────────────────────────────

page_header(
    "Portfolio Intelligence",
    subtitle="AI-powered equity research · Updated every 24 hours",
    icon="📈",
)

stats = _db_stats()
run   = _latest_run()

# ── Alerts ────────────────────────────────────────────────────────────────────
if stats.get("error"):
    st.error(f"Database error: {stats['error']}")
elif stats.get("schema_errors"):
    st.warning(f"DB schema issues in: {', '.join(stats['schema_errors'])} — some counts may be incomplete")

# ── Key Metrics ───────────────────────────────────────────────────────────────
c1, c2, c3, c4, c5, c6 = st.columns(6)

metrics = [
    (c1, str(stats.get("tickers_fundamentals") or "—"), "Tickers Analyzed",   "📋", PRIMARY),
    (c2, str(stats.get("news_today")           or "—"), "News Today",         "📰", SUCCESS),
    (c3, str(stats.get("tickers_predictions")  or "—"), "APEX Predictions",   "🤖", "#7C3AED"),
    (c4, str(stats.get("tickers_research")     or "—"), "Research Coverage",  "🔬", "#0891B2"),
    (c5, str(stats.get("holdings_rows")        or "—"), "Portfolio Holdings", "💼", WARNING),
    (c6, str(stats.get("news_rows")            or "—"), "Total News Records", "📡", NEUTRAL),
]
for col, val, label, icon, color in metrics:
    with col:
        st.markdown(stat_card_html(val, label, icon, color), unsafe_allow_html=True)

st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)

# ── Two-column layout ─────────────────────────────────────────────────────────
left, right = st.columns([3, 2], gap="large")

with left:
    section_title("Latest APEX Predictions", badge_text="Live", badge_color=SUCCESS)
    preds = stats.get("top_predictions", [])
    if preds:
        for p in preds:
            rec      = p.get("recommendation", "HOLD")
            col_hex, bg, _ = REC_STYLES.get(rec, (NEUTRAL, "#F9FAFB", rec))
            conf     = p.get("confidence")
            comp     = p.get("composite_score")
            date_str = (p.get("created_at") or "")[:10]
            pred_label = p.get("prediction", "")

            card(
                f'<div style="display:flex;justify-content:space-between;align-items:center">'
                f'<div style="display:flex;align-items:center;gap:12px">'
                f'<span style="font-size:1.05rem;font-weight:800;color:#111827;'
                f'letter-spacing:-0.02em">{p["ticker"]}</span>'
                f'{rec_badge_html(rec)}'
                f'<span style="background:#F3F4F6;color:#6B7280;padding:2px 8px;'
                f'border-radius:6px;font-size:0.76rem;font-weight:500">'
                f'{pred_label}</span>'
                f'</div>'
                f'<div style="text-align:right">'
                f'<span style="font-size:0.78rem;color:#9CA3AF">{date_str}</span>'
                f'<br><span style="font-size:0.73rem;color:#9CA3AF">'
                f'Conf {conf}/10 · Score {comp}/10</span>'
                f'</div></div>',
                padding="13px 18px",
            )
    else:
        st.info("No predictions yet. Open **APEX Chat** and analyze a ticker.", icon="💡")

    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
    if st.button("Open APEX Chat →", type="primary", use_container_width=True):
        st.switch_page("pages/4_🤖_Chat.py")

with right:
    # ── Run Status ────────────────────────────────────────────────────────────
    section_title("Daily Pipeline Status")
    if run:
        if run["has_finish"] and not run["has_error"]:
            st.success(f"Completed successfully — {run['date_str']}")
        elif run["has_error"]:
            st.error(f"Completed with errors — {run['date_str']}")
        else:
            st.info(f"In progress / incomplete — {run['date_str']}")
        st.caption(f"{run['lines']:,} log lines recorded")
    else:
        st.warning("No pipeline runs found yet.")

    st.markdown("<div style='height:20px'></div>", unsafe_allow_html=True)

    # ── Quick Actions ─────────────────────────────────────────────────────────
    section_title("Quick Actions")
    qa1, qa2 = st.columns(2)
    with qa1:
        if st.button("📊 Database", use_container_width=True):
            st.switch_page("pages/1_📊_Database.py")
        if st.button("📋 Watchlist", use_container_width=True):
            st.switch_page("pages/3_📋_Watchlist.py")
    with qa2:
        if st.button("🗓️ Schedule", use_container_width=True):
            st.switch_page("pages/2_🗓️_Schedule.py")
        if st.button("🤖 APEX Chat", use_container_width=True):
            st.switch_page("pages/4_🤖_Chat.py")

    st.markdown("<div style='height:20px'></div>", unsafe_allow_html=True)

    # ── System Health ─────────────────────────────────────────────────────────
    section_title("System Health")
    db_ok  = _DB.exists()
    wl_ok  = (_ROOT / "config" / "watchlist.yaml").exists()
    env_ok = (_ROOT / ".env").exists()

    for label, ok in [
        ("Database (SQLite)", db_ok),
        ("Watchlist config",  wl_ok),
        (".env credentials",  env_ok),
    ]:
        dot   = "●"
        color = "#10B981" if ok else "#EF4444"
        text  = "OK" if ok else "Missing"
        st.markdown(
            f'<div style="display:flex;justify-content:space-between;align-items:center;'
            f'padding:9px 0;border-bottom:1px solid #F9FAFB">'
            f'<span style="font-size:0.84rem;color:#374151;font-weight:500">{label}</span>'
            f'<span style="display:flex;align-items:center;gap:5px;font-size:0.78rem;'
            f'font-weight:600;color:{color}">'
            f'<span style="font-size:0.6rem">{dot}</span>{text}</span></div>',
            unsafe_allow_html=True,
        )
