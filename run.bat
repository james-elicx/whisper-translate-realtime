@echo off
rem whisper-translate-realtime: live English captions for a program's audio
rem (cs2.exe by default). Extra options pass through, e.g.:
rem   run.bat --show-original
rem   run.bat --model D:\models\faster-whisper-large-v3
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo No Python environment found - run setup.bat first.
  pause
  exit /b 1
)
"%PY%" whisper_translate.py %*
if errorlevel 1 pause
