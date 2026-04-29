@echo off
title Build Web Scraper Client EXE

echo ===================================================
echo     Building Web Scraper Client...
echo ===================================================

REM Check Python environment
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not in PATH.
    pause
    goto :EOF
)

echo Installing build dependencies...
python -m pip install pyinstaller -i https://pypi.tuna.tsinghua.edu.cn/simple

echo Starting build...
python build.py

pause
