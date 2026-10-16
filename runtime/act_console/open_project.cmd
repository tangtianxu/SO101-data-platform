@echo off
setlocal
if exist "E:\software\vscode\Microsoft VS Code\Code.exe" (
  "E:\software\vscode\Microsoft VS Code\Code.exe" "%~dp0so101-real-act.code-workspace"
) else (
  echo Open this folder in VS Code: %~dp0
  pause
)
endlocal
