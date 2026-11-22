@echo off
setlocal
cd /d "%~dp0"
title BSG CoreProbeTask
set "PY=%~dp0..\ok-bsg\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -m ok run_task -c src.config:config CoreProbeTask
echo.
echo [Done] exit=%ERRORLEVEL%
pause
