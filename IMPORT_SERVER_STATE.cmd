@echo off
if "%~1"=="" (
  echo Drag JevArena-state.private.zip onto this file, or pass its full path.
  pause
  exit /b 2
)
py -3.12 "%~dp0tools\server_transfer.py" import --root "%~dp0." --archive "%~1" --source-stopped
pause
