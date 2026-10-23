@echo off
setlocal
cd /d "%~dp0"
echo Starting in console mode. Keep this window open while using the app.
python "%~dp0app.py"
if errorlevel 1 (
    echo.
    echo The app exited with an error. See the message above.
    pause
)
