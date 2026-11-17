@echo off
setlocal
REM setup.bat [--dev]: CPU bootstrap first, then local runtime/pipeline or tests.
chcp 65001 >nul
cd /d "%~dp0"
if not "%~1"=="" if /i not "%~1"=="--dev" (
  echo Usage: setup.bat [--dev]
  exit /b 2
)
echo [1/5] Checking Python ...
where python >nul 2>nul || goto :fail
python -c "import sys; assert sys.version_info >= (3,11), 'Python 3.11+ required'; print(sys.version)" || goto :fail
echo [2/5] Preparing .venv ...
if not exist ".venv\Scripts\python.exe" python -m venv .venv
if errorlevel 1 goto :fail
set "PYTHON=%~dp0.venv\Scripts\python.exe"
"%PYTHON%" -m pip install --upgrade pip || goto :fail
echo [3/5] Installing CPU torch once ...
"%PYTHON%" -m pip install -r requirements-cpu.txt || goto :fail
echo [4/5] Installing runtime and offline tools ...
if /i "%~1"=="--dev" goto :dev
"%PYTHON%" -m pip install -r requirements.txt || goto :fail
goto :check
:dev
"%PYTHON%" -m pip install -r requirements-dev.txt || goto :fail
:check
echo [5/5] Checking dependencies ...
"%PYTHON%" -m pip check || goto :fail
echo Ready. Set FLOOD_ADMIN_PASSWORD before first login, or FLOOD_DEMO=1 for local demo only.
echo Run run.bat to start.
exit /b 0
:fail
echo Setup failed. Fix the error above before starting the service.
exit /b 1
