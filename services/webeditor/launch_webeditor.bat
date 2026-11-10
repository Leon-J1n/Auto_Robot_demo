@echo off
rem Start the bundled WebEditor gateway from the robot-agent services directory.
cd /d "%~dp0"
python local_gateway.py --port 8099 %*
pause
