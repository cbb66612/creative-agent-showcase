@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
if not exist "data\logs" mkdir "data\logs"

echo [%date% %time%] sync start >> "data\logs\task.log"

start "" "%CHROME%" --no-first-run --remote-debugging-port=9335 --user-data-dir="%~dp0data\chrome-tv" "https://dz-ailab.dzkjm.cn/admin-web/presets"

ping -n 16 127.0.0.1 >nul

"%~dp0runtime\python.exe" "%~dp0sync_all.py" >> "data\logs\task.log" 2>&1
echo [%date% %time%] sync end rc=%ERRORLEVEL% >> "data\logs\task.log"
