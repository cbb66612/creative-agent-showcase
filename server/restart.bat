@echo off
chcp 65001 >nul
title Restart ju-ma-li server
cd /d "%~dp0"
call "%~dp0stop.bat" >nul 2>nul
timeout /t 1 >nul
start "" cmd /c ""%~dp0start.bat""
echo   Restart requested - look at the new console window.
timeout /t 2 >nul
