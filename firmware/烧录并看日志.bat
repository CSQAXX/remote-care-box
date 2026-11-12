@echo off
chcp 936 >nul
cd /d "%~dp0"
echo ==========================================================
echo  Burn firmware (with "xiao dian nao") + open serial log
echo ==========================================================
echo.
echo  Checklist before running:
echo    1. Board is plugged in by USB
echo    2. Close any other serial tool / monitor window
echo.
echo  After burning, the log window opens.
echo  Say:  "xiao dian nao, da kai wei xin"
echo  Press  Ctrl + ]  to quit the log window.
echo.
pause
call idf_build.bat flash monitor
echo.
echo ---- log closed ----
pause
