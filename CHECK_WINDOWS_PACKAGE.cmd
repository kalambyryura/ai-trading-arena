@echo off
cd /d "%~dp0"
py -3.12 -B -X utf8 "%~dp0deployment\server\run_server.py" check
pause
