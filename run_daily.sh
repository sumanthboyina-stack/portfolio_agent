#!/bin/bash
# Daily job wrapper — routes output to YYYYMMDD_HHMMSS_daily.log (same format as web UI)
TS=$(date +%Y%m%d_%H%M%S)
LOG_DIR="/Users/sumanthboyina/portfolio-agent/logs"
mkdir -p "$LOG_DIR"

exec >> "${LOG_DIR}/${TS}_daily.log" 2>&1   # merge stderr into stdout

echo "========================================"
echo "  Daily job started : $(date)"
echo "  Phase 1 — Fundamentals (EDGAR check)"
echo "  Phase 2 — Research"
echo "  Phase 3 — News (triage + agent)"
echo "========================================"

cd /Users/sumanthboyina/portfolio-agent
/Users/sumanthboyina/portfolio-agent/.venv/bin/python -u main.py --daily

echo "  Finished : $(date)"
