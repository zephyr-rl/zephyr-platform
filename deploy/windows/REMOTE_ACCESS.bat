@echo off
title Zephyr Platform - link for the phone
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\tunnel.ps1" -Root "%~dp0."
pause
