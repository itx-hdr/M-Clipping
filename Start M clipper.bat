@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
  set "MCLIPPER_PYTHON=.venv\Scripts\python.exe"
) else (
  set "MCLIPPER_PYTHON=python"
)

start "M clipper dashboard" /min cmd /c "timeout /t 3 /nobreak ^>nul ^& start http://127.0.0.1:8765"
"%MCLIPPER_PYTHON%" -m studio.server
endlocal
