@echo off
cd /d "%~dp0"
echo Jev Arena notifier: binds your Telegram chat with a one-time code, then sends one test message.
echo Follow NOTIFIER_TELEGRAM_UA.md. It never touches orders, OKX keys or rounds.
py -3.12 -B -X utf8 "%~dp0tools\arena_notifier.py" find-chat
if errorlevel 1 goto done
py -3.12 -B -X utf8 "%~dp0tools\arena_notifier.py" test
:done
pause
