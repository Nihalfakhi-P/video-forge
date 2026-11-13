@echo off
title VideoForge - keep this window open while you use it
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo VideoForge is not installed yet. Double-click Install.bat first.
  pause
  exit /b 1
)
echo Starting VideoForge... your browser will open at http://127.0.0.1:8321
echo Close this window to stop VideoForge.
start "" /b cmd /c "timeout /t 5 /nobreak >nul & start http://127.0.0.1:8321"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1"
pause
