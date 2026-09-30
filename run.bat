@echo off
rem ============================================================
rem  UM982 双天线 RTK 上位机 —— 启动脚本
rem
rem  用法：
rem    run.bat                    启动界面，在网页下拉框中选串口后点「连接」
rem    run.bat --serial COM10      启动并自动连接 COM10（零点击）
rem    run.bat --sim               用内置模拟器演示（无需硬件）
rem    run.bat --replay fixtures\um982_raw.txt   回放抓取到的真实报文
rem    run.bat --list-ports        只列出串口后退出
rem ============================================================
setlocal
cd /d "%~dp0"

set PY=python
where python >nul 2>nul
if errorlevel 1 (
  where py >nul 2>nul
  if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.9+ 并加入 PATH。
    echo        下载地址：https://www.python.org/downloads/
    pause
    exit /b 1
  )
  set PY=py -3
)

%PY% -c "import serial" >nul 2>nul
if errorlevel 1 (
  echo [提示] 缺少 pyserial，正在自动安装...
  %PY% -m pip install -r requirements.txt
  if errorlevel 1 (
    echo [错误] pyserial 安装失败，请手动执行：python -m pip install pyserial
    pause
    exit /b 1
  )
)

%PY% -m app.server %*
if errorlevel 1 (
  echo.
  echo [启动失败] 请检查上方错误信息。
  pause
)
endlocal
