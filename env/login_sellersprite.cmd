@echo off
cd /d "%~dp0.."
if not exist "env\.venv\Scripts\python.exe" (
  echo Run powershell -File env\setup.ps1 first.
  pause
  exit /b 2
)
"env\.venv\Scripts\python.exe" run_seller_vat.py login
pause
