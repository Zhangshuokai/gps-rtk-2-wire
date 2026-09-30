@echo off
rem 用内置模拟器启动（无需硬件，用于验证界面/演示）
setlocal
cd /d "%~dp0"
python -m app.server --sim --no-browser %*
pause
endlocal
