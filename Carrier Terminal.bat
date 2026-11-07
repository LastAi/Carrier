@echo off
rem Opens Debrief (Carrier's review tool) in the terminal for a project folder.
rem Usage: drag a project folder onto this file, or run:
rem        "Carrier Terminal.bat" C:\path\to\project
setlocal
set "HERE=%~dp0"
if not exist "%HERE%.venv\.carrier-installed" (
  echo Run Carrier.bat once first to set things up.
  pause
  exit /b 1
)
if "%~1"=="" (set "PROJECT=%CD%") else (set "PROJECT=%~1")
cd /d "%PROJECT%" || exit /b 1
"%HERE%.venv\Scripts\python.exe" -m carrier debrief %2 %3 %4 %5
pause
