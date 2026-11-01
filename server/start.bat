@echo off
chcp 65001 >nul
title 创作资源工作台 · Skill 预设库（局域网服务）
cd /d "%~dp0"

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
set PORT=8012

set "PY="
if exist "runtime\python.exe" set "PY=runtime\python.exe"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  echo.
  echo   没有找到 Python。
  echo   正常情况下这个文件夹里带 runtime\python.exe，不用安装任何东西；
  echo   如果它缺失，请到 python.org 安装 Python 3.10 以上，再双击本文件。
  echo.
  pause
  exit /b 1
)

echo.
echo  ============================================================
echo     创作资源工作台 · Skill 预设库     局域网服务启动中
echo  ============================================================
echo.
echo    本机打开：      http://127.0.0.1:8012
echo    其他设备打开：http://本机IP:8012   （下面的地址里挑一个）
echo.
echo    关闭这个黑窗口 = 停止服务。
echo  ============================================================
echo.

%PY% app.py

echo.
echo  服务已停止。按任意键关闭窗口。
pause >nul
