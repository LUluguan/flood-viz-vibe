@echo off
setlocal
REM Local watchdog: Ctrl+C stops it. Use the project venv, not host site-packages.
cd /d "%~dp0.."
set "PYTHON=%CD%\.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
  echo Missing project virtual environment. Run setup.bat first.
  exit /b 1
)
if "%FLOOD_PORT%"=="" set "FLOOD_PORT=8001"
:loop
echo [%date% %time%] Starting http://127.0.0.1:%FLOOD_PORT%/
"%PYTHON%" -m uvicorn app:app --host 127.0.0.1 --port %FLOOD_PORT%
echo [%date% %time%] Service exited. Restarting in 3 seconds...
timeout /t 3 /nobreak >nul
goto loop
