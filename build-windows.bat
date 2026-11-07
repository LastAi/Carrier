@echo off
rem Builds a standalone Carrier folder (Carrier.exe + carrier-cli.exe)
rem into dist\ - no Python needed on the machines you copy it to.
setlocal
cd /d "%~dp0"
if not exist ".venv\.carrier-installed" (
  echo Run Carrier.bat once first to set things up.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install --quiet -e ".[build]" || goto :fail
".venv\Scripts\python.exe" packaging\build.py %* || goto :fail
echo.
echo Finished. Your build is in the dist folder.
pause
exit /b 0
:fail
echo Build failed - see the messages above.
pause
exit /b 1
