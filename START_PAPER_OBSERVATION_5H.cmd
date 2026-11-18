@echo off
setlocal
set "PAPER_ROOT=%~dp0runtime\forward_paper_2026_10_26\source_71d669a"
if not exist "%PAPER_ROOT%\paper-isolation.json" (
  echo Pinned PAPER package is missing. No process started.
  pause
  exit /b 1
)
echo Five-hour PAPER observation: four frozen V1 recipes, public market data only.
echo No real orders, account keys or paid Jev. Keep this window open.
echo Viewer after startup: http://127.0.0.1:8766/arena
py -3.12 -B -X utf8 "%PAPER_ROOT%\tools\run_isolated_paper.py" --root "%PAPER_ROOT%" --port 8766
echo PAPER process ended. Existing rounds are never restarted automatically.
pause
