@echo off
rem Launch the ObjectLibraryServer. Any arguments (e.g. --port 8080 --host 0.0.0.0)
rem override values from config.json.
cd /d "%~dp0"
python server.py %*
if errorlevel 1 pause
