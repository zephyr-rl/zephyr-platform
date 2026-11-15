@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\check.ps1" -Root "%~dp0."
pause
