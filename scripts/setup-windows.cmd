@echo off
setlocal
cd /d "%~dp0.."
py -3.12 -m venv .venv
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m pip install -r requirements-tested.txt
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m pip install -e . --no-deps
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m unittest discover -s tests -v
exit /b %errorlevel%
