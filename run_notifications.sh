#!/bin/bash
# Notification delivery wrapper — run every few minutes (see README). Delivers due outbox rows (email / phone push).
# Routes output to YYYYMMDD_HHMMSS_notifications.log like the other job wrappers.
TS=$(date +%Y%m%d_%H%M%S)
LOG_DIR="/Users/sumanthboyina/portfolio-agent/logs"
mkdir -p "$LOG_DIR"

exec >> "${LOG_DIR}/${TS}_notifications.log" 2>&1   # merge stderr into stdout

cd /Users/sumanthboyina/portfolio-agent
/Users/sumanthboyina/portfolio-agent/.venv/bin/python -u -m scripts.send_notifications
