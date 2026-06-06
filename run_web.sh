#!/bin/bash
# Launch the Portfolio Agent web dashboard
cd /Users/sumanthboyina/portfolio-agent
.venv/bin/streamlit run web/app.py \
    --server.port 8501 \
    --browser.gatherUsageStats false
