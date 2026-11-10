@echo off
rem Spec 11.6: double-click entry for a release bundle. The gateway serves dist/
rem itself, so no Node and no Vite are involved here. Extra arguments are passed
rem through, e.g. launch_gateway.bat --port 9000.
cd /d "%~dp0"
python local_gateway.py %*
pause
