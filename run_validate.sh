#!/bin/bash
# Evening job wrapper — runs L1 outcome evaluation + L2 rolling metrics +
# Score Calibration outcome-fill + portfolio price snapshot + screener outcomes.
# Scheduled: 5:00 PM CST/CDT Mon–Fri  (after markets close + settlement)
TS=$(date +%Y%m%d_%H%M%S)
LOG_DIR="/Users/sumanthboyina/portfolio-agent/logs"
mkdir -p "$LOG_DIR"

RUN_ID="${TS}_evening"
export PIPELINE_RUN_ID="${RUN_ID}"
exec >> "${LOG_DIR}/${RUN_ID}.log" 2>&1   # merge stderr into stdout

echo "========================================"
echo "  Evening job started : $(date)"
echo "  Layer 1   — Outcome assignment (matured predictions)"
echo "  Layer 2   — Rolling metrics recomputation"
echo "  Layer 2.5 — Score Calibration outcome-fill"
echo "  Layer 3   — Portfolio price snapshot"
echo "  Layer 4   — Screener outcome scoring"
echo "========================================"

cd /Users/sumanthboyina/portfolio-agent
/Users/sumanthboyina/portfolio-agent/.venv/bin/python -u main.py --batch evening

echo "  Finished : $(date)"
