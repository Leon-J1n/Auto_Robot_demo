@echo off
cd /d "%~dp0"
python init_index.py %*
if errorlevel 1 pause