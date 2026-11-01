@echo off
chcp 65001 >nul
title Stop ju-ma-li server (safe: only the python process)
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
if exist "runtime\python.exe" (set "PY=runtime\python.exe") else (set "PY=python")

echo.
echo   Looking for the process on port 8000 ...
set "PORT_PID="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do if not defined PORT_PID set "PORT_PID=%%P"

if not defined PORT_PID (
  echo   Nothing is listening on port 8000.
  echo.
  pause
  exit /b 0
)

set "IMG="
for /f "tokens=1" %%N in ('tasklist /FI "PID eq %PORT_PID%" /NH') do if not defined IMG set "IMG=%%N"
echo   PID=%PORT_PID%  image=%IMG%

echo %IMG% | findstr /i "python" >nul
if errorlevel 1 (
  echo.
  echo   [cancelled] That process is not python - maybe a browser or something else.
  echo   This script will not kill it. Please check by hand.
  echo.
  pause
  exit /b 1
)

taskkill /F /PID %PORT_PID% >nul 2>nul
if errorlevel 1 (
  echo   It may have already exited.
) else (
  echo   Stopped PID=%PORT_PID%.
)
echo.
pause
