@echo off
cd /d "%~dp0"
echo Jev Arena notifier: (re)starts in the background. The panel is not restarted.
py -3.12 -B -X utf8 "%~dp0tools\arena_notifier.py" restart
pause
