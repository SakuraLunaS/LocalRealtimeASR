@echo off
setlocal
cd /d "%~dp0"
echo ============================================================
echo  LocalASR V2 - dependency installer
echo  Python 3.10 x64 and an NVIDIA driver are required.
echo ============================================================
python --version
if errorlevel 1 (
    echo ERROR: Python was not found on PATH.
    pause
    exit /b 1
)
python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 (
    echo.
    echo Dependency installation failed. Check the network and the message above.
    pause
    exit /b 1
)
echo.
echo Dependencies installed.
echo The initializer will now open. Configure an optional SOCKS5 proxy,
echo then download and verify the model (about 1.62 GB).
echo.
python setup_wizard.py
if errorlevel 1 (
    echo.
    echo The initializer exited with an error. See the message above.
    pause
    exit /b 1
)
echo.
echo Setup finished. Start the app with run.bat.
pause
