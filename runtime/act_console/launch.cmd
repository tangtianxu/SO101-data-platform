@echo off
setlocal
chcp 65001 >nul
set "PYTHONNOUSERSITE=1"
set "PYTHONDONTWRITEBYTECODE=1"
set "PYTHONPATH="
cd /d "%~dp0"
"E:\robot\envs\lerobot\python.exe" -s -B "%~dp0run.py" %*
if errorlevel 1 pause
endlocal
