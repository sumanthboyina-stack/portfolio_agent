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
    inject_global_css, top_nav, page_header, section_title,
    badge_html, SUCCESS, WARNING, DANGER, PRIMARY, NEUTRAL,
)

_LOGS = _ROOT / "logs"
_MAIN = _ROOT / "main.py"
_CP   = _ROOT / "data" / "batch_checkpoint.json"
_DB   = _ROOT / "data" / "portfolio.db"
_LOGS.mkdir(parents=True, exist_ok=True)

st.set_page_config(
    page_title="Schedule — Portfolio Intelligence",
    page_icon="🗓️",
    layout="wide",
    initial_sidebar_state="expanded",
)
inject_global_css()
top_nav("schedule")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _to_local(utc_str: str) -> str:
    """Convert a UTC ISO string from the DB to a local-time display string."""
    try:
        return datetime.fromisoformat(utc_str).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return (utc_str or "")[:19].replace("T", " ")


def _is_pid_alive(pid: int) -> bool:
    try:
        # waitpid with WNOHANG reaps zombie processes; returns (0,0) if still running
        done, _ = os.waitpid(pid, os.WNOHANG)
        return done == 0
    except ChildProcessError:
        # Not a direct child — fall back to kill -0
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    except OSError:
        return False


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
    ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
    job_label = {
        "--daily":              "daily",
        "--daily-fundamentals": "fundamentals",
        "--daily-research":     "research",
        "--daily-news":         "news",
    }.get(flag, "daily")
    run_id    = f"{ts}_{job_label}"
    log_path  = _LOGS / f"{run_id}.log"
    log_file  = open(log_path, "w", buffering=1)   # line-buffered
    env       = {**os.environ, "PIPELINE_RUN_ID": run_id}
    proc = subprocess.Popen(
        ["python", "-u", str(_MAIN), flag],         # -u = unbuffered stdout
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
        # New format:  YYYYMMDD_HHMMSS_daily / _news / _fundamentals / _research
        m = re.match(r"^(\d{8})_(\d{6})_(daily|news|fundamentals|research)$", name)
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
        else:
            has_fin = "finished :" in lower
        # has_err only flags pipeline-stopping failures — transient provider
        # errors (Groq TPD, rate limits) that triggered failover are NOT errors
        # because the pipeline kept running and may have completed successfully.
        has_err  = any(k in lower for k in (
            "[error]", "traceback (most recent", "exception:",
            "[all models exhausted]",   # entire failover chain failed — pipeline stopped
        ))
        # has_fin takes priority: if the pipeline completed, show ✅ even if
        # transient errors occurred along the way (fallbacks handled them).
        status   = "✅ Completed" if has_fin \
                   else ("❌ Errored"  if has_err else "🔵 Incomplete")
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
        return "🤖 Phase 4 – APEX"
    if "phase 3" in lower or "fundamentals phase" in lower:
        return "📄 Phase 3 – Fundamentals"
    if "phase 2" in lower or "research phase" in lower:
        return "🔬 Phase 2 – Research"
    if "phase 1" in lower or "news phase" in lower:
        return "📰 Phase 1 – News"
    return "🔄 Starting…"


def _parse_progress(log_text: str) -> dict:
    """
    Parse log output into structured phase/ticker progress.
    Returns a dict with total_tickers and per-phase state.
    """
    phases: dict[str, dict] = {
        "fundamentals": dict(status="pending", total=0, completed=0, failed=0, ticker="", note=""),
        "research":     dict(status="pending", total=0, completed=0, failed=0, ticker="", note="", track_a=0),
        "news":         dict(status="pending", total=0, completed=0, failed=0, ticker="", note="", flagged=0,
                            filter_analyzed=0, filter_no_art=0, filter_low_mat=0, filter_no_new=0),
        "apex":         dict(status="pending", total=0, completed=0, failed=0, ticker="", note=""),
    }
    total_tickers = 0
    current = None

    for line in log_text.splitlines():
        # ── total tickers ────────────────────────────────────────────────────
        m = re.search(r"Daily Job — (\d+) tickers total", line)
        if m:
            total_tickers = int(m.group(1))

        # ── phase markers (order: News → Research → Fundamentals → APEX) ───────
        if re.search(r"Phase 1.{0,20}News|── Phase 1", line):
            current = "news"
            phases["news"]["status"] = "running"
        elif re.search(r"Phase 2.{0,20}Research|── Phase 2", line):
            current = "research"
            phases["research"]["status"] = "running"
            if phases["news"]["status"] == "running":
                phases["news"]["status"] = "done"
        elif re.search(r"Phase 3.{0,20}Fundamentals|── Phase 3", line):
            current = "fundamentals"
            phases["fundamentals"]["status"] = "running"
            if phases["research"]["status"] == "running":
                phases["research"]["status"] = "done"
        elif re.search(r"Phase 4.{0,20}APEX|── Phase 4", line):
            current = "apex"
            phases["apex"]["status"] = "running"
            if phases["fundamentals"]["status"] == "running":
                phases["fundamentals"]["status"] = "done"

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

    phases: dict[str, dict] = {
        "fundamentals": dict(status="pending", total=0, completed=0, failed=0, ticker="", note=""),
        "research":     dict(status="pending", total=0, completed=0, failed=0, ticker="", note="", track_a=0),
        "news":         dict(status="pending", total=0, completed=0, failed=0, ticker="", note="", flagged=0,
                            filter_analyzed=0, filter_no_art=0, filter_low_mat=0, filter_no_new=0),
        "apex":         dict(status="pending", total=0, completed=0, failed=0, ticker="", note=""),
    }
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
    return {"total_tickers": total, "phases": phases}


def _render_progress(log_text: str, job_type: str) -> None:
    """Render milestone timeline + per-phase progress bars above the log."""
    prog = _parse_progress(log_text)
    _render_progress_parsed(prog, job_type)


def _render_progress_parsed(prog: dict, job_type: str) -> None:
    """Render milestone timeline + per-phase progress bars from pre-parsed dict."""
    total = prog["total_tickers"]
    ph    = prog["phases"]

    # Determine which phases are relevant for this job type
    phase_keys = {
        "daily":        ["news", "research", "fundamentals", "apex"],
        "fundamentals": ["fundamentals"],
        "research":     ["research"],
        "news":         ["news"],
    }.get(job_type, ["news", "research", "fundamentals", "apex"])

    # Nothing to show yet
    if total == 0 and all(ph[k]["status"] == "pending" for k in phase_keys):
        st.caption("⏳ Job starting up…")
        return

    PHASE_META = {
        "news":         ("📰", "News",              "Phase 1"),
        "research":     ("🔬", "Research",          "Phase 2"),
        "fundamentals": ("📄", "Fundamentals",      "Phase 3"),
        "apex":         ("🤖", "APEX Predictions",  "Phase 4"),
    }
    STATUS_COLOR = {
        "pending":   "#475569",
        "running":   "#3B82F6",
        "done":      "#10B981",
        "exhausted": "#F59E0B",
    }
    STATUS_DOT = {
        "pending":   "#475569",
        "running":   "#3B82F6",
        "done":      "#10B981",
        "exhausted": "#F59E0B",
    }

    # ── milestone timeline ─────────────────────────────────────────────────────
    dot_html = ""
    for i, k in enumerate(phase_keys):
        status = ph[k]["status"]
        dot_color = STATUS_DOT[status]
        label_icon = PHASE_META[k][0]

        # connector line before the dot (skip for first)
        if i > 0:
            prev_done = ph[phase_keys[i - 1]]["status"] in ("done", "exhausted")
            line_color = "#10B981" if prev_done else "#334155"
            dot_html += f'<div style="flex:1;height:3px;background:{line_color};margin:0 2px"></div>'

        dot_html += (
            f'<div style="display:flex;flex-direction:column;align-items:center;gap:4px">'
            f'<div style="width:18px;height:18px;border-radius:50%;background:{dot_color};'
            f'box-shadow:0 0 0 3px {dot_color}33;flex-shrink:0"></div>'
            f'<span style="font-size:0.75rem;color:{dot_color};white-space:nowrap">'
            f'{label_icon} {PHASE_META[k][1]}</span>'
            f'</div>'
        )

    total_label = f"— {total} tickers" if total else ""
    st.markdown(
        f'<div style="background:#1E293B;border:1px solid #334155;border-radius:10px;'
        f'padding:16px 20px;margin-bottom:12px">'
        f'<div style="font-size:0.85rem;font-weight:600;color:#94A3B8;margin-bottom:14px">'
        f'PIPELINE PROGRESS {total_label}</div>'
        f'<div style="display:flex;align-items:center;gap:0;padding:0 8px">{dot_html}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # ── per-phase progress bars ────────────────────────────────────────────────
    for k in phase_keys:
        p      = ph[k]
        icon, label, phase_num = PHASE_META[k]
        status = p["status"]
        color  = STATUS_COLOR[status]

        if status == "pending":
            bar_val  = 0.0
            bar_text = "Waiting…"
        elif status == "running":
            bar_val  = p["completed"] / max(p["total"], 1)
            ticker_str = f" · {p['ticker']} in progress" if p["ticker"] else ""
            batch_str  = f" · {p['note']}" if p.get("note") and k == "news" else ""
            bar_text = f"{p['completed']} / {p['total']} complete{ticker_str}{batch_str}"
        elif status == "done":
            bar_val  = 1.0
            fail_str = f", {p['failed']} failed" if p.get("failed") else ""
            bar_text = f"{p['completed']} saved{fail_str}"
        else:  # exhausted
            bar_val  = p["completed"] / max(p["total"], 1)
            bar_text = f"{p['completed']} / {p['total']} · ⚠️ {p.get('note', 'models exhausted')}"

        # Research: show track A info too
        extra = ""
        if k == "research" and p.get("track_a"):
            extra = f"  ·  {p['track_a']} raw broker records fetched (no LLM)"
        if k == "news":
            if p.get("filter_analyzed") or p.get("filter_low_mat") or p.get("filter_no_new") or p.get("filter_no_art"):
                parts = [f"{p['filter_analyzed']} analyzed"]
                if p.get("filter_low_mat"):  parts.append(f"{p['filter_low_mat']} low-materiality")
                if p.get("filter_no_new"):   parts.append(f"{p['filter_no_new']} no-new-articles")
                if p.get("filter_no_art"):   parts.append(f"{p['filter_no_art']} no-articles")
                extra = "  ·  " + " | ".join(parts)
            elif p.get("flagged"):
                extra = f"  ·  {p['flagged']} tickers flagged for analysis"

        col_label, col_bar = st.columns([2, 7])
        with col_label:
            st.markdown(
                f'<div style="padding:6px 0;color:{color};font-weight:600;font-size:0.88rem">'
                f'{phase_num} {icon} {label}</div>',
                unsafe_allow_html=True,
            )
        with col_bar:
            st.progress(bar_val, text=bar_text + extra)


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
    # Log-content fallback for runs without DB tracking
    log_done = False
    if not db_done and st.session_state.active_log and Path(st.session_state.active_log).exists():
        _log_txt = Path(st.session_state.active_log).read_text(errors="replace").lower()
        job = st.session_state.active_job or "daily"
        if job == "daily":
            log_done = any(k in _log_txt for k in ("daily pipeline complete", "finished"))
        elif job == "fundamentals":
            log_done = "fundamentals phase done" in _log_txt
        elif job == "research":
            log_done = "research phase done" in _log_txt
        elif job == "news":
            log_done = "news phase done" in _log_txt
        else:
            log_done = any(k in _log_txt for k in ("pipeline complete", "finished"))
    if pid_done or db_done or log_done:
        st.session_state.active_pid = None  # keep active_log for display

job_is_alive = bool(st.session_state.active_pid)

# ── Page header ───────────────────────────────────────────────────────────────

page_header(
    "Pipeline Schedule & Logs",
    subtitle="Monitor daily runs, trigger manual jobs, and stream live output",
    icon="🗓️",
)

# ── Status banner ─────────────────────────────────────────────────────────────

if job_is_alive:
    job_label = {
        "daily":        "Full Daily",
        "fundamentals": "Fundamentals-Only",
        "research":     "Research-Only",
        "news":         "News-Only",
    }.get(st.session_state.active_job or "", "Pipeline")
    st.markdown(
        f'<div style="background:#DBEAFE;border:1px solid #93C5FD;border-radius:10px;'
        f'padding:14px 20px;display:flex;align-items:center;gap:12px;margin-bottom:16px">'
        f'<span style="font-size:1.4rem">🔄</span>'
        f'<div>'
        f'<strong style="color:#1E40AF">{job_label} pipeline is running</strong>'
        f'<p style="margin:2px 0 0;font-size:0.85rem;color:#3B82F6">'
        f'PID {st.session_state.active_pid} · Log: '
        f'{Path(st.session_state.active_log).name}</p>'
        f'</div></div>',
        unsafe_allow_html=True,
    )
else:
    _db_latest = (_load_db_runs(limit=1) or [None])[0]
    if _db_latest:
        _s = _db_latest.get("status", "")
        _ts = _to_local(_db_latest.get("started_at", ""))
        _jt = _db_latest.get("job_type", "").upper()
        _fin = _db_latest.get("finished_at", "")
        if _s == "completed":
            bg, border, fg, icon = "#D1FAE5", "#6EE7B7", "#065F46", "✅"
            _status_text = "Completed"
        elif _s == "error":
            bg, border, fg, icon = "#FEE2E2", "#FCA5A5", "#991B1B", "❌"
            _status_text = "Errored"
        else:
            bg, border, fg, icon = "#FEF3C7", "#FDE68A", "#92400E", "🔵"
            _status_text = "Incomplete"
        _detail = f"Finished {_to_local(_fin)}" if _fin else f"Started {_ts}"
        st.markdown(
            f'<div style="background:{bg};border:1px solid {border};border-radius:10px;'
            f'padding:14px 20px;display:flex;align-items:center;gap:12px;margin-bottom:16px">'
            f'<span style="font-size:1.2rem">{icon}</span>'
            f'<div><strong style="color:{fg}">Last run: {_ts} ({_jt})</strong>'
            f'<p style="margin:2px 0 0;font-size:0.85rem;color:{fg}">'
            f'{_status_text} · {_detail}</p></div></div>',
            unsafe_allow_html=True,
        )
    else:
        _log_runs = _scan_logs()
        if _log_runs:
            latest = _log_runs[0]
            bg, border, fg, icon = (
                ("#D1FAE5", "#6EE7B7", "#065F46", "✅") if "Completed" in latest["status"] and not latest["has_error"]
                else (("#FEE2E2", "#FCA5A5", "#991B1B", "❌") if latest["has_error"]
                      else ("#FEF3C7", "#FDE68A", "#92400E", "🔵"))
            )
            st.markdown(
                f'<div style="background:{bg};border:1px solid {border};border-radius:10px;'
                f'padding:14px 20px;display:flex;align-items:center;gap:12px;margin-bottom:16px">'
                f'<span style="font-size:1.2rem">{icon}</span>'
                f'<div><strong style="color:{fg}">Last run: {latest["date_label"]} ({latest["job_type"]})</strong>'
                f'<p style="margin:2px 0 0;font-size:0.85rem;color:{fg}">'
                f'{latest["status"]} · {latest["lines"]:,} log lines</p></div></div>',
                unsafe_allow_html=True,
            )
        else:
            st.info("No pipeline runs found yet.", icon="ℹ️")

# ── Run controls ──────────────────────────────────────────────────────────────

# Inject red style for the active stop button
st.markdown(
    '<style>'
    'div[data-testid="stButton"] button[kind="primary"].stop-btn {'
    '  background:#DC2626!important;border-color:#DC2626!important;'
    '  color:white!important;}'
    '</style>',
    unsafe_allow_html=True,
)

_JOB_DEFS = [
    ("--daily",              "▶ Full Daily",   "daily",        "News + Research + Fundamentals + APEX"),
    ("--daily-news",         "📰 News",         "news",         "News triage + news agent only"),
    ("--daily-research",     "🔬 Research",     "research",     "Broker data fetch + LLM research summary"),
    ("--daily-fundamentals", "📄 Fundamentals", "fundamentals", "EDGAR check + batch fundamentals LLM"),
]

section_title("⚡ Run Controls")
rc1, rc2, rc3, rc4, rc5 = st.columns(5)

def _stop_job() -> None:
    try:
        os.kill(st.session_state.active_pid, signal.SIGTERM)
    except Exception:
        pass
    st.session_state.active_pid = None

for col, (flag, run_label, job_key, help_text) in zip([rc1, rc2, rc3, rc4], _JOB_DEFS):
    with col:
        is_this_running  = job_is_alive and st.session_state.active_job == job_key
        is_other_running = job_is_alive and not is_this_running

        if is_this_running:
            # Same button position, stop label + red styling hint
            if st.button(
                f"⏹ Stop {run_label.split()[-1]}",
                type="primary",
                use_container_width=True,
                key=f"btn_{job_key}",
                help="Click to stop this job",
            ):
                _stop_job()
                st.rerun()
        else:
            if st.button(
                run_label,
                type="primary" if job_key == "daily" else "secondary",
                use_container_width=True,
                key=f"btn_{job_key}",
                disabled=is_other_running,
                help=help_text,
            ):
                pid, log_path, run_id = _start_job(flag)
                st.session_state.active_pid    = pid
                st.session_state.active_log    = str(log_path)
                st.session_state.active_job    = job_key
                st.session_state.active_run_id = run_id
                st.rerun()

with rc5:
    if st.button("🔄 Refresh", use_container_width=True):
        st.rerun()

st.caption(
    "Running job button turns into **⏹ Stop** — click it to terminate. "
    "Other buttons are disabled while a job is active."
)

st.divider()

# ── Live log viewer ───────────────────────────────────────────────────────────

active_log_path = Path(st.session_state.active_log) if st.session_state.active_log else None

if active_log_path and active_log_path.exists():
    log_text = _read_log_tail(active_log_path)
    log_lines = log_text.count("\n") + 1 if log_text else 0

    if job_is_alive:
        section_title(
            "📡 Live Output",
            badge_text=f"{_phase_badge(log_text)}  ·  {log_lines} lines",
            badge_color="#3B82F6",
        )
        st.caption(f"Auto-refreshing every 3 s  ·  {active_log_path.name}")
    else:
        _fin_job = st.session_state.active_job or "daily"
        _lt_lower = log_text.lower()
        _finished = (
            "daily pipeline complete" in _lt_lower or "finished" in _lt_lower
            if _fin_job == "daily"
            else f"{_fin_job} phase done" in _lt_lower
        )
        section_title(
            "📋 Last Run Output",
            badge_text=f"{'✅ Finished' if _finished else '🔵 Ended'}  ·  {log_lines} lines",
        )
        st.caption(active_log_path.name)

    # ── Progress tracker — prefer DB (real-time) over log parsing ────────────
    _active_run_id = st.session_state.get("active_run_id")
    _db_run = _load_run_from_db(_active_run_id) if _active_run_id else None
    if _db_run and _db_run["phases"]:
        _render_progress_parsed(_db_to_parsed(_db_run), st.session_state.active_job or "daily")
    else:
        _render_progress(log_text, st.session_state.active_job or "daily")

    # ── Scrollable terminal block ─────────────────────────────────────────────
    with st.expander("📋 Raw log output", expanded=job_is_alive):
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
        section_title("🔖 Saved Checkpoint", badge_text="Pending tickers", badge_color=WARNING)
        st.caption(f"Saved at: {cp.get('saved_at','')} · Run date: {cp.get('run_date','')}")
        for phase in ["fundamentals", "news", "research"]:
            pd_data = cp.get(phase, {})
            pending = pd_data.get("pending", [])
            if pending:
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
        st.divider()
    except Exception:
        pass

# ── Historical runs ───────────────────────────────────────────────────────────

def _db_status_label(s: str) -> str:
    return {"completed": "✅ Completed", "error": "❌ Errored", "running": "🔵 Running"}.get(s, f"🔵 {s.title()}")

def _db_phase_counts(db_run: dict, job_type: str) -> dict:
    phases = db_run.get("phases", {})
    def _fmt(key: str) -> str:
        p = phases.get(key)
        if not p:
            return "—"
        done, tot, fail = p.get("completed", 0), p.get("total", 0), p.get("failed", 0)
        if done == 0 and tot == 0:
            return "—"
        base = f"{done}/{tot}" if tot else str(done)
        return base + (f" ({fail}✗)" if fail else "")
    jt = job_type.lower()
    if jt in ("news", "research", "fundamentals"):
        return {
            "News":         _fmt("news")         if jt == "news"         else "—",
            "Research":     _fmt("research")     if jt == "research"     else "—",
            "Fundamentals": _fmt("fundamentals") if jt == "fundamentals" else "—",
            "Predictions":  "—",
        }
    return {"News": _fmt("news"), "Research": _fmt("research"),
            "Fundamentals": _fmt("fundamentals"), "Predictions": _fmt("apex")}

# DB runs — primary source
_db_all = _load_db_runs(limit=40)
_db_run_ids = {r["run_id"] for r in _db_all}

# Legacy log runs — only include those NOT already in DB
_log_runs   = _scan_logs()
_legacy     = [r for r in _log_runs if r["log_path"].stem not in _db_run_ids]

# Build unified display list: DB entries first (sorted newest first), then legacy
_all_display: list[dict] = []
for r in _db_all:
    ts_raw = _to_local(r.get("started_at", ""))
    log_f  = r.get("log_file", "")
    _all_display.append({
        "_source":   "db",
        "_run_id":   r["run_id"],
        "_log_file": log_f,
        "_db_run":   {"run": r, "phases": r.get("phases", {})},
        "_job_type": r.get("job_type", "daily"),
        "Job":       r.get("job_type", "").upper(),
        "Status":    _db_status_label(r.get("status", "")),
        "Started":   ts_raw,
        "Source":    "DB",
        **_db_phase_counts(r, r.get("job_type", "daily")),
    })
for r in _legacy:
    _all_display.append({
        "_source":   "log",
        "_run_id":   r["log_path"].stem,
        "_log_file": str(r["log_path"]),
        "_db_run":   None,
        "_job_type": r["job_type"].lower(),
        "Job":       r["job_type"],
        "Status":    r["status"],
        "Started":   r["date_label"],
        "Source":    "log",
        **r["phase_counts"],
    })

total_runs = len(_all_display)
section_title("📋 Historical Runs", badge_text=f"{total_runs} runs")

if _all_display:
    _table_cols = ["Job", "Status", "Started", "Source", "News", "Research", "Fundamentals", "Predictions"]
    st.dataframe(
        [{c: row[c] for c in _table_cols} for row in _all_display],
        hide_index=True,
        use_container_width=True,
    )

    st.markdown("**View a past run:**")
    sel = st.selectbox(
        "Select run",
        options=range(len(_all_display)),
        format_func=lambda i: f"{_all_display[i]['Started']}  [{_all_display[i]['Job']}]  {_all_display[i]['Status']}",
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
            with st.expander("📋 Raw log output", expanded=False):
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
    st.info("No pipeline runs found yet. Run a job to get started.", icon="📄")

# ── Auto-refresh while job is alive ──────────────────────────────────────────
# Sleep then rerun — keeps the live log updated without any extra component.

if job_is_alive:
    time.sleep(3)
    st.rerun()
