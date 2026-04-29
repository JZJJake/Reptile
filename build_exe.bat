@echo off
chcp 65001 >nul
title 网页全量信息提取客户端 - 一键打包为EXE

echo ===================================================
echo.
echo     正在将客户端打包为独立的 .exe 桌面应用程序
echo     这可能需要几分钟时间，请耐心等待...
echo.
echo ===================================================

REM 检查并安装打包所需的依赖
echo [1/2] 检查 PyInstaller 环境...
pip install pyinstaller -i https://pypi.tuna.tsinghua.edu.cn/simple

echo.
echo [2/2] 开始打包...
python build.py

echo.
echo ===================================================
echo [完成] 打包结束！
echo 您可以在当前目录下的 "dist/WebScraperClient" 文件夹中找到您的程序。
echo.
echo 部署说明：
echo 您只需要将 "dist/WebScraperClient" 整个文件夹拷贝到 Windows Server 2023 即可。
echo 然后在服务器上双击运行里面的 WebScraperClient.exe
echo ===================================================

pause