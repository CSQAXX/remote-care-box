@echo off
call "D:\Development Environment\ESP-IDF\esp-idf\export.bat"
if errorlevel 1 exit /b 1
cd /d "D:\Software\kanhu\remote-care-box\firmware"
idf.py %*
