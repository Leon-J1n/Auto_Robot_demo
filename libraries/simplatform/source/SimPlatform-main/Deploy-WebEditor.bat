@echo off
setlocal EnableExtensions

rem Install Python service dependencies and start the local WebEditor.
pushd "%~dp0"
where python >nul 2>&1 || (
    echo ERROR: Python 3.10+ was not found on PATH.
    popd
    exit /b 1
)
echo Installing Python WebEditor dependencies ...
python -m pip install -e ".[web]"
if errorlevel 1 (
    echo ERROR: Python dependency installation failed.
    popd
    exit /b 1
)

echo Starting WebEditor services ...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-WebEditor-Production.ps1"
set "RESULT=%ERRORLEVEL%"
popd
exit /b %RESULT%
