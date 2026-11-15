#!/usr/bin/env bash
# First run creates a private Python environment next to the app, then starts it.
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -r requirements.txt
fi
exec .venv/bin/streamlit run app.py --server.address 127.0.0.1 --browser.gatherUsageStats false
