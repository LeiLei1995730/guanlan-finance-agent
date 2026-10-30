@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo First run: python -m venv .venv
  echo Then: .venv\Scripts\python.exe -m pip install -r requirements.txt
  pause
  exit /b 1
)
set COLLECT_INTERVAL_MINUTES=360
start http://127.0.0.1:18765
.venv\Scripts\python.exe -m finance_agent serve
