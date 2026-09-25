"""Schedule & Logs — pipeline status, run controls, live log streaming."""

from __future__ import annotations

import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import streamlit as st

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from web.styles import (
    section_tile,
    inject_global_css, top_nav, page_header, section_title,
    badge_html, SUCCESS, WARNING, DANGER, PRIMARY, NEUTRAL,
    icon_html, material, status_dot_html,
)

_LOGS = _ROOT / "logs"
_MAIN = _ROOT / "main.py"
_CP   = _ROOT / "data" / "batch_checkpoint.json"
_DB   = _ROOT / "data" / "portfolio.db"
_LOGS.mkdir(parents=True, exist_ok=True)

from web.auth import current_context, require_login

st.set_page_config(
    page_title="APEX — Schedule",
    page_icon=str(_ROOT / "web" / "static" / "apex_mark.png"),
    layout="wide",
    initial_sidebar_state="expanded",
)
inject_global_css()
require_login()
top_nav("schedule")

# Sidebar button styling — must be injected into the page head, not inside the sidebar block
st.markdown("""
<style>
[data-testid="stSidebar"] button,
[data-testid="stSidebar"] button:focus {
    background-color: rgba(255,255,255,0.08) !important;
    color: #E5E7EB !important;
    border: 1px solid rgba(255,255,255,0.22) !important;
    font-weight: 600 !important;
    box-shadow: none !important;
}
[data-testid="stSidebar"] button:hover,
[data-testid="stSidebar"] button:active {
    background-color: rgba(255,255,255,0.18) !important;
    color: #FFFFFF !important;
    border: 1px solid rgba(255,255,255,0.4) !important;
    box-shadow: 0 2px 8px rgba(0,0,0,0.3) !important;
}
[data-testid="stSidebar"] button:disabled {
    background-color: rgba(255,255,255,0.03) !important;
    color: #6B7280 !important;
    border: 1px solid rgba(255,255,255,0.08) !important;
    opacity: 1 !important;
}
</style>
""", unsafe_allow_html=True)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _to_local(utc_str: str) -> str:
    """Convert a UTC ISO string from the DB to a local-time display string."""
    try:
        return datetime.fromisoformat(utc_str).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return (utc_str or "")[:19].replace("T", " ")


from web.lib import pid_alive as _is_pid_alive


