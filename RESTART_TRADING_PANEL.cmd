@echo off
powershell.exe -NoProfile -File "%~dp0tools\restart_panel.ps1"
if errorlevel 1 pause
