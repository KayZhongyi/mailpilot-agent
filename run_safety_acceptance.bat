@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Run run_mailpilot_app.bat once before the safety acceptance check.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -c "import pytest, ruff" >nul 2>nul
if errorlevel 1 (
  echo Acceptance tools are missing. Go online and run run_mailpilot_app.bat once to update the environment.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" scripts\safety_acceptance.py
set "STATUS=%ERRORLEVEL%"
pause
exit /b %STATUS%
