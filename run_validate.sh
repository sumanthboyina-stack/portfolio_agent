#!/bin/bash
# Validation job wrapper — runs L1 + L2 outcome evaluation
# Scheduled: 5:00 PM CST/CDT Mon–Fri  (after markets close + settlement)
TS=$(date +%Y%m%d_%H%M%S)
LOG_DIR="/Users/sumanthboyina/portfolio-agent/logs"
mkdir -p "$LOG_DIR"

RUN_ID="${TS}_validation"
export PIPELINE_RUN_ID="${RUN_ID}"
exec >> "${LOG_DIR}/${RUN_ID}.log" 2>&1   # merge stderr into stdout

echo "========================================"
echo "  Validation job started : $(date)"
echo "  Layer 1 — Outcome assignment (matured predictions)"
echo "  Layer 2 — Rolling metrics recomputation"
echo "========================================"

cd /Users/sumanthboyina/portfolio-agent
/Users/sumanthboyina/portfolio-agent/.venv/bin/python -u main.py --validate

echo "  Finished : $(date)"
