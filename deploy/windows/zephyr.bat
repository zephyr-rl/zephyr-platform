@echo off
rem Administrative commands with this install's settings (run as administrator), for example:
rem   zephyr.bat compare-shadow --store zephyrstore --folder "legacy\Eldorado API - ZephyrStore"
setlocal
set "PYTHONPATH=%~dp0app\backend"
"%~dp0venv\Scripts\python.exe" -m app.deploy.run --root "%~dp0." %*
exit /b %errorlevel%
