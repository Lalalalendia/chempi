@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
 echo Python launcher not installed. Install Python 3.11 or newer.
 pause
 exit /b 1
)
if not exist ".venv\Scripts\python.exe" py -3 -m venv .venv
if errorlevel 1 (
 echo Could not create Python environment.
 pause
 exit /b 1
)
call .venv\Scripts\python.exe -m pip install -e ".[gui]"
if errorlevel 1 (
 echo Install failed. Check internet connection and Python version.
 pause
 exit /b 1
)
call .venv\Scripts\python.exe -m chempi gui
if errorlevel 1 pause
