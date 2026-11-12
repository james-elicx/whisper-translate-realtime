@echo off
rem Creates .venv next to this file and installs the Python packages (about 1 GB).
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo The Python launcher "py" was not found. Install Python 3.12 or newer from python.org.
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo Creating Python environment in .venv
  py -3 -m venv .venv
  if errorlevel 1 exit /b 1
)
echo Installing packages, about 1 GB the first time
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 exit /b 1
echo.
echo Setup done. Start it with run.bat
