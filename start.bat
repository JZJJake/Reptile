@echo off
title Web Scraper Client

echo ===================================================
echo     Starting Web Scraper Client...
echo ===================================================

REM Check Python environment
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not in PATH.
    echo Please install Python 3.11 from https://www.python.org/downloads/
    pause
    goto :EOF
)

echo [1/3] Installing Python dependencies...
python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
if %errorlevel% neq 0 (
    echo [WARNING] Failed to install some dependencies.
) else (
    echo [SUCCESS] Dependencies installed.
)

echo.
echo [2/3] Installing Playwright browsers...
python -m playwright install chromium
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install Playwright browser.
    pause
    goto :EOF
) else (
    echo [SUCCESS] Browser installed.
)

echo.
echo [3/3] Starting the application server...
echo The application should automatically open in your default browser.
echo If not, please open http://127.0.0.1:8000
echo.
echo DO NOT close this window.
echo ===================================================

python main.py

pause
