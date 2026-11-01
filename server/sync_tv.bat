@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
if exist "runtime\python.exe" (set PY=runtime\python.exe) else (set PY=python)
echo ============================================================
echo  TV 后台 -^> 本地库 同步
echo  1) 干跑看看会改动什么：   sync_tv.bat --dry-run
echo  2) 正式同步：             sync_tv.bat
echo  3) 探接口字段：           sync_tv.bat --probe
echo ============================================================
echo.
if "%~1"=="" (
  "%PY%" sync_tv.py
) else (
  "%PY%" sync_tv.py %*
)
echo.
pause
