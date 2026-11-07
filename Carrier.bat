@echo off
rem ======================================================================
rem  Carrier for Windows - double-click to start.
rem
rem  First run: creates a private Python environment in .venv and installs
rem  Carrier into it (about a minute). Later runs start straight away.
rem  Needs Python 3.11+ (https://www.python.org/downloads/ - tick
rem  "Add python.exe to PATH" or use the "py" launcher), Git, and Claude Code.
rem ======================================================================
setlocal
cd /d "%~dp0"

set "PY=py -3"
where py >nul 2>nul || set "PY=python"
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if errorlevel 1 (
  echo Carrier needs Python 3.11 or newer.
  echo Download it from https://www.python.org/downloads/ and run this again.
  pause
  exit /b 1
)

rem ".venv\.carrier-installed" is written only after a successful install, so a
rem failed or interrupted first run is retried next time instead of
rem silently starting a half-installed app.
if not exist ".venv\.carrier-installed" (
  echo Setting up Carrier - this happens only once...
  if not exist ".venv\Scripts\python.exe" ( %PY% -m venv .venv || goto :fail )
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip || goto :fail
  rem Remove the pre-rename "debrief" package if an older version was installed.
  ".venv\Scripts\python.exe" -m pip uninstall --yes --quiet debrief >nul 2>nul
  ".venv\Scripts\python.exe" -m pip install --quiet -e . || goto :fail
  ".venv\Scripts\python.exe" -c "import carrier.desktop.main, webview" || goto :fail
  echo ok> ".venv\.carrier-installed"
  echo Done.
)

rem pythonw = no console window behind the app. Errors go to
rem %APPDATA%\Carrier\desktop.log (and a message box for startup problems).
start "" ".venv\Scripts\pythonw.exe" -m carrier.desktop
exit /b 0

:fail
echo.
echo Setup failed - see the messages above.
pause
exit /b 1
