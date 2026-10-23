@echo off
setlocal
cd /d "%~dp0"
where pythonw.exe >nul 2>nul
if errorlevel 1 (
    echo pythonw.exe was not found on PATH. Install Python 3.10 and dependencies first.
    pause
    exit /b 1
)
start "" pythonw.exe "%~dp0app.py"
