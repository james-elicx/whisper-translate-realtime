@echo off
rem whisper-translate-realtime: live English captions for all system audio,
rem shown in this console. Extra options pass through, e.g.:
rem   run.bat --show-original
rem   run.bat --process cs2.exe --overlay
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo No Python environment found - run setup.bat first.
  pause
  exit /b 1
)
"%PY%" whisper_translate.py %*
if errorlevel 1 pause
