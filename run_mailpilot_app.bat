@echo off
setlocal
cd /d "%~dp0"

set "BOOTSTRAP=python"
where py >nul 2>nul && set "BOOTSTRAP=py -3"
%BOOTSTRAP% --version >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python 3 was not found.
  echo Install it from https://www.python.org/downloads/ and enable "Add Python to PATH".
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo First launch: creating a private MailPilot Python environment...
  %BOOTSTRAP% -m venv .venv
  if errorlevel 1 goto :error_venv
)

if not exist ".venv\.mailpilot-ready-v0.2.0" (
  echo First launch: installing dependencies. This needs internet and may take a few minutes...
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -e ".[dev]"
  if errorlevel 1 goto :error_install
  type nul > ".venv\.mailpilot-ready-v0.2.0"
)

if "%MAILPILOT_SMOKE%"=="1" (
  ".venv\Scripts\python.exe" -c "import app; response=app.app.test_client().get('/'); assert response.status_code == 200; print('MailPilot launcher smoke: HTTP', response.status_code)"
  if errorlevel 1 exit /b 1
  exit /b 0
)

".venv\Scripts\python.exe" app.py
if errorlevel 1 goto :error_app
exit /b 0

:error_venv
echo [ERROR] Could not create .venv. Send this window to your maintainer.
pause
exit /b 1

:error_install
echo [ERROR] Dependency installation failed. Check the network and send this window to your maintainer.
pause
exit /b 1

:error_app
echo [ERROR] MailPilot stopped unexpectedly. Send this window to your maintainer.
pause
exit /b 1
