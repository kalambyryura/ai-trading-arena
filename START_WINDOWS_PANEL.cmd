@echo off
cd /d "%~dp0"
echo Open http://127.0.0.1:8765/arena after startup.
echo This opens the panel only. Start the prepared Live round yourself.
echo Keep this window open. Disconnecting RDP is OK; do not log off or shut down.
py -3.12 -B -X utf8 "%~dp0deployment\server\run_server.py" serve --port 8765
if errorlevel 1 echo Panel did not start. Follow START_HERE_LIVE_UA.md and preserve all files.
pause
