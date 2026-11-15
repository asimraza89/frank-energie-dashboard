@echo off
REM First run creates a private Python environment next to the app, then starts it.
cd /d "%~dp0"
if not exist .venv (
  py -3 -m venv .venv
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\pip install -r requirements.txt
)
.venv\Scripts\streamlit run app.py --server.address 127.0.0.1 --browser.gatherUsageStats false
