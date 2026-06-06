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
        stats["schema_errors"] = list(dict.fromkeys(schema_errors))  # dedup, preserve order

    # Use the typed domain layer for top predictions — avoids duplicate-row issue
    # from same-second inserts and gives a clean typed API.
    try:
        preds = get_all_latest_predictions()
        top = sorted(
            preds.values(),
            key=lambda p: (p.composite_score or 0),
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
    st.markdown('<p style="font-size:0.7rem;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;color:#475569;margin:0 0 10px">Pipeline Status</p>', unsafe_allow_html=True)
    run = _latest_run()
    if run:
        status_color = "#059669" if run["has_finish"] and not run["has_error"] \
            else ("#DC2626" if run["has_error"] else "#3B82F6")
        status_label = "✅ Completed" if run["has_finish"] and not run["has_error"] \
            else ("❌ Errored" if run["has_error"] else "🔵 Running")
        st.markdown(
            f'<div style="background:#1E293B;border-radius:8px;padding:12px 14px">'
            f'<div style="font-size:0.78rem;color:#94A3B8">Last run</div>'
            f'<div style="font-size:0.9rem;font-weight:600;color:#F1F5F9;margin:2px 0">{run["date_str"]}</div>'
            f'<div style="font-size:0.8rem;color:{status_color};font-weight:600">{status_label}</div>'
            f'<div style="font-size:0.72rem;color:#475569;margin-top:4px">{run["lines"]:,} log lines</div>'
            f'</div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown('<p style="font-size:0.82rem;color:#475569">No runs logged yet.</p>', unsafe_allow_html=True)


# ── Main content ──────────────────────────────────────────────────────────────

page_header(
    "Portfolio Intelligence Dashboard",
    subtitle="Real-time AI equity research · Updated every 24h",
    icon="📈",
)

stats = _db_stats()
run   = _latest_run()

# ── Key Metrics Row ───────────────────────────────────────────────────────────
c1, c2, c3, c4, c5, c6 = st.columns(6)

if stats.get("error"):
    st.error(f"⚠️ Database error: {stats['error']}")
elif stats.get("schema_errors"):
    st.warning(f"⚠️ DB schema errors in tables: {', '.join(stats['schema_errors'])} — counts may be incomplete")

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

st.markdown("<br>", unsafe_allow_html=True)

# ── Two-column layout ─────────────────────────────────────────────────────────
left, right = st.columns([3, 2], gap="large")

with left:
    # ── APEX Latest Predictions ───────────────────────────────────────────────
    section_title("🤖 Latest APEX Predictions", badge_text="Live", badge_color=SUCCESS)
    preds = stats.get("top_predictions", [])
    if preds:
        for p in preds:
            rec  = p.get("recommendation", "HOLD")
            col_hex, bg, _ = REC_STYLES.get(rec, (NEUTRAL, "#F1F5F9", rec))
            conf = p.get("confidence")
            comp = p.get("composite_score")
            date_str = (p.get("created_at") or "")[:10]
            pred_label = p.get("prediction", "")

            card(
                f'<div style="display:flex;justify-content:space-between;align-items:center">'
                f'<div style="display:flex;align-items:center;gap:12px">'
                f'<span style="font-size:1.1rem;font-weight:800;color:#0F172A">{p["ticker"]}</span>'
                f'{rec_badge_html(rec)}'
                f'<span style="background:#F1F5F9;color:#64748B;padding:2px 8px;border-radius:6px;'
                f'font-size:0.78rem">{pred_label}</span>'
                f'</div>'
                f'<div style="text-align:right">'
                f'<span style="font-size:0.8rem;color:#64748B">{date_str}</span>'
                f'<br><span style="font-size:0.78rem;color:#94A3B8">'
                f'Conf: {conf}/10 · Score: {comp}/10</span>'
                f'</div></div>',
                padding="14px 18px",
            )
    else:
        st.info("No predictions yet. Open **APEX Chat** and analyze a ticker.", icon="💡")

    st.markdown("")
    if st.button("🤖 Open APEX Chat →", type="primary", use_container_width=True):
        st.switch_page("pages/4_🤖_Chat.py")

with right:
    # ── Run Status ────────────────────────────────────────────────────────────
    section_title("🗓️ Daily Pipeline Status")
    if run:
        if run["has_finish"] and not run["has_error"]:
            st.success(f"✅ Completed successfully on {run['date_str']}", icon="✅")
        elif run["has_error"]:
            st.error(f"❌ Completed with errors on {run['date_str']}", icon="⚠️")
        else:
            st.info(f"🔵 In progress / incomplete — {run['date_str']}", icon="🔵")
        st.caption(f"{run['lines']:,} log lines")
    else:
        st.warning("No pipeline runs found yet.", icon="⚠️")

    st.markdown("")

    # ── Quick Actions ─────────────────────────────────────────────────────────
    section_title("⚡ Quick Actions")

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

    st.markdown("")

    # ── System Health ─────────────────────────────────────────────────────────
    section_title("🔧 System")
    db_ok = _DB.exists()
    wl_ok = (_ROOT / "config" / "watchlist.yaml").exists()
    env_ok = (_ROOT / ".env").exists()

    for label, ok in [
        ("Database (SQLite)", db_ok),
        ("Watchlist config",  wl_ok),
        (".env credentials",  env_ok),
    ]:
        icon = "✅" if ok else "❌"
        status = "OK" if ok else "Missing"
        st.markdown(
            f'<div style="display:flex;justify-content:space-between;padding:6px 0;'
            f'border-bottom:1px solid #F1F5F9;font-size:0.875rem">'
            f'<span style="color:#475569">{label}</span>'
            f'<span style="font-weight:600;color:{"#059669" if ok else "#DC2626"}">'
            f'{icon} {status}</span></div>',
            unsafe_allow_html=True,
        )
