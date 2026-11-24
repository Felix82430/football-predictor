@echo off
chcp 65001 >nul
title 足球比分量化情报与复盘系统
cd /d "%~dp0"
echo ========================================================
echo   足球比分量化情报与赛后复盘系统 (五大联赛 + 欧冠)
echo   正在启动本地服务：http://127.0.0.1:8765
echo   2秒后将自动打开浏览器，关闭此黑色窗口即可停止服务
echo ========================================================
start "" /b cmd /c "timeout /t 2 /nobreak >nul && start http://127.0.0.1:8765"
".venv\Scripts\python.exe" cli.py serve --port 8765
pause
