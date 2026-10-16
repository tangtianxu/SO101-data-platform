@echo off
setlocal
cd /d "%~dp0"
set "SO101_SETUP_ROOT=%~dp0workspace"
if defined SO101_WORKSPACE set "SO101_SETUP_ROOT=%SO101_WORKSPACE%"
if not exist "%SO101_SETUP_ROOT%\temp" mkdir "%SO101_SETUP_ROOT%\temp"
set "TEMP=%SO101_SETUP_ROOT%\temp"
set "TMP=%TEMP%"
set "PIP_CACHE_DIR=%SO101_SETUP_ROOT%\cache\pip"
if not exist ".venv-ui\Scripts\python.exe" (
  py -3.12 -m venv .venv-ui
  if errorlevel 1 (
    echo Install Python 3.12 first, or reuse an existing UI environment via SO101_UI_PYTHON.
    pause
    exit /b 1
  )
)
".venv-ui\Scripts\python.exe" -m pip install -r requirements-ui.txt
if errorlevel 1 exit /b 1
echo UI environment installed. Launch with launch.cmd. No devices were connected.
pause