def _db_conn() -> sqlite3.Connection | None:
    if not _DB.exists():
        return None
    try:
        conn = sqlite3.connect(str(_DB), timeout=5, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


def _load_run_from_db(run_id: str) -> dict | None:
    """Return run + phase rows from DB, or None if not found."""
    conn = _db_conn()
    if conn is None:
        return None
    try:
        run = conn.execute(
            "SELECT * FROM pipeline_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if run is None:
            return None
        phases = conn.execute(
            "SELECT * FROM pipeline_phase_progress WHERE run_id = ?", (run_id,)
        ).fetchall()
        return {"run": dict(run), "phases": {p["phase"]: dict(p) for p in phases}}
    except Exception:
        return None
    finally:
        conn.close()


def _load_db_runs(limit: int = 40) -> list[dict]:
    """Return recent pipeline runs from DB, newest first."""
    conn = _db_conn()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT * FROM pipeline_runs ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        result = []
        for row in rows:
            rd = dict(row)
            phases = conn.execute(
                "SELECT * FROM pipeline_phase_progress WHERE run_id = ?", (rd["run_id"],)
            ).fetchall()
            rd["phases"] = {p["phase"]: dict(p) for p in phases}
            result.append(rd)
        return result
    except Exception:
        return []
    finally:
        conn.close()


def _start_job(flag: str) -> tuple[int, Path, str]:
    """Launch job in background, piping stdout+stderr to a timestamped log file."""
    import shlex
    ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
    job_label = {
        "--batch morning":       "morning",
        "--batch intraday":      "intraday",
        "--batch evening":       "evening",
        "--daily --force-all":   "daily",
        "--daily":               "daily",
        "--daily-fundamentals":  "fundamentals",
        "--daily-research":      "research",
        "--daily-news":          "news",
        "--weekly-analysis":     "weekly",
    }.get(flag, flag.replace("-", "").replace(" ", "_")[:20])
    run_id    = f"{ts}_{job_label}"
    log_path  = _LOGS / f"{run_id}.log"
    log_file  = open(log_path, "w", buffering=1)   # line-buffered
    env       = {**os.environ, "PIPELINE_RUN_ID": run_id, "PIPELINE_TRIGGER": "web"}
    # Split compound flags like "--batch intraday" into separate argv elements
    flag_args = shlex.split(flag)
    proc = subprocess.Popen(
        ["python", "-u", str(_MAIN)] + flag_args,   # -u = unbuffered stdout
        cwd=str(_ROOT),
        stdout=log_file,
        stderr=subprocess.STDOUT,                   # merge stderr into stdout
        env=env,
    )
    return proc.pid, log_path, run_id


def _scan_logs() -> list[dict]:
    """Return metadata for every daily/news log file, newest first."""
    runs = []
    for log in _LOGS.glob("*.log"):
        name = log.stem
        # New format:  YYYYMMDD_HHMMSS_daily / _news / _fundamentals / _research / _validation
        m = re.match(r"^(\d{8})_(\d{6})_(daily|news|fundamentals|research|validation|weekly)$", name)
        if m:
            try:
                run_dt   = datetime.strptime(f"{m.group(1)}_{m.group(2)}", "%Y%m%d_%H%M%S")
                job_type = m.group(3)
            except ValueError:
                continue
        else:
            # Legacy format:  MMDDYYYY_daily
            m2 = re.match(r"^(\d{8})_daily$", name)
            if not m2:
                continue
            try:
                run_dt   = datetime.strptime(m2.group(1), "%m%d%Y")
                job_type = "daily"
            except ValueError:
                continue

        text     = log.read_text(errors="replace")
        lines    = text.splitlines()
        lower    = text.lower()
        # Use the job-specific final marker — "phase done" alone would fire as soon
        # as Phase 1 (News) finishes, falsely completing a still-running daily job.
        if job_type == "daily":
            has_fin = "daily pipeline complete" in lower or "finished :" in lower
        elif job_type == "news":
            has_fin = "news phase done" in lower or "finished :" in lower
        elif job_type == "fundamentals":
            has_fin = "fundamentals phase done" in lower or "finished :" in lower
        elif job_type == "research":
            has_fin = "research phase done" in lower or "finished :" in lower
        elif job_type == "validation":
            has_fin = "validation complete." in lower or "finished :" in lower
        elif job_type == "weekly":
            has_fin = "weekly analysis complete" in lower or "finished :" in lower
        else:
            has_fin = "finished :" in lower
        # has_err only flags pipeline-stopping failures — transient provider
        # errors (Groq TPD, rate limits) that triggered failover are NOT errors
        # because the pipeline kept running and may have completed successfully.
        has_err  = any(k in lower for k in (
            "[error]", "traceback (most recent", "exception:",
            "[all models exhausted]",   # entire failover chain failed — pipeline stopped
        ))
        # has_fin takes priority: if the pipeline completed, show Completed even if
        # transient errors occurred along the way (fallbacks handled them).
        status   = "Completed" if has_fin \
                   else ("Errored"  if has_err else "Incomplete")
        runs.append({
            "datetime":        run_dt,
            "date_label":      run_dt.strftime("%b %d, %Y  %H:%M:%S"),
            "job_type":        job_type.upper(),
            "status":          status,
            "lines":           len(lines),
            "phase_counts":    _phase_ticker_counts(text, job_type),
            "has_error":       has_err,
            "log_path":        log,
        })
    return sorted(runs, key=lambda r: r["datetime"], reverse=True)[:40]


def _phase_ticker_counts(log_text: str, job_type: str) -> dict:
    """Return per-phase ticker counts as a dict with keys: news, research, fundamentals, apex."""
    prog  = _parse_progress(log_text)
    ph    = prog["phases"]
    total = prog["total_tickers"]

    def _fmt(key: str) -> str:
        p    = ph.get(key, {})
        done = p.get("completed", 0)
        tot  = p.get("total", 0) or total
        if done == 0 and tot == 0:
            return "—"
        return f"{done}/{tot}" if tot else str(done)

    def _fmt_news() -> str:
        p    = ph.get("news", {})
        done = p.get("completed", 0)
        tot  = p.get("total", 0) or total
        if done == 0 and tot == 0:
            return "—"
        base = f"{done}/{tot}" if tot else str(done)
        parts = []
        if p.get("filter_low_mat"):  parts.append(f"{p['filter_low_mat']} lt")
        if p.get("filter_no_new"):   parts.append(f"{p['filter_no_new']} nn")
        if p.get("filter_no_art"):   parts.append(f"{p['filter_no_art']} na")
        if parts:
            base += " (" + ", ".join(parts) + ")"
        return base

    # Validation/weekly-analysis have no ticker phases — show dashes everywhere
    if job_type in ("validation", "weekly"):
        return {"News": "—", "Research": "—", "Fundamentals": "—", "Predictions": "—"}
    # For single-phase jobs only populate the relevant column
    if job_type in ("news", "research", "fundamentals"):
        return {
            "News":         _fmt_news()          if job_type == "news"         else "—",
            "Research":     _fmt("research")     if job_type == "research"     else "—",
            "Fundamentals": _fmt("fundamentals") if job_type == "fundamentals" else "—",
            "Predictions":  "—",
        }
    return {
        "News":         _fmt_news(),
        "Research":     _fmt("research"),
        "Fundamentals": _fmt("fundamentals"),
        "Predictions":  _fmt("apex"),
    }


def _db_status_label(s: str, log_file: str = "", job_type: str = "daily") -> str:
    if s == "running" and log_file and Path(log_file).exists():
        try:
            txt = Path(log_file).read_text(errors="replace").lower()
            job = job_type.lower()
            done = (
                ("daily pipeline complete" in txt or "morning batch complete" in txt) if job == "daily"
                else "morning batch complete" in txt   if job == "morning"
                else "intraday batch complete" in txt  if job == "intraday"
                else "evening batch complete" in txt   if job == "evening"
                else f"{job} phase done" in txt        if job in ("news", "research", "fundamentals")
                else "validation complete." in txt     if job == "validation"
                else "weekly analysis complete" in txt if job == "weekly"
                else "batch complete" in txt or "pipeline complete" in txt
            )
            if done:
                return "Completed"
        except Exception:
            pass
    return {"completed": "Completed", "error": "Errored", "running": "Running"}.get(s, s.title())

def _validation_done_in_log(txt: str) -> bool:
    return "validation complete." in txt.lower()

def _read_log_tail(path: Path, max_lines: int = 300) -> str:
    if not path or not path.exists():
        return ""
    lines = path.read_text(errors="replace").splitlines()
    if len(lines) > max_lines:
        return f"[… {len(lines) - max_lines} earlier lines omitted …]\n" \
               + "\n".join(lines[-max_lines:])
    return "\n".join(lines)


def _phase_badge(text: str) -> str:
    """Detect the current phase from log text and return a coloured label."""
    lower = text.lower()
    if "phase 4" in lower or "apex phase" in lower:
        return f"{icon_html('smart_toy', 13)} Phase 4 – APEX"
    if "phase 3" in lower or "fundamentals phase" in lower:
        return f"{icon_html('description', 13)} Phase 3 – Fundamentals"
    if "phase 2" in lower or "research phase" in lower:
        return f"{icon_html('science', 13)} Phase 2 – Research"
    if "phase 1" in lower or "news phase" in lower:
        return f"{icon_html('newspaper', 13)} Phase 1 – News"
    return f"{icon_html('autorenew', 13)} Starting…"


# Phases with no meaningful per-ticker count (they run once per pipeline
# invocation) — rendered as a single done/pending indicator, not a X/Y bar.
_BINARY_PHASES = {
    "short_interest", "universe_screen", "price_backfill",
    "l1_outcome", "l2_metrics", "calibration_outcome", "l3_snapshot", "l4_screener_outcomes",
}


def _new_phases_dict() -> dict[str, dict]:
    """Base per-phase state, shared by the log-text parser and the DB reader."""
    base = dict(status="pending", total=0, completed=0, failed=0, ticker="", note="")
    return {
        "news":            {**base, "flagged": 0, "filter_analyzed": 0,
                             "filter_no_art": 0, "filter_low_mat": 0, "filter_no_new": 0},
        "short_interest":  dict(base),
        "research":        {**base, "track_a": 0},
        "fundamentals":    dict(base),
        "apex":            dict(base),
        "risk_technical":  dict(base),
        "universe_screen": dict(base),
        "price_backfill":  dict(base),
        "l1_outcome":      dict(base),
        "l2_metrics":      dict(base),
        "calibration_outcome": dict(base),
        "l3_snapshot":     dict(base),
        "l4_screener_outcomes": dict(base),
    }


def _parse_progress(log_text: str) -> dict:
    """
    Parse log output into structured phase/ticker progress.
    Returns a dict with total_tickers and per-phase state.
    """
    phases: dict[str, dict] = _new_phases_dict()
    total_tickers = 0
    current = None

    for line in log_text.splitlines():
        # ── total tickers ────────────────────────────────────────────────────
        m = re.search(r"Daily Job — (\d+) tickers total", line)
        if m:
            total_tickers = int(m.group(1))

        # ── phase markers (order: News → Short Interest → Research → Fundamentals
        #    → APEX → Risk/Technical → Universe Screen → Price Backfill) ────────
        if re.search(r"Phase 1.{0,20}News|── Phase 1", line):
            current = "news"
            phases["news"]["status"] = "running"
        elif re.search(r"── Short Interest Check", line):
            current = "short_interest"
            phases["short_interest"]["status"] = "running"
            if phases["news"]["status"] == "running":
                phases["news"]["status"] = "done"
        elif re.search(r"Phase 2.{0,20}Research|── Phase 2", line):
            current = "research"
            phases["research"]["status"] = "running"
            if phases["short_interest"]["status"] == "running":
                phases["short_interest"]["status"] = "done"
            if phases["news"]["status"] == "running":
                phases["news"]["status"] = "done"
        elif re.search(r"Phase 3.{0,20}Fundamentals|── Phase 3", line):
            current = "fundamentals"
            phases["fundamentals"]["status"] = "running"
            if phases["research"]["status"] == "running":
                phases["research"]["status"] = "done"
        elif re.search(r"Phase 4\.5.{0,20}Risk|── Phase 4\.5", line):
            current = "risk_technical"
            phases["risk_technical"]["status"] = "running"
            if phases["apex"]["status"] == "running":
                phases["apex"]["status"] = "done"
        elif re.search(r"Phase 4.{0,20}APEX|── Phase 4", line):
            current = "apex"
            phases["apex"]["status"] = "running"
            if phases["fundamentals"]["status"] == "running":
                phases["fundamentals"]["status"] = "done"
        elif re.search(r"Phase 5.{0,20}Universe Screen|── Phase 5", line):
            current = "universe_screen"
            phases["universe_screen"]["status"] = "running"
            if phases["risk_technical"]["status"] == "running":
                phases["risk_technical"]["status"] = "done"
        elif re.search(r"── Price History Backfill", line):
            current = "price_backfill"
            phases["price_backfill"]["status"] = "running"
            if phases["universe_screen"]["status"] == "running":
                phases["universe_screen"]["status"] = "done"

        # ── fundamentals: to-process count ───────────────────────────────────
        m = re.search(r"Fundamentals: (\d+) to process", line)
        if m:
            phases["fundamentals"]["total"] = int(m.group(1))

        # ── research track A ─────────────────────────────────────────────────
        m = re.search(r"Track A done — (\d+) fetched", line)
        if m:
            phases["research"]["track_a"] = int(m.group(1))
        m = re.search(r"Track B: (\d+) need LLM summary", line)
        if m:
            phases["research"]["total"] = int(m.group(1))

        # ── news: flagged/total count ─────────────────────────────────────────
        m = re.search(r"News Batch — (\d+) / \d+ tickers", line)
        if m:
            phases["news"]["flagged"] = int(m.group(1))
            phases["news"]["total"]   = int(m.group(1))
            # news-only runs have no "Phase 1" header — auto-enter news phase
            if current is None:
                current = "news"
                phases["news"]["status"] = "running"

        # ── news: batch-level progress  "── News Batch N/M: ..." ─────────────
        m = re.search(r"── News Batch (\d+)/(\d+):", line)
        if m and current == "news":
            batch_num   = int(m.group(1))
            batch_total = int(m.group(2))
            phases["news"]["note"] = f"batch {batch_num}/{batch_total}"
            # Only set total from batches if the triage line hasn't set it yet
            if phases["news"]["total"] == 0:
                phases["news"]["total"] = batch_total * 5  # rough estimate

        # ── news: filter breakdown  "[news/filter] total=N analyzed=N ..." ─────
        m = re.search(
            r"\[news/filter\] total=(\d+) analyzed=(\d+) "
            r"no_articles=(\d+) low_materiality=(\d+) no_new_articles=(\d+)", line
        )
        if m:
            phases["news"]["filter_analyzed"] = int(m.group(2))
            phases["news"]["filter_no_art"]   = int(m.group(3))
            phases["news"]["filter_low_mat"]  = int(m.group(4))
            phases["news"]["filter_no_new"]   = int(m.group(5))

        # ── news: count individual ticker saves  "[Model] news saved for AAPL" ──
        if re.search(r"\] news saved for ", line, re.IGNORECASE) and current == "news":
            phases["news"]["completed"] = phases["news"]["completed"] + 1

        # ── ticker [N/M] progress ─────────────────────────────────────────────
        m = re.search(r"^\[(\d+)/(\d+)\]\s+([A-Z0-9]+)", line)
        if m and current:
            done  = int(m.group(1)) - 1      # current is IN PROGRESS, not finished
            total = int(m.group(2))
            tick  = m.group(3)
            p = phases[current]
            p["total"]     = max(p["total"], total)
            p["completed"] = done
            p["ticker"]    = tick

        # ── phase completions ─────────────────────────────────────────────────
        if "Fundamentals phase done" in line:
            m = re.search(r"(\d+) saved, (\d+) failed", line)
            if m:
                phases["fundamentals"]["completed"] = int(m.group(1))
                phases["fundamentals"]["failed"]    = int(m.group(2))
            phases["fundamentals"]["status"] = "done"

        if "Research phase done" in line:
            m = re.search(r"Track B: (\d+) LLM summar", line)
            if m:
                phases["research"]["completed"] = int(m.group(1))
            m2 = re.search(r"Track A: (\d+) raw", line)
            if m2:
                phases["research"]["track_a"] = int(m2.group(1))
            phases["research"]["status"] = "done"

        if "News phase done" in line:
            # Explicit completion line — overrides incremental count
            m = re.search(r"(\d+) processed, (\d+) failed", line)
            if m:
                phases["news"]["completed"] = int(m.group(1))
                phases["news"]["failed"]    = int(m.group(2))
            phases["news"]["status"] = "done"

        # ── risk & technical: per-ticker progress + completion ────────────────
        m = re.search(r"\[(\d+)/(\d+)\]\s+(\S+)\s+—\s+risk=", line)
        if m and current == "risk_technical":
            phases["risk_technical"]["completed"] = int(m.group(1)) - 1
            phases["risk_technical"]["total"]     = int(m.group(2))
            phases["risk_technical"]["ticker"]    = m.group(3)
        if "Risk/Technical phase done" in line:
            m = re.search(r"(\d+) flag\(s\) saved.*?(\d+) failed", line)
            if m:
                phases["risk_technical"]["completed"] = int(m.group(1))
                phases["risk_technical"]["failed"]    = int(m.group(2))
            phases["risk_technical"]["status"] = "done"

        # ── universe screen: candidate summary ─────────────────────────────────
        m = re.search(r"\[universe\] (\d+) candidate\(s\) flagged", line)
        if m:
            phases["universe_screen"].update(
                note=f"{m.group(1)} candidates flagged", status="done", completed=1, total=1,
            )
        elif re.search(r"\[universe\] No signals today", line):
            phases["universe_screen"].update(
                note="no signals today", status="done", completed=1, total=1,
            )

        # ── price backfill: row-count summary ──────────────────────────────────
        m = re.search(r"Backfilled (\d+) price rows across (\d+) missing day", line)
        if m:
            phases["price_backfill"].update(
                note=f"{m.group(1)} rows backfilled", status="done", completed=1, total=1,
            )
        elif re.search(r"Price history up to date", line):
            phases["price_backfill"].update(
                note="up to date", status="done", completed=1, total=1,
            )

        # ── evening/validation layers ────────────────────────────────────────
        if re.search(r"── Layer 1: Outcome Assignment|Layer 1: Outcome Assignment ===", line):
            current = "l1_outcome"
            phases["l1_outcome"]["status"] = "running"
        if re.search(r"── Layer 2: Rolling Metrics|Layer 2: Rolling Metrics ===", line):
            current = "l2_metrics"
            phases["l2_metrics"]["status"] = "running"
            if phases["l1_outcome"]["status"] == "running":
                phases["l1_outcome"]["status"] = "done"
        if re.search(r"── Layer 2\.5: Score Calibration|Score Calibration — Outcome Fill ===", line):
            current = "calibration_outcome"
            phases["calibration_outcome"]["status"] = "running"
            if phases["l2_metrics"]["status"] == "running":
                phases["l2_metrics"]["status"] = "done"
        if re.search(r"── Layer 3: Portfolio Price Snapshot", line):
            current = "l3_snapshot"
            phases["l3_snapshot"]["status"] = "running"
            if phases["calibration_outcome"]["status"] == "running":
                phases["calibration_outcome"]["status"] = "done"
            elif phases["l2_metrics"]["status"] == "running":
                phases["l2_metrics"]["status"] = "done"
        if re.search(r"── Layer 4: Screener Outcome Scoring", line):
            current = "l4_screener_outcomes"
            phases["l4_screener_outcomes"]["status"] = "running"
            if phases["l3_snapshot"]["status"] == "running":
                phases["l3_snapshot"]["status"] = "done"
        m = re.search(r"L1: (\d+) evaluated\s+(\d+) data_missing\s+(\d+) errors", line)
        if m:
            phases["l1_outcome"].update(
                note=f"{m.group(1)} evaluated · {m.group(2)} missing · {m.group(3)} errors",
                status="done", completed=1, total=1,
            )
        m = re.search(r"L2: (\d+) metric rows written", line)
        if m:
            phases["l2_metrics"].update(
                note=f"{m.group(1)} metric rows written", status="done", completed=1, total=1,
            )
        m = re.search(r"L2\.5: (.+)|Calibration: (.+)", line)
        if m:
            phases["calibration_outcome"].update(
                note=(m.group(1) or m.group(2))[:160], status="done", completed=1, total=1,
            )
        m = re.search(r"L3: (\d+) holdings snapped", line)
        if m:
            phases["l3_snapshot"].update(
                note=f"{m.group(1)} holdings snapped", status="done", completed=1, total=1,
            )
        m = re.search(r"L4: (\d+) evaluated\s+(\d+) data_missing\s+(\d+) errors", line)
        if m:
            phases["l4_screener_outcomes"].update(
                note=f"{m.group(1)} evaluated · {m.group(2)} missing · {m.group(3)} errors",
                status="done", completed=1, total=1,
            )

        # ── exhausted / checkpoint ────────────────────────────────────────────
        # ── apex: total and completion ────────────────────────────────────────
        m = re.search(r"(\d+) / \d+ portfolio tickers → APEX", line)
        if m:
            phases["apex"]["total"] = int(m.group(1))
        # Batch progress: "── APEX Batch N/M: AAPL, MSFT, ..."
        m = re.search(r"APEX Batch (\d+)/(\d+):", line)
        if m and current == "apex":
            phases["apex"]["completed"] = int(m.group(1)) - 1
            phases["apex"]["total"]     = max(phases["apex"]["total"], int(m.group(2)))
        if "APEX phase done" in line:
            # Match "X tickers · Y failed" (current format) or legacy "X predictions saved, Y failed"
            m = re.search(r"(\d+) tickers? · (\d+) failed", line) or \
                re.search(r"(\d+) predictions saved, (\d+) failed", line)
            if m:
                phases["apex"]["completed"] = int(m.group(1))
                phases["apex"]["failed"]    = int(m.group(2))
            phases["apex"]["status"] = "done"
        # Per-ticker result: "[ModelLabel] APEX: AAPL → BUY (confidence=8)"
        m = re.search(r"\] APEX: (\w+) → (\w+) \(confidence=(\d+)\)", line)
        if m and current == "apex":
            phases["apex"]["note"] = f"{m.group(1)}: {m.group(2)} ({m.group(3)}/10)"

        if ("ALL MODELS EXHAUSTED" in line or "CREDIT EXHAUSTED" in line) and current:
            phases[current]["status"] = "exhausted"
            m = re.search(r"checkpoint — (\d+)", line)
            if m:
                phases[current]["note"] = f"{m.group(1)} tickers retry next run"

    return {"total_tickers": total_tickers, "phases": phases}


def _db_to_parsed(db_run: dict) -> dict:
    """Convert DB run data to the same format as _parse_progress() output."""
    run   = db_run["run"]
    dphases = db_run["phases"]
    total = run.get("total_tickers", 0)

    phases: dict[str, dict] = _new_phases_dict()
    for name, p in dphases.items():
        if name not in phases:
            continue
        db_status = p.get("status", "pending")
        phases[name].update({
            "status":    "exhausted" if db_status == "error" else db_status,
            "total":     p.get("total", 0),
            "completed": p.get("completed", 0),
            "failed":    p.get("failed", 0),
            "ticker":    p.get("current_ticker", ""),
            "note":      p.get("note", ""),
        })
    return {"total_tickers": total, "phases": phases, "run_status": run.get("status", "")}


def _render_progress(log_text: str, job_type: str) -> None:
    """Render milestone timeline + per-phase progress bars above the log."""
    prog = _parse_progress(log_text)
    _render_progress_parsed(prog, job_type)


def _render_progress_parsed(prog: dict, job_type: str) -> None:
    """Render milestone timeline + per-phase progress bars from pre-parsed dict."""
    total = prog["total_tickers"]
    ph    = prog["phases"]
    # Only set when prog came from _db_to_parsed() — log-only progress (still-
    # running jobs with no DB rows yet) has no run_status, so "pending" there
    # genuinely means "hasn't started," not "was skipped."
    run_finished = prog.get("run_status") in ("completed", "error")

    # Determine which phases are relevant for this job type
    _DAILY_PHASES = [
        "news", "short_interest", "research", "fundamentals", "apex",
        "risk_technical", "universe_screen", "price_backfill",
    ]
    phase_keys = {
        "daily":      _DAILY_PHASES,
        "morning":    _DAILY_PHASES,
        "intraday":   ["news", "research", "fundamentals", "apex", "price_backfill"],
        "evening":    ["l1_outcome", "l2_metrics", "calibration_outcome", "l3_snapshot", "l4_screener_outcomes"],
        "validation": ["l1_outcome", "l2_metrics", "calibration_outcome"],
        "weekly":     [],
    }.get(job_type, _DAILY_PHASES)

    # A finished run with every tracked phase still "pending" genuinely has
    # nothing to show (e.g. no DB rows at all); a run still in progress is
    # "starting up." Either way there's no bar data to render.
    if total == 0 and all(ph[k]["status"] == "pending" for k in phase_keys):
        st.caption(
            f"{icon_html('check_circle', 13, color=SUCCESS)} Run finished — no phase data recorded."
            if run_finished else
            f"{icon_html('hourglass_empty', 13)} Job starting up…",
            unsafe_allow_html=True,
        )
        return

    # Once the run has finished, a phase still "pending" wasn't skipped by the
    # UI — the pipeline decided there was nothing to do (or never reached it).
    # Render that as "skipped," not an indefinite "Waiting…".
    if run_finished:
        for k in phase_keys:
            if ph[k]["status"] == "pending":
                ph[k]["status"] = "skipped"

    # Icon values are Material Symbols names, rendered via icon_html() at each
    # use site below (color varies with phase status, so it can't be baked in here).
    PHASE_META = {
        "news":            ("newspaper",       "News",             "1 ·"),
        "short_interest":  ("trending_down",   "Short Interest",   "1.5 ·"),
        "research":        ("science",         "Research",         "2 ·"),
        "fundamentals":    ("description",     "Fundamentals",      "3 ·"),
        "apex":            ("smart_toy",       "APEX",              "4 ·"),
        "risk_technical":  ("shield",          "Risk & Technical", "4.5 ·"),
        "universe_screen": ("travel_explore",  "Universe Screen",   "5 ·"),
        "price_backfill":  ("calculate",       "Price Backfill",    "6 ·"),
        "l1_outcome":      ("check_circle",    "Outcome Scoring",   "1 ·"),
        "l2_metrics":      ("trending_up",     "Rolling Metrics",   "2 ·"),
        "calibration_outcome": ("calculate",   "Score Calibration", "2.5 ·"),
        "l3_snapshot":     ("photo_camera",    "Portfolio Snapshot","3 ·"),
        "l4_screener_outcomes": ("query_stats", "Screener Outcomes", "4 ·"),
    }
    STATUS_COLOR = {
        "pending":   "#475569",
        "running":   "#3B82F6",
        "done":      "#10B981",
        "exhausted": "#F59E0B",
        "skipped":   "#6B7280",
    }
    STATUS_DOT = {
        "pending":   "#475569",
        "running":   "#3B82F6",
        "done":      "#10B981",
        "exhausted": "#F59E0B",
        "skipped":   "#6B7280",
    }

    # ── milestone timeline ─────────────────────────────────────────────────────
    dot_html = ""
    for i, k in enumerate(phase_keys):
        status = ph[k]["status"]
        dot_color = STATUS_DOT[status]
        label_icon = PHASE_META[k][0]

        # connector line before the dot (skip for first)
        if i > 0:
            prev_done = ph[phase_keys[i - 1]]["status"] in ("done", "exhausted", "skipped")
            line_color = "#10B981" if prev_done else "#334155"
            dot_html += f'<div style="flex:1;height:3px;background:{line_color};margin:0 2px"></div>'

        dot_html += (
            f'<div style="display:flex;flex-direction:column;align-items:center;gap:4px">'
            f'<div style="width:18px;height:18px;border-radius:50%;background:{dot_color};'
            f'box-shadow:0 0 0 3px {dot_color}33;flex-shrink:0"></div>'
            f'<span style="font-size:0.75rem;color:{dot_color};white-space:nowrap">'
            f'{icon_html(label_icon, 13, color=dot_color)} {PHASE_META[k][1]}</span>'
            f'</div>'
        )

    batch_label = {
        "morning":  "MORNING BATCH",
        "intraday": "INTRADAY BATCH",
        "daily":    "FULL DAILY",
        "weekly":   "WEEKLY ANALYSIS",
    }.get(job_type, job_type.upper())
    total_label = f"· {total} tickers" if total else ""
    st.markdown(
        f'<div style="background:#1E293B;border:1px solid #334155;border-radius:10px;'
        f'padding:16px 20px;margin-bottom:12px">'
        f'<div style="font-size:0.78rem;font-weight:700;text-transform:uppercase;'
        f'letter-spacing:0.08em;color:#64748B;margin-bottom:14px">'
        f'{batch_label} {total_label}</div>'
        f'<div style="display:flex;align-items:center;gap:0;padding:0 8px">{dot_html}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # ── per-phase progress bars ────────────────────────────────────────────────
    bars_html = '<div style="display:flex;flex-direction:column;gap:8px;margin-top:4px">'
    for k in phase_keys:
        p      = ph[k]
        icon, label, phase_num = PHASE_META[k]
        status = p["status"]
        color  = STATUS_COLOR[status]

        is_binary = k in _BINARY_PHASES

        if status == "pending":
            pct      = 0
            bar_text = "Waiting…"
        elif status == "skipped":
            pct      = 100
            bar_text = f"{icon_html('skip_next', 12, color=color)} Skipped — not needed this run"
        elif status == "running":
            if is_binary:
                pct      = 0
                bar_text = p.get("note") or "Running…"
            else:
                pct        = int(p["completed"] / max(p["total"], 1) * 100)
                ticker_str = f" · {p['ticker']} in progress" if p["ticker"] else ""
                bar_text   = f"{p['completed']} / {p['total']} complete{ticker_str}"
                if k == "news" and p.get("note"):
                    bar_text += f" · {p['note']}"
        elif status == "done":
            pct = 100
            if is_binary:
                bar_text = p.get("note") or "Done"
            else:
                fail_str = f", {p['failed']} failed" if p.get("failed") else ""
                bar_text = f"{p['completed']} saved{fail_str}"
                if k == "research" and p.get("track_a"):
                    bar_text += f" · {p['track_a']} raw fetched"
                if k == "news":
                    parts = []
                    if p.get("filter_analyzed"): parts.append(f"{p['filter_analyzed']} analyzed")
                    if p.get("filter_low_mat"):  parts.append(f"{p['filter_low_mat']} low-mat")
                    if p.get("filter_no_new"):   parts.append(f"{p['filter_no_new']} no-new")
                    if p.get("filter_no_art"):   parts.append(f"{p['filter_no_art']} no-art")
                    if parts: bar_text += " · " + " | ".join(parts)
        else:  # exhausted
            pct      = int(p["completed"] / max(p["total"], 1) * 100)
            bar_text = (
                f"{p['completed']} / {p['total']} · "
                f"{icon_html('warning', 12, color=WARNING)} {p.get('note', 'models exhausted')}"
            )

        bg_track = "#1E293B"
        fill_color = color
        bars_html += (
            f'<div style="display:flex;align-items:center;gap:10px;min-height:28px">'
            f'<div style="width:160px;flex-shrink:0;font-size:0.82rem;font-weight:600;color:{color}">'
            f'{phase_num} {icon_html(icon, 14, color=color)} {label}</div>'
            f'<div style="flex:1;background:{bg_track};border-radius:6px;height:10px;overflow:hidden">'
            f'<div style="width:{pct}%;height:100%;background:{fill_color};border-radius:6px;'
            f'transition:width 0.4s ease"></div></div>'
            f'<div style="width:300px;flex-shrink:0;font-size:0.78rem;color:#94A3B8;'
            f'white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{bar_text}</div>'
            f'</div>'
        )
    bars_html += '</div>'
    st.markdown(bars_html, unsafe_allow_html=True)


# ── Session state init ────────────────────────────────────────────────────────

for key, default in [
    ("active_pid",    None),
    ("active_log",    None),
    ("active_job",    None),
    ("active_run_id", None),
]:
    if key not in st.session_state:
        st.session_state[key] = default

# Reconcile: clear stale pid if process has ended (DB status, PID check, or log content)
if st.session_state.active_pid:
    pid_done = not _is_pid_alive(st.session_state.active_pid)
    # DB is the most reliable — if run_id is marked completed/error, job is done
    db_done = False
    _active_run = _load_run_from_db(st.session_state.get("active_run_id") or "")
    if _active_run:
        db_done = _active_run["run"].get("status") in ("completed", "error")
    # Log check always runs — most reliable completion signal
    log_done = False
    if st.session_state.active_log and Path(st.session_state.active_log).exists():
        _log_txt = Path(st.session_state.active_log).read_text(errors="replace").lower()
        job = st.session_state.active_job or "daily"
        if job == "daily":
            log_done = "daily pipeline complete" in _log_txt or "morning batch complete" in _log_txt
        elif job == "morning":
            log_done = "morning batch complete" in _log_txt
        elif job == "intraday":
            log_done = "intraday batch complete" in _log_txt
        elif job == "evening":
            log_done = "evening batch complete" in _log_txt
        elif job == "fundamentals":
            log_done = "fundamentals phase done" in _log_txt
        elif job == "research":
            log_done = "research phase done" in _log_txt
        elif job == "news":
            log_done = "news phase done" in _log_txt
        elif job == "validation":
            log_done = "validation complete." in _log_txt
        elif job == "weekly":
            log_done = "weekly analysis complete" in _log_txt
        else:
            log_done = "pipeline complete" in _log_txt or "batch complete" in _log_txt
    if pid_done or db_done or log_done:
        st.session_state.active_pid = None  # keep active_log for display

job_is_alive = bool(st.session_state.active_pid)

# ── Sidebar — Run Controls ────────────────────────────────────────────────────

with st.sidebar:
    st.markdown(
        '<p style="font-size:0.65rem;font-weight:700;text-transform:uppercase;'
        'letter-spacing:0.1em;color:#4B5563;margin:0 0 14px">Run Controls</p>',
        unsafe_allow_html=True,
    )

    def _stop_job() -> None:
        try:
            os.kill(st.session_state.active_pid, signal.SIGTERM)
        except Exception:
            pass
        st.session_state.active_pid = None

    _JOB_DEFS = [
        ("--batch morning",      "light_mode",  "Morning",           "morning",    "Event detection + scheduled/event APEX predictions"),
        ("--batch intraday",     "bolt",        "Intraday",          "intraday",   "Event check + News/Research/Fundamentals/APEX for trending tickers (runs Morning first if it hasn't completed today)"),
        ("--batch evening",      "nights_stay", "Evening",           "evening",    "Validate matured predictions + recompute metrics + calibration outcome-fill + portfolio snapshot (runs Morning first if it hasn't completed today)"),
        ("--daily --force-all",  "play_arrow",  "Full Daily",        "daily",      "News + Research + Fundamentals + APEX (all tickers)"),
        ("--weekly-analysis",    "science",     "Weekly Analysis",   "weekly",     "LLM pattern analysis of wrong predictions (Layer 3 post-mortem)"),
    ]

    for flag, icon_name, run_label, job_key, help_text in _JOB_DEFS:
        is_this_running  = job_is_alive and st.session_state.active_job == job_key
        is_other_running = job_is_alive and not is_this_running

        if is_this_running:
            if st.button(
                f"Stop {run_label.split()[-1]}",
                icon=material("stop"),
                use_container_width=True,
                key=f"sb_btn_{job_key}",
                help="Click to stop this job",
            ):
                _stop_job()
                st.rerun()
        else:
            if st.button(
                run_label,
                icon=material(icon_name),
                use_container_width=True,
                key=f"sb_btn_{job_key}",
                disabled=is_other_running,
                help=help_text,
            ):
                pid, log_path, run_id = _start_job(flag)
                st.session_state.active_pid    = pid
                st.session_state.active_log    = str(log_path)
                st.session_state.active_job    = job_key
                st.session_state.active_run_id = run_id
                st.rerun()

    st.divider()

    if st.button("Refresh", icon=material("refresh"), use_container_width=True, key="sb_refresh"):
        st.rerun()

    if job_is_alive:
        st.markdown(
            f'<div style="margin-top:10px;background:rgba(37,99,235,0.15);'
            f'border:1px solid rgba(37,99,235,0.3);border-radius:8px;padding:10px 12px">'
            f'<div style="font-size:0.75rem;font-weight:600;color:#93C5FD">'
            f'{icon_html("autorenew", 13, color="#93C5FD")} Running</div>'
            f'<div style="font-size:0.7rem;color:#6B7280;margin-top:3px">'
            f'PID {st.session_state.active_pid}</div></div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<p style="font-size:0.7rem;color:#4B5563;margin-top:8px;line-height:1.5">'
            f'Running job turns into <strong style="color:#9CA3AF">{icon_html("stop", 11)} Stop</strong>. '
            'Other buttons disable while a job is active.</p>',
            unsafe_allow_html=True,
        )


# ── Page header ───────────────────────────────────────────────────────────────

page_header(
    "Pipeline Schedule & Logs",
    subtitle="Monitor daily runs, trigger manual jobs, and stream live output",
    icon="calendar_month",
)

# ── Schedule Overview ─────────────────────────────────────────────────────────

_SCHEDULE_DEF = [
    ("morning",    "light_mode",  "Morning",         "Mon–Fri  06:30 CST",                       "News + Research + Fundamentals + APEX predictions"),
    ("intraday",   "bolt",        "Intraday",        "Mon–Fri  11:00 / 13:00 / 15:00 CST",       "Event check + full pipeline (News/Research/Fundamentals/APEX 5d) for trending tickers · auto-runs Morning first if needed"),
    ("evening",    "nights_stay", "Evening",         "Mon–Fri  17:30 CST",                        "Validate matured predictions + recompute metrics + calibration outcome-fill + portfolio snapshot · auto-runs Morning first if needed"),
    ("daily",      "play_arrow",  "Full Daily",      "Manual trigger only",                        "Force-all: all phases for all tickers"),
    ("weekly",     "science",     "Weekly Analysis", "Manual trigger only",                  "LLM post-mortem analysis of wrong predictions (Layer 3)"),
]

# Last run per job_type
_last_per_job: dict = {}
try:
    from portfolio_agent.tools.db import db_conn as _dbc2
    with _dbc2(_DB) as _c2:
        _jrows = _c2.execute("""
            SELECT p.job_type, p.started_at, p.finished_at, p.status
            FROM pipeline_runs p
            JOIN (SELECT job_type, MAX(started_at) mx FROM pipeline_runs GROUP BY job_type) m
              ON p.job_type=m.job_type AND p.started_at=m.mx
        """).fetchall()
        for _r in _jrows:
            _last_per_job[_r[0]] = dict(_r)
except Exception:
    pass

# Find the single most-recently-run job_type across all batches
_latest_jt = None
_latest_ts  = ""
for _jt2, _lr2 in _last_per_job.items():
    _ts2 = _lr2.get("started_at", "") or ""
    if _ts2 > _latest_ts:
        _latest_ts = _ts2
        _latest_jt = _jt2

def _sched_card(job_type, icon_name, label, timing, desc):
    lr        = _last_per_job.get(job_type)
    is_latest = (job_type == _latest_jt) and not job_is_alive

    if lr:
        _ts  = _to_local(lr.get("started_at", ""))[:16]
        _st  = lr.get("status", "")
        _lf  = lr.get("log_file", "")
        _resolved = _db_status_label(_st, _lf, job_type)
        completed = _resolved == "Completed"
        errored   = _st == "error"
        _col = SUCCESS if completed else (DANGER if errored else WARNING)
        last = (
            f'<div style="font-size:0.72rem;font-weight:700;color:{_col}">'
            f'{status_dot_html(_col, 7)} {_ts}</div>'
        )
    else:
        completed = errored = False
        last = '<div style="font-size:0.72rem;color:#9CA3AF">never run</div>'

    # Card highlight based on latest-run status
    if is_latest and completed:
        bg, border, title_col = "#F0FDF4", "#86EFAC", "#065F46"
        badge = '<span style="font-size:0.65rem;font-weight:700;background:#BBF7D0;color:#065F46;padding:2px 7px;border-radius:4px;margin-left:6px">LATEST</span>'
    elif is_latest and errored:
        bg, border, title_col = "#FEF2F2", "#FECACA", "#991B1B"
        badge = '<span style="font-size:0.65rem;font-weight:700;background:#FECACA;color:#991B1B;padding:2px 7px;border-radius:4px;margin-left:6px">FAILED</span>'
    elif is_latest:
        bg, border, title_col = "#FFFBEB", "#FDE68A", "#92400E"
        badge = '<span style="font-size:0.65rem;font-weight:700;background:#FDE68A;color:#92400E;padding:2px 7px;border-radius:4px;margin-left:6px">LATEST</span>'
    else:
        bg, border, title_col = "#fff", "#E5E7EB", "#111827"
        badge = ""

    return (
        f'<div style="background:{bg};border:2px solid {border};border-radius:12px;'
        f'padding:14px 16px;flex:1;min-width:0">'
        f'<div style="font-size:1.1rem;margin-bottom:4px">{icon_html(icon_name, 20, color=title_col)}</div>'
        f'<div style="display:flex;align-items:center;margin-bottom:2px">'
        f'<span style="font-size:0.88rem;font-weight:800;color:{title_col}">{label}</span>'
        f'{badge}</div>'
        f'<div style="font-size:0.7rem;font-weight:600;color:#6B7280;margin-bottom:8px">'
        f'{icon_html("schedule", 12, color="#6B7280")} {timing}</div>'
        f'<div style="font-size:0.75rem;color:#374151;line-height:1.4;margin-bottom:10px">{desc}</div>'
        f'<div style="border-top:1px solid {border};padding-top:8px">'
        f'<div style="font-size:0.63rem;font-weight:700;text-transform:uppercase;'
        f'letter-spacing:0.08em;color:#9CA3AF;margin-bottom:3px">Last Run</div>'
        f'{last}</div>'
        f'</div>'
    )

_cards_html = '<div style="display:flex;gap:12px;margin-bottom:20px;flex-wrap:wrap">'
for _jt, _ic, _name, _ti, _de in _SCHEDULE_DEF:
    _cards_html += _sched_card(_jt, _ic, _name, _ti, _de)
_cards_html += '</div>'

st.markdown(_cards_html, unsafe_allow_html=True)

# ── Running banner (only shown when a job is actively running) ────────────────

if job_is_alive:
    job_label = {
        "daily":      "Full Daily",
        "morning":    "Morning Batch",
        "intraday":   "Intraday Batch",
        "evening":    "Evening Batch",
        "validation": "Validation",
        "weekly":     "Weekly Analysis",
    }.get(st.session_state.active_job or "", "Pipeline")
    st.markdown(
        f'<div style="background:#DBEAFE;border:1px solid #93C5FD;border-radius:10px;'
        f'padding:14px 20px;display:flex;align-items:center;gap:12px;margin-bottom:16px">'
        f'{icon_html("autorenew", 22, color="#1E40AF")}'
        f'<div><strong style="color:#1E40AF">{job_label} pipeline is running</strong>'
        f'<p style="margin:2px 0 0;font-size:0.85rem;color:#3B82F6">'
        f'PID {st.session_state.active_pid} · Log: '
        f'{Path(st.session_state.active_log).name}</p>'
        f'</div></div>',
        unsafe_allow_html=True,
    )

# ── Live log viewer ───────────────────────────────────────────────────────────

active_log_path = Path(st.session_state.active_log) if st.session_state.active_log else None

if active_log_path and active_log_path.exists():
    log_text = _read_log_tail(active_log_path)
    log_lines = log_text.count("\n") + 1 if log_text else 0

    if job_is_alive:
        _tile_1 = section_tile(f"{icon_html('sensors', 16)} Live Output", badge_text=f"{_phase_badge(log_text)}  ·  {log_lines} lines", badge_color="#3B82F6", expanded=True, key="schedule_1")
        if _tile_1:
            with _tile_1:
                st.caption(f"Auto-refreshing every 3 s  ·  {active_log_path.name}")
    else:
        _fin_job = st.session_state.active_job or "daily"
        _lt_lower = log_text.lower()
        _DONE_PHRASES = {
            "daily":    ["daily pipeline complete", "morning batch complete", "finished"],
            "morning":  ["morning batch complete", "finished"],
            "intraday": ["intraday batch complete", "finished"],
            "evening":  ["evening batch complete", "finished"],
        }
        _fin_phrases = _DONE_PHRASES.get(_fin_job, [f"{_fin_job} phase done", "finished"])
        _finished = any(p in _lt_lower for p in _fin_phrases)
        _fin_badge = (
            f"{icon_html('check_circle', 13, color=SUCCESS)} Finished"
            if _finished else
            f"{status_dot_html(PRIMARY)} Ended"
        )
        _tile_2 = section_tile(f"{icon_html('list_alt', 16)} Last Run Output", badge_text=f"{_fin_badge}  ·  {log_lines} lines", expanded=False, key="schedule_2")
        if _tile_2:
            with _tile_2:
                st.caption(active_log_path.name)
    # ── Progress tracker — prefer DB (real-time) over log parsing ────────────
    _active_run_id = st.session_state.get("active_run_id")
    _db_run = _load_run_from_db(_active_run_id) if _active_run_id else None
    if _db_run and _db_run["phases"]:
        _render_progress_parsed(_db_to_parsed(_db_run), st.session_state.active_job or "daily")
    else:
        _render_progress(log_text, st.session_state.active_job or "daily")

    # ── Scrollable terminal block ─────────────────────────────────────────────
    with st.expander("Raw log output", icon=material("list_alt"), expanded=job_is_alive):
        st.markdown(
            f'<div style="background:#0F172A;border:1px solid #334155;border-radius:10px;'
            f'padding:16px;font-family:monospace;font-size:0.78rem;line-height:1.5;'
            f'max-height:520px;overflow-y:auto;white-space:pre-wrap;color:#E2E8F0">'
            + log_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
            + '</div>',
            unsafe_allow_html=True,
        )

    st.divider()

# ── Checkpoint status ─────────────────────────────────────────────────────────

if _CP.exists():
    try:
        cp = json.loads(_CP.read_text())
        _cp_phases = [
            (phase, cp.get(phase, {}).get("pending", []))
            for phase in ["fundamentals", "news", "research"]
        ]
        _cp_any = any(pending for _, pending in _cp_phases)
        if _cp_any:
            _tile_3 = section_tile(f"{icon_html('bookmark', 16)} Saved Checkpoint", badge_text="Pending tickers", badge_color=WARNING, expanded=False, key="schedule_3")
            if _tile_3:
                with _tile_3:
                    st.caption(f"Saved at: {cp.get('saved_at','')} · Run date: {cp.get('run_date','')}")
                    for phase, pending in _cp_phases:
                        if pending:
                            pd_data = cp.get(phase, {})
                            st.markdown(
                                f'<div style="background:#FEF3C7;border:1px solid #FDE68A;border-radius:8px;'
                                f'padding:10px 14px;margin-bottom:8px">'
                                f'<strong style="color:#92400E">{phase.title()}</strong>: '
                                f'{len(pending)} tickers pending — '
                                f'{", ".join(pending[:10])}{"…" if len(pending) > 10 else ""}'
                                f'<br><span style="font-size:0.8rem;color:#B45309">'
                                f'Error: {pd_data.get("error","")[:120]}</span></div>',
                                unsafe_allow_html=True,
                            )
    except Exception:
        pass

# ── Historical runs ───────────────────────────────────────────────────────────

_BATCH_LABEL = {
    "morning":      "Morning",
    "intraday":     "Intraday",
    "evening":      "Evening",
    "validation":   "Validation",
    "daily":        "Full Daily",
    "news":         "News",
    "research":     "Research",
    "fundamentals": "Fundamentals",
    "weekly":       "Weekly Analysis",
}

# Pre-fetch metrics_rolling: metric_date → {horizon_days: num_predictions}
_metrics_by_date: dict = {}
try:
    with _dbc2(_DB) as _mc:
        _mrows = _mc.execute("""
            SELECT metric_date, horizon_days, num_predictions
            FROM metrics_rolling WHERE lookback_days=90
        """).fetchall()
        for _mr in _mrows:
            _d = _mr[0]
            _metrics_by_date.setdefault(_d, {})[_mr[1]] = _mr[2]
except Exception:
    pass

def _run_summary(phases: dict, job_type: str,
                 log_phase_counts: dict | None = None,
                 finished_at: str = "") -> str:
    """Build a single summary string for a run based on job type and phase data."""
    jt = job_type.lower()

    def _p(key):
        p = phases.get(key) or {}
        done = p.get("completed", 0)
        tot  = p.get("total", 0)
        fail = p.get("failed", 0)
        if done == 0 and tot == 0:
            return None
        s = f"{done}/{tot}" if tot else str(done)
        return s + (f" ({fail} failed)" if fail else "")

    def _lp(key):
        if not log_phase_counts:
            return None
        v = log_phase_counts.get(key, "—")
        return None if v == "—" else v

    if jt in ("morning", "daily"):
        parts = []
        for key, label in [("news","News"), ("research","Res"),
                            ("fundamentals","Fund"), ("apex","APEX")]:
            v = _p(key) or _lp(key)
            if v:
                parts.append(f"{label} {v}")
        return "  ·  ".join(parts) if parts else "Phases recorded"

    if jt == "intraday":
        apex = _p("apex") or _lp("Predictions")
        return f"APEX triggered — {apex} predictions" if apex else "Event check — no triggers"

    if jt in ("evening", "validation"):
        # Use the run's finished_at date to look up per-horizon scored counts
        run_date = (finished_at or "")[:10]
        horizon_data = _metrics_by_date.get(run_date, {})
        if horizon_data:
            _HZ = {5: "5d", 21: "21d", 63: "63d", 250: "1y"}
            parts = [
                f"{_HZ.get(h, f'{h}d')}: {n}"
                for h, n in sorted(horizon_data.items())
                if n > 0
            ]
            return "Scored — " + "  ·  ".join(parts) if parts else "Scored matured predictions"
        return "Scored matured predictions"

    if jt == "news":
        v = _p("news") or _lp("News")
        return f"News scored — {v} tickers" if v else "News phase"

    if jt == "research":
        v = _p("research") or _lp("Research")
        return f"Research updated — {v} tickers" if v else "Research phase"

    if jt == "fundamentals":
        v = _p("fundamentals") or _lp("Fundamentals")
        return f"Fundamentals updated — {v} tickers" if v else "Fundamentals phase"

    if jt == "weekly":
        return "LLM post-mortem on wrong predictions"

    return "—"


def _run_duration(started: str, finished: str) -> str:
    if not started or not finished:
        return "—"
    try:
        from datetime import datetime as _dt
        s = _dt.fromisoformat(started[:19])
        f = _dt.fromisoformat(finished[:19])
        secs = int((f - s).total_seconds())
        if secs < 0:
            return "—"
        if secs < 60:
            return f"{secs}s"
        return f"{secs // 60}m {secs % 60:02d}s"
    except Exception:
        return "—"


# DB runs — primary source
_db_all = _load_db_runs(limit=40)
_db_run_ids = {r["run_id"] for r in _db_all}

# Legacy log runs — only include those NOT already in DB
_log_runs = _scan_logs()
_legacy   = [r for r in _log_runs if r["log_path"].stem not in _db_run_ids]

# Build unified display list
_active_run_id_for_log = st.session_state.get("active_run_id", "")
_all_display: list[dict] = []

for r in _db_all:
    ts_raw = _to_local(r.get("started_at", ""))
    log_f  = r.get("log_file", "")
    if not log_f and r["run_id"] == _active_run_id_for_log and st.session_state.get("active_log"):
        log_f = st.session_state.active_log
    jt  = r.get("job_type", "daily")
    ph  = r.get("phases", {})
    _all_display.append({
        "_source":   "db",
        "_run_id":   r["run_id"],
        "_log_file": log_f,
        "_db_run":   {"run": r, "phases": ph},
        "_job_type": jt,
        "Batch":     _BATCH_LABEL.get(jt, jt.title()),
        "Started":   ts_raw,
        "Duration":  _run_duration(r.get("started_at",""), r.get("finished_at","")),
        "Summary":   _run_summary(ph, jt, finished_at=r.get("finished_at","")),
        "Status":    _db_status_label(r.get("status",""), log_f, jt),
    })

for r in _legacy:
    jt = r["job_type"].lower()
    _all_display.append({
        "_source":   "log",
        "_run_id":   r["log_path"].stem,
        "_log_file": str(r["log_path"]),
        "_db_run":   None,
        "_job_type": jt,
        "Batch":     _BATCH_LABEL.get(jt, r["job_type"].title()),
        "Started":   r["date_label"],
        "Duration":  "—",
        "Summary":   _run_summary({}, jt, r.get("phase_counts")),
        "Status":    r["status"],
    })

total_runs = len(_all_display)
_tile_4 = section_tile(f"{icon_html('list_alt', 16)} Historical Runs", badge_text=f"{total_runs} runs", expanded=False, key="schedule_4")
if _tile_4:
    with _tile_4:

        if _all_display:
            import pandas as pd
            _table_cols = ["Batch", "Started", "Duration", "Summary", "Status"]
            _df = pd.DataFrame([{c: row[c] for c in _table_cols} for row in _all_display])

            def _style_runs(row):
                if row.name == 0:
                    return ["background-color:#D1FAE5;color:#065F46;font-weight:700"] * len(row)
                return [""] * len(row)

            st.dataframe(
                _df.style.apply(_style_runs, axis=1),
                hide_index=True,
                use_container_width=True,
                column_config={
                    "Batch":    st.column_config.TextColumn("Batch",    width="small"),
                    "Started":  st.column_config.TextColumn("Started",  width="medium"),
                    "Duration": st.column_config.TextColumn("Duration", width="small"),
                    "Summary":  st.column_config.TextColumn("Summary",  width="large"),
                    "Status":   st.column_config.TextColumn("Status",   width="small"),
                },
            )

            st.markdown("**View a past run:**")
            sel = st.selectbox(
                "Select run",
                options=range(len(_all_display)),
                format_func=lambda i: f"{_all_display[i]['Started']}  [{_all_display[i]['Batch']}]  {_all_display[i]['Status']}",
                key="hist_sel",
                label_visibility="collapsed",
            )
            if sel is not None:
                _sel = _all_display[sel]
                _db_r = _sel["_db_run"]
                if _db_r and _db_r.get("phases"):
                    _render_progress_parsed(_db_to_parsed(_db_r), _sel["_job_type"])
                log_f = _sel["_log_file"]
                if log_f and Path(log_f).exists():
                    hist_text = _read_log_tail(Path(log_f))
                    with st.expander("Raw log output", icon=material("list_alt"), expanded=False):
                        st.markdown(
                            f'<div style="background:#0F172A;border:1px solid #334155;border-radius:10px;'
                            f'padding:16px;font-family:monospace;font-size:0.78rem;line-height:1.5;'
                            f'max-height:520px;overflow-y:auto;white-space:pre-wrap;color:#E2E8F0">'
                            + hist_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
                            + '</div>',
                            unsafe_allow_html=True,
                        )
                elif not log_f or not Path(log_f).exists():
                    st.caption("Log file not available for this run.")
        else:
            st.info("No pipeline runs found yet. Run a job to get started.", icon=material("description"))

        # ── Auto-refresh while job is alive ──────────────────────────────────────────
        # Sleep then rerun — keeps the live log updated without any extra component.

        if job_is_alive:
            time.sleep(3)
            st.rerun()