@echo off
chcp 936 >nul
cd /d "%~dp0"
echo [1/2] 从专用浏览器窗口抓数据 ...
runtime\python.exe tv_dump.py
if errorlevel 1 goto fail
echo.
echo [2/2] 写进本地库 ...
runtime\python.exe sync_tv.py --from-dir data\tv_raw
echo.
echo 完成。回浏览器刷新 资源库 - TV 预设 就能看到。
pause
exit /b 0
:fail
echo.
echo 抓取失败。先双击 tv_login.bat 登录一次，再回来跑这个。
pause
