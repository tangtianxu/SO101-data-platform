@echo off
set "PYTHONNOUSERSITE=1"
set "PYTHONDONTWRITEBYTECODE=1"
if defined SO101_UI_PYTHON (
  start "" /D "%~dp0" "%SO101_UI_PYTHON%" -s -B -X utf8 "%~dp0desktop.py" --offline %*
  exit /b
)
if exist "%~dp0.venv-ui\Scripts\pythonw.exe" (
  start "" /D "%~dp0" "%~dp0.venv-ui\Scripts\pythonw.exe" -s -B -X utf8 "%~dp0desktop.py" --offline %*
  exit /b
)
echo UI environment missing. Run setup-ui.cmd first, or set SO101_UI_PYTHON to an existing pywebview Python.
pause
