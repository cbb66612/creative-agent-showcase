@echo off
chcp 936 >nul
cd /d "%~dp0"
echo ==========================================================
echo  TV 后台专用登录窗口
echo  这个窗口有自己的浏览器数据（lan\data\chrome-tv），
echo  只用来登录一次 TV 后台；登录后 Cookie 会一直留着。
echo  登录完不用关窗口，双击 同步TV.bat 即可。
echo ==========================================================
echo.
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --user-data-dir="%~dp0data\chrome-tv" --remote-debugging-port=9335 --no-first-run "https://dz-ailab.dzkjm.cn/admin-web/presets"
echo 已打开浏览器。登录完成后，关掉这个黑窗口，双击 同步TV.bat。
pause
