#!/bin/bash
# News-only job wrapper — routes output to YYYYMMDD_HHMMSS_news.log (same format as web UI)
TS=$(date +%Y%m%d_%H%M%S)
LOG_DIR="/Users/sumanthboyina/portfolio-agent/logs"
mkdir -p "$LOG_DIR"

exec >> "${LOG_DIR}/${TS}_news.log" 2>&1   # merge stderr into stdout

echo "========================================"
echo "  News-only job started : $(date)"
echo "========================================"

cd /Users/sumanthboyina/portfolio-agent
/Users/sumanthboyina/portfolio-agent/.venv/bin/python -u main.py --daily-news

echo "  Finished : $(date)"
