@echo off
setlocal
cd /d "%~dp0"
echo Starting LocalASR V2 initializer...
python setup_wizard.py
if errorlevel 1 (
    echo.
    echo The initializer exited with an error. See the message above.
    pause
)
