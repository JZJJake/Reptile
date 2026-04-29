@echo off
chcp 65001 >nul
title 网页全量信息提取客户端 - 启动程序

echo ===================================================
echo.
echo     欢迎使用网页全量信息提取客户端
echo     正在为您检查并配置运行环境...
echo.
echo ===================================================

REM 检查 Python 是否安装
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未检测到 Python，请先安装 Python 3.11 及以上版本。
    echo 请前往 https://www.python.org/downloads/ 下载并安装（安装时请勾选 "Add Python to PATH"）。
    pause
    exit /b
)

echo [1/3] 正在安装或更新 Python 依赖包...
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
if %errorlevel% neq 0 (
    echo [警告] 依赖包安装可能出现问题，但我们将尝试继续...
) else (
    echo [成功] 依赖包检查完毕！
)

echo.
echo [2/3] 正在检查 Playwright 浏览器内核...
REM 检查是否已经安装了 chromium 内核，如果没有则安装
python -m playwright install chromium
if %errorlevel% neq 0 (
    echo [错误] Playwright 浏览器内核安装失败，请检查网络连接！
    pause
    exit /b
) else (
    echo [成功] 浏览器内核检查完毕！
)

echo.
echo [3/3] 正在启动客户端服务...
echo 提示：启动后，您的默认浏览器将会自动打开操作界面。
echo 如果没有自动打开，请手动在浏览器访问：http://127.0.0.1:8000
echo.
echo 请不要关闭此黑色窗口，关闭窗口将会停止运行！
echo ===================================================

REM 运行主程序
python main.py

pause