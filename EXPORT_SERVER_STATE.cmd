@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0deployment\server\export_from_pc.ps1"
pause
