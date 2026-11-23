@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 优先用项目自带的虚拟环境，没有就用系统 python
set PY=..\ok-bsg\.venv\Scripts\python.exe
if not exist "%PY%" set PY=python
"%PY%" -m workbench
if errorlevel 1 pause
