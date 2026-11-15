@echo off
rem Copies the old stores' data (C:\EldoradoBots\Eldorado API - *) into legacy\ for the
rem importers and the daily comparison. The old folders are only read.
pushd "%~dp0app\backend"
"%~dp0venv\Scripts\python.exe" -m app.deploy.legacy_snapshot --root "%~dp0." --source "C:\EldoradoBots"
popd
pause
